# Adaptive V2 — audit di accuratezza sul VOD da 7 ore

Data: 2026-10-13. Sorgente: `THEORYCRAFTING DAY 1.mp4` (25 350 s, 1080p60).
Evidenza compatta: `adaptive_v2_accuracy_theorycrafting.json` (eventi, partite, finestre e confronti).
Stato: **Adaptive V2 resta sperimentale**; il tradizionale resta il default.

## Esito

| | Tradizionale | V2 run GUI (prima) | **V2 corretta** |
|---|---|---|---|
| Detection | 796 s | 267 s | **369 s** (2,2× più veloce) |
| Monitor | 677 s | 160 s | 222 s |
| Refinement risultati 60 FPS | 105 s (463 s di finestre) | 93 s (426 s) | 131 s (575 s) |
| Eventi | 55 | 53 | **56** |
| WIN / LOSS / UNKNOWN | 18 / 9 / 3 | 18 / 7 / 5 | **18 / 10 / 2** |
| Partite complete | 24 | 23 | **26** |
| Risultati con timestamp identico al tradizionale | 27/27 | 24/27 | **27/27 (errore 0)** |
| Pairing errati | 0 | 0 | **0** |
| Processi FFmpeg (refinement VS e risultati) | — | 83 | **81** |

Le differenze rispetto al tradizionale, dopo la correzione, sono tutte verificate sui frame reali:
- **Un risultato in più**: DEFEAT 13562.299.
- **Uno START in più**: VS 20951.999.
- **Uno START più preciso**: 21266.199 invece di 21268.182.

## 1. Le 7 righe problematiche (run GUI)

| # | START | Fine | V2 prima | Tradizionale | Causa | Dopo |
|---|---|---|---|---|---|---|
| 3 | VS 5419.75 | DEFEAT **6391.982** | UNKNOWN | LOSS | **Regressione detector**: calo di board di 1 s (6393) imputato dal lattice → trigger 6396 → finestra [6394, 6401] dopo una DEFEAT di 0,2 s | **LOSS** |
| 12 | VS 12268.23 | DEFEAT **12778.049** | UNKNOWN | LOSS | **Regressione detector**: calo isolato a 12775 imputato → finestra [12780, 12787] dopo la DEFEAT | **LOSS** |
| 13 | VS 13287.75 | DEFEAT **13562.299** (0,18 s, "Sconfitta!") | UNKNOWN | UNKNOWN | **Perso da entrambi**: il banner arriva 2,7 s prima che la board si scurisca; la finestra −2 s inizia dopo | **LOSS** |
| 10 | VS 10448.75 | "Sconfitta!" visibile a 11159.8 | UNKNOWN | UNKNOWN | Banner in animazione: il matcher frozen lo riconosce in **un solo frame** e la policy frozen lo scarta (`insufficient_support`). Ora la finestra lo contiene | UNKNOWN (policy invariata) |
| 8 | VS 9035.93 | nessuna | UNKNOWN | UNKNOWN | **UNKNOWN reale**: partita in corso a 9178, nero a 9180, menu a 9182, nessuna schermata di risultato | UNKNOWN |
| 7 | — | VICTORY 9010.10 | WIN senza START, nessun ritratto o classe | uguale | **VS mai trasmesso**: deckbuilder fino a ~8970, nero a 8975, partita già avanzata da 8976. Hero Groups campionava solo da START + 60 s | WIN senza START; **hunter** (Rexxar, Steady Shot) |
| 26 | — | VICTORY 20906.97 | WIN senza START, nessun ritratto o classe | uguale | Come la 7: nessun VS osservabile prima della partita | WIN senza START; **warlock** (Gul'dan, Life Tap) |

**Rilevate per confronto, oltre alle 7:**
- **VICTORY 18492.115**, persa da una prima versione della correzione (vedi §5): recuperata.
- **Partita 27:** il VS 20951.999 è reale ma visibile solo 0,6 s (hit 0.94/0.96 a 6 FPS). Il tradizionale non lo coglie e lascia la partita senza START; la V2 sì.
- **VS successivo:** visibile da 21266.2 a 21268.65; il tradizionale lo data 21268.18, la V2 21266.20, che è l'inizio vero.
- **Partita 16 da 14 s** (VS 14993.4 → DEFEAT 15007.9): identica nei due detector.

## 2. Confronto con il tradizionale

La cache tradizionale non esisteva più. Ho rieseguito **una sola volta** il tradizionale completo (796 s, senza cache, ASR o AI), che coincide con la baseline storica: 30 partite, 18/9/3, 55 eventi, 24 complete.

V2 della GUI contro tradizionale:
- 52/55 eventi corrispondono;
- mancano DEFEAT 6391.982, DEFEAT 12778.049 e VS 21268.182 (che per la V2 è 21266.2);
- c'è in più il VS 20951.999;
- la DEFEAT 14274.865 è datata 14275.015, perché la finestra ha tagliato i primi 0,15 s della schermata.

V2 corretta contro tradizionale:
- 27/27 risultati con timestamp identico;
- 0 pairing errati;
- gli START differiscono di ≤ 0,133 s. Il refinement VS a 6 FPS ha passo 0,167 s e la fase della finestra dipende dalla prima riga coarse: i tradizionali sono a +0.049 s, la V2 a +0.015 s. Il comportamento è preesistente, non introdotto qui.

## 3. Detector rispetto a Hero Groups e GUI

- **Nessun errore di presentazione.** La GUI mostrava esattamente quanto c'era nelle cache di detection e Hero Groups.
- **#3, #12, #13: problemi del detector** (finestra risultato), poi assemblati correttamente come START senza END → UNKNOWN.
- **#7, #26: Hero Groups.** `sample_times` restituiva `[]` per una partita senza START, quindi niente campioni, ritratto o classe, pur con 30–40 s di board valida prima dell'END.
- **ROI e riferimenti verificati:**
  - il ritratto è quello **inferiore**, il nostro (`portrait_roi` .466/.707);
  - l'ROI non tocca la webcam;
  - sono caricati 11 riferimenti Hero Power, incluso Death Knight `HERO_11bp`;
  - skin (raggruppamento) e classe (portrait o Hero Power) restano distinti.

## 4. Verifica del sampling adattivo

Gli eventi persi cadevano tutti dove la V2 **copiava righe 1 FPS dal campione precedente**:
- a fine partita la board tremola in fascia media (0.20–0.38) e scende ≤ 0.20 per **un solo secondo**;
- il tradizionale apre lì la finestra −2/+5 s a 60 FPS, mentre la V2, imputando quella riga, la apriva 2–3 s dopo.

Le schermate DEFEAT durano 0,1–0,2 s, quindi uno spostamento di 2 s basta a perderle. Nessun problema di timestamp, PyAV o confini delle finestre VS. I VS non sono mai stati persi: la griglia di 2 s li ha colti tutti, anche quello di 0,6 s.

## 5. Correzioni (generali, nessun timestamp né nome di video)

1. **Righe reali mentre la board è "presente"** (`_present_rows`).
   - Finché il planner può far partire una finestra "board sparita" e l'ultimo campione reale non è una board solida (≥ 0.38), la riga 1 FPS viene letta, non imputata. Il primo calo è quindi reale come nel tradizionale.
   - Fuori partita e nelle board solide l'imputazione resta.
   - Costo: +1098 righe (da 53,4% a 57,7% della timeline).
2. **Recupero END su calo sostenuto** (`_sustained_windows`).
   - Problema emerso nella prima verifica: un calo di **un frame** a metà partita (18367: V2 0.158 a +0.015 s, tradizionale 0.229 a +0.049 s) chiudeva la presenza della board, che poi restava in fascia media. Alla fine vera (18493) nessuna finestra: VICTORY persa. Il tradizionale l'aveva trovata solo grazie a 34 ms di fase.
   - Correzione: oltre alle finestre del planner frozen (invariato), la V2 apre una finestra sul primo calo **sostenuto** (≥ 2 righe ≤ low) dopo una board presente. Un calo singolo non consuma la fine partita.
3. **Finestra di recupero ancorata all'ultima board solida**, con tetto `recovery_lookback_s = 10 s` (limite di costo in `SparseSettings`, validato 2–30).
   - Sequenza di fine partita: esplosione, banner (anche 0,2 s), board grigia, board scura.
   - Il calo arriva 2,7–4,2 s dopo il banner, oltre i 2 s fissi del planner.
   - Le soglie e il planner del tradizionale non cambiano.
4. **Hero Groups senza START** (`sample_times(..., after=)`, `previous_boundaries`).
   - Si campiona la stessa ampiezza della finestra normale (`horizon_s − delay_s` = 60 s) che termina 2 s prima dell'END, mai prima del confine precedente in ordine **cronologico**. Le partite senza START sono elencate in fondo, quindi non va usato l'ordine della lista.
   - La validazione board esistente scarta i frame fuori gioco, e nessuna classe viene assegnata senza evidenze.
5. **`analyzer_version` → `hearthstone-adaptive-sparse-v2.2`**: la cache della run GUI (v2.1, con i risultati errati) non viene più riutilizzata.

Nessuna modifica a soglie, FPS, matcher, result policy, planner tradizionale, Whisper, Packaging o export. Il tradizionale resta il default.

## 6. Test e accuratezza

- **Test nuovi in `tests/test_adaptive_v2.py`:**
  - calo di 1 s prima di un risultato di 0,1 s (sintetico; **fallisce senza la correzione**);
  - `_present_rows`;
  - calo di un frame che non consuma la fine partita;
  - ancoraggio della finestra all'ultima board solida;
  - validazione del lookback;
  - **regressione sulla sorgente reale**: partita VS 12268 → DEFEAT 12778 tagliata dal VOD, equivalenza V2 ↔ tradizionale. Saltato se il file manca; percorso via `AUTO_CUTTER_TEST_THEORYCRAFTING`. **Fallisce senza la correzione.**
- **Test nuovo in `tests/test_hero_groups.py`:** partita senza START, ordine cronologico, mai campioni della partita precedente.
- **Suite pertinente** (Gameplay / Adaptive / Shared / Hero Groups): **592 passed, 0 failed, 0 skipped**. Ruff e mypy puliti, `--smoke-test` exit 0.
- **Artifact frozen non-codice:** 23/23 identici.
- **Cache reali:** nessun file scritto. Le prove usano cache disabilitate o temporanee.
- **VOD completo:** 3 run V2 di sola detection (dopo la correzione 1, poi 1+2, poi finale) e 1 tradizionale. Nessun Whisper, Classic, Packaging o chiamata AI.
- **Clip reali:** 7 finestre confrontate tradizionale ↔ V2 prima della correzione.

## 7. UNKNOWN residui

- **#8:** nessuna schermata di risultato trasmessa (nero → menu). UNKNOWN corretto.
- **#10:** la sconfitta è reale e il banner è dentro la finestra, ma il matcher frozen lo supera in un frame solo. La result policy richiede più supporto e non è stata modificata. Recuperarla richiederebbe una revisione della policy o dei template, da fare separatamente sui 49 artifact frozen.

## 8. Prestazioni

- Detection da 267 s a **369 s** (+38%), sempre **2,2× più veloce** del tradizionale (796 s).
- Monitor: +1098 righe reali; lo scarto 204 → 222 s tra due run con le stesse 14 639 righe indica circa ±10% di rumore.
- Refinement risultati: da 426 a 575 s di finestre (93 → 131 s), per le finestre di recupero più lunghe.
- **Processi FFmpeg:** 81. Il recupero non ne aggiunge, perché le righe sono lette in-process.

## Problemi aperti

- **Altri VOD:** validato su un solo VOD lungo (più videoprova nei test). Il tetto di 10 s del recupero deriva dalla sequenza di fine partita di Hearthstone, ma va osservato su altri VOD.
- **Profili scritti dai test:** le run dei test scrivono profili fittizi (`video.mp4`, circa 6 KB) in `docs/performance_reports/`. È un comportamento preesistente di `report_directory()` in checkout sorgente con test senza `save=False`. Non li ho cancellati.
