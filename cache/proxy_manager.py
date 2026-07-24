from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ProxyMedia:
    media_id: str
    proxy_path: Path
    width: int
    height: int
    fps: float


class ProxyManager:
    """
    Placeholder proxy manager for low-res editing media.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get_proxy(self, media_id: str) -> Optional[ProxyMedia]:
        return None

    def request_proxy(self, media_id: str) -> None:
        return
