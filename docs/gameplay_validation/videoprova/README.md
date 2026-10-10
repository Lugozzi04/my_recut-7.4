# Validation indipendente — videoprova.mp4

9 ottobre 2026. VOD nuovo, mai usato per calibrazione, template, soglie o sviluppo. Un solo run automatico, eseguito dopo la ground truth manuale e il freeze. Nessuna modifica al detector, prima, durante o dopo. Nessun gameplay cut.

**Decisione: READY_FOR_GAMEPLAY_CUTS**, con il caveat sulla dimensione del campione descritto sotto: 2 LOSS e 2 WIN con risultato osservabile.

## Metadata VOD

|Campo|Valore|
|---|---|
|Path|`C:\Users\jakub\Downloads\videoprova.mp4` (file originale non modificato)|
|SHA-256|`73b902ecf9b2b3a418cff5f817f6dfc8efcee771159b55746b7d5dd4197e4b4e`|
|Contenitore|MP4 (isom), 2,896,751,780 byte, 6.18 Mb/s|
|Durata|3750.021 s (1h02m30s)|
|Video|H.264 High, 1920×1080, yuv420p, 60 FPS costanti, 225,000 frame|
|Audio|AAC LC, 48 kHz, stereo, start 0.005 s|
|Altro|un data stream `bin_data` (1 pacchetto, metadati Twitch); titolo "THEORYCRAFTING! Nuova espansione! DEV vs STREAMER", VOD id 2893413017|
|Integrità|decodifica completa video+audio con `ffmpeg -v error`: 0 errori, 1m34s|

Contenuto: 0–631 s deckbuilding/collezione e menu, poi 5 partite Classificate Standard (Bronzo 10→9). Il VOD termina a metà della partita 5. Condizioni nuove rispetto a 73/74/86: overlay "Streamers vs Devs" in alto a destra, webcam in basso a sinistra, schermate a tutto schermo "Scegli un rimpiazzo" durante le partite, una finestra di chat sul board (~2260–2276 s), una resa via menu seguita da un'animazione drago di 7 s, e banner di sconfitta brevissimi (0.3–0.5 s).

## Metodologia della ground truth (prima di qualsiasi run del detector)

1. Contact sheet dell'intero VOD, un frame ogni 5 s (749 frame, 21 fogli).
2. Seek esatti a 2 FPS attorno a ogni VS (±5 s) e a ogni fine partita; 3–10 FPS attorno a ogni banner risultato.
3. Classe letta dal testo italiano sotto il ritratto ("Vittoria!" / "Sconfitta!"), confermata dalla schermata rank successiva ("Bonus stelle" dopo le vittorie, "Non puoi perdere stelle" dopo le sconfitte).
4. Start = primo frame VS visibile (campionamento 0.5 s); result = primo frame con banner leggibile.

Output del detector mai consultato. File: [ground_truth_manual.json](ground_truth_manual.json). Frame di evidenza in [evidence/](evidence/): sono documentazione di validation, **non** calibration data né candidati template.

|#|Start GT|Result GT|GT|Banner visibile|Note|
|---|---:|---:|---|---|---|
|1|631.0|1301.1|LOSS|1301.1–1301.6 (~0.5 s)||
|2|1359.0|1786.0|WIN|1786.0–1790.0||
|3|1820.0|2792.0|LOSS|2792.0–2792.3 (~0.33 s)|resa da menu, animazione drago 2784.5–2791.7|
|4|2864.0|3525.5|WIN|3525.5–3530.0||
|5|3628.5|—|—|—|in corso alla fine del VOD (3750 s)|

Totale: **5 partite. 4 con risultato osservabile: 2 WIN, 2 LOSS.** La partita 5 non ha risultato nel video. L'evaluator richiede un risultato noto per ogni partita GT, quindi è stato eseguito su `annotated_range` [0, 3628.0] (partite 1–4); la partita 5 è valutata a mano: il comportamento atteso è una partita parziale con solo lo start e senza evento risultato.

## Freeze

[freeze/freeze_manifest.json](freeze/freeze_manifest.json): 49 hash, cioè codice `analysis/gameplay`, profilo e asset iteration 5, pack VS iteration 4, tool e studio della policy, test della policy, ground truth held-out 73 e questa ground truth. I 48 file in comune con il freeze iteration 6 ([manifest](../state_aware/result_decision/freeze/freeze_manifest.json)) sono **identici byte per byte**: nessuna modifica al detector dopo il blind 73. Una guardia `blind_started.json` rifiuta un secondo avvio. Audit dopo il run: [49/49 invariati](freeze/hash_audit_after_blind.json). Sequenza UTC: GT 13:31:22 → freeze 13:31:37 → run 13:31:45–13:33:43 → audit → evaluation.

Configurazione congelata: monitor 1 FPS; board high/low 0.38/0.20; weak trigger 0.65/0.04; finestra −2/+5 s a 60 FPS; ROI risultato [0.425, 0.5, 0.15, 0.161] grayscale; template v74_820, v74_1385, d74_6092, d74_6119 (scale 0.90–1.15); VS pack iteration4 (0.92, 6 FPS, ≥3 osservazioni); `result-decision-v1` (floor 0.65, gap 0.5 s, ≥2 PTS, margin 0.04, argmax sul peak); single-frame policy disabilitata; dedup 45 s; assembler con durata massima 1800 s. `hearthstone-state-aware-v2`, `--no-cache`.

Comando: `python -m analysis.gameplay.hearthstone.debug_analyze <Downloads>/videoprova.mp4 --strategy state-aware --state-profile docs/gameplay_validation/state_aware/iteration5_frozen/profile.json --template-pack docs/gameplay_validation/86/iteration4_frozen/templates.json --ground-truth docs/gameplay_validation/videoprova/ground_truth_manual.json --output-dir docs/gameplay_validation/videoprova/run --no-cache`. Exit 0, nessun bug infrastrutturale. Output: [analysis](run/gameplay_analysis.json), [evaluation](run/gameplay_evaluation.json), [log](run.log).

## Partita per partita

|#|Start GT|Result GT|GT|Start rilevato|Result rilevato|Predizione|Completa?|Corretta?|
|---|---:|---:|---|---:|---:|---|---|---|
|1|631.0|1301.1|LOSS|630.583|1301.083|LOSS|sì|sì|
|2|1359.0|1786.0|WIN|1359.450|1785.983|WIN|sì|sì|
|3|1820.0|2792.0|LOSS|1819.750|2791.983|LOSS|sì|sì|
|4|2864.0|3525.5|WIN|2863.750|3525.500|WIN|sì|sì|
|5|3628.5|—|—|3628.933|—|UNKNOWN|parziale, attesa|sì (nessun risultato inventato)|

Failure per categoria: missed VS 0, missed result 0, wrong WIN/LOSS 0, partial non attese 0, pairing error 0, extra game 0, false LOSS 0, false WIN 0, duplicate result 0, UNKNOWN 1 (partita 5, corretta perché il VOD è troncato).

## Metriche

|Metrica|Valore|
|---|---|
|Partite GT|5 (4 con risultato: 2 WIN, 2 LOSS)|
|Complete / parziali|4 / 1 (la parziale è la partita 5 troncata, attesa)|
|Game precision / complete recall|100% / 100% (4/4)|
|VS recall|100% (5/5, partita 5 compresa)|
|Result recall|100% (4/4)|
|VICTORY recall / DEFEAT recall|100% (2/2) / 100% (2/2)|
|WIN precision / LOSS precision|100% / 100%|
|False WIN / false LOSS|0 / 0|
|Classificazione sulle partite con risultato osservato|4/4|
|Pairing errors / extra games / duplicate result events|0 / 0 / 0|
|Extra result events / falsi eventi|0 / 0|
|UNKNOWN|1 (partita 5, nessun risultato nel video)|
|Errore start: media / mediana / max|0.342 / 0.334 / 0.450 s (evaluator, 4 partite); 0.360 / 0.417 / 0.450 s con la partita 5|
|Errore result: media / mediana / max|0.013 / 0.017 / 0.017 s|

L'errore sullo start rientra nella risoluzione della GT (0.5 s). Il VS della partita 1 compare tra 630.5 (nero) e 631.0, coerente con il rilevamento a 630.583.

## Evidenza della result policy (nessun tuning)

|Evento|Timestamp rappresentativo (peak)|VICTORY|DEFEAT|Classe|Margin|Frame nel cluster|Span cluster|Template vincente|
|---|---:|---:|---:|---|---:|---:|---:|---|
|G1 LOSS|1301.200|0.3389|0.9565|DEFEAT|0.618|10|0.183 s|d74_6092 @1.00|
|G2 WIN|1786.466|0.9928|0.3867|VICTORY|0.606|272|4.517 s|v74_820 @1.00|
|G3 LOSS|2792.166|0.5275|0.9470|DEFEAT|0.420|10|0.183 s|d74_6092 @1.15|
|G4 WIN|3525.966|0.9927|0.3903|VICTORY|0.602|295|4.900 s|v74_820 @1.00|

- Tutte le decisioni sono `event_peak_argmax` con supporto opposto 0.
- Un solo cluster rifiutato: 2792.683, 1 PTS, DEFEAT 0.711, `insufficient_support`. È l'ultimo frame del fade del banner G3, già coperto dall'evento classificato: nessuna conseguenza.
- **Nessun risultato mancato**, quindi nessuna analisi di rifiuto da fare.
- Osservazione, non tuning: il picco G3 ha VICTORY 0.528 e margin 0.420. Sono entrambi fuori dall'intervallo visto in calibration (VICTORY sui picchi DEFEAT ≤ 0.448, margin minimo 0.476). La decisione resta comunque lontanissima dal gate di 0.04.

**Hard negatives in questo VOD:** 7 finestre su 11 non contenevano un risultato. Erano aperte da board coperto da overlay "Scegli un rimpiazzo", chat e animazioni (742, 752, 825, 2583, 2611, 3405, 3539 s). Hanno prodotto **0 frame** sopra il floor: tutti i 588 frame ≥ 0.65 appartengono ai 4 banner reali. A 1 FPS il massimo max(V,D) è 0.326 in partita (2846 frame) e 0.343 durante il deckbuilding (630 frame).

**Trigger.** Ogni risultato è coperto da una finestra:

|Partita|Trigger|Finestra|
|---|---|---|
|G1|board 1302|1300–1307|
|G2|board 1783 + weak 1786|1781–1791|
|G3|weak 2792 + board 2793|2790–2798|
|G4|weak 3526|3524–3531|

Nelle due LOSS brevi il board scende sotto 0.20 **dopo** il banner (1302 contro 1301.1; 2793 contro 2792.0). Sono state coperte dalla pre-finestra di −2 s, con 0.9 e 1.0 s di margine. G3 è coperta anche dal weak trigger, perché un frame a 1 FPS è caduto sul banner da 0.33 s. È il principale rischio residuo (vedi sotto).

## Performance

|Misura|videoprova|73 (iteration 6)|
|---|---:|---:|
|Durata video|1.042 h|1.894 h|
|Runtime analisi|117.3 s (1m57s; wall 117.8 s)|208.3 s|
|Analisi per ora di video|**112.6 s/h**|109.9 s/h|
|Frame monitor (1 FPS)|3750|6817|
|Frame VS refinement (6 FPS)|266|722|
|Finestre risultato / secondi|11 / 81 s|18 / 159 s|
|Frame ROI 60 FPS|4860|9421|
|Confronti template (inclusi VS)|253,236 (board 22,500; text monitor 90,000; text finestre 116,640; VS 24,096)|475,848|

Tempo per stage, wall su macchina condivisa:

|Stage|Sampling/decode wait|Matching|Stream FFmpeg|
|---|---:|---:|---:|
|Monitor (board + weak + VS coarse)|50.7 s|47.9 s|1|
|VS refinement|0.8 s|1.2 s|5|
|Finestre risultato|1.7 s|14.8 s|11|

Triggering, decisione e assembler lavorano su dati già in memoria e non sono profilati separatamente (trascurabili rispetto al decode). Il costo per ora è in linea con 73 ed è circa 20× più veloce della vecchia scansione globale (38m58s su 73).

## Test software

Codice production non modificato in questa iterazione, quindi nessun nuovo test.

|Controllo|Esito|
|---|---|
|Gameplay|344 passed|
|Full pytest|793 passed, 1 skipped (googleapiclient opzionale, preesistente)|
|Ruff|PASS|
|Mypy|PASS (58 file, stesso set del quality gate)|
|GUI smoke offscreen|exit 0|

## Rischi residui (documentati, non corretti)

1. **Banner brevi prima del trigger board.** Nelle LOSS di questo VOD il banner dura 0.3–0.5 s e precede di ~1 s la caduta del board. La copertura dipende dalla pre-finestra di 2 s, con soli 0.9–1.0 s di margine. Una LOSS con banner ancora più anticipato rispetto al board, e senza weak trigger fortunato a 1 FPS, non verrebbe vista: sarebbe una partita parziale, mai una falsa LOSS. Da monitorare su nuovi VOD, non da ritoccare su questo.
2. **Campione piccolo:** 2 LOSS e 2 WIN osservabili. Insieme al blind 73 sono 15 partite di validation, 6 LOSS e 0 false LOSS, ma 73 non è più vergine per lo strato decisionale.
3. **Partite troncate** (inizio o fine VOD) restano UNKNOWN. La futura cut policy non deve tagliare segmenti UNKNOWN o parziali.

## Decisione

**READY_FOR_GAMEPLAY_CUTS.** Su un VOD completamente nuovo, con overlay e condizioni mai viste, la configurazione congelata ottiene VS 100%, VICTORY 100%, LOSS recall 100%, LOSS precision 100%, 0 false LOSS, 0 pairing errors, nessun duplicato e nessun falso evento nelle 7 finestre di disturbo. Non emergono failure strutturali. Le due LOSS sono proprio il caso difficile (banner da 0.3–0.5 s, una dopo una resa) e passano con 10 PTS di supporto e margin ≥ 0.42.

Caveat: il VOD da solo contiene poche LOSS. Se si vuole una prova statisticamente forte su questo solo VOD, il criterio "NEEDS_MORE_VALIDATION" sarebbe difendibile. La raccomandazione è procedere con una cut policy conservativa: tagliare solo LOSS confermate in partite complete, mai UNKNOWN o parziali, tagli rivedibili nell'editor. Continuare poi a raccogliere VOD come validation passiva.
