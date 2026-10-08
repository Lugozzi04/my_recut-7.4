# Evidenze della validazione di 73.mp4

Esito: **NEEDS_TUNING**. Questi dati documentano il prototipo; il pack non è pronto per la produzione. Il VOD originale non è incluso.

Ground truth manuale: tests/fixtures/gameplay/73_held_out_ground_truth.json. Le prime 3 partite (0–1400 s) sono riservate alla calibrazione; le 11 successive (7 WIN, 4 LOSS) alla validation. Le annotazioni mantengono complete=false e la tolleranza è 5 s: tutte le metriche sono provvisorie.

- baseline_analysis.json / baseline_evaluation.json: scansione precedente, coarse 1 FPS e fine 6 FPS su tutte le classi.
- dense20_original_analysis.json / dense20_original_evaluation.json: scan ROI 20 FPS con il pack originale.
- final_analysis.json / final_evaluation.json: scan ROI 20 FPS con il pack congelato e refinement nativo locale.
- baseline_diagnostics.json / final_diagnostics.json: score, PTS, cause dei mancati anchor e verifica del banner VICTORY duplicato.
- debug_frames/: immagini dei casi diagnosticati.
- calibration_summary.json: verifica sui soli 3 risultati di calibrazione.
- template_pack/templates_original.json: configurazione originale, scale 0.9 / 1 / 1.1.
- template_pack/templates.json: configurazione congelata, scale risultato 0.9–1.1 con passo 0.025.
- I 3 PNG sono identici fra i due pack. Soglia 0.92 e margine 0.04 invariati.
- quality_gate.json: conteggi, ambiente e comandi dei test.

Il [report principale](../../GAMEPLAY_VALIDATION_73.md) contiene metriche, confronto per partita, errori, performance e limiti.

Riproduzione dalla root, con il VOD disponibile localmente:

```powershell
python -m analysis.gameplay.hearthstone.debug_analyze "<percorso>/73.mp4" `
  --template-pack "docs/gameplay_validation/73/template_pack/templates.json" `
  --ground-truth "tests/fixtures/gameplay/73_held_out_ground_truth.json" `
  --output-dir ".pytest_cache/gameplay-real-73/reproduction" `
  --coarse-fps 1 --fine-fps 6 --result-fps 20 --result-refine-fps 120 --no-cache
```

Il refinement a 120 FPS riguarda solo le finestre ±0.15 s dei candidati instabili. Legge i frame sorgente disponibili, senza generarne di nuovi; sul VOD sono circa 60 FPS.

Per confrontare il sampling legacy usare --result-fps 0 con il pack originale. Per disabilitare il refinement usare --result-refine-fps 0. Le versioni e la fase dei bin delle esecuzioni archiviate sono descritte nel report: i confronti storici non sono nuove calibrazioni.
