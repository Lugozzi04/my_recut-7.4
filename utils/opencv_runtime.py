"""OpenCV process-exit hang on Windows.

When OpenCV's first real operation runs on a worker thread (analysis jobs run on QThreads), the process hangs
at exit (opencv-python 4.12: Python finishes, the process never terminates). One tiny OpenCV call on the main
thread first avoids it: the GUI calls warm_up_opencv() once, deferred, after the window is built.
"""
from __future__ import annotations


def warm_up_opencv() -> bool:
    """Returns False when OpenCV is not installed (the features that need it report that themselves)."""
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001 - optional dependency
        return False
    cv2.cvtColor(np.zeros((2, 2, 3), dtype=np.uint8), cv2.COLOR_BGR2GRAY)
    return True
