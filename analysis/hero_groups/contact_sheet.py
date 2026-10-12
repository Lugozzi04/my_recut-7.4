"""Local diagnostic portrait sheet; no video decode, rendering or AI calls."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
import numpy as np
from analysis.gameplay.hearthstone.visual.templates import require_opencv


def write_contact_sheet(grouping: dict[str, Any], destination: Path) -> Path:
    cv = require_opencv()
    rows = grouping.get("games", [])
    width, cell_height = 720, 116
    sheet = np.full((max(1, len(rows)) * cell_height, width, 3), 32, np.uint8)
    for index, row in enumerate(rows):
        y = index * cell_height
        if row.get("portrait"):
            image = cv.imdecode(np.frombuffer(base64.b64decode(row["portrait"]), np.uint8), cv.IMREAD_COLOR)
            if image is not None:
                sheet[y + 8 : y + 108, 8:88] = cv.resize(image, (80, 100))
        game = row["game"]
        from .labels import class_label

        text = f"Game {game['index']}  {game['result']}  " + (
            class_label(row.get("group"))
            if grouping.get("grouping_mode") == "class"
            else f"Group {row.get('group') or 'unknown'}"
        )
        cv.putText(sheet, text, (104, y + 30), cv.FONT_HERSHEY_SIMPLEX, 0.6, (240, 240, 240), 1, cv.LINE_AA)
        cv.putText(
            sheet,
            f"similarity {row.get('class_similarity', row.get('similarity', 0)):.3f} / support {row.get('class_support', row.get('support', 0))}",
            (104, y + 58),
            cv.FONT_HERSHEY_SIMPLEX,
            0.5,
            (200, 200, 200),
            1,
            cv.LINE_AA,
        )
        cv.putText(
            sheet,
            (
                f"{row.get('class_source')} / {row.get('class_status')} / margin {row.get('class_margin', 0):.3f}"
                if grouping.get("grouping_mode") == "class"
                else str(row.get("reason", ""))
            )[:76],
            (104, y + 84),
            cv.FONT_HERSHEY_SIMPLEX,
            0.4,
            (160, 190, 220),
            1,
            cv.LINE_AA,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv.imencode(".jpg", sheet)
    if not ok:
        raise ValueError("Could not encode portrait sheet.")
    destination.write_bytes(encoded.tobytes())
    return destination
