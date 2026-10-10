"""Deterministic fakes for the AI YouTube Packaging tests (no ffmpeg, no ASR, no agent CLI)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from analysis.boundaries.models import EvidenceStatus, TranscriptSegment, TranscriptWord

W, H = 640, 360
WEBCAM_ROI = "0.0,0.55,0.25,0.45"


def synthetic_frame(t: float) -> np.ndarray:
    """A 'board' with moving shapes plus a constant webcam area bottom-left (sharp, textured)."""
    image = np.zeros((H, W, 3), np.uint8)
    image[:] = (40, 60 + int(t) % 50, 90)
    x = int(40 + (t * 13) % (W - 160))
    image[100:220, x:x + 120] = (30, 200, 240)
    image[::8, :, 1] = 120  # texture so the sharpness metric is non-trivial
    image[int(H * 0.55):, : int(W * 0.25)] = (150, 170, 200)
    image[int(H * 0.62):int(H * 0.80), int(W * 0.06):int(W * 0.18)] = (90, 120, 190)
    return image


class FakeSampler:
    def __init__(self) -> None:
        self.calls = 0

    def sample(self, path: Any, *, fps: float, start_s: float = 0.0, end_s: float | None = None,
               max_width: int = 960, cancellation: Any = None):
        self.calls += 1
        t = start_s
        end = end_s if end_s is not None else start_s + 1.0
        while t < end:
            yield SimpleNamespace(timestamp=round(t, 3), image=synthetic_frame(t))
            t += 1.0 / fps


def script_text(t: float) -> str:
    if 40.0 <= t < 44.0:
        return "incredibile! assurdo"
    if 160.0 <= t < 164.0:
        return "no no no pazzesco!"
    return "allora qui gioco il minion e poi attacco"


class FakeEngine:
    """Emits one word every 0.5 s in each window; text depends on the absolute time."""

    def __init__(self, status: str = "ok") -> None:
        self.status = status
        self.calls = 0
        self.windows: list[tuple[float, float]] = []

    def transcribe(self, video: Any, windows: Any, *, cancellation: Any = None):
        self.calls += 1
        if self.status != "ok":
            return EvidenceStatus(self.status, "fake", "engine not installed"), [[] for _ in windows]
        out = []
        for window in windows:
            self.windows.append((window.start, window.end))
            segments = []
            t = window.start
            while t + 4.0 <= window.end:
                words = tuple(TranscriptWord(t + 0.5 * i, t + 0.5 * i + 0.4, " " + w, 0.9)
                              for i, w in enumerate(script_text(t).split()))
                segments.append(TranscriptSegment(t, words[-1].end, script_text(t), 0.8, 0.01, words))
                t += 5.0
            out.append(segments)
        return EvidenceStatus("ok", "fake"), out


def fake_rms(request: Any, *, start_s: float, end_s: float, cancellation: Any = None) -> np.ndarray:
    times = start_s + np.arange(int((end_s - start_s) / request.hop_s)) * request.hop_s
    rms = np.full(times.size, 0.01)
    rms[(times >= 41.0) & (times < 43.0)] = 0.08  # loud reaction
    rms[(times >= 300.0) & (times < 302.0)] = 0.06
    return rms


def failing_rms(*args: Any, **kwargs: Any) -> np.ndarray:
    raise RuntimeError("no audio stream")


def fake_scanner(source: str, start: float, end: float, *, cancellation: Any = None):
    frames = []
    t = start
    while t < end:
        image = np.full((90, 160), 40 if t < 200 else 200, np.uint8)
        frames.append((t, image))
        t += 2.0
    return frames


def write_project(folder: Path, media: Path, *, duration: float = 600.0) -> Path:
    """Two kept WIN games with silence cuts inside, one removed LOSS between them."""
    keeps = [(20.0, 60.0), (62.0, 150.0), (151.0, 200.0), (400.0, 470.0), (472.0, 560.0)]
    cuts = []
    cursor = 0.0
    for a, b in keeps:
        if a > cursor:
            cuts.append({"start": cursor, "end": a})
        cursor = b
    cuts.append({"start": cursor, "end": duration})
    games = [
        {"id": "g-1", "index": 1, "start": 20.0, "end": 195.0, "result": "WIN", "disposition": "KEEP"},
        {"id": "g-2", "index": 2, "start": 210.0, "end": 380.0, "result": "LOSS", "disposition": "REMOVE"},
        {"id": "g-3", "index": 3, "start": 400.0, "end": 555.0, "result": "WIN", "disposition": "KEEP"},
    ]
    islands = [{"start": 20.0, "end": 200.0, "game": games[0]}, {"start": 400.0, "end": 560.0, "game": games[2]}]
    payload = {"format": "autocutter_project", "version": 2, "project_name": "fake",
               "global": {}, "gameplay": {"version": 2, "mode": "keep", "games": games, "keep_islands": islands},
               "tracks": [{"path": str(media), "duration": duration, "segment_source_in": 0.0,
                           "segment_source_out": duration, "cuts_enabled": True, "cuts": cuts,
                           "keeps": [{"start": a, "end": b} for a, b in keeps], "cfg": {}}]}
    project = folder / "fake.autocutter"
    project.write_text(json.dumps(payload), encoding="utf-8")
    return project


def planner() -> Any:
    from automation.exporter import ProjectExportPlanner

    return ProjectExportPlanner(duration_probe=lambda path: 600.0, audio_probe=lambda path: True)


class FakeAgent:
    """Builds valid answers from the ids printed in the prompt; records every call."""

    def __init__(self, *, fail: Exception | None = None, deepen: bool = False, invalid_first: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail
        self.deepen = deepen
        self.invalid_first = invalid_first
        self.repair_prompts: list[str] = []
        self.settings = SimpleNamespace(provider="codex")

    def run(self, task: str, *, prompt: str, schema: dict[str, Any], system_prompt: str, prompt_version: str,
            images: Any = None, validate: Any = None, repair: Any = None):
        self.calls.append({"task": task, "images": len(images or []), "prompt_chars": len(prompt), "cost_usd": 0.01,
                           "prompt": prompt})
        if self.fail is not None:
            raise self.fail
        if task == "packaging_fast":
            answer = self.fast(prompt)
            if self.invalid_first:
                broken = json.loads(json.dumps(answer))
                broken["concepts"][0]["highlight_ids"] = ["H99"]
                try:
                    validate(broken)
                except ValueError as error:
                    self.repair_prompts.append(repair(broken, str(error)))
                    self.calls.append({"task": task + "_repair", "images": 0,
                                       "prompt_chars": len(self.repair_prompts[-1]), "cost_usd": 0.01})
        elif task == "packaging_fast_patch":
            answer = self.patch(prompt)
        elif task.startswith("video_story"):
            answer = self.story(prompt)
        else:
            answer = self.creative(prompt)
        if validate is not None:
            try:
                answer = validate(answer)
            except ValueError as error:  # like PackagingAgent: a rejected answer is an invalid_output failure
                from analysis.boundaries.agents import AgentCallError
                raise AgentCallError("invalid_output", str(error)) from error
        return SimpleNamespace(answer=answer, meta={})

    def fast(self, prompt: str) -> dict[str, Any]:
        n_concepts = int(re.findall(r"exactly (\d+) concepts", prompt)[0])
        n_titles = int(re.findall(r"exactly (\d+) titles", prompt)[0])
        highlights = re.findall(r"^- (H\d+) game=", prompt, re.M)
        frames = list(dict.fromkeys(re.findall(r"\b(F\d{3})\b", prompt)))
        webcam = re.findall(r"webcam (W\d+)", prompt)
        lines = re.findall(r"^(T\d{4}) \[", prompt, re.M)
        games = re.findall(r"^- ((?:g-|segment-|video)[\w-]*): index", prompt, re.M)
        layouts = (["reaction_focus"] if webcam else []) + ["board_focus", "story_focus", "board_focus"]
        texts = ["Che lettura", "Rimonta pazza", "Turno chiave", "Mossa giusta", "Finale", "Ultimo turno"]
        concepts = []
        for i in range(n_concepts):
            layout = layouts[i % len(layouts)]
            concepts.append({
                "concept_id": f"C{i + 1}", "narrative_angle": f"angolo {i + 1}", "direction": "competitive",
                "title": f"Una partita vinta con una lettura perfetta, versione {'ABCDEF'[i]}",
                "thumbnail_text": texts[i], "highlight_ids": [highlights[i % len(highlights)]],
                "source_frame_ids": [frames[i % len(frames)]],
                "webcam_frame_id": webcam[0] if layout == "reaction_focus" else None,
                "card_asset_ids": ["O1"] if i == 1 else [],
                "composition_plan": {"layout": layout, "background_frame_id": frames[i % len(frames)],
                                     "focus_box": [0.1, 0.1, 0.6, 0.6] if i % 2 else None, "inset_id": None,
                                     "inset_box": None, "text_side": "left", "webcam_side": "right",
                                     "emphasis_word": None, "badge": None, "accent_color": "#FFD400",
                                     "text_color": "#FFFFFF", "rationale": "test"},
                "evidence": [{"claim": "momento", "highlight_id": highlights[0], "transcript_ids": lines[:1],
                              "frame_ids": [frames[0]]}],
                "risk_notes": ""})
        titles = [{"title_id": f"T{i + 1}", "title": f"Titolo numero {chr(65 + i)} di una vittoria pulita",
                   "concept_id": f"C{i % n_concepts + 1}", "style": "narrative", "highlight_ids": highlights[:1],
                   "transcript_ids": [], "rationale": "test"} for i in range(n_titles)]
        return {"language": "it", "main_topic": "Due vittorie", "summary": "s", "themes": ["rimonta"],
                "why_interesting": "w",
                "games": [{"game_id": g, "summary": "partita", "key_moments": [
                    {"highlight_id": highlights[0], "description": "m", "transcript_ids": lines[:1]}],
                    "deck_talk": [], "conclusion": "c"} for g in games],
                "strongest_highlights": [{"highlight_id": h, "why": "w", "strength": 0.8} for h in highlights[:3]],
                "narrative_angles": [{"angle_id": "A1", "angle": "a", "highlight_ids": highlights[:1]}],
                "card_observations": [{"obs_id": "O1", "frame_id": frames[0], "box": [0.1, 0.25, 0.2, 0.35],
                                       "name": "Minion", "legibility": "clear", "confidence": 0.7,
                                       "transcript_ids": lines[:1]}],
                "risky_claims": [], "concepts": concepts, "titles": titles, "preferred_title_id": "T1",
                "preferred_concept_id": "C1",
                "deepen_requests": [{"concept_id": "C2", "edited_start": 10.0, "edited_end": 20.0, "want": "frames",
                                     "question": "which card?"}] if self.deepen else []}

    @staticmethod
    def patch(prompt: str) -> dict[str, Any]:
        concept = json.loads(prompt.split("CONCEPT:\n", 1)[1].split("\n", 1)[0])
        concept["thumbnail_text"] = "Carta letta"
        return {"answer": "the card is now readable", "card_observations": [], "concept": concept}

    @staticmethod
    def story(prompt: str) -> dict[str, Any]:
        highlights = re.findall(r"^- (H\d+) game=", prompt, re.M)
        frames = re.findall(r"^- (F\d+) gameplay", prompt, re.M)
        lines = re.findall(r"^(T\d{4}) \[", prompt, re.M)
        games = re.findall(r"^- ((?:g-|segment-|video)[\w-]*): ", prompt, re.M)
        return {"language": "it", "main_topic": "Due vittorie", "summary": "Due partite vinte.",
                "themes": ["rimonta"], "why_interesting": "reazioni forti",
                "games": [{"game_id": g, "summary": "partita", "specific_plays": [
                    {"description": "giocata", "transcript_ids": lines[:1]}], "general_talk": [],
                    "conclusion": "vinta"} for g in games],
                "strongest_highlights": [{"highlight_id": h, "title": "momento", "why": "reazione",
                                          "transcript_ids": lines[:2], "frame_ids": frames[:1], "strength": 0.8}
                                         for h in highlights[:3]],
                "narrative_angles": [{"angle_id": "A1", "angle": "rimonta", "highlight_ids": highlights[:1]},
                                     {"angle_id": "A2", "angle": "lettura", "highlight_ids": highlights[1:2]}],
                "card_observations": [{"frame_id": frames[0], "box": [0.1, 0.25, 0.2, 0.35], "name": "Minion",
                                        "legibility": "clear", "confidence": 0.7, "transcript_ids": lines[:1]}],
                "risky_claims": [{"claim": "winrate", "why": "not in evidence"}], "deepen_requests": []}

    @staticmethod
    def creative(prompt: str) -> dict[str, Any]:
        n_concepts = int(re.findall(r"exactly (\d+) concepts", prompt)[0])
        n_titles = int(re.findall(r"exactly (\d+) titles", prompt)[0])
        section = prompt.split("GAMEPLAY FRAMES")[1].split("WEBCAM CROPS")[0]
        frames = re.findall(r"^- (F\d+) ", section, re.M)
        webcam = re.findall(r"^- (W\d+) ", prompt.split("WEBCAM CROPS")[1].split("CARD CROPS")[0], re.M)
        highlights = re.findall(r"^- (H\d+) game=", prompt, re.M)
        layouts = (["split_reaction"] if webcam else []) + ["board_focus", "cinematic_text", "board_focus"]
        concepts = []
        for i in range(n_concepts):
            layout = layouts[i % len(layouts)]
            concepts.append({
                "concept_id": f"C{i + 1}", "narrative_angle": f"angolo {i + 1}", "direction": "competitive",
                "title": f"Una partita vinta con una lettura perfetta, versione {'ABCDEF'[i]}",
                "thumbnail_text": ["Che lettura", "Rimonta pazza", "Turno chiave", "Mossa giusta", "Finale",
                                   "Ultimo turno"][i],
                "highlight_ids": highlights[i % len(highlights):i % len(highlights) + 1],
                "source_frame_ids": [frames[i % len(frames)]],
                "webcam_frame_id": webcam[0] if layout == "split_reaction" else None, "card_asset_ids": [],
                "composition_plan": {"layout": layout, "background_frame_id": frames[i % len(frames)],
                                     "focus_box": [0.1, 0.1, 0.6, 0.6] if i % 2 else None, "inset_id": None,
                                     "inset_box": None, "text_side": "left", "webcam_side": "right",
                                     "emphasis_word": None, "badge": None, "accent_color": "#FFD400",
                                     "text_color": "#FFFFFF", "rationale": "test"},
                "evidence": [{"claim": "momento", "highlight_id": highlights[0], "transcript_ids": [],
                              "frame_ids": [frames[0]]}],
                "risk_notes": ""})
        titles = [{"title_id": f"T{i + 1}", "title": f"Titolo numero {chr(65 + i)} di una vittoria pulita",
                   "concept_id": f"C{i % n_concepts + 1}", "style": "narrative", "highlight_ids": highlights[:1],
                   "transcript_ids": [], "rationale": "test"} for i in range(n_titles)]
        return {"concepts": concepts, "titles": titles, "preferred_title_id": "T1", "preferred_concept_id": "C1"}
