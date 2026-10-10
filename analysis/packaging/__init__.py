"""AI YouTube Packaging: titles, rendered thumbnails and creative concepts for an edited video.

Strict separation of responsibilities:

* local analysis (timeline, transcript, audio, frames, webcam, games, highlights)
  produces *evidence* with ids and timestamps;
* the reasoning agent (the user's `claude` or `codex` CLI) interprets that evidence
  and proposes a VideoStory and paired title/thumbnail concepts, referencing ids only;
* ThumbnailRenderer turns a validated, structured composition plan into PNG/JPEG files.

The agent never receives file-system access, never runs code and never produces
pixels; every claim it makes must point at evidence ids that ReCut validates.
See docs/PACKAGING.md.
"""
from __future__ import annotations

from .models import PackagingError

__all__ = ["PackagingError"]
