"""Check the frozen CLI's exit status and real redirected JSON stdout."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    executable = str(Path(sys.argv[1]).resolve())
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="auto-cutter-cli-cwd-") as cwd:
        for command in (["--help"], ["presets", "--json"], ["jobs", "--json"]):
            result = subprocess.run(
                [executable, *command], cwd=cwd, capture_output=True,
                encoding="utf-8", timeout=60, creationflags=flags,
            )
            if result.returncode:
                raise RuntimeError(f"Packaged CLI {command[0]} failed: exit {result.returncode}")
            if "--json" in command:
                payload = json.loads(result.stdout)
                if payload.get("ok") is not True:
                    raise RuntimeError(f"Packaged CLI {command[0]} returned failure JSON")
            elif "usage:" not in result.stdout.lower():
                raise RuntimeError("Packaged CLI help did not reach stdout")
    print("Packaged CLI help, presets and jobs verified from another cwd.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
