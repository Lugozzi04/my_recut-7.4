"""Fail before cleaning a release target when a pinned runtime dependency is absent."""
from __future__ import annotations

import importlib.metadata
import sys
from collections.abc import Callable
from pathlib import Path


def dependency_errors(
    requirements: Path,
    lookup: Callable[[str], str] = importlib.metadata.version,
) -> list[str]:
    errors: list[str] = []
    for raw in requirements.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            errors.extend(dependency_errors(requirements.parent / line[3:].strip(), lookup))
            continue
        if "==" not in line:
            errors.append(f"Requirement must be exactly pinned: {line}")
            continue
        package, expected = line.split("==", 1)
        try:
            installed = lookup(package)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"{package}=={expected}: not installed")
            continue
        if installed != expected:
            errors.append(f"{package}: expected {expected}, installed {installed}")
    return errors


def main() -> int:
    requirements = Path(__file__).resolve().parents[1] / "requirements-build.txt"
    errors = dependency_errors(requirements)
    if errors:
        print("Build dependency check failed:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print("Pinned build and runtime dependencies verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
