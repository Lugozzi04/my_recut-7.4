# YouTube PRIVATE delivery

Auto Cutter uses the official YouTube Data API and Google Python libraries. Every
new video has `privacyStatus="private"` and `notifySubscribers=False`. There is
no automatic public/unlisted option. Publish manually in YouTube Studio after
reviewing the video, title, description, thumbnail and channel settings.

## Configure your own Desktop OAuth client

1. Create/select a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **YouTube Data API v3** for that project.
3. Configure the OAuth consent screen. If the application is in Testing, add the
   Google account that owns the intended YouTube channel as a test user.
4. Create an OAuth client with application type **Desktop app**. Download its
   JSON file into a private user directory outside the repository and exports.
5. Install the pinned runtime dependencies using your selected Python interpreter:

   ```powershell
   python -m pip install -r requirements.txt
   ```

6. Connect the channel interactively once:

   ```powershell
   python main.py auth youtube --client-config "C:\Users\YOUR_USER\Documents\Google\desktop-client.json"
   ```

   The packaged equivalent is `AutoCutter.exe auth youtube --client-config PATH`.
   Complete the Google consent screen in the browser opened by the command.
   Desktop OAuth uses an exclusively bound `127.0.0.1` listener on an available
   port, a random state, PKCE and offline access. No embedded browser or Studio
   automation is involved. The listener times out after five minutes and Ctrl+C
   closes it. Callback URLs/codes are never logged.

7. Check credentials or disconnect:

   ```powershell
   python main.py auth youtube --status
   python main.py auth youtube --logout
   ```

`AUTO_CUTTER_YOUTUBE_CLIENT_CONFIG` may specify the initial client JSON instead
of `--client-config`. Once connected, access/refresh token, client id and desktop
client secret are saved together in encrypted credentials; subsequent runs do
not need the downloaded JSON. A future interactive reauthorization can also
reuse that saved client configuration. Refresh is automatic when needed.

The implementation follows Google's
[Desktop OAuth guidance](https://developers.google.com/identity/protocols/oauth2/native-app).
An OAuth project in Testing may have limited refresh-token lifetime; configure
the consent screen appropriately for sustained unattended use. A real account
must grant the requested `youtube.upload` scope. Channel permissions, OAuth
verification, quotas and API project audit requirements remain Google account
and project configuration responsibilities. See the official
[videos.insert reference](https://developers.google.com/youtube/v3/docs/videos/insert).

## Start an unattended job

```powershell
python main.py vod "https://www.twitch.tv/videos/1234567890" `
    --start 00:42:00 --end 03:15:00 `
    --preset "Balanced (Default)" --youtube `
    --title "Review this private recording"
```

```powershell
python main.py process "D:\Videos\vod.mp4" `
    --preset "Balanced (Default)" --youtube `
    --thumbnail "D:\Videos\thumbnail.png"
```

Upload begins only after export artifact validation. A missing thumbnail never
blocks upload. Explicit titles must have 1–100 characters and no angle brackets;
titles derived from remote metadata are cleaned and truncated. Description is
optional. Thumbnail upload is performed after the video id is durable; failures
retain that id and resume retries the thumbnail without inserting another video.
Custom thumbnails can require additional channel permissions.

## Writable paths and protection

On Windows the default directory is:

```text
%LOCALAPPDATA%\Auto Cutter\credentials\
    youtube-token.bin
    youtube-token.bin.epoch
    youtube-token.bin.lock
    youtube-token.bin.refresh.lock
    youtube-uploads\<SHA256 of job id>.bin
```

`AUTO_CUTTER_CREDENTIALS_DIR` overrides the directory; alternatively
`AUTO_CUTTER_DATA_DIR` overrides its parent application-data directory. Resources
inside the repository or PyInstaller bundle are never writable credential roots.
Tokens and resumable session URLs are encrypted with the existing Windows DPAPI
abstraction for the current Windows user. Only a random, non-secret logout epoch
and lock markers are plaintext. Writes flush and fsync a temporary sibling file
before replacement. Logout advances the durable epoch before deleting the token;
an older GUI/CLI authorization or refresh response cannot resurrect it.

The production cipher currently requires Windows. Other platforms need a secure
credential backend before authenticating; they do not silently fall back to
plaintext. Mock tests inject a synthetic cipher and never use real credentials.
Moving DPAPI files to another Windows account does not transfer authorization.

Job JSON contains the YouTube video id, delivery metadata and an attempt marker.
It never contains OAuth tokens, Authorization headers or resumable session URLs.
Resumable checkpoints are retained on logout: they are bound to a hash of the
client id/refresh token and the export file's path/size/mtime. A changed account
or changed video cannot silently reuse a previous upload session.

## Resumable upload and retries

Uploads use the official client's `MediaFileUpload` and `HttpRequest.next_chunk`
with 8 MiB chunks (a multiple of 256 KiB). A narrow HTTP adapter persists the
session Location before the first media PUT, and marks the last chunk as
`finalizing` before sending it. After a restart or network error, the uploader
queries the session's authoritative byte range before sending more bytes.

Network failures, timeouts, HTTP 429, 500/502/503/504 and rate-limit 403 errors
have a bounded exponential backoff of at most five HTTP retries per chunk/request
and 32 seconds per pause. If those retries are exhausted on a temporary failure,
the shared stage policy allows two further stage attempts, resuming the existing
session rather than inserting another video. Cancellation is checked during
backoff; active sockets are shut down and
HTTP transport is closed. Permanent API errors and rejected authorization are
not retried indefinitely. Thumbnail retries operate on the existing video id.
The protocol follows Google's
[resumable upload guide](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol).

## Crash boundaries and duplicate prevention

1. **Before session initialization:** the manager atomically saves
   `upload_attempt_started=True`; the encrypted checkpoint records initialization
   before the POST. A crash can be reconciled without treating a missing checkpoint
   as a new job.
2. **After Location is returned:** its encrypted checkpoint is durable before the
   client can send the first media chunk. Resume queries this same session.
3. **Before the last media PUT:** `finalizing` is durable. A lost successful final
   response can be recovered by querying the session; its completion response
   supplies the original video id.
4. **As soon as a video id is returned:** `record_youtube_video_id` saves it to the
   atomic job store before progress, thumbnail, cleanup or DONE. If cancellation
   arrives during the last request, a late completion still saves the id while
   retaining CANCELLED.
5. **Job already has a video id:** no `videos.insert` is constructed or sent.
   With no pending thumbnail it needs neither another network upload nor another
   OAuth refresh; with a thumbnail it performs only that optional operation.

There is an unavoidable distributed boundary if the remote side completed but
the process lost both its response and an eventually expired session. The API
offers no application-supplied insertion idempotency key. Auto Cutter therefore
returns `upload_outcome_unknown` and refuses to insert again when initialization
is uncertain, a checkpoint is missing/corrupt after an attempt, the bound account
or export changed, or a finalizing session has expired. This deliberately
requires resolution rather than creating a duplicate. If a session expires while
the saved state confirms incomplete media (before finalizing), a subsequent
resume may safely create a fresh session.

For an unknown outcome, inspect YouTube Studio on the account used by the original
attempt. If the corresponding private video exists, provide its confirmed id
while resuming the job:

```powershell
python main.py resume JOB_ID --youtube-video-id "VIDEO_ID_11"
```

Run the API command with the same writable-state overrides used for the original
job. Do not guess an id or clear attempt markers/checkpoints to force a blind
retry. A user-authorized new job is appropriate only after determining the remote
outcome. API response loss, real network interruption, subscriber-only Twitch
VODs, real channel thumbnail permissions and quota/audit settings require real
account verification; no account or credentials are embedded in this repository.
