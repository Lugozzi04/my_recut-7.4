# Iteration 2 — evidenze congelate

Esito: **NEEDS_TUNING**. 9/11 complete + 2 partial; VS 11/11, risultati 9/11, LOSS recall 50%, false LOSS 0. Tempo 26m38s. Vedi [report completo](../../../GAMEPLAY_VALIDATION_73.md).

Il pack contiene soltanto frame dei primi tre giochi (0–1400 s). Le 11 partite successive non hanno fornito template o threshold. Configurazione e codice sono congelati in frozen_manifest.json prima della singola nuova esecuzione full-VOD.

- failure_analysis_before.json: cause e score già osservati prima delle modifiche.
- calibration_summary.json: distribuzioni per classe, negativi e benchmark thread.
- calibration_analysis/evaluation.json: audit reale dei soli 1400 s di calibrazione.
- before_evaluation.json: vecchia analisi con le nuove metriche complete/partial.
- after_analysis/evaluation.json: nuova validation congelata.
- run_integrity.json: tempo wall e hash verificati prima/dopo.
- quality_gate.json: suite, lint, tipi, GUI smoke.

Le annotazioni restano provvisorie. Questa validation è stata riusata per failure analysis, pur essendo esclusa dal fit; servirà un VOD indipendente per un nuovo test cieco. Il pack è sperimentale e viene selezionato esplicitamente; risorse di default ed editor non sono stati modificati.

Dalla root, con l’interprete dell’ambiente selezionato:

```powershell
python -m analysis.gameplay.hearthstone.debug_analyze "$env:USERPROFILE\Downloads\73.mp4" `
  --template-pack "docs/gameplay_validation/73/iteration2/template_pack/templates.json" `
  --ground-truth "tests/fixtures/gameplay/73_held_out_ground_truth.json" `
  --output-dir ".pytest_cache/gameplay-real-73/iteration2/reproduction" `
  --coarse-fps 1 --fine-fps 6 --result-fps 20 --result-refine-fps 120 `
  --candidate-similarity 0.80 --max-game-duration 1800 --no-cache --debug-frames
```

Nessun gameplay cut viene generato. 0.80 attiva solo refinement; per gli eventi restano 0.92, margine 0.04 e supporto temporale distinto.
