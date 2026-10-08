# Gameplay Cutter Hearthstone: prototipo video-only

Questa milestone comprende detection e diagnostica. Non genera tagli, non modifica
TrackState/progetti/preset/timeline/export e non legge log, telemetry o dati di gioco.
Non usa PyTorch, un LLM o servizi di rete. Dopo installazione, funziona offline.

## Stato e limiti della validazione

Il codice contiene il detector OpenCV, ma il pack distribuito è intenzionalmente
vuoto. Dal VOD utente 73.mp4 sono state preparate patch locali e annotazioni
provvisorie, descritte in GAMEPLAY_VALIDATION_73.md; il pack non è ancora validato.
Per altri VOD occorre catturare template distintivi per ciascuna classe.
Un pack incompleto produce un errore esplicito prima della scansione.

Le soglie iniziali (0.92 similarity e 0.04 margine) sono **non calibrate**.
Le misure reali e il relativo split calibrazione/validation sono riportati in
[GAMEPLAY_VALIDATION_73.md](GAMEPLAY_VALIDATION_73.md), con i limiti delle annotazioni.
I test sintetici verificano gli algoritmi, non la generalizzazione al gioco.

OpenCV 4.12 è ora installato nell’ambiente Python del progetto: i test CV usano
il matcher reale. Il precedente blocco di installazione resta documentato nello
storico della validazione. requirements-dev.txt installa l’extra anche nella CI.
Le prove del sampler FFmpeg non richiedono OpenCV.

## Architettura e riuso

    file video
      -> VS: frame coarse 1 FPS -> finestre candidate -> fine scan 6 FPS
      -> risultati: crop FFmpeg ROI -> scan denso 20 FPS
                   -> solo candidati instabili: ROI locale ±0.15 s a 120 FPS
      -> VisualEventDetector / TemplateVisualDetector
      -> GameEvent[] -> consolidate_events (debounce e stabilità osservata)
      -> GameAssembler -> GameSegment[]
      -> GameplayAnalysisResult + report JSON

- analysis/gameplay/models.py: GameEvent, GameSegment, enum e serializzazione.
- analysis/gameplay/analyzer.py: protocollo generico e cancellation condivisa.
- analysis/gameplay/assembler.py: consolidamento e ricostruzione conservativa.
- analysis/gameplay/cache.py: fingerprint e JSON atomico.
- analysis/gameplay/evaluation.py: metriche su annotazioni manuali.
- analysis/gameplay/hearthstone/analyzer.py: orchestratore del profilo.
- analysis/gameplay/hearthstone/visual/: sampler, pack e detector sostituibile.
- analysis/gameplay/hearthstone/debug_analyze.py: frontend diagnostico.

Il sampler riusa ensure_ffmpeg(), subprocess senza finestre, probe cancellabili e
AnalysisCancellation di analysis/audio_service.py. La cache usa cache_root(), le
risorse resource_path(), e il fingerprint leggero già usato da core/project_file.py.
La promozione dei template riusa atomic_promote_output(); la diagnostica usa anche
ensure_output_is_safe() per proteggere video, annotazioni e manifest.
Nessun modulo gameplay importa Qt. Progress è una callback e la cancellazione può
essere richiesta da un thread esterno. Una futura GUI potrà aggiungere un adapter
worker come AnalyzeWorker; automation potrà chiamare lo stesso servizio.

GameSegment descrive contenuto; non è un Segment di cut. La futura GameCutPolicy
potrà trasformare LOSS affidabili in Segment e usare merge_overlaps() e
invert_to_keeps() esistenti. Questa policy e l'integrazione editoriale sono rinviate
alla validazione del detector.

## Installazione e help

Dalla repository, con l'interprete selezionato (esempio Windows):

    .\.venv310\Scripts\python.exe -m pip install -r requirements-gameplay.txt
    .\.venv310\Scripts\python.exe -m analysis.gameplay.hearthstone.debug_analyze --help

L'extra pinna opencv-python-headless==4.12.0.88, compatibile con Python 3.10 e il
NumPy 2.2.6 già presente. Headless evita di introdurre un secondo runtime Qt.
Non installare contemporaneamente altri pacchetti che forniscono cv2.
Questo deliverable si esegue da source; il main CLI/GUI ed il build distribuito
non sono stati estesi per esporre il nuovo comando.

## Preparare i template dal VOD

Scegli frame in cui VS, VICTORY e DEFEAT siano visibili e stabili.
Cattura una piccola regione distintiva della grafica, senza webcam, sottotitoli,
nomi variabili o ampie zone uniformi. Le coordinate ROI sono x, y, larghezza,
altezza, tutte normalizzate 0..1 nel viewport ridimensionato a reference_size.

Esempi **di sintassi**: sostituire timestamp e ROI con quelli osservati nel video.
Queste coordinate non sono una calibrazione Hearthstone verificata.

    python -m analysis.gameplay.hearthstone.debug_analyze "D:\VOD\video.mp4" --capture VS_SCREEN --at 00:02:14 --roi 0.35 0.30 0.30 0.35
    python -m analysis.gameplay.hearthstone.debug_analyze "D:\VOD\video.mp4" --capture VICTORY --at 00:10:42 --roi 0.30 0.25 0.40 0.30
    python -m analysis.gameplay.hearthstone.debug_analyze "D:\VOD\video.mp4" --capture DEFEAT --at 00:20:13 --roi 0.30 0.25 0.40 0.30

Senza --template-pack, capture crea un pack nella config scrivibile dell'utente.
Puoi specificare --template-pack "D:\Dataset\hearthstone\templates.json".
Il comando copia la configurazione iniziale, scrive PNG completi tramite temporaneo
nella stessa directory e non sovrascrive template esistenti. --name opzionale
sceglie il nome; i nomi riservati Windows e i separatori sono rifiutati.
Più catture aggiungono varianti alla stessa classe.

    templates.json
    templates/vs/*.png
    templates/victory/*.png
    templates/defeat/*.png

Modifica il JSON per adattare viewport (ritaglio del gameplay dentro il video),
ROI di ricerca per classe, reference_size, scales, threshold e ambiguity_margin.
Le ROI di ricerca sono normalizzate dentro il viewport. Le patch sono in pixel
alla geometria reference_size. Se cambi viewport/reference_size, ricattura le
patch. Un titolo/screenshot in altra lingua richiede template appropriati.

Il loader controlla schema, path relativi, traversal/symlink, immagini uniformi,
dimensioni e compatibilità con la ROI. I nomi Unicode vengono letti tramite byte.
Il detector usa grayscale TM_CCOEFF_NORMED, precompila le varianti di scala e prende
il miglior template per classe. Un quasi pareggio fra classi viene rifiutato.

Riferimenti primari: [OpenCV template matching](https://docs.opencv.org/4.x/d4/dc6/tutorial_py_template_matching.html),
[distribuzione headless selezionata](https://pypi.org/project/opencv-python-headless/4.12.0.88/).

## Analizzare

    python -m analysis.gameplay.hearthstone.debug_analyze "D:\VOD\video.mp4" --output-dir "D:\Gameplay debug" --debug-frames

    python -m analysis.gameplay.hearthstone.debug_analyze "D:\VOD\video.mp4" --template-pack "D:\Dataset\hearthstone\templates.json" --coarse-fps 1 --fine-fps 6 --result-fps 20 --result-refine-fps 120 --json

    from analysis.gameplay.hearthstone.analyzer import analyze_gameplay
    result = analyze_gameplay("video.mp4", on_progress=print)
    for game in result.games:
        print(game.index, game.start, game.end, game.result.value)

analyze_gameplay() accetta anche settings=HearthstoneSettings(...), cancellation,
detector/sampler iniettabili, use_cache=False e debug_frames=Path(...).
Il core non stampa: l'esempio on_progress=print è un adapter scelto dal chiamante.

Output: gameplay_analysis.json e, opzionalmente, debug_frames/*.jpg (massimo 100
per default, regolabile con --max-debug-frames). Il JSON include eventi consolidati,
partite, confidence separate, provenienza, settings, versioni, template SHA256,
fingerprint video, tempi e conteggi dei frame. --json mantiene stdout leggibile da
script, con progress su stderr. --debug-frames ripete la scansione anche con cache.

Le confidence sono punteggi di correlazione, **non probabilità**. Il consolidamento
mantiene timestamp iniziale e confidence massima, senza bonus numerici arbitrari;
riporta count, first_seen, last_seen, peak_timestamp e span osservato. Per accettare
un VS servono almeno 3 osservazioni fini e 0.25 s di span. Per i risultati brevi
servono almeno 2 osservazioni con PTS sorgente distinti e span >= 1/120 s: una sola
immagine ad alta similarity non basta. Se lo scan ROI a 20 FPS trova un cluster
insufficiente, viene raffinata soltanto una finestra locale ±0.15 s a 120 FPS.
Il sampler non duplica frame quando la sorgente ha FPS inferiori; 120 FPS permette
di osservare tutti i frame reali di una sorgente a circa 60 FPS. Le osservazioni
ripetute durante il raffinamento vengono deduplicate prima del consolidamento.

VS e risultati hanno requisiti di stabilità separati in HearthstoneSettings.
Le scale e le soglie del matcher sono definite nel pack, non corrette dal sampler.
La calibrazione di 73.mp4 ha motivato una griglia uniforme di scale risultato
0.9..1.1 con passo 0.025, lasciando similarity 0.92 e margine 0.04 invariati.
Questa scelta riguarda il pack locale separato, non i default delle risorse vuote.
Non è una garanzia di generalizzazione ad altri VOD.

--result-fps 0 riproduce il percorso diagnostico legacy coarse-to-fine per tutte
le classi; può perdere schermate risultato brevi e non è la configurazione finale
raccomandata. --result-refine-fps 0 disabilita solo il raffinamento dei candidati
risultato instabili. Detector/sampler iniettati privi dell’API ROI mantengono il
percorso legacy per backward compatibility. Un candidato coarse da solo non basta.

Un risultato senza VS conserva start=None; un VS senza risultato conserva end=None
e result=UNKNOWN. Risultati contraddittori vicini diventano UNKNOWN. Non si inventa
l'inizio del VOD, la fine del VOD o il precedente risultato come boundary mancante.
Una scansione senza eventi emette un avviso: non prova l'assenza di partite.

Ctrl+C termina l'analisi con codice 70, interrompendo FFmpeg e chiudendo pipe/thread.
Help/successo restituiscono 0; input, configurazione o analisi fallita restituiscono 2.
L'API solleva eccezioni strutturate e non gestisce segnali globali: solo la CLI li usa.

## Diagnosticare un mancato evento

TemplateVisualDetector espone score(frame, timestamp, event_types=...) e
score_roi(crop, timestamp, roi=..., event_types=...). Entrambi usano lo stesso
matching di detect()/detect_roi(), ma restituiscono similarity, threshold,
margine, template_id, scala, rettangolo normalizzato, best_type e accepted_type
anche per match rifiutati. Il crop deve avere esattamente le dimensioni della ROI
nel reference_size; roi è relativo al viewport, non al video completo.
result_scan_roi() restituisce l’unione delle ROI VICTORY/DEFEAT. I rettangoli di
match vengono trasformati alle coordinate normalizzate del video originale.

Per ciascun falso negativo distinguere:

- A: il sampler non osserva alcun frame utile; controllare PTS e fase di sampling.
- B: il frame esiste ma il template non copre l’aspetto, la lingua o l’animazione.
- C: il matcher emette eventi ma il consolidamento respinge count/span insufficienti.
- D: gli eventi sono corretti ma il GameAssembler li associa alla partita sbagliata.
- E: similarity/margine rifiutano un frame corretto; calibrare soltanto sul calibration set.
- F: il crop/viewport esclude o altera il banner; verificare le coordinate di riferimento.

Salvare score e frame locali attorno all’errore prima di cambiare soglie. Non
trasformare le annotazioni di validation in nuovi template o parametri di tuning.

## Cache, filesystem e prestazioni

Default Windows, salvo override runtime già presenti nell'app:

- Pack utente: %APPDATA%\Auto Cutter\Auto Cutter\gameplay\hearthstone\templates.json
- Pack iniziale: resources/gameplay/hearthstone/templates.json (sola lettura).
- Cache: %LOCALAPPDATA%\Auto Cutter\cache\gameplay\hearthstone\<hash>.json
- Report: %LOCALAPPDATA%\Auto Cutter\gameplay\reports\<hash-percorso>\gameplay_analysis.json
- Debug frame: directory debug_frames accanto al report.

--output-dir cambia i report, --template-pack sceglie il pack. Nessun dato runtime
viene scritto nella repository, nel CWD o in _MEIPASS per default.
Cache identity: path canonico, dimensione, mtime_ns e fingerprint head/tail 64 KiB,
più contenuto/config dei template, versione detector/OpenCV e settings. Il
fingerprint è un controllo leggero, non l'hash completo di un VOD di molte ore.
JSON corrotti/incoerenti sono cache miss; write tramite tmp+flush+fsync+replace.
Il video viene ricontrollato prima del cache hit e alla fine della scansione.
FFprobe è riusato nella stessa istanza finché path/size/mtime non cambiano.

La memoria dei frame è limitata da queue; i frame vengono processati e scartati.
Le finestre fini sovrapposte sono unite. Un limite alle osservazioni interrompe
configurazioni degeneri, invece di accumulare dati senza limite.
FFmpeg seleziona PTS reali, senza inventare frame tramite fps resampling; il sampler
copre VFR, offset iniziali, seek e Unicode. Interframe decoding resta necessario: selezionare pochi frame non evita di
decodificare i frame di riferimento del codec. Nel percorso denso FFmpeg ritaglia
e ridimensiona la ROI prima di inviarla a Python; OpenCV non riceve frame completi
a 20 FPS. La geometria è arrotondata prima sul reference_size, poi trasformata
tramite viewport; il matcher non normalizza di nuovo il crop come un frame intero.

Il report distingue coarse_frames, fine_frames, dense_result_frames,
result_refine_frames e result_refine_windows; registra dense_frame_size,
coarse_elapsed_seconds, fine_elapsed_seconds, dense_elapsed_seconds ed
elapsed_seconds. Misurare questi valori su VOD lunghi prima di promettere throughput.

## Ground truth e valutazione reale

Annota manualmente tutti i giochi di un intervallo, usando start=VS e end=schermata
risultato. Esempio **fittizio di formato**, non dataset realmente misurato:

    {
      "format": "recut_gameplay_ground_truth",
      "version": 1,
      "complete": true,
      "annotated_range": [0, 1800],
      "games": [
        {"start": 134.2, "end": 642.6, "result": "WIN"},
        {"start": 680.1, "end": 1213.4, "result": "LOSS"}
      ]
    }

Facoltativo video_fingerprint deve corrispondere al report, per evitare dataset
abbinati al video sbagliato. Imposta complete=false durante l'annotazione: le
metriche saranno marcate provisional. Il valutatore non annota automaticamente
le predizioni e non considera ground truth una classificazione del detector.

    python -m analysis.gameplay.hearthstone.debug_analyze "video.mp4" --ground-truth "labels.json" --tolerance 5 --output-dir "debug"

Produce anche gameplay_evaluation.json: game detection precision/recall, accuratezza
WIN/LOSS (UNKNOWN è errore sui giochi abbinati), confusion matrix, LOSS
precision/recall, falsi positivi LOSS e MAE/mediana/max error dei timestamp.
Il matching è uno a uno e ignora il risultato per evitare di gonfiare l’accuracy.
Le metriche legacy game_detection_* possono abbinare una predizione incompleta
tramite l’unico boundary disponibile. Per decidere se una partita è ricostruita
usare complete_game_detection_* e complete_matched_games: richiedono entrambi i
boundary entro tolleranza. complete_win_loss_accuracy, complete_loss_recall,
complete_loss_precision e complete_false_positive_loss_count misurano la sicurezza
sulle partite con entrambi i boundary, senza confondere un risultato isolato con
un intervallo utilizzabile.

Le metriche sugli anchor sono separate dall’assembler: vs_recall,
result_detection_recall, victory_recall e defeat_recall confrontano direttamente
gli eventi temporali con le annotazioni. result_detection_recall verifica la
presenza di un risultato entro tolleranza indipendentemente dall’etichetta;
victory_recall/defeat_recall richiedono anche la classe corretta. result_event_accuracy
misura le classi sugli anchor abbinati. vs_timestamp_error e result_timestamp_error
riportano mean_absolute_error_s, median_absolute_error_s e max_absolute_error_s.

complete_game_comparisons e event_comparisons riportano per partita riferimenti,
predizioni, errori e stato OK/FALSE_NEGATIVE/MISCLASSIFIED/PARTIAL, distinguendo
un mancato riconoscimento da un pairing errato. unmatched_events e unmatched_games
espongono separatamente le predizioni spurie. Le metriche restano provvisorie
quando complete=false, anche se tutte le partite annotate risultano corrette.
Predizioni con entrambi i boundary fuori dall'intervallo annotato o che lo
attraversano non sono valutate; quelle incomplete con anchor interno sì.
I denominatori vuoti restituiscono null, non un successo artificiale.
false_positive_loss_fraction significa LOSS errate/non abbinate / tutte le LOSS
predette; false_positive_loss_rate significa WIN annotate classificate LOSS /
tutte le WIN annotate. Sono metriche differenti, entrambe documentate nel JSON.

Protocollo suggerito: separare VOD di calibrazione e validazione; includere più
sessioni, lingue, risoluzioni, overlay, sconfitte per concede/disconnect e schermate
brevi. Misurare prima i falsi positivi LOSS e l'errore di boundary, poi il recall.
Nessuna rimozione automatica finché il set di validazione non dimostra precisione
adeguata. Valutare un piccolo modello locale soltanto dopo aver esaminato errori
reali ricorrenti e verificato che template/ROI/stabilità non bastino.

## Prima ricognizione di un VOD reale

Il file utente 73.mp4 ha permesso di preparare template locali e un riferimento
provvisorio di 14 partite. Le prime 3 sono riservate alla calibrazione; le altre
11 restano separate per la validation. I banner WIN/LOSS brevi e animati richiedono
sia sampling ROI denso sia scale coerenti e verifica temporale locale. Il pack
originale, gli esperimenti di calibrazione e il pack congelato sono conservati
separatamente; il report di validazione documenta evidenze e risultati senza
confondere annotazioni manuali con predizioni automatiche.

Dettagli e comando: [GAMEPLAY_VALIDATION_73.md](GAMEPLAY_VALIDATION_73.md).
