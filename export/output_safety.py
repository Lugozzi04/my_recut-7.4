from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path


class OutputSourceCollisionError(ValueError):
    """A delivery artifact would replace media or its project."""


def paths_refer_to_same_file(left: str | Path, right: str | Path) -> bool:
    first = Path(left).expanduser().resolve()
    second = Path(right).expanduser().resolve()
    if os.path.normcase(str(first)) == os.path.normcase(str(second)):
        return True
    try:
        return first.samefile(second)
    except (FileNotFoundError, NotADirectoryError):
        return False


def ensure_output_is_safe(output: str | Path, protected: Iterable[str | Path]) -> Path:
    target = Path(output).expanduser().resolve()
    for source in protected:
        if paths_refer_to_same_file(target, source):
            raise OutputSourceCollisionError(f"Output would replace a source or project: {target}")
    return target


def choose_output_path(
    requested: str | Path,
    *,
    force: bool = False,
    protected: Iterable[str | Path] = (),
    companion_suffix: str | None = ".automation.json",
) -> Path:
    """Choose a safe name; the coordinator persists/reserves it before rendering."""
    protected_paths = tuple(protected)
    requested_path = ensure_output_is_safe(requested, protected_paths)
    candidate = requested_path
    index = 2
    while True:
        ensure_output_is_safe(candidate, protected_paths)
        companion = Path(str(candidate) + companion_suffix) if companion_suffix else None
        if companion is not None:
            ensure_output_is_safe(companion, protected_paths)
        occupied = os.path.lexists(candidate) or (companion is not None and os.path.lexists(companion))
        if force or not occupied:
            return candidate
        candidate = requested_path.with_name(f"{requested_path.stem}_{index}{requested_path.suffix}")
        index += 1


def atomic_promote_output(temporary: Path, output: Path, *, overwrite: bool) -> None:
    """Publish a validated file in its own directory without a no-force race."""
    if overwrite:
        os.replace(temporary, output)
    elif os.name == "nt":
        # Unlike POSIX rename, Windows rename fails when the destination exists.
        os.rename(temporary, output)
    else:
        # Linking publishes the complete file and atomically refuses an existing name.
        os.link(temporary, output)
        temporary.unlink()
