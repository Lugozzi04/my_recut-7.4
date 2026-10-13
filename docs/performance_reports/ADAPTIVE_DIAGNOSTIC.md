# Adaptive Detector — diagnosi del fallback

2026-10-12. **NOT_BENEFICIAL: sperimentale, tradizionale ancora default.**

Il candidato v2 conserva tutti gli eventi e i timestamp di videoprova e riduce i confronti globali del 25,90%, ma non dimostra un vantaggio temporale utile: 120,330 s contro 119,333 s, una misura per backend. Lo 0,83% è troppo piccolo per distinguere beneficio, rumore e overhead diagnostico. Nessuna promozione automatica.

## Artifact e protocollo

Sorgente: videoprova.mp4, 3749,999667 s, 1920×1080. Una baseline integrale e un run adattivo integrale, dopo finestre mirate. Solo detection video; nessun Classic, ASR, AI, Hero Groups, Packaging, render o nuovo modello. Nessun run di 73/74/86 o del VOD da sette ore.

Cache detection disattivata. Probe cache attiva; cache filesystem del sistema operativo non svuotata. Non si tratta di un confronto disco fisico freddo. I tempi misurano detector.analyze, escluso caricamento iniziale degli asset. La baseline raccoglie anche 750 fingerprint dai frame già consegnati per diagnosticare la vecchia policy: overhead non cronometrato separatamente, ulteriore ragione per non interpretare il delta di un secondo.

- [adaptive_diagnostic_reference.json](adaptive_diagnostic_reference.json): risultato tradizionale, profiler e replay della vecchia policy su un campione ogni cinque frame della baseline. Questo replay non è un nuovo benchmark temporizzato del vecchio adattivo.
- [adaptive_diagnostic_old_policy.json](adaptive_diagnostic_old_policy.json): copertura e cause precedenti ricostruite dal replay.
- [adaptive_diagnostic_candidate.json](adaptive_diagnostic_candidate.json): risultato v2, profiler, configurazione/hash dei moduli, tracce, confronto A/B e diagnostica autonoma C.
- [adaptive_diagnostic_clip_loss_vs.json](adaptive_diagnostic_clip_loss_vs.json): primo esperimento 1250–1430 s; l'adattivo recupera anche un END orfano reale che la baseline della clip iniziata a partita avviata perde.
- [adaptive_diagnostic_clip_win_vs.json](adaptive_diagnostic_clip_win_vs.json): esperimento 1730–1880 s prima del restringimento delle finestre dense.
- [adaptive_diagnostic_clip_narrow.json](adaptive_diagnostic_clip_narrow.json): stessa clip dopo il restringimento; stesso WIN e VS, timestamp identici.
- Ground truth separata: ../gameplay_validation/videoprova/ground_truth_manual.json, invariata.

I JSON non contengono frame, transcript, credenziali o percorsi personali assoluti. Nessun hash completo del VOD eseguito. Gli hash implementation_sha256 del candidato identificano i quattro moduli effettivamente misurati.

## 1. Cause del fallback precedente

La vecchia continuity usava il minimo delle similarità: una sola regione animata/diversa/illeggibile richiedeva recupero. L'identità incerta veniva confusa con board sconosciuta. Anche una variazione board 0,39→0,37 era una transizione, senza isteresi. Uno stato precedente UNKNOWN/POSSIBLE estendeva il recupero all'intervallo successivo, anche se ormai chiaro.

Sul replay di 750 campioni: 555 UNKNOWN_RECOVERY, 14 IN_GAME, 892 richieste comprese guardie e follow-up. Copertura prima della promozione: **3622,000 s / 96,59%**; superata la soglia di costo 65%, veniva riletta tutta la sorgente (**100%**) dopo la ricognizione.

| Causa precedente | Campioni | Copertura attribuibile, s |
|---|---:|---:|
| Board nel dead band | 271 | 1649 |
| Ritratto avversario cambiato | 274 | 1562 |
| Ritratto giocatore cambiato | 192 | 1164 |
| Board transition | 101 | 741 |
| Potere giocatore cambiato | 96 | 688 |
| Struttura end-turn / hero-power cambiata | 69 / 68 | 533 / 504 |
| Potere avversario cambiato | 63 | 455 |
| Ritratto / struttura illeggibile | 3 / 3 | 23 / 23 |
| Weak START / risultato | 7 / 2 | 55 / 18 |
| Follow-up dello stato precedente | — | 369 |
| Guardie periodiche | — | 1122 |

Le coperture per causa si sovrappongono e **non si sommano**. Sono secondi sorgente richiesti, non tempo CPU. Un cambiamento del descrittore non è un nuovo game dimostrato.

## 2. Modifiche implementate

### Stato e identità separati

- STATE_UNKNOWN: board non ancora determinata o buco anomalo nel campionamento.
- IDENTITY_UNCERTAIN: board presente secondo isteresi, ma identità insufficiente.
- TRANSITION_SUSPECTED: transizione board definita, weak VS/result, entrambi i ritratti cambiati o reset mana affidabile fornito.
- CONTINUITY_PLAUSIBLE: sostegno indipendente per giocatore, avversario e struttura; oppure stato fuori partita stabile.

OUT_OF_GAME/board_present=false distingue sempre i campioni fuori partita. La continuità plausibile dello stato non prova identità della partita.

Nel dead band 0,20–0,38 rimane l'ultimo stato board definito osservato. Un ritratto coperto o potere animato non rende la board UNKNOWN se rimangono sostegni affidabili per entrambi i lati e una struttura. Se mancano, il gap è comunque controllato a 1 FPS. Due ritratti cambiati sono un sospetto, mai START/END.

Il weak result usa il fronte di salita. L'incertezza non trascina automaticamente il gap successivo.

Nessuna soglia numerica nuova: continuità 0,90; board high/low 0,38/0,20; weak result 0,65 e margin 0,04; VS weak 0,80. Soglie, ROI, template e classificazione/refinement WIN/LOSS invariati. Non viene letto mana numerico: manca un reader validato. Il campo opzionale resta un sospetto; niente OCR/ML.

### Un solo decoding del monitor

La prima versione rileggeva il video dopo la ricognizione. Il v2 consegna sequenzialmente gli stessi campioni 1 FPS, trattenendoli in un buffer limitato:

1. Ogni 5 s calcola gli indizi coarse.
2. Il planner incrementale decide quali gap controllare a 1 FPS.
3. Il matching tradizionale usa i frame ancora nel buffer, senza seek o nuova lettura.
4. I frame non richiesti vengono scartati.
5. Restano overlap ±2 s, guardie 6 s ogni 20 s, inizio/fine e recupero delle zone incerte.

Massimo misurato: **14 frame nel buffer**. Nessuna sequenza completa in RAM, nuova cache persistente o modifica a FrameSamplingService.

La promozione al full scan al 65% era una scelta di costo: nel percorso streaming non serve, perché anche copertura locale 100% usa la prima lettura. Il fallback completo resta su richiesta esplicita o errore e richiama esattamente il monitor tradizionale. Cancellazione senza retry.

Un campione coarse su cui VS era stato omesso viene ora effettivamente controllato per VS quando rientra nel recupero. Versione/chiave v2 impedisce hit sulla vecchia cache adattiva; cache tradizionali e progetti rimangono compatibili.

### Finestre dense

Il primo esperimento locale apriva 60 FPS su tutto il gap coarse: nella clip WIN passava da 7 a 14 s senza aggiungere risultati. Ora il drop viene prima localizzato dai campioni 1 FPS nel buffer, poi usa gli stessi refinement 6/60 FPS. Mantiene END orfani, missing boundary, ResultDecisionPolicy, deduplication e GameAssembler.

Nessuna modifica a Whisper, Smart Boundaries, Hero Groups/DK, keep policy, export o Packaging.

## 3. Candidato C prima del fallback generico

Per evitare una terza passata integrale, C è ricostruito da:

- campioni coarse;
- ricerche locali dirette da board transition, weak VS/result, cambiamento simultaneo dei ritratti o mana affidabile;
- refinement limitati alle finestre aperte da queste evidenze.

**Esclude i campioni aggiuntivi presenti soltanto per guardie periodiche, inizio/fine o incertezza generica.** Le osservazioni vengono filtrate e nuovamente consolidate/classificate/assemblate: non è un filtro degli eventi finali B.

Campi precisi:
- result.metadata.adaptive.autonomous_trigger_intervals
- result.metadata.adaptive.autonomous_vs_windows / autonomous_result_windows
- result.metadata.adaptive.autonomous.events / games
- autonomous_comparison
- fallback_only_events
- autonomous_missing_diagnosis

C trova **9/9 eventi**, 5 VS, 2 VICTORY, 2 DEFEAT, stessi timestamp e pairing. **Zero eventi recuperati esclusivamente dal fallback generico; zero mancanti da diagnosticare in questo run.** Non tutti i banner sono visibili coarse: i trigger aprono refinement locali.

Non significa che le guardie siano inutili: i test mantengono START trovati soltanto dalla guardia periodica e una transizione invisibile a passo 30 s. C non ha un wall-clock autonomo: riusa decoding/refinement B, quindi non viene stimato un tempo separato.

## 4. Copertura nuova

| Categoria coarse | Campioni |
|---|---:|
| CONTINUITY_PLAUSIBLE, inclusi stati fuori partita | 404 |
| TRANSITION_SUSPECTED | 275 |
| IDENTITY_UNCERTAIN | 71 |
| STATE_UNKNOWN | 0 |

Zero STATE_UNKNOWN non prova board perfetta: il primo campione dà assenza definita e l'isteresi mantiene lo stato osservato nel dead band.

535 richieste unite in **119 intervalli**; copertura di recupero **2539,999667 s / 67,73%**. Questi intervalli non sono 119 processi FFmpeg: usano la singola sessione monitor. Ricerche dirette dai trigger C: **1631 s / 43,49%**; copertura aggiuntiva esclusiva di guardie/incertezza: **909 s / 24,24%**.

| Causa nuova | Campioni | Copertura attribuibile sovrapposta, s |
|---|---:|---:|
| Board transition definita | 19 | 171 |
| Ritratto avversario / giocatore cambiato nei gap da verificare | 295 / 279 | 1711 / 1635 |
| Identity uncertain | 76 | 532 |
| Potere giocatore / avversario cambiato | 171 / 119 | 1143 / 827 |
| Struttura hero-power / end-turn cambiata | 155 / 100 | 1055 / 772 |
| Weak START / risultato | 7 / 2 | 55 / 18 |
| Guardie periodiche | — | 1122 |

Le cause possono coesistere: non tutte sono trigger indipendenti. 76 occorrenze identity_uncertain contro 71 categorie finali derivano da campioni dove prevale un altro trigger. Le 275 transizioni sospette non sono nuove partite: animazioni/overlay modificano ancora entrambi i descrittori. È il limite principale del router.

## 5. Benchmark A/B

| Misura | A tradizionale | B adattivo v2 |
|---|---:|---:|
| Detection complessiva | 120,330 s | 119,333 s |
| Monitor globale | 99,753 s | 100,319 s |
| Attesa/consegna frame nel monitor | 50,573 s | 64,286 s |
| Preparazione immagini monitor | 1,355 s | 0,601 s |
| VS matching globale | 15,304 s | 10,624 s |
| Board matching globale | 15,720 s | 11,065 s |
| WIN/LOSS matching globale | 16,285 s | 11,877 s |
| Ricognizione coarse, matching incluso | — | 9,513 s |
| Fingerprint, interna alla ricognizione | — | 0,453 s |
| Planning incrementale | — | 0,143 s |
| Matching locale dal buffer, trigger e fallback insieme | — | 25,714 s |
| VS refinement | 2,136 s | 2,102 s |
| Result refinement | 18,359 s | 16,845 s |

Le fasi interne **non si sommano** al monitor/totale. Ricognizione e recupero contengono i matcher; il decoding/consegna è condiviso. Il tempo del fallback generico escluso quello diretto dai trigger non è cronometrato separatamente: è disponibile la copertura esclusiva, non una stima di CPU.

I tre matcher globali scendono da 47,309 a 33,566 s: 13,74 s risparmiati. L'attesa cresce di circa 13,71 s. È compatibile con il produttore FFmpeg/consegna come limite quando il consumer OpenCV diventa più leggero: le operazioni si sovrappongono, quindi risparmiare matching non riduce necessariamente il wall-clock.

**Attesa non significa solo decoding**: include disponibilità, lettura/trasporto e accodamento. CPU interna FFmpeg, I/O disco e scheduling non sono misurati separatamente. Non si può concludere che FFmpeg sia diventato più lento di 13 s.

| Contatore | A | B |
|---|---:|---:|
| Frame monitor consegnati | 3750 | 3750 |
| Campioni con fingerprint | 750 diagnostici | 750 |
| Frame con board/result matching | 3750 | 2792 |
| Chiamate VS globali | 3750 | 2713 |
| Confronti board globali | 22500 | 16752 |
| Confronti result globali | 90000 | 67008 |
| Confronti VS globali | 22500 | 16278 |
| Confronti globali totali | 135000 | 100038 |
| Finestre VS / frame fine | 5 / 266 | 5 / 266 |
| Finestre result / durata sorgente | 11 / 81 s | 10 / 74 s |
| Frame ROI risultati | 4860 | 4440 |
| Frame complessivi consegnati | 8876 | 8456 |
| Sessioni FFmpeg / con seek | 17 / 16 | 16 / 15 |
| Sessioni solo monitor | 1 | 1 |

Evitate 958 chiamate board e result e 1037 VS: **34962 confronti globali, 25,90%**. 2540 frame riutilizzati per matching locale; 1210 consegnati ma fuori dalle finestre, di cui 252 già analizzati coarse. Non sono 1210 frame totalmente senza matching.

Il conteggio riguarda frame consegnati dal sampler, **non quelli interni decodificati dal codec**. La passata H264 attraversa ancora tutto il video.

## 6. Equivalenza eventi e pairing

| Evento | A, s | B, s | C senza fallback generico, s |
|---|---:|---:|---:|
| VS #1 | 630,583 | 630,583 | 630,583 |
| DEFEAT #1 | 1301,083 | 1301,083 | 1301,083 |
| VS #2 | 1359,450 | 1359,450 | 1359,450 |
| VICTORY #2 | 1785,983 | 1785,983 | 1785,983 |
| VS #3 | 1819,750 | 1819,750 | 1819,750 |
| DEFEAT #3 | 2791,983 | 2791,983 | 2791,983 |
| VS #4 | 2863,750 | 2863,750 | 2863,750 |
| VICTORY #4 | 3525,500 | 3525,500 | 3525,500 |
| VS #5 | 3628,933 | 3628,933 | 3628,933 |

A/B/C: 5 partite, 4 complete, LOSS/WIN/LOSS/WIN e ultima start-only UNKNOWN. Nessun confine inventato, evento perso/extra, falsa LOSS o errore di pairing rispetto alla baseline. Candidate window recall VS/result: 100%. Delta timestamp medio/massimo: **0 s**, non soltanto entro la tolleranza di confronto 1 s.

Rispetto alle quattro partite complete del ground truth manuale: START MAE 0,34175 s, mediana 0,3335 s, massimo 0,450 s; result MAE 0,01275 s, mediana/massimo circa 0,017 s. L'annotazione ha incertezza 0,5 s allo START e 0,33 s al risultato: non è una misura umana al millisecondo. Il quinto VS esterno al range annotato rimane incompleto come previsto.

## 7. Test

- Adattivi: **50 passed**.
- Regressioni adaptive/state-aware/result decision/assembler/sampler/shared analysis/classi: **223 passed**.
- Suite completa, cache test isolata: **1209 passed, 0 failed, 1 skipped**, 188,80 s.
- Ruff check .: passed.
- Mypy: passed, 25 file.
- GUI source smoke: main.py --smoke-test, exit 0.
- git diff --check: passed.
- Audit SHA256: **32 file frozen invariati**.

Nuovi test: stato/identità, dead band, ritratto coperto con evidenze residue, potere animato, due ritratti cambiati, nessun trascinamento dell'incertezza, planner incrementale/offline equivalente, buffer limitato, una sola sessione anche con copertura locale 100%, VS coarse omesso e recuperato, C che non rivendica START solo periodici, fallback esplicito. Rimangono test di errore/cancellazione, cache/shadow, DK, risultati brevissimi, orfani e controesempio a 30 s.

Nessun test frozen modificato, provider AI chiamato, cache utente/progetto cancellato o commit.

## 8. Riproduzione e default

Runner video-only dalla repository, output nuovo obbligatorio:

~~~powershell
& .\.venv310\Scripts\python.exe scripts/diagnose_adaptive_gameplay.py "$env:USERPROFILE\Downloads\videoprova.mp4" --backend adaptive --full-videoprova --reference docs/performance_reports/adaptive_diagnostic_reference.json --output docs/performance_reports/adaptive_diagnostic_new_run.json
~~~

Richiede videoprova.mp4 e durata <=3800 s; non può avviare incidentalmente il VOD da sette ore. Il confronto sopra è già stato eseguito.

GUI invariata: tradizionale default. Per test: AUTO_CUTTER_GAMEPLAY_BACKEND=adaptive. adaptive-shadow confronta entrambi e restituisce il tradizionale; non è la modalità per misurare una velocizzazione dell'editor. Ritorno: traditional o variabile rimossa. AUTO_CUTTER_ADAPTIVE_FULL_FALLBACK=1 conserva il fallback esatto.

## 9. Decisione e limiti

**NOT_BENEFICIAL per sostituire il tradizionale.** Risolve la doppia scansione e riduce matching, ma non supera il gate di velocità su questa sorgente intera. Le clip possono ancora essere più lente, soprattutto se recuperano un END orfano reale assente nella loro baseline.

Due problemi residui:
1. Descrittori 16×20 troppo variabili con animazioni/hover/overlay: 275 campioni sospetti, recupero 67,73%. Servono descrittori più invarianti verificati su finestre annotate, non soglie WIN/LOSS più basse o passo maggiore arbitrario.
2. Il monitor consegna ancora tutti i frame 1 FPS. Il risparmio OpenCV è assorbito dall'attesa: una futura strategia di seek/finestre deve misurare decoding/consegna e costo totale, preservando recupero e recall.

Due partite identiche possono nascondere START/END fra campioni: nessuna fingerprint garantisce il recupero di segnali invisibili. Le guardie non vengono rimosse su questo singolo risultato. Nessuna equivalenza universale o velocizzazione sul VOD da sette ore dichiarata.

## File di questa iterazione

Modificati: adaptive.py, adaptive_policy.py, fingerprint.py; state_aware.py aggiunge un hook diagnostico no-op tradizionale; benchmark_adaptive_gameplay.py aggiunge scelta dei passi; tests/test_adaptive_gameplay.py esteso. Nuovo scripts/diagnose_adaptive_gameplay.py.

Documentazione: questo report, appendice in ../ADAPTIVE_GAMEPLAY_DETECTOR.md e JSON elencati. Modifiche preesistenti a gameplay_editing, sampler e mapping DK preservate.
