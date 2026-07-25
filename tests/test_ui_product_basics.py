from __future__ import annotations

from html.parser import HTMLParser
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.crash_handler import write_crash_log
from utils.i18n import normalize_language, text


ROOT = Path(__file__).resolve().parents[1]


class _ButtonAccessibilityParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.missing_labels: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "button":
            return
        values = dict(attrs)
        if not values.get("aria-label"):
            self.missing_labels.append(str(values.get("id") or "<button>"))


class UiProductBasicsTests(unittest.TestCase):
    def test_only_layout_webviews_are_created(self) -> None:
        source = (ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")

        self.assertEqual(source.count("QWebEngineView()"), 3)

    def test_web_buttons_have_accessible_labels(self) -> None:
        for filename in ("topbar.html", "bottombar.html"):
            parser = _ButtonAccessibilityParser()
            parser.feed((ROOT / "webui" / filename).read_text(encoding="utf-8"))
            self.assertEqual(parser.missing_labels, [], filename)

    def test_basic_localization(self) -> None:
        self.assertEqual(normalize_language("it-IT"), "it")
        self.assertEqual(text("export_mp4", "it"), "Esporta MP4")
        self.assertEqual(text("export_mp4", "en"), "Export MP4")

    def test_crash_log_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"AUTO_CUTTER_CRASH_DIR": tmp}):
                try:
                    raise RuntimeError("test crash")
                except RuntimeError as exc:
                    output = write_crash_log(type(exc), exc, exc.__traceback__)

            self.assertTrue(output.is_file())
            self.assertIn("RuntimeError: test crash", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
