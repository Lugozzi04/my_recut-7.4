"""Atomic, lightly compressed NumPy artifacts; no pickles and no full media payloads."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np


def atomic_save_arrays(target: Path, **arrays: Any) -> None:
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".npz", delete=False) as stream:
            temporary = Path(stream.name)
            # Default savez_compressed uses level 6. Selected frames prefer low
            # CPU overhead while keeping lossless values and NumPy compatibility.
            with ZipFile(stream, "w", compression=ZIP_DEFLATED, compresslevel=1) as archive:
                for name, value in arrays.items():
                    with archive.open(name + ".npy", "w", force_zip64=True) as member:
                        np.lib.format.write_array(member, np.asarray(value), allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
