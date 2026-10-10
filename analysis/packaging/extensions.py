"""Optional, pluggable extensions. None of them is required; V1 ships only the null implementations.

ImageGenerationProvider: a future source of generated background/accent art. The
renderer never depends on it, and nothing assumes that a ChatGPT/Codex/Claude
subscription grants an image API. Generated art must be labelled as such in the
report and never presented as a frame of the video.

TrendsProvider: current YouTube trends through an authorised API. Kept separate
from video understanding and creator style; never invented. Not used in V1.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class ImageGenerationProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def generate(self, prompt: str, *, size: tuple[int, int], target: Path) -> dict[str, Any]:
        """Write an image to target and return provenance (model, prompt digest, licence)."""
        ...


class NoImageGeneration:
    name = "none"

    def available(self) -> bool:
        return False

    def generate(self, prompt: str, *, size: tuple[int, int], target: Path) -> dict[str, Any]:
        raise RuntimeError("no image generation provider is configured")


class TrendsProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def trends(self, *, language: str, topic: str) -> list[dict[str, Any]]: ...


class NoTrends:
    name = "none"

    def available(self) -> bool:
        return False

    def trends(self, *, language: str, topic: str) -> list[dict[str, Any]]:
        return []


def extension_status(image_generation: ImageGenerationProvider | None = None,
                     trends: TrendsProvider | None = None) -> dict[str, Any]:
    image_generation = image_generation or NoImageGeneration()
    trends = trends or NoTrends()
    return {"image_generation": {"provider": image_generation.name, "available": image_generation.available(),
                                 "used": False},
            "youtube_trends": {"provider": trends.name, "available": trends.available(), "used": False,
                               "note": "trends are never inferred; V1 does not query any trends source"}}
