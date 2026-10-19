"""Shorts 9:16 layout: the stream webcam on top, the Hearthstone board below; board only when there is no webcam.

Everything is computed in source pixels from the packaging webcam ROI (normalized x, y, w, h). The filter
graph starts from [vc] (the concatenated clip at source resolution) and ends with [vo].
"""
from __future__ import annotations

from dataclasses import dataclass

SIZE = (1080, 1920)
CAM_SHARE = (0.30, 0.42)  # webcam panel height, share of the output height
BOARD_ONLY_ASPECT = 3 / 4  # board crop when there is no webcam: full height, 3:4, over a blurred background


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


@dataclass(frozen=True)
class VerticalLayout:
    mode: str  # webcam_board | board_only
    source: tuple[int, int]  # width, height every part is normalized to
    board: tuple[int, int, int, int]  # source crop x, y, w, h
    board_h: int  # output height of the board panel
    webcam: tuple[int, int, int, int] | None = None
    cam_h: int = 0
    size: tuple[int, int] = SIZE

    @property
    def caption_y(self) -> int:
        """Caption centre: on the seam between webcam and board (covers the mic/shoulders and the board edge, not
        the face, the heroes or the cards; far from the app UI at the bottom)."""
        w, h = self.size
        if self.mode == "webcam_board":
            return int(self.cam_h)
        return int((h - self.board_h) / 2 + self.board_h * 0.78)

    def filter_graph(self) -> list[str]:
        w, h = self.size
        bx, by, bw, bh = self.board
        board = f"crop={bw}:{bh}:{bx}:{by},scale={w}:{self.board_h}:flags=lanczos,setsar=1"
        if self.mode == "webcam_board" and self.webcam is not None:
            cx, cy, cw, ch = self.webcam
            return ["[vc]split=2[cam][brd]",
                    f"[cam]crop={cw}:{ch}:{cx}:{cy},scale={w}:{self.cam_h}:flags=lanczos,setsar=1[top]",
                    f"[brd]{board}[bot]",
                    "[top][bot]vstack=inputs=2[stack]"]
        return ["[vc]split=2[bg][fg]",
                f"[bg]scale=-2:{h},crop={w}:{h},boxblur=24:2,eq=brightness=-0.12,setsar=1[blur]",
                f"[fg]{board}[main]",
                f"[blur][main]overlay=0:{(h - self.board_h) // 2}[stack]"]


def _board_box(source_w: int, source_h: int, panel_aspect: float, board_center_x: float,
               focus: tuple[float, float, float] | None) -> tuple[int, int, int, int]:
    """Board crop with the panel aspect: full height centred on board_center_x, or zoomed on focus (normalized
    centre x, y and zoom >= 1: the crop is 1/zoom of the full-height crop, kept inside the frame)."""
    bw, bh = min(source_w, source_h * panel_aspect), float(source_h)
    cx, cy = board_center_x, 0.5
    if focus is not None:
        cx, cy, zoom = focus[0], focus[1], max(1.0, focus[2])
        bw, bh = bw / zoom, bh / zoom
    bx = min(max(source_w * cx - bw / 2, 0), source_w - bw)
    by = min(max(source_h * cy - bh / 2, 0), source_h - bh)
    return _even(bx), _even(by), _even(bw), _even(bh)


def compute_layout(source_w: int, source_h: int, roi: tuple[float, float, float, float] | None, *,
                   size: tuple[int, int] = SIZE, board_center_x: float = 0.5,
                   focus: tuple[float, float, float] | None = None) -> VerticalLayout:
    """focus: zoom the board panel on the play (normalized centre x, y and zoom factor), see short.py."""
    w, h = size
    if roi is not None:
        rx, ry, rw, rh = (min(max(v, 0.0), 1.0) for v in roi)
        px, py, pw, ph = rx * source_w, ry * source_h, rw * source_w, rh * source_h
        if pw >= 16 and ph >= 16:
            cam_h = _even(min(max(w / (pw / ph), h * CAM_SHARE[0]), h * CAM_SHARE[1]))
            want = w / cam_h  # crop the webcam box to the panel aspect, centred
            if pw / ph > want:
                cw, ch = ph * want, ph
            else:
                cw, ch = pw, pw / want
            webcam = _inside(px + (pw - cw) / 2, py + (ph - ch) / 2, cw, ch)
            board_h = h - cam_h
            board = _board_box(source_w, source_h, w / board_h, board_center_x, focus)
            return VerticalLayout("webcam_board", (source_w, source_h), board, board_h,
                                  webcam=_clamp(webcam, source_w, source_h), cam_h=cam_h, size=size)
    board_h = _even(w / BOARD_ONLY_ASPECT)
    return VerticalLayout("board_only", (source_w, source_h),
                          _board_box(source_w, source_h, BOARD_ONLY_ASPECT, board_center_x, focus), board_h, size=size)


def _inside(x: float, y: float, w: float, h: float) -> tuple[int, int, int, int]:
    """Even pixel box that never leaves the float box (no game pixels at the webcam edges)."""
    import math
    x0, y0 = math.ceil(x / 2) * 2, math.ceil(y / 2) * 2
    x1, y1 = math.floor((x + w) / 2) * 2, math.floor((y + h) / 2) * 2
    return x0, y0, max(2, x1 - x0), max(2, y1 - y0)


def _clamp(box: tuple[int, int, int, int], source_w: int, source_h: int) -> tuple[int, int, int, int]:
    x, y, bw, bh = box
    bw, bh = min(bw, source_w), min(bh, source_h)
    return max(0, min(x, source_w - bw)), max(0, min(y, source_h - bh)), bw, bh
