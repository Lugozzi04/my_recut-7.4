# Auto Cutter / ReCut — report di implementazione

Verifica locale del 4 ottobre 2026. La copia locale è stata mantenuta come source
of truth; le modifiche già presenti sono state preservate. La repository di
confronto ReCut CLI non è stata modificata.

Il percorso **source CLI locale** è verificato fino a DONE. Il codice YouTube è
implementato e testato con mock. Il criterio finale completo resta aperto per
l'installazione dell'SDK Google, il rebuild dell'eseguibile e le prove con rete e
account reali: questo ambiente ne impedisce l'esecuzione. Il vecchio bundle non
è stato sostituito con un build incompleto.

## 1. Architettura

```text
GUI TwitchIntegration ─┐
                       ├─ PipelineManager / PipelineStore / lock per job
CLI / PipelineRuntime ─┘          │
                    metadata e intervallo numerico
                                 ↓
                 PipelineDownloadService / yt-dlp / FFmpeg
                                 ↓
                 PipelineAnalysisService / AudioAnalysisService
                 PresetRepository / Classic / cut engine
                                 ↓
                       progetto .autocutter
                                 ↓
                 PipelineExportService / ExportWorker / FFprobe
                                 ↓
                    output validato + manifest
                                 ↓
             DONE oppure PipelineUploadService / YouTube PRIVATE
                                 ↓
                         video id persistito / DONE
```

La CLI chiama gli stessi servizi sincroni che le queue della GUI eseguono in
background. Non crea MainWindow, QApplication, QWidget o QWebEngine. QtCore
legge le impostazioni esistenti. Il dispatch precede gli import GUI pesanti.

Preset, normalizzazione e calcolo Classic sono stati estratti dai metodi GUI.
I nuovi job conservano lo snapshot del preset, delle impostazioni export e
dell'intervallo. I vecchi job senza snapshot mantengono il profilo Classic
precedente e la serializzazione versione 1. Il renderer esistente è stato
riusato; non è stato introdotto un secondo downloader o cut engine.

## 2. Bug corretti

- **Audio cancellation:** prima si termina FFmpeg, con attesa e kill di fallback;
  il thread che legge possiede la chiusura delle pipe. Il canceller non chiude
  BufferedReader mentre un altro thread è dentro read(). Test con subprocess
  realmente bloccato verificano termine del reader, raccolta del processo e retry.
- **Lifecycle GUI:** la chiusura aspetta i QThread attivi, ne conserva i riferimenti
  e riprova tramite timer; evita la distruzione di un thread ancora in esecuzione.
- **Export corrotto/retry:** un file esistente è una cache solo dopo verifica di
  manifest, firma, fingerprint, durata e stream. Il rendering usa un temporaneo
  accanto alla destinazione, validato prima della promozione. Il journal di
  promozione distingue il file posseduto dal job da un file estraneo apparso
  durante il rendering. Senza force la promozione non rimpiazza file estranei.
  Source, progetto e relativi hard link sono protetti.
- **Twitch logout race:** generation locale più epoch persistente sotto lock OS;
  logout invalida l'epoch prima dell'eliminazione. OAuth, validazione e refresh
  precedenti non possono salvare token o identità dopo logout, anche fra GUI e
  CLI. Migrazione opaca dei vecchi token cifrati nella directory credentials.
- **Startup recovery:** ricostruzione queue, inclusa READY_EXPORT, e controllo
  degli artifact con i validator condivisi. Gli executor vivi non vengono
  interrotti da un'altra istanza.
- **Retry/backoff:** errore transiente e marker del retry sono persistiti nella
  stessa transazione. Un crash durante l'attesa è recuperabile; esauriti tre
  tentativi dello stage il job resta FAILED per retry esplicito. CANCELLED resta
  fermo. Nessun retry automatico per errori permanenti.
- **Probe cancellation:** le FFprobe della pipeline, comprese quelle nei worker
  di export parallelo, osservano il token ogni 50 ms. Cleanup termina e raccoglie
  il processo; la cancellazione attraversa i validator senza essere scambiata
  per corruzione. La queue rimane utilizzabile e la CLI restituisce 70.
- **Segreti:** redazione prima di scrivere log/diagnostics e prima di troncare
  messaggi; copre token, cookie, header, URL di sessione, traceback su stderr e
  messaggi JSON annidati. Anche gli errori download persistiti vengono redatti.
- **Exit code:** errori OAuth 50 e storage 60 conservati attraverso l'orchestratore;
  output posseduto da un altro executor restituisce 75 e resta READY_EXPORT.

## 3. File esistenti modificati

Questo elenco riguarda il lavoro corrente, compresi file che erano già presenti
localmente ma non tracciati da Git. Il diff rispetto a HEAD contiene anche
modifiche anteriori a questo intervento.

```text
.github/workflows/quality.yml
CHANGELOG.md
PRIVACY.md
README.md
THIRD_PARTY_NOTICES.md
requirements.txt
main.py
analysis/audio_service.py
automation/analyzer.py
automation/downloader.py
automation/exporter.py
automation/manager.py
automation/models.py
automation/store.py
automation/twitch_auth.py
build/AutoCutter.spec
build/build.ps1
export/exporter.py
ui/main_window.py
ui/twitch_integration.py
utils/crash_handler.py
utils/diagnostics.py
utils/ffmpeg.py
utils/runtime_paths.py
tests/test_main_entrypoint.py
tests/test_pipeline_exporter.py
tests/test_twitch_main_window_flow.py
```

## 4. File creati

```text
analysis/classic.py
automation/cli.py
automation/execution.py
automation/locking.py
automation/paths.py
automation/retry.py
automation/runtime.py
automation/uploader.py
build/check_dependencies.py
build/verify_packaged_cli.py
core/config.py
core/presets.py
export/output_safety.py
integrations/__init__.py
integrations/youtube/__init__.py
integrations/youtube/auth.py
integrations/youtube/uploader.py
utils/console.py
utils/credential_files.py
utils/ffconcat.py
utils/redaction.py
docs/CLI.md
docs/PIPELINE_ARCHITECTURE.md
docs/YOUTUBE.md
docs/IMPLEMENTATION_REPORT.md
tests/test_cli.py
tests/test_credential_migration.py
tests/test_download_artifact_recovery.py
tests/test_export_recovery_regressions.py
tests/test_ffconcat_paths.py
tests/test_gui_close_lifecycle.py
tests/test_packaging_cli.py
tests/test_pipeline_blocking_regressions.py
tests/test_pipeline_concurrency.py
tests/test_pipeline_paths.py
tests/test_pipeline_resume.py
tests/test_pipeline_retry.py
tests/test_pipeline_runtime.py
tests/test_probe_cancellation.py
tests/test_secret_redaction.py
tests/test_shared_presets.py
tests/test_twitch_auth_epochs.py
tests/test_twitch_ui_races.py
tests/test_twitch_upload_integration.py
tests/test_youtube_auth.py
tests/test_youtube_upload.py
```

## 5. CLI e help

Da source, con l'interprete della venv selezionata:

```powershell
python main.py --help
python main.py presets
python main.py jobs
python main.py jobs --job JOB_ID --json
python main.py process "C:\Videos\vod.mp4" --preset "Balanced (Default)"
python main.py vod "https://www.twitch.tv/videos/1234567890" `
    --start 01:20:00 --end 03:45:00 `
    --preset "Balanced (Default)" --output-dir "D:\Videos\ReCut"
python main.py vod "https://www.twitch.tv/videos/1234567890" `
    --start 00:42:00 --end 03:15:00 `
    --preset "Balanced (Default)" --youtube
python main.py resume JOB_ID
python main.py auth twitch --client-id TWITCH_CLIENT_ID
python main.py auth youtube --client-config "C:\Private\desktop-client.json"
```

`AutoCutter.exe` utilizza gli stessi argomenti dopo il rebuild. L'avvio senza
argomenti apre la GUI. L'eseguibile attualmente preesistente non è stato aggiornato.

Help globale verificato:

```text
usage: AutoCutter [-h] [--json] [--version]
                  {presets,jobs,process,vod,resume,auth} ...

presets     List the actual GUI presets.
jobs        List persisted jobs, or inspect one.
process     Analyze, cut, export and validate a local video.
vod         Download and process a selected Twitch VOD interval.
resume      Resume or retry using the saved preset snapshot.
auth        Authenticate Twitch or YouTube, inspect status, or log out.
```

Help specifico: `process --help`, `vod --help`, `resume --help`, `jobs --help`,
`presets --help`, `auth twitch --help`, `auth youtube --help` dopo `main.py`.

Process/vod supportano start, end o duration, preset, output o output-dir,
quality, youtube, title, description, thumbnail, keep-source, force, dry-run e
json. TIME accetta 90, 01:30, 01:02:30 e secondi frazionari. End e duration sono
esclusivi. Start/end diventano float; piccoli superamenti della durata vengono
limitati con avviso, gli altri rifiutati. Il preset YouTube funziona se esiste nel
catalogo reale: non è stato inventato un catalogo separato.

Dry-run risolve metadata e mostra intervallo richiesto/effettivo, preset,
destinazione e privacy senza creare job o elaborare media. JSON produce un unico
oggetto su stdout; progress/log vanno su stderr. Ctrl+C cancella lo stage e
persiste CANCELLED quando esiste un job.

Exit code: **0** successo, **2** argomenti/preset/job, **10** Twitch/download,
**20** analisi, **30** export/validazione, **40** upload, **50** autenticazione,
**60** filesystem/storage, **70** cancellato, **75** executor/output occupato.
Sintassi completa e opzioni: [CLI.md](CLI.md).

## 6. Path Windows

| Dato | Default |
|---|---|
| Risorse read-only | Cartella dell'app; `_MEIPASS` nel bundle. |
| Config | QSettings storico `HKCU\Software\Auto Cutter\Auto Cutter`; con override config usa INI. |
| Preset | `%APPDATA%\Auto Cutter\Auto Cutter\presets.json`, fallback al catalogo incluso. |
| Twitch auth | `%LOCALAPPDATA%\Auto Cutter\credentials\twitch-token.bin`, epoch/lock accanto. |
| YouTube OAuth | Stessa directory, `youtube-token.bin`, epoch/lock/refresh lock. |
| Sessioni upload | `credentials\youtube-uploads\<hash job>.bin`, cifrate. |
| Job | `%LOCALAPPDATA%\Auto Cutter\pipeline\jobs.json`, store lock e directory lock per job accanto. |
| Progetti CLI | `pipeline\artifacts\<request fingerprint>.autocutter`. |
| Download CLI | `%USERPROFILE%\Videos\Auto Cutter\Automation\<prime 24 cifre del request fingerprint>`; GUI conserva cartella scelta. |
| Cache | `%LOCALAPPDATA%\Auto Cutter\cache`. |
| Log | `%APPDATA%\Auto Cutter\Auto Cutter\session_logs`; crash in `%LOCALAPPDATA%\Auto Cutter\crash_logs`. |
| Temporaneo export | File partial univoco nella directory del final output; manifest/journal sibling. |
| Final output CLI | `%USERPROFILE%\Videos\Auto Cutter\Exports` o destinazione esplicita. |
| Final output GUI | Cartella download scelta; comportamento legacy conservato. |

Override: AUTO_CUTTER_CONFIG_DIR, AUTO_CUTTER_DATA_DIR,
AUTO_CUTTER_PIPELINE_STORE, AUTO_CUTTER_CREDENTIALS_DIR,
AUTO_CUTTER_DOWNLOAD_DIR, AUTO_CUTTER_CACHE_DIR, AUTO_CUTTER_OUTPUT_DIR.
Twitch supporta inoltre AUTO_CUTTER_TWITCH_TOKEN_FILE. I path non dipendono da
CWD; token/job/download non vengono salvati automaticamente nel bundle.

Spazi, apostrofi, Unicode, accenti ed emoji sono testati anche con FFmpeg reale.
I nomi remoti sono singoli componenti sanitizzati, compresi traversal, drive
prefix, caratteri Windows illegali, reserved names e punti/spazi finali.
Collisioni producono suffissi numerici; force abilita sostituzione esplicita.
Temporaneo e final sono sullo stesso filesystem anche con job su C: e output
su D:. Non è stata eseguita una prova fisica su D:, ma posizione del temporaneo
e promozione sono coperte da test con directory separate.

## 7. Recovery per stato

La GUI ricostruisce le queue all'avvio. La CLI `resume JOB_ID` riprende il job
indicato tramite lo stesso manager. Le queue in memoria non sono fonte di stato.

| Stato reale | Comportamento dopo riavvio/resume |
|---|---|
| DISCOVERED | Metadata del VOD già nel job; attende configurazione intervallo. |
| WAITING_RANGE | Attende selezione GUI; la CLI crea richieste complete atomicamente e non resta qui. |
| DOWNLOADING | Recupera lo stage; valida un source già completo e lo riusa. Parziale invalido ripulito e download pulito; non è supportato append/resume HTTP del vecchio parziale. |
| ANALYZING | Riusa progetto/cache coerente con source e snapshot; altrimenti rianalizza. |
| READY_EXPORT | Viene riaccodato. Reconciliation ripara gli input configurati mancanti/invalidi tornando allo stage necessario. |
| EXPORTING | Valida output/manifest durabili; se completo avanza senza render. In caso contrario riparte da READY_EXPORT. |
| READY_UPLOAD | Con delivery YouTube opt-in va in upload. Con opt-out esplicito valida e conclude DONE. Legacy senza flag resta compatibile in READY_UPLOAD. |
| UPLOADING | Riusa video id o sessione cifrata, interroga i byte remoti; esito sconosciuto blocca un nuovo insert. |
| DONE | Nessuna queue; CLI verifica output, oppure riusa video id già completato. |
| FAILED | Errori permanenti/tentativi esauriti richiedono retry esplicito; interrupted o retry transiente pendente vengono recuperati. |
| CANCELLED | Non viene riaccodato automaticamente; resume esplicito riparte conservando artifact/video id. |

JSON e credenziali sono scritti con temporaneo sibling, flush/fsync e replace.
Un store corrotto viene segnalato e non sovrascritto. Lock OS per job più lock
transazionale breve per lo store e lock per output permettono job differenti
in parallelo. Il processo terminato rilascia automaticamente l'execution lock.

I source originali locali restano sempre. Un download posseduto dal job viene
eliminato solo dopo DONE se percorso e fingerprint coincidono; keep-source lo
mantiene. Su failure non si elimina il source. Dopo cleanup il progetto può
essere offline: usare keep-source per continuare l'editing.

## 8. YouTube

Abilitare YouTube Data API v3 nel progetto Google Cloud, configurare consenso e
test user, creare client **Desktop app** e tenere il JSON fuori dalla repo.
Eseguire auth youtube una volta e completare il consenso nel browser. Il flusso
usa loopback 127.0.0.1, state, PKCE e offline access; i refresh token sono cifrati
DPAPI e riutilizzati. Logout invalida anche le richieste in corso.
[OAuth desktop ufficiale](https://developers.google.com/identity/protocols/oauth2/native-app).

Ogni insert usa **PRIVATE** e notifySubscribers false. Titolo da flag o metadata
sanitizzati; descrizione e thumbnail facoltative. Thumbnail viene applicata
dopo aver persistito l'id, e può essere ritentata senza reinserire il video.

Upload resumable con chunk 8 MiB. Session URL/byte/checkpoint sono cifrati;
Location viene salvata prima del primo PUT e finalizing prima dell'ultimo.
Resume interroga la sessione originale. Retry HTTP fino a cinque per richiesta
con backoff massimo 32 s; lo stage condiviso aggiunge due tentativi. Errori
permanenti non vengono ritentati automaticamente.
[Protocollo resumable ufficiale](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol).

Appena arriva il video id è persistito atomicamente, prima di progress, thumbnail
o DONE, anche per una risposta tardiva dopo cancel. Un id già presente esclude
un altro videos.insert. Se una risposta finale è persa e la sessione poi scade,
l'API non garantisce un inserimento idempotente tramite chiave applicativa:
upload_outcome_unknown richiede controllo manuale, senza nuovo insert automatico.
Anche un 308 anomalo che conferma tutti i byte resta finalizing.

Dopo aver identificato il video PRIVATE corrispondente in Studio:

```powershell
python main.py resume JOB_ID --youtube-video-id "ID_DI_11CHR"
```

Non usare id casuali e non cancellare checkpoint per forzare upload. Dettagli:
[YOUTUBE.md](YOUTUBE.md).

## 9. Dipendenze aggiunte

In requirements.txt, senza aggiornamenti indiscriminati:

| Pin | Scopo |
|---|---|
| google-auth==2.48.0 | Credenziali e refresh OAuth Google. |
| google-auth-oauthlib==1.5.0 | Flusso desktop OAuth ufficiale/PKCE. |
| google-api-python-client==2.201.0 | API YouTube e upload resumable ufficiale. |

Compatibili con Python 3.10. requirements-dev/build includono requirements.txt;
yt-dlp esistente è stato riusato. Trasporti Google/OAuth sono dipendenze transitive.
Notices/licenze Apache-2.0 e metadata sono inclusi nella configurazione del build.

## 10. Test e comandi eseguiti

**446 raccolti: 445 passed, 0 failed, 1 skipped in 51.18 s.** La base iniziale
locale aveva 182 casi incluso smoke; i casi precedenti passano nella suite completa.
L'unico skip è test_official_sdk_boundary_with_fake_transport_when_dependency_is_installed:
googleapiclient.http non è installato. Non è un upload reale saltato in CI.

```powershell
.\.venv310\Scripts\python.exe -m pytest -o addopts= -q -rs
.\.venv310\Scripts\python.exe -m ruff check .
.\.venv310\Scripts\python.exe -m mypy automation analysis/audio_service.py analysis/classic.py analysis/cancellation.py core/project_file.py core/project_session.py core/config.py core/presets.py integrations utils/codec_detection.py utils/runtime_paths.py utils/console.py utils/credential_files.py utils/ffconcat.py utils/redaction.py utils/ffmpeg.py utils/crash_handler.py utils/diagnostics.py build/check_dependencies.py build/verify_packaged_cli.py
.\.venv310\Scripts\python.exe -m compileall -q analysis automation core export integrations ui utils widgets main.py build/check_dependencies.py build/verify_packaged_cli.py
git diff --check
```

Ruff passa; mypy passa su **39 source file**; compileall e diff check passano
(Git segnala soltanto normalizzazione LF/CRLF). CI ora usa pytest per includere
anche i test parametrizzati/funzionali nuovi e controlla i moduli condivisi.

Copertura nuova: audio/pipe/processi reali bloccati, probe cancellate e retry,
race OAuth intra/inter-processo, store atomico/concorrenza, READY_EXPORT recovery,
artifact mancanti/corrotti, provenienza/promozione export, path/collisioni,
preset snapshot, CLI/import headless/JSON/exit code, OAuth/upload/checkpoint/retry,
thumbnail, id persistito e assenza di insert duplicato.

Prova reale **da un CWD diverso**: media di 3 s con spazi/apostrofo/accento/emoji,
range 0.2–2.8 s, Balanced, due cut, export h264_amf/chunked_parallel, FFprobe,
DONE, source originale mantenuto, resume exit 0. Ripetuta dopo il fix probe,
job `5c07b166274b40ebbf5ae03b736c5386`; process/resume/GUI smoke exit **0**.
Output di verifica mantenuto in:

```text
%TEMP%\autocutter-final-e2e-2055l4h2\other output directory\montaggio è pronto 🎬.mp4
```

Help, presets, jobs, process dry-run e JSON sono stati eseguiti anche come veri
subprocess CLI. L'integration test FFmpeg già presente è stato esteso alla
pipeline locale analisi → progetto → export → validazione → DONE.

## 11. Build

| Verifica | Esito |
|---|---|
| Source GUI `python main.py --smoke-test` | PASS, ambiente dati/config isolato. |
| Source CLI senza display | PASS, compreso media reale e resume. |
| PowerShell build syntax/contract | PASS. |
| Build release `build/build.ps1 -SkipDependencyInstall` | BLOCCATO al controllo dipendenze, prima di cleanup/PyInstaller. |
| Nuovo PyInstaller bundle | Non prodotto: OAuthlib installato 0.4.6 invece di 1.5.0; google-api-python-client assente. |
| Packaged GUI/CLI smoke nuovo bundle | Non eseguito, dipende dal rebuild. |

Lo spec include Google auth/client, discovery document statici, metadata/licenze
e yt-dlp. console=True mantiene stdout/stderr e Ctrl+C nella CLI; GUI stacca
soltanto la console privata di un avvio Explorer, preservando terminali esistenti.
Il build verifica pin prima di eliminare vecchi target, verifica i path di cleanup
e rifiuta junction/symlink. Smoke GUI e CLI del bundle sono predisposti con dati
isolati; CLI verifica help/presets/jobs JSON da un altro CWD.

L'installazione pip è stata tentata ed è fallita con **WinError 10013**; i pin
esatti non erano disponibili neppure nella cache offline. Non è stato aggirato
il blocco di rete. Da terminale normale del PC:

```powershell
.\.venv310\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv310\Scripts\python.exe -m pytest -o addopts= -q -rs
powershell -NoProfile -ExecutionPolicy Bypass -File .\build\build.ps1 -SkipDependencyInstall
```

Il parametro PythonExe permette un interprete/venv differente. Il runtime non
dipende dal percorso della venv di sviluppo.

## 12. Limiti residui e criterio di completamento

- Installare le dipendenze Google corrette; eseguire il test SDK e build/smoke
  confezionati prima di dichiarare completato il criterio dell'eseguibile.
- Twitch reale: tentativo dry-run fermo sui metadata gql.twitch.tv con socket
  WinError 10013, exit 10, nessun job creato. Download/export online non verificati.
  L'URL numerico d'esempio non è stato validato come VOD attualmente disponibile.
- YouTube reale: richiede client OAuth e consenso manuale iniziale. Nessun upload
  reale è stato eseguito. Refresh/account, interruzioni di rete reali, permessi
  thumbnail e quota/audit Google devono essere verificati sul progetto dell'utente.
- Subscriber-only Twitch VOD non verificati; autenticare la discovery non prova
  da solo che la risoluzione media abbia accesso a un VOD subscriber-only.
- La cancellazione durante richieste metadata/OAuth di rete può attendere il
  timeout corrente. Letture FFmpeg audio, rendering e probe della pipeline sono
  invece collegate alla cancellazione. Non è stato simulato ogni crash hardware.
- DPAPI produzione richiede Windows; su altri sistemi serve un backend sicuro,
  senza fallback plaintext. I test iniettano un cipher sintetico.
- Un esito remoto upload sconosciuto può richiedere reconciliation manuale:
  impedire duplicati ha precedenza su un nuovo insert automatico.
- La GUI mantiene il workflow manuale e riprende job YouTube opt-in; questa
  versione non aggiunge un nuovo pannello GUI di configurazione upload. OAuth e
  opt-in YouTube sono disponibili dalla CLI.

Il codice e la pipeline locale sono verificati. **Non considero chiuso l'intero
criterio finale finché SDK, nuovo eseguibile e verifiche online/account non sono
stati completati.**
