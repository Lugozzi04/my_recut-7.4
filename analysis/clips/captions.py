"""Burned-in Shorts captions: Whisper word timestamps -> an ASS file, 1-3 words at a time, the spoken word in yellow.

Times are relative to the clip: the clip is a contiguous range of the EDITED timeline, so a word at edited time
t is at t - clip.edited_start in the rendered clip (the cuts of the edit are already gone).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Sequence

from .candidates import Analysis
from .models import ClipCandidate

MAX_WORDS, MAX_CHARS = 3, 18
GAP_BREAK_S = 0.45  # a pause starts a new caption
HOLD_S = 0.4  # how long the last word stays when nothing follows
FONTS = (("ariblk.ttf", "Arial Black"), ("impact.ttf", "Impact"), ("arialbd.ttf", "Arial"))
WHITE, YELLOW, BLACK = "&H00FFFFFF", "&H0000D7FF", "&H00000000"  # ASS colours are &HAABBGGRR
FONT_SCALE = 0.085  # caption size, share of the video width (Shorts from a Twitch clip: BIG_FONT_SCALE)
BIG_FONT_SCALE = 0.105
SIDE_MARGIN = 60
CHAR_EM, SPACE_EM = 0.78, 0.32  # upper-case Arial Black/Impact: average advance per character, in font sizes


def clip_words(analysis: Analysis, clip: ClipCandidate) -> list[tuple[float, float, str]]:
    """Words inside the clip, clip-relative. Lines without word timestamps are spread evenly over the line."""
    out: list[tuple[float, float, str]] = []
    for line in analysis.lines:
        if line.end <= clip.edited_start or line.start >= clip.edited_end:
            continue
        words = line.words
        if not words:
            tokens = line.text.split()
            step = (line.end - line.start) / max(1, len(tokens))
            words = [(line.start + n * step, line.start + (n + 1) * step, t) for n, t in enumerate(tokens)]
        for start, end, text in words:
            if text and end > clip.edited_start and start < clip.edited_end:
                out.append((max(0.0, start - clip.edited_start), min(clip.duration, end - clip.edited_start), text))
    return sorted(out)


def merge_elisions(words: Sequence[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Whisper splits Italian elisions ("c'" + "è", "l'" + "ho"): one caption word "C'È"."""
    out: list[tuple[float, float, str]] = []
    for word in words:
        previous = out[-1] if out else None
        if previous is not None and re.fullmatch(r"\w{1,5}['’]", previous[2].strip()) and word[0] - previous[1] < 0.5:
            out[-1] = (previous[0], word[1], previous[2].strip() + word[2].strip())
        else:
            out.append(word)
    return out


def chunk_words(words: Sequence[tuple[float, float, str]]) -> list[list[tuple[float, float, str]]]:
    chunks: list[list[tuple[float, float, str]]] = []
    for word in merge_elisions(words):
        current = chunks[-1] if chunks else None
        if (current is None or len(current) >= MAX_WORDS or word[0] - current[-1][1] > GAP_BREAK_S
                or re.search(r"[.!?…,;:]$", current[-1][2])
                or len(" ".join(w[2] for w in current + [word])) > MAX_CHARS):
            chunks.append([word])
        else:
            current.append(word)
    return chunks


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t // 60 % 60):02d}:{t % 60:05.2f}"


def _clean(text: str) -> str:
    text = re.sub(r"[{}\\]", "", text).strip(" \"“”«»")
    if not re.search(r"\w['’]\w", text):  # keep the apostrophe of an elision (C'È), drop quoting ones
        text = text.strip("'’")
    return re.sub(r"[.,;:]+$", "", text).upper()


def fitted_size(text: str, size: int, width: int) -> int:
    """Font size at which the (upper-case) caption fits in the width minus the margins: big captions never
    leave the frame (WrapStyle 2 does not wrap)."""
    em = sum(SPACE_EM if ch == " " else CHAR_EM for ch in text)
    available = width - 2 * SIDE_MARGIN
    return size if em * size <= available else max(int(size * 0.5), int(available / max(em, 1e-6)))


def ass_document(words: Sequence[tuple[float, float, str]], *, duration: float, size: tuple[int, int],
                 caption_y: int, font: str = "Arial Black", scale: float = FONT_SCALE) -> str:
    w, h = size
    font_size = int(w * scale)
    head = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 2",
            "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
            f"Style: Cap,{font},{font_size},{WHITE},{WHITE},{BLACK},&H64000000,0,0,0,0,100,100,0,0,1,"
            f"{max(4, int(w * scale * 0.077))},3,5,{SIDE_MARGIN},{SIDE_MARGIN},0,1", "", "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    events = []
    chunks = chunk_words(words)
    for n, chunk in enumerate(chunks):
        next_start = chunks[n + 1][0][0] if n + 1 < len(chunks) else duration
        texts = [_clean(word[2]) for word in chunk]
        fitted = fitted_size(" ".join(t for t in texts if t), font_size, w)
        fit = "" if fitted == font_size else f"\\fs{fitted}"
        for k, (start, end, _text) in enumerate(chunk):
            until = chunk[k + 1][0] if k + 1 < len(chunk) else min(next_start, end + HOLD_S, duration)
            if until - start < 0.05:
                continue
            line = " ".join(("{\\c" + YELLOW + "&}" + t + "{\\c" + WHITE + "&}") if i == k else t
                            for i, t in enumerate(texts) if t)
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(until)},Cap,,0,0,0,,{{\\pos({w // 2},{caption_y}){fit}}}{line}")
    return "\n".join(head + events) + "\n"


def install_font(folder: Path) -> str:
    """Copies a bold system font into <folder>/.fonts (relative fontsdir: no Windows path escaping in the
    filter graph). Returns the font family name; libass falls back to its default when none is found."""
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename, family in FONTS:
        source = fonts / filename
        if source.is_file():
            target = folder / ".fonts" / filename
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            return family
    return "Arial"
