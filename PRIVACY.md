# Privacy

Auto Cutter has no proprietary account, purchase, licensing, or Microsoft Store
integration. The manual editor processes media locally and does not include
analytics or telemetry.

The optional Twitch automation connects to Twitch OAuth and API endpoints only
after the user configures it. When enabled, it checks the connected channel for
new VODs; yt-dlp resolves the selected VOD stream and FFmpeg downloads the
chosen interval from Twitch's media network. Twitch access and refresh tokens
are encrypted for the current Windows user with DPAPI. Downloaded video ranges
are stored in the folder selected by the user.

An explicitly requested YouTube upload sends the validated exported video,
title, optional description and optional thumbnail to the official YouTube Data
API. Auto Cutter requests desktop OAuth consent and always uploads PRIVATE;
publication is a manual action in YouTube Studio. Google access/refresh tokens
and resumable-upload session checkpoints are encrypted for the current Windows
user with DPAPI in the dedicated credentials directory. They are excluded from
job JSON, application logs and diagnostics. Logout invalidates in-flight local
authentication sessions and removes stored credentials.

Application settings, recovery snapshots, caches, and logs are stored in the
current Windows user profile. Diagnostics bundles are created only when the
user requests one. They include runtime metadata and redacted logs, but users
should review a bundle before sharing it.

The optional AI setup script downloads pinned Python packages and model
components from their respective package/model providers. Those downloads are
not required for the core editor.

No automatic update channel is enabled until a signed release manifest and
download endpoint are configured.
