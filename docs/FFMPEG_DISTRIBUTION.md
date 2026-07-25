# FFmpeg distribution record

This document records the FFmpeg binaries currently shipped with Auto Cutter.
It is operational documentation, not legal advice.

## Binary provenance

- Package: Gyan FFmpeg `8.0.1-essentials_build`
- Package page: https://www.gyan.dev/ffmpeg/builds/
- License reported by `ffmpeg -L`: GPL version 3 or later
- `bin/ffmpeg.exe` SHA-256:
  `5af82a0d4fe2b9eae211b967332ea97edfc51c6b328ca35b827e73eac560dc0d`
- `bin/ffprobe.exe` SHA-256:
  `192a1d6899059765ac8c39764fc3148d4e6049955956dc2029f81f4bd6a8972d`

The binaries report a static GPLv3 build with `--enable-gpl`,
`--enable-version3`, `--enable-libx264`, `--enable-libx265` and other external
libraries. Run `bin/ffmpeg.exe -version` for the complete configure line.

## Corresponding source

`build/prepare-third-party.ps1` downloads the official FFmpeg 8.0.1 source
archive and verifies:

`05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41`

The archive and its `COPYING.GPLv3` are included under `licenses/` in the
application build.

## Release checklist

1. Verify the two executable hashes above.
2. Run `build/prepare-third-party.ps1`.
3. Confirm the installer contains `licenses/ffmpeg-8.0.1.tar.xz`,
   `licenses/FFMPEG-GPL-3.0.txt`, and `licenses/THIRD_PARTY_NOTICES.md`.
4. Publish corresponding source next to every public binary download.
5. Preserve the complete FFmpeg configure line and external-library versions.
6. Obtain legal review covering all statically linked external libraries and
   the separation between Auto Cutter and the FFmpeg executables.

Do not release if the binary version or hashes change without updating this
record and the corresponding source package.
