# Adaptive Gameplay Detector — candidato sperimentale

Stato: **NEEDS_TUNING — disattivato per default**. Il criterio «meno costo senza perdere eventi» non è superato sugli estratti reali misurati. La GUI continua a usare il detector tradizionale. Nessun commit, download di modelli, chiamata AI, nuovo run di 73 o analisi integrale del VOD da sette ore.

## 1. Pipeline reale e architettura

GameplayEditingService → hearthstone_detector → StateAwareHearthstoneAnalyzer.

Il monitor attuale legge il VOD sequenzialmente a 1 FPS: board e result matching su ogni campione, VS con cadenza 1 s. Lo stato viene utilizzato successivamente per pianificare le finestre dei risultati, senza sopprimere i matcher globali.

Il candidato AdaptiveHearthstoneAnalyzer sostituisce soltanto il monitor. Riutilizza:
- finestre VS ±2 s, refinement 6 FPS e consolidamento;
- planning state-aware e ROI risultati a 60 FPS;
- ResultDecisionPolicy, deduplicazione e GameAssembler;
- FrameSamplingService, GameplayCache e profiler esistenti.

state_aware.py estrae il corpo originale della scansione in _scan_monitor e introduce piccoli hook. Per il backend tradizionale i parametri, la versione, la chiave cache e il comportamento restano quelli precedenti. Nessuna modifica a soglie, template, WIN/LOSS, durate, Smart Boundaries, Whisper, keep policy, TimelineMapper, export, Packaging o formato progetto.

Nuovi moduli:
- analysis/gameplay/hearthstone/fingerprint.py
- analysis/gameplay/hearthstone/adaptive_policy.py
- analysis/gameplay/hearthstone/adaptive.py
- analysis/gameplay/hearthstone/adaptive_compare.py
- scripts/benchmark_adaptive_gameplay.py
- tests/test_adaptive_gameplay.py

File esistenti modificati: state_aware.py, visual/sampler.py (solo contatore seek), analysis/gameplay_editing.py (selezione backend), analysis/hero_groups/labels.py (mapping asset DK).

## 2. Game Fingerprint: segnali effettivi

Descrittori grayscale 16×20 centrati e normalizzati su piccole ROI, con controllo di leggibilità. I descrittori non sono SIFT, né probabilità di stessa partita.

| Segnale | ROI normalizzata x / y / w / h |
|---|---|
| Nostro ritratto | 0.466 / 0.707 / 0.068 / 0.128, da PortraitSettings |
| Ritratto avversario | 0.466 / 0.143 / 0.068 / 0.128 |
| Nostro Hero Power | 0.565625 / 0.692592593 / 0.08125 / 0.144444444 |
| Hero Power avversario | 0.565625 / 0.144 / 0.08125 / 0.144444444 |
| Struttura board | ROI end-turn e hero-power del profilo attuale, escludendo artwork/testo interni |

La presenza board usa il detector strutturale esistente. Crop piatti, saturi, mancanti o non finiti non dimostrano continuità. Non si usano minion, carte o differenze dell'intero frame come unico criterio. Le ROI superiori sono sperimentali e meno validate del ritratto inferiore.

Non viene letto il mana numerico: manca un reader locale validato. Il modello ammette un valore massimo opzionale con confidenza; un reset affidabile 6+ → 3 o meno richiede soltanto recovery. I test distinguono reset, riduzioni non sospette ed evidenza numerica incerta. Nessun valore di mana viene inventato dai frame.

## 3. Politica adattiva e stati

Configurazione candidata implementata, non promossa:
- ricognizione ogni 5, 10, 20 o 30 s; default sperimentale 5 s;
- board e result matcher sui campioni radi;
- VS omesso nel campione rado soltanto quando entrambi gli estremi hanno board affidabile, tutte le regioni concordano e non c'è weak result;
- stati OUT_OF_GAME, POSSIBLE_START, IN_GAME, POSSIBLE_END, UNKNOWN_RECOVERY;
- board transition, descrittori cambiati/illeggibili, weak VS/result e buchi temporali richiedono verifica dell'intero intervallo;
- overlap ±2 s, arrotondamento verso l'esterno, clipping e merge prima della lettura;
- controlli indipendenti dalla similarità: 6 s ogni 20 s;
- inizio e fine sempre verificati;
- recovery a 1 FPS con tutti i matcher, seguito dagli stessi refinement 6/60 FPS;
- copertura recovery >=65% → una scansione tradizionale completa;
- errore di ricognizione → fallback completo; cancellazione propagata senza retry.

0.90 di continuità e 65% di fallback sono parametri iniziali del router, non nuove soglie WIN/LOSS. Non derivano dai failure frozen di 73 e non sono sufficientemente validati per promuovere il backend.

Nel fallback completo gli indizi radi vengono scartati prima della ricostruzione del monitor. Si ripete la stessa traversata tradizionale, senza mescolare osservazioni VFR delle due passate. Nel percorso parziale le osservazioni vengono ordinate e deduplicate per timestamp.

## 4. Recupero e limiti

La similarità non prova continuità. Un test conserva un controesempio a passo 30 s: estremi identici, DEFEAT intermedio invisibile ai campioni e perso. Non è stata aggiunta una regola sul suo timestamp.

I controlli periodici limitano il rischio, ma non garantiscono recupero di una transizione completamente invisibile. Nessuna durata minima di partita viene introdotta.

Nel percorso parziale una scomparsa board dopo almeno due osservazioni consecutive può pianificare un risultato orfano, senza VS. Non crea uno start né assegna WIN/LOSS. Il fallback completo conserva il planning tradizionale. Missing START/END restano espliciti e il pairing usa sempre l'assembler esistente.

## 5. Shared Store, cache, DK e Hero Groups

Il candidato utilizza FrameSamplingService come adapter. Non introduce una seconda cache dei frame. Mantiene PTS reali, cancellazione dei subprocess e lo stesso sampler ROI.

GameplayCache salva il candidato in gameplay/hearthstone/adaptive, separatamente da state-aware. Sorgente, parametri del router, fingerprint version e asset dei matcher partecipano alla chiave. Cache e progetti precedenti non vengono migrati o eliminati.

Le fingerprint non sono passate a Hero Groups: non hanno la rappresentazione SIFT, la qualità o necessariamente i timestamp dei campioni attuali. Sostituire questi campioni non avrebbe equivalenza dimostrata. **Il costo Hero Groups non è risolto da questa iterazione.** Non è stato abilitato ulteriore batching, già risultato peggiore a cache fredda. Classificazione, clustering, campioni e correzioni manuali rimangono invariati.

L'asset class_refs/HERO_11bp.webp veniva ignorato perché mancava il mapping del nome. Ora viene caricato come death_knight. Un test verifica tutti gli 11 riferimenti e la lista classi completa. Il raggruppamento continua a utilizzare il nostro ritratto inferiore, mai quello avversario. Cambiare gli asset class_refs invalida soltanto la cache Hero Groups che li include nella sua identità.

## 6. Profiler e shadow

Nuove fasi: adaptive.total, reconnaissance, fingerprint, planning, recovery, routing e comparison. Registrano target richiesti, frame consegnati, confronti/chiamate matcher, finestre, recovery %, sessioni FFmpeg e sessioni con seek. L'attesa pipe non è descritta come puro tempo decoder.

In shadow il confronto è in:
docs/performance_reports/gameplay_profile_*.json → phases.adaptive.comparison.comparison

Contiene ogni START/END, timestamp, eventi mancanti/extra, giochi complete/partial, risultato, pairing, candidate recall delle finestre prima del refinement, cache hit di entrambi e wall time delle due esecuzioni. La tolleranza diagnostica di matching è 1 s; exact_event_timestamps distingue l'identità esatta.

Shadow restituisce il risultato tradizionale. Un errore del candidato viene registrato e conserva la baseline; Ctrl+C/cancellazione si propaga. Shadow esegue entrambi i detector: non è la modalità per misurare un'accelerazione della GUI.

## 7. Benchmark reali brevi

Solo estratti temporanei stream-copy dei calibration VOD 74/86. Cache detector disattivata. Nessun ASR, agent, Hero Groups, Packaging o export nei benchmark. Il tempo totale è quello del detector con refinement.

Ogni configurazione è stata misurata una volta: dati indicativi, non stime statistiche o estrapolazioni a sette ore. I timestamp sono relativi all'estratto; il mux può aggiungere un offset rispetto al taglio nominale.

### 86.mp4, estratto 1860–2010 s
Artifact: [adaptive_86_short_c6fbc7c9.json](performance_reports/adaptive_86_short_c6fbc7c9.json)

| Backend / passo | Scan s | Detector totale s | Frame monitor | Confronti globali | Sessioni FFmpeg | Fallback |
|---|---:|---:|---:|---:|---:|---:|
| Tradizionale | 3.429 | 5.565 | 151 | 5436 | 4 | — |
| Adattivo 5 s | 6.472 | 8.510 | 182 | 6552 | 5 | 100% |
| Adattivo 10 s | 6.480 | 8.552 | 167 | 6012 | 5 | 100% |
| Adattivo 20 s | 6.462 | 8.485 | 159 | 5724 | 5 | 100% |
| Adattivo 30 s | 6.454 | 8.489 | 157 | 5652 | 5 | 100% |

### 74.mp4, estratto 6000–6150 s
Artifact: [adaptive_74_short_6651185d.json](performance_reports/adaptive_74_short_6651185d.json)

| Backend / passo | Scan s | Detector totale s | Frame monitor | Confronti globali | Sessioni FFmpeg | Fallback |
|---|---:|---:|---:|---:|---:|---:|
| Tradizionale | 3.747 | 5.254 | 153 | 5502 | 2 | — |
| Adattivo 5 s | 7.399 | 8.908 | 185 | 6618 | 3 | 100% |
| Adattivo 10 s | 7.395 | 8.922 | 169 | 6066 | 3 | 100% |
| Adattivo 20 s | 7.398 | 8.933 | 162 | 5820 | 3 | 100% |
| Adattivo 30 s | 7.368 | 8.896 | 159 | 5718 | 3 | 100% |

### 74.mp4, estratto 780–900 s
Artifact: [adaptive_74_win_49deb6c1.json](performance_reports/adaptive_74_win_49deb6c1.json)

| Backend / passo | Scan s | Detector totale s | Frame monitor | Confronti globali | Sessioni FFmpeg | Fallback |
|---|---:|---:|---:|---:|---:|---:|
| Tradizionale | 2.549 | 4.083 | 121 | 4356 | 2 | — |
| Adattivo 5 s | 4.953 | 6.504 | 146 | 5250 | 3 | 100% |
| Adattivo 10 s | 4.934 | 6.480 | 134 | 4824 | 3 | 100% |
| Adattivo 20 s | 4.924 | 6.480 | 128 | 4608 | 3 | 100% |
| Adattivo 30 s | 4.904 | 6.446 | 126 | 4536 | 3 | 100% |

Tutte le configurazioni attivano il fallback al 100% su queste clip: **nessun risparmio netto** di frame/confronti. La ricognizione aggiunge una seconda decodifica. Le fingerprint costano invece circa 2–13 ms complessivi per estratto: il limite è la capacità di evitare recovery in sicurezza.

## 8. Confronto evento per evento

| Clip | Evento | Baseline s | Candidato s, tutti i passi |
|---|---|---:|---:|
| 86.mp4 @ 1860 | VS_SCREEN | 13.983 | 13.983 |
| 86.mp4 @ 1860 | DEFEAT | 122.850 | 122.850 |
| 86.mp4 @ 1860 | VS_SCREEN | 144.983 | 144.983 |
| 74.mp4 @ 6000 | DEFEAT | 93.958 | 93.958 |
| 74.mp4 @ 780 | VICTORY | 40.200 | 40.200 |

Nessun evento extra o cambio di pairing rispetto alla baseline sugli estratti. Tutti i timestamp sono identici, ottenuti però tramite fallback completo. Questo non valida l'accuratezza del percorso rado né dimostra precision/recall assoluta del 100% rispetto a nuovo ground truth.

I test sintetici coprono il percorso parziale: a 5/10/20 s preserva gli eventi dello scenario; a 30 s conserva esplicitamente il controesempio. Copertura aggiuntiva: Game↔Menu, overlay, reset mana reali/apparenti, orphan result, missing START/END, risultato di 0.1 s, luminosità, cache, errori/cancellazione e DK. Gli artifact frozen restano invariati.

## 9. Attivazione dalla GUI

Chiudere ReCut e avviarlo da PowerShell nella repository:

~~~powershell
$env:AUTO_CUTTER_GAMEPLAY_BACKEND = "adaptive"
$env:AUTO_CUTTER_ADAPTIVE_STEP_S = "5"
& .\.venv310\Scripts\python.exe main.py
~~~

Oppure confronto che mantiene i risultati tradizionali nell'editor:

~~~powershell
$env:AUTO_CUTTER_GAMEPLAY_BACKEND = "adaptive-shadow"
& .\.venv310\Scripts\python.exe main.py
~~~

Poi Analysis → Detect Games. Il primo run candidato ha cache distinta; quelli successivi possono essere cache hit. Controllare lo stato cache prima di confrontare tempi.

Per tornare subito al tradizionale, al successivo avvio:

~~~powershell
$env:AUTO_CUTTER_GAMEPLAY_BACKEND = "traditional"
Remove-Item Env:AUTO_CUTTER_ADAPTIVE_STEP_S -ErrorAction SilentlyContinue
Remove-Item Env:AUTO_CUTTER_ADAPTIVE_FULL_FALLBACK -ErrorAction SilentlyContinue
& .\.venv310\Scripts\python.exe main.py
~~~

Senza variabili si usa sempre il tradizionale. AUTO_CUTTER_ADAPTIVE_FULL_FALLBACK=1 permette di verificare il fallback esatto. Valori backend e passi non validi vengono rifiutati.

## 10. Decisione e limiti aperti

**Non promuovere il candidato.** Board incerta, crop variabili/illeggibili e transizioni producono troppo recovery. La passata rada deve ancora decodificare le dipendenze H264; con fallback completo ripete lavoro.

Serve validare meglio la continuità e il routing, senza aumentare arbitrariamente il passo o abbassare le soglie WIN/LOSS. Mana e riuso dei ritratti richiedono prove indipendenti. Non è risolta la transizione completamente invisibile.

Baseline lunga storica: monitor 669.45 s, Detect Games 1110.11 s, Hero Groups 79.29 s. Nessun nuovo benchmark lungo o miglioramento di tali valori viene dichiarato.

## 11. Quality gate

- Test adattivi mirati: 38 passed.
- Suite completa finale: 1197 passed, 0 failed, 1 skipped, 166.31 s.
- Ruff check .: passed.
- Mypy su hearthstone, gameplay_editing, labels e benchmark: passed, 24 source files.
- GUI source smoke: main.py --smoke-test, exit 0.
- git diff --check: passed.
- Audit SHA256: 32 file frozen verificati, 0 modifiche.
- Cache della suite isolata in una directory temporanea; nessuna cache/progetto precedente cancellato.

La ricognizione a 5 s evidenzia 24/31 stati UNKNOWN nella clip 86 @1860, 14/32 nella clip 74 @6000 e 21/25 nella clip WIN 74 @780. Le principali cause registrate sono board nella fascia incerta, cambiamenti dei ritratti e dei poteri/strutture, oltre ai weak result. Le tabelle mostrano il costo effettivo del fallback: non è attribuito falsamente alla sola fingerprint.

Nessun test ha richiesto un provider AI reale. I report A/B non comprendono ASR o il VOD lungo.

## 12. Diagnosi successiva su videoprova — 2026-10-12

Le sezioni precedenti descrivono v1 e restano come evidenza storica. La versione sperimentale corrente è v2: planner incrementale con isteresi e categorie STATE_UNKNOWN / IDENTITY_UNCERTAIN / TRANSITION_SUSPECTED / CONTINUITY_PLAUSIBLE; una sola consegna 1 FPS con buffer limitato per evitare la seconda decodifica; stessi refinement e assembler.

Report completo: [performance_reports/ADAPTIVE_DIAGNOSTIC.md](performance_reports/ADAPTIVE_DIAGNOSTIC.md).

Su videoprova (una misura per backend, detection cache off): tradizionale 120,330 s, adattivo 119,333 s. 9/9 eventi e timestamp identici, 4 complete + 1 UNKNOWN, nessun pairing errato. La diagnostica C da coarse + ricerche dirette dai trigger + refinement, esclusi recuperi generici, conserva anch'essa 9/9 eventi; non è un terzo run temporizzato.

Recupero dal 100% v1 al 67,73%, confronti globali -25,90%; monitor però 99,753→100,319 s. Il risparmio matching è assorbito dall'attesa dei frame. Stato: **NOT_BENEFICIAL per la promozione**, sperimentale disattivato per default. Non è dimostrata una velocizzazione affidabile, né equivalenza universale.

La soglia di promozione 65% della vecchia policy non provoca più una seconda passata nel monitor streaming: il buffer consente anche copertura locale completa dalla prima lettura. Il fallback completo rimane esplicito o su errore. AUTO_CUTTER_GAMEPLAY_BACKEND e adaptive-shadow funzionano come prima; cache v2 separata per versione. Nessuna modifica a Hero Groups, DK, Whisper, Smart Boundaries, export o Packaging in questa iterazione.

Quality gate: 50 test adattivi; 223 regressioni pertinenti; 1209 passed, 0 failed, 1 skipped nella suite completa; ruff/mypy/GUI smoke/diff check passati; 32 file frozen invariati. Nessun commit, chiamata AI o analisi del VOD lungo.

## 13. Scheduler V2 cost-aware (lattice + accesso in-process) — 2026-10-13

Report completo: [performance_reports/ADAPTIVE_SCHEDULER_RESULTS.md](performance_reports/ADAPTIVE_SCHEDULER_RESULTS.md).

La V2 (`adaptive-v2`) usa ora:
- un decoder in-process per worker, pixel/PTS identico al percorso FFmpeg, invece di un processo per seek;
- la gerarchia 30 s snappata su un lattice di 2 s;
- righe 1 FPS reali solo dove due campioni del lattice differiscono negli ingressi del planner, e righe imputate altrove.

Su videoprova:
- detection 44,10 s, contro 120,33 s del tradizionale storico e 212,36 s della V2 precedente;
- 15 processi FFmpeg;
- fallback sull'8,5% della timeline;
- 9/9 eventi con errore 0 e pairing corretto.

Due finestre del planner, senza risultati, differiscono per cali isolati di 1 s (ipotesi documentata). Il tradizionale resta il default. Lo scheduler precedente si richiama con `AUTO_CUTTER_ADAPTIVE_SCHEDULER=legacy`.
