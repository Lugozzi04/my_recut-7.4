"""Clips: short videos cut from an edit (a ReCut project or an exported video).

Headless and CLI-first, like analysis.packaging, whose local analysis (transcript, highlights, games and
the source <-> edited map) it reuses without any provider call:

  candidates.py  packaging highlights -> clip candidates on sentence boundaries, 20-60 s, ranked
  render.py      one FFmpeg call per clip from the ORIGINAL sources (kept parts only), partial -> final
  service.py     ClipSession: finds or builds the analysis, keeps clips.json, renders selected clips
"""
