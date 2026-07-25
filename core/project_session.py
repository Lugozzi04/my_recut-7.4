from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import uuid

from core.project import Clip, Project
from core.track_state import TrackState


@dataclass
class ProjectSession:
    project: Project = field(default_factory=Project.create_default)
    track_states: list[TrackState] = field(default_factory=lambda: [TrackState()])

    @classmethod
    def create_default(cls, name: str = "Untitled") -> "ProjectSession":
        return cls(project=Project.create_default(name), track_states=[TrackState()])

    def reset(self, name: str = "Untitled") -> None:
        self.project = Project.create_default(name)
        self.track_states = [TrackState()]

    def ensure_track_for_state(self, state: TrackState, kind: str) -> str:
        if kind not in {"video", "audio"}:
            raise ValueError(f"Unsupported track kind: {kind}")
        id_attr = f"{kind}_track_id"
        current_id = getattr(state, id_attr)
        if current_id and self.project.get_track(str(current_id)):
            return str(current_id)

        used = {
            getattr(item, id_attr)
            for item in self.track_states
            if getattr(item, id_attr)
        }
        for track in self.project.tracks:
            if track.kind == kind and track.id not in used:
                setattr(state, id_attr, track.id)
                return track.id

        index = len([track for track in self.project.tracks if track.kind == kind]) + 1
        prefix = "V" if kind == "video" else "A"
        track = self.project.add_track(f"{prefix}{index}", kind)
        setattr(state, id_attr, track.id)
        return track.id

    def find_clip(self, clip_id: str | None) -> Clip | None:
        if not clip_id:
            return None
        for track in self.project.tracks:
            for clip in track.clips:
                if clip.id == clip_id:
                    return clip
        return None

    def add_media_for_state(self, path: str, state: TrackState) -> None:
        name = Path(path).name
        media = self.project.add_media(path, name=name)
        state.media_id = media.id
        video_track_id = self.ensure_track_for_state(state, "video")
        audio_track_id = self.ensure_track_for_state(state, "audio")

        source_in = float(state.segment_source_in or 0.0)
        source_out = float(state.segment_source_out or 0.0)
        if source_out <= source_in:
            source_in = 0.0
            source_out = float(state.duration or 0.0)
        duration = max(0.0, source_out - source_in)
        if duration > 0.0:
            state.duration = duration

        timeline_in = float(self.project.timeline_duration() or 0.0)
        timeline_out = timeline_in + duration
        link_id = uuid.uuid4().hex
        audio_clip = self.project.add_clip(
            track_id=audio_track_id,
            media_id=media.id,
            link_id=link_id,
            source_in=source_in,
            source_out=source_out,
            timeline_in=timeline_in,
            timeline_out=timeline_out,
            name=name,
        )
        video_clip = self.project.add_clip(
            track_id=video_track_id,
            media_id=media.id,
            link_id=link_id,
            source_in=source_in,
            source_out=source_out,
            timeline_in=timeline_in,
            timeline_out=timeline_out,
            name=name,
            color=state.video_color,
            edge=state.video_edge,
        )
        state.audio_clip_id = audio_clip.id
        state.video_clip_id = video_clip.id

    def sync_state_to_project(self, state: TrackState) -> None:
        duration = float(state.duration or 0.0)
        media = self.project.get_media(str(state.media_id)) if state.media_id else None
        if media is not None:
            media.duration = duration
        if float(state.segment_source_out or 0.0) <= 0.0:
            state.segment_source_in = float(state.segment_source_in or 0.0)
            state.segment_source_out = state.segment_source_in + duration

        for clip_id in (state.audio_clip_id, state.video_clip_id):
            clip = self.find_clip(clip_id)
            if clip is None or duration <= 0.0:
                continue
            if float(clip.source_out) <= float(clip.source_in) + 1e-6:
                clip.source_out = float(clip.source_in) + duration
            if float(clip.timeline_out) <= float(clip.timeline_in) + 1e-6:
                clip.timeline_out = float(clip.timeline_in) + duration

    def consistency_errors(self) -> list[str]:
        errors: list[str] = []
        for index, state in enumerate(self.track_states):
            if not state.path:
                continue
            if not state.media_id or self.project.get_media(str(state.media_id)) is None:
                errors.append(f"track_states[{index}] has no matching media")
            for kind in ("video", "audio"):
                track_id = getattr(state, f"{kind}_track_id")
                if not track_id or self.project.get_track(str(track_id)) is None:
                    errors.append(f"track_states[{index}] has no matching {kind} track")
                clip_id = getattr(state, f"{kind}_clip_id")
                if not clip_id or self.find_clip(str(clip_id)) is None:
                    errors.append(f"track_states[{index}] has no matching {kind} clip")
        return errors
