# Pipeline CLI end-to-end — smart gameplay editing + ReCut classico

9 ottobre 2026. Primo flusso CLI completo:

```text
video locale → gameplay detection → completezza partite → keep-games wins → boundary semantici (exact/claude/codex)
→ gameplay cuts → classic silence cuts → composizione → UN solo export locale
```

Pubblicazione opzionale. Gameplay Detector invariato (49/49 hash del freeze videoprova identici). Nessun PyTorch, nessun redesign GUI.

## 1–2. Architettura ed entrypoint reali

Entrypoint: `main.py` → `automation.cli.run_cli` (la stessa CLI dei job Twitch/locali). Pipeline: `PipelineRuntime` (stati del job), con stage download (solo Twitch) → **analysis** → export → upload opzionale. La feature si innesta nello stage di analysis, senza una seconda pipeline:

```text
automation/cli.py        flag --gameplay/--keep-games/--boundary-mode/--agent-*/--report/--analyze-only, VIDEO nudo
automation/runtime.py    plan → metadata["gameplay"] (validato); run(stop_after_analysis); tempi per stage
automation/analyzer.py   classic RMS (invariato) → GameplayEditingService → compute_classic_cuts(..., gameplay_cuts)
                         → progetto .autocutter (cuts/keeps composti + gameplay_cuts separati) + report JSON
analysis/gameplay_editing.py   detection → GameplayTimeline(wins) → BoundaryAnalyzer (solo partite rimosse)
                               → create_resolver(mode) (solo confini non fissi) → GameCutPolicy → report
analysis/boundaries/cut_policy.py  BoundaryDecision → analysis.cut_engine.Segment (clamp di sicurezza)
analysis/classic.py      composizione generica classic + gameplay + manual − suppressed
automation/exporter.py   INVARIATO: legge keeps dal progetto (gameplay-agnostic)
```

## 3–7. Sintassi, input, output, publishing, preset

```bash
python main.py "C:\Videos\vod.mp4" --preset Balanced --gameplay hearthstone --keep-games wins \
    --boundary-mode codex [--agent-timeout 600] [--agent-model M] [--agent-effort E] \
    [--output "D:\out\final.mp4" | --output-dir DIR] [--report report.json] [--analyze-only] [--json]
```

(equivalente a `process VIDEO ...`; con l'eseguibile pacchettizzato: `AutoCutter VIDEO ...`)

|Aspetto|Comportamento|
|---|---|
|Input|percorso locale = sorgente standard. Un primo argomento che è un file esistente o un nome con estensione video diventa `process`; un URL `twitch.tv/videos/…` diventa `vod` (download, poi la stessa pipeline). Parole sconosciute restano un errore di argomenti.|
|Output|default invariato: cartella export dell'utente / `titolo.estensione`, con suffisso numerico se esiste già. `--output` / `--output-dir` come prima.|
|Publishing|separato: l'export locale termina il job (`complete_without_upload`); YouTube solo con `--youtube` (comportamento esistente).|
|Preset|`--preset` usa il catalogo reale della GUI (`PresetRepository` + `normalize_preset_cfg`). Nuovo `match_preset_name`: nome esatto, poi case-insensitive, poi prefisso univoco (`Balanced` → `Balanced (Default)`); un prefisso ambiguo è un errore.|

## 8–13. Completezza, termination e policy `keep-games wins`

La completezza è rappresentata esplicitamente (`GameTermination`) e derivata dall'evidenza, non dall'enum del risultato.

|Termination|Significato|Evidenza|
|---|---|---|
|COMPLETE|VS e risultato osservati, WIN/LOSS|anchor|
|AMBIGUOUS_RESULT|VS e fine osservati, risultato UNKNOWN (conflitto)|anchor|
|MISSING_START|fine osservata, VS no|—|
|TRUNCATED_AT_START|prima partita senza VS **e** board presente all'inizio del file|monitor 1 FPS|
|MISSING_RESULT|VS osservato, fine no (interna, oppure ultima senza evidenza)|—|
|IN_PROGRESS_AT_EOF|ultima partita, board presente agli ultimi campioni|monitor 1 FPS|

Policy `wins` (`GameplayTimeline(policy="wins")`): si pubblica **solo** COMPLETE + WIN.

- COMPLETE LOSS → `REMOVE`.
- TRUNCATED_AT_START → `REMOVE_FROM_START` (0 → END smart).
- Ultima partita senza risultato → `REMOVE_TO_EOF` (START smart → EOF).
- Tutto il resto (WIN senza VS, LOSS senza VS, fine mancante interna, risultato ambiguo) → `REMOVE`.
- La policy legacy `remove-losses` resta il default per l'analisi di debug.

Anchor mancanti: stimati dal board del monitor, con margini di sicurezza documentati (−10 s prima della comparsa del board, +5 s dopo la scomparsa). In assenza di board: la fine della partita precedente o l'inizio della successiva. Gli anchor stimati diventano candidati e sono dichiarati nel prompt.

Persistenza retro-compatibile:
- la completezza sta nel blocco `gameplay` del progetto e nel report;
- `GameSegment` del detector non è modificato (schema e hash invariati);
- `PROJECT_VERSION` non è stato bumpato.

## 14–17. START/END smart, prompt v2, shortlist

**Finestre editoriali** (`EDITORIAL_WINDOWS`): START −45/+10 s, END −10/+60 s, mai oltre le partite vicine.

**Analisi dei confini solo dove serve:**
- solo partite rimosse;
- nessun context START per `REMOVE_FROM_START`;
- END non risolto per `REMOVE_TO_EOF`.

**Prompt `boundary_agent_prompt_v2`:**
- prima etichetta OGNI segmento del transcript come `PREVIOUS_CONTENT` / `CURRENT_REMOVED_GAME` / `GENERAL_COMMENTARY` / `NEXT_CONTENT` / `UNCERTAIN`, poi sceglie il candidato;
- START = dove inizia il contenuto della partita rimossa (anche prima del VS, senza spezzare frasi);
- END = subito dopo l'ultimo contenuto della partita rimossa;
- la reaction va rimossa; i commenti generali sul mazzo/matchup e il contenuto successivo restano;
- avverte esplicitamente che lo stesso mazzo è spesso usato per tutto il VOD (spostamenti di argomento sottili: decidere dai riferimenti specifici alla partita, non da keyword o classe);
- include le partite vicine (risultato, KEPT o REMOVED), il motivo della rimozione, il tipo di anchor (osservato o stimato), l'intero transcript della finestra (fino a 80 segmenti), silenzi, marker, picchi visivi e, per candidato, i segmenti prima/dentro/dopo.

**Output stretto:** `segment_labels`, `selected_candidate`, `confidence`, `reason`. Le etichette sono validate e salvate nel report come traccia del ragionamento.

**Shortlist non distruttiva** (`shortlist.py`):
- unica regola rigida: copertura. Un START dopo l'anchor iniziale o un END prima dell'anchor finale lascerebbe gameplay nel video;
- oltre 40 candidati si scartano solo `SILENCE_MID`/`VISUAL_CHANGE` puri, i più lontani per primi;
- l'agente può scegliere solo indici mostrati.

Sui context reali: 20–41 candidati mostrati su 26–53.

## 18–21. Codex, Claude, fallback, cache

Riusati senza duplicazioni: `create_resolver` con `exact`, `claude` o `codex` (`heuristic`/`local` riservati), con sandbox, schema, timeout, process-tree kill, fallback a exact e cache per provider già esistenti.

La pipeline non sa quale resolver è stato usato: riceve `BoundaryDecision`.

La modalità remota è dichiarata, senza prompt interattivi:
- avviso su stderr a ogni run `claude`/`codex`;
- `remote_context_sent` nel report;
- `--boundary-mode exact` non invia nulla.

Persistenza delle decisioni:
- cache agent (stesso context + resolver + prompt → nessuna nuova chiamata);
- cache del progetto di analisi (la firma della richiesta include le impostazioni gameplay; i job legacy hanno la firma invariata);
- cache del detector e cache ASR.

## 22–27. GameCutPolicy, composizione, manual/suppressed, exporter

**`GameCutPolicy`** produce `analysis.cut_engine.Segment` (nessun nuovo tipo di intervallo). Ogni regione è:
- `[START, END]` per le partite rimosse con entrambi i confini;
- `[0, END]` per `REMOVE_FROM_START`;
- `[START, EOF]` per `REMOVE_TO_EOF`.

Due clamp con provenienza registrata:
- **copertura:** la regione contiene sempre gli anchor della partita rimossa;
- **protezione:** non entra mai nello span [VS, risultato] di una WIN mantenuta.

Le regioni sovrapposte (partite rimosse consecutive) si fondono solo nei `segments`.

**Composizione** (`compute_classic_cuts(..., gameplay_cuts=)`):

```text
automatici = merge(classic silence cuts + gameplay cuts) − suppressed
effettivi  = merge(automatici + manual)          keeps = invert_to_keeps(effettivi)
```

L'utente ha l'ultima parola: `suppressed` ripristina anche parti di un gameplay cut, e un taglio `manual` vince sempre. Il range `--start/--end` resta espresso come manual cut (semantica esistente). Senza gameplay cuts il risultato è identico al legacy (testato). Con intensità classica < 10 i gameplay cuts si applicano comunque.

**Progetto:** la traccia conserva `cuts`/`keeps` composti (consumati da exporter e GUI) più `gameplay_cuts` e `gameplay_cuts_enabled` separati, accanto a `classic_*`/`manual_cuts`/`suppressed_cuts`. Blocco generico `gameplay` a livello di progetto con partite, completezza, decisioni e tagli. Nessun campo `hearthstone_*` nel core.

**GUI (minimo):** `TrackState.gameplay_cuts`/`gameplay_cuts_enabled` vengono ripristinati, salvati, clonati e passati a `compute_classic_cuts` nei due ricalcoli. Un progetto gameplay aperto e modificato nell'editor non perde le rimozioni.

**Exporter:** invariato. Riceve keeps e non contiene logica di gioco.

## 28–29. `--analyze-only` e report JSON

`--analyze-only` esegue detection, boundary, resolver, classic e composizione, scrive progetto e report e si ferma a `ready_export` senza renderizzare. `resume <job>` esporta poi senza rianalizzare.

Report (`--report` oppure accanto al progetto; il path è nel risultato JSON e nei metadata del job):
- impostazioni, invio remoto, detector;
- per partita: anchor grezzi, termination, disposition e motivo, anchor della regione (osservati o stimati), decisioni START/END (resolver, candidato e sorgenti, timestamp, confidenza, fallback ed errore, runtime, cache, versione del prompt, candidati mostrati, etichette per segmento);
- gameplay cuts con aggiustamenti;
- composizione: classic, manual ed effective cuts, keeps, secondi per categoria;
- tempi e warning.

Nessun segreto.

## 30–32. videoprova end-to-end (reale)

GT nota:

|Partita|Span|Risultato|
|---|---|---|
|G1|631.0–1301.1|LOSS|
|G2|1359.0–1786.0|WIN|
|G3|1820.0–2792.0|LOSS|
|G4|2864.0–3525.5|WIN|
|G5|3628.5–EOF|in corso a fine VOD|

Due run in directory dedicate (store isolato, nessun artifact cancellato):

|Run|Tempo totale|
|---|---:|
|[exact, analyze-only](gameplay_validation/videoprova/pipeline_e2e/exact_analyze_only/)|156.8 s|
|[codex, export completo](gameplay_validation/videoprova/pipeline_e2e/codex_full_export/)|311.6 s|

Disposizioni identiche nei due run: G1 e G3 REMOVE, G2 e G4 KEEP, G5 REMOVE_TO_EOF.

|Confine|exact|codex (confidenza, motivo)|
|---|---:|---|
|G1 START|630.583|630.583 (0.86; parlato pre-VS = commento generale sull'evento/mazzo)|
|G1 END|1301.083|1310.073 (0.94; reaction rimossa, apertura busta e commento generale tenuti)|
|G3 START|1819.750|1817.733 (0.80; taglio prima della frase che attraversa il VS)|
|G3 END|2791.983|2803.858 (0.98; scuse dopo la resa rimosse, domanda sul prossimo mazzo tenuta)|
|G5 START|3628.933|3628.458 (0.88; pausa prima del VS, discussione sui mazzi tenuta)|

Codex: 5 chiamate, 0 fallback, 0 clamp necessari.

Verifica sui keeps finali:

|Partita|Secondi mantenuti|
|---|---|
|G1|0 di 670.1|
|G3|0 di 972.0|
|G5|0 di 121.5|
|G2|367.0 di 427.0|
|G4|605.3 di 661.5|

Nessun gameplay cut interseca le WIN: i secondi mancanti nelle WIN sono tagli classici di silenzio/tempo morto, quindi il comportamento classico resta attivo anche dentro le partite mantenute.

Composizione (codex):

|Voce|Secondi|
|---|---:|
|Classic|476.6 (173 tagli)|
|Gameplay|1787.2|
|Effettivi rimossi|2027.9 (88 tagli)|
|Mantenuti|1722.1|

Export reale: `videoprova_recut.mp4`, H.264 1080p60 + AAC, 1745.9 s (da 3750.0 s), validato dalla pipeline. Il file è 23.8 s più lungo dei keeps pianificati (circa 0.27 s per giunzione su 88 tagli). Viene dalla gestione dei bordi `smart_hybrid` dell'exporter esistente, non modificata e non investigata in questa iterazione.

**Performance per fase (videoprova, 62.5 min):**

|Fase|exact (cache vuote)|codex (cache detector/ASR calde)|
|---|---:|---:|
|Analisi classica RMS|10.1 s|9.6 s|
|Gameplay detection|108.4 s|0.17 s (cache)|
|Boundary analysis (ASR/audio/visual, 5 finestre)|37.7 s|8.5 s (ASR in cache)|
|Resolver|0 s|158.2 s (5 chiamate Codex, 16–52 s ciascuna, effort `xhigh` di default)|
|Stage analysis totale|~157 s|176.5 s|
|Export|—|134.2 s|

Nessun lavoro duplicato: ASR una volta per finestra, nessuna chiamata agent ripetuta, un solo export.

## 33–35. Suite, freeze, file

|Controllo|Esito|
|---|---|
|Full pytest|891 passed, 1 skipped (googleapiclient opzionale, preesistente); baseline iniziale 855|
|Ruff|PASS|
|Mypy|PASS, 76 file|
|GUI smoke|exit 0|
|Freeze detector|49/49 hash identici|

Test nuovi:
- `tests/test_gameplay_editing.py` (22): policy, clamp, VOD che inizia o finisce durante una partita, servizio con analyzer reale, fallback, cache, composizione;
- `tests/test_gameplay_pipeline.py` (10): CLI, runtime analyze-only, stage di analysis, firma legacy;
- 4 test aggiunti in `tests/test_boundary_agents.py`, più gli adattamenti al prompt v2.

File:
- **nuovi:** `analysis/gameplay_editing.py`, `analysis/boundaries/{cut_policy,shortlist}.py`, i due test sopra, questo documento;
- **modificati:** `analysis/classic.py`, `analysis/boundaries/{models,gameplay,analyzer,resolvers,agents}.py`, `automation/{cli,runtime,analyzer}.py`, `core/{presets,track_state}.py`, `ui/main_window.py` (solo persistenza/ricalcolo dei gameplay cuts), `tests/test_boundary_agents.py`.
- **invariati:** detector ed exporter.

## 36–37. Limiti e prossimi passi GUI

**Limiti:**
- **Asset del detector:** il profilo validato e il template pack sono letti da `docs/gameplay_validation/...`; una build pacchettizzata deve includerli o esporre un'opzione per indicarli (oggi solo in `GameplayEditSettings`, senza flag CLI).
- **Range e gameplay:** `--start/--end` con gameplay analizza comunque l'intero file (detection e anchor in coordinate assolute).
- **Costo dell'agente:** Codex con effort `xhigh` di default impiega circa 30 s per confine; `--agent-effort` lo riduce.
- **Ground truth dei confini:** manca ancora, quindi non si può dire quanto le scelte semantiche siano "giuste", solo che sono coerenti con le etichette e con le osservazioni precedenti.
- **Partite rimosse consecutive:** il contenuto fra due partite rimosse è deciso da due resolver indipendenti (END dell'una, START dell'altra).
- **`user_override` del `GameSegment`:** non ancora usato dalla policy.

**Prossimi passi GUI:**
- mostrare le partite (termination/disposition) e i gameplay cuts con provenienza sulla timeline;
- toggle "Remove lost games" (`gameplay_cuts_enabled`);
- ripristino o modifica di un singolo gameplay cut (oggi possibile via suppressed/manual);
- mostrare il motivo e le etichette dell'agente dal report;
- opt-in esplicito per le modalità remote;
- un flusso di etichettatura dei confini che alimenti `dataset.py`.

## Aggiornamento 2026-10-09 — `keep-games wins` basata sulle KEEP (correzione concettuale)

**Bug.** La vecchia `GameCutPolicy` tagliava solo le regioni delle partite non pubblicabili. Tutto ciò che non apparteneva a nessuna di quelle regioni restava nel video: intro pre-game, chiacchiere fra partite, coda dopo l'ultima WIN. Riproduzione (intro 0–100, LOSS 100–500, WIN 550–900): keeps finali `[(0, 100), (500, 1000)]`.

**Nuova sorgente della verità: `GameKeepPolicy`** (`analysis/boundaries/keep_policy.py`).
- Una isola editoriale per ogni partita pubblicabile (START e END osservati e risultato WIN): `[smart START, smart END]`.
- Tutto il resto viene rimosso: LOSS, partite senza START o senza END, partite in corso a EOF, risultati ambigui, intro, chiacchiere orfane, coda.
- `gameplay_cuts = invert(gameplay_keeps)` è solo derivato.
- Clamp: **copertura** (l'isola contiene sempre l'intera WIN [VS, risultato]) e **isolamento** (non entra mai nello span di gameplay di un'altra partita, osservato o stimato); entrambi registrati.
- Isole sovrapposte di WIN consecutive si fondono.
- Senza WIN complete: errore esplicito `no_publishable_games` invece di un export vuoto.

`GameCutPolicy` (`cut_policy.py`) è stata eliminata.

**Lavoro.** Analisi dei confini (ASR/audio/visual) e resolver solo attorno alle WIN complete, per START ed END: 2 chiamate per WIN. LOSS e partite incomplete non richiedono alcun lavoro sui confini.

**Composizione.** `compute_classic_cuts(..., gameplay_cuts=invert(keeps))` esistente:

```text
automatici = merge(classic + invert(keeps)) − suppressed ;  effettivi = merge(automatici + manual)
final keeps = invert(effettivi) = keeps − classic − manual (suppressed può ripristinare)
```

Quindi i tagli classici agiscono dentro le isole e non possono riportare in vita contenuto esterno. Manual e suppressed conservano la semantica esistente (l'utente ha l'ultima parola).

Progetto: la traccia contiene `gameplay_mode: "keep"`, `gameplay_keeps` (verità), `gameplay_cuts` (complemento usato dall'editor) e `gameplay_cuts_enabled`. La GUI ripristina, salva e clona anche keeps e mode. Il blocco progetto `gameplay` v2 contiene isole e partite. L'exporter è invariato.

**Prompt `boundary_agent_prompt_v3`** (ownership dei contenuti).
- La v2 era stata usata con la semantica di rimozione, quindi il nuovo prompt ha una versione nuova; la versione fa parte della chiave di cache e nessuna decisione v1/v2 viene riusata (testato).
- Etichette: `PREVIOUS_CONTENT` / `CURRENT_GAME_SPECIFIC` / `GENERAL_COMMENTARY` / `NEXT_CONTENT` / `UNCERTAIN`, con gli esempi dati ("qui avrei dovuto giocare X", "contro questo matchup in generale…", "questa lista è forte", "ok vediamo la prossima", "contro Warrior…").
- Procedura in tre passi: etichetta, trova dove inizia/finisce il contenuto della partita, scegli il candidato separatore.
- Per una partita KEEP:
  - START: niente `PREVIOUS_CONTENT`; il commento generale viene incluso solo se introduce questa partita; le chiacchiere orfane spariscono; nessuna frase spezzata al VS;
  - END: reaction e commenti specifici inclusi; il commento generale solo se prosegue senza stacco; stop prima di transizioni o `NEXT_CONTENT`.
- Avvertenza sullo stesso mazzo usato per tutto il VOD.
- La semantica di rimozione resta per i context di debug legacy.
- La decisione è sempre e solo `selected_candidate` (fra gli indici mostrati) più `confidence`; `reason` e `segment_labels` sono tracce di audit nel report e non producono mai timestamp.

**Report v2.** Per ogni partita: `complete`, `publishable`, termination e disposition, anchor grezzi. Per ogni WIN: smart start/end, aggiustamenti, decisioni (resolver, candidato e sorgenti, confidenza, fallback, versione del prompt, candidati mostrati, etichette). Poi `keep_islands`, `gameplay_keeps`, `gameplay_cuts` derivati e composizione (`classic_cuts_all`, `classic_cuts_inside_keeps`, manual, effective, `final_keeps`, secondi).

**videoprova reale** ([codex_keep_wins_v3/](gameplay_validation/videoprova/pipeline_e2e/codex_keep_wins_v3/), nuova directory e nuovo file; gli export precedenti restano intatti): `--keep-games wins --boundary-mode codex`, 4 chiamate v3, 0 fallback, 0 clamp.

|Isola|START|END|
|---|---|---|
|G2|1333.13 (VS −26.3 s: intro della coda per questa partita)|1817.50 (risultato +31.5 s: critica al mazzo nata da questa partita; stop prima di "Road to legend")|
|G4|2847.00 (VS −16.8 s)|3544.38 (risultato +18.9 s)|

Verifica sui keeps finali:

|Regione|Secondi mantenuti|
|---|---|
|Intro 0→1333.1|0 di 1333.1|
|LOSS G1|0 di 670.1|
|Orfano fra le isole|0 di 1029.5|
|LOSS G3|0 di 972.0|
|Coda dopo l'ultima isola|0 di 205.6|
|Parziale G5|0 di 121.5|

- G2 e G4 sono interamente dentro le isole.
- I tagli classici restano attivi dentro le isole: 50 tagli, 125.4 s (G2 367.0/427.0 s, G4 605.3/661.5 s).
- Mantenuti 1056.4 s su 3750.0 s.
- Export `videoprova_wins_only.mp4`: H.264 1080p + AAC, 1070.4 s, 59.75 fps medi come l'export precedente (`r_frame_rate` 240/1 è solo l'euristica di timebase ffprobe dopo le giunzioni). +14 s rispetto ai keeps pianificati (circa 0.26 s per giunzione, exporter esistente).

**Tempi:**

|Fase|Tempo|
|---|---:|
|Detection|0.2 s (cache)|
|Analisi classica|10.9 s|
|Analisi dei confini|34.5 s (ASR nuove finestre attorno alle WIN)|
|Resolver Codex|138.8 s (4 chiamate, 29–49 s)|
|Stage analysis|184.4 s|
|Export|84.7 s|
|Totale|270.0 s|

**Suite:** 899 passed, 1 skipped (baseline 891). Ruff, mypy (76 file) e GUI smoke OK. Freeze del detector 49/49.

**Limiti:**
- **Finestre editoriali:** START −45 s, END +60 s. Contenuto più lontano dal VS o dal risultato non può entrare in un'isola senza allargare `EDITORIAL_WINDOWS`.
- **Ground truth dei confini:** manca ancora una misura manuale delle scelte semantiche.
- **WIN consecutive:** le decisioni (END dell'una, START dell'altra) sono indipendenti; le isole possono fondersi.

## Aggiornamento 2026-10-11 — giunzioni senza ripetizioni e END editoriale (tight/balanced/extended)

### Ripetizione di 1–2 s dopo un taglio (export smart)

Causa confermata su `pipeline_e2e/codex_keep_wins_v3/videoprova_wins_only.mp4`, con `method auto` risolto in
`smart_hybrid`:

- Ogni frame esportato è stato confrontato con il sorgente. Risultato: 6 ripetizioni da 0.55 a 1.53 s (1.07, 0.77,
  1.53, 0.55, 1.22 e 0.98 s), 33 salti indietro e +12.9 s di frame in più. Durata 1070.40 s contro 1056.39 s di keep.
- `utils/ffmpeg.scan_splice_safe_pts` leggeva come Annex B anche i pacchetti MP4. Un B-frame di 256–511 byte ha il
  prefisso di lunghezza `00 00 01 xx`, che veniva scambiato per uno start code: il byte seguente era letto come NAL
  IDR. Su videoprova risultavano IDR i punti 1335.083, 1335.166, … (B-frame), mentre i veri IDR sono ogni 2 s.
- Un pezzo in stream copy che parte da uno di quei punti (`-ss 1335.083 -c:v copy`) in realtà parte dal sync sample
  precedente (1334.000). Così il tratto già mostrato veniva ripetuto.

Correzioni minime:

1. Lo scanner fa parsing a lunghezza prefissata quando c'è `avcC`/`hvcC` e accetta solo pacchetti che sono keyframe
   del container. Cache splice alla v2: i punti v1 sbagliati non vengono più letti.
2. I pezzi smart vengono tagliati per numero di frame (`-frames:v`). Prima il copy sbordava di 1–3 frame nel GOP
   successivo e i pezzi ricodificati venivano allungati.
3. La concat TS dei pezzi usa per ogni file una `duration` pari all'estensione del video. Senza questo, lo sbordo
   AAC (priming e ultimo frame parziale) spostava il video di 1–3 frame a ogni pezzo.
4. smart_render e smart_hybrid ricostruiscono l'audio in un solo passaggio dai keep (`atrim` + `concat` con
   gain/LUFS/limiter), mentre il video, ora esatto, viene copiato.

Accurate (`filter_concat`) e Fast (`chunked_parallel`) erano già corretti e sono invariati.

Verifica:

- **Sorgente sintetico.** 60 fps, GOP 2 s, B-frame, timebase al millisecondo; indice del frame dipinto nei pixel e
  blip audio a ogni secondo intero (`tests/test_export_junctions.py`). Tutti e 4 i metodi:
  - ogni frame tenuto compare una sola volta, in ordine;
  - nessun frame fuori dai keep;
  - durata entro 0.1 s;
  - A/V costante entro 2.5 frame.

  Sul codice precedente lo stesso test fallisce per smart_render e smart_hybrid.
- **Export reale completo dei 52 keep.** 1056.87 s contro 1056.85 s di keep normalizzati, 0 ripetizioni. I 12 keep
  iniziali restano entro ±1.4 frame per keep.

L'anteprima della GUI (seek di QMediaPlayer sul sorgente) non è stata modificata: la ripetizione segnalata era
presente nell'MP4 esportato.

### END delle partite tenute: rilevanza prima della completezza

- **Etichette `boundary_agent_prompt_v4_end`, solo per l'END di partite KEEP:** `WIN_REACTION`,
  `GAME_SPECIFIC_RECAP`, `GAME_STRATEGY`, `GENERAL_DECK_TALK`, `CHAT_INTERACTION`, `UNRELATED_COMMENTARY`,
  `NEXT_GAME`, `UNCERTAIN`. START e partite rimosse restano su v3, con le loro cache.
- **Priorità nel prompt:** rilevanza per il video YouTube, poi appartenenza alla partita, poi quantità di contenuto
  inutile, poi naturalezza del taglio. Una domanda della chat sulla giocata è contenuto (`GAME_STRATEGY`).
- **Politica deterministica** (`analysis/boundaries/end_policy.py`), applicata alle etichette dell'agent:
  - la reazione resta sempre;
  - recap e strategia restano entro un budget (`tight` 10 s, `balanced` 25 s, `extended` 60 s; `tight` esclude la
    strategia);
  - ci si ferma al primo blocco irrilevante;
  - il candidato si sceglie per costo: secondi di parlato irrilevante tenuti, parlato rilevante tagliato,
    naturalezza del punto;
  - tenuta minima sulla schermata di vittoria: 2 s (1.5 in `tight`);
  - la scelta dell'agent resta se è entro margine.
- **Nuovi candidati `WORD_GAP`:** confini tra parole dopo il risultato, così si può tagliare dentro una frase
  irrilevante invece di aspettare una pausa lontana.
- **Default:** `balanced`. Opzione `--end-policy tight|balanced|extended` per `process`/`vod`, e
  `python -m analysis.boundaries`.

Prima e dopo su videoprova (contesti reali; etichette del test al posto dell'agent; nessuna chiamata agent):

| Partita | Codex v3 (prima) | v4, tight / balanced / extended |
|---|---|---|
| 2 (WIN) | +31.5 s: commenti generali sul nerf, fino a "road to legend" | +14.7 s: subito dopo "Ma quanto è rotto? Nerfatelo!" |
| 4 (WIN, nessuna reazione, chat su pet e skin) | +18.9 s | tight +1.7 s, balanced/extended +3.1 s (confine di parola nella chat) |

Casi di regressione con intervallo accettabile e motivo editoriale: `tests/test_boundary_end_policy.py`.
