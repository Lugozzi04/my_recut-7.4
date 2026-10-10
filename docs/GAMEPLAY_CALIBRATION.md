# Calibrazione video-only Hearthstone

Il comando di calibrazione analizza solo finestre annotate, senza scansione globale,
GameAssembler o cut. Non installa template e non sceglie/abilita soglie.
Il set delle 11 partite di validation di 73.mp4 è congelato: non usarlo per
scegliere patch, scale, ROI, soglie o policy.

## 1. Annotare un VOD indipendente

Usare un file diverso dalla validation. 74.mp4 è la prima sorgente indipendente
esaminata; le annotazioni provvisorie sono in
[calibration_annotations.json](gameplay_validation/74/calibration_annotations.json).
Sono un sottoinsieme, NON la ground truth completa di tutte le sue partite.

Esempio minimo (tempi numerici in secondi):

```json
{
  "format": "recut_gameplay_calibration",
  "version": 1,
  "role": "calibration",
  "games": [
    {"start": 123.4, "result": "loss", "result_time": 655.2}
  ],
  "negative_windows": [
    {"start": 300.0, "end": 302.0}
  ]
}
```

Aggiungere preferibilmente video_fingerprint: il tool rifiuta un video diverso.
Per ottenerlo, dalla directory del progetto:

```powershell
python -c "from pathlib import Path; from analysis.gameplay.cache import video_identity; import sys; print(video_identity(Path(sys.argv[1]))['fingerprint'])" "NEW_VOD.mp4"
```

In alternativa a games si può usare events con type (VS_SCREEN/VICTORY/DEFEAT)
e timestamp, come nelle annotazioni di 74. Più finestre della stessa animazione
non sono altrettante partite indipendenti.

Un timestamp identifica una finestra da esaminare: NON rende positivi tutti i suoi
frame. Dopo verifica visiva aggiungere visible_start/visible_end all'evento,
oppure result_visible_start/result_visible_end e start_visible_start/start_visible_end
al game. Gli intervalli devono essere inclusi nella finestra campionata.
I frame fuori da questi intervalli restano UNREVIEWED: non sono negativi impliciti.
Le negative_windows devono essere verificate e non sovrapporsi agli anchor;
sono limitate a 60 secondi ciascuna. Annotazioni con role=validation sono rifiutate.

## 2. Estrarre tracce e frame locali

```powershell
$vod = Join-Path $env:USERPROFILE "Downloads\74.mp4"
python -m analysis.gameplay.hearthstone.debug_analyze "$vod" --template-pack "docs/gameplay_validation/73/iteration2/template_pack/templates.json" --calibrate "docs/gameplay_validation/74/calibration_annotations.json" --calibration-fps 60 --calibration-padding 1 --debug-frames --max-debug-frames 240 --output-dir ".pytest_cache/calibration74" --json
```

Output: gameplay_calibration.json e calibration_frames/window_NNNN/.
Il JSON riporta per ogni frame anche score sotto soglia, template migliore,
scala, rectangle, score delle altre classi, margin e label manuale; contiene
configurazione, fingerprint del video/pack e hash delle annotazioni.
I frame salvati sono ripartiti tra finestre e includono il picco misurato.
La selezione del picco è diagnostica: verificare il testo visivamente prima di
considerarlo ground truth. stdout è JSON con --json; progress/errori su stderr.
Ctrl+C cancella e chiude il sampler FFmpeg.

Sampling locale: risultati fino al frame rate sorgente (default richiesta 60 FPS),
VS a 6 FPS, negativi ROI a 20 FPS. Nessun frame duplicato/sintetico.
Il crop risultato usa la stessa geometria arrotondata del detector di produzione.
Per fasi stabili o animazioni diverse aggiungere un altro anchor della stessa
classe, senza dichiararlo automaticamente una nuova partita.

## 3. Espandere un pack candidato

Lavorare su una COPIA esplicita del pack, senza sostituire config/default o il pack
congelato di iteration2. Il pack candidato di questa iterazione si trova in
[74/candidate_pack](gameplay_validation/74/candidate_pack/README.md).

Dopo aver controllato un frame, estrarre una patch stretta che contenga il testo,
in coordinate normalizzate del viewport di riferimento. Esempio realmente usato,
solo da 74 e NON dalle 11 partite di validation:

```powershell
python -m analysis.gameplay.hearthstone.debug_analyze "$vod" --template-pack "docs/gameplay_validation/74/candidate_pack/templates.json" --capture DEFEAT --at 6119 --roi 0.446875 0.5814814814814815 0.11875 0.053703703703703705 --name external74_defeat_stable --capture-annotations "docs/gameplay_validation/74/capture_source_annotations.json" --json
```

Questa patch esiste già: il comando la rifiuterà se ripetuto.
La cattura registra timestamp effettivo, fingerprint video, hash annotazioni,
ROI, hash patch e template simili nel companion .png.provenance.json.
Per attestare role=calibration richiede un fingerprint corrispondente e un anchor
annotato di quella classe entro la finestra di cattura. Senza annotazioni, la
cattura mantiene la compatibilità precedente ma la provenienza è unassigned,
quindi non basta per dichiarare il template utilizzabile in calibrazione.

Duplicati pixel-identici (nello spazio grayscale usato dal detector) sono rifiutati.
Per patch di rapporto d'aspetto simile si confronta la correlazione dopo resize;
a >=0.995 la cattura richiede revisione esplicita e --allow-similar-template.
0.995 è solo un avviso di curation, NON una soglia di detection. Non si eliminano
file né si deduplicano automaticamente fasi diverse. Nessuna sovrascrittura di
patch o provenienza preesistente.

## 4. Policy single-frame: congelare SOLO quando giustificata

Il comportamento ordinario resta >=2 osservazioni risultato distinte e span
>=1/120 s. VS mantiene >=3 osservazioni e >=0.25 s.
SingleFrameResultPolicy ammette regole separate per VICTORY e DEFEAT:
peak_similarity, minimum_class_margin, neighbor_similarity,
maximum_neighbor_gap_s. Ogni regola può essere null (disabilitata).

Una regola abilitata richiede un calibration_id e il fingerprint del pack esatto;
non può allentare la soglia ordinaria o il margin del pack. Richiede un solo
frame risultato forte, entrambi gli score concorrenti, un picco locale stretto
con frame distinti prima E dopo, e almeno un vicino più debole che favorisca la
stessa classe e superi la soglia di supporto calibrata. Non si applica a VS.
Evidenza incompleta/ambigua/isolata viene respinta con motivo nel report.
Gli score dei frame vicini sono riutilizzati dal matching del refinement nativo:
non serve un'altra passata globale.

Al momento entrambe le regole sono null. Esempio eseguibile e volutamente inattivo:
[single_frame_policy.disabled.json](gameplay_validation/74/single_frame_policy.disabled.json).
Può essere passato all'analisi con --single-frame-policy PATH.
I numeri dei test sintetici NON sono parametri utilizzabili in produzione.

Prima di abilitarla raccogliere altri risultati brevissimi verificati visivamente,
con tracce native prima/durante/dopo e falsi candidati difficili (overlay/testi,
transizioni, carte, menu). La calibrazione attuale contiene due LOSS indipendenti
in 74, entrambe con più frame forti, e solo 8 s di negativi nuovi: non basta
per scegliere una soglia della rara eccezione single-frame. Usare altri anchor
indipendenti o 86.mp4; non occorre un nuovo formato né un modello ML.

Dopo revisione: scegliere i parametri dal SOLO calibration data, congelare
annotazioni/pack/config/hash e codice; eseguire UNA validation di 73 senza retuning.
Se la validation fallisce, riportare il fallimento e cercare altri dati indipendenti.
Un miglioramento sul frame da cui è stata estratta la patch è training fit,
non prova di generalizzazione. Non abilitare gameplay cuts da questi report.


## 5. Iteration 4: calibrazione74↔86 e pack congelato

È stata completata la revisione di86:14 partite(8WIN,6LOSS), annotazioni manuali
e risultati automatici separati in [86/README.md](gameplay_validation/86/README.md).
Non è una nuova fonte di validation:74 e86 sono calibration. Le11 partite
held-out di73 rimangono escluse dal tuning.

Il CLI `--calibrate` ora riporta anche `template_scores` per ogni patch distinta
(con la sua miglior scala), `second_best_template`, percentili p10/p90/p95.
La diagnostica riusa i match calcolati; non esegue un secondo matching.
`score_roi(..., include_template_scores=True)` è opt-in: il detector ordinario
non conserva questi ranking durante la scansione globale.

```powershell
$vod = Join-Path $env:USERPROFILE "Downloads\86.mp4"
python -m analysis.gameplay.hearthstone.debug_analyze "$vod" --template-pack "docs/gameplay_validation/86/iteration4_frozen/templates.json" --calibrate "docs/gameplay_validation/86/calibration_annotations.json" --calibration-fps 60 --calibration-padding 1 --debug-frames --max-debug-frames 240 --output-dir ".pytest_cache/calibration86" --json
```

Il comando fornisce diagnostici sulle finestre annotate; NON una nuova misura
di recall sull’intero VOD. Le annotazioni hard-negative sono conservate a parte
per distinguere VICTORY (negativa per DEFEAT soltanto), altre schermate e
transizioni miste. Il manifest [study_sampling_manifest.json](gameplay_validation/86/study_sampling_manifest.json)
riporta tutte le56 finestre ROI usate nello studio, FPS, intervalli visibili e
override per-frame; i cache NPZ locali evitano decode duplicati fra pack.

Per una successiva raccolta: minare score DEFEAT globali a sampling coarse,
selezionare top-N distanziati, verificare visivamente classe e vicini nativi,
poi confrontare i candidati sugli stessi frame positivi/negativi indipendenti.
Non promuovere patch che migliorano soltanto il proprio frame sorgente.
La raccolta attuale ha30 neighborhoods revisionati di86; non è un audit
esaustivo di tutti i frame né copre layout/lingue diversi.

Pack finale di questa iterazione:
[iteration4_frozen](gameplay_validation/86/iteration4_frozen/templates.json).
Contiene solo le aggiunte accettate:74 stable e86 late. `86/candidate_pack`
conserva anche early/mid **scartati**, quindi non va usato al posto del congelato.
Il default installato dell’app non viene modificato.

Soglia0.92 e margin0.04 invariati. Single-frame ancoraOFF: il pack accettato
fornisce≥2 osservazioni native per le8 LOSS calibration indipendenti; non esiste
un nuovo gate single-frame numerico motivato dal campione. Non allentare
questa scelta guardando il risultato del blind73.

Per verificare l’immutabilità prima/dopo una validation:

```powershell
python -c "import hashlib,json; from pathlib import Path; m=json.loads(Path('docs/gameplay_validation/86/iteration4_frozen/freeze_manifest.json').read_text()); changed=[p for p,h in m['files'].items() if hashlib.sha256(Path(p).read_bytes()).hexdigest()!=h]; print(changed); raise SystemExit(bool(changed))"
```

Il manifest vincola codice gameplay, annotazioni86, patch e settings. Le tracce
multi-pack compresse sono in `86/score_traces.json.gz`; il JSON del pack
congelato è `86/combined_study_scores.json`. I percentili per-frame includono
fasi deboli: valutare separatamente copertura del picco, supporto temporale,
e ricostruzione finale. Non scambiare499 frame positivi per499 partite.


### Esito del freeze Iteration4

L’unico blind73 è concluso:9/11 complete,2 partial,LOSS recall50%,LOSS precision100%,0 false LOSS ricostruite. VS e VICTORY100%,0 pairing errati. È comparso un anchor DEFEAT duplicato della stessa LOSS, conservato nella metrica eventi (precision DEFEAT2/3), oltre alla VICTORY duplicata precedente. Nessun retuning dopo il risultato.

**NEEDS_MORE_CALIBRATION_DATA**: il pack aggiunge supporto di fase ma non recupera le LOSS held-out. Rimane sperimentale, non modifica il default installato. Single-frame OFF; nessun nuovo gate scelto usando73. Cercare altri risultati calibration brevissimi e negativi difficili con il pack finale prima di nuove regole. Non emerge inseparabilità che giustifichi PyTorch.

Risultati, tempi e confronto per partita: [GAMEPLAY_VALIDATION_73.md](GAMEPLAY_VALIDATION_73.md), [86/blind73](gameplay_validation/86/blind73/comparison_summary.json). L’immagine in blind73 è diagnostica held-out: non usarla per template o calibrazione. Il pack congelato e tutti i suoi31 file vincolati sono rimasti invariati.


## 6. Iteration 5 — State-aware candidate, micro-ROI e OCR comparativo

Il detector globale precedente rimane il default. Il candidato opt-in usa un passaggio1FPS per VS/board/testo debole, poi crop risultato144×87 a60FPS SOLO nelle finestre trigger−2/+5s. VS e GameAssembler invariati, single-frame exception OFF. Le soglie del nuovo matcher testo sono0.85 e margin0.04, selezionate esclusivamente da74+86; non trasferire automaticamente il0.92 del vecchio matcher a patch diverse.

[Report dettagliato e artefatti](gameplay_validation/state_aware/README.md), [profilo congelato](gameplay_validation/state_aware/iteration5_frozen/profile.json), [uso CLI](GAMEPLAY_STATE_AWARE.md). Quattro template testo e sei riferimenti board provengono solo da74;86 verifica generalizzazione fra VOD e supporto dei risultati brevi. Nessun template dalle11 validation games di73. Le immagini templates/ fornite dall'utente hanno provenienza non confermata: impiegate per geometria generale, non come calibration quantitativa.

Nuovo esperimento riproducibile sulle finestre annotate:

~~~powershell
python -m analysis.gameplay.hearthstone.result_experiment NEW_CALIBRATION_VOD.mp4 --annotations CALIBRATION.json --state-profile docs/gameplay_validation/state_aware/iteration5_frozen/profile.json --output-dir CALIBRATION_REPORT --robustness --ocr-manifest
~~~

Il tooling valida role=calibration e fingerprint, campiona ogni finestra una volta, confronta grayscale/CLAHE/Sobel sugli stessi frame e produce perturbazioni di test senza creare template. L'OCR è opzionale e separato:

~~~powershell
python -m tools.gameplay.ocr_experiment CALIBRATION_REPORT/ocr_manifest.json --output CALIBRATION_REPORT/ocr_report.json
~~~

Usa Windows.Media.Ocr con SDK/.NET e lingua italiana già installati; nessuna dipendenza runtime aggiunta e nessun import dal detector. Normalizzazione di punteggiatura/accenti e parole esatte, nessuna correzione fuzzy che forzi LOSS.

Il pool revisionato mantiene499 DEFEAT; una label storica86_hard_08@7390.583 è stata corretta in una copia aVICTORY dopo revisione visiva. Diventa635 VICTORY e377 altri negativi;765 UNREVIEWED restano esclusi. [Override](gameplay_validation/state_aware/calibration/label_overrides.json), originale preservato. I1511 frame revisionati sono correlati: soltanto8 LOSS calibration indipendenti.

Sui22 picchi/fasi,330 perturbazioni realistiche:grayscale330/330,CLAHE325/330,Sobel326/330,OCR239/330. Il grayscale è scelto; il context più largo non aggiunge coverage e peggiora la separazione DEFEAT/negativi. OCR ha maggiore recall su alcune fasi native deboli ma minore robustezza sintetica e costo operativo maggiore; resta esperimento complementare.

Freeze:61 hash di codice/config/asset/annotazioni, fingerprint4aaeed45c036d569d5db00d6d2ae84775be5e21300b341b6819fcf0bc5031bdb. Parametri operativi identici alle misure calibration, sola metadata/provenienza arricchita nella copia sealed. OLD/NEW calibration deve completarsi prima dell'unico retest blind73. Nessun retuning dopo quel risultato.


Esito finale Iteration5: **NEEDS_MORE_CALIBRATION_DATA**. Blind73 una sola volta,config invariata:10/11complete+1partial,LOSS recall75%,precision100%,falseLOSS0,VS/VICTORY100%,pairing0;3m27s invece38m58s. Failure5010.7 correttamente campionato/ROI,score0.827201<gate0.85;nessun ritocco sul validation frame.61hash prima/dopo uguali. [Report blind](GAMEPLAY_VALIDATION_73.md),[confronto JSON](gameplay_validation/state_aware/blind73/comparison_summary.json). Continuare con nuovi esempi calibration indipendenti;non promuovere cut automatici da questo risultato.

## Reference provenance clarification — 2026-10-09

L’utente ha successivamente confermato che i sei screenshot in templates/ provengono da un altro VOD, non ancora scaricato. Provenienza dichiarata dall’utente: URL/ID, timestamp dei frame e fingerprint del video restano da registrare e verificare. I riferimenti sono candidati per un prossimo ciclo di calibrazione indipendente; al momento del freeze erano stati usati soltanto per geometria generale. Il video sorgente servirà anche per valutare animazioni, trigger e risultati brevi. Nessuna modifica al pack congelato o alle metriche già pubblicate. [Inventario con hash e provenienza](gameplay_validation/state_aware/reference_images_provenance.json).


## 7. Iteration 6 — Result decision policy (solo 74 + 86)

Lo studio offline `python tools/gameplay/result_decision_study.py` rigioca la policy implementata (`analysis/gameplay/hearthstone/result_decision.py`) sugli score già salvati di 74/86 (nessun VOD decodificato, 73 mai letto) e rigenera `docs/gameplay_validation/state_aware/result_decision/calibration_study.json` e la fixture `tests/fixtures/gameplay/result_decision_calibration_traces.json`. Esce con codice ≠ 0 se una traccia risultato non è più classificata correttamente. Per nuovi VOD calibration: aggiungere le loro tracce revisionate allo studio PRIMA di cambiare qualsiasi parametro della policy; la single-frame policy resta disabilitata. Risultati e razionale: [result_decision/README.md](gameplay_validation/state_aware/result_decision/README.md).
