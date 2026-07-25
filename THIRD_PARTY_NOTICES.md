# Third-party notices

Auto Cutter includes or depends on third-party software. Copyright remains
with the respective authors.

## FFmpeg

The Windows package includes `ffmpeg.exe` and `ffprobe.exe` from the Gyan
FFmpeg 8.0.1 essentials build.

- Upstream: https://ffmpeg.org/
- Windows build: https://www.gyan.dev/ffmpeg/builds/
- Corresponding FFmpeg source: https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz
- License: GNU General Public License version 3 or later

This build reports `--enable-gpl`, `--enable-version3`, `--enable-static`,
`--enable-libx264` and other external libraries. The packaged
`licenses/ffmpeg-8.0.1.tar.xz` and `licenses/FFMPEG-GPL-3.0.txt` are prepared
by the release build. See `docs/FFMPEG_DISTRIBUTION.md` for hashes and release
requirements.

FFmpeg is provided without warranty. Auto Cutter is a separate application
that invokes the FFmpeg command-line programs.

## Python runtime dependencies

- PySide6 6.10.2: LGPLv3/GPLv3/commercial licensing options,
  https://doc.qt.io/qtforpython-6/licenses.html
- NumPy 2.2.6: BSD-3-Clause, https://numpy.org/doc/stable/license.html
- PyAV 16.1.0: BSD-3-Clause, https://github.com/PyAV-Org/PyAV

Optional AI dependencies are listed and pinned in `requirements-ai.txt`.
Their license files must be collected and reviewed before distributing an AI
runtime package.
