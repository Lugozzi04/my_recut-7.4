# Calibration source 74.mp4 — Iteration 3

Stato: NEEDS_MORE_CALIBRATION_DATA per abilitare la single-frame result policy.
Il workflow di calibrazione è implementato ed eseguito realmente.

- calibration_annotations.json: 7 finestre anchor +4 finestre negative, etichette
  visive provvisorie. Comprendono 1 VS, 3 WIN e2 LOSS distinti; la prima LOSS è
  campionata due volte per confrontare ingresso e fase stabile.
- capture_source_annotations.json: snapshot delle annotazioni al momento della
  cattura del template, conservato per verificare l'hash della provenienza.
- calibration_before.json: score con il pack calibration-only di iteration2.
- calibration_candidate.json: stessi frame con una nuova patch DEFEAT da74.
- candidate_pack/: pack sperimentale esplicito, non installato come default.
- previous_calibration_candidate_scores.json: solo negativi e WIN del calibration
  subset0..1400 di73, riusati da cache; nessun frame delle11 validation games.
- contact_sheets/: tutti i frame diagnostici salvati, raccolti per finestra.
- debug_frames/: un picco misurato per finestra; non è automaticamente un positivo.
- single_frame_policy.disabled.json: regole entrambe null; nessuna soglia inventata.
- quality_gate.json: test e controlli finali.

I 219 JPEG singoli del run di calibrazione sono locali sotto
.pytest_cache/gameplay-real-74/iteration3/final_before/calibration_frames;
le contact sheet ne conservano l'evidenza visiva. I contatori di performance nei
report si riferiscono al run originale, non al sottoinsieme archiviato.
Sono report di calibrazione, NON una valutazione completa delle partite di74.
I picchi selezionati con il matcher sono stati ispezionati visivamente prima di
attribuire etichette. Le altre transizioni esplorate non diventano GT completa.

Vedi ../../GAMEPLAY_CALIBRATION.md per riprodurre il workflow e
../../GAMEPLAY_VALIDATION_73.md per l'Iteration3 e lo stato frozen della validation.
