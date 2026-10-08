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
