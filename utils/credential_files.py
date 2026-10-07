from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path


def migrate_credential_file(legacy_path: Path, target_path: Path) -> bool:
    """Atomically publish opaque encrypted bytes without replacing newer data.

    The temporary file lives beside the destination, so migration also works
    when the user moves the credentials directory onto another volume.
    """
    legacy = Path(legacy_path)
    target = Path(target_path)
    if target.exists() or not legacy.is_file() or legacy.resolve() == target.resolve():
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.migration.tmp")
    try:
        with legacy.open("rb") as source, temporary.open("xb") as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            # Publishing via a hard link is atomic and refuses an existing
            # destination, including one another process just created.
            os.link(temporary, target)
        except FileExistsError:
            return False
        except OSError:
            if os.name != "nt":
                raise
            # Windows rename also refuses to replace an existing destination;
            # this handles filesystems that do not support hard links.
            try:
                os.rename(temporary, target)
            except FileExistsError:
                return False
        legacy.unlink(missing_ok=True)
        return True
    finally:
        temporary.unlink(missing_ok=True)
