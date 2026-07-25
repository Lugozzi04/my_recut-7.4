from __future__ import annotations

import unittest

from core.project_session import ProjectSession
from core.track_state import TrackState


class ProjectSessionTests(unittest.TestCase):
    def test_add_media_creates_consistent_project_links(self) -> None:
        state = TrackState(path="clip.mp4", duration=12.5)
        session = ProjectSession.create_default()
        session.track_states = [state]

        session.add_media_for_state(state.path, state)

        self.assertEqual(session.consistency_errors(), [])
        self.assertEqual(session.project.timeline_duration(), 12.5)
        self.assertIsNotNone(session.project.get_media(str(state.media_id)))

    def test_sync_updates_media_duration(self) -> None:
        state = TrackState(path="clip.mp4", duration=5.0)
        session = ProjectSession.create_default()
        session.track_states = [state]
        session.add_media_for_state(state.path, state)

        state.duration = 8.0
        session.sync_state_to_project(state)

        media = session.project.get_media(str(state.media_id))
        self.assertIsNotNone(media)
        self.assertEqual(media.duration, 8.0)

    def test_consistency_check_reports_broken_links(self) -> None:
        session = ProjectSession.create_default()
        session.track_states = [TrackState(path="missing.mp4")]

        errors = session.consistency_errors()

        self.assertTrue(any("no matching media" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
