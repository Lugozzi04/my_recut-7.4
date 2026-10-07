"""Keep the frozen CLI attached to its terminal, detach an Explorer GUI launch."""
from __future__ import annotations

import os
import sys


def detach_gui_console() -> bool:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    import ctypes

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        processes = (ctypes.c_ulong * 2)()
        kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]
        kernel.GetConsoleProcessList.restype = ctypes.c_ulong
        # A parent terminal must remain attached and visible. Explorer launches
        # give this process its own console, which the GUI does not need.
        if kernel.GetConsoleProcessList(processes, len(processes)) != 1:
            return False
        if processes[0] != os.getpid():
            return False
        kernel.FreeConsole.argtypes = []
        kernel.FreeConsole.restype = ctypes.c_int
        if not kernel.FreeConsole():
            return False
        # FreeConsole invalidates its original handles. GUI libraries still
        # write diagnostics, so provide usable sinks rather than dead handles.
        sys.stdin = open(os.devnull, "r", encoding="utf-8")
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
        return True
    except (AttributeError, OSError):
        return False
