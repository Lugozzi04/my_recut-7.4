# Boundary Analysis Layer — confini editoriali attorno alle partite

9 ottobre 2026. Infrastruttura per decidere *dove* tagliare una partita da rimuovere. Produce **candidati e feature**, non decisioni. Unico resolver implementato: `ExactBoundaryResolver` (baseline: VS → risultato).

Fuori scope in questa iterazione:
- nessun modello addestrato e nessun PyTorch;
- nessuna chiamata Claude/Codex;
- nessuna policy smart;
- nessun gameplay cut integrato;
- nessuna modifica a timeline/UI/exporter.

Gameplay Detector invariato: i 49 hash del freeze videoprova sono identici dopo questa iterazione.

## 1. Architettura

```text
GameplayAnalysisResult (già salvato, detector NON rieseguito)
   │  analysis/boundaries/gameplay.py   GameplayTimeline: anchor, partite vicine, marker board,
   │                                    termination + disposition (REMOVE / REMOVE_TO_EOF / KEEP)
   ▼
search windows START / END          windows.py  (asimmetriche, mai oltre partite vicine o file)
   ▼
evidence locale per finestra
   ├─ audio.py       RMS locale → silenzi (motore silenzio dell'editor) → speech
   ├─ transcript.py  faster-whisper opzionale, una sola chiamata per START+END, cache
   ├─ visual.py      frame-difference a 2 FPS, 160 px (sampler gameplay esistente)
   └─ gameplay.py    BOARD_DISAPPEARED / BOARD_REACQUIRED dai metadata state-aware
   ▼
candidates.py   candidati da tutte le sorgenti → dedup → feature numeriche + evidence raw
   ▼
models.py       BoundaryContext (JSON, versionato) ── resolvers.py  ExactBoundaryResolver
                                                     ├─ agent_payload / parse_agent_response (contratto futuro)
                                                     └─ dataset.py   esempi JSONL + righe feature
analyzer.py     BoundaryAnalyzer: orchestrazione, cancellazione, profiling (nessun Qt)
__main__.py     CLI di debug
```

Separazione delle responsabilità:
- Hearthstone fornisce solo anchor e metadata già prodotti. Lo strato boundary non importa nulla del detector, tranne i modelli `GameplayAnalysisResult`/`GameSegment` e il sampler video generico.
- È UI-independent e cancellabile (`AnalysisCancellation`); usabile da CLI o da worker GUI.
- Import leggero: non carica Qt, torch o faster-whisper (verificato da un test).

File aggiunti: `analysis/boundaries/{__init__,__main__,models,gameplay,windows,audio,media,transcript,visual,candidates,analyzer,resolvers,dataset}.py`, `tests/test_boundaries.py`, questo documento, `docs/gameplay_validation/videoprova/boundaries/`.

File modificato: `analysis/audio_service.py`. Aggiunte retro-compatibili: seek opzionale in `_extract_rms`, `extract_rms_window()` pubblica, `auto_threshold_from_rms()` estratta dalla formula esistente, costante `VOICE_FILTER`. Il comportamento dell'analisi classica è identico.

## 2. BoundaryContext

Un context descrive UN confine (START o END) di UNA partita. Formato `recut_boundary_context` v1, JSON puro:

|Campo|Contenuto|
|---|---|
|`boundary_type`|`START` / `END`|
|`video`|path, durata, fingerprint, versione detector|
|`game`|id, index, start, end, result, `termination`, `disposition`, reason|
|`anchors`|`game_start`, `game_end`, `previous_game_end`, `next_game_start`, `end_of_file`|
|`window`, `window_clipped_by`|finestra analizzata e motivo dei tagli (partita vicina, inizio/fine file)|
|`transcript`|`status` (ok/cached/disabled/unavailable/failed, engine, lingue, opzioni) + segmenti con parole|
|`audio`|`status` (soglia, impostazioni) + `silences` + `speech`|
|`visual`|`status` + `samples` (timestamp, change)|
|`gameplay_markers`|marker board nella finestra|
|`candidates`|lista di `CandidateBoundary`|
|`feature_names`|ordine stabile delle feature|
|`settings`|anchor del context e tutte le impostazioni (versione `boundary-analysis-v1`)|

Ogni modalità dichiara il proprio stato: un dato assente non è mai fabbricato. Un resolver distingue così "nessun silenzio" (audio `ok`, lista vuota) da "audio non analizzato" (`failed`/`unavailable`).

## 3. CandidateBoundary

```python
CandidateBoundary(timestamp, boundary_type, sources: tuple[CandidateSource, ...],
                  evidence: dict,                 # raw: testi, regioni, timestamp fusi
                  features: dict[str, float|None])  # numeriche derivate; None = modalità non disponibile
```

- **`evidence`:** `transcript_segment_before`, `transcript_segment_after` e `transcript_segment_containing` (con testo), `silence_region`, `speech_region`, `merged_timestamps`, dettagli di ogni sorgente (ad esempio durata del silenzio, change visuale).
- **`features`:** sempre tutte le chiavi di `FEATURE_NAMES`.
- **`feature_vector()`:** restituisce `(values, mask)`; `None` diventa `0.0` con mask `0`, quindi il dato mancante resta esplicito.

## 4. START vs END

Restano due problemi distinti. `boundary_type` è nel context, in ogni candidato e nella feature `is_end_boundary`; finestre, anchor e sorgenti differiscono.

|Aspetto|START|END|
|---|---|---|
|Domanda|da quando il contenuto appartiene alla partita rimossa|quando finisce il commento della partita appena chiusa|
|Anchor|`GAME_START_ANCHOR` (VS)|`RESULT_ANCHOR` (banner), oppure `END_OF_FILE` per un terminal partial|
|Finestra di default|−25 s / +5 s|−5 s / +35 s|
|Limite|mai prima di `previous_game_end`|mai oltre `next_game_start`|
|Anchor extra|`PREVIOUS_GAME_END`, `START_OF_FILE`|`NEXT_GAME_START`, `END_OF_FILE`|

Default motivati e configurabili (`WindowSettings`, flag CLI `--start-before`, ecc.):
- prima del VS ci sono coda e "vediamo cosa troviamo";
- dopo il banner c'è il commento post-partita, mentre ciò che sta prima del banner appartiene alla partita.

Totale circa 70 s per partita rimossa, nell'ordine dei ~60 s per LOSS indicati come accettabili.

## 5. Transcript timestampato

Il repository non aveva ASR. È stato scelto **faster-whisper** (CTranslate2, non PyTorch):
- i modelli `tiny`, `base` e `small` erano già nella cache Hugging Face;
- il pacchetto c'è nel Python 3.10 di sistema, non nei venv del progetto.

L'integrazione segue lo stesso pattern di Spleeter/Silero in `ai_pipeline.py`:

|Aspetto|Comportamento|
|---|---|
|Ricerca del runtime|in-process se importabile, altrimenti interprete esterno da `AUTO_CUTTER_WHISPER_PY`, `--whisper-python` o dalle cartelle runtime candidate (stesse root di `_candidate_ai_roots`)|
|Audio|un WAV 16 kHz mono per finestra, estratto con input seek|
|Batching|START ed END in **un solo processo** (un solo caricamento del modello)|
|Modello|prima `local_files_only`, poi download|
|Segmenti|`TranscriptSegment(start, end, text, confidence = exp(avg_logprob), no_speech_probability, words)`|
|Timestamp|assoluti, limitati alla finestra|
|Parole|`word_timestamps=True`, per individuare i confini di frase|
|Allucinazioni|`vad_filter=True` le evita nei silenzi|
|Cache|JSON atomico per (identità video, finestra, opzioni engine) in `cache_root()/boundaries/transcripts`|
|Degrado|senza runtime lo stato è `unavailable` e le feature transcript sono `None`|

Lingua: default auto. Sulle finestre brevi l'auto-detect è inaffidabile: la START della partita 5 è stata rilevata come `en` (p=0.36) pur essendo italiana. Per questi VOD si usa `--language it`; le lingue per finestra sono riportate nello status. Nessuna keyword Hearthstone è usata come policy.

## 6. VAD / silence evidence

Nessun secondo sistema: si riusa il motore di silenzio dell'editor.
- **RMS:** `extract_rms_window` estrae SOLO la finestra (±1 s di padding, poi ritaglio), con lo stesso filtro voce e lo stesso framing (hop 0.03 s) di `analyze_audio`. Il padding evita che smoothing e attack/release partano "a freddo" sul bordo.
- **Silenzi:** `compute_cuts_from_rms` con padding/edge/aggressività neutri, cioè run di silenzio grezzi ≥ `min_silence`.
- **Parametri dal preset di default** ("Balanced", stessa derivazione di `compute_classic_cuts`): min_silence 0.48 s, smoothing Medium 120 ms, attack 120 ms, release 250 ms.
- **Soglia:** quella automatica dell'editor (`auto_threshold_from_rms`), calcolata sull'RMS locale. Una futura integrazione GUI può passare la soglia del progetto (`SilenceSettings.threshold`) per far coincidere i silenzi con quelli tagliati dall'editor.
- **Speech:** complemento dei silenzi nella finestra (motore `rms`), oppure Silero VAD opzionale tramite l'esistente `_run_silero_vad` (`--speech-engine silero`).

Feature audio: `silence_before_s`, `silence_after_s`, `speech_before_s`, `speech_after_s` (durate contigue adiacenti), `speech_density_before/after` (frazione di parlato in ±5 s), `is_inside_speech`, `is_inside_silence`, `rms_db_before/after` (1 s).

## 7. Visual evidence

`visual.py` riusa `FFmpegFrameSampler` a 2 FPS e 160 px di larghezza, solo dentro la finestra:
- change = 0.5 × differenza media assoluta in grigio + 0.5 × variazione totale dell'istogramma, in [0, 1];
- feature `visual_change_score` = massimo entro ±0.75 s;
- candidati `VISUAL_CHANGE` solo sui picchi locali ≥ 0.35.

Non è usato per decidere. I marker gameplay vengono dagli eventi state-aware già prodotti: `BOARD_DISAPPEARED` dai trigger delle result window, `BOARD_REACQUIRED` da `board_reacquisitions`. "Queue/menu transition" non esiste come evento del detector e non è stato inventato (vedi §20).

## 8. Feature numeriche (45, ordine stabile `FEATURE_NAMES`)

|Gruppo|Feature|
|---|---|
|Tipo/anchor|`is_end_boundary`, `offset_from_anchor_s`, `distance_from_game_start_s`, `distance_from_game_end_s`, `distance_to_previous_game_end_s`, `distance_to_next_game_start_s`, `nearest_gameplay_marker_s`|
|Finestra|`relative_window_position`, `distance_to_window_start_s`, `distance_to_window_end_s`|
|Audio|`silence_before_s`, `silence_after_s`, `speech_before_s`, `speech_after_s`, `speech_density_before`, `speech_density_after`, `is_inside_speech`, `is_inside_silence`, `rms_db_before`, `rms_db_after`|
|Transcript|`transcript_gap_before_s`, `transcript_gap_after_s`, `is_inside_transcript_segment`, `words_before`, `words_after` (±5 s), `is_sentence_boundary`, `is_transcript_segment_boundary`|
|Visual|`visual_change_score`|
|Sorgenti|`source_count` + 16 one-hot `source_<sorgente>`|

Le distanze sono con segno, come `t − anchor`, tranne `distance_to_next_game_start_s` (`next − t`).

## 9. Sorgenti dei candidati

- **Gameplay:** `GAME_START_ANCHOR`, `RESULT_ANCHOR`, `PREVIOUS_GAME_END`, `NEXT_GAME_START`, `BOARD_DISAPPEARED`, `BOARD_REACQUIRED`.
- **File:** `START_OF_FILE`, `END_OF_FILE`.
- **Audio:** `SILENCE_START`, `SILENCE_END`, `SILENCE_MID` (solo pause ≥ 1 s). I bordi di un silenzio tagliati dalla finestra non sono confini reali e vengono scartati.
- **Transcript:** `TRANSCRIPT_SEGMENT_START`, `TRANSCRIPT_SEGMENT_END`, `SENTENCE_END`, `SENTENCE_START` (dalle parole che finiscono con `.?!…`).
- **Visual:** `VISUAL_CHANGE`.

## 10. Deduplicazione

I candidati entro 0.25 s (circa 8 hop RMS) si fondono.

Timestamp del gruppo per priorità:
1. anchor esatti;
2. struttura del parlato;
3. bordi di silenzio;
4. marker board e visual;
5. `SILENCE_MID`.

Si conservano TUTTE le sorgenti e `merged_timestamps`. Esempio reale: `RESULT_ANCHOR + TRANSCRIPT_SEGMENT_END` a 1301.083 (partita 1). Sul VOD reale sono stati fusi da 0 a 8 candidati per context.

## 11. Serializzazione e contratto agent

`BoundaryContext.to_mapping()` / `from_mapping()` sono lossless (test di round-trip), JSON senza NaN e versionati.

`agent_payload(context)` produce un payload compatto `recut_boundary_agent_request`:
- tipo di confine, risultato e termination della partita, anchor, finestra;
- stato delle modalità, transcript (testi + tempi) e silenzi;
- candidati con indice, timestamp, sorgenti e feature arrotondate;
- `response_schema`.

Non richiede accesso a file, player o UI. `parse_agent_response()` accetta SOLO `{"selected_candidate": int, "confidence": 0..1}` e rifiuta indici fuori range, booleani, confidenze fuori intervallo e testo non JSON.

## 12. Formato dataset (predisposto per PyTorch, nessun training)

`dataset.py` usa JSONL versionato, una riga per confine etichettato:

```json
{"format": "recut_boundary_example", "version": 1, "id": "<hash stabile video+game+tipo+finestra>",
 "label": {"timestamp": 1310.06, "candidate_index": 17, "labeler": "manual", "source": "manual", "notes": ""},
 "context": { ...BoundaryContext completo... }}
```

- `match_candidate(context, t, 0.25)` collega il taglio scelto al candidato più vicino. `null` se nessuno è abbastanza vicino: serve a misurare la recall della generazione di candidati.
- `candidate_rows()` produce righe `{features[45], mask[45], target 0/1, distance_to_label_s, boundary_type}`. Va bene sia per uno scorer per-candidato sia per un modello listwise (raggruppando per `id`), con START ed END separabili o condizionati.

## 13. Terminal partial vs UNKNOWN ambiguo

Si decide con l'evidenza, non con l'enum `result` (`GameplayTimeline.termination`):

|Caso|Termination|Disposition|
|---|---|---|
|start e risultato osservati, LOSS|COMPLETE|REMOVE|
|start e risultato osservati, WIN|COMPLETE|KEEP|
|completa ma risultato UNKNOWN (conflitto)|COMPLETE|KEEP|
|ultima partita senza risultato, board ancora presente a EOF|IN_PROGRESS_AT_EOF|**REMOVE_TO_EOF**|
|senza risultato e con una partita successiva|MISSING_RESULT|KEEP|
|ultima partita senza evidenza positiva a EOF, oppure EOF oltre la durata massima della partita|MISSING_RESULT|KEEP|
|start mancante|MISSING_START|KEEP|

"Board presente a EOF" significa: ultimi `board_observations` campioni del monitor ≥ `board_high`, con l'ultimo campione entro 2 s da EOF (soglie lette dai settings dell'analisi).

Per un terminal partial, il context END usa l'anchor `END_OF_FILE` e una finestra tagliata a EOF. `user_override` è riportato nel motivo ma non ancora applicato.

Proposta per rendere il modello dati pulito (non implementata, richiede schema v2): aggiungere a `GameSegment` un campo `termination` scritto dall'analyzer state-aware, che dispone già dell'evidenza del board. La lettura dai metadata sparirebbe e la distinzione sopravvivrebbe alla persistenza del progetto.

## 14. ExactBoundaryResolver

Protocollo `BoundaryResolver.resolve(context) -> BoundaryDecision(boundary_type, timestamp, candidate_index, confidence, resolver, detail)`.

Il resolver esatto restituisce:
- per START, il candidato `GAME_START_ANCHOR`;
- per END, `RESULT_ANCHOR` oppure `END_OF_FILE`;
- in assenza del candidato, l'anchor dai settings.

Confidence 1.0. Garantisce la compatibilità end-to-end con il comportamento VS → risultato.

## 15. Comando CLI di debug

```bash
python -m analysis.boundaries VIDEO --gameplay-analysis gameplay_analysis.json --list
python -m analysis.boundaries VIDEO --gameplay-analysis gameplay_analysis.json [--game N ...]
    [--language it] [--whisper-python PATH] [--whisper-model small] [--no-transcript] [--no-visual]
    [--speech-engine rms|silero] [--start-before S --start-after S --end-before S --end-after S]
    [--include-kept] [--agent-payload] [--output contexts.json] [--json]
```

Senza `--game` analizza tutte le partite rimovibili. Stampa:
- partite con termination e disposition;
- profiling e finestre;
- transcript, silenzi, picco visuale, marker gameplay;
- candidati con le feature principali e la scelta del resolver esatto.

Non esporta video e non rilancia il detector. È modellato su `debug_analyze`, senza un nuovo framework CLI.

## 16. Risultati reali su videoprova

Smoke finale in una directory dedicata ([final_smoke_it/](gameplay_validation/videoprova/boundaries/final_smoke_it/), [JSON](gameplay_validation/videoprova/boundaries/final_smoke_it/removable_contexts.json), [log](gameplay_validation/videoprova/boundaries/final_smoke_it/debug_output.txt)): `--language it`, faster-whisper `small` int8 su CPU, exit 0, nessun warning. Gli artifact precedenti in `boundaries/` sono conservati.

Disposizioni:
- partite 1 e 3 LOSS → REMOVE;
- partite 2 e 4 WIN → KEEP;
- partita 5 → IN_PROGRESS_AT_EOF / REMOVE_TO_EOF (board a 0.397 e 0.401 negli ultimi 2 s).

|Context|Finestra (s)|Segmenti transcript|Silenzi|Candidati (fusi)|Anchor|
|---|---|---:|---:|---:|---|
|G1 START|605.58–635.58|4|5|22 (3)|630.583 VS + SILENCE_MID|
|G1 END|1296.08–1336.08|7|11|41 (7)|1301.083 RESULT + TRANSCRIPT_SEGMENT_END|
|G3 START|1794.75–1824.75|9|6|27 (8)|1819.750 VS|
|G3 END|2786.98–2826.98|5|6|25 (5)|2791.983 RESULT + TRANSCRIPT_SEGMENT_END/START|
|G5 START|3603.93–3633.93|6|1|11 (5)|3628.933 VS|
|G5 END|3745.00–3750.00 (clipped EOF)|0|2|5 (0)|3750.000 END_OF_FILE|

`ExactBoundaryResolver` sceglie sempre l'anchor atteso.

## Observed editorial signals on videoprova

Solo evidenza per i resolver futuri, **non regole**. Nulla di quanto segue è codificato.

- **END partita 1.** Reazione alla LOSS attorno al banner ("Porca puttana", "sembra rotto l'antera", "Cos'era cos'era?", "Non so cos'era però ok"). Intorno a 1310.06 s cambia argomento ("[Ve]diamo una busta, posso aprirla?"). Seguono circa 17 s senza transcript (1312.0 → 1328.8) con molti silenzi e un picco visuale (0.41 a 1315.6).
- **END partita 3.** Commento e scuse sulla partita appena chiusa ("My bad… chiedo umilmente perdono, mi sono completamente dimenticato") fino a circa 2802.57 s. Poi cambio argomento ("comunque Hunter sembra molto rotto… streamer deck… qualche lista interessante").
- **START partite 1, 3, 5.** Il parlato pre-partita (evento/theorycrafting, nerf, mazzi da provare) inizia prima del VS e continua oltre (G1 fino a 635.2, G3 fino a 1824.1, G5 fino a 3633.3). Il VS non coincide con un confine del parlato.
- **END partita 5.** Nessun transcript negli ultimi 5 s prima di EOF: solo silenzi e `END_OF_FILE`.

Qualità ASR: buona sul senso generale, errori su termini di gioco ("teore crafting", "Rode to legend", "loc con tune").

## 17. Performance

|Partita|Secondi analizzati|ASR (modello incluso)|Visual|Audio|Candidati|
|---|---:|---:|---:|---:|---:|
|G1|70|6.19 s|1.74 s|0.31 s|<0.01 s|
|G3|70|7.77 s|1.79 s|0.33 s|<0.01 s|
|G5|35|5.47 s|1.13 s|0.24 s|<0.01 s|

- Totale 25.2 s wall per 3 partite e 175 s di audio/video locale; VOD da 62.5 min **mai** analizzato per intero.
- Re-run con transcript in cache: circa 2.4 s per partita.
- L'ASR domina; il modello si carica una volta per partita. Ottimizzazioni possibili, non fatte: un solo processo ASR per tutte le partite; modello `base` (più veloce, qualità minore; non misurato).
- Stima per un VOD di 3 h con 10 LOSS: circa 700 s di finestre e 70–90 s di analisi, a freddo.

## 18. Test aggiunti

`tests/test_boundaries.py`, 34 test. Copertura:
- **Generazione candidati:** silenzi (bordi + midpoint), bordi tagliati dalla finestra, transcript (segmenti e frasi dalle parole), anchor START/END/EOF/SOF, marker board, picchi visuali, marker sconosciuti ignorati.
- **Deduplicazione:** priorità, unione delle sorgenti, timestamp fusi.
- **Feature:** distanze, audio, transcript, distinzione START/END.
- **Degrado:**
  - nessun parlato;
  - nessun silenzio utile;
  - modalità mancanti → `None` + mask 0;
  - nessun ASR / runtime mancante;
  - offset ASR;
  - finestre vicino a inizio file e a EOF;
  - finestre che non attraversano le partite vicine.
- **Riuso del motore di silenzio:** preset, durata minima, dB assoluti; test reale FFmpeg (tono + silenzio 4–6 s).
- **Policy:** termination/disposition, incluso terminal partial vs MISSING_RESULT (3 varianti), MISSING_START, partita troppo lunga.
- **Analyzer:** una sola chiamata ASR per START+END, KEEP rifiutato salvo `--include-kept`, END del terminal partial su EOF.
- **Contratti:** round-trip JSON, `ExactBoundaryResolver`, payload e validazione della risposta agent, dataset JSONL e righe, CLI `--list`, import senza Qt/torch/faster-whisper.

## 19. Suite completa

|Controllo|Esito|
|---|---|
|Gameplay + boundary|378 passed|
|Full pytest|827 passed, 1 skipped (googleapiclient opzionale, preesistente)|
|Ruff|PASS|
|Mypy|PASS, 71 file (set del quality gate + `analysis/boundaries`)|
|GUI smoke offscreen|exit 0|
|Freeze detector videoprova|49/49 hash invariati|

## 20. Cosa manca

**HeuristicBoundaryResolver** (il passo più vicino):
- regole esplicite sulle feature, ad esempio END = primo `SILENCE_START` o `SENTENCE_END` dopo il risultato seguito da un silenzio ≥ X s, entro limiti massimi;
- START = ultimo silenzio lungo prima del VS;
- fallback all'anchor;
- un piccolo set di confini etichettati a mano per scegliere X e i limiti (su VOD diversi da quelli di test);
- una metrica: errore in secondi rispetto all'etichetta e frazione "taglio dentro una frase";
- la policy di merge con i silence cut e il padding.

**AgentBoundaryResolver (Claude/Codex):**
- chiamata reale (CLI o API) che invii `agent_payload` e validi con `parse_agent_response`;
- prompt/istruzioni versionati;
- timeout, costo e cache delle decisioni per hash del payload;
- fallback a Exact/Heuristic in caso di errore o bassa confidenza;
- valutazione contro etichette manuali;
- decisione su privacy e invio del transcript a un servizio esterno.

**LocalModelBoundaryResolver (PyTorch):**
- un dataset etichettato via `append_example` (decine o centinaia di confini, da più VOD/streamer);
- split per VOD;
- scelta dell'architettura (scorer per-candidato con softmax listwise su `features`/`mask`, condizionato su `is_end_boundary`, oppure due modelli);
- eventuale embedding testuale del transcript, oggi assente;
- training ed esportazione; runtime inferenza senza PyTorch nell'app (ad esempio ONNX), calibrazione della confidenza.

**Comune a tutti:**
- il segnale "queue/menu transition" non esiste nel detector (oggi solo board/VS/risultato);
- `termination` persistita in `GameSegment`;
- la soglia di silenzio del progetto passata dalla GUI;
- un'interfaccia GUI per etichettare i confini (che è anche la fonte del dataset);
- l'integrazione finale `Segment` → merge con silence e manual cut → exporter.

## Aggiornamento — resolver pluggabili (exact, Claude, Codex)

I resolver agent-based, la factory `create_resolver`, il prompt versionato `boundary_agent_prompt_v1`, il fallback e lo smoke reale sono documentati in [GAMEPLAY_BOUNDARY_RESOLVERS.md](GAMEPLAY_BOUNDARY_RESOLVERS.md).
