# Iteration 6 — Result decision policy (screen confirmation ≠ WIN/LOSS)

9 ottobre 2026. Strategia state-aware invariata (monitor 1 FPS, trigger board/weak, finestre −2/+5 s a 60 FPS, ROI 144×87 grayscale, stessi 4 template di74, stesso pack VS). Cambia solo la **decisione finale** sui frame già misurati nelle finestre risultato. Nessun nuovo template, nessun PyTorch, nessun gameplay cut. 73.mp4 non è stato letto prima dell'unico blind run.

## Policy precedente (iteration 5)

Per ogni frame: `score ≥ 0.85` e `margin ≥ 0.04` → osservazione; poi cluster per classe (gap 0.5 s), accettato con ≥2 PTS distinti. Un'unica soglia assoluta faceva DUE lavori: dire "c'è una schermata risultato" e "quale". Frame reali ma sotto 0.85 venivano scartati prima ancora del clustering.

## Nuova policy — `analysis/gameplay/hearthstone/result_decision.py`

```text
frame in finestra 60 FPS → ResultEvidence(V, D)        (solo se max(V,D) ≥ 0.65)
→ cluster temporale class-agnostic (gap ≤ 0.5 s, PTS duplicati contati una volta)
→ RESULT_SCREEN confermato se ≥ 2 PTS distinti, span ≥ 1/120 s     altrimenti NO_RESULT
→ peak = frame con max(V,D) massimo nel cluster
→ WIN/LOSS = argmax(V, D) del peak
   UNKNOWN se margin del peak < 0.04 o se la classe opposta ha ≥ 2 frame di supporto propri
→ 1 GameEvent per cluster → deduplicazione episodio (invariata, 45 s, barriere VS/board) → GameAssembler (invariato)
```

|Parametro|Valore|Provenienza|
|---|---:|---|
|`evidence_score`|0.65|`weak_score` del monitor, congelato in iteration 5 prima del blind|
|`minimum_observations`|2|supporto ordinario esistente (2 PTS distinti)|
|`minimum_span_s`|1/120|invariato|
|`cluster_gap_s`|0.5|`result_gap_s` esistente|
|`minimum_peak_margin`|0.04|`margin` del profilo esistente|

**Zero nuovi valori numerici.** Tutti i parametri sono riusati da configurazioni congelate prima del blind iteration 5; i dati 74+86 sono serviti a verificare che questi valori siano sostenibili, non a sceglierli tra alternative. La soglia profilo 0.85 non è più un gate di accettazione nella strategia state-aware (resta nel profilo e nel metodo `ResultTextDetector.event`, usato solo dal tooling sperimentale).

Classi: nessuna nuova classe oltre `ResultDecisionPolicy` (parametri), `ResultEvidence` (score di un PTS) e `ResultDecision` (esito + motivo + frame + peak). Funzioni pure `cluster_evidence`, `decide_cluster`, `decide_results`; nessuna dipendenza Qt. `ANALYZER_VERSION` → `hearthstone-state-aware-v2` (cache invalidata); la policy è registrata in `settings.result_decision` e ogni decisione in `metadata.result_decisions` / `rejected_events`.

### Representative peak

Frame con il massimo `max(V,D)` nel cluster (pareggio → il più precoce). Nessuna media o mediana: i dati non giustificano altro, e il picco è anche il frame meno affetto da fade/movimento. Il timestamp dell'evento resta il primo PTS del cluster; `peak_timestamp` il picco.

### Frame transitori

Non si fa argmax per frame. I frame dove vince la classe sbagliata hanno score bassi (massimo osservato 0.511) e restano sotto il floor 0.65, quindi non entrano neppure nel cluster. Anche se un singolo frame transitorio superasse il floor, la classe è decisa dal peak; solo un supporto opposto di ≥2 frame con margin ≥0.04 rende l'evento UNKNOWN (conflitto reale, mai osservato).

### Hard negatives

Non raggiungono il floor: nessun cluster, nessuna conferma. Un margine alto con score basso (es. V=0.10, D=0.60) non conta, perché la conferma usa lo score, non il margine.

### Single-frame policy

Non necessaria e rimasta disabilitata, separata, non collegata alla strategia state-aware. Un cluster con un solo PTS è sempre `NO_RESULT/insufficient_support`; `minimum_observations` < 2 è rifiutato dal validatore.

## Evidenza calibration (solo 74 + 86)

Artifact riusati (nessun VOD rilanciato per lo studio): `calibration/final_representation_study_reviewed.json` (2276 frame nativi 60 FPS, 1511 revisionati, label override 86_hard_08 già applicato), `calibration/new_86_60/gameplay_analysis.json` e `calibration/new_74_60/gameplay_analysis.json` (score testo 1 FPS su tutto il VOD), `86/ground_truth_manual.json`, `74/calibration_annotations.json`, `calibration/score_tables.json`. Studio riproducibile: `python tools/gameplay/result_decision_study.py` → [calibration_study.json](calibration_study.json) e fixture test `tests/fixtures/gameplay/result_decision_calibration_traces.json`. Lo studio esegue il codice implementato, non una copia.

|Popolazione|Frame|max(V,D) massimo|≥ 0.65|
|---|---:|---:|---:|
|Hard negatives revisionati (24 gruppi)|377|0.478|0|
|86 in-game, 1 FPS (GT completa)|6837|0.334|0|
|86 menu prima della prima partita|539|0.437|0|
|74 in-game, 1 FPS (partite candidate verificate)|5028|0.349|0|
|74 menu iniziali|168|0.330|0|
|Frame risultato etichettati dove vince la classe sbagliata|41|0.511|0|

|Eventi risultato (cluster)|Valore|
|---|---|
|Tracce risultato replayate|20: 14 di86 + 6 di74; 16 indipendenti dai template, 4 finestre 74 contengono il frame sorgente di un template|
|Corrette con la nuova policy|20/20, 22 cluster classificati, 0 UNKNOWN|
|Peak score min / mediana|0.902 / 0.993|
|Peak margin min / mediana|0.476 / 0.603|
|Frame di supporto per cluster, min / mediana|6 / 36|
|Frame post-risultato 86 a 1 FPS fra 0.65 e 0.85|21 (banner reali scartati dalla vecchia soglia frame-level)|
|Frame post-risultato 86 con classe sbagliata e score ≥ 0.5|0|

Lettura: separazione di ~0.17 fra il negativo più alto (0.478) e il floor, e ~0.25 fra il floor e il picco risultato più debole. Per-frame argmax sui frame etichettati è giusto 1093/1134: per questo la classe si decide sul picco dell'evento. Limite: 8 LOSS e 14 WIN indipendenti, tutte italiane e con layout simile; frame correlati, non migliaia di esempi indipendenti.

## Calibration end-to-end con la nuova policy (prima del freeze)

|VOD|Esito|Note|
|---|---|---|
|86 ([report](cal_86/gameplay_analysis.json), [evaluation](cal_86/gameplay_evaluation.json))|14/14 complete, WIN/LOSS 14/14, LOSS recall/precision 100%/100%, false LOSS 0, extra result events 0|1 cluster `insufficient_support` (1 PTS, 5286.5 s, coda di una LOSS reale già rilevata), 4m07s|
|74 ([report](cal_74/gameplay_analysis.json))|12 partite, 10 WIN / 2 LOSS, 24 eventi identici (tipo e tempo a 0.1 s) all'iteration 5|0 rifiuti, 3m59s|

Nessun parametro modificato dopo queste due run.

## Test

`tests/test_gameplay_result_decision.py` (25): strong WIN/LOSS, LOSS sotto la vecchia soglia ma supportata, transitori con classe sbagliata (incl. V=0.2299/D=0.2296 e V=0.3408/D=0.4211), hard negatives reali, margin alto con score basso, frame singolo, PTS ripetuto, peak ambiguo e pareggio → UNKNOWN, supporto contrapposto → UNKNOWN, cluster class-agnostic e gap, metadata evento, due cluster della stessa animazione → un evento dopo dedup, validazione parametri, replay di tutte le tracce 74/86. `tests/test_gameplay_state_aware.py` (+4 integrazione con l'analyzer reale e sampler fixture): LOSS sotto soglia classificata, ambiguous/unsupported → partita senza risultato, cluster duplicati → 1 evento. Full pytest 793 passed, 1 skipped (googleapiclient opzionale, preesistente); ruff e mypy PASS.

## Freeze e blind 73

[Manifest](freeze/freeze_manifest.json): 48 hash (codice analysis/gameplay, profilo e asset iteration5, pack VS iteration4, tool studio, studio calibration, ground truth held-out, test). Guardia `blind_started.json` che rifiuta un secondo avvio; audit hash dopo il blind.

**Unico blind run** (attempt 1, exit 0, 3m29s wall), stesso profilo/pack/held-out GT dell'iteration 5, nessun parametro cambiato dopo. Audit post-blind: 48/48 hash invariati. [Confronto macchina](blind73_comparison.json), [analisi](blind73/gameplay_analysis.json), [evaluation](blind73/gameplay_evaluation.json), [log](blind73.log).

|Metrica (11 partite held-out)|Iteration 5|Iteration 6|
|---|---:|---:|
|Complete|10/11|**11/11**|
|Complete recall / precision|90.91% / 100%|**100% / 100%**|
|LOSS recall|75% (3/4)|**100% (4/4)**|
|LOSS precision|100%|100%|
|False LOSS|0|0|
|VS recall|100%|100%|
|VICTORY recall|100% (7/7)|100% (7/7)|
|Pairing errors / unmatched|0 / 0|0 / 0|
|Classificazione|10/10|11/11|
|Extra result events|0|0|
|Start MAE / max|0.224 / 0.767 s|0.224 / 0.767 s|
|Result MAE / max|0.078 / 0.334 s (10)|0.076 / 0.384 s (11)|
|Runtime analisi|207.3 s (3m27s)|208.3 s (3m28s)|
|Confronti template inclusi VS|475,848|475,848 (identici)|

La LOSS 5010.7: cluster di 2 PTS (5010.716 → 5010.783), peak DEFEAT 0.8272 / VICTORY 0.3681, margin 0.459, supporto opposto 0 → `event_peak_argmax`. Nessuna decisione rifiutata nel blind. Le altre 10 partite hanno lo stesso esito e gli stessi tempi di inizio dell'iteration 5.

Performance: la policy lavora solo sui frame già campionati nelle 18 finestre (159 s, 9421 frame ROI, 1830 sopra il floor). Nessuna passata full-video, nessun decode o matching aggiuntivo: frame e confronti sono identici all'iteration 5.

## Limiti e failure residui

- Nessun failure sul blind 73. La LOSS recuperata ha però il supporto MINIMO (2 PTS a 60 FPS, picco 0.827): un'animazione ancora più breve o un campionamento a 30 FPS la renderebbe `insufficient_support` (rifiuto conservativo, mai una LOSS falsa).
- Onestà sul protocollo: i numeri del failure 73 erano noti prima di questa iterazione. Per questo nessun valore è stato scelto: tutti e cinque i parametri sono riusati da configurazioni congelate prima del blind iteration 5 e verificati solo su 74+86. Questo blind conferma la policy, ma 73 non è più un test vergine per lo strato decisionale; la prossima conferma indipendente deve venire da un VOD nuovo (es. il VOD dei riferimenti in templates/, una volta documentata la provenienza).
- Campione calibration piccolo: 8 LOSS e 14 WIN indipendenti, solo client italiano e layout simile. Lingue, skin, overlay e crop diversi richiedono nuovi dati.
- La separazione negativi/floor (0.478 vs 0.65) è misurata su schermate reali del gioco, non garantita per contenuti arbitrari dello stream (overlay con la parola "Vittoria", ecc.).

## Decisione

**RESULT_POLICY_READY** — la conferma della schermata e la classificazione sono separate, nessun nuovo parametro, calibrazione 74+86 senza errori, blind 73 11/11 con LOSS precision 100% e zero false LOSS. Prima di usare l'output per tagliare automaticamente le LOSS si raccomanda una validazione su almeno un VOD nuovo e indipendente. Gameplay cuts non integrati.
