from __future__ import annotations

import os


def quote_ffconcat_path(path: str | os.PathLike[str]) -> str:
    """Quote one path using FFmpeg's token syntax, not shell or SQL syntax."""
    value = os.fspath(path).replace("\\", "/")
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise ValueError("A concat media path cannot contain a newline or NUL.")
    return "'" + value.replace("'", "'\\''") + "'"
