# FFmpeg Frame Access — microbenchmark mirato

Data: 2026-10-12. Nessuna modifica al detector o al codice production. Nessuna analisi integrale.

## Funzionamento attuale

- `FFmpegFrameSampler.sample()` avvia un processo per finestra, con seek **prima dell'input**:
  `-copyts -start_at_zero -ss START -t DURATA -noautorotate -i VIDEO`.
- Filtri: `select='isnan(prev_selected_t)+gte(t-prev_selected_t,PERIODO)',scale=W:H,format=bgr24,showinfo`.
  Seguono `-fps_mode passthrough -threads 1 -pix_fmt bgr24 -f rawvideo pipe:1`.
  A 1 FPS, PERIODO è circa 1 secondo. Il parametro threads è dopo l'input: non va interpretato come imposizione di un decoder a un solo thread.
- **Non usa il filtro fps**: select conserva frame sorgente reali; showinfo fornisce i PTS originali relativi al video. Anche fps, se usato come filtro, ridurrebbe l'output, non eliminerebbe la necessità di decodifica dei frame interdipendenti.
- Select precede scale: evita resize/conversione/trasferimento dei frame scartati, ma FFmpeg decodifica ancora la porzione attraversata. Il seek accurato può decodificare anche il preroll dal keyframe precedente; non è un seek keyframe-only.
- Due reader con code limitate leggono raw BGR e PTS. Cancellazione e chiusura anticipata terminano e raccolgono il processo.
- `FrameSamplingService.get_many()` ordina/deduplica le richieste, consulta la cache selettiva e raggruppa i miss: gap massimo 3 s, span massimo 24 s. Richieste distanti 30 s diventano già seek singoli.
- Il batch `sample_at()` sceglie il primo frame reale in ogni finestra [t, t+0.1 s), mantenendo i PTS; decodifica anche gli intervalli fra richieste dello stesso batch. Fallimenti del batch hanno il fallback singolo esistente; la cancellazione non avvia il fallback.

## Metodo

Sorgente: `videoprova.mp4`, 3749.999667 s, 1920×1080, H.264/60 FPS secondo i metadati già disponibili. FFmpeg 8.0.1 essentials.

Solo tre finestre: **600–661, 1500–1561, 2900–2961 s**, cioè 183 s distinti, circa il 4.88% del video. Due ripetizioni, ordine dei metodi invertito nella seconda.

Output BGR 960×540. Cache applicativa dei frame disabilitata; nessun asset scritto. Cache del sistema operativo non svuotata: queste misure **non sono un benchmark di disco a cache fredda**. Probe iniziale 0.0686 s escluso dai confronti; metadati riutilizzati. Nessun matcher, ASR, provider, GPU o parallelismo aggiunto.

- Sequenziale: intera finestra di 61 s a 1 FPS.
- Sparse: tre richieste a +0/+30/+60 s con seek accurati indipendenti.
- Batch distante: stesse tre richieste, raggruppamento forzato soltanto nel benchmark.
- Vicine: tre richieste a +0/+1/+2 s, seek indipendenti oppure batch predefinito.

## Risultati misurati

Totali sulle 3 finestre × 2 ripetizioni. La mediana è il tempo per una finestra/terna di richieste, non per singolo frame.

| Metodo | Tempo totale | Mediana per caso | Processi FFmpeg | Frame restituiti |
|---|---:|---:|---:|---:|
| Sequenziale attuale 1 FPS | 8.993 s | 1.545 s | 6 | 366 |
| Seek accurati ogni 30 s | 1.892 s | 0.323 s | 18 | 18 |
| Batch forzato con gap 30 s | 8.713 s | 1.515 s | 6 | 18 |
| Tre frame vicini, seek singoli | 1.994 s | 0.328 s | 18 | 18 |
| Tre frame vicini, batch esistente | 1.318 s | 0.217 s | 6 | 18 |

Il recupero sparse costa **78.96% in meno, circa 4.75× più rapido**, rispetto a leggere le stesse finestre a 1 FPS. Restituisce però molti meno campioni: **non è una misura di accelerazione del detector a recall invariato**.

Il batch distante risparmia processi ma continua ad attraversare circa 60 s di video: migliora soltanto del 3.12% sul sequenziale. Per frame vicini, il batch esistente risparmia **33.89%** rispetto ai seek singoli.

Per posizione, tempi medi delle due ripetizioni:

| Inizio finestra | Sequenziale | Sparse 30 s | Batch distante | Vicini singoli | Vicini batch |
|---|---:|---:|---:|---:|---:|
| 600 s | 1.375 s | 0.295 s | 1.322 s | 0.328 s | 0.216 s |
| 1500 s | 1.573 s | 0.323 s | 1.516 s | 0.348 s | 0.233 s |
| 2900 s | 1.549 s | 0.328 s | 1.517 s | 0.321 s | 0.210 s |

### Avvio e correttezza

- Ritorno della chiamata Popen: circa **3.55 ms medi** nei 18 seek sparse; non comprende l'intera inizializzazione del decoder.
- Primo frame delle sei letture sequenziali: **96.8–103.0 ms**, inclusi avvio, seek, preroll, decode, filtri e consegna. Questi costi non sono isolati fra loro.
- Ogni terna sparse costa circa 0.315 s medi, circa 105 ms per richiesta includendo cleanup e overhead del servizio.
- **72/72 frame richiesti dai quattro percorsi alternativi** hanno PTS, dimensioni e SHA-256 dei pixel identici ai corrispondenti frame della lettura sequenziale. Nessuna richiesta mancante. Esempio: richieste 600, 630, 660 → PTS 600, 630, 660.
- L'equivalenza riguarda questi campioni CFR. Non dimostra equivalenza universale per ogni codec, VFR, offset o seek frazionari.

## Decisione implementativa

**Nessuna modifica production necessaria in questo intervento.** Il servizio esistente sceglie già i due percorsi risultati migliori: seek indipendenti per richieste lontane e piccoli batch per quelle vicine. Allargare indiscriminatamente il raggruppamento sarebbe controproducente. Non è emersa una nuova ottimizzazione sicura da applicare al servizio.

Aggiunti soltanto:
- `scripts/benchmark_frame_access.py`: benchmark limitato, riproducibile, senza detector.
- `docs/performance_reports/ffmpeg_frame_access.json`: misure per run, PTS e hash dei frame campionati.
- Questo report.

Il percorso sequenziale, le cache esistenti e tutte le modifiche precedenti del working tree sono preservati.

## Strategia consigliata per una futura ricerca 30 → 15 → 7.5 s

1. Usare `get_many()` per una ricognizione sparse a 30 s: i seek possono evitare la lettura continua del video.
2. Richiedere soltanto i nuovi timestamp intermedi degli intervalli sospetti; riusare dalla cache gli estremi già disponibili.
3. Mantenere inizialmente seek distinti a 15/7.5 s. Il punto di pareggio per quei gap non è stato misurato qui; non estendere automaticamente i batch.
4. Usare batch locali per frame ravvicinati e il sampler sequenziale/ROI già esistente per finestre dense di refinement.
5. Valutare il costo totale includendo recuperi, fallback, refinement e recall evento per evento prima di sostituire il detector. Nessuna ricerca adattiva implementata ora.

Due frame distanti e simili non escludono una transizione intermedia invisibile. Questo benchmark risolve la scelta del percorso di accesso, non il problema algoritmico del recall.

## Verifiche e limiti

Comandi eseguiti con l'interprete del progetto:

~~~powershell
python scripts/benchmark_frame_access.py "<Downloads>/videoprova.mp4" --output docs/performance_reports/ffmpeg_frame_access.json
python -m pytest -o addopts= -q tests/test_gameplay_sampler.py tests/test_shared_analysis.py -k "frame or batch or sampler"
python -m ruff check scripts/benchmark_frame_access.py
python -m mypy --follow-imports=silent scripts/benchmark_frame_access.py
~~~

**27 passed, 0 failed, 0 skipped, 16 deselected**; ruff e mypy passati sul solo nuovo script. I test comprendono equivalenza batch/seek su fixture CFR/VFR, cache, fallback e cancellazione. Nessuna suite completa, GUI smoke, analisi integrale o render.

Non misurati: CPU interna del decoder, numero esatto di frame internamente decodificati, keyframe/GOP per ogni seek, separazione pura fra I/O e decode, disco freddo, qualità thumbnail a risoluzione originale, costo dei futuri raffinamenti 15/7.5 s. Codec/GOP, disco e concorrenza esterna possono cambiare il punto di pareggio. Nessuna estrapolazione prestazionale al VOD da sette ore.
