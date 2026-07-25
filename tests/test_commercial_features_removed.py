from __future__ import annotations

import unittest

import main
from ui.main_window import MainWindow
from utils.runtime_paths import project_root


class CommercialFeaturesRemovedTests(unittest.TestCase):
    def test_commercial_files_are_absent(self) -> None:
        root = project_root()
        obsolete_paths = (
            "licensing",
            "msix",
            "store.env.example",
            "build/build-msix.ps1",
            "ui/store_activation_dialog.py",
            "ui/store_dialogs.py",
        )

        for relative_path in obsolete_paths:
            self.assertFalse((root / relative_path).exists(), relative_path)

    def test_runtime_has_no_commercial_gate_or_actions(self) -> None:
        self.assertFalse(hasattr(main, "_resolve_store_license"))
        for method_name in (
            "set_license_context",
            "_license_is_active_for_export",
            "_build_store_state",
            "_purchase_store_plan",
            "_refresh_license_now",
        ):
            self.assertFalse(hasattr(MainWindow, method_name), method_name)

    def test_dependencies_do_not_include_windows_store_sdk(self) -> None:
        requirements = (project_root() / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("winsdk", requirements)


if __name__ == "__main__":
    unittest.main()
