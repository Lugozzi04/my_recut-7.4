# AI YouTube Packaging — titoli, thumbnail renderizzate, concept

10 ottobre 2026. Dal progetto ReCut montato (`.autocutter`) o da un MP4 già montato, ReCut genera
titoli YouTube, **thumbnail PNG reali** (non prompt), concept creativi legati a momenti verificabili,
un report degli highlight e una Creator Style Memory persistente. CLI-first, salvataggio solo locale,
nessun upload YouTube automatico.

Invariati: Gameplay Detector e policy validate, `GameKeepPolicy`, classic cuts, exporter, redesign GUI.
L'unica modifica a codice esistente fuori dal pacchetto è l'estrazione di `run_agent_process()` in
`analysis/boundaries/agents.py` (stesso comportamento, test dei boundary resolver invariati) e la
registrazione dei comandi in `automation/cli.py`.

## V2 — percorso FAST, Codex economico, thumbnail più curate

Default `--mode fast`; la pipeline V1 (sezioni sotto) resta disponibile come `--mode deep`.

```text
FAST  Evidence locali ─► PackagingBrief (brief.py) ─► 1 chiamata: Story + angoli + titoli + concept + piani
      ─► [solo se l'agent lo chiede e --max-deepen ≥ 1: estrazione locale + 1 chiamata "patch" sul concept]
      ─► crop carte ─► render + Quality Gate locale (quality.py) ─► PNG
DEEP  Story (1) ─► [deepen: estrazione + Story raffinata senza ritrasmettere il transcript (1)] ─► Creative (1)
```

| Modalità | Chiamate agent | Massimo |
|---|---|---|
| fast (standard) | 1 | 1 + 1 correzione JSON + 1 patch (+1 correzione) |
| fast `--max-deepen 0` | 1 | 1 + 1 correzione JSON |
| deep | 2–3 | 3 (+1 correzione ciascuna) |
| `--only render` (testo, colori, layout, tuning) | 0 | 0 |

**PackagingBrief.** Per partita: id, risultato, intervallo, marker, transcript con budget (14 000
caratteri ripartiti per durata; prima le righe vicine agli highlight, max 60 %, poi un campione uniforme
nel tempo per coprire spiegazioni e passaggi senza picchi audio; se tutto il transcript entra, entra
tutto). Highlight con frame **già selezionati** (migliore + alternativa diversa) e webcam distinte.
Immagini: 1 contact sheet per partita (solo frame selezionati), 1 sheet webcam, ≤ 2 frame con griglia per
le carte; massimo 6, nessuna immagine o transcript inviato due volte.

**Codex.** Effort predefinito `medium` (`-c model_reasoning_effort=medium`); `--agent-effort
low|medium|high`, `xhigh` non esposto. Budget di input `--agent-max-input` (60 000 caratteri fast /
120 000 deep: un prompt più grande viene rifiutato, non inviato), massimo 6 immagini in fast. La correzione
di un JSON non valido invia solo risposta precedente + errori + ID validi (niente transcript, niente
immagini). Codex non ha un tetto di costo da CLI: i token (`tokens used`) sono registrati. Claude:
`--agent-budget-usd` → `--max-budget-usd`. Ogni chiamata registra durata, token/costo, caratteri,
byte immagini, tentativi, cache hit (`agent_calls.jsonl`, `packaging_report.json`).

**Archetipi** (`render.py`, renderer v2): `reaction_focus` (volto dominante a tutta altezza, bordo interno
sfumato, nessuna cornice, scena dietro scurita, un solo elemento complementare sotto il titolo),
`board_focus` (carta/combo/board protagonista; carte grandi con ombra e filo chiaro; webcam piccola o
assente), `story_focus` (momento decisivo con lato testo forte, oppure due momenti divisi in diagonale).
I nomi V1 sono alias. Tipografia: altezza maiuscole reale, interruzioni senza articoli/preposizioni
sospese ("LISTA / DA RIVEDERE"), spazio per le maiuscole accentate, ombra morbida, contorno sottile.

**Selezione frame.** Burst di 4 s attorno al picco: nitidezza relativa al burst, penalità per animazioni
(differenza con i frame vicini) e affollamento, 2 candidati per highlight diversi tra loro e da tutti i
frame già scelti (dHash). Webcam: volto obbligatorio se presente nel burst, dimensione e nitidezza del
volto, risoluzione reale, niente espressioni quasi identiche.

**Quality Gate** (deterministico, nessuna chiamata AI): 16:9, testo nei margini e fuori dal badge durata,
testo che copre volto/carte, volto presente in `reaction_focus`, crop troppo piccoli, sfondo sfocato
(< 0.38 × nitidezza mediana dei frame del video, tarato su videoprova), contrasto nell'area del testo,
leggibilità a 320 px, varianti quasi identiche. Correzioni automatiche (max 3 render per concept): lato
testo/webcam, backdrop più scuro, una sola riduzione del testo, sfondo alternativo dello stesso
highlight, archetipo senza webcam. Le correzioni salvano i valori originali dell'agent e vengono
ri-derivate a ogni render (`render_tuning.auto`); un tuning scritto a mano viene mantenuto.

**Creator Style.** `packaging-style set` accetta `--webcam-size small|medium|large`, `--cards
prefer|auto|avoid`, `--text minimal|short`, `--typography impact|heavy|condensed`, `--layouts ...`; nel
prompt entrano come una riga compatta, con al massimo 6 esempi.

### Misure V2 (videoprova, ambiente isolato)

| | V1 deep, Codex (reale, prima) | V2 fast, Codex (reale, 1 run) |
|---|---|---|
| Chiamate | 3 | **1** (nessuna correzione) |
| Prompt | 23.4k + 43.5k + 31.3k caratteri | 19.6k caratteri + 5 immagini (1.4 MB) |
| Immagini | 9 + 2 + 11 | 5 |
| Token (riportati da Codex) | 61 916 + ~50 600 + 55 195 | **35 179** |
| Effort | default CLI (xhigh) | medium |
| Tempo agent | 1754 s | **134.5 s** |
| Totale pipeline | 1768 s | **147 s** (analisi locale da cache) |

Claude non è stato rieseguito in V2. Confronto grafico a parità di concept (stesse decisioni agent,
solo renderer + gate): `packaging_validation/videoprova/v2/*_before_after.jpg` (sinistra V1, destra V2).
Il run FAST reale è in `packaging_validation/videoprova/v2/codex_fast_1/` (ri-renderizzato con il
renderer finale; i PNG originali del run sono nella sua `history/` nello scratch di validazione).

## 1. Architettura

```text
Project (.autocutter) ── ProjectExportPlanner (lo stesso dell'export) ──┐
Final MP4 ───────────────────────────────────────────────────────────────┤
                                                                         ▼
                       EditMap  source ↔ edited (solo final keeps)
        ┌───────────────┬──────────────┬───────────────┬─────────────────┐
  Transcript (ASR)   Audio RMS     Keyframe change   GameContext (progetto / detector)
  FasterWhisperEngine  extract_rms_window  ffmpeg nokey   games_from_project / _detector
        └───────────────┴──────────────┴───────────────┴─────────────────┘
                                   ▼
                    Highlight Discovery (multi-segnale + copertura)
                                   ▼
          FrameExtractor (FFmpegFrameSampler) → frames/, webcam/, contact_sheets/
                                   ▼
   [agent] Phase A  VideoStory  (+ deepen: ReCut estrae intervalli richiesti, max N × 20 s)
                                   ▼
                 Cards: crop letti dall'agent + verifica col transcript
                                   ▼
   [agent] Phase B  Creative Packaging: concept = titolo + composition plan strutturato
                                   ▼
            ThumbnailRenderer (QPainter offscreen, 2× → 1280×720) → thumbnails/*.png
                                   ▼
                           packaging_report.json
```

Separazione netta: l'analisi locale produce **evidenze con ID** (T0001 transcript, H01 highlight,
F001 frame, W001 webcam, K01 carta); l'agent interpreta e propone citando solo ID; ReCut valida ogni
riferimento; il renderer produce i pixel. L'agent non vede file system, non esegue codice, non genera
immagini.

| Modulo (`analysis/packaging/`) | Ruolo |
|---|---|
| `timeline.py` | `EditMap`: mapping preciso source↔edited, spans, digest per range |
| `transcript.py` | transcript dei soli keep (chunk su griglia 120 s, overlap 3 s) via `FasterWhisperEngine` |
| `audio.py` | dB relativi alla baseline del contenuto tenuto, picchi 1 s |
| `visual.py` | cambi tra keyframe (decodifica solo keyframe, 160×90) |
| `games.py` | `GameContext` da progetto/detector + segmenti non-game |
| `highlights.py` | `HighlightCandidate` multi-segnale, copertura di ogni partita |
| `frames.py` | estrazione full-res, qualità frame, contact sheet, cache bundle |
| `webcam.py` | ROI normalizzata (CLI/profilo/auto), crop, qualità (proxy, non emozioni) |
| `cards.py` | crop carte lette dall'agent, stato verified/legible/uncertain, provider asset opzionale |
| `agent.py` | adapter `claude`/`codex` con immagini, cache, repair, log |
| `story.py` / `creative.py` | prompt, schema JSON strict, validazione |
| `render.py` | `ThumbnailRenderer`, 4 layout |
| `style.py` | Creator Style Memory versionata |
| `extensions.py` | `ImageGenerationProvider`, `TrendsProvider` (solo interfacce null in V1) |
| `cache.py` | cache separate: transcript, evidence, frames, agent, render |
| `pipeline.py` | orchestrazione, timing, rigenerazione parziale, report |
| `automation/packaging_cli.py` | comandi `packaging`, `packaging-style`, `packaging-feedback` |

## 2. CLI

```powershell
python main.py packaging --project "video.autocutter" --provider codex --language it --thumbnails 3 --output ".\packaging_output"
python main.py packaging --video "video_finale.mp4" --provider claude --games detect
python main.py packaging --project p.autocutter --analyze-only --output out      # nulla esce dalla macchina
python main.py packaging --output out --only titles                              # solo titoli, analisi riusata
python main.py packaging --output out --only thumbnails --provider claude        # solo thumbnail
python main.py packaging --output out --only render                              # dopo aver modificato thumbnail_concepts.json
python main.py packaging-style create Lugozzi --channel "Lugozzi"
python main.py packaging-style import Lugozzi --titles titoli.txt --thumbnails .\miniature
python main.py packaging-style set Lugozzi --colors "#FFD400,#38E1FF" --fonts Impact --webcam-roi 0.004,0.513,0.202,0.267
python main.py packaging-feedback --output out --select-title T3 --reject-thumbnail C2 --note "meno testo"
```

Opzioni principali: `--titles 5..10`, `--thumbnails 3..6`, `--webcam-roi auto|none|x,y,w,h`,
`--creator`, `--no-style`, `--games auto|project|detect|none`, `--asr-language`, `--asr-model`,
`--max-highlights`, `--max-deepen 0..4`, `--format png|jpg`, `--agent-model`, `--agent-effort`,
`--agent-timeout`, `--no-cache`, `--overwrite`, `--json`. Exit code: 0 ok, 2 argomenti/output
esistente, 20 errore analisi/agent (recuperabile: rilanciare lo stesso comando riusa le cache).

Output: una cartella esistente non viene mai sovrascritta senza `--overwrite`/`--only`; i file
sostituiti vengono **copiati** in `history/<timestamp>/`, mai cancellati.

```text
output/
  content_analysis.json  transcript.json  game_contexts.json  highlights.json
  video_story.json  title_candidates.json  thumbnail_concepts.json  cards.json
  packaging_report.json  agent_calls.jsonl  selection.json (dopo packaging-feedback)
  frames/  webcam/  cards/  contact_sheets/  thumbnails/concept_01.png … overview.jpg
  history/<timestamp>/   (versioni precedenti)
```

## 3. Mapping timestamp

`EditMap.from_project` usa `ProjectExportPlanner.build()` dell'exporter: stessi keep, stesso ordine,
stesso clamp del video renderizzato. `edited = edited_start(piece) + (source - source_start)`;
un istante tagliato non ha tempo edited (`to_edited → None`); `to_edited_nearest` lo porta allo
splice più vicino (marker VS che cadono in un taglio di silenzio). Transcript, highlight, frame,
report portano sempre entrambi i tempi.

## 4. Transcript e audio

ASR: `FasterWhisperEngine` esistente (stesso interprete esterno `AUTO_CUTTER_WHISPER_PY`, stessa cache
per finestra). I keep di ogni sorgente si uniscono attraverso tagli < 20 s, poi si spezzano su una
griglia fissa di 120 s di tempo source con 3 s di overlap: cambiare un keep invalida solo i chunk
toccati. Le parole con punto medio in contenuto tagliato vengono scartate e un taglio spezza la frase
in due righe. Lingua configurabile (`--asr-language`, default = `--language`, `auto` = rilevamento).
Senza faster-whisper il transcript è `unavailable` e nessun testo viene inventato.

Audio: `extract_rms_window` (filtro voce dell'editor) solo sugli span tenuti, dB relativi alla
mediana del contenuto tenuto. L'energia è evidenza di energia, non di emozione.

## 5. Highlight discovery

Segnali: `audio_peak`, `reaction` (lessico IT/EN di interiezioni, esclamazioni, risate — euristica
dichiarata), `speech_burst`, `pause_break`, `explanation` (≥ 25 s di parlato continuo: spiegazioni
strategiche/mazzo), `near_result` (ultimi secondi prima del risultato WIN), `game_intro`,
`visual_change`. I segnali vicini (14 s, stessa partita) diventano un candidato di 8–24 s con audio,
contesto transcript (righe ±4 s), frame before/peak/after e crop webcam. Lo score locale **ordina**
soltanto: decide l'agent. Ogni partita tenuta riceve almeno `per_game` candidati; una partita senza
segnali riceve un candidato `coverage` nel punto di parlato più denso.

## 6. Webcam

ROI normalizzata `x,y,w,h` da CLI, profilo creator o `auto`. `auto`: volto stabile (Haar cascade
OpenCV, locale) in ≥ 40 % dei frame campionati su tutto il video, poi bordi dell'overlay trovati
accumulando profili di gradiente sui frame (il bordo rettilineo più vicino al volto per lato, con
fallback testa-spalle per lato). Su videoprova: `[0.004, 0.513, 0.202, 0.267]`, i 4 lati da bordi.
ROI fuori dal frame → webcam disattivata con warning; parzialmente fuori → clamp con warning.
Per ogni highlight si scelgono 2 crop dal burst del picco con nitidezza, esposizione, volto presente,
centratura e attività (differenza tra frame: un *proxy*, mai un'emozione dichiarata).
Nei layout con webcam l'overlay webcam originale nel background viene sfocato e scurito per non
duplicare il volto.

## 7. Carte

Nessun database di carte/meta/combo. L'agent, in Phase A, può leggere carte nei frame del picco
inviati con una griglia 10×10 di riferimento e restituisce box normalizzato, nome o `null`,
leggibilità e righe transcript che la nominano. ReCut ritaglia il box dal frame full-res; se la
lettura non è `clear` prova i frame vicini (±1.5 s) e tiene il crop più nitido. Stato:
`verified` (leggibile + citata nel transcript, match fuzzy), `legible`, `uncertain` (mai nominata in
titoli o testo; non può essere il soggetto di `card_spotlight`). `CardAssetProvider` permette in
futuro artwork ufficiali con provenienza e licenza; V1 non scarica nulla.

## 8. Agent: Claude e Codex

| | Claude Code 2.1.296 | Codex CLI 0.161.0 |
|---|---|---|
| Invocazione | `claude -p --input-format stream-json --output-format stream-json --verbose` | `codex exec --sandbox read-only --ephemeral --skip-git-repo-check --ignore-rules` |
| Immagini | blocchi `image` base64 nel messaggio stream-json (verificato) | `--image=<file>` per immagine, prompt da stdin dopo `--` (verificato) |
| Output | `--json-schema`, evento `result.structured_output` | `--output-schema` + `-o answer.json` |
| Tool | `--tools ""`, `--strict-mcp-config`, `--no-session-persistence` | sandbox read-only, working dir vuota |
| Costi | `total_cost_usd`, token da `usage` | `tokens used` da stderr (nessun costo in $) |

Ogni chiamata gira in una directory temporanea con working dir **vuota**; le sole immagini del task
vengono copiate in `inputs/`. Nessuna modalità pericolosa, nessuna credenziale nei log
(`agent_calls.jsonl` contiene solo metadati). Il video MP4 non viene mai inviato: testo + immagini.
CLI mancante → errore `cli_missing` con istruzioni, non crash. Risposta non valida → un tentativo di
riparazione con l'errore di validazione; poi errore recuperabile, artifact locali conservati, nessun
titolo inventato. **Privacy:** il CLI stampa e il report registra che transcript, evidenze e frame
selezionati vengono inviati al provider configurato in quel CLI; `--analyze-only` non invia nulla.

Protocollo di approfondimento: Phase A può chiedere fino a `--max-deepen` intervalli (≤ 20 s,
tempi edited) per frame o webcam; ReCut li estrae (6 frame ciascuno), crea un contact sheet e
rilancia Phase A con l'analisi precedente come contesto.

## 9. VideoStory e titoli

Phase A (`packaging_story_v1`): tema principale, temi, perché è interessante, per partita giocate
specifiche vs discorsi generali sul mazzo (stesso mazzo per tutta la live: niente inferenze da classe
o keyword), highlight più forti, angoli narrativi diversi, letture carte, affermazioni rischiose.

Phase B (`packaging_creative_v1`): N concept (titolo + composition plan) e M titoli. Validazione:
ID esistenti, layout del catalogo, box normalizzati, colori `#RRGGBB`, testo thumbnail 1–5 parole
≤ 32 caratteri, titoli 15–100 caratteri non tutto maiuscolo e non duplicati, numeri solo se presenti
nelle righe transcript citate o nei dati partita, nessun nome di carta incerta, nessuna promessa di
CTR/visualizzazioni, badge WIN solo su partite WIN, almeno 2 layout e background diversi, angoli e
testi distinti. Lo stile del creator entra come ispirazione esplicita ("non copiare").

## 10. ThumbnailRenderer

PySide6 QPainter su `QImage` offscreen (nessuna nuova dipendenza), canvas 2560×1440 ridotto con
filtro smooth a 1280×720; PNG (JPEG automatico se > 2 MB, limite YouTube) o JPEG.
Layout: `split_reaction`, `board_focus`, `card_spotlight`, `cinematic_text` (vedi `LAYOUTS`).
Testo: maiuscolo, autofit (binary search sulla dimensione più grande che entra in ≤ 2–3 righe
bilanciate), contorno nero, ombra morbida, parola enfatizzata nel colore accento, spaziatura extra
per maiuscole accentate (È), margini di sicurezza 5 %, angolo in basso a destra libero (badge durata
YouTube). Ogni render riporta font px, altezza maiuscole a 320 px di larghezza, bounds e overflow.
Font di sistema caricati da `Windows\Fonts` (Impact, Bahnschrift, Arial Black…), non distribuiti.

## 11. Creator Style Memory

`config_root()/packaging/creators/<slug>/profile.json`, `revision` incrementale, revisioni precedenti
in `history/`. Campi: identità, canale, lingua, brand (colori, font, ROI webcam), statistiche titoli e
thumbnail derivate da esempi importati (copie locali), approvati, rifiutati, storico feedback.
Disattivabile (`--disable` o `--no-style`). Profilo vuoto ⇒ "nessuna preferenza". Nessun training.

## 12. Cache

| Namespace | Chiave | Effetto |
|---|---|---|
| ASR (esistente) | video + finestra + opzioni engine | chunk su griglia: invalidazione locale |
| `transcript` | digest edit + opzioni | righe ricomposte |
| `evidence` | identità range dei keep + parametri | RMS e keyframe per span |
| `frames` | identità video + intervallo + fps | burst JPEG (nessun pickle) |
| `agent` | task, provider, modello, effort, prompt version, hash prompt/schema/immagini | risposte valide |

Cambiare testo thumbnail → `--only render` (nessuna analisi, nessun agent). Cambiare provider → stessa
evidenza locale dalla cache. Cambiare i keep → digest dei range cambiati, il resto riusato.

## 13. Validazione reale (videoprova, ambiente isolato)

Progetto `videoprova` keep-games wins (2 WIN, 48 keep, 972 s montati), Windows, CPU, env isolato
(`AUTO_CUTTER_*` in cartella temporanea); stato utente reale (registry + AppData) invariato: 0 differenze.

| Fase | Codex (default xhigh) | Claude (default) |
|---|---|---|
| Transcript faster-whisper small, it (11 chunk, 179 righe) | 94 s (primo run) | 0.0 s (cache) |
| Audio RMS / keyframe visivi | 2.9 s / 12.5 s (primo run) | cache |
| Highlight (14) + frame/webcam (45 + 28) | 6.4 s | 4.7 s |
| Phase A VideoStory (9 immagini) | 516 s, 61 916 token | 154 s, $0.68 |
| Deepening (2 intervalli richiesti) + Phase A raffinata | 3.2 s + 457 s | 1.7 s + 56 s, $0.33 |
| Phase B creative (4 concept, 8 titoli) | 782 s | 76 s, $0.44 |
| Render 4 thumbnail | 0.9 s | 1.0 s |
| Totale | 1768 s | 296 s, **$1.45** |

Entrambi i provider hanno prodotto risposte valide al primo tentativo (nessuna riparazione).
`--only render` dopo una modifica: 2 s, nessuna chiamata agent. MP4 montato senza progetto
(`--games detect`): detector condiviso 35 s, 2 WIN + 2 segmenti di parlato, stessa ROI webcam.

Correzioni emerse dall'ispezione visiva dei PNG reali: overlay webcam originale che appariva come
blocco scuro al bordo (ora rimosso con inpainting), titolo che invadeva l'angolo del badge durata,
maiuscole accentate che toccavano la riga sopra, ROI webcam automatica troppo larga (ora bordi reali).

## 14. Limiti e sviluppi futuri

- Webcam a 1080p ≈ 390×290 px: nei pannelli grandi è upscalata (unsharp mask attenuato).
- Box carte stimati dall'agent: precisione limitata; per questo i crop hanno margine e stato esplicito.
- Codex non riporta costi in dollari; solo token.
- Trend YouTube e generazione immagini: solo interfacce.
- Nessuna schermata GUI in questa iterazione (comandi CLI e file JSON).
