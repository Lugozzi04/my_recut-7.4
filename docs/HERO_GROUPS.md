# Player classes — local Gameplay Editing

Funzione aggiuntiva sulle partite già rilevate. Riconoscimento locale OpenCV, senza modelli cloud, addestramento o download. Detector Gameplay, WIN/LOSS, Smart Boundaries e renderer restano quelli esistenti.

## Uso GUI — versione 8

1. Salvare il progetto e riavviare ReCut per caricare il codice aggiornato, quindi riaprire il progetto.
2. Premere **Detect games**: subito dopo detection e boundary, lo stesso worker analizza automaticamente le classi. Non esiste più un pulsante Analyze classes separato; Cancel interrompe anche questa fase. Il pannello Player classes mostra i risultati. Per un vecchio progetto senza classi basta Detect games again, che riusa le cache disponibili.
3. La cartella **class_refs** del progetto viene usata automaticamente, indipendentemente dalla directory di avvio. Il pulsante Class refs è stato rimosso. Nell'eseguibile si usa una cartella class_refs accanto all'app se presente, altrimenti la copia inclusa nel bundle. Nessun asset viene scaricato.
4. Le righe mostrano ritratto, risultato originale, classe, nome e similarità/campioni. Le classi sono Hunter, Mage, Rogue, ecc.; le lettere A/B/C dei vecchi progetti vengono migrate al ricalcolo. La similarità non è una probabilità calibrata. **≈** segnala stime deboli o conflitti; il tooltip indica sorgente, stato e margin.
5. Nella scheda Analysis, doppio clic su **Class** per correggere scegliendo una classe. Nella scheda Export, doppio clic su **Name** per un nome libero. Gli override manuali hanno priorità e sono salvati nel progetto.
6. Aprire la scheda **Export → Videos by class**, spuntare le classi e premere **Export selected groups separately…**. Compaiono soltanto classi con WIN complete ancora esportabili; il conteggio WINs kept considera il montaggio corrente. Ogni classe genera un progetto/output distinto, usando gli attuali keep e boundary. Senza Gameplay Editing la sezione non compare e Export video mantiene il normale comportamento.
7. Salvare il progetto .autocutter per conservare classi, correzioni, nomi e selezione. La normale funzione Export mantiene il comportamento esistente.

Le isole WIN della timeline assumono il colore della classe su **video, audio e righello**. Non vengono spostate o ricalcolate. Colori fissi: Druid arancione, Hunter verde chiaro, Mage azzurro, Paladin rosa, Priest bianco, Rogue giallo, Shaman blu, Warlock lilla, Warrior beige, Demon Hunter viola, Death Knight rosso. Il testo si adatta per essere leggibile.

Le correzioni aggiornano subito i colori. Una classe senza frame utilizzabili rimane **Da assegnare**, correggibile manualmente: non si inventa una classe in assenza di dati. Un eventuale risultato **UNKNOWN** nella colonna Result è distinto dalla classe e continua a rappresentare la partita incompleta del detector originale.

## CLI

Serve un progetto con Gameplay Editing già eseguito. Detection, ASR e boundary analysis non vengono rilanciati.

~~~powershell
python main.py groups "VOD.autocutter" --analyze
python main.py groups "VOD.autocutter"
python main.py groups "VOD.autocutter" --json
python main.py groups "VOD.autocutter" --export hunter --output-dir ".\outputs"
python main.py groups "VOD.autocutter" --export selected --output-dir ".\outputs"
python main.py groups --help
~~~

--analyze usa la cache valida e salva atomicamente il progetto preservando le correzioni. Senza --analyze legge solo il progetto. --class-refs DIR resta un override CLI opzionale per compatibilità; normalmente non serve. --export selected rispetta le spunte salvate. --json separa il risultato su stdout dal progress su stderr. Ctrl+C cancella sampling/render con i token esistenti; gli export interrotti restano riprendibili.

## Riconoscimento e assegnazione

Implementazione UI-independent in analysis/hero_groups; algoritmo **versione 8**.

- Circa 10 richieste per partita tramite FFmpegFrameSampler esistente. Finestra: start + min(60 s, 25% della durata) fino a min(start + 120 s, end - margine), adattata alle partite corte. Nessuna analisi completa del VOD.
- ROI ritratto **giocatore in basso**: x=.466, y=.707, w=.068, h=.128. ROI potere eroe: .565625, .692592593, .08125, .144444444. Coordinate normalizzate, layout Hearthstone 16:9; l'avversario non entra nel riconoscimento.
- Gate BOARD_PRESENT riutilizzato in sola lettura dal profilo frozen, soglia .38. Crop piatti, scuri/saturi o molto sfocati vengono scartati. Una reference locale rifiuta il pulsante italiano di conferma mulligan che può coprire il ritratto; provenance in assets/confirmation.provenance.json.
- Ritratto: grayscale normalizzato 48×56, gradienti 24×28, istogramma HSV e confronto geometrico SIFT locale limitato all'interno. Soglie ritratto invariate: struttura .88, colore .80, almeno 3 frame e consenso 60%. Niente singolo hash o colore medio.
- Potere eroe: le carte verticali standard in class_refs vengono ritagliate automaticamente sull'illustrazione, escludendo testo e mana; si accettano anche crop delle icone. Ricerca locale a più scale, grayscale e feature percettive. I nomi file definiscono la classe; dh/dk sono alias. I 10 riferimenti attuali non includono Death Knight.
- Per classe si aggregano i migliori 5 score Hero Power. Un anchor affidabile richiede score >=.60, margin >=.08, almeno 3 frame e almeno 60% di voti concordi. La precedente soglia .92 era eccessiva per carte illustrate confrontate con icone in-game: sul campione gli score reali sono .70–.81 con margin .20–.24. È una soglia pratica del nuovo confronto, non una precisione certificata su un dataset ampio.
- Le skin con firma robusta e Hero Power affidabile diventano riferimenti **solo all'interno del VOD**. Un'altra partita può essere riconosciuta confrontando almeno 3 suoi campioni concordi con tali riferimenti, anche se l'animazione impedisce una firma unica per tutta la partita.
- Per ogni classe si prende il maggiore tra score aggregato del potere eroe e score della skin. Vince la classe col valore maggiore. Evidenza sotto le condizioni di affidabilità viene comunque proposta come **estimated (≈)**, su richiesta di preferire la migliore corrispondenza a unknown; contraddizioni tra i segnali sono marcate **conflict (≈)**. Non vengono nascoste come riconoscimenti certi.
- I riferimenti di skin derivano soltanto da Hero Power affidabili, non da stime propagate: un campione ambiguo non crea una catena di nuovi anchor. I confronti sono deterministici, senza aggiornamenti dipendenti dall'ordine di elaborazione.

Con i riferimenti disponibili il raggruppamento usa ID di classe canonici, per esempio hunter. In loro assenza resta disponibile il raggruppamento per ritratto senza inventare nomi di classe; la GUI consente l'assegnazione manuale. Non esiste memoria globale tra VOD.

## Persistenza, cache e output

Formato .autocutter v2 esteso additivamente: gameplay_report conserva partite e boundary originali; gameplay_grouping contiene campioni, score, classe automatica, overrides per game ID, names, selected, gruppi e miniature portabili. I progetti precedenti continuano a caricarsi. Alla migrazione A/B/C si conservano correzioni, nomi e selezione.

Cache in **cache_root()/hero_groups**, writable app data, mai cwd/_MEIPASS. Chiave: sorgente/fingerprint/size/mtime, versione algoritmo, parametri, game IDs/intervalli/risultati, timestamp campioni, profilo board/reference di qualità e immagini Hero Power. PTS reali e hash dei crop sono registrati. JSON corrotto viene ignorato. Gli override del progetto sono separati dalla cache automatica. Versione 8 invalida il vecchio risultato una volta; un secondo export non ripete detection, ASR o boundary analysis.

Ogni output interseca **le isole correnti** del gruppo con i **final keeps esistenti**, mantenendo ordine cronologico, tagli Classic/manuali e boundary corretti. La policy esistente conserva WIN complete. Project ID, job ID, directory e output sono separati per video. Nomi manuali non diventano filesystem paths. Progetti portabili e salvataggi atomici.

Renderer condiviso: PipelineExportService → PipelineProjectExporter → ExportWorker, con validator, partial files, locking, promozione e sidecar esistenti. Aprire il progetto figlio per Packaging/titoli/thumbnail specifici. Nessuna chiamata Packaging durante riconoscimento/export delle classi. Nomi anonimi non vengono trasformati in classi inventate nei prompt.

## Verifica attuale — versione 8

Evidenze: [videoprova_classes_v8.json](hero_group_validation/videoprova_classes_v8.json), [contact sheet](hero_group_validation/videoprova_classes_v8.jpg), [pannello](hero_group_validation/classes_panel_v8.png), [colori timeline](hero_group_validation/class_timeline_preview_v8.png).

Cinque partite già rilevate, 50 richieste campioni, **7.547 s** per campionamento + riconoscimento; nessuna detection/ASR/full-video analysis o render VOD. Il preview timeline usa gli anchor già salvati solo per verificare il disegno; non modifica gli Smart Boundaries del progetto utente.

| Partita | Risultato originale | Classe | Similarità classe | Supporto | Segnale vincente | Score HP | Margin HP |
|---|---|---|---:|---:|---|---:|---:|
| 1 | LOSS | Druid | .743 | 8 | Hero Power | .743 | .242 |
| 2 | WIN | Hunter | 1.000 | 6 | Ritratto | .722 | .208 |
| 3 | LOSS | Hunter | 1.000 | 3 | Ritratto | .707 | .198 |
| 4 | WIN | Paladin | .814 | 10 | Hero Power | .814 | .208 |
| 5 | UNKNOWN / partial | Hunter | .990 | 3 | Ritratto | .703 | .200 |

**5/5 classi assegnate**, nessuna classe unknown nel campione. Hunter: 3 partite / 1 WIN; Paladin: 1 / 1; Druid: 1 / 0. La quinta partita resta UNKNOWN nel risultato perché incompleta; non è una classe sconosciuta.

Diagnosi della vecchia partita 3 unknown: **7 crop validi, massimo sottoinsieme interamente compatibile di 4, necessari 5** (ceil(.60×7)). Mancava un campione concorde; non era semplicemente uno score appena sotto soglia. Il nuovo confronto con skin e Hero Power recupera Hunter senza abbassare le soglie del ritratto.

Un punteggio 1.000 qui indica forte similarità del matcher, non certezza statistica. Questa verifica su cinque partite non misura precision/recall di produzione. Animazioni, overlay, zoom e poteri modificati possono ancora produrre errori: rivedere ≈ e correggere la classe prima di esportare. Il gate board non certifica tutte le occlusioni. Il reference pack attuale ha 10 classi; per Death Knight serve un riferimento locale o un override manuale.

## Test finali — versione 8

- Verifica prima del collegamento automatico: test mirati hero classes/groups, export reale breve, CLI/GUI, timeline e integrazione Gameplay: **71 passed** (5.91 s).
- Full pytest: **1109 passed, 1 failed, 1 skipped** (143.33 s). Unico failure: tests/test_youtube_packaging_gen.py::test_rerender_reuses_generated_images_without_any_call. È lo stesso failure preesistente già isolato nella verifica versione 7: il rerender richiama il fake generator. Non è stato modificato codice relativo a questo problema, fuori ambito. Nessun provider AI reale chiamato.
- ruff check .: **PASS**.
- Gate mypy CI con nuovi moduli condivisi/pannello: **75 source files PASS**.
- python main.py --smoke-test: **PASS**.
- Nessuna modifica ai file del detector frozen in analysis/gameplay o resources/gameplay. Nessun run completo sui VOD 73/74/86 e nessun render di VOD lungo.
- Nessun build PyInstaller eseguito: spec aggiornato per includere class_refs e la reference di leggibilità. Nessun commit.

## Verifiche precedenti — versione 7, conservate come evidenza storica

I paragrafi seguenti descrivono il comportamento prima della nuova assegnazione e non sono le istruzioni attuali.

## Verifica locale e limiti

Evidenze: docs/hero_group_validation/videoprova.json e videoprova.jpg; samples_initial.jpg/json documentano la diagnosi iniziale. Cinque partite già rilevate, 50 richieste di frame, circa **7 s** di campionamento + riconoscimento, senza full-video analysis/render:

| Partita | Risultato originale | Gruppo | Campioni concordi | Similarità |
|---|---|---|---:|---:|
| 1 | LOSS | A | 8 | .930 |
| 2 | WIN | B | 5 | .971 |
| 3 | LOSS | unknown | 0 | 0 |
| 4 | WIN | C | 10 | .929 |
| 5 | UNKNOWN / partial | B | 3 | .949 |

Revisione visiva: 2 e 5 mostrano la stessa skin; anche 3 sembra appartenere a B ma resta unknown per insufficiente consenso conservativo. Nessun accorpamento visivamente scorretto nei quattro assegnati. Questo piccolo controllo NON misura precision/recall di produzione. Nessun VOD 73/86 rilanciato e nessuna modifica ad asset/test frozen del detector.

Limiti: skin animate/effetti, zoom, tooltip persistenti, overlay e layout fuori profilo possono ridurre recall; il gate board non certifica semanticamente ogni occlusione. Il filtro mulligan è italiano, non una classificazione universale di tutti i menu. Hero Power: test sintetici passati; trovata anche la cartella locale class_refs con 10 carte complete. Su un frame archivio Hunter il confronto locale sceglie hunter come miglior candidato (.7134 vs .4972 secondo), ma resta sotto .92 e quindi si astiene. Non è stata abbassata la soglia né inventata una classe. Ritagli delle icone direttamente in-game o riferimenti meglio allineati sono ancora consigliati per etichettatura automatica affidabile. Serve validazione su più VOD/skin prima di affidarsi al grouping senza revisione. Correzione manuale e unknown sono parte della funzione.

Test finali della feature: **31 passed** (31 nuovi test), incluso export reale di due video da fixture di 3 s con audio/video, durata e sidecar validati. Coperti luminosità, skin diverse, bordo condiviso, più frame, A–B–A, bridge ambiguity, campioni illeggibili, Hero Power opzionale, cache/invalidation/corruzione, override persistenti, gruppi senza WIN, split/ordine/boundary manuali, nomi ostili, output isolati, GUI/QThread, CLI e cancellazione.

Full pytest finale: **1093 passed, 1 failed, 1 skipped** (166.04 s). Il precedente run era 1085 passed / 1 failed / 1 skipped; gli otto ulteriori casi sono ora inclusi nella suite completa. Unico failure: test_youtube_packaging_gen.py::test_rerender_reuses_generated_images_without_any_call. Riprodotto in un processo separato togliendo in memoria tutte le nuove aggiunte ai prompt Packaging: è un problema preesistente del rerender che richiama il fake generator; non è stato modificato perché fuori ambito. Nessun provider AI reale chiamato nei test.

Un mock preesistente in test_gameplay_pipeline.py è stato aggiornato alla firma source_range già presente nel servizio, con asserzione (0,10), senza modificare produzione/algoritmi.

ruff check .: PASS. Gate mypy CI esteso a hero_groups, nuovo pannello e moduli condivisi modificati: **73 source files PASS**. Il check ulteriore diretto su tutta MainWindow/layout legacy non è pulito (errori Qt enum/mixin preesistenti, fuori gate CI), non riscritto in questo intervento. python main.py --smoke-test: PASS. Nessun nuovo build PyInstaller eseguito; spec aggiornato soltanto per includere la piccola reference di leggibilità. Nessun commit.


## File di questo intervento

Nuovi: analysis/hero_groups/{__init__,recognition,classes,labels,projects,quality,contact_sheet,packaging}.py, reference e provenance in assets/, automation/hero_groups_cli.py, ui/hero_groups.py, tests/test_hero_classes.py e tests/test_hero_groups*.py, questo documento ed evidenze in docs/hero_group_validation/.

Hook incrementali: core/track_state.py, core/project.py, ui/main_window.py, ui/gameplay_editing_ui.py, ui/layout_main_page.py, widgets/timeline.py, automation/cli.py, analysis/packaging/pipeline.py e pipeline_v3.py (contesto Packaging), build/AutoCutter.spec (asset qualità e class_refs), docs/CLI.md. Aggiornato il solo mock source_range in tests/test_gameplay_pipeline.py. Gli altri cambiamenti preesistenti della working tree sono preservati. Nessun commit.

## Collegamento automatico a Detect games

GameplayEditWorker esegue il riconoscimento classi dopo GameplayEditingService, nello stesso thread e con lo stesso cancellation token. La progress bar riserva gli ultimi 10% alle classi. Il risultato comprende il grouping, applicato a tutti i segmenti della stessa sorgente e persistito con il report. Gli override precedenti sono passati come snapshot e conservati. La fase viene eseguita anche se non esistono WIN esportabili. Un errore di riconoscimento classe viene segnalato senza perdere partite/keep rilevati; una cancellazione impedisce di applicare risultati parziali.

Tre regressioni coperte: avvio automatico anche senza WIN e snapshot override; errore classi con keep conservati; cancellazione durante classi senza risultati parziali.

Verifica dopo questo collegamento: **74 test mirati passed** (5.83 s), ruff check . PASS, mypy sui due moduli UI modificati PASS, GUI smoke PASS. Nessun nuovo run completo VOD o render lungo; suite completa precedente invariata come evidenza, non rieseguita per questa piccola modifica.

## Export per classe e rimozione manuale delle WIN

La tabella di revisione dei ritratti resta in Analysis; selezione classi, conteggi delle WIN effettivamente conservate, pulsante export e cancellazione degli export sono nella sezione Export → Videos by class. La selezione viene derivata dalle isole correnti intersecate con i final keeps: classi senza WIN, partite rimosse e WIN interamente tagliate non producono scelte vuote. Le selezioni nascoste sono conservate nel progetto per riapparire dopo Undo, ma non vengono passate al renderer.

Click destro dentro una WIN su video/audio/cuts aggiunge **Remove this WIN from the edit**, oltre alle normali voci per tagli, split, duplicazione e rimozione video. Fuori dalle WIN il menu resta quello normale. L'azione rimuove il keep e gli eventuali frammenti della stessa partita nei segmenti della stessa sorgente, senza alterare risultati/detection. Vengono ricostruiti i gameplay cuts tramite il complemento esistente; i vecchi Restore-cut dentro quella partita non possono reintrodurla. Gli altri tagli manuali restano intatti. Ctrl+Z ripristina il montaggio; Reset all boundaries ripristina anche le isole rilevate originali.

I keep vuoti in modalità keep sono espliciti: il planner non recupera più le isole dal report originale dopo la rimozione dell'ultima WIN. Restano valide le fallback dei vecchi progetti CLI senza keep locali. Persistenza e concatenazione cronologica usano il formato e renderer condivisi esistenti; nessuna nuova pipeline di export o analisi.

Verifica: **85 test mirati passed** (8.89 s), inclusi nove nuovi scenari menu/rimozione/selection/persistenza e quattro varianti di export reale su fixture di 3 s (classi nominate/legacy, con/senza prima WIN rimossa). Ruff PASS, mypy 75 moduli PASS, GUI smoke PASS. Controllo Qt offscreen dei due pannelli in [Export](hero_group_validation/export_classes_panel.png) e [Analysis](hero_group_validation/analysis_classes_review.png), senza decodificare VOD. La suite completa precedente non è stata rieseguita; nessun run VOD lungo, modifica al detector frozen, dipendenza aggiunta o commit.


## Covers per exported video

Export → Videos by class now offers **Generate a cover for each video**. Each output uses its own saved edit and Packaging folder; choose it in Thumbnails to generate later or review/confirm its cover. See [PER_OUTPUT_PACKAGING.md](PER_OUTPUT_PACKAGING.md) for the pipeline, cache and recovery details.
