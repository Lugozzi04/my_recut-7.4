# Hearthstone — calibration 86.mp4, Iteration 4

8 ottobre 2026. Studio video-only, senza cut, modifiche GUI o PyTorch. `73.mp4` games 4–14 rimane held-out; nessun suo frame è stato usato per questo pack.

## Ground truth manuale

**14 partite: 8 WIN, 6 LOSS.** Annotazione da un solo revisore: panoramica globale ogni 10 s, scansione VS a 1 FPS come proposta, verifica locale VS a 6 FPS, transizioni risultato a 10 FPS e ROI nativa a 60 FPS. Le classi sono state lette da “Vittoria!”/“Sconfitta!”, non dedotte dagli score. La partita 11 è WIN, malgrado una prima impressione basata sulla transizione.

[ground_truth_manual.json](ground_truth_manual.json) è separato dai risultati automatici. Tempi approssimativi: inizio = primo VS chiaramente leggibile al sampling locale (incertezza ≤0.19 s); risultato = primo tratto leggibile selezionato, non la morte dell’eroe (incertezza circa0.034 s). `result_visible_*` descrive porzioni conservative verificate, non necessariamente tutta la durata del banner. Il conteggio è completo secondo questa revisione, non una certificazione da più annotatori.

|Game|VS, secondi|Risultato, secondi|Classe|
|---|---:|---:|---|
|86_game_01|539.733|1119.116|WIN|
|86_game_02|1149.900|1847.450|LOSS|
|86_game_03|1873.900|1982.733|LOSS|
|86_game_04|2004.900|2128.733|LOSS|
|86_game_05|2149.200|2300.600|LOSS|
|86_game_06|2319.733|2945.933|LOSS|
|86_game_07|2999.733|4116.633|WIN|
|86_game_08|4144.900|4727.433|WIN|
|86_game_09|4805.200|5285.516|LOSS|
|86_game_10|5314.200|6052.716|WIN|
|86_game_11|6079.900|6530.716|WIN|
|86_game_12|6560.383|6970.916|WIN|
|86_game_13|6999.033|7374.000|WIN|
|86_game_14|7472.733|7896.000|WIN|

Evidenze: [contact sheets](evidence/), native_01…14 per i risultati, vs_01…14 per gli start. Stable/exit della partita6 sono fasi dello stesso evento, non partite aggiuntive.

## Campioni e hard negatives

- 86:1392 ROI frame misurati;319 TRUE DEFEAT di6 LOSS,337 TRUE VICTORY nei14 result windows; ulteriori330 frame da30 hard-negative neighborhoods (116 VICTORY,214 NON-DEFEAT di altre schermate).406 frame restano UNREVIEWED.
- 74:884 ROI frame;180 TRUE DEFEAT di2 LOSS in3 finestre/fasi,181 TRUE VICTORY,164 negativi verificati,359 UNREVIEWED. Le annotazioni74 sono un sottoinsieme, non ground truth completa del VOD.
- Pool:2276 frame misurati,499 TRUE DEFEAT,634 TRUE VICTORY,378 negativi puri;1012 NON-DEFEAT per la distribuzione negativa DEFEAT.765 UNREVIEWED esclusi dalle statistiche.

Mining86:7981 ROI a1 FPS sull’intero VOD con il pack74, ordinati per score DEFEAT, distanziati≥10 s, esclusi gli intervalli LOSS annotati e verificate visivamente le30 proposte. I vicini±0.25 s sono estratti a20 FPS e revisionati; un’uscita VICTORY→rank badge nella proposta8 è etichettata per-frame. [Annotazioni](hard_negative_annotations.json), [etichette per-frame](study_frame_labels.json). Le VICTORY sono negative per DEFEAT, **non** negative per VICTORY.

La selezione dei negativi usa il pack74 prima della selezione dei candidati86: non è un mining esaustivo della coda del pack finale né una stima del false-positive rate su ogni frame di più ore. I frame della stessa animazione sono correlati:499 frame non equivalgono a499 LOSS indipendenti.

## Distribuzioni del pack congelato

|Campione|N|min|p10|mediana|p90|p95|max|
|---|---:|---:|---:|---:|---:|---:|---:|
|TRUE DEFEAT|499|0.1769|0.8488|0.8961|0.9690|0.9808|1.0000|
|TRUE VICTORY|634|0.1784|0.8617|0.9379|0.9975|0.9979|0.9996|
|NON-DEFEAT, score DEFEAT|1012|0.1390|0.2539|0.4696|0.4910|0.4988|0.5409|

I percentili sono lineari, calcolati sui soli frame revisionati. Similarity non è una probabilità. Le distribuzioni per-frame **si sovrappongono**: fasi piccole/di ingresso/uscita possono avere score bassi. Sul calibration data ogni LOSS offre però una fase con almeno2 osservazioni distinte sopra0.92; questo è diverso dal richiedere che ogni frame dell’animazione sia riconosciuto.

### Picchi DEFEAT e identità dei template

|Finestra|N positivi|Timestamp picco|Score|max alternativo: secondo template|Score secondo|Margin D−V|N≥0.92 con margin≥0.04|
|---|---:|---:|---:|---|---:|---:|---:|
|74_window_08|60|6092.748667|0.9783 (defeat_01.png)|calibration_phase_1324.900.png|0.8030|0.6691|14|
|74_window_09|60|6119.015333|1.0000 (external74_defeat_stable.png)|external86_defeat_late.png|0.6129|0.5330|30|
|74_window_10|60|7606.156667|0.9783 (defeat_01.png)|calibration_phase_1324.900.png|0.7901|0.6639|10|
|86_game_02|21|1847.500000|0.9566 (external86_defeat_late.png)|calibration_phase_1324.933.png|0.8552|0.4513|9|
|86_game_03|40|1982.933000|0.9754 (defeat_01.png)|calibration_phase_1324.900.png|0.8065|0.6612|13|
|86_game_04|23|2129.033000|0.9341 (external86_defeat_late.png)|calibration_phase_1324.933.png|0.8785|0.4557|6|
|86_game_05|10|2300.666000|1.0000 (external86_defeat_late.png)|calibration_phase_1324.933.png|0.9270|0.4868|2|
|86_game_06|40|2946.200000|0.9493 (calibration_phase_1324.933.png)|external86_defeat_late.png|0.8827|0.4532|11|
|86_game_06_exit|78|2976.400000|0.9777 (external74_defeat_stable.png)|external86_defeat_late.png|0.6134|0.5129|44|
|86_game_06_stable|60|2961.583000|0.9979 (external74_defeat_stable.png)|external86_defeat_late.png|0.6130|0.5303|33|
|86_game_09|47|5285.716000|0.9753 (defeat_01.png)|calibration_phase_1324.900.png|0.8179|0.6603|14|

Il secondo template è un’altra patch della stessa classe, massimizzata sulle scale; il margin D−V è invece rispetto alla classe alternativa. Non sono la stessa metrica. Identità/scores di **tutte** le patch sono disponibili nei JSON; il ranking diagnostico riusa i match già calcolati.

### Hard negatives più pericolosi, uno per neighborhood

|Gruppo|t|Contenuto|Best D|Score|Second D|Score secondo|Margin D−V|
|---|---:|---|---|---:|---|---:|---:|
|86_hard_01|506.733|NEGATIVE|calibration_phase_1324.933.png|0.5409|external86_defeat_late.png|0.5242|0.1165|
|86_hard_02|7898.533|VICTORY|calibration_phase_1324.933.png|0.5060|external86_defeat_late.png|0.4702|-0.4026|
|86_hard_07|6973.383|VICTORY|calibration_phase_1324.933.png|0.5050|external86_defeat_late.png|0.4688|-0.3449|
|86_hard_03|4767.583|VICTORY|calibration_phase_1324.933.png|0.5049|external86_defeat_late.png|0.4694|-0.4017|
|86_hard_05|7376.533|VICTORY|calibration_phase_1324.933.png|0.5049|external86_defeat_late.png|0.4697|-0.3843|
|86_hard_04|4747.633|VICTORY|calibration_phase_1324.933.png|0.5045|external86_defeat_late.png|0.4691|-0.3606|
|86_hard_06|4730.683|VICTORY|calibration_phase_1324.933.png|0.5025|external86_defeat_late.png|0.4706|-0.3881|
|86_hard_08|7390.300|VICTORY|calibration_phase_1324.933.png|0.4979|external86_defeat_late.png|0.4736|-0.3486|
|86_hard_10|519.300|NEGATIVE|external86_defeat_late.png|0.4926|calibration_phase_1324.933.png|0.4868|0.0924|
|86_hard_13|4757.300|VICTORY|calibration_phase_1324.933.png|0.4833|external86_defeat_late.png|0.4725|-0.4089|

Il massimo negativo revisionato è0.540902, schermata acquisto completato; la nuova patch late vi arriva a0.524169, quindi non aumenta il massimo del pack. La soglia0.92 rimane distante da questa coda osservata. Non estrapolare zero falsi positivi universale dal campione.

## Curation e cross-VOD

|Candidato|Decisione|Frame nuovi≥0.92 su74 rispetto al pack74|Frame nuovi su86|Max negativo della sola patch|Motivo|
|---|---|---:|---:|---:|---|
|external86_defeat_early.png|REJECT|0|1|0.6315|Nessun guadagno≥0.92 sul VOD indipendente; fit al proprio frame|
|external86_defeat_mid.png|REJECT|0|1|0.5089|Nessun guadagno≥0.92 sul VOD indipendente; fit al proprio frame|
|external86_defeat_late.png|ACCEPT|13|31|0.5242|Guadagno di copertura anche sul VOD indipendente|

Il candidate_pack contiene anche i due scarti per conservare l’esperimento; **non usarlo come pack finale**. Il pack congelato include solo late di86 e stable di74 oltre alle patch precedenti. Provenance completa nei companion: role, fingerprint sorgente, hash snapshot annotazioni, timestamp, ROI, SHA256 patch e duplicate review. Nessun duplicato pixel-identico o override near-duplicate usato. Un mancato avviso di duplicato non prova utilità: decide il guadagno cross-VOD.

- `74 stable →86 stable`: picco0.620213→0.997861 su una LOSS indipendente.
- `86 late →74`:13 frame prima sotto0.92 diventano sopra soglia, in entrambe le LOSS animate. I picchi animati erano già riconosciuti: è guadagno di fase/supporto, non due nuove partite.
- `86 late →86 breve`: due osservazioni forti anziché una nel game5; questo rimane training-VOD fit. Il test indipendente finale è73.

|Pack, sempre con base precedente|LOSS74, supporto locale|LOSS86, supporto locale|Finestre/fasi supportate|Max NON-DEFEAT|
|---|---:|---:|---:|---:|
|Base precedente|2/2|5/6|7/11|0.5409|
|+74 stable (74→86)|2/2|5/6|10/11|0.5409|
|+86 late, senza74 stable (86→74)|2/2|6/6|8/11|0.5409|
|Combinato accettato|2/2|6/6|11/11|0.5409|

Questi rapporti misurano osservazioni native nelle finestre manuali: **non sono recall di una nuova scansione end-to-end di86**. VICTORY: tutte le3 finestre74 e8 partite WIN86 hanno≥2 frame forti; la patch VICTORY non è cambiata e nessun positivo VICTORY viene classificato DEFEAT a0.92.

## Soglie e single-frame

Grid dichiarata prima dello scoring:0.80,0.85,0.90,0.92,0.95,0.98; margin0.04. Supporto ordinario:≥2 PTS distinti, span≥1/120 s, gap≤0.5 s.

|Soglia|Recall frame D|Precision frame D|LOSS supportate|Precision eventi locale|FP frame|False LOSS-equivalent clusters|Finestre/fasi|
|---|---:|---:|---:|---:|---:|---:|---:|
|0.80|94.79%|100%|8/8|100%|0|0|11/11|
|0.85|89.78%|100%|8/8|100%|0|0|11/11|
|0.90|47.70%|100%|8/8|100%|0|0|11/11|
|0.92|37.27%|100%|8/8|100%|0|0|11/11|
|0.95|16.83%|100%|6/8|100%|0|0|8/11|
|0.98|5.61%|100%|2/8|100%|0|0|2/11|

**Manteniamo0.92:** è la soglia più alta della grid che supporta tutte le8 LOSS calibration con il pack accettato.0.90 non aggiunge eventi;0.95 perde supporto. Non è stato scelto alcun numero usando73.

**Single-frame OFF** per entrambe le classi, nessuna nuova strong threshold. Il pack risolve il caso calibration con una sola osservazione ordinaria fornendo due osservazioni native, senza eccezione. Il campione ha8 LOSS indipendenti, layout/lingua simili e negativi minati sparse: non basta a certificare il rischio di picchi isolati né a scegliere le condizioni di una rara eccezione. Le protezioni già implementate restano disponibili, invariate.

## Configurazione congelata

[iteration4_frozen/freeze_manifest.json](iteration4_frozen/freeze_manifest.json), hash codice/annotazioni/patch/settings registrati prima del retest. Pack fingerprint:

`4a481ff015e8e0bbe500fc098b1abee0d975f9af733c90b4adfcf6beb08e99fc`

8 patch:2VS,1VICTORY,5DEFEAT. Risultati54 varianti (6patch×9scale), precedente36. Threshold0.92 per tutte le classi, margin0.04, grayscale/TM_CCOEFF_NORMED, ROI risultato[0.35,0.47,0.30,0.22],9scale0.90…1.10 step0.025. VS1FPS+6FPS; risultati20FPS crop288×120, refinement locale120FPS richiesti (solo PTS sorgente reali), gate candidato0.80. Single-frame OFF; VS e assembler invariati. Pack esplicito, non installato automaticamente come default dell’app.

Comando blind esatto, con placeholder per directory locali: [blind_invocation.json](iteration4_frozen/blind_invocation.json). UNA esecuzione senza cache; risultato inserito a fine analisi.

## Performance e test

Ricognizione86:241.26s, overview798 frame ogni10 s, circa7980 frame VS coarse; mining ROI1FPS239.69s,140.49s matching,7981frame. I due lavori erano concorrenti: non sommare i wall e non interpretarli come benchmark isolati. Campionamento locale una volta, cache NPZ riusata tra pack. Matching su2276frame:base21.56s, +74stable26.30s, tutti i candidati86(anche scarti)41.72s. Questa iterazione privilegia precisione; aggiungere patch aumenta il costo.

I percentili e ranking aggiunti ai diagnostici non aggiungono chiamate matchTemplate; ranking di patch distinte, aggregato sulle scale. Nel normale detector il ranking è opt-in, assente dal percorso globale della validation. Nessuna ottimizzazione globale o passata full-frame20FPS.

Test dopo le sole modifiche diagnostiche:249 gameplay passed;698 full passed,0 failed,1 skipped (googleapiclient opzionale assente). Ruff PASS; mypy51file PASS; GUI smoke offscreen exit0. Tre nuovi test:ranking senza doppio matching, seconda patch distinta/assente, percentili sui positivi revisionati. Nessuna nuova dipendenza. Dettagli in [quality_gate.json](quality_gate.json).

## Blind73 e decisione

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

**Decisione finale: NEEDS_MORE_CALIBRATION_DATA.** Il retest è stato eseguito UNA volta dopo il freeze, senza cache e senza retuning successivo. Le due LOSS frozen restano mancanti:2305 s ha una sola osservazione0.941881 respinta perché single-frame OFF; a5010.7 s non esiste un risultato stabile accettato. Il massimo0.9099 appartiene alla misura storica e non è dichiarato come nuovo massimo del pack.

Il nuovo pack generalizza alcune fasi (74 stable riconosce la schermata vera Sconfitta@6065.383), ma questa appartiene a una LOSS già riconosciuta. Il suo anchor extra@6063.400 viene contato come falso positivo temporale nel confronto uno-a-uno: DEFEAT event precision2/3, pur restando LOSS game precision100% e0false LOSS ricostruite. L’assembler lo mantiene nella stessa partita; la VICTORY extra@5686.483 era già presente. Non sono state rimosse evidenze scomode né ritoccati parametri.

Performance nuova:2337.738 s (38m58s),+46.27%;2107.258 s (90.14%) nel matching globale.6817 coarse,722 fine,136280 ROI densi,1863 refinement;49 finestre native. Misura locale indicativa, non benchmark isolato. Il pack non è promosso al default: il costo cresce senza recall held-out aggiuntivo.

File: [analisi](blind73/gameplay_analysis.json), [evaluation](blind73/gameplay_evaluation.json), [BEFORE/AFTER e profiling](blind73/comparison_summary.json), [integrità del run](blind73/run_integrity.json), [log](blind73/validation_run.log). Tutti i31 hash del freeze sono invariati e i settings effettivi corrispondono al file policy OFF esplicitamente invocato; l’audit spiega il solo calibration_id descrittivo nel riepilogo del manifest. [Confronto per le11 partite e failure analysis](../../GAMEPLAY_VALIDATION_73.md).

Servono altri calibration samples indipendenti brevissimi, evidenza single-frame e negativi minati con il pack finale. Il matcher separa ancora bene i picchi calibration dai negativi raccolti: non c’è evidenza di TEMPLATE_MATCHING_LIMIT_REACHED e non si introduce PyTorch. Nessun gameplay cut integrato.

