# Pack candidato: calibrazione esterna 74

NON installato come default. NON validato sulle 11 partite congelate di 73.
Base: copia byte-per-byte delle 6 patch calibration-only del pack iteration2.
Aggiunta: una sola patch DEFEAT, external74_defeat_stable.png, dal frame
6119.015333 di 74.mp4, ROI di riferimento [429,314,114,29].
Provenienza: companion .png.provenance.json e ../capture_source_annotations.json.
Quest'ultimo conserva l'esatto input al momento della cattura; le annotazioni
sono poi state estese in ../calibration_annotations.json con la seconda LOSS.

Template totali: 7 (VS=2, VICTORY=1, DEFEAT=4).
ROI, grayscale, soglie 0.92, margin 0.04 e scale sono invariati.
Varianti risultato precompilate: 45 invece di36; costo maggiore se attivato.
Fingerprint: 0eb1c441279a75fd2ea56e0dda96d547ebee65ee5119c51bde40723a5fa919c8.

La patch migliora il training fit della fase stabile: non è una prova di recall
su VOD nuovi. Non aggiungere template dai frame held-out di 73 e non attivare
una single-frame policy finché non sono stati raccolti esempi brevissimi reali
e hard negatives indipendenti. Nessun gameplay cut è prodotto.
