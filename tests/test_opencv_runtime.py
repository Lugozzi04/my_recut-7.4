"""OpenCV used first on a worker thread hangs the process at exit (Windows); the main-thread warm-up avoids it."""
from __future__ import annotations

import subprocess
import sys

import pytest

SCRIPT = """
import threading, numpy as np
from utils.opencv_runtime import warm_up_opencv
assert warm_up_opencv()
def work():
    import cv2
    image = (np.random.rand(360, 640, 3) * 255).astype(np.uint8)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cascade.detectMultiScale(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 1.1, 4)
thread = threading.Thread(target=work)
thread.start()
thread.join()
print("worker done")
"""


def test_process_exits_after_opencv_on_a_worker_thread_when_warmed_up_on_the_main_thread():
    pytest.importorskip("cv2")
    done = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=90)
    assert done.returncode == 0 and "worker done" in done.stdout
