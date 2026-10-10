# Iteration 5 — State-aware candidate (74 + 86 calibration)

**Stato finale: NEEDS_MORE_CALIBRATION_DATA.** Blind 73: 10/11 complete, LOSS recall 75%, precision 100%, false LOSS 0, 3m27s. Nessun retuning dopo il test.

9 ottobre 2026. Strategia sperimentale opt-in, video-only. La scansione globale resta il default; nessun gameplay cut, modifica all'editor/exporter, o PyTorch. Questa pagina conserva configurazione, misure e limiti; il test blind è riportato separatamente dopo il confronto calibration.

## Architettura e componenti riusati

Un solo passaggio globale a 1 FPS alimenta il VS detector esistente, due anchor del board e il matcher debole del testo. Il VS mantiene pack, soglia 0.92, refinement 6 FPS, almeno 3 osservazioni e span 0.25 s. Il monitor pianifica finestre; non emette risultati e non inventa confini. Solo osservazioni VICTORY/DEFEAT confermate nel refinement diventano GameEvent. Deduplicazione nel layer eventi, poi GameAssembler esistente invariato.

Riusati: FFmpegFrameSampler/sample_roi e PTS sorgente reali, AnalysisCancellation, GameplayCache atomica, modelli GameEvent/GameSegment, consolidate_events, evaluation e debug CLI. I moduli nuovi non dipendono da widget Qt. La selezione è esplicita con --strategy state-aware e --state-profile; il frontend globale precedente rimane disponibile.

## ROI e asset

Coordinate [x,y,width,height] normalizzate sul viewport, riferimento 960×540:

|Segnale|ROI normalizzata|Pixel di riferimento|Rappresentazione|
|---|---|---|---|
|Board: bordo end-turn|[0.766667,0.418519,0.084375,0.079630]|[736,226,81,43]|Canny mascherato|
|Board: cornice hero-power|[0.565625,0.692593,0.081250,0.144444]|[543,374,78,78]|Canny mascherato|
|Ricerca risultato/testo|[0.425,0.5,0.15,0.161111]|[408,270,144,87]|Grayscale|
|Context banner, solo esperimento|[0.425,0.551852,0.15,0.101852]|[408,298,144,55]|Grayscale/CLAHE/gradient|

Il centro variabile dei due anchor board è escluso. Ogni anchor confronta tre riferimenti di74, con tolleranza ±8 pixel; il minimo dei due score è BOARD_PRESENT. Nessun matching sull'intero board. Il testo usa quattro patch strette di74, due VICTORY e due DEFEAT, più sei scale [0.90,0.95,1,1.05,1.10,1.15]. La search ROI contiene tolleranza al movimento del banner, mentre le patch contengono soprattutto glifi, non hero/rank/reward/background.

Provenienza completa in [profile.json](iteration5_frozen/profile.json): timestamp nativi, fingerprint sorgente, rettangoli e asset. I riferimenti board vengono da74@300.000,1605.014667,5400.012; le patch testo da74@820.602667,1385.056,6092.748667,6119.015333. Nessun nuovo template da86 né dalle11 validation games di73. Le immagini nella cartella templates/ dell'utente hanno provenienza non verificata: usate per comprendere la geometria, non per scegliere quantitativamente i parametri o creare asset.

## Trigger e consolidamento

- Monitor 1 FPS; due osservazioni BOARD_PRESENT≥0.38 dopo VS armano la transizione. Una caduta≤0.20 apre una finestra. Il board deve tornare prima di riarmare questo trigger.
- Trigger indipendente sul rising edge di un risultato debole: score≥0.65 e margin≥0.04. Può aprire una finestra anche con VS/board mancanti, ma non dichiara WIN/LOSS.
- Finestre trigger−2 s → trigger+5 s, clamped e unite quando sovrapposte; FFmpeg crop/scale prima dell'output, 60 FPS locali. Nessuna passata densa globale.
- Risultato: TM_CCOEFF_NORMED grayscale≥0.85, margin fra classi≥0.04, almeno due PTS sorgente distinti, span≥1/120 s. Parità esatta → UNKNOWN. Single-frame exception OFF.
- Cluster ravvicinati della stessa classe/sorgente vengono consolidati PRIMA del GameAssembler; VS e board reacquisition sono barriere. Gap massimo45 s, esiti opposti preservati per il controllo conflitti. Non si inventano start né si nascondono contraddizioni.

Il grid calibration di finestre +3/+5 s mostra che +3 perde tre risultati di86, +5 li copre tutti. 2 FPS monitor non aggiunge recall rispetto a1 FPS e aumenta le finestre. La configurazione finale produce20 finestre/164 s su86,18/158.006 s su74.

## Disciplina dataset e score

Sono stati riusati gli stessi2276 frame nativi cached di74+86:1511 revisionati e765 UNREVIEWED esclusi. Pool revisionato:499 DEFEAT,635 VICTORY,377 altre schermate;1012 NON-DEFEAT. I frame sono correlati, non1511 esempi indipendenti. Le LOSS indipendenti sono8. Un errore storico di label è stato corretto solo nella copia di questa iterazione:86_hard_08@7390.583 contiene Vittoria durante l'uscita, non NEGATIVE. L'originale rimane intatto; [override](calibration/label_overrides.json). La distribuzione NON-DEFEAT non cambia.

|Metodo|TRUE DEFEAT min / p10 / mediana / p90 / max|NON-DEFEAT mediana / p95 / max|False DEFEAT a0.85/.04|
|---|---|---|---:|
|Grayscale|0.084 /0.532 /0.816 /0.968 /1.000|0.384 /0.422 /0.479|0|
|CLAHE|0.128 /0.468 /0.763 /0.952 /0.990|0.309 /0.346 /0.381|0|
|Gradient Sobel|0.168 /0.530 /0.759 /0.944 /0.995|0.443 /0.502 /0.599|0|

I frame fading/partial si sovrappongono ai negativi: non si pretende di classificare ogni frame dell'animazione. Si cercano almeno due frame forti per evento. A livello evento tutti gli8 casi LOSS calibration sono coperti da patch estratte solo da74, inclusi i6 casi indipendenti di86. Secondo frame più debole fra i casi corretti:0.868121; massimi NON-DEFEAT0.478860. Questo è un margine convincente sul piccolo campione disponibile, non una garanzia universale.

|Gate grayscale, margin0.04|LOSS con≥2 PTS /8|False DEFEAT frame revisionati|
|---|---:|---:|
|0.80|8/8|0|
|0.85, scelto|8/8|0|
|0.90|7/8|0|
|0.92|6/8|0|
|0.95|4/8|0|

Il0.92 del vecchio matcher non è trasferibile automaticamente a patch/ROI diverse.0.85 conserva supporto per tutte le8 LOSS calibration;0.80 non aggiunge eventi. Nessuna soglia deriva dai failure di73. [Tracce e identità template](calibration/score_tables.json), [confronto rappresentazioni](calibration/final_representation_study_reviewed.json).

La variante context più larga non aggiunge eventi; mediana TRUE DEFEAT0.602 e massimo NON-DEFEAT0.609, contro0.816/0.479 del testo. Nessun gate pesato context/OCR viene attivato. Tutti gli score sono correlazioni, non probabilità.

## Robustezza sintetica e OCR

22 picchi/fasi positive di19 partite calibration indipendenti,15 varianti ciascuno:330 casi. Brightness±20, contrast0.8/1.2, gamma0.85/1.15, saturation0/0.7/1.3, blur0.55, sharpening, JPEG quality35, scale0.95/1.05 e originale. Sono test, mai nuovi template.

|Metodo|Corretti /330|False DEFEAT|
|---|---:|---:|
|Grayscale|330/330|0|
|CLAHE|325/330|0|
|Gradient|326/330|0|
|Windows OCR exact Italian words|239/330|0|

OCR sperimentale Windows.Media.Ocr it-IT, compilato da sorgente nel helper opzionale. Stessa micro-ROI 144×87, ingrandita 4× con interpolazione cubica a576×348 per leggibilità, senza contesto aggiuntivo. Sui1511 frame nativi revisionati: DEFEAT441/499, VICTORY562/635, altre schermate377/377 UNKNOWN;1380/1511 corretti,0 false DEFEAT. L'OCR legge più fasi deboli del template conservativo a livello frame, ma scende a239/330 nelle perturbazioni, contro330/330 grayscale. Errori tipici di font/animazione: Vdittoria, Scorifitta. Non si usa fuzzy correction per forzare LOSS.

29.308 s complessivi per1841 chiamate (~15.9 ms/frame, escluso compile/setup). Il matcher grayscale dello studio impiega17.005 s per2276 frame (~7.5 ms/frame); misure indicative con input e carico differenti. Nessun OCR su tutto il VOD, nessuna nuova dipendenza applicativa: l'esperimento richiede Windows SDK/.NET reference assemblies e lingua OCR installati. OCR resta candidato complementare futuro, non selezionato nella configurazione congelata. [API ufficiale](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.recognizeasync?view=winrt-26100), [misure](calibration/ocr_final_measurements.json), [sintesi](calibration/ocr_final_summary.json), [robustezza](calibration/final_robustness_study.json).

## Confronto 30/60 FPS locali

Su86, stessi trigger/ROI/threshold/template:

|Metrica|30 FPS|60 FPS scelto|
|---|---:|---:|
|Complete corrette|13/14|14/14|
|LOSS recall|5/6|6/6|
|False LOSS|0|0|
|Frame risultato locali|4920|9840|
|Tempo wall|299.910 s|256.074 s|

La LOSS breve di86#5 ha un solo frame forte a30 FPS (2300.750,0.868121), quindi è respinta dalla regola ordinaria. A60 FPS ha anche2300.716,0.906123: due PTS reali, evento accettato. Questo giustifica60 FPS senza abilitare eccezioni single-frame. I tempi sono raccolti con carico concorrente: non dimostrano che60 FPS sia intrinsecamente più veloce di30.

## Freeze e riproduzione

[Manifest](iteration5_frozen/freeze_manifest.json):61 hash di codice, asset, configurazione, annotazioni e ground truth held-out. Fingerprint profilo:4aaeed45c036d569d5db00d6d2ae84775be5e21300b341b6819fcf0bc5031bdb. Il profilo usato nelle misure calibration differisce dal sealed solo per metadati di status/provenienza; parametri operativi e byte degli asset identici, [relazione verificata](calibration/freeze_profile_relation.json).

Uso e tooling riproducibile: [GAMEPLAY_STATE_AWARE.md](../../GAMEPLAY_STATE_AWARE.md). Il CLI result_experiment è stato eseguito anche su74 reale e valida role=calibration/fingerprint prima del sampling. Nessun uso delle11 validation games per ROI, threshold, template, scale o trigger. L'unico blind73 può iniziare soltanto dopo il confronto OLD/NEW calibration; nessun retuning dopo.

## Quality gate

66 nuovi test: profile/path/provenance boundaries, board mask/blank, tie/margin/representation, ordinary distinct-frame support, 30/60 sampling effects, weak/board trigger e hysteresis, merging/clamping/cancel, missing starts, duplicate outcomes/barriers/opposite outcomes/idempotence, candidate CLI headless/cache/debug, calibration-role rejection, robustness e OCR word normalization.

- Gameplay:315 passed,0 failed,0 skipped.
- Full pytest:764 passed,0 failed,1 skipped (googleapiclient opzionale assente, preesistente).
- Ruff:PASS; mypy:PASS,59 file; GUI smoke offscreen:PASS.
- Primo tentativo full:un test export esistente usa il legacy cache nella home non scrivibile in questo ambiente. Rieseguita suite con AUTO_CUTTER_CHUNK_CACHE_DIR isolata → PASS; nessuna modifica all'exporter. Evidenza iniziale preservata.

[Comandi/output](calibration/quality_gate.json). Nuovi moduli:state_profile,result_text,board,state_monitor,state_aware,result_events,result_experiment; helper OCR Python/C# e test. Unica modifica nuova al codice esistente:dispatch/argomenti incrementali di debug_analyze.py. VS,GameAssembler,GUI,exporter,TrackState e formato progetto non modificati in questa iterazione.

## Limiti

74 ha annotazioni SPARSE: cinque anchor risultato indipendenti, non ground truth completa. I12 giochi del candidato74 sono stati verificati visivamente tramite VS/banner ma questa revisione è candidate-seeded, quindi non misura il recall totale di74.86 ha ground truth completa di14 giochi; resta calibration, non test indipendente finale.

Layout Hearthstone italiano e viewport simili; altre lingue, crop/overlay, font e skin richiedono nuovi VOD indipendenti. Se il board non viene mai stabilito e il monitor debole manca un banner brevissimo, una finestra può non aprirsi. Dedup può restare ambiguo per giochi ravvicinati con VS e board-return entrambi mancanti. Nessuna prova di produzione da questi piccoli dataset, nessuna cancellazione automatica di partite.



## Evidenza scelta board

[Figura ROI calibration74](calibration/roi_geometry74.jpg). Sei regioni candidate sono state misurate sul monitor2FPS di86 con riferimenti74: end-turn,hero-power,hero alto/basso,mana tray,right rail. Il confronto usa interni annotati start+12s → result−5s e zone esterne agli intervalli start−5s → result+3s, quindi proxy temporali di stato, non una ground truth per-frame del board. Le schermate risultato lunghe possono entrare nel gruppo OUT; la verifica determinante è la copertura dei trigger sugli anchor manuali.

|Anchor edge|IN p10 / mediana|OUT massimo|
|---|---|---:|
|End-turn|0.487 /0.601|0.407|
|Hero-power|0.339 /0.540|0.322|
|Bottom hero|0.363 /0.433|0.378|
|Top hero|0.332 /0.387|0.320|
|Mana tray|0.363 /0.443|0.348|
|Right rail|0.407 /0.474|0.469|

La congiunzione delle prime due cornici è meno dipendente da ritratto/carte/mana; i centri sono mascherati. Gli score grayscale end-turn hanno OUT fino0.668 e right-rail fino0.671: il colore/texture non basta. [Studio completo](calibration/board_anchor_study.log), [grid trigger](calibration/trigger_grid.log). La nota esplorativa top-DEFEAT nel log non è ground truth negativa: comprende una schermata LOSS lunga, quindi non è usata nelle metriche negative o nella scelta della soglia.


## File e scope di questa iterazione

Nuovi backend: analysis/gameplay/hearthstone/{state_aware,state_monitor,result_experiment}.py, visual/{state_profile,board,result_text}.py e analysis/gameplay/result_events.py. Tool OCR: tools/gameplay/{ocr_experiment.py,WindowsOcrExperiment.cs}. Test: tests/test_gameplay_state_aware.py. Documentazione/asset: GAMEPLAY_STATE_AWARE.md e questa directory.

Modifica incrementale: analysis/gameplay/hearthstone/debug_analyze.py per dispatch/argomenti del candidato. Append a GAMEPLAY_CALIBRATION.md e GAMEPLAY_VALIDATION_73.md. Nella working tree erano già presenti modifiche delle iterazioni precedenti ad assembler, evaluation, analyzer globale e relativi test: non sono state riscritte né revertite. Nessuna nuova modifica in questa iterazione a GUI/exporter/TrackState/project persistence/preset.

Il piccolo esperimento sul threading OpenCV non ha dato un guadagno convincente per queste ROI (32 thread1.946s,1 thread2.028s,2 thread2.341s sullo stesso microbenchmark); non è stata introdotta una configurazione globale dei thread. [Misura](calibration/small_roi_threads.log). Il guadagno deriva dal volume di matching e dal crop, non da una riduzione del recall o da nuovi codec.


## OLD vs NEW — confronto calibration completato prima del blind

[Confronto macchina](calibration/old_new_comparison.json), completato2026-10-08T23:50:20.850913Z. OLD usa il pack iteration4 congelato,20 FPS globali288×120+refinement nativo; NEW il profilo micro-ROI/monitor congelato. Entrambi senza cache. Nessun parametro è stato cambiato dopo il confronto.

|Misura|74 OLD|74 NEW|86 OLD|86 NEW|
|---|---:|---:|---:|---:|
|Complete candidate|12|12|14|14|
|Partial|0|0|0|0|
|WIN / LOSS|10 /2|10 /2|8 /6|8 /6|
|Anchor risultato totali|24|12|29|14|
|Known result anchor recall74 (sparse)|5/5|5/5|—|—|
|Complete game precision/recall86|—|—|100% /100%|100% /100%|
|DEFEAT/VICTORY/VS recall86|—|—|100% /100% /100%|100% /100% /100%|
|Extra result anchors86 (duplicati/fasi)|—|—|15|0|
|Extra DEFEAT anchors86|—|—|2|0|
|False LOSS games86|—|—|0|0|
|Frame coarse/monitor|7641|7641|7980|7980|
|Frame VS refinement|588|588|668|668|
|Frame risultato ad alta frequenza|159435|9478|165735|9840|
|Tutti i frame emessi/analizzati|167664|17707|174383|18488|
|Confronti template inclusiVS|8658864|506076|9001578|527448|
|Tempo analisi|46m32.131s|5m23.889s|50m10.342s|4m16.074s|

Risparmio confronti:8152788(94.16%) su74;8474130(94.14%) su86. Riduzione frame alta frequenza94.06% su entrambi; tutti i frame emessi si riducono di circa89.4%. NEW esegue anche text weak matching sui frame monitor: questi confronti sono INCLUSI nel totale, non nascosti nel risparmio. Le righe alta frequenza confrontano solo OLD dense+native con NEW finestre locali.

Formula OLD:54 varianti risultato×(dense+native), più6 variantiVS×(coarse+fine). NEW:counter reali board+text_monitor+text_windows, piùgli stessi6VS×(coarse+fine). Non si contano come doppie le diagnostiche che riusano gli stessi score.

74:nessuna pretesa di recall totale o pairing validato sull'intero VOD. La revisione candidate-seeded conferma i12 banner classificati; i5 anchor indipendenti già annotati passano in entrambe le strategie.86:14/14 complete corrette, classifica14/14, LOSS6/6, zero pairing errati. I15 extra anchor OLD sono fasi/duplicazioni di risultati reali, non15 partite inesistenti né15 false LOSS. Il nuovo layer eventi li elimina prima dell'assembler.

I tempi sono wall time su macchina condivisa. Un benchmark OLD74 è partito due volte per errore; qui è conservato soltanto il PRIMO risultato completo, prima che la copia ridondante possa sovrascrivere il cache locale. Questa duplicazione rende il confronto temporale meno controllato; non modifica conteggi, parametri o numero di blind run. La riduzione del volume di matching è verificabile indipendentemente dal carico.

## Profiling

|Fase86|OLD sampling wait / matching|NEW sampling wait / matching|
|---|---|---|
|Coarse VS / nuovo monitor VS+board+weak|164.809 /32.734 s|110.872 /100.210 s|
|VS refinement|2.654 /2.840 s|2.591 /3.104 s|
|Global dense risultato|6.728 /2671.404 s|nessuna passata|
|Native result refinement / finestre|18.999 /99.864 s|3.472 /35.547 s|

OLD86 passa88.74% del tempo nel matching globale denso; NEW lo elimina. Il nuovo costo principale è il passaggio monitor+decode (circa82.4% del totale), con43.3% in sampling wait del monitor e39.1% nel suo matching. Il matching delle finestre pesa13.9%. Su74 il monitor+decode pesa83.2%.

Stream FFmpeg:86 da159 a35,74 da177 a31 (VS invariato). Sampling wait include decoding, attesa pipe e startup; il nuovo timing non isola il primo-frame startup, quindi non viene inventata una misura CPU/decode separata. FFmpeg può decodificare molti più frame sorgente di quelli emessi; i totali in tabella NON sono i frame interni del decoder. Le finestre locali comportano anche seek/GOP cost, già incluso nel wall time. [Report OLD86](calibration/old_86/gameplay_analysis.json), [OLD74](calibration/old_74/gameplay_analysis.json), [NEW86](calibration/new_86_60/gameplay_analysis.json), [NEW74](calibration/new_74_60/gameplay_analysis.json).


## Blind finale73 — decisione

**NEEDS_MORE_CALIBRATION_DATA.** Unico run completo dopo freeze/confronto calibration;nessun retuning. 11 partite frozen:10 complete corrette+1partial,game precision100%,complete recall90.91%,VS11/11,VICTORY7/7,DEFEAT3/4(75%),LOSS precision100%,zero false LOSS/extra result anchors/pairing errati. Classifica10/10 risultati osservati;non11/11. Start MAE/mediana/max0.224/0.100/0.767s(11anchor);result0.0785/0.0335/0.334s(10anchor).

Tempo207.342s(3m27s),contro2337.738s(38m58s);6817monitor,722VSfine,9421resultROIin18finestre/159s. Frame totali145682→16960(−88.36%);confronti7504956→475848(−93.66%),inclusi weak text/board/VS. Nuovo costo principale:monitor+decode82.5%,local result matching13.1%. Tempi wall indicativi su macchina condivisa.

LOSS2305 recuperata con3PTS distinti in0.067s,peak0.919962. LOSS5010.7 ancora sotto il gate:trigger5008→5015 corretto,frame presente,ROI corretta,piccoDEFEAT0.827201 vsVICTORY0.368112,margin0.459089. Zero osservazioni ammesse,quindi nessun failure del consolidator o dell'assembler. Un replay diagnostico della sola finestra7s(420frame) usa gli stessi parametri;non è un secondo blind né un nuovo tuning. Il frame resta escluso da calibration/template creation.

[BEFORE/AFTER macchina](blind73/comparison_summary.json),[analisi](blind73/gameplay_analysis.json),[evaluation](blind73/gameplay_evaluation.json),[failure diagnostic](blind73/failure_diagnostic.json),[tabella11partite e storia completa](../../GAMEPLAY_VALIDATION_73.md).61 hash invariati prima/dopo,in [audit](iteration5_frozen/hash_audit_after_blind.json);guardia esclusiva registra1tentativo.

La strategia state/trigger copre11/11 timestamp risultato held-out,ma il riconoscimento/gate del testo resta il limite su una variante. Servono altri VOD calibration indipendenti e hard negatives,non una soglia scelta dal failure73. OpenCV resta promettente:classe alternativa distante e nessuna evidenza di overlap sistematico. Nessun PyTorch,nessun gameplay cut,default precedente preservato.


Chiusura sessione: tutti i processi sperimentali sono terminati con exit 0. Il benchmark OLD74 partito per errore una seconda volta produce eventi e games identici al primo, con wall time 2485.143 s invece 2792.131 s; il confronto principale mantiene il primo risultato. Nessun altro blind73, nessun retuning. [Controllo finale](calibration/benchmark_cleanup.json).

## Reference provenance clarification — 2026-10-09

L’utente ha confermato successivamente che i sei screenshot in templates/ provengono da un altro VOD non ancora scaricato. Sono candidati per una futura calibrazione esterna, sulla base della dichiarazione dell’utente; URL/ID sorgente, timestamp e fingerprint del video restano da documentare. Le note precedenti riflettono quanto noto al freeze: uso per sola geometria generale, nessuna calibrazione quantitativa con questi screenshot. Configurazione congelata, asset, metriche e unico blind run su 73.mp4 restano invariati. [Inventario con SHA-256 e provenienza](reference_images_provenance.json).


## Iteration 6 — Result decision policy (2026-10-09)

Strato decisionale sostituito senza toccare trigger, ROI, template o assembler: cluster class-agnostic di frame con max(V,D) ≥ 0.65 (gap 0.5 s), conferma della schermata con ≥ 2 PTS distinti, poi WIN/LOSS = argmax del frame di picco; UNKNOWN se il picco è ambiguo (< 0.04) o se c'è supporto contrapposto. Tutti i parametri sono riusati, nessuno nuovo. Calibration 74+86: 20/20 tracce, 0 hard negatives sopra il floor. Blind 73 unico: **11/11 complete, LOSS 4/4, precision 100%, 0 false LOSS, 0 pairing errors, 3m28s.** Decisione: RESULT_POLICY_READY. Dettagli: [result_decision/README.md](result_decision/README.md).

## Validation indipendente videoprova.mp4 (2026-10-09)

VOD nuovo (1h02m30s), GT manuale prima del run, configurazione iteration 6 congelata (48/48 hash identici), un solo run, nessun tuning. 5 partite (2 WIN, 2 LOSS, 1 troncata a fine VOD): 4/4 complete corrette, VS 5/5, LOSS recall/precision 100%/100%, 0 false LOSS, 0 pairing errors, partita troncata lasciata UNKNOWN, 117 s (112.6 s per ora di video). Decisione: READY_FOR_GAMEPLAY_CUTS, con caveat sul campione (2 LOSS). Dettagli: [videoprova/README.md](../videoprova/README.md).
