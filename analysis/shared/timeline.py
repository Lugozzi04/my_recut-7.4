"""Authoritative timeline mapping, shared with Packaging and per-group exports.

An alias, not a second implementation: EditMap is constructed from the existing
ProjectExportPlanner and therefore includes manual cuts and final_keeps.
"""
from analysis.packaging.timeline import EditMap as TimelineMapper, KeepPiece

__all__ = ["TimelineMapper", "KeepPiece"]
