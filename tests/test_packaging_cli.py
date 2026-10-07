from __future__ import annotations

import ctypes
import io
import importlib.metadata
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils.console import detach_gui_console


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dependency_checker", ROOT / "build" / "check_dependencies.py")
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class PackagingCliTests(unittest.TestCase):
    def test_missing_and_wrong_pins_block_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "runtime.txt").write_text("google-auth==2.48.0\nmissing==1.0\n", encoding="utf-8")
            (root / "build.txt").write_text("-r runtime.txt\npyinstaller==6.16.0\n", encoding="utf-8")

            def installed(name: str) -> str:
                if name == "missing":
                    raise importlib.metadata.PackageNotFoundError(name)
                return {"google-auth": "2.47.0", "pyinstaller": "6.16.0"}[name]

            errors = CHECKER.dependency_errors(root / "build.txt", installed)
            self.assertEqual(len(errors), 2)
            self.assertIn("installed 2.47.0", errors[0])
            self.assertIn("not installed", errors[1])

    def test_frozen_gui_detaches_only_its_own_console(self) -> None:
        kernel = Mock()

        def own_console(processes: object, _size: int) -> int:
            processes[0] = os.getpid()  # type: ignore[index]
            return 1

        kernel.GetConsoleProcessList.side_effect = own_console
        kernel.FreeConsole.return_value = 1
        with patch("utils.console.os.name", "nt"), patch("utils.console.sys.frozen", True, create=True), patch.object(
            ctypes, "WinDLL", return_value=kernel, create=True,
        ), patch("utils.console.sys.stdout"), patch("utils.console.sys.stderr"), patch("utils.console.sys.stdin"), patch(
            "builtins.open", side_effect=lambda *args, **kwargs: io.StringIO(),
        ):
            self.assertTrue(detach_gui_console())
            kernel.FreeConsole.assert_called_once()
            kernel.FreeConsole.reset_mock()
            kernel.GetConsoleProcessList.side_effect = None
            kernel.GetConsoleProcessList.return_value = 2
            self.assertFalse(detach_gui_console())
            kernel.FreeConsole.assert_not_called()

    def test_build_supports_console_and_both_smoke_checks(self) -> None:
        spec = (ROOT / "build" / "AutoCutter.spec").read_text(encoding="utf-8")
        script = (ROOT / "build" / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("console=True", spec)
        self.assertIn('collect_data_files("googleapiclient")', spec)
        self.assertIn('collect_submodules("google_auth_oauthlib")', spec)
        self.assertLess(script.index("check_dependencies.py"), script.index("Remove-Item -LiteralPath $Target"))
        self.assertLess(script.index("Assert-WorkspaceTarget $DistDir"), script.index("Remove-Item -LiteralPath $Target"))
        self.assertIn("verify_packaged_cli.py", script)
        self.assertIn("AUTO_CUTTER_CREDENTIALS_DIR", script)


if __name__ == "__main__":
    unittest.main()
