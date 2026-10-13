# Riconoscimento WIN/LOSS — risultati visibili in un solo frame

Data: 2026-10-13. VOD: `THEORYCRAFTING DAY 1.mp4`.
Stato: la conferma single-frame è attiva **solo in Adaptive V2**, che resta sperimentale; il detector tradizionale è invariato e resta il default.

## Esito

| | V2 prima | **V2 dopo** | Tradizionale |
|---|---|---|---|
| WIN / LOSS / UNKNOWN | 18 / 10 / 2 | **18 / 11 / 1** | 18 / 9 / 3 |
| Partite complete | 26 | **27** | 24 |
| Partita #10 (VS 10448.75) | UNKNOWN | **LOSS**, DEFEAT 11159.815 | UNKNOWN |
| Partita #8 (VS 9035.93) | UNKNOWN | **UNKNOWN** | UNKNOWN |
| Falsi WIN/LOSS | — | **0** | — |

## 1. Causa della mancata LOSS #10

Percorso: frame 60 FPS nella finestra risultato → template matching ROI (VICTORY/DEFEAT) → `ResultEvidence` se max ≥ 0.65 → cluster (gap 0.5 s) → conferma se **≥ 2 PTS distinti** → classe dal picco → deduplicazione → assembler → Hero Groups → GUI.

Il banner "Sconfitta!" a 11159.8 è in animazione e la scena cambia subito dopo. A 60 FPS:

```
11159.799 D 0.29 | 11159.815 D 0.898 (V 0.336) | 11159.832 D 0.59 | 11159.849 D 0.54 | 11159.865 V 0.14 ...
```

- Solo un PTS supera il floor 0.65, quindi la policy `result-decision-v1` lo scarta (`NO_RESULT / insufficient_support`). Il suo validatore vieta `minimum_observations < 2`.
- La finestra risultato conteneva il frame (corretto nell'audit precedente). Il problema è solo la regola di conferma.
- La vecchia eccezione `SingleFrameResultPolicy` appartiene all'analyzer denso precedente, è disabilitata e non è collegata al percorso state-aware.
- Il timestamp è usato solo nei test e nelle fixture, non nel codice di produzione.

## 2. Modifica

`SingleFrameConfirmation` (`analysis/gameplay/hearthstone/result_decision.py`), funzione pura. Si applica **solo** a un cluster di un PTS rifiutato per supporto insufficiente. La V2 rilegge ±0,25 s a 60 FPS attorno al candidato, perché i frame sotto il floor non vengono conservati.

Conferma soltanto se valgono tutte le condizioni:
1. **Picco ≥ 0.85** e massimo locale stretto in ±0,25 s.
2. **Classe opposta al picco ≤ 0.511**: picco non ambiguo.
3. **Vicini su entrambi i lati**, con almeno uno **della stessa classe > 0.511**: coerenza temporale a 60 FPS.
4. **Nessun vicino della classe opposta > 0.511**: nessun segnale contraddittorio.

Nessuna soglia nuova:
- 0.85 è la soglia di accettazione per frame dell'iteration 5;
- 0.511 è il punteggio più alto mai misurato su un frame che non è risultato, o è risultato della classe sbagliata, nello studio di calibrazione revisionato: hard negative 0.478, gioco/menu 0.437, classe sbagliata 0.511 (`state_aware/result_decision/calibration_study.json`).

Altrimenti il cluster resta `NO_RESULT`, con il motivo del rifiuto (`single_frame_*`).

Integrazione:
- **`state_aware.py`:** solo l'hook `_confirm_result_decisions`, che restituisce le decisioni invariate. Il comportamento del tradizionale non cambia: è verificato da un test sulla stessa finestra reale, dove il tradizionale dà ancora UNKNOWN.
- **`adaptive_v2.py`:** implementa l'hook. La regola entra nei settings, quindi nella chiave cache, e la versione passa a `hearthstone-adaptive-sparse-v2.3`.
- **Costo:** una lettura ROI di 0,5 s per ogni cluster di un frame. Sul VOD da 7 ore sono 3 letture da circa 31 frame ROI ciascuna, nell'ordine di 1 s su circa 369 s. Nessuna scansione globale; scheduler, sampling e PyAV non toccati.

## 3. Positivi e hard negative

**Stress test sulle tracce di calibrazione 74/86** (`result_decision_calibration_traces.json`): **ogni** frame viene valutato come se fosse l'unico candidato, con i suoi veri vicini a 60 FPS.

| Popolazione | Tracce | Frame | Conferme | Classe sbagliata |
|---|---:|---:|---:|---:|
| Hard negative revisionati | 24 | 377 | **0** | 0 |
| WIN | 11 | 888 | 20 frame; 11/11 tracce | **0** |
| LOSS | 9 | 756 | 13 frame; 9/9 tracce | **0** |

Anche se ogni banner fosse visibile in un solo frame, tutte le 20 partite di calibrazione resterebbero riconoscibili, e nessun negativo diventerebbe un risultato.

**Casi reali da un frame del VOD** (fixture `single_frame_result_traces.json`, tracce reali con etichette verificate a occhio):

| PTS | Natura | Esito |
|---|---|---|
| 11159.815 | "Sconfitta!" isolata (#10) | **DEFEAT confermata** |
| 15381.015 | coda reale del banner LOSS 15380.33 | DEFEAT, unita dalla deduplicazione alla LOSS esistente |
| 6392.682 | coda reale della LOSS 6391.98 (picco 0.711) | rifiutata (`peak_below_gate`), già coperta |

## 4. Falsi positivi

- **Nessun falso positivo** su negativi di calibrazione, tracce WIN/LOSS e VOD reale.
- **Partita #8:** nessun frame supera il floor (schermo nero, poi menu), quindi nessun candidato e la partita resta UNKNOWN. È verificato da un test sulla sorgente reale.

## 5. WIN/LOSS/UNKNOWN sull'intero VOD

Ricostruzione dal run V2 completo (stesse finestre ed evidenze), applicando le nuove decisioni ai 3 cluster da un frame e rieseguendo deduplicazione e `GameAssembler` di produzione:
- cambia **solo la #10**: UNKNOWN → LOSS (END 11159.815);
- le altre 29 partite, i pairing e i timestamp sono identici;
- 18 WIN, 11 LOSS, 1 UNKNOWN; eventi da 56 a 57.

Rispetto al tradizionale: le 18 WIN e le 9 LOSS del tradizionale sono tutte presenti con timestamp identico. In più ci sono le LOSS #13 (audit precedente) e #10, entrambe "Sconfitta!" verificate sui frame.

## 6. Classi e Hero Groups

- Hero Groups campiona dal nostro **ritratto inferiore** a START + 60…120 s. Per la #10 gli istanti di campionamento sono **identici** prima e dopo (verificato), quindi la classe resta warlock.
- Il risultato entra in Hero Groups solo nel conteggio delle vittorie del gruppo, non nella classificazione.
- Hunter, Warlock, Death Knight e le altre classi restano invariate; SIFT, clustering e correzioni manuali non sono stati toccati.

## 7. Test

- **`tests/test_gameplay_result_decision.py` (+12):**
  - #10 reale confermata, mentre la policy ordinaria resta `insufficient_support`;
  - code reali;
  - ogni regola di rifiuto (picco basso, ambiguo, isolato, vicino opposto, non massimo locale, vicinato incompleto);
  - nessun effetto su decisioni multi-frame;
  - stress test su tutte le tracce di calibrazione;
  - validazione dei parametri.
- **`tests/test_adaptive_v2.py` (+1, sorgente reale, saltato se il VOD manca):**
  - finestra #10: V2 → LOSS con `single_frame_local_peak`, tradizionale → UNKNOWN;
  - finestra #8: V2 → UNKNOWN, nessun evento risultato.
  - Il test **fallisce con l'hook disattivato** e passa con la correzione.
- **Suite pertinente** Gameplay / Adaptive / Shared / Hero Groups: **606 passed, 0 failed, 0 skipped**. Ruff e mypy puliti, `--smoke-test` exit 0.
- **Artifact frozen non-codice:** 23/23 identici. Cache reali non scritte.
- **Analisi integrale delle 7 ore:** non rieseguita, perché l'effetto è stato ricostruito con il codice di produzione sulle evidenze del run completo precedente.

## 8. Limiti

- **Calibrazione:** 20 partite di calibrazione più un VOD; tutte italiane, con lo stesso layout e template.
  - Il tetto 0.511 è misurato su schermate reali del gioco, non garantito per contenuti arbitrari dello stream (overlay con la parola "Sconfitta", replay di altri streamer).
  - Su nuovi VOD conviene controllare le decisioni con `result_acceptance = single_frame_local_peak` (sono registrate negli eventi).
- **Conferma minima:** un banner visibile in un frame **senza** alcun vicino della stessa classe sopra 0.511 resta UNKNOWN per scelta: l'evidenza non basta a distinguerlo da un falso positivo.
- **Tradizionale:** non beneficia della correzione (vincolo); su questo VOD continua a dare 3 UNKNOWN.
