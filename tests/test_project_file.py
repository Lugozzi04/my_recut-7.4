from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.project_file import (
    PROJECT_VERSION,
    ProjectFormatError,
    make_payload_portable,
    normalize_project_payload,
    relink_items_in_directory,
    resolve_project_items,
)


class ProjectFileTests(unittest.TestCase):
    def test_v1_payload_is_migrated(self) -> None:
        payload = normalize_project_payload(
            {"format": "autocutter_project", "version": 1, "tracks": [{"path": "video.mp4"}]}
        )

        self.assertEqual(payload["version"], PROJECT_VERSION)

    def test_future_version_is_rejected(self) -> None:
        with self.assertRaises(ProjectFormatError):
            normalize_project_payload(
                {"format": "autocutter_project", "version": PROJECT_VERSION + 1, "tracks": [{"path": "x"}]}
            )

    def test_project_paths_are_relative_and_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media" / "clip.mp4"
            media.parent.mkdir()
            media.write_bytes(b"video-data")
            project = root / "edit" / "project.autocutter"
            project.parent.mkdir()
            payload = {
                "format": "autocutter_project",
                "version": PROJECT_VERSION,
                "tracks": [{"path": str(media)}],
            }

            portable = make_payload_portable(payload, project)
            available, missing = resolve_project_items(portable["tracks"], project)

            self.assertEqual(portable["tracks"][0]["path_kind"], "relative")
            self.assertEqual(Path(available[0]["path"]), media)
            self.assertEqual(missing, [])

    def test_relink_uses_name_size_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "new-location" / "clip.mp4"
            media.parent.mkdir()
            media.write_bytes(b"matching-video")
            project = root / "project.autocutter"
            payload = make_payload_portable(
                {
                    "format": "autocutter_project",
                    "version": PROJECT_VERSION,
                    "tracks": [{"path": str(media)}],
                },
                project,
            )
            item = payload["tracks"][0]
            item["path"] = "missing/clip.mp4"

            relinked, unresolved = relink_items_in_directory([item], root / "new-location")

            self.assertEqual(Path(relinked[0]["path"]), media)
            self.assertEqual(unresolved, [])


if __name__ == "__main__":
    unittest.main()
