# Auto Cutter da terminale

La CLI usa `PipelineManager`, lo stesso downloader Twitch, l'analisi Classic, i preset dell'editor, `ExportWorker` e la validazione FFprobe della GUI. Nessun comando CLI crea `QApplication`, `QWidget`, `MainWindow` o QWebEngine. QtCore può leggere le impostazioni già salvate dall'editor.

Da source usa l'interprete della tua virtual environment e `python main.py`. L'eseguibile confezionato usa gli stessi argomenti con `AutoCutter.exe`. L'avvio senza argomenti apre la GUI; `--smoke-test` mantiene il comportamento esistente.

```powershell
python main.py --help
python main.py presets
python main.py jobs
python main.py jobs --job JOB_ID --json

python main.py process "C:\Videos\video à 🎮.mp4" `
    --preset "Balanced (Default)" `
    --output-dir "D:\Videos\ReCut"

python main.py vod "https://www.twitch.tv/videos/1234567890" `
    --start 01:20:00 `
    --end 03:45:00 `
    --preset "Balanced (Default)" `
    --output-dir "D:\Videos\ReCut" `
    --youtube

python main.py resume JOB_ID
```

`--preset` è facoltativo: viene scelto il preset predefinito del catalogo reale. Il job conserva una copia della configurazione, delle impostazioni export e dell'intervallo; `resume` non rilegge un preset modificato successivamente. Il nome `YouTube` funziona se esiste nel tuo catalogo, senza introdurre un secondo catalogo CLI.

## Comandi e help

```text
AutoCutter [--json] [--version] COMMAND

presets [--json]
jobs [--job JOB_ID] [--json]
process LOCAL_FILE [OPTIONS]
vod TWITCH_URL [OPTIONS]
resume JOB_ID [--dry-run | --youtube-video-id VIDEO_ID] [--json]
auth twitch [--client-id ID] [--status | --logout] [--json]
auth youtube [--client-config CLIENT_JSON] [--status | --logout] [--json]
```

`AutoCutter COMMAND --help` mostra l'help specifico, compresi `auth twitch --help` e `auth youtube --help`.

Opzioni `process` e `vod`:

| Opzione | Comportamento |
|---|---|
| `--start TIME` | Inizio; default 0. |
| `--end TIME` | Fine; default fine del media. |
| `--duration TIME` | Durata da `--start`, alternativa esclusiva a `--end`. |
| `--preset NAME` | Preset GUI reale. |
| `--output PATH` | File di destinazione; senza estensione usa il container salvato. |
| `--output-dir DIR` | Directory alternativa, esclusiva a `--output`. |
| `--quality NAME` | `maximum`, `very_high`, `high`, `balanced`, `compact`. |
| `--youtube` | Upload del video validato con privacy PRIVATE. Richiede OAuth configurato. |
| `--title TITLE` | Titolo YouTube; default titolo del media. |
| `--description TEXT` | Descrizione YouTube, facoltativa. |
| `--thumbnail PATH` | Thumbnail facoltativa, applicata dopo la creazione del video. |
| `--keep-source` | Mantiene il download Twitch anche dopo DONE. |
| `--force` | Nuovo job e permesso esplicito di sostituire la destinazione richiesta. Non sostituisce mai source o progetto. |
| `--dry-run` | Solo metadata, intervallo, preset e destinazione; nessun job/download/analisi/export/upload. |
| `--json` | Un solo risultato JSON su stdout; progress e log su stderr. |

TIME accetta secondi (`90`), minuti/secondi (`01:30`) e ore/minuti/secondi (`01:02:30`), anche secondi frazionari. Internamente vengono usati float in secondi. Start deve precedere la fine; end deve superare start. Un piccolo superamento della durata viene limitato con avviso; superamenti maggiori vengono rifiutati. Per un file locale lo stesso cut engine esclude le parti fuori intervallo, senza scaricare o riscrivere il source.

```powershell
python main.py vod "https://www.twitch.tv/videos/1234567890" `
    --start 1:00:00 --duration 2:00:00 `
    --preset "Balanced (Default)" --youtube --dry-run --json
```

La destinazione definitiva viene riservata e persistita prima del rendering. Se occupata, viene usato `video_2.mp4` o un suffisso successivo. Il temporaneo export e il manifest risiedono accanto al file finale, anche se lo stato del job è su un altro drive. Un file esistente viene riusato soltanto con manifest e validazione coerenti; l'esistenza da sola non prova il completamento.

## Autenticazione

Twitch usa il device flow esistente dell'app:

```powershell
python main.py auth twitch --client-id TWITCH_APPLICATION_CLIENT_ID
python main.py auth twitch --status
python main.py auth twitch --logout
```

Il client ID viene risolto da flag, `AUTO_CUTTER_TWITCH_CLIENT_ID`, poi impostazione GUI `automation/twitch/client_id`. La CLI mostra il codice temporaneo di autorizzazione e apre la pagina Twitch. I token non vengono emessi nei risultati JSON. Il logout elimina le credenziali locali; gli epoch del servizio invalidano le richieste in corso.

YouTube richiede un client OAuth Desktop e il consenso manuale una prima volta:

```powershell
python main.py auth youtube --client-config "C:\Private Config\desktop-client.json"
python main.py auth youtube --status
python main.py auth youtube --logout
```

La configurazione dettagliata, i limiti Google e le garanzie di upload sono descritti in [YOUTUBE.md](YOUTUBE.md). Ogni upload automatico resta PRIVATE. Non serve una thumbnail per iniziare. Il comando di elaborazione non sostituisce la configurazione OAuth manuale.

## Resume, cancellazione e pulizia

Ctrl+C viene propagato al token dello stage attivo, che termina FFmpeg o interrompe l'upload; il job diventa CANCELLED dopo la chiusura dello stage. `resume JOB_ID` riparte con il preset salvato e riutilizza gli artifact validi. Le letture metadata precedenti alla creazione del job possono attendere il timeout di rete corrente prima di osservare la cancellazione.

Un executor possiede un lock OS per job; job differenti possono procedere. In startup il manager ricostruisce le queue, compreso READY_EXPORT. Gli upload con ID YouTube salvato riutilizzano quell'ID. Un esito upload sconosciuto conserva la sessione e richiede reconciliation, impedendo un nuovo insert automatico che potrebbe duplicare il video.

Per `upload_outcome_unknown`, apri YouTube Studio e controlla manualmente che il video PRIVATE corrisponda al job: contenuto, titolo e momento del caricamento. Solo dopo aver identificato quel video puoi registrarne l'ID:

```powershell
python main.py resume JOB_ID --youtube-video-id "abcdefghijk"
```

L'ID deve avere 11 caratteri e viene persistito atomicamente prima del resume. Il programma riutilizza quel video e può completare la thumbnail, senza un nuovo `videos.insert`. Questa opzione è una conferma esplicita del video controllato dall'utente: non usare un ID casuale o appartenente a un altro video. Un ID già registrato diverso viene rifiutato. `--youtube-video-id` e `--dry-run` sono incompatibili; il resume automatico normale non richiede questa opzione.

I file locali originali vengono sempre mantenuti. Il download Twitch posseduto dal job può essere eliminato solo dopo DONE e solo se percorso e fingerprint coincidono; `--keep-source` lo preserva. Source e artifact validi vengono mantenuti su failure; temporanei invalidi vengono ripuliti. Dopo la pulizia di un source scaricato il progetto può essere offline: usa `--keep-source` se vuoi continuare a modificarlo nell'editor.

## Path e configurazione

I path non dipendono dalla directory corrente o da `_MEIPASS`. Su Windows i default sono:

| Dato | Percorso |
|---|---|
| Preset | `%APPDATA%\Auto Cutter\Auto Cutter\presets.json`; fallback al preset incluso se non ancora salvato. |
| Impostazioni GUI/CLI | QSettings storico `HKEY_CURRENT_USER\Software\Auto Cutter\Auto Cutter`; con override config viene usato INI. |
| Stato job | `%LOCALAPPDATA%\Auto Cutter\pipeline\jobs.json`. |
| Progetti automatici CLI | `pipeline\artifacts` accanto allo stato job. |
| Credenziali | `%LOCALAPPDATA%\Auto Cutter\credentials`, con protezione DPAPI Windows. |
| Download | `%USERPROFILE%\Videos\Auto Cutter\Automation\<request fingerprint>`. |
| Cache | `%LOCALAPPDATA%\Auto Cutter\cache`. |
| Log sessione | `%APPDATA%\Auto Cutter\Auto Cutter\session_logs`. |
| Output | `%USERPROFILE%\Videos\Auto Cutter\Exports`, oppure destinazione CLI. |

Override disponibili: `AUTO_CUTTER_CONFIG_DIR`, `AUTO_CUTTER_DATA_DIR`, `AUTO_CUTTER_PIPELINE_STORE`, `AUTO_CUTTER_CREDENTIALS_DIR`, `AUTO_CUTTER_DOWNLOAD_DIR`, `AUTO_CUTTER_CACHE_DIR`, `AUTO_CUTTER_OUTPUT_DIR`. I path con spazi, Unicode e output su altro drive sono supportati.

## Exit code

| Codice | Significato |
|---|---|
| 0 | Successo, incluso dry-run/help. |
| 2 | Argomenti, intervallo, preset o job ID invalidi. |
| 10 | Twitch/metadata/download. |
| 20 | Analisi. |
| 30 | Export/validazione. |
| 40 | Upload YouTube, incluso esito sconosciuto che richiede reconciliation. |
| 50 | Autenticazione o dipendenze OAuth mancanti. |
| 60 | Filesystem/storage. |
| 70 | Cancellazione. |
| 75 | Job già posseduto da un altro executor. |

Con `--json`, il successo contiene `ok`, `command` e il risultato; una failure contiene `ok:false`, `error.code`, `error.message` e `exit_code`. I messaggi vengono redatti e nessun token o URL di sessione resumable viene incluso nel job o nell'output.
