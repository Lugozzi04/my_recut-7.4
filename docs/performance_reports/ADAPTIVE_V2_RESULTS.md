# Adaptive Detector V2 — sparse seek + Game Fingerprint

Data: 2026-10-12. Stato: **EXPERIMENTAL / NOT_BENEFICIAL**.
Il tradizionale resta il default. La V2 conserva gli eventi su questo VOD, ma **non raggiunge il requisito di velocità né quello di riduzione dei frame complessivi**.

## Implementazione effettiva

Nuovi moduli:
- `analysis/gameplay/hearthstone/adaptive_v2.py`: backend sparse, gerarchia, recuperi locali, metriche e namespace cache separato.
- `analysis/gameplay/hearthstone/fingerprint_v2.py`: descrittori indipendenti dei due eroi, bordi della mappa, lettura opzionale del mana totale, confronto delle evidenze.
- `resources/gameplay/hearthstone/adaptive_v2/mana.npz` e `mana_provenance.json`: piccoli riferimenti locali del denominatore, senza modelli/download.
- `tests/test_adaptive_v2.py`: 32 test.
- `scripts/benchmark_adaptive_v2.py`: esecuzione limitata esplicitamente al VOD di riferimento, senza ASR/provider.

Modificato `analysis/gameplay_editing.py` soltanto per aggiungere i backend `adaptive-v2` e `adaptive-v2-shadow` alla selezione sperimentale esistente. Il vecchio adattivo resta disponibile.

Riutilizzati senza modifiche: FrameSamplingService, sampler FFmpeg, matcher frozen VS/board/risultati, refinement VS 6 FPS, risultati 60 FPS, result decision/deduplication e GameAssembler. Whisper, Smart Boundaries, Hero Groups, Packaging, export e progetto non sono stati modificati.

### Accesso davvero sparse

Prima ricognizione: richieste 0, 30, 60, …, 3720 s e campione finale a 3749.899667 s: **126 richieste iniziali**.

Si usa `get_many()` con seek accurati individuali di 100 ms, non uno stream a 1 FPS che scarta i frame intermedi. Il batch in RAM è limitato a 8 frame. FFmpeg può comunque decodificare il preroll del GOP: frame richiesti/consegnati non significano numero esatto di frame internamente decodificati.

Per ogni intervallo sospetto vengono richiesti i punti medi:
**30 → 15 → 7.5 → 3.75 s**. Ogni metà viene valutata indipendentemente, senza scegliere ciecamente una sola direzione. I leaf irrisolti passano al monitor tradizionale locale a 1 FPS. Le finestre sono allargate di 4 s, arrotondate a secondi interi e unite prima della lettura.

Controlli indipendenti iniziali, finali e periodici: ogni 60 s, finestra nominale di 8 s, **16 s effettivi con l'overlap di 4 s per lato**. Servono per cercare transizioni invisibili agli estremi, ma non ne garantiscono il recupero universale.

Errori del percorso sparse attivano il fallback completo. La cancellazione viene propagata senza avviare un'altra analisi.

### ROI verificate su frame reali

Coordinate normalizzate sul reference frame 960×540, dopo il viewport esistente. Consultare `adaptive_v2_geometry.jpg` e `adaptive_v2_mana_samples.jpg`.

| Segnale | x | y | w | h |
|---|---:|---:|---:|---:|
| Nostro ritratto, basso | .466 | .707 | .068 | .128 |
| Ritratto avversario, alto | .466 | .085 | .068 | .128 |
| Mappa, bordo alto sinistro | .235 | .045 | .105 | .075 |
| Mappa, bordo alto destro | .675 | .065 | .100 | .075 |
| Mappa, bordo basso sinistro | .235 | .735 | .100 | .060 |
| Mappa, bordo basso destro | .685 | .745 | .090 | .055 |
| Mappa, bordo sinistro | .221 | .250 | .023 | .220 |
| Mappa, bordo destro | .854 | .270 | .022 | .220 |
| Mana totale, micro-ROI denominatore | 634/960 | 496/540 | 6/960 | 9/540 |

Nessun descrittore del centro del board, minion o mano. Esclusioni configurabili per webcam, scoreboard e margini deck/chat; una ROI che interseca un'esclusione viene omessa. I due ritratti non vengono scambiati e la fingerprint non assegna classi. Il riferimento DK e la classificazione Hero Groups rimangono quelli esistenti, coperti dalle regressioni.

I bordi possono comunque avere decorazioni animate: queste ROI non equivalgono a una garanzia di geometria immutabile. Le coordinate sono riferite al layout verificato, non a tutti i possibili overlay.

### Firma, stato e mana

Ritratti e bordi hanno descrittori separati: Gaussian blur leggero, resize 16×20, rimozione della media e normalizzazione, riutilizzando il descrittore esistente. Nessuna identità pixel per pixel.

Continuità plausibile richiede entrambi i ritratti compatibili e almeno due bordi compatibili, con floor .90. Non è una probabilità di stessa partita né una prova di assenza di transizioni. Board high/low rimangono .38/.20; la fascia intermedia è UNKNOWN in questa V2. I candidati deboli VS/risultato aprono approfondimenti anche indipendentemente dall'identità.

Il lettore mana considera **soltanto il numero dopo la barra**, non il numeratore o i cristalli luminosi. Confronta piccoli glyph locali, richiedendo correlazione >= .995 e margine >= .05. Riferimenti manualmente osservati da videoprova: totali 1, 2, 3, 4, 5, 6, 8, 9. **Copertura incompleta: 0, 7 e 10 non sono nel pack; letture ambigue/non coperte restano None.** Non è un OCR generalizzato né un lettore già validato su altri VOD/font.

Controlli locali hanno letto correttamente, fra gli altri, 1/5 e 0/3 come totale 5 e 3, e rifiutato schermate oscurate. Nel run sono state accettate 99 letture su 856 osservazioni sparse; nessuna foglia aveva il reset mana come causa principale. Non attribuiamo a questo segnale un miglioramento misurato.

Un decremento affidabile del totale è solo sospetto, compresi possibili effetti delle carte. Non produce START, END o un risultato. Mana assente non viene interpretato come zero.

Le run del monitor vengono spezzate ai gap osservativi: due board hit distanti 30 s non contano come osservazioni consecutive. I risultati orfani mantengono start mancante; l'assembler resta invariato.

## Unico confronto integrale

Sorgente: `videoprova.mp4`, durata 3749.999667 s. Nessun VOD da sette ore, 73/74/86, ASR, agente o Packaging avviato.

V2 eseguita **una sola volta** con cache detection e persistenza dei selected frame disabilitate. Confronto con il riferimento tradizionale già salvato in `adaptive_diagnostic_reference.json`; non abbiamo ripetuto il tradizionale. Fingerprint della sorgente, asset dei matcher e result policy sono stati verificati compatibili. La cache disco del sistema operativo non è stata controllata: i tempi tradizionali sono una baseline storica, non una nuova prova alternata nello stesso processo.

Dati dettagliati: `adaptive_v2_videoprova.json`. Include hash dell'implementazione misurata, confronto evento per evento, fasi, contatori, gerarchia e intervalli.

| Metrica | Tradizionale storico | Sparse V2 |
|---|---:|---:|
| Detection completa | 120.330 s | **212.360 s** |
| Monitor globale, inclusi recuperi | 99.753 s | **193.085 s** |
| Refinement VS | 2.136 s | 2.030 s |
| Refinement risultati | 18.359 s | 17.200 s |
| Processi FFmpeg totali | 17 | **896** |
| Sessioni con seek | 16 | **894** |
| Frame consegnati complessivi | 8876 | **9054** |
| Frame consegnati al monitor | 3750 | **4198** |
| Frame unici con matching monitor | 3750 | **3974** |
| Chiamate VS / board / risultato nel monitor | 3750 ciascuna | **3974 ciascuna** |
| Confronti visivi globali VS+board+result | 135000 | **143064** |
| Frame refinement VS | 266 | 266 |
| Frame refinement risultati | 4860 | 4590 |
| Fallback locale, durata | intero monitor 3750 s | **3342.000 s / 89.12%** |
| Eventi | 9 | 9 |
| Partite complete / incomplete | 4 / 1 | 4 / 1 |
| WIN / LOSS / UNKNOWN | 2 / 2 / 1 | 2 / 2 / 1 |
| Pairing errati | 0 | 0 |

La V2 costa **92.03 s in più (+76.48%)**. Pur richiedendo pochissimi frame iniziali, la gerarchia richiede altri **730 punti intermedi**. In totale: 856 frame sparse + 3342 frame fallback. Gli overlap riutilizzano alcuni score, ma non eliminano tutta la consegna duplicata.

### Dove viene speso il tempo

| Fase V2 | Tempo |
|---|---:|
| Ricognizione iniziale, inclusi seek e matching | 14.209 s |
| Gerarchia, inclusi seek e matching | 92.832 s |
| Fallback locali | 85.977 s |
| Richieste frame sparse, incluse nelle prime due righe | **98.393 s** |
| Fingerprint CPU, inclusa nelle prime due righe | 0.667 s |

Non sommare le righe incluse alle fasi parent. L'accesso sparse comprende identità, seek, preroll, decode, filtri, consegna e cleanup; non è CPU pura del decoder.

La rappresentazione in sé è economica; **856 processi per la parte sparse e quasi tutto il VOD recuperato** eliminano il vantaggio. Inoltre i matcher eseguono circa il 5.97% di confronti globali in più.

### Cause degli approfondimenti

Le cause sono diagnostiche di routing, non annotazioni di vere transizioni.

| Causa | Intervalli iniziali sospetti | Foglie inviate al fallback |
|---|---:|---:|
| Stato UNKNOWN | 64 | **429** |
| Discontinuità identità sospetta | 36 | **318** |
| Transizione board | 11 | 9 |
| Candidato visuale VS/risultato | 2 | 19 |
| Identità insufficiente | 0 | 5 |
| Totale | 113 | 780 |

I primi due casi rappresentano 747/780 foglie, circa **95.8%**. 359/856 osservazioni sparse cadono nello stato UNKNOWN: è una proporzione dei campioni, non della durata del VOD. Gli intervalli ampliati si uniscono in 25 recuperi, con copertura 89.12%.

L'incertezza viene approfondita fino al leaf anche quando aggiunge pochi indizi; la somiglianza dei ritratti non è abbastanza stabile per evitare molti recuperi. Questa implementazione non ha ancora un criterio di arresto basato sul costo del seek rispetto al recupero sequenziale. Nessun parametro è stato ritoccato dopo il run.

## Accuratezza evento per evento

| Tipo | Baseline (s) | V2 (s) | Errore |
|---|---:|---:|---:|
| VS_SCREEN | 630.583 | 630.583 | 0 |
| DEFEAT | 1301.083 | 1301.083 | 0 |
| VS_SCREEN | 1359.450 | 1359.450 | 0 |
| VICTORY | 1785.983 | 1785.983 | 0 |
| VS_SCREEN | 1819.750 | 1819.750 | 0 |
| DEFEAT | 2791.983 | 2791.983 | 0 |
| VS_SCREEN | 2863.750 | 2863.750 | 0 |
| VICTORY | 3525.500 | 3525.500 | 0 |
| VS_SCREEN | 3628.933 | 3628.933 | 0 |

**9/9**, nessun evento extra o mancante, timestamp esatti. Candidate-window recall VS e risultati: 100% rispetto alla baseline. Le quattro coppie START→END sono identiche e l'ultimo START rimane incompleto/UNKNOWN. Nessuna falsa LOSS osservata su questo VOD.

Non è una misura del recall autonomo senza recuperi: il risultato include il fallback all'89.12%. Né un singolo VOD, usato anche per geometria/riferimenti mana, dimostra generalizzazione.

## Cache, compatibilità e test

- Cache detection V2 separata in `cache_root()/gameplay/hearthstone/adaptive-sparse-v2`.
- Identità sorgente, parametri, ROI/esclusioni, versione e fingerprint del pack mana entrano nella chiave.
- Settings normalizzati a JSON per garantire il confronto dopo il round-trip persistente.
- Selected frames e loro parametri/risoluzioni rimangono nella cache condivisa esistente.
- Nessuna invalidazione di Whisper, boundary, gruppi o cache precedenti.
- Missing frame e overlay non inventano eventi; errori sparse usano il tradizionale completo. Ctrl+C/cancellation non attivano fallback.
- Sono mantenuti i test negativi su una partita completamente invisibile fra campioni con fingerprint uguali: la V2 non ha una garanzia universale.

Risultati:
- 190 test mirati prima del run reale.
- **554 passed, 0 failed, 0 skipped** nella suite pertinente finale Gameplay/Adaptive/Hero Groups/Shared Analysis, inclusi **32 test V2**.
- Ruff sui cinque file Python aggiunti/modificati: pass.
- Mypy sui quattro moduli/script runtime interessati: pass.
- `python main.py --smoke-test`: exit 0.
- `python main.py presets`: exit 0.
- 32 asset frozen controllati tramite SHA-256: **invariati**.
- Suite completa non rieseguita; nessuna chiamata AI a pagamento, download o commit.

## Prova manuale dalla GUI

Dalla root del progetto, con il proprio interprete:

~~~powershell
$env:AUTO_CUTTER_GAMEPLAY_BACKEND = "adaptive-v2"
.\.venv310\Scripts\python.exe main.py
~~~

Usare il normale pulsante **Detect Games**. Per confrontare entrambi e conservare il risultato tradizionale:

~~~powershell
$env:AUTO_CUTTER_GAMEPLAY_BACKEND = "adaptive-v2-shadow"
.\.venv310\Scripts\python.exe main.py
~~~

Shadow esegue entrambi i backend, può usare le rispettive cache ed è intenzionalmente più costoso. I confronti sono in `metadata.adaptive_shadow`. I report GUI esistenti raccolgono le fasi `adaptive_v2.*`, `sampling`, `shared.frames` e gli hit/miss della cache.

Ritorno immediato al tradizionale:

~~~powershell
$env:AUTO_CUTTER_GAMEPLAY_BACKEND = "traditional"
.\.venv310\Scripts\python.exe main.py
~~~

Per forzare il fallback completo della V2: `AUTO_CUTTER_ADAPTIVE_FULL_FALLBACK=1`. Rimuovere/reimpostare a 0 per le prove sparse. La cache usa una chiave distinta per questa opzione. Non cancellare cache di altri componenti per confrontare il solo detector.

## Decisione e limiti aperti

**Non abilitare V2 come default.** L'accuratezza rispetto alla baseline è preservata sul materiale provato, ma il requisito prestazionale fallisce.

Prima di un altro run integrale occorre verificare su finestre brevi:
1. Arresto dell'approfondimento quando l'incertezza persiste e il recupero sequenziale costa meno di altri seek.
2. Separazione del dead band della board dall'incertezza di identità, senza cambiare i matcher frozen.
3. Descrittori più stabili alle animazioni dei ritratti e ai bordi decorativi, con evidenza indipendente prima di rilassare criteri.
4. Copertura e affidabilità del lettore mana, attualmente parziale.
5. Budget complessivo di seek, fallback e guardie; ridurre semplicemente i controlli senza misurare gli eventi mancati sarebbe scorretto.

Restano possibili transizioni interamente invisibili fra campioni, overlay/layout diversi e più transizioni nello stesso intervallo. Nessuna fingerprint elimina quel limite. Nessuna ricerca su 73 frozen o sul VOD da sette ore è stata effettuata.
