# Privacy

Auto Cutter has no account, purchase, licensing, or Microsoft Store integration.
The core editor processes media locally and does not include analytics or
telemetry.

Application settings, recovery snapshots, caches, and logs are stored in the
current Windows user profile. Diagnostics bundles are created only when the
user requests one. They include runtime metadata and redacted logs, but users
should review a bundle before sharing it.

The optional AI setup script downloads pinned Python packages and model
components from their respective package/model providers. Those downloads are
not required for the core editor.

No automatic update channel is enabled until a signed release manifest and
download endpoint are configured.
