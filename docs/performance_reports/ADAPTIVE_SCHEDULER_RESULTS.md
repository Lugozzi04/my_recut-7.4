# Adaptive Detector V2 — scheduler cost-aware (lattice + accesso in-process)

Data: 2026-10-13. Stato: **EXPERIMENTAL / BENEFICIAL su videoprova**. Il tradizionale **resta il default** (regola dell'handoff); la V2 si attiva solo con `AUTO_CUTTER_GAMEPLAY_BACKEND=adaptive-v2`.

| Metrica (videoprova, 3750 s) | Tradizionale (storico) | V2 prima | **V2 scheduler lattice** |
|---|---:|---:|---:|
| Detection completa | 120.33 s | 212.36 s | **44.10 s** (−63%, 2.73× più veloce) |
| Monitor globale | 99.75 s | 193.09 s | **26.57 s** |
| Processi FFmpeg | 17 | 896 | **15** (+ 4 sessioni decoder in-process) |
| Eventi | 9/9 | 9/9 | **9/9, errore 0.000 s** |
| Pairing errati | 0 | 0 | **0** |

## 1. Perché la V2 era lenta (causa architetturale, non soglie)

1. **Un processo FFmpeg per ogni seek.** `get_many()` batcha solo richieste distanti ≤3 s. I punti della gerarchia (30/15/7.5/3.75 s) sono quasi sempre più lontani: 856 richieste → 856 processi, circa 105 ms l'uno. La sola consegna sparse costava 98 s.
2. **Il fallback era la scansione tradizionale.** Ogni foglia incerta diventava una finestra sequenziale a 1 FPS, allargata di 4 s per lato, più le guardie (16 s effettivi ogni 60 s). Lo stato UNKNOWN (dead band della board, 37% della timeline) e l'identità instabile generavano il 95.8% delle foglie. Risultato: 89.1% della timeline decodificato per intero.
3. **Decodifica e matching sono due pavimenti distinti.** La decodifica sequenziale di un 1080p60 costa circa 22–27 ms per secondo sorgente, anche in-process e multi-thread (misurato). Il matching VS+board+result costa circa 13 ms per riga. Ridurre solo i processi portava a circa 125 s, cioè nessun guadagno sul tradizionale.

Il lavoro ridondante da eliminare era quindi: processi per seek, decodifica di tutti i frame intermedi e righe 1 FPS che non cambiano nulla per il planner.

## 2. Modifiche

### C. Accesso ai frame in-process (`analysis/shared/inprocess_frames.py`, opt-in in `FrameSamplingService`)
- PyAV (già in `requirements.txt`, `av==16.1.0`; **nessuna nuova dipendenza**).
- Una sessione decoder per worker (4) per tutta la scansione, invece di un processo per richiesta.
- Selezione identica a `sample_at`: primo frame reale in `[t, t+0.10)`.
- Stesso decoder libavcodec, swscale BICUBIC (default del filtro `scale` FFmpeg), BGR24 e geometria identica.
- PTS video-relativi come `-copyts -start_at_zero`, arrotondati a 6 decimali come `showinfo`.
- **Verificato pixel e PTS identici** al percorso FFmpeg:
  - 15 timestamp su videoprova (allineati, dispari, frazionari, inizio e fine) e 2 finestre sequenziali reali;
  - test automatico su clip H.264 con B-frame.
- Errore del reader → stesso servizio sul percorso FFmpeg. La cancellazione non è mai un fallback.
- I frame del monitor V2 **non vengono più persistiti**: con la V2 di prima, la GUI scriveva circa 1.5 MB × 856 frame npz per run. Il default degli altri consumer (Packaging, Hero Groups, boundary) è invariato.

### A. Cost-aware, con costi misurati
- `AccessCosts` misura durante il run, con media esponenziale:
  - `seek_s`: costo di un seek più la decodifica fino al frame;
  - `forward_s_per_s`: costo di decodificare in avanti, per secondo sorgente.
- Una richiesta più avanti della posizione del decoder viene decodificata in avanti, invece di fare seek, se la distanza è sotto il **break-even** `seek_s / forward_s_per_s`.
  - Seed documentati: 25 ms e 22 ms/s, misurati su questo VOD.
  - Valori finali misurati nel run: 59 ms per seek (4 worker concorrenti), 37 ms/s, break-even 1.6 s.
- Su richieste dense il reader diventa automaticamente una scansione sequenziale; su richieste distanti fa seek. Nessuna soglia fissa nascosta.
- La gerarchia è "snappata" sul lattice di 2 s, quindi i suoi campioni sono un **sottoinsieme** della scansione densa del lattice: non costa mai più della scansione dell'intervallo.

### D. Gerarchia conservata (`adaptive_v2.py`, scheduler `lattice`)
- `30 s → 14/16 → 6/8 → 2/4 → 2 s`: i punti medi di 30/15/7.5/3.75 arrotondati al lattice di 2 s.
  - È l'intervallo keyframe del VOD: i seek cadono su keyframe, con lag 0.
  - Garantisce almeno 2 campioni dentro ogni VS: la visibilità minima misurata è 5.2 s al matcher frozen ≥.80.
- Stessi criteri di prima (`discontinuities`: mana, eroi, mappa, stato, candidati deboli VS/risultato) e entrambe le metà esplorate.
- Unico criterio aggiunto, che rende la V2 **più prudente**: due campioni fuori partita non sono mai "plausibili", perché un VS può stare fra due menu. Si salta solo la continuità IN_GAME con identità stabile.
- Guardie iniziali, finali e periodiche (8 s ogni 60 s) conservate, campionate sul lattice invece che come fallback sequenziali di 16 s.
- Recupero degli END orfani (finestre `orphan_board_disappeared`) conservato. Sul clip 1200–1900 s ha recuperato una DEFEAT che il tradizionale perde perché la clip inizia a metà partita.

### B. Pianificazione dei fallback: righe reali solo dove il planner può cambiare
- Il planner dei risultati (`GameStateMonitor`, invariato) usa solo quattro ingressi per riga: board ≥ high, board ≤ low, risultato debole e VS grossolano.
- Il fallback ora è per **foglia di 2 s**:
  - se i due campioni del lattice differiscono su uno di questi ingressi, si legge la riga 1 FPS intermedia reale (refill), così ogni transizione usa righe vere;
  - se coincidono, la riga intermedia è imputata copiando il campione sinistro.
- Il planner gira su una **sequenza 1 FPS completa** (reale più imputata) in un solo run. Arming, `board_present`, riacquisizioni e trigger hanno lo stesso stato del tradizionale, senza gli spezzoni con contesto mancante della V2.
- Refill raccolti, deduplicati e letti in un unico batch; i campioni già letti vengono riutilizzati, senza mai rileggere la stessa riga.
- Le finestre VS 6 FPS e risultati 60 FPS sono quelle del tradizionale, con lo stesso codice.

## 3. Unico confronto integrale (`adaptive_scheduler_videoprova.json`)

Un solo run di `scripts/benchmark_adaptive_v2.py --full-videoprova` (scheduler `lattice`):
- cache detection e selected frame disabilitate;
- nessun ASR, agente, Packaging o VOD da 7 h.

Confronto con il riferimento tradizionale salvato (`adaptive_diagnostic_reference.json`), come per la V2: il tradizionale non è stato rieseguito. La cache disco del sistema operativo non è controllata.

### Tempi

| Fase | Tradizionale | V2 prima | Lattice |
|---|---:|---:|---:|
| Detection | 120.33 s | 212.36 s | **44.10 s** |
| Monitor (ricognizione + gerarchia + fallback) | 99.75 | 193.09 | **26.57** |
| — ricognizione 30 s + guardie | — | 14.21 | 4.26 |
| — gerarchia | — | 92.83 | 16.56 |
| — fallback (sequenziale prima / refill ora) | — | 85.98 | 5.65 |
| — attesa frame totale | 50.57 | 98.39 | **0.67** |
| — matching board / result / VS | 15.7 / 16.3 / 15.3 | 14.2 / 14.9 / 13.9 | 7.8 / 8.2 / 7.4 |
| Refinement VS (6 FPS) | 2.14 | 2.03 | 2.03 |
| Refinement risultati (60 FPS) | 18.36 | 17.20 | 15.48 |

La decodifica è ora quasi interamente sovrapposta al matching, con 0.67 s di attesa. Il monitor è limitato dal matching (circa 25 s), non più dall'accesso ai frame.

### Lavoro

| Contatore | Tradizionale | V2 prima | Lattice |
|---|---:|---:|---:|
| Processi FFmpeg | 17 | 896 | **15** |
| Sessioni con seek FFmpeg | 16 | 894 | 15 |
| Seek in-process (4 container) | — | — | 2064 |
| Frame decodificati internamente dal reader | — | — | 22104 |
| Frame consegnati, totale | 8876 | 9054 | **6784** |
| Frame consegnati al monitor | 3750 | 4198 | **2078** |
| — campioni lattice/gerarchia (di cui guardie) | — | 856 | 1758 (255) |
| — righe 1 FPS reali di refill | — | 3342 | **320** |
| Righe imputate per il planner | — | — | 1672 |
| Timeline in fallback | 100% | 89.12% | **8.53%** (320 s) |
| Righe monitor reali / timeline | 100% | 106% | 55.4% |
| Chiamate VS monitor | 3750 | 3974 | 2078 |
| Confronti board / text monitor / text finestre | 22500 / 90000 / 116640 | 23844 / 95376 / 110160 | 12468 / 49872 / 106560 |
| Finestre risultato (secondi) | 11 (81 s) | 10 (76.5 s) | 10 (74 s) |

### Eventi

| Tipo | Baseline (s) | Lattice (s) | Errore |
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

- **9/9**, nessun evento extra o mancante.
- Quattro partite complete con lo stesso risultato: LOSS, WIN, LOSS, WIN. L'ultima resta incompleta/UNKNOWN.
- 0 pairing errati; recall delle finestre candidate VS e risultati 100%.
- Le 5 finestre VS candidate coincidono con quelle del tradizionale.

### Differenza misurata, nessuna perdita di eventi

Il planner non è identico in **2 punti su 11**:
- manca la finestra tradizionale 2583–2590 e con essa la riacquisizione 2600;
- la finestra 3539–3546 diventa 3546–3553.

Causa: in entrambi i casi è un **calo isolato di un solo secondo** in una riga dispari: 2585 vale 0.084 fra 0.331 e 0.329; 3541 vale 0.000 fra 0.222 e 0.201. I due campioni del lattice concordano, quindi la riga viene imputata.

Nessuna delle due finestre conteneva un risultato:
- un vero END tiene la board assente per diversi secondi, quindi il lattice lo vede e il refill lo legge;
- una riacquisizione mancante cambia la deduplicazione solo se ci sono risultati entro 45 s da entrambe le parti, cosa impossibile tra due partite.

Quantificazione sulle righe tradizionali:
- 77 righe dispari su 1875 cambiano classe board rispetto a due vicini concordi; solo 2 alterano il planner.
- Leggere sempre le righe dispari fuori dalle zone board-alta costerebbe circa 688 righe in più (+33%) e comunque non sarebbe esatto per i cali dentro zone alte. Non è stato fatto.

**Ipotesi documentata dello scheduler: le transizioni rilevanti per il planner durano almeno quanto il passo del lattice (2 s).** Come la V2, non c'è garanzia universale di transizioni invisibili (`invisible_transition_guarantee=False`).

### Clip breve reale (prima del run integrale)

1200–1900 s di videoprova, copia di stream dal keyframe 1200:

| | Tempo |
|---|---:|
| Tradizionale | 20.27 s |
| Lattice | 9.87 s |

- I 3 eventi tradizionali sono identici (errore 0), con le stesse riacquisizioni.
- In più c'è la DEFEAT orfana a 101.083 della clip, recuperata dalla V2 (la clip inizia a metà partita).

## 4. Test e controlli

- `tests/test_adaptive_v2.py`: 45 test.
  - Nuovi:
    - equivalenza lattice ↔ tradizionale su fixture, con meno righe reali;
    - refill solo alle transizioni del planner;
    - sequenza planner completa;
    - incertezza d'identità → lattice denso, non sequenziale;
    - fallback completo su errore e propagazione della cancellazione;
    - validazione delle impostazioni;
    - default in-process non persistito;
    - reader in-process **pixel/PTS identico a FFmpeg** su H.264 reale con B-frame, inclusi riuso delle sessioni, seek all'indietro e scan sequenziale;
    - errore del reader → percorso FFmpeg;
    - cancellazione ≠ fallback.
  - I test della V2 precedente restano, fissati su `scheduler="legacy"`.
  - La fixture sintetica storica mostra il VS per 0.7 s, contro i ≥5.2 s misurati nel VOD; per il lattice c'è una fixture con VS di 6 s.
- Suite pertinente Gameplay / Adaptive / Shared Analysis / Hero Groups: **584 passed, 0 failed, 0 skipped**.
- Ruff e mypy sui file modificati: pass. `python main.py --smoke-test`: exit 0.
- Freeze videoprova, controllato senza modificare i file:
  - 23/23 artifact frozen non-codice (template, profili, pack) **identici**.
  - 21/26 file di codice identici. I 5 diversi (`state_aware.py`, `visual/board.py`, `visual/detector.py`, `visual/result_text.py`, `visual/sampler.py`) sono cambiati prima di questo intervento: commit `05f8c79` (10-10), `8b0067a` (10-12) e modifiche non committate già presenti all'inizio della sessione.
  - Questo intervento **non tocca** detector tradizionale, matcher, soglie, FPS, result policy, Whisper, boundary, export, Packaging o GUI.
- Cache reali (`%LOCALAPPDATA%\Auto Cutter\cache`): nessun file scritto durante sviluppo e benchmark.

## 5. File modificati

- `analysis/shared/inprocess_frames.py` (nuovo): reader in-process e costi misurati.
- `analysis/shared/frames.py`:
  - opzioni `in_process` e `workers`;
  - metodi `iter_many` (consegna in streaming), `scan`, `close`, `access_counters`.
  - Il default è invariato per tutti gli altri consumer.
- `analysis/gameplay/hearthstone/adaptive_v2.py`:
  - scheduler `lattice` (default della V2) e `legacy` conservato;
  - `_scan_lattice`, planner su sequenza completa;
  - versione `hearthstone-adaptive-sparse-v2.1`, quindi chiave cache distinta.
- `analysis/gameplay_editing.py`: `AUTO_CUTTER_ADAPTIVE_SCHEDULER` (`lattice`/`legacy`) solo per il backend V2.
- `scripts/benchmark_adaptive_v2.py`: `--scheduler`, hash anche del reader.
- `tests/test_adaptive_v2.py`.
- Evidenza: `docs/performance_reports/adaptive_scheduler_videoprova.json`.

## 6. Prova dalla GUI

~~~powershell
$env:AUTO_CUTTER_GAMEPLAY_BACKEND = "adaptive-v2"          # scheduler lattice
.\.venv310\Scripts\python.exe main.py
~~~

- Confronto con il tradizionale nello stesso run, più costoso: `adaptive-v2-shadow`.
- V2 precedente: `$env:AUTO_CUTTER_ADAPTIVE_SCHEDULER = "legacy"`.
- Ritorno al default: `$env:AUTO_CUTTER_GAMEPLAY_BACKEND = "traditional"`.

## 7. Problemi aperti

1. **VOD da 7 h non eseguito** (come richiesto). Il guadagno atteso è proporzionale: monitor circa 3.7× più veloce sul solo monitor. Va verificato dalla GUI, preferibilmente in `adaptive-v2-shadow` una volta.
2. **Un solo VOD** con questi tempi e una sola sorgente (1080p60, GOP 2 s).
   - Con GOP lunghi ogni campione del lattice costa più decodifica; il break-even misurato sposta il reader verso la decodifica in avanti, ma il guadagno sarà minore.
   - Con VFR o 29.97 fps le righe restano "primo frame ≥ k s", come nella V2.
3. **La gerarchia salta poco su questo VOD**: 35 intervalli plausibili, 304 s.
   - I campioni lattice sono 1758 su 1875 possibili: il guadagno viene da accesso in-process e righe imputate, non dal salto di intervalli.
   - Descrittori d'identità più stabili ridurrebbero ancora i campioni. Non sono stati toccati: niente nuove euristiche visive prima dei costi, come da handoff.
4. Il monitor ora è **limitato dal matching** (circa 25 s su 26.6 s). Un matching parallelo dei matcher frozen è possibile ma non fatto: cambia la concorrenza dei matcher, va validato a parte.
5. Il refinement risultati a 60 FPS (15.5 s, invariato) pesa ora il 35% del totale.
6. Ipotesi "transizioni ≥2 s" (§3): 2 finestre e 1 riacquisizione diverse su questo VOD, senza eventi persi.
7. Il batch FFmpeg `sample_at` confronta PTS float: su richieste esattamente al confine di un frame può saltare quel frame, mentre il percorso FFmpeg a richiesta singola lo restituisce. Il reader segue la richiesta singola, con tolleranza di 1e-9. È documentato nei test.
8. Memoria: 4 decoder con frame threading restano aperti solo durante la scansione e vengono chiusi alla fine (`close()`).
