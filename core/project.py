from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import uuid


def _uid() -> str:
    return uuid.uuid4().hex


@dataclass
class Media:
    id: str
    path: str
    name: str
    duration: float = 0.0
    fps: float = 0.0
    audio_sr: int = 0
    audio_channels: int = 0
    has_video: bool = True
    has_audio: bool = True


@dataclass
class Clip:
    id: str
    media_id: str
    track_id: str
    source_in: float
    source_out: float
    timeline_in: float
    timeline_out: float
    link_id: str | None = None
    enabled: bool = True
    locked: bool = False
    name: str = ""
    color: tuple[int, int, int, int] | None = None
    edge: tuple[int, int, int] | None = None

    @property
    def duration(self) -> float:
        return max(0.0, float(self.timeline_out) - float(self.timeline_in))


@dataclass
class Track:
    id: str
    name: str
    kind: str  # "video" | "audio"
    clips: List[Clip] = field(default_factory=list)
    muted: bool = False
    solo: bool = False
    locked: bool = False
    height: int = 70

    def add_clip(self, clip: Clip) -> None:
        self.clips.append(clip)

    def remove_clip(self, clip_id: str) -> None:
        self.clips = [c for c in self.clips if c.id != clip_id]

    def sorted_clips(self) -> List[Clip]:
        return sorted(self.clips, key=lambda c: (c.timeline_in, c.timeline_out))


@dataclass
class Transition:
    id: str
    kind: str  # "crossfade", "dissolve", ...
    duration: float
    track_id: str
    at: float


@dataclass
class Marker:
    id: str
    time: float
    label: str = ""
    color: str = "yellow"


@dataclass
class Project:
    id: str
    name: str
    tracks: List[Track] = field(default_factory=list)
    media: Dict[str, Media] = field(default_factory=dict)
    transitions: List[Transition] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    fps: float = 30.0
    sample_rate: int = 48000

    @classmethod
    def create_default(cls, name: str = "Untitled") -> "Project":
        p = cls(id=_uid(), name=name)
        p.add_track("V1", "video")
        p.add_track("A1", "audio")
        return p

    def add_track(self, name: str, kind: str) -> Track:
        t = Track(id=_uid(), name=name, kind=kind)
        self.tracks.append(t)
        return t

    def get_track(self, track_id: str) -> Optional[Track]:
        for t in self.tracks:
            if t.id == track_id:
                return t
        return None

    def add_media(self, path: str, name: Optional[str] = None) -> Media:
        m = Media(
            id=_uid(),
            path=path,
            name=name or path,
        )
        self.media[m.id] = m
        return m

    def get_media(self, media_id: str) -> Optional[Media]:
        return self.media.get(media_id)

    def add_clip(
        self,
        track_id: str,
        media_id: str,
        link_id: str | None,
        source_in: float,
        source_out: float,
        timeline_in: float,
        timeline_out: float,
        name: str = "",
        color: tuple[int, int, int, int] | None = None,
        edge: tuple[int, int, int] | None = None,
    ) -> Clip:
        clip = Clip(
            id=_uid(),
            media_id=media_id,
            track_id=track_id,
            link_id=link_id,
            source_in=float(source_in),
            source_out=float(source_out),
            timeline_in=float(timeline_in),
            timeline_out=float(timeline_out),
            name=name,
            color=color,
            edge=edge,
        )
        track = self.get_track(track_id)
        if track is not None:
            track.add_clip(clip)
        return clip

    def timeline_duration(self) -> float:
        end = 0.0
        for t in self.tracks:
            for c in t.clips:
                end = max(end, float(c.timeline_out))
        return float(end)

    def video_tracks(self) -> List[Track]:
        return [t for t in self.tracks if t.kind == "video"]

    def audio_tracks(self) -> List[Track]:
        return [t for t in self.tracks if t.kind == "audio"]

