> Aggiornamento Iteration 3: **NEEDS_MORE_CALIBRATION_DATA**. Workflow testato su `74.mp4`; policy single-frame implementata ma OFF, validation di `73.mp4` congelata. Vedi la nuova sezione finale e [guida calibration](GAMEPLAY_CALIBRATION.md).

Ultimo aggiornamento: **Iteration 5 — NEEDS_MORE_CALIBRATION_DATA**. Blind frozen: 10/11 complete, LOSS recall 75%, false LOSS 0, 3m27s. I risultati precedenti sotto sono conservati come storia; dettagli nella sezione Iteration 5 in fondo e nel [report state-aware](gameplay_validation/state_aware/README.md).

> Aggiornamento Iteration 2: **NEEDS_TUNING**. Vedi la sezione finale «Iteration 2 / Failure-driven tuning»; i risultati precedenti sono conservati come evidenze storiche.

# Validazione reale Hearthstone: 73.mp4

**Esito: NEEDS_TUNING. Gate per integrare gameplay cuts NON superato.**

Aggiornamento 8 ottobre 2026. OpenCV 4.12.0 è installato e usato realmente.
La scansione completa del VOD ricostruisce 13 GameSegment e 26 eventi automatici.
Sugli 11 giochi held-out ricostruisce correttamente 8 partite complete (6 WIN / 2 LOSS),
con precisione 88.89%, recall 72.73%, LOSS recall 50% e 0 false LOSS nel riferimento annotato.
Le 8 classificazioni abbinate sono corrette; questo non significa 11/11.

Nessun gameplay cut è stato generato o integrato in GUI, TrackState, preset,
project persistence o export. Nessun Power.log/HDT/telemetry, AI o PyTorch.

## Media, annotazioni e protocollo

- Media utente: 73.mp4, 1920×1080, H.264, circa 60 FPS, 5266117863 byte.
- Durata video: 6819.999667 s (1 h 53 m 40 s). Il file è stato solo letto.
- Fingerprint leggero: 69ab4eb4426b464b89643e86f4032791cc49328d08a70c75e6c659d87a4e0b24.
- Ground truth MANUALE provvisoria: 14 partite, 9 WIN / 5 LOSS.
- Calibrazione: prime 3 partite, intervallo [0,1400] s. Validation: 11 successive, 7 WIN / 4 LOSS.
- Intervallo di validation: [1400,6819.999667] s; tolleranza 5 s; matching uno a uno senza usare
  l’etichetta WIN/LOSS per scegliere gli abbinamenti.
- Le annotazioni restano complete=false: possono mancare altri eventi brevi.
  Precision/recall sono provvisori e limitati a questo piccolo riferimento.
- I timestamp sono anchor visuali osservati, con precisione manuale variabile;
  non sono boundary editoriali né ground truth annotata frame-per-frame.

Le annotazioni originali restano in tests/fixtures/gameplay/73_ground_truth.json e
73_held_out_ground_truth.json. Non sono state sostituite con predizioni.

| VOD # | Set | VS osservato | Risultato osservato | Classe manuale |
|---|---|---|---|---|
| 1 | calibrazione | 00:03:32.000 | 00:07:20.100 | WIN |
| 2 | calibrazione | 00:08:34.000 | 00:16:49.300 | WIN |
| 3 | calibrazione | 00:18:08.000 | 00:22:05.000 | LOSS |
| 4 | validation | 00:23:43.400 | 00:31:27.800 | WIN |
| 5 | validation | 00:33:13.600 | 00:38:25.000 | LOSS |
| 6 | validation | 00:38:59.200 | 00:48:06.400 | WIN |
| 7 | validation | 00:48:38.600 | 00:55:27.000 | WIN |
| 8 | validation | 00:56:14.000 | 01:02:32.100 | LOSS |
| 9 | validation | 01:03:16.000 | 01:07:41.400 | WIN |
| 10 | validation | 01:16:04.000 | 01:23:30.700 | LOSS |
| 11 | validation | 01:24:19.000 | 01:34:38.000 | WIN |
| 12 | validation | 01:35:30.000 | 01:41:02.000 | LOSS |
| 13 | validation | 01:41:35.000 | 01:45:54.000 | WIN |
| 14 | validation | 01:46:20.000 | 01:52:18.000 | WIN |

## Configurazione finale congelata

Analyzer hearthstone-visual-v3; detector hearthstone-template-v2;
OpenCV 4.12.0; grayscale TM_CCOEFF_NORMED. Confidence = correlazione misurata, non probabilità.
Fingerprint del pack: 65c08042c32508fd64e601a403b2c1b0f00583e30082777e4e248ba52c2401a0.

- Reference 960×540; viewport [0,0,1,1].
- ROI VS [0.4,0.34,0.2,0.3], scale [0.9,1,1.1].
- ROI VICTORY/DEFEAT [0.35,0.47,0.3,0.22], boundary di riferimento [336,253,624,373].
- Threshold 0.92 e margine di ambiguità 0.04 INVARIATI per tutte le classi.
- Scale risultato uniformi [0.9,0.925,0.95,0.975,1,1.025,1.05,1.075,1.1].
- I 3 PNG originali sono invariati: VS @ 214 s, VICTORY @ 1010 s, DEFEAT @ 1325 s.

Il pack originale è conservato separatamente. Solo la griglia di scale risultato
è stata modificata, usando i primi 3 giochi. Il pack è congelato alle 00:45:48 UTC,
prima della scansione calibrata avviata alle 00:46:34 UTC. Non sono state modificate soglie o template
dopo avere misurato le 11 partite di validation. Le finestre
locali di produzione derivano dai candidati visuali, non dai timestamp della ground truth.

### Sampling e stabilità

1. VS: 1 FPS, frame completo ridotto a 960 px; solo finestre candidate ±2 s, unite, a 6 FPS.
2. Risultati: 20 FPS sull’intero VOD, con crop FFmpeg PRIMA di resize/trasferimento;
   OpenCV riceve solo 288×120 pixel. Nessun frame completo 1080p a 20 FPS.
3. Solo cluster risultato instabili: ROI locale ±0.15 s a 120 FPS richiesti. Sul media
   a circa 60 FPS vengono letti soltanto i frame sorgente disponibili, senza sintesi.
4. Gap di debounce 0.5 s; VS richiede 3 osservazioni con span 0.25 s; i risultati 2 PTS distinti
   e span minimo 1/120 s. Ripetere lo stesso frame non aumenta il supporto.
5. ROI con bin temporali centrati: corretta la perdita di frame a 60 FPS causata dai
   PTS quantizzati a 1 ms (prima 80 frame / 2 s; ora 120 frame reali / 2 s).

La verifica di calibrazione ha confermato 3/3 RISULTATI con 3, 55, 2 osservazioni,
span 0.050/1.083/0.017 s. Questo dato è calibrazione, non accuracy held-out.

## Risultati delle tre esecuzioni reali

Per “partita ricostruita” si richiedono ENTRAMBI i boundary entro tolleranza.
Le metriche legacy possono abbinare predizioni incomplete e rimangono nel JSON
per compatibilità; non sono usate come headline del gate.

| Metrica (11 giochi held-out) | Baseline v1 | 20 FPS, pack originale | Finale congelato |
|---|---:|---:|---:|
| Giochi completi corretti | 5/11 | 6/11 | 8/11 |
| Giochi completi predetti | 6 | 7 | 9 |
| Game precision (completi) | 83.33% | 85.71% | 88.89% |
| Game recall (completi) | 45.45% | 54.55% | 72.73% |
| VS recall | 90.91% | 90.91% | 90.91% |
| Result recall | 54.55% | 63.64% | 81.82% |
| VICTORY recall | 85.71% | 100.00% | 100.00% |
| DEFEAT recall | 0.00% | 0.00% | 50.00% |
| WIN/LOSS accuracy (completi abbinati) | 100.00% | 100.00% | 100.00% |
| LOSS recall (giochi completi) | 0.00% | 0.00% | 50.00% |
| LOSS precision (giochi completi) | n/d | n/d | 100.00% |
| False LOSS (giochi completi) | 0 | 0 | 0 |
| False DEFEAT (eventi) | 0 | 0 | 0 |

Finale: 9 predizioni complete in validation, 8 abbinate correttamente e 1 pairing errato.
L’accuracy WIN/LOSS del 100% riguarda 8/8 partite complete abbinate (6 WIN / 2 LOSS);
risultati visuali classificati correttamente su 9/9 anchor abbinati.
Considerando anche le incomplete, la metrica legacy riporta 9 match / 11 e accuracy
8/9 = 88.89%: non dimostra 9 partite complete.

LOSS precision 2/2 = 100%; LOSS recall 2/4 = 50%; false LOSS (giochi) 0; false DEFEAT (eventi) 0;
false-positive LOSS rate sulle WIN annotate 0/7. Con 4 LOSS soltanto, lo zero non certifica sicurezza.
C’è 1 evento VICTORY extra rispetto agli anchor annotati; è elencato separatamente
nei JSON e non genera una nuova partita. È la continuazione della stessa schermata
Vittoria, separata dal debounce temporale (caso C nella tabella degli errori).

## Confronto per partita di validation

NONE significa nessuna partita COMPLETA correttamente abbinata. Gli anchor
parziali e il pairing sbagliato sono conservati nelle tabelle successive/JSON.

| Val / VOD | GT start | GT risultato @ tempo | Rilevato start → fine / risultato | Errore start / risultato (s) | Esito |
|---|---|---|---|---:|---|
| 1 / 4 | 00:23:43.400 | WIN @ 00:31:27.800 | 00:23:43.400 → 00:31:28.033 / WIN | 0.000 / 0.233 | OK |
| 2 / 5 | 00:33:13.600 | LOSS @ 00:38:25.000 | NONE | — / — | FALSE_NEGATIVE |
| 3 / 6 | 00:38:59.200 | WIN @ 00:48:06.400 | 00:39:00.233 → 00:48:06.483 / WIN | 1.033 / 0.083 | OK |
| 4 / 7 | 00:48:38.600 | WIN @ 00:55:27.000 | 00:48:38.583 → 00:55:26.983 / WIN | 0.017 / 0.017 | OK |
| 5 / 8 | 00:56:14.000 | LOSS @ 01:02:32.100 | 00:56:13.933 → 01:02:32.133 / LOSS | 0.067 / 0.033 | OK |
| 6 / 9 | 01:03:16.000 | WIN @ 01:07:41.400 | 01:03:16.100 → 01:07:41.333 / WIN | 0.100 / 0.067 | OK |
| 7 / 10 | 01:16:04.000 | LOSS @ 01:23:30.700 | 01:16:03.933 → 01:34:38.000 / WIN (pairing errato) | — / — | FALSE_NEGATIVE |
| 8 / 11 | 01:24:19.000 | WIN @ 01:34:38.000 | 01:16:03.933 → 01:34:38.000 / WIN (pairing errato) | — / — | FALSE_NEGATIVE |
| 9 / 12 | 01:35:30.000 | LOSS @ 01:41:02.000 | 01:35:30.400 → 01:41:01.700 / LOSS | 0.400 / 0.300 | OK |
| 10 / 13 | 01:41:35.000 | WIN @ 01:45:54.000 | 01:41:34.233 → 01:45:54.133 / WIN | 0.767 / 0.133 | OK |
| 11 / 14 | 01:46:20.000 | WIN @ 01:52:18.000 | 01:46:19.400 → 01:52:17.833 / WIN | 0.600 / 0.167 | OK |

Val 2 conserva lo start a 1993.583 s, ma end=None / result=UNKNOWN.
Val 7/8 sono attraversate dalla stessa predizione 4563.933→5678 WIN: risultato
WIN della seconda partita associato allo start della precedente LOSS.
È 1 falso GameSegment completo e 2 giochi ground truth non ricostruiti correttamente,
non una falsa LOSS.

## Errori temporali

| Anchor, solo match entro 5 s | N | MAE (s) | Mediana (s) | Max (s) |
|---|---:|---:|---:|---:|
| Start / VS | 10 | 0.307 | 0.083 | 1.033 |
| Risultato | 9 | 0.115 | 0.083 | 0.300 |

Le statistiche riguardano solo anchor abbinati; le omissioni sono conteggiate nel
recall. Non usare questi valori per nascondere le detection mancanti.
La baseline aveva start MAE/mediana 0.307/0.084 s e result MAE/mediana 0.441/0.533 s.
I valori subsecondo non superano la precisione delle annotazioni manuali.

## Eventi automatici finali: intero VOD

| Timestamp | Tipo | Similarity max | Frame distinti | Span (s) |
|---|---|---:|---:|---:|
| 00:03:33.583 | VS_SCREEN | 0.9998 | 20 | 3.667 |
| 00:07:20.150 | VICTORY | 0.9694 | 3 | 0.050 |
| 00:08:33.750 | VS_SCREEN | 0.9440 | 24 | 4.550 |
| 00:16:49.333 | VICTORY | 0.9999 | 24 | 1.350 |
| 00:18:08.100 | VS_SCREEN | 0.9493 | 36 | 6.133 |
| 00:22:04.983 | DEFEAT | 0.9999 | 2 | 0.017 |
| 00:23:43.400 | VS_SCREEN | 0.9506 | 36 | 6.133 |
| 00:31:28.033 | VICTORY | 0.9597 | 24 | 1.350 |
| 00:33:13.583 | VS_SCREEN | 0.9449 | 34 | 5.767 |
| 00:39:00.233 | VS_SCREEN | 0.9505 | 26 | 4.367 |
| 00:48:06.483 | VICTORY | 0.9987 | 92 | 6.850 |
| 00:48:38.583 | VS_SCREEN | 0.9499 | 16 | 2.800 |
| 00:55:26.983 | VICTORY | 0.9995 | 58 | 4.000 |
| 00:56:13.933 | VS_SCREEN | 0.9502 | 24 | 4.017 |
| 01:02:32.133 | DEFEAT | 0.9231 | 2 | 0.050 |
| 01:03:16.100 | VS_SCREEN | 0.9493 | 33 | 5.600 |
| 01:07:41.333 | VICTORY | 0.9996 | 93 | 6.800 |
| 01:16:03.933 | VS_SCREEN | 0.9505 | 32 | 5.417 |
| 01:34:38.000 | VICTORY | 0.9971 | 86 | 7.933 |
| 01:34:46.483 | VICTORY | 0.9211 | 2 | 0.050 |
| 01:35:30.400 | VS_SCREEN | 0.9440 | 32 | 5.600 |
| 01:41:01.700 | DEFEAT | 0.9761 | 3 | 0.033 |
| 01:41:34.233 | VS_SCREEN | 0.9450 | 31 | 5.250 |
| 01:45:54.133 | VICTORY | 0.9989 | 33 | 1.950 |
| 01:46:19.400 | VS_SCREEN | 0.9450 | 32 | 5.433 |
| 01:52:17.833 | VICTORY | 0.9952 | 8 | 0.550 |

## GameSegment automatici finali: intero VOD

| GameSegment | Start | Fine | Risultato | Note |
|---|---|---|---|---|
| 1 | 00:03:33.583 | 00:07:20.150 | WIN | — |
| 2 | 00:08:33.750 | 00:16:49.333 | WIN | — |
| 3 | 00:18:08.100 | 00:22:04.983 | LOSS | — |
| 4 | 00:23:43.400 | 00:31:28.033 | WIN | — |
| 5 | 00:33:13.583 | — | UNKNOWN | missing_end |
| 6 | 00:39:00.233 | 00:48:06.483 | WIN | — |
| 7 | 00:48:38.583 | 00:55:26.983 | WIN | — |
| 8 | 00:56:13.933 | 01:02:32.133 | LOSS | — |
| 9 | 01:03:16.100 | 01:07:41.333 | WIN | — |
| 10 | 01:16:03.933 | 01:34:38.000 | WIN | — |
| 11 | 01:35:30.400 | 01:41:01.700 | LOSS | — |
| 12 | 01:41:34.233 | 01:45:54.133 | WIN | — |
| 13 | 01:46:19.400 | 01:52:17.833 | WIN | — |

## Analisi degli errori A–F

A = frame utile non campionato; B = frame presente ma template non riconosciuto;
C = consolidamento/stabilità; D = pairing; E = soglia; F = ROI.

| Caso | Evidenza misurata | Livello / esito |
|---|---|---|
| LOSS a 2305 s | Pack congelato: massimo 0.94188 @ 2305.050 s. Lo scan globale a 20 FPS seleziona 2305.033 e 2305.083 s, saltando l’unico match nativo a 2305.050 s, che cade nello stesso bin 46101 del frame a 2305.033 s. Anche forzando 120 FPS locali, soltanto 1 frame supera 0.92. | A dimostrato: il frame utile non è campionato. Inoltre il supporto del template è troppo stretto (B): il gate di 2 frame (C) non può confermare neppure forzando il fine scan. FN rimane. |
| LOSS a 3752.1 s | La baseline a 1 FPS perde il banner; finale: 2 match a 3752.133/.183 s, span 0.050 s. | A/C della baseline risolti; LOSS ricostruita. |
| LOSS a 5010.7 s | Testo corretto dentro la ROI, massimo nativo 0.909909 @ 5010.716 s, sotto 0.92 anche con 9 scale. | B/E, FN rimane. Una specifica causa di scala/animazione era un’ipotesi, non provata dai soli score. |
| VS a 5059 s | VS visibile; peak su frame completo a 6 FPS: 0.91203; peak ROI a 6 FPS: 0.89954; entrambi sotto 0.92. | B/E, start mancante. Piccola differenza di resize/phase documentata. |
| LOSS a 6062 s | Un seed a 20 FPS, poi 3 PTS nativi a 6061.700/.716/.733 s, span 0.033 s. | Supporto temporale recuperato dal refinement; LOSS ricostruita. |
| VICTORY a 6738 s | Baseline senza finestra fine; banner breve di 0.10 s nel probe. | A/C baseline risolti; WIN ricostruita. |
| Val 7+8 | LOSS a 5010.7 s e VS a 5059 s mancanti; 4563.933→5678 WIN. | D: pairing errato come conseguenza di anchor mancanti. Non sono stati inventati nuovi boundary. |
| VICTORY a 5686.483 s | 2 frame, similarity 0.921095, span 0.050 s. È ancora la stessa schermata Vittoria con “Clicca per continuare”. Il cluster precedente termina a 5685.933 s: gap 0.55 s, maggiore del debounce di 0.5 s. | C dimostrato: frammentazione temporale dello stesso banner, conteggiata come 1 evento extra rispetto agli anchor GT. GameAssembler lo riunisce entro la finestra di 1.5 s: nessun GameSegment aggiuntivo. |
| VS a 2339.183 s | 2 frame / span 0.167 s, cluster rifiutato; successivo VS a 2340.233 s accettato. | C: start ritardato di 1.033 s, massimo errore start osservato. |

Tutti i peak diagnostici dei risultati mancanti contengono la scritta corretta
nella ROI: F non è indicato dalle evidenze di questo VOD.
Il probe locale a 20 FPS dopo seek può avere fase diversa dalla scansione globale; il probe
120 FPS copre tutti i frame sorgente della finestra e dimostra i limiti B/E/C.
Non sono state abbassate soglie per recuperare questi casi held-out.

Evidenze: [tracce baseline](gameplay_validation/73/baseline_diagnostics.json),
[tracce frozen](gameplay_validation/73/final_diagnostics.json),
[LOSS5010.716](gameplay_validation/73/debug_frames/005010.700_defeat_120fps_peak_0.9099_at_005010.716.jpg),
[LOSS2305.050](gameplay_validation/73/debug_frames/002305.000_defeat_120fps_peak_0.9419_at_002305.050.jpg).

## Performance

| Scansione completa | Tempo wall (s) | Coarse full | Fine full | ROI 20 FPS | ROI nativa locale |
|---|---:|---:|---:|---:|---:|
| Baseline v1 | 180.597 | 6817 | 912 | 0 | 0 |
| Solo 20 FPS, pack originale | 494.539 | 6817 | 617 | 136279 | 0 |
| Finale v3, pack congelato | 947.916 | 6817 | 617 | 136280 | 54 |

Core finale 947.770 s: coarse 196.773 s, fine 5.403 s, dense + refinement 745.592 s.
Tempo wall totale 15 min 47.916 s, circa 7.2 volte più veloce del VOD.
Le 3 finestre native sono [440.033,440.333], [1324.833,1325.133],
[6061.583,6061.883]: 0.9 s di video in totale, 54 frame ROI.
Lo scan fitto trasferisce immagini 288×120, circa 60 volte meno pixel di 1920×1080.
FFmpeg deve comunque decodificare i frame dipendenti del codec interframe.

Tempi indicativi su questa macchina; scan e test sono stati parzialmente
sovrapposti. Non è un benchmark isolato. Le 9 scale aumentano il costo rispetto
alle 3 originali. La memoria dei frame resta limitata dalle queue; osservazioni
limitate a 100000. Nessuna scansione di frame completi a 20 FPS.

## Cambiamenti implementati

- visual/sampler.py: ROI FFmpeg crop→resize, PTS reali, bin centrati, cancellation/retry.
- visual/detector.py: selezione classi, detect_roi/score_roi, geometria reference/viewport.
- hearthstone/analyzer.py: VS coarse + scan denso dei risultati indipendente dai coarse seed,
  refinement nativo locale dei cluster instabili, deduplicazione dei PTS, stabilità separata,
  progress monotono, contatori/timer/rejected_events, debug limitato per fase.
- Cache: versione e strategia effettiva nella chiave; niente riuso legacy per scan ROI.
- debug_analyze.py: --result-fps e --result-refine-fps, mantenendo CLI headless.
- evaluation.py: metriche complete, recall dei singoli anchor, mediane, confronti
  per gioco, errori di pairing e FP separati da giochi incompleti.
- Test: regressioni sampler/detector/evaluator e nuovo test_gameplay_dense_analyzer.py.
- Documentazione aggiornata; pack originale e calibrato/evidenze conservati separati.

Detection → eventi → consolidamento → GameAssembler rimane separato. Il detector non
produce Segment di cut; MainWindow/exporter/TrackState non sono stati modificati.
Non sono state aggiunte dipendenze in questa iterazione.

## Test e quality gate software

- Gameplay: 192 passed, 0 failed, 0 skipped.
- Suite completa: 641 passed, 0 failed, 1 skipped, in 56.42 s.
- Unico skip: Google SDK non disponibile per tests/test_youtube_upload.py.
- Ruff: PASS; mypy: PASS su 48 moduli; compileall: PASS.
- GUI smoke offscreen: exit 0; debug CLI --help headless: exit 0; git diff --check: PASS.
- Baseline nota 573 passed / 24 skipped: 23 test OpenCV prima saltati ora eseguiti,
  più 45 nuove regressioni; nessuna regressione osservata.

Comandi principali eseguiti:

```text
python -m pytest -o addopts= -q -rs [11 moduli tests/test_gameplay_*.py, espansi]
python -m pytest -o addopts= -q -rs
python -m ruff check .
python -m compileall -q analysis automation core export integrations ui utils widgets main.py
python main.py --smoke-test
python -m analysis.gameplay.hearthstone.debug_analyze --help
```

La lista completa dei moduli pytest, il comando mypy e gli override offscreen/cache
sono in [quality_gate.json](gameplay_validation/73/quality_gate.json). I wrapper
hanno usato cache dentro il workspace. I test non richiedono account reali.

## Riproduzione e artifact

[Artifact/istruzioni](gameplay_validation/73/README.md).
[Analisi baseline](gameplay_validation/73/baseline_analysis.json),
[Valutazione baseline](gameplay_validation/73/baseline_evaluation.json),
[Analisi 20 FPS, pack originale](gameplay_validation/73/dense20_original_analysis.json),
[Valutazione 20 FPS, pack originale](gameplay_validation/73/dense20_original_evaluation.json),
[Analisi finale](gameplay_validation/73/final_analysis.json),
[Valutazione finale](gameplay_validation/73/final_evaluation.json),
[calibrazione](gameplay_validation/73/calibration_summary.json),
[Pack congelato](gameplay_validation/73/template_pack/templates.json).

```powershell
python -m analysis.gameplay.hearthstone.debug_analyze "$env:USERPROFILE\Downloads\73.mp4" `
  --template-pack "docs/gameplay_validation/73/template_pack/templates.json" `
  --ground-truth "tests/fixtures/gameplay/73_held_out_ground_truth.json" `
  --output-dir ".pytest_cache/gameplay-real-73/reproduction" `
  --coarse-fps 1 --fine-fps 6 --result-fps 20 --result-refine-fps 120 --no-cache
```

Il pack di prova è distribuito come evidenza, non come default di produzione.
I PNG originali e le configurazioni sono archiviati, quindi una pulizia della
.pytest_cache non elimina il dossier. I JSON pubblicabili usano 73.mp4 come source
e percorsi macchina redatti; il fingerprint rimane verificabile.

## Limiti residui e decisione

**NEEDS_TUNING.** 8/11 non soddisfa il gate 11/11; LOSS recall 50% non soddisfa 100%.
Rimane 1 pairing errato; 0 false LOSS su 4 LOSS / 7 WIN non basta per autorizzare tagli.
L’attuale pack template non è abbastanza affidabile per rimuovere partite.
Le evidenze non dimostrano che serva PyTorch: prima occorrono patch/template
più rappresentativi delle animazioni, scelti su NUOVA calibrazione, e altri VOD
indipendenti per validation. Non riutilizzare questi 11 giochi come tuning set e
continuare poi a chiamarli held-out.

Altri limiti: un solo VOD/lingua/overlay; annotazioni provvisorie; nessuna misura
statistica di produzione; viewport non pieno non validato realmente e possibile
differenza di circa 1 pixel sorgente per ordine di arrotondamento/resize.
Cancellazione/retry sono testati; nessun test di account/network richiesto.
Restano rinviati gameplay cuts / timeline / override / persistence / preset / AI.

## Evidenze storiche: preparazione precedente

Il testo seguente è mantenuto per conservare le evidenze. Le frasi su OpenCV
mancante e detector non eseguito descrivono lo stato PRECEDENTE, superato sopra.

### Preparazione iniziale

Stato: preparazione della validazione, detector OpenCV non ancora eseguito.

## Media e verifiche eseguite

- VOD fornito dall'utente: 73.mp4, 1920x1080, H.264, circa 60 FPS.
- Durata del video:6819.999667 s (1 h 53 m 40 s); dimensione:5266117863 byte.
- Sampler reale:30 frame a 1 FPS nell'intervallo200..230 s in0.911 s su questa macchina.
- Cancellazione del subprocess verificata; latenza osservata circa0.011 s.
- Retry successivo riuscito sugli stessi dati.
- Il file sorgente è stato solo letto.

Questi sono controlli del sampler, non misure di accuratezza del detector.

## Riferimento provvisorio

L'ispezione visiva dei frame ha ricostruito 14 partite: 9 WIN e 5 LOSS.
Le classi sono state lette dalle scritte italiane Vittoria/Sconfitta, senza inferirle
soltanto da board, rank o animazione del ritratto. Gli anchor sono timestamp di
frame osservati, non boundary editoriali esatti.

- tests/fixtures/gameplay/73_ground_truth.json: riferimento dell'intero VOD.
- tests/fixtures/gameplay/73_held_out_ground_truth.json:11 partite con inizio dopo 1400 s.
- Le prime 3 partite sono riservate alla selezione dei template/calibrazione.
- Entrambi i file mantengono complete=false: il campionamento della ricognizione
  può perdere eventi molto brevi. Precision/recall futuri saranno provvisori.
- Il fingerprint leggero del media evita l'abbinamento al VOD sbagliato.

## Casi critici osservati

| Anchor | Timestamp osservato | Evidenza |
|---|---:|---|
| VICTORY |440.1s|Visibile a440.1s, assente a440.0 e440.2s|
| DEFEAT |3752.1s|Visibile a3752.1 e3752.2s, assente a3752.0 e3752.3s|
| DEFEAT |5010.7s|Visibile a5010.7s, assente a5010.6 e5010.8s|

Sono osservazioni a 10 FPS, non durate misurate frame-per-frame.
La scansione coarse a 1 FPS può saltare questi banner. Inoltre il requisito corrente
minimum_stability_s=0.25 può respingere una detection vera ma brevissima. Non sono
state abbassate le soglie senza misurare i falsi positivi. Questa limitazione è
aperta; non si può promettere il recupero di tutte le sconfitte con i default.

## Asset locali e riproduzione

Le anteprime, le evidenze e il pack reale sono in:

    .pytest_cache/gameplay-real-73/
        annotation_early/
        annotation_middle/
        annotation_late/
        template_pack/templates.json
        sampler_validation.json
        ground_truth.json
        held_out_ground_truth.json

Il pack locale contiene tre patch estratte con FFmpeg: VS a 214 s, Vittoria a 1010 s,
Sconfitta a 1325 s. Le ROI escludono webcam/chat. Le soglie rimangono non calibrate.
Le immagini non vengono distribuite come template già validati nelle risorse.
Gli artifact sotto .pytest_cache sono ignorati da Git e possono essere rimossi
quando si pulisce la cache; i due JSON di riferimento sono invece nella repository.

Dopo installazione di requirements-gameplay.txt, dalla root:

    python -m analysis.gameplay.hearthstone.debug_analyze "$env:USERPROFILE\Downloads\73.mp4" --template-pack ".pytest_cache/gameplay-real-73/template_pack/templates.json" --ground-truth "tests/fixtures/gameplay/73_held_out_ground_truth.json" --output-dir ".pytest_cache/gameplay-real-73/detector_report" --debug-frames

Questo comando non è ancora riuscito nell'ambiente di implementazione: manca cv2
e pip non può scaricarlo per errore socket Windows 10013. La CLI restituisce errore 2
con un messaggio esplicito sulla dipendenza mancante. Nessun risultato automatico,
confidence di matching o precision/recall su questo VOD viene dichiarato.

## Passo successivo

1. Installare OpenCV nell'interprete selezionato e avviare i test CV prima saltati.
2. Eseguire il pack sul VOD e confrontare gli 11 giochi fuori calibrazione.
3. Misurare score, margini, falsi positivi e mancati eventi brevi.
4. Valutare un trigger visivo economico per le finestre fini anche quando il coarse
   perde la scritta; verificarne costo e precisione prima di cambiare i default.
5. Rimanere su template/ROI finché non ci sono evidenze che il metodo sia insufficiente.

Non è stata abilitata alcuna rimozione automatica delle LOSS.


## Iteration 2 / Failure-driven tuning

Registro iniziale, prima delle modifiche: analisi delle cause completata, calibrazione e validation ancora in preparazione. Esito finale dell’iterazione: **NEEDS_TUNING**, documentato nelle sezioni successive. I risultati precedenti restano invariati.

I codici di questa sezione seguono la nuova richiesta: A sampling, B ROI errata, C score sotto soglia, D variante non rappresentata, E consolidamento, F assembler, G altro.

| Failure | Frame/score già misurati | Causa accertata prima del tuning |
|---|---|---|
| LOSS 2305 s | Scan20FPS:2305.033→0.5340,2305.083→0.1796; nativo2305.050→0.941881,unico frame sopra0.92 | A: il frame utile è saltato. E: anche forzando decode nativo resta una sola osservazione, respinta dal gate2frame. ROI corretta. D non ancora dimostrato. |
| LOSS5010.7s |20FPS5010.783→0.851311; nativo5010.716→0.909909 | C: testo presente e ROI corretta, score sotto0.92. Una causa specifica di animazione/variante(D) rimane ipotesi. |
| VS5059s |Full6FPS5063→0.91203; ROI6FPS5060.433→0.899543 | C: screen osservato ma sotto0.92; non un errore di ROI. Cursor/phase vanno verificati sulla calibrazione. F è la conseguenza sul pairing. |

Al momento di questa analisi iniziale non erano ancora stati modificati threshold o template. Il vincolo fissato allora è stato rispettato: tutti i nuovi template hanno timestamp nei primi1400s; nessun frame held-out è stato usato per tuning.

### Configurazione congelata e audit di calibrazione

Il registro sopra descrive lo stato PRIMA delle modifiche. Questa iterazione usa
esclusivamente le prime 3 partite / 0–1400 s per scegliere template e impostazioni.
Il pack precedente resta intatto; il nuovo è in
[iteration2/template_pack](gameplay_validation/73/iteration2/template_pack/templates.json).
Il [manifest](gameplay_validation/73/iteration2/frozen_manifest.json) contiene
fingerprint del pack, impostazioni e SHA-256 del codice congelati prima della nuova
esecuzione held-out. Nessun retuning è consentito dopo quella esecuzione.

- Threshold finale: **0.92** per tutte le classi; margine fra classi **0.04**.
- ROI, grayscale e griglie di scala restano invariati. Il formato dei pack supporta
  già configurazioni separate per classe; non serve introdurre nuove euristiche.
- VS: 1 FPS coarse; candidati >=0.80 attivano finestre ±2 s a 6 FPS.
  L'evento richiede comunque >=3 frame sopra soglia e span >=0.25 s.
- Risultati: 20 FPS, ROI 288×120, crop in FFmpeg prima di resize/trasferimento.
  Un candidato debole >=0.80 o un cluster forte instabile attiva soltanto una
  finestra locale ±0.15 s fino a 120 FPS richiesti / circa 60 frame sorgente reali.
  La soglia 0.80 NON produce eventi accettati. Restano necessari >=2 timestamp
  sorgente distinti sopra 0.92, span >=1/120 s e margine >=0.04.
- Matching singolo: API candidati e score condividono lo stesso passaggio OpenCV;
  non si ripete il matching per decidere se avviare il refinement.
- Versioni: hearthstone-visual-v4 / hearthstone-template-v3. La cache include
  impostazioni, pack e capacità effettive del detector; replay non conta come
  seconda osservazione. Progress e cancellazione restano operativi.

Nuovi template, tutti da calibrazione:

| Classe | Timestamp sorgente | Rettangolo alla geometria 960×540 | Motivo |
|---|---:|---|---|
| VS_SCREEN |216.783|431,204,123,134|Variante senza cursore incorporato nel vecchio template|
| DEFEAT |1324.900|432,310,101,30|Fase iniziale del testo animato|
| DEFEAT |1324.933|422,321,114,28|Fase con overshoot / testo ingrandito|

Sono aggiunti ai 3 PNG originali, non ricavati dalle 11 partite di validation.
Il crop della seconda fase DEFEAT è stato verificato visivamente: il miglior
match del vecchio template a quel timestamp puntava a una bordatura, non al testo;
un primo crop automatico errato è stato scartato PRIMA di congelare il pack.

Calibrazione empirica su 782 frame di gameplay interni alle tre partite,
escludendo ±30 s dagli anchor. Massimi delle classi sui negativi:
VICTORY 0.4382, DEFEAT 0.4508, VS 0.3351; nessun candidato >=0.80.
Questa separazione sostiene il gate debole conservativo; non è una stima di
precisione su altri VOD. I punteggi sono correlazioni normalizzate, non probabilità.

Sulle finestre native di calibrazione, DEFEAT passa da 2 a 5 frame accettati;
VICTORY breve resta 3 frame, VICTORY lunga resta 71. I massimi VS passano da
0.9998 / 0.9440 / 0.9493 a 0.9999 / 0.9940 / 0.9982.
Non è emersa evidenza per cambiare preprocessing/threshold/ROI di VICTORY.
La vulnerabilità alle fasi animate e al contenuto variabile dei template è quindi
visibile anche sui dati di calibrazione, senza usare validation per catturare patch.

Audit reale di tutti i 1400 s di calibrazione: **3/3 complete, 3/3 VS,
3/3 risultati, 2 WIN + 1 LOSS corrette, zero false DEFEAT**; 276.39 s,
1400 coarse / 162 fine / 28001 dense ROI / 51 refinement.
Lo span a 20 FPS e i frame nativi necessari dipendono dai PTS reali, non da frame
sintetici. Audit e distribuzioni sono archiviati in calibration_analysis.json,
calibration_evaluation.json e calibration_summary.json sotto iteration2/.

### Assembler e definizioni delle metriche

Il consolidamento dell'assembler ora rispetta le barriere cronologiche fra start
e risultati: non fonde due WIN vicine attraversando un VS, né due VS attraverso
un risultato. Un risultato successivo a un gioco terminato resta orfano se manca
lo start; un nuovo VS non diventa la fine del precedente gioco incompleto.

Il core generico offre limiti configurabili min/max VS→risultato. Il profilo
Hearthstone usa massimo 1800 s e nessun minimo arbitrario: una concessione può
avvenire presto. Un abbinamento troppo lungo diventa start-only UNKNOWN più
result-only con start=None. **1800 s è un guard configurabile, non una durata
appresa o una garanzia**: se mancano contemporaneamente una fine e lo start
successivo, una coppia apparentemente plausibile entro il limite può restare
ambigua. Nessun timestamp viene inventato. Questo limite non è stato scelto per
respingere l'intervallo errato storico di 1114 s.

Nuove metriche separano:

- recall degli anchor VS / VICTORY / DEFEAT, indipendente dall'assembler;
- risultato classificato correttamente con end abbinato, indipendente dallo start;
- ricostruzione completa: ENTRAMBI gli anchor entro 5 s;
- ricostruzione partial: un confine mancante, assegnata solo alle annotazioni non
  già abbinate a una previsione completa; non gonfia il recall completo;
- somma disgiunta complete+partial, missing_start vs missing_result e pairing errati.

Le metriche legacy rimangono disponibili per compatibilità. Il BEFORE è ricalcolato
sull'analisi v3 originale con le nuove definizioni, senza rieseguire/toccare il
vecchio detector. BEFORE: 8 complete corrette / 9 complete previste, 1 partial
start-only abbinata, 1 pairing errato; 9/11 giochi rappresentati includendo partial.

### Quality gate Iteration 2

- Gameplay: **209 passed, 0 failed, 0 skipped**.
- Suite completa: **658 passed, 0 failed, 1 skipped**, 47.33 s.
- Skip: googleapiclient.http non installato; test opzionale del vero SDK YouTube.
- Ruff: All checks passed.
- Mypy: no issues in 48 source files, stesso scope precedente.
- GUI: python main.py --smoke-test, exit 0, Qt offscreen e storage di smoke isolato.
- Nessuna modifica a timeline, exporter, TrackState, project persistence o cut policy.

Comandi: python -m pytest -o addopts= -q sui test_gameplay_*.py; python -m pytest
-o addopts= -q -rs sull'intera suite; python -m ruff check .; python -m mypy sullo
scope automation, analysis/gameplay e moduli già controllati dal progetto.
I nuovi test coprono start mancanti, risultati mancanti/isolati, barriere fra
partite vicine, durate implausibili, weak-only che NON diventa LOSS, refinement
senza strong seed, cache per capability e metriche partial senza doppio conteggio.

### Nuova validation congelata: BEFORE / AFTER

Esito: **NEEDS_TUNING**. Tolleranza 5 s, 11 giochi: 7 WIN / 4 LOSS.

Le 11 partite non hanno fornito template o valori di tuning. Essendo già state ispezionate per failure analysis, questa è validation riusata, non un nuovo test cieco. Servono altri VOD indipendenti.

| Metrica | BEFORE v3 | AFTER v4 |
|---|---:|---:|
|Ground truth games|11|11|
|Complete previste|9|9|
|Complete abbinate|8|9|
|Partial previste|1|2|
|Partial abbinate (esclusi match completi)|1|2|
|Game precision (complete)|88.89%|100.00%|
|Game recall (complete)|72.73%|81.82%|
|Partial reconstruction recall|9.09%|18.18%|
|Complete + partial recall|81.82%|100.00%|
|VS event recall|90.91%|100.00%|
|Result event recall|81.82%|81.82%|
|VICTORY event recall|100.00%|100.00%|
|DEFEAT / LOSS result recall|50.00%|50.00%|
|LOSS result precision|100.00%|100.00%|
|LOSS complete recall|50.00%|50.00%|
|LOSS complete precision|100.00%|100.00%|
|WIN/LOSS accuracy (complete abbinate)|100.00%|100.00%|
|Result classification accuracy (end abbinato)|100.00%|100.00%|
|False LOSS (eventi)|0|0|
|False LOSS (giochi)|0|0|
|Eventi risultato spuri/frammentati|1|1|
|Start anchor error matched anchors|10|11|
|Start anchor error mean_absolute_error_s (s)|0.307|0.224|
|Start anchor error median_absolute_error_s (s)|0.083|0.100|
|Start anchor error max_absolute_error_s (s)|1.033|0.767|
|Result anchor error matched anchors|9|9|
|Result anchor error mean_absolute_error_s (s)|0.115|0.087|
|Result anchor error median_absolute_error_s (s)|0.083|0.050|
|Result anchor error max_absolute_error_s (s)|0.300|0.384|

Tempo wall: BEFORE **947.916 s**, AFTER **1598.337 s**. Core precedente 947.770 s; nuovo 1598.224 s. Confronto indicativo sulla stessa macchina, senza controllo di clock/load termico.

### Confronto per partita (validation #, VOD # = validation # + 3)

|#|GT start|GT end|GT result|Detected start|Detected end|Result|Start error s|Result error s|Stato|
|---|---|---|---|---|---|---|---:|---:|---|
|1|00:23:43.400|00:31:27.800|WIN|00:23:43.400|00:31:27.850|WIN|0.000|0.050|OK|
|2|00:33:13.600|00:38:25.000|LOSS|NONE|NONE|NONE|n/a|n/a|FALSE_NEGATIVE|
|3|00:38:59.200|00:48:06.400|WIN|00:38:59.050|00:48:06.450|WIN|0.150|0.050|OK|
|4|00:48:38.600|00:55:27.000|WIN|00:48:38.583|00:55:26.983|WIN|0.017|0.017|OK|
|5|00:56:14.000|01:02:32.100|LOSS|00:56:13.933|01:02:32.133|LOSS|0.067|0.033|OK|
|6|01:03:16.000|01:07:41.400|WIN|01:03:16.100|01:07:41.333|WIN|0.100|0.067|OK|
|7|01:16:04.000|01:23:30.700|LOSS|NONE|NONE|NONE|n/a|n/a|FALSE_NEGATIVE|
|8|01:24:19.000|01:34:38.000|WIN|01:24:19.400|01:34:38.000|WIN|0.400|0.000|OK|
|9|01:35:30.000|01:41:02.000|LOSS|01:35:30.283|01:41:01.616|LOSS|0.283|0.384|OK|
|10|01:41:35.000|01:45:54.000|WIN|01:41:34.233|01:45:54.016|WIN|0.767|0.016|OK|
|11|01:46:20.000|01:52:18.000|WIN|01:46:19.400|01:52:17.833|WIN|0.600|0.167|OK|

La tabella richiede entrambi i confini corretti. Un nearby_prediction mostrato su una riga FALSE_NEGATIVE non è un match. Le comparazioni raw degli anchor e quelle partial sono in after_evaluation.json.

### Eventi e giochi rilevati

|Evento|Timestamp|Peak similarity|Frame di supporto|Template del picco|
|---|---|---:|---:|---|
|VS_SCREEN|00:23:43.400|0.999652|36|templates/vs/calibration_cursor_free.png|
|VICTORY|00:31:27.850|0.959740|31|templates/victory/victory_01.png|
|VS_SCREEN|00:33:13.583|0.995215|34|templates/vs/calibration_cursor_free.png|
|VS_SCREEN|00:38:59.050|0.999731|32|templates/vs/calibration_cursor_free.png|
|VICTORY|00:48:06.450|0.998688|182|templates/victory/victory_01.png|
|VS_SCREEN|00:48:38.583|0.998193|31|templates/vs/calibration_cursor_free.png|
|VICTORY|00:55:26.983|0.999549|109|templates/victory/victory_01.png|
|VS_SCREEN|00:56:13.933|0.997676|36|templates/vs/calibration_cursor_free.png|
|DEFEAT|01:02:32.133|0.923096|2|templates/defeat/defeat_01.png|
|VS_SCREEN|01:03:16.100|0.998332|33|templates/vs/calibration_cursor_free.png|
|VICTORY|01:07:41.333|0.999599|175|templates/victory/victory_01.png|
|VS_SCREEN|01:16:03.933|0.999879|32|templates/vs/calibration_cursor_free.png|
|VS_SCREEN|01:24:19.400|0.957872|28|templates/vs/calibration_cursor_free.png|
|VICTORY|01:34:38.000|0.997105|205|templates/victory/victory_01.png|
|VICTORY|01:34:46.483|0.921531|4|templates/victory/victory_01.png|
|VS_SCREEN|01:35:30.283|0.994123|34|templates/vs/calibration_cursor_free.png|
|DEFEAT|01:41:01.616|0.976086|8|templates/defeat/defeat_01.png|
|VS_SCREEN|01:41:34.233|0.995175|31|templates/vs/calibration_cursor_free.png|
|VICTORY|01:45:54.016|0.998941|52|templates/victory/victory_01.png|
|VS_SCREEN|01:46:19.400|0.995139|32|templates/vs/calibration_cursor_free.png|
|VICTORY|01:52:17.833|0.995227|22|templates/victory/victory_01.png|

|Game index VOD|Start|End|Result|Issues|
|---|---|---|---|---|
|4|00:23:43.400|00:31:27.850|WIN|—|
|5|00:33:13.583|NONE|UNKNOWN|missing_end|
|6|00:38:59.050|00:48:06.450|WIN|—|
|7|00:48:38.583|00:55:26.983|WIN|—|
|8|00:56:13.933|01:02:32.133|LOSS|—|
|9|01:03:16.100|01:07:41.333|WIN|—|
|10|01:16:03.933|NONE|UNKNOWN|missing_end|
|11|01:24:19.400|01:34:38.000|WIN|—|
|12|01:35:30.283|01:41:01.616|LOSS|—|
|13|01:41:34.233|01:45:54.016|WIN|—|
|14|01:46:19.400|01:52:17.833|WIN|—|

### Profiling performance

|Componente|Wall fase s|Matching s|Attesa sampling s|Startup + primo frame s (incluso nell’attesa)|Stream FFmpeg|
|---|---:|---:|---:|---:|---:|
|coarse|151.580|22.648|128.398|0.126|1|
|fine|5.982|2.907|2.596|1.710|14|
|dense|1417.244|1405.753|4.274|0.127|1|
|refinement|23.416|16.793|6.533|6.406|45|

Frame: **6817 coarse**, **722 fine**, **136280 dense ROI**, **1656 refinement**. Weak result seeds: 301; finestre native: 45.

L’attesa sampling misura next() del generatore: include startup FFmpeg, decodifica iniziale, pipe/queue wait e cleanup EOF; non è tempo CPU puro del decoder. Startup + primo frame è una sua sottocomponente, non va sommata. FFmpeg lavora in parallelo al matcher e può attendere backpressure. Il maggior costo previsto/misurabile è il matching ROI, passato da 18 a 36 varianti template/scala per frame. Il benchmark 200 ROI di calibrazione con 32/1/2 thread è circa 2.09 s in tutti i casi: nessuna modifica dei thread giustificata. Le varianti sono precompilate, il grayscale è condiviso nello stesso passaggio e il weak gate non esegue un secondo matching.

### Failure rimasti e decisione

Failure raw: [{"ground_truth_index": 2, "vs": {"ground_truth": {"timestamp": 1993.6, "type": "VS_SCREEN"}, "detected": {"timestamp": 1993.583, "type": "VS_SCREEN", "confidence": 0.9952146410942078, "source": "template_matching", "metadata": {"candidate_only": false, "template_id": "templates/vs/calibration_cursor_free.png", "similarity_method": "TM_CCOEFF_NORMED", "raw_similarity": 0.9952146410942078, "second_best_similarity": -1.0, "class_margin": 1.9952146410942078, "class_scores": {"VS_SCREEN": 0.9952146410942078}, "threshold": 0.92, "scale": 1.0, "matched_rect_normalized": [0.44895833333333335, 0.37777777777777777, 0.128125, 0.24814814814814815], "detector_version": "hearthstone-template-v3", "observation_count": 34, "first_seen": 1993.583, "last_seen": 1999.35, "peak_timestamp": 1998.133, "temporal_span_s": 5.766999999999825, "sampling_fps": 6.0}}, "absolute_error_s": 0.016999999999825377, "status": "OK"}, "result": {"ground_truth": {"timestamp": 2305.0, "type": "DEFEAT"}, "detected": null, "absolute_error_s": null, "status": "FALSE_NEGATIVE"}}, {"ground_truth_index": 7, "vs": {"ground_truth": {"timestamp": 4564.0, "type": "VS_SCREEN"}, "detected": {"timestamp": 4563.933, "type": "VS_SCREEN", "confidence": 0.9998794198036194, "source": "template_matching", "metadata": {"candidate_only": false, "template_id": "templates/vs/calibration_cursor_free.png", "similarity_method": "TM_CCOEFF_NORMED", "raw_similarity": 0.9998794198036194, "second_best_similarity": -1.0, "class_margin": 1.9998794198036194, "class_scores": {"VS_SCREEN": 0.9998794198036194}, "threshold": 0.92, "scale": 1.0, "matched_rect_normalized": [0.44895833333333335, 0.37777777777777777, 0.128125, 0.24814814814814815], "detector_version": "hearthstone-template-v3", "observation_count": 32, "first_seen": 4563.933, "last_seen": 4569.35, "peak_timestamp": 4567.083, "temporal_span_s": 5.417000000000371, "sampling_fps": 6.0}}, "absolute_error_s": 0.06700000000000728, "status": "OK"}, "result": {"ground_truth": {"timestamp": 5010.7, "type": "DEFEAT"}, "detected": null, "absolute_error_s": null, "status": "FALSE_NEGATIVE"}}]
False DEFEAT: 0. Risultati non abbinati: [{"timestamp": 5686.483, "type": "VICTORY"}]

Il template matching resta una strada da verificare con altri dati di calibrazione e VOD indipendenti. Non è stato introdotto PyTorch: questa iterazione non costituisce prova che un classifier sia necessario. Le cause C misurano score insufficienti, non dimostrano da sole inseparabilità delle classi. Eventuali varianti nuove richiedono calibrazione indipendente, non patch tratte da queste 11 partite.

Nessuna rimozione automatica autorizzata dal solo risultato su questo piccolo VOD. Restano annotazioni provvisorie, una lingua/capture, supporto limitato alle varianti osservate e il caso intrinsecamente ambiguo di due anchor consecutivi mancanti entro una durata plausibile. Non sono cambiati editor, exporter, TrackState o formato progetto.

Integrità: fingerprint 9a3528d080912ef286cd90531cef65f52f9c9fc9ae57f73bdbdd8525756f5224; hash del codice invariati prima/dopo, retuning_after_validation=false.

### Diagnostica post-validation dei tre failure (nessun retuning)

I frame in [iteration2/debug_frames](gameplay_validation/73/iteration2/debug_frames/)
sono diagnostici, NON template. Lo stesso pack congelato è stato usato anche per
questo controllo locale. ROI corretta in tutti i casi.

|Caso|BEFORE|AFTER|Causa attuale|
|---|---|---|---|
|LOSS 2305 s|Il 20 FPS salta il solo frame nativo da0.941881; frame vicino2304.983≈0.8574|Weak seed attiva2304.833–2305.183; il frame2305.050 viene recuperato a0.941881, ma rimane l'unico sopra0.92|**A risolto; E effettivo**: count1/span0 in rejected_events. I due template aggiunti non ampliano il supporto sopra soglia su questa LOSS. Nessuno start/end inventato.|
|LOSS 5010.7 s|Nativo5010.716→0.909909|Weak seed attiva5010.583–5010.933; massimo ancora5010.716→0.909909, zero frame accettati|**C persistente**: frame presente, ROI corretta, similarity sotto0.92. Il picco usa ancora il vecchio template. Non è un errore dell'assembler.|
|VS 5059 s|Picco full≈0.91203, sotto soglia|Evento5059.400; picco diagnostico5060.983→0.957872,28 frame accettati nella finestra a6FPS|**C risolto** con variante calibration_cursor_free.png. Il vecchio template incorporava il cursore; cursor/phase non sono stati isolati con un'ablation, quindi non si attribuisce tutto al solo cursore.|

I picchi delle due LOSS continuano a scegliere defeat_01.png: le nuove fasi
migliorano la calibrazione ma **non generalizzano a questi due casi**. Non sono
stati aggiunti template dai frame di validation e non è stato allentato il gate
a un singolo frame. Dettagli per ogni frame in
[failure_diagnostics_after.json](gameplay_validation/73/iteration2/failure_diagnostics_after.json).

A2305 il margine DEFEAT–VICTORY è0.5492; a5010.716 è0.5869. Le classi non sono
vicine nei frame dei picchi: queste evidenze non dimostrano un problema di
inseparabilità WIN/LOSS che giustifichi PyTorch. La causa confermata a5010 rimane C;
D/variante non coperta è compatibile con i dati, ma non si identifica una specifica
variante grafica soltanto dal mancato superamento della soglia.

|Validation game|Start rilevato|End|Result|Stato partial|
|---|---:|---|---|---|
|2|1993.583|NONE|UNKNOWN|START_DETECTED_RESULT_MISSING|
|7|4563.933|NONE|UNKNOWN|START_DETECTED_RESULT_MISSING|

Il pairing storico4563.933→5678 è scomparso perché il VS5059.4 ora separa le due
partite. Il guard1800s non sarebbe bastato a respingere quella coppia di1114s e non
viene presentato come la causa del recupero. Le nuove barriere e i test proteggono
l'assembler da slittamenti quando un solo start/result manca; due anchor adiacenti
mancanti possono restare intrinsecamente ambigui.

Resta1 evento VICTORY extra a5686.483: è la frammentazione del banner già documentata
nella precedente iterazione. L'assembler la riconcilia con la VICTORY precedente;
non crea una partita extra. Quindi **0 false LOSS, 0 giochi spuri, 0 pairing errati**
su queste annotazioni, ma non zero falsi positivi raw: c'è1 frammento VICTORY non
abbinato alla ground truth. Non è stata modificata la ground truth per nasconderlo.

**Decisione: B — nuovi dati di calibrazione indipendenti.** Il template matching è
ancora promettente per gli start e discrimina le classi sui picchi osservati, ma
questo pack NON è sufficiente per recuperare tutte le LOSS. Prima di ML servono
altri esempi indipendenti delle animazioni/qualità di cattura e un confronto
calibration-only di patch testuali/preprocessing semplice, seguito da un nuovo
VOD di test. Nessun ulteriore tuning su queste11 partite.

Costo: **26m38s** contro15m48s, circa+68.6%. Matching globale risultati1405.75s,
circa88% del wall; matching totale circa90.6%. Il refinement45 finestre/1656 frame
costa23.42s: i subprocess locali non sono il collo di bottiglia principale.
Le due fasi aggiunte raddoppiano le varianti risultato18→36 senza recuperare altre
LOSS in validation. Sono conservate come evidenza sperimentale nel pack esplicito;
non vengono installate come pack di default o dichiarate ottimizzazione pronta.

File modificati: analysis/gameplay/assembler.py, evaluation.py,
hearthstone/analyzer.py, hearthstone/debug_analyze.py, hearthstone/visual/detector.py;
i4 relativi file di test e questo report. File nuovi: fixture calibration-only e
artifact/pack sotto docs/gameplay_validation/73/iteration2/. Nessuna nuova dipendenza.


## Iteration 3 — Single-frame result policy and external calibration preparation

8 ottobre 2026. **NEEDS_MORE_CALIBRATION_DATA** per abilitare l'eccezione
single-frame. Il workflow esterno è pronto ed è stato eseguito realmente su
**74.mp4** (1920×1080, durata7641.057s). Le11 partite di validation di73 sono
**FROZEN**: nessun loro frame è stato usato per patch/parametri e nessun retest
è stato eseguito in questa iterazione. I risultati delle sezioni precedenti
rimangono storici; non sono un BEFORE/AFTER nuovo.

### Consolidator e policy configurabile

Il problema strutturale è confermato: il percorso ordinario richiede almeno2
osservazioni risultato con PTS distinti e span>=1/120s; una sola correlazione
forte è scartata anche dopo il refinement. Questo comportamento resta il default.
VS mantiene>=3 osservazioni e>=0.25s; detector VS e GameAssembler non sono
stati ritoccati in questa iterazione.

È stato aggiunto SingleFrameResultPolicy con regole distinte VICTORY/DEFEAT,
**entrambe disabilitate per default**. Ogni regola configurabile contiene:

- peak_similarity: gate del picco;
- minimum_class_margin: separazione dalla classe risultato alternativa;
- neighbor_similarity: supporto visivo sotto soglia nei vicini;
- maximum_neighbor_gap_s: distanza massima dei vicini.

Non c'è una soglia numerica single-frame scelta per la produzione. Il file
[single_frame_policy.disabled.json](gameplay_validation/74/single_frame_policy.disabled.json)
contiene null per entrambe le classi. I valori nei test sono esplicitamente
sintetici e NON derivano dalla validation né vengono installati come default.
La soglia ordinaria resta0.92.

Se una regola verrà giustificata e abilitata, un evento deve avere una sola
osservazione template reale, superare il gate del picco, avere entrambi gli score
VICTORY/DEFEAT e il margin richiesto. Il refinement deve aver osservato frame
su ENTRAMBI i lati: il candidato deve essere un picco locale stretto e almeno
un vicino più debole deve favorire la stessa classe e superare il support gate.
Replay dello stesso timestamp, margine ambiguo, picco casuale isolato, punteggi
mancanti o finestra tronca non bastano. La policy si vincola al fingerprint
esatto del pack e a un calibration_id; non può allentare soglia/margin ordinari.
I motivi di rifiuto sono disponibili nei rejected_events.

La nuova observe_roi restituisce eventi e score diagnostici con UN solo matching.
Quando la policy è abilitata, questi score vengono conservati soltanto nelle
finestre native già previste. Quando è disabilitata, non si raccolgono contesti
aggiuntivi. Nessuna seconda scansione globale né modifica a VS/assembler.
Analyzer version v5, cache identity include la configurazione della policy.

### Calibration-only: 74.mp4 e distribuzioni misurate

Annotazioni e report separati in [74/README.md](gameplay_validation/74/README.md).
La ricognizione visiva ha individuato due DEFEAT reali, intorno a6092.6s e7606.0s.
Entrambe restano visibili per più frame: non sono esempi verificati di un risultato
presente in un solo frame. Sono stati misurati7 anchor windows (1VS, 3VICTORY,
3DEFEAT: ingresso e fase stabile della prima LOSS, più la seconda LOSS) e4
finestre negative di2s ciascuna, verificate visivamente. NON sono7 partite né
una ground truth completa di74.

I timestamp al centro delle finestre sono annotazioni provvisorie; i picchi nella
tabella NON sono stime del primo frame di apparizione. Gli intervalli visible
annotati manualmente identificano positivi; il resto della finestra è UNREVIEWED,
non negativo implicito. La selezione dei picchi del matcher è stata seguita da
verifica visiva del testo, senza usare il matcher come unica fonte delle etichette.

|Finestra di74|Classe manuale|Picco pack precedente|Score|Margin risultato|Template migliore|
|---|---|---:|---:|---:|---|
|169.5–171.5|VS_SCREEN|170.504667|0.993805|0.852506*|calibration_cursor_free|
|819.6–821.6|VICTORY|821.336000|0.998794|0.521770|victory_01|
|1384.5–1386.5|VICTORY|1386.172667|0.998932|0.521600|victory_01|
|2197.2–2199.2|VICTORY|2199.031333|0.999513|0.522440|victory_01|
|6092.1–6094.1|DEFEAT ingresso|6092.748667|0.978260|0.669137|defeat_01|
|6118–6120|DEFEAT stabile, stessa partita|6119.948667|0.618986|0.162072|calibration_phase_1324.933|
|7605.6–7607.6|DEFEAT, seconda partita|7606.156667|0.978318|0.663870|defeat_01|

*Per VS il margin è rispetto alla migliore delle altre classi esaminate sul frame
completo; per i risultati è DEFEAT–VICTORY o viceversa sulla ROI dedicata.

Sul pack precedente, i180 frame manualmente visibili DEFEAT hanno
min/mediana/max **0.587311/0.854407/0.978318**; margin min/mediana/max
**0.119514/0.360635/0.669137**. Il template migliore è defeat_01 in10 frame,
phase_1324.900 in9, phase_1324.933 in161.
I181 positivi VICTORY hanno min/mediana/max0.295547/0.992305/0.998611:
le fasi di ingresso molto piccole possono essere sotto soglia anche se il testo
è già visibile. Non si interpreta similarity come probabilità calibrata.

I164 frame delle quattro finestre negative hanno score massimo VICTORY0.256164,
DEFEAT0.339418. Contando anche le classi alternative nei frame positivi verificati,
il massimo falso DEFEAT è **0.507379** (351 osservazioni non-DEFEAT), il massimo
falso VICTORY0.525321. Nessun negativo di questo campione supera0.80 o0.92.
Si tratta di assenza di falsi candidati forti nel campione, NON della loro
rappresentativa distribuzione di coda su ore di VOD.

È stato riusato anche il cache delle SOLE prime3 partite di calibrazione di73:
783 frame gameplay negativi, DEFEAT max0.450763; 222 frame intorno alle due WIN,
falso DEFEAT max0.513113. Nessun falso DEFEAT>=0.80 con il pack candidato.
Questo audit include gli estremi delle finestre e conta783 frame contro782 nel
riepilogo storico; non sono cambiate le etichette dei giochi né è stata letta
la validation. Dati in
[previous_calibration_candidate_scores.json](gameplay_validation/74/previous_calibration_candidate_scores.json).

### Template manager: una sola fase aggiunta, pack candidato

Prima di aggiungere patch sono stati misurati score, margin e identità dei
migliori template. La fase stabile indipendente è chiaramente nella ROI, ma
il pack attuale non la copre bene. È stato quindi creato un pack candidato
esplicito, senza cambiare il pack congelato né il default dell'app.

Una sola nuova patch DEFEAT viene da74, frame **6119.015333**, rettangolo di
riferimento **[429,314,114,29]**; provenienza e annotazioni al momento della
cattura sono conservate. Non sono state aggiunte tre copie quasi identiche.
La curation rifiuta duplicati pixel-identici; segnala correlazione>=0.995 per
patch con aspect ratio simile e richiede un override esplicito dopo revisione.
Questa soglia di avviso NON è una soglia di detection; nessun file viene eliminato.
Patch e companion non possono sovrascrivere file preesistenti.

ROI, grayscale, scale, soglia0.92 e ambiguity margin0.04 restano invariati.
Nel candidato le patch sono7 anziché6, DEFEAT4 anziché3; varianti risultato
45 anziché36. Fingerprint candidato:
0eb1c441279a75fd2ea56e0dda96d547ebee65ee5119c51bde40723a5fa919c8.

|Calibrazione74|Pack precedente|Pack candidato|
|---|---:|---:|
|Picco DEFEAT fase stabile|0.618986|0.999974|
|Margin al picco stabile|0.162072|0.533019|
|DEFEAT positivi min/mediana/max|0.587311/0.854407/0.978318|0.765483/0.872104/0.999974|
|Max falso DEFEAT, incluse WIN|0.507379|0.507379|
|Max DEFEAT nei negativi gameplay/menu nuovi|0.339418|0.339418|

L'aumento sul frame sorgente è **training fit**, non una nuova misura di recall.
Il miglior template all'ingresso delle due LOSS resta defeat_01; quello nuovo
vince in63 frame positivi verificati. Questo è compatibile con un problema di
copertura delle fasi, non con l'inseparabilità delle classi. Non prova ancora
che il candidato generalizzi a una LOSS brevissima o a VOD diversi.

### Perché la policy rimane OFF e cosa serve

I dati attuali separano bene i picchi osservati dai negativi disponibili, ma non
forniscono ancora positivi reali single-frame della classe DEFEAT né falsi picchi
isolati difficili vicino al futuro gate. Le due nuove LOSS hanno più osservazioni
forti a frame rate nativo; tanti frame della stessa animazione non sono tanti
esempi indipendenti del caso raro. Non si sceglie una soglia dal fallimento noto
a2305s, né dal massimo del set di validation.

Servono altri anchor calibration-only (anche da86.mp4 o da una diversa porzione
indipendente), con verifica visiva di risultati da circa100–200ms e tracce native
prima/dopo; inoltre finestre negative difficili con overlay, testo simile, menu,
carte e transizioni. Annotare gli intervalli di visibilità e verificare i falsi
candidati massimi. Poi scegliere/fissare peak gate, margin e support gate sulle
sole distribuzioni di calibrazione, congelare pack/config/codice e fare UNA
validation di73 senza retuning. Il tooling necessario è pronto: istruzioni in
[GAMEPLAY_CALIBRATION.md](GAMEPLAY_CALIBRATION.md).

Non è stato eseguito il retest blind di73 perché mancano parametri giustificati
per abilitare la policy. Ultimi risultati disponibili restano quelli diIteration2:
9/11 complete,2partial, LOSS recall50%, LOSS precision100%,0 false LOSS,
11/11VS e0 pairing errati. Non sono presentati come nuove misure.

### Performance e test

Queste misure sono su finestre locali annotate, **NON sull'intero VOD**:

|Misura|Pack precedente|Candidato|
|---|---:|---:|
|Tempo locale|9.981s|11.210s|
|Matching OpenCV|7.869s|9.684s|
|Attesa sampler/FFmpeg|1.559s|1.521s|
|Frame|896|896|
|Stream locali|11|11|
|Frame debug salvati|219|0|

Sono720 frame ROI risultato a60FPS richiesti,164 ROI negativi a20FPS e12 frame
VS a6FPS; solo frame sorgente reali. Il confronto wall è indicativo, con budget
debug diversi, non un benchmark controllato. Il template in più aumenta il
matching: non viene dichiarato un miglioramento di velocità. Il nuovo workflow
non esegue una scansione densa globale. La policy OFF non aggiunge matching;
ON riutilizza il matching delle finestre native, verificato con un test del numero
di chiamate. La precedente scansione globale da26m38s resta l'ultima misura
completa: nessuna nuova promessa sul suo costo.

**37 nuovi test**: forte/mediocre/isolato/ambiguo single-frame, VICTORY simmetrica,
VS escluso, supporto prima/dopo, PTS duplicati, dati concorrenti mancanti, binding
pack e config invalidi; integrazione senza doppio matching; annotazioni calibration
separate, positivi/UNREVIEWED/negativi, limiti, cancel e sampler chiuso; CLI headless,
JSON, capture provenance, rifiuto validation e collisioni; controllo duplicati.
I precedenti test ordinari con2–3 osservazioni continuano a passare.

- Gameplay: **246 passed,0 failed,0 skipped** (baseline209).
- Full pytest: **695 passed,0 failed,1 skipped**,47.97s (baseline658).
- Skip preesistente: SDK opzionale googleapiclient.http assente.
- Ruff: PASS; mypy: PASS su51 file, inclusi i3 moduli nuovi.
- GUI smoke offscreen: PASS, exit0.
- Nessuna nuova dipendenza. Comandi e dettagli in
  [quality_gate.json](gameplay_validation/74/quality_gate.json).

Moduli nuovi: hearthstone/result_policy.py, hearthstone/calibration.py,
hearthstone/visual/template_tools.py; due nuovi file di test e guida di calibrazione.
Modifiche incrementali a analyzer.py, visual/detector.py e debug_analyze.py e ai
relativi test. Nessuna modifica nuova a GameAssembler, VS, GUI, cut engine,
exporter, TrackState o formato progetto in questa iterazione.

**Decisione: B — altro calibration data indipendente; template matching ancora
promettente.** Il workflow è READY_FOR_EXTERNAL_CALIBRATION, ma lo stato finale
per abilitare la policy e procedere al retest è **NEEDS_MORE_CALIBRATION_DATA**.
Nessuna evidenza di TEMPLATE_MATCHING_LIMIT_REACHED e nessun PyTorch introdotto.


## Iteration 4 — Calibration 74 + 86, frozen pack and blind validation

8 ottobre 2026. **Esito: NEEDS_MORE_CALIBRATION_DATA.** La calibrazione indipendente migliora la copertura delle fasi DEFEAT, ma il retest frozen non recupera nessuna delle due LOSS mancanti. Non è stato integrato alcun gameplay cut e non è stato introdotto PyTorch.

### Calibrazione, curation e freeze

Revisione visiva di86: **14 partite, 8 WIN e 6 LOSS**. Annotazioni manuali separate dagli score automatici; panoramica ogni10 s, verifica locale VS e lettura nativa dei banner risultato. Ground truth, evidenze, distribuzioni complete e confronto74↔86 sono in [86/README.md](gameplay_validation/86/README.md).

- 86:319 frame TRUE DEFEAT di6 LOSS;337 TRUE VICTORY nelle finestre risultato. Mining di30 neighborhoods NON-DEFEAT,330 frame revisionati (116 VICTORY e214 altre schermate).
- Pool74+86:499 TRUE DEFEAT,634 TRUE VICTORY,378 altri negativi;1012 NON-DEFEAT per lo score DEFEAT.765 UNREVIEWED esclusi. Le LOSS indipendenti sono8: i frame delle stesse animazioni sono correlati.
- Pack combinato: TRUE DEFEAT min/p10/mediana/p90/max =0.1769/0.8488/0.8961/0.9690/1.0000; NON-DEFEAT min/mediana/p90/p95/max =0.1390/0.4696/0.4910/0.4988/0.5409. Fasi deboli si sovrappongono ai negativi; i picchi dei risultati calibration rimangono separati.
- La patch74 stable generalizza alla fase stabile di86:0.620213→0.997861. La patch86 late aggiunge13 frame sopra0.92 sulle due LOSS indipendenti di74.86 early/mid scartate: nessun nuovo supporto≥0.92 sul VOD indipendente. Il guadagno sul proprio frame di cattura non è generalizzazione.
- Cross-VOD locale: base2/2 LOSS74 e5/6 LOSS86; +74 stable stesso numero di giochi ma10/11 fasi supportate; +86 late senza74 stable2/2 e6/6,8/11 fasi; combinato2/2 e6/6,11/11 fasi. Queste sono misure su finestre native annotate, non nuovo recall globale di86.
- Grid scelta prima dello scoring:0.80,0.85,0.90,0.92,0.95,0.98. A0.92 tutti gli8 eventi LOSS hanno≥2 osservazioni native forti; a0.95 solo6/8.0.90 non aggiunge eventi. Zero falsi candidati LOSS-equivalent nel campione negativo revisionato, non una garanzia universale.

Configurazione congelata **prima** del retest:8 patch (2VS,1VICTORY,5DEFEAT), soglia0.92, margin0.04, grayscale/TM_CCOEFF_NORMED, ROI risultati[0.35,0.47,0.30,0.22],9 scale0.90…1.10. VS1FPS+6FPS, risultato20FPS crop288×120; refinement120FPS richiesti, solo PTS sorgente reali. **Single-frame OFF** per entrambe le classi:8 LOSS con layout simili e mining negativo sparse non giustificano una nuova soglia di eccezione. VS e GameAssembler invariati.

Manifest e fingerprint: [freeze_manifest.json](gameplay_validation/86/iteration4_frozen/freeze_manifest.json), pack4a481ff015e8e0bbe500fc098b1abee0d975f9af733c90b4adfcf6beb08e99fc. Il candidate pack conserva anche gli scarti; quello usato è esclusivamente iteration4_frozen. Non è stato promosso a default installato.

### Unico retest blind — nessun retuning successivo

UNA esecuzione senza cache, iniziata dopo il freeze, ground truth originale delle11 partite held-out di73. [Invocazione](gameplay_validation/86/iteration4_frozen/blind_invocation.json), [analisi](gameplay_validation/86/blind73/gameplay_analysis.json), [evaluation](gameplay_validation/86/blind73/gameplay_evaluation.json), [confronto macchina](gameplay_validation/86/blind73/comparison_summary.json).

|Metrica|BEFORE: Iteration 2|AFTER: blind Iteration 4|
|---|---:|---:|
|Complete corrette|9/11|9/11|
|Partial abbinate|2|2|
|Complete game precision|100.00%|100.00%|
|Complete game recall|81.82%|81.82%|
|VS recall|11/11 (100%)|11/11 (100%)|
|VICTORY recall|7/7 (100%)|7/7 (100%)|
|DEFEAT / LOSS recall|2/4 (50%)|2/4 (50%)|
|Result event recall|9/11 (81.82%)|9/11 (81.82%)|
|LOSS precision, partite|100%|100%|
|False LOSS, partite|0|0|
|Classificazione sulle complete|9/9 (100%)|9/9 (100%)|
|Pairing errati|0|0|
|Anchor risultato aggiuntivi|1 VICTORY|1 VICTORY + 1 DEFEAT|
|DEFEAT event precision|100%|66.67%|
|Start MAE / mediana / max|0.224 / 0.100 / 0.767 s|0.224 / 0.100 / 0.767 s|
|Result MAE / mediana / max|0.087 / 0.050 / 0.384 s|0.085 / 0.050 / 0.367 s|
|Tempo totale|26m38s|38m58s|

Le metriche legacy di game recall possono includere le partial: qui si usa il recall **completo**, con entrambi i confini osservati. Le due partial hanno VS corretto ma result UNKNOWN; nessuna stima del boundary mancante. Classificazione9/9 si riferisce alle complete; includendo UNKNOWN fra gli errori è9/11. Le predizioni complete sono9 e le partial2, senza partite complete spurie.

### Confronto per partita

Tempi HH:MM:SS.mmm; ground truth provvisoria invariata e tolleranza5 s. Gli errori sono assoluti in secondi.

|Validation game (VOD game)|GT VS|GT risultato|GT classe|Detected VS|Detected risultato|Classe|Errore VS / risultato|Stato|
|---|---|---|---|---|---|---|---|---|
|1 (4)|00:23:43.400|00:31:27.800|WIN|00:23:43.400|00:31:27.850|WIN|0.000 / 0.050|OK|
|2 (5)|00:33:13.600|00:38:25.000|LOSS|00:33:13.583|—|UNKNOWN|0.017 / —|PARTIAL|
|3 (6)|00:38:59.200|00:48:06.400|WIN|00:38:59.050|00:48:06.450|WIN|0.150 / 0.050|OK|
|4 (7)|00:48:38.600|00:55:27.000|WIN|00:48:38.583|00:55:26.983|WIN|0.017 / 0.017|OK|
|5 (8)|00:56:14.000|01:02:32.100|LOSS|00:56:13.933|01:02:32.133|LOSS|0.067 / 0.033|OK|
|6 (9)|01:03:16.000|01:07:41.400|WIN|01:03:16.100|01:07:41.333|WIN|0.100 / 0.067|OK|
|7 (10)|01:16:04.000|01:23:30.700|LOSS|01:16:03.933|—|UNKNOWN|0.067 / —|PARTIAL|
|8 (11)|01:24:19.000|01:34:38.000|WIN|01:24:19.400|01:34:38.000|WIN|0.400 / 0.000|OK|
|9 (12)|01:35:30.000|01:41:02.000|LOSS|01:35:30.283|01:41:01.633|LOSS|0.283 / 0.367|OK|
|10 (13)|01:41:35.000|01:45:54.000|WIN|01:41:34.233|01:45:54.016|WIN|0.767 / 0.016|OK|
|11 (14)|01:46:20.000|01:52:18.000|WIN|01:46:19.400|01:52:17.833|WIN|0.600 / 0.167|OK|

### Failure rimasti e anchor extra

1. **2305 s:** native refinement visitato, DEFEAT@2305.050,score0.941881,margin0.549199,template defeat_01,una sola osservazione forte,span0. Rejected con single_frame_rejection=disabled. È ancora un limite del supporto temporale ordinario; non un frame mai campionato o una LOSS classificata WIN. Non si abilita la policy usando questa evidenza held-out.
2. **5010.7 s:** resta START_DETECTED_RESULT_MISSING. Il run visita la finestra nativa5010.583…5010.933 ma non produce un risultato accettato/stabile.0.9099 è il massimo **storico** dell’Iteration2, non una nuova misura del pack corrente: il blind non conserva tutti gli score subthreshold e non è stata eseguita una nuova passata di matching diagnostico. La calibrazione non dimostra copertura di questa variante held-out.
3. **Anchor DEFEAT extra@6063.400:**score0.957060,template external74_defeat_stable,peak6065.383,101 osservazioni,span2.85 s. Verifica visiva post-blind [anchor_6065_383.jpg](gameplay_validation/86/blind73/anchor_6065_383.jpg): il banner dice Sconfitta ed è una fase successiva della stessa LOSS già ancorata a6061.633 (GT6062). GameAssembler conserva tutto nella stessa partita: **0 false LOSS ricostruite**, ma il matching uno-a-uno degli anchor conta1 DEFEAT extra e precision eventi DEFEAT2/3. Non va nascosto dietro la precision delle partite.
4. Rimane l’anchor VICTORY extra@5686.483 già presente nella baseline. Anchor risultati extra totali1→2; nessun pairing a cascata.

L’immagine post-blind è stata estratta solo per verificare il duplicato, senza rieseguire il detector né fare tuning; è held-out, **vietato riutilizzarla come template/calibration**. Nessun codice, patch o parametro cambiato dopo il freeze. [Audit](gameplay_validation/86/blind73/run_integrity.json):31 hash invariati, fingerprint e settings effettivi corrispondenti. Il riepilogo del manifest contiene il vecchio calibration_id vuoto della policy OFF; il file policy esplicito, già incluso negli hash, usa iteration4-74-86. La differenza è descrittiva, entrambe le regole restano null.

### Performance

Tempo totale2337.738 s = **38m58s**, rispetto a1598.224 s =26m38s (+46.27%). Nessun miglioramento di velocità dichiarato. Le verifiche locali si sono parzialmente sovrapposte all’inizio del run: confronto wall indicativo, non benchmark isolato.

|Fase|Wall, s|Attesa sampler, s|Matching OpenCV, s|Stream locali/globali|
|---|---:|---:|---:|---:|
|VS coarse|181.185|150.288|30.131|1|
|VS fine|6.145|2.628|3.453|14|
|Risultati ROI globale|2116.645|4.832|2107.258|1|
|Refinement nativo|33.760|6.459|27.228|49|

Frame:6817 coarse,722 fine,136280 ROI densi,1863 refinement (prima1656).49 stream refinement, prima45. Matching globale2107.258 s = **90.14%** del wall totale; resta il bottleneck. Il pack passa da36 a54 varianti risultato. Le attese sampler includono startup/seek/lettura e backpressure, non misurano CPU decode separatamente; first-frame wait è già incluso nelle attese. Non sono state aggiunte passate globali20FPS full-frame. I ranking diagnostici opzionali riusano il matching e non sono attivi nel percorso globale.

### Test e decisione

- Gameplay: **249 passed,0 failed,0 skipped**, baseline246.
- Full pytest: **698 passed,0 failed,1 skipped**, baseline695; skip preesistente SDK googleapiclient.http opzionale assente.
- Ruff PASS; mypy PASS su51 file; GUI smoke offscreen PASS exit0; git diff --check PASS.
- Tre test nuovi per ranking di patch distinte/max sulle scale senza doppio matching, secondo template assente, percentili corretti sui frame revisionati. Nessuna dipendenza aggiunta. Comandi/log in [quality_gate.json](gameplay_validation/86/quality_gate.json).

**Decisione finale: NEEDS_MORE_CALIBRATION_DATA.** Il gate LOSS≥90–100% non è raggiunto. La nuova patch86 generalizza come supporto di fase su74;74 stable riconosce anche una fase held-out reale, ma nessuna delle due aumenta il recall delle LOSS frozen. Non basta a promuovere il pack né a integrare tagli.

Servono nuovi anchor calibration indipendenti con risultati brevissimi e vera evidenza single-frame, più mining dei negativi con il pack finale e layout/lingue variati. Scegliere eventuali nuove regole SOLO su quei dati e congelare un’altra configurazione prima di un nuovo ciclo dichiarato. Le distribuzioni dei picchi calibration non mostrano forte sovrapposizione con i negativi: **TEMPLATE_MATCHING_LIMIT_REACHED non dimostrato; PyTorch non giustificato da questa iterazione**. Nessun tuning di VS/assembler, cut, timeline, TrackState, exporter o project persistence.


## Iteration 5 — State-aware candidate, frozen blind validation

9 ottobre2026. **Esito: NEEDS_MORE_CALIBRATION_DATA.** Il candidato migliora il recall LOSS50%→75%, mantiene precision100%/zero false LOSS e riduce il tempo38m58s→3m27s. Il target LOSS≥90% NON è raggiunto. Nessun gameplay cut, nessun PyTorch, nessuna promozione del candidato a default.

### Configurazione, calibrazione e integrità

[Report completo](gameplay_validation/state_aware/README.md) con ROI, grayscale/CLAHE/gradient/OCR, robustness, confronto OLD/NEW74+86 e profiling. Template quantitativi testo/board solo da74;86 usato per verifica calibration indipendente, senza nuove patch. Screenshot templates/ dell'utente: geometria generale soltanto, provenienza non verificata. Le11 validation games di73 non hanno fornito template, threshold, ROI, scale o trigger.

- Monitor1FPS:VS esistente + due cornici board mascherate + testo debole. VS invariato:1FPS,refinement6FPS,soglia0.92.
- Board end-turn ROI[0.766667,0.418519,0.084375,0.079630];hero-power[0.565625,0.692593,0.081250,0.144444]. Canny/minimum, tre riferimenti74 per anchor, centri esclusi, tolleranza±8 reference pixels.
- Trigger board dopoVS:due osservazioni≥0.38, poi caduta≤0.20. Backup indipendente:text weak rising edge≥0.65 e margin≥0.04.
- Finestre−2/+5s, merging/clamping,FFmpeg crop prima output,60FPS locali. ROI testo[0.425,0.5,0.15,0.161111],144×87 su960×540.
- Grayscale TM_CCOEFF_NORMED≥0.85,margin≥0.04,scale0.90/0.95/1/1.05/1.10/1.15;quattro patch glyph74. Due PTS sorgente distinti e span≥1/120s. **Single-frame OFF**.
- Context più largo e OCR non abilitati. Dedup eventi della stessa schermata prima dell'assembler, conVS/board-return come barriere;GameAssembler invariato.

Calibrazione:8/8 LOSS indipendenti74+86 con≥2 PTS;1012 NON-DEFEAT revisionati con massimo scoreDEFEAT0.478860. A0.90 solo7/8 LOSS, a0.92 solo6/8;0.80 non aggiunge eventi rispetto0.85. Grayscale330/330 robustness,CLAHE325/330,gradient326/330,OCR239/330. A30FPS locali86 perde la LOSS breve#5 (un solo frame forte);a60FPS ha due frame e14/14 complete. Sul VOD74 gli anchor manuali sono sparse:5/5 risultati indipendenti recuperati,nessuna pretesa di recall totale. Tutti i dettagli/provenance sono nel report collegato.

Freeze2026-10-08T23:25:05Z,profilo fingerprint4aaeed45c036d569d5db00d6d2ae84775be5e21300b341b6819fcf0bc5031bdb. [Manifest61 hash](gameplay_validation/state_aware/iteration5_frozen/freeze_manifest.json). Confronto OLD/NEW calibration completato23:50:20.850913Z;unico blind iniziato23:50:20.920988Z e terminato23:53:48.816812Z,exit0. [Guardia start](gameplay_validation/state_aware/iteration5_frozen/blind_started.json),[fine](gameplay_validation/state_aware/iteration5_frozen/blind_finished.json). Tutti i61 hash uguali [prima](gameplay_validation/state_aware/iteration5_frozen/hash_audit_before_blind.json) e [dopo](gameplay_validation/state_aware/iteration5_frozen/hash_audit_after_blind.json). Nessun retuning successivo.

### BEFORE / AFTER sulle11 partite frozen

[Analisi nuova](gameplay_validation/state_aware/blind73/gameplay_analysis.json),[evaluation](gameplay_validation/state_aware/blind73/gameplay_evaluation.json),[comparison JSON](gameplay_validation/state_aware/blind73/comparison_summary.json). Tolerance5s,annotazioni held-out originali/provvisorie e range[1400,6819.999667] invariati. Il run analizza l'intero VOD, ma le metriche seguenti riguardano SOLO le11 partite held-out.

|Metrica|BEFORE Iteration4 global|AFTER state-aware|
|---|---:|---:|
|Complete corrette|9/11|10/11|
|Partial abbinate|2|1|
|Complete game precision|100%|100%|
|Complete game recall|81.82%|90.91%|
|VS event recall|11/11 (100%)|11/11 (100%)|
|VICTORY event recall|7/7 (100%)|7/7 (100%)|
|DEFEAT / LOSS result recall|2/4 (50%)|3/4 (75%)|
|Result event recall|9/11 (81.82%)|10/11 (90.91%)|
|Complete LOSS recall|50%|75%|
|LOSS precision, games|100%|100%|
|False LOSS games|0|0|
|Extra risultato / extra DEFEAT anchor|2 /1|0 /0|
|Classificazione su risultati associati|9/9 (100%)|10/10 (100%)|
|Classificazione tutte11,UNKNOWN errato|9/11|10/11|
|Pairing errati|0|0|
|Any reconstruction (include partial)|11/11|11/11|
|Start MAE / mediana / max,11VS|0.224 /0.100 /0.767 s|0.224 /0.100 /0.767 s|
|Result MAE / mediana / max|0.085 /0.050 /0.367 s,n9|0.0785 /0.0335 /0.334 s,n10|
|Tempo analisi interoVOD|2337.738 s (38m58s)|207.342 s (3m27s)|

La partial ha start osservato, end=None, result=UNKNOWN. Non è una partita completamente persa né una LOSS riconosciuta:è il risultato a mancare. Nessun confine viene inventato. Le medie temporali dei risultati hanno denominatore diverso(9→10);non sono un confronto sugli stessi soli eventi. Nessuna falsa detection LOSS nel set annotato, non una garanzia universale.

### Confronto per partita

|Validation / VOD|GT start|Detected start|GT / detected result|GT result time|Detected result time|Start error|Result error|Status|
|---|---|---|---|---|---|---|---|---|
|1 / VOD#4|00:23:43.400|00:23:43.400|WIN / WIN|00:31:27.800|00:31:27.833|0.000 s|0.033 s|OK|
|2 / VOD#5|00:33:13.600|00:33:13.583|LOSS / LOSS|00:38:25.000|00:38:24.983|0.017 s|0.017 s|OK|
|3 / VOD#6|00:38:59.200|00:38:59.050|WIN / WIN|00:48:06.400|00:48:06.450|0.150 s|0.050 s|OK|
|4 / VOD#7|00:48:38.600|00:48:38.583|WIN / WIN|00:55:27.000|00:55:26.966|0.017 s|0.034 s|OK|
|5 / VOD#8|00:56:14.000|00:56:13.933|LOSS / LOSS|01:02:32.100|01:02:32.133|0.067 s|0.033 s|OK|
|6 / VOD#9|01:03:16.000|01:03:16.100|WIN / WIN|01:07:41.400|01:07:41.316|0.100 s|0.084 s|OK|
|7 / VOD#10|01:16:04.000|01:16:03.933|LOSS / UNKNOWN|01:23:30.700|UNKNOWN|0.067 s|—|PARTIAL: result FN|
|8 / VOD#11|01:24:19.000|01:24:19.400|WIN / WIN|01:34:38.000|01:34:38.000|0.400 s|0.000 s|OK|
|9 / VOD#12|01:35:30.000|01:35:30.283|LOSS / LOSS|01:41:02.000|01:41:01.666|0.283 s|0.334 s|OK|
|10 / VOD#13|01:41:35.000|01:41:34.233|WIN / WIN|01:45:54.000|01:45:54.016|0.767 s|0.016 s|OK|
|11 / VOD#14|01:46:20.000|01:46:19.400|WIN / WIN|01:52:18.000|01:52:17.816|0.600 s|0.184 s|OK|

### Failure analysis, senza tuning sul test

**LOSS2305s recuperata.** Il nuovo evento è2304.983,peak0.919962,margin0.565756,tre osservazioni sorgente distinte in0.067s. La regola ordinaria passa;nessuna eccezione single-frame. ROI/gate nuovi sono stati scelti su74+86. Gli score di patch differenti non sono direttamente confrontabili con0.9419 del vecchio matcher.

**LOSS5010.7s ancora mancata.** Il trigger board apre5008→5015 e la ROI contiene interamente Sconfitta!. La diagnostica ripete SOLO questa finestra di7s con profilo/FPS/ROI identici:420 frame reali,zero osservazioni ammesse. Non è un secondo blind run né un retest di nuovi parametri. Picco a5010.716:DEFEAT0.827201,VICTORY0.368112,margin0.459089,bestd74_6092,scale1.0. Il margin passa,la soglia assoluta0.85 no. [Traccia](gameplay_validation/state_aware/blind73/failure_diagnostic.json),[frame completo](gameplay_validation/state_aware/blind73/failure_full_5010.716000.jpg),[ROI](gameplay_validation/state_aware/blind73/failure_defeat_0_5010.716000_0.827201.jpg).

|Layer|Esito|
|---|---|
|State transition / high-FPS trigger|PASS:11/11 result timestamp held-out dentro finestre|
|Frame sampling|PASS sul failure:frame utile5010.716 effettivamente presente|
|ROI|PASS:glifi completi e best match interno|
|Text/template recognition gate|FAIL sul caso5010.7:0.827201<0.85|
|Temporal consolidation|Non raggiunto su quel caso:zero osservazioni sopra gate,non rifiuto di un singleton|
|GameAssembler|PASS:partial esplicita e nessun cascading pairing|

La causa osservabile è il gate/matching testuale, compatibile con coverage di rendering/fase/compressione insufficiente. Non si identifica definitivamente quale trasformazione causi lo score senza nuovo calibration data. Non si prova una nuova ROI/preprocessing/scala/soglia su questi frame e non se ne crea un template. La separazione da VICTORY rimane ampia:nessuna evidenza attuale di inseparabilità delle classi che giustifichi PyTorch.

**Dedup LOSS6062s.** Due cluster della stessa schermata diventano un solo evento6061.666,81 osservazioni aggregate,span2.984s,peak0.987197. Zero anchor DEFEAT aggiuntivi nel test,contro uno della baseline. Il GameAssembler non nasconde rumore:eventi già consolidati.

### Performance e costi

18 finestre,159s di VOD ad alta frequenza;6817 frame monitor/coarse,722VSfine,9421ROIlocali,zero dense globali. Tutti i frame emessi16960,contro145682 della baseline(−88.36%). Frame risultato alta frequenza138143→9421(−93.18%);confronti template7504956→475848(−93.66%),inclusi board,text weak eVS. Tempo wall−91.13%,circa11.27× più breve;benchmark condiviso,non CPU isolata. Frame emessi NON significa frame interni decodificati daFFmpeg.

Profiling nuovo:monitor sampling wait89.463s,matching81.506s;VSfine2.570/2.823s;finestre3.452/27.141s. Il costo ora è soprattutto monitor+decode(82.5%);matching delle finestre13.1%. Startup compreso nel sampler wait,non isolato.33 streamFFmpeg totali(1monitor+14VS+18result).60JPEG diagnostici bounded;cache disabilitata.

Il confronto completo calibration OLD/NEW riporta74:46m32s→5m24s,86:50m10s→4m16s,conservando tutti i risultati noti e riducendo confronti di circa94%. Un benchmark OLD74 duplicato è stato avviato per errore;il primo completo è quello preservato per le misure. Carico concorrente dichiarato,nessun uso del repeat per selezionare parametri e nessun altro blind73.

### Test e decisione

66 nuovi test,315 gameplay passed;full764 passed,0 failed,1 skipped preesistente(googleapiclient opzionale). RuffPASS,mypyPASS59file,GUI smoke offscreenPASS,CLI headless helpPASS. Il primo tentativo full aveva un failure del cache export legacy in home non scrivibile;riesecuzione con AUTO_CUTTER_CHUNK_CACHE_DIR supportato passa,senza modifiche exporter. [Comandi/output](gameplay_validation/state_aware/calibration/quality_gate.json).

**NEEDS_MORE_CALIBRATION_DATA.** State-aware è molto più economico e recupera una LOSS in più,ma75% recallLOSS non raggiunge il gate≥90%. Mantenere il candidato opt-in e la baseline disponibile. Raccogliere altri DEFEAT/VICTORY brevi da VOD calibration indipendenti,con variazioni reali di animazione/rendering/compressione e hard negatives;valutare coverage/gate sul SOLO calibration data,poi nuovo freeze e validation indipendente. Non abbassare0.85 usando il caso5010.7. Nessun PyTorch,nessun gameplay cut.73 rimane congelato e questa versione non viene ritestata/ritoccata sul suo esito.

## Reference provenance clarification — 2026-10-09

Dopo il blind test, l’utente ha chiarito che gli screenshot in templates/ vengono da un altro VOD, non ancora scaricato. Sono riferimenti candidati per futura calibrazione esterna, con provenienza attestata dall’utente ma URL/ID, timestamp e fingerprint del video ancora mancanti. La nota storica sulla provenienza non verificata descrive le informazioni disponibili al freeze. Nessun asset, parametro o risultato della validazione congelata di 73.mp4 è stato modificato; nessun nuovo retest è stato eseguito. [Inventario e limiti](gameplay_validation/state_aware/reference_images_provenance.json).


## Iteration 6 — Result decision policy (2026-10-09)

Strato decisionale sostituito senza toccare trigger, ROI, template o assembler: cluster class-agnostic di frame con max(V,D) ≥ 0.65 (gap 0.5 s), conferma della schermata con ≥ 2 PTS distinti, poi WIN/LOSS = argmax del frame di picco; UNKNOWN se il picco è ambiguo (< 0.04) o se c'è supporto contrapposto. Tutti i parametri sono riusati, nessuno nuovo. Calibration 74+86: 20/20 tracce, 0 hard negatives sopra il floor. Blind 73 unico: **11/11 complete, LOSS 4/4, precision 100%, 0 false LOSS, 0 pairing errors, 3m28s.** Decisione: RESULT_POLICY_READY. Tabella BEFORE/AFTER, freeze e audit: [result_decision/README.md](gameplay_validation/state_aware/result_decision/README.md).
