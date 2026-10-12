"""Pluggable boundary resolvers: each picks ONE existing candidate of a BoundaryContext.

  exact      ExactBoundaryResolver (gameplay anchor; local, deterministic)
  claude     ClaudeBoundaryResolver (local `claude` CLI; sends a text context to its provider)
  codex      CodexBoundaryResolver  (local `codex` CLI; sends a text context to its provider)
  heuristic  reserved: rules over context features (not implemented)
  local      reserved: LocalModelBoundaryResolver scoring feature_vector() rows (not implemented)

create_resolver(mode, options) is the single selection point used by the debug
CLI and, later, by the main ReCut CLI (--boundary-mode).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Protocol

from .candidates import FEATURE_NAMES
from .models import BoundaryContext, BoundaryDecision, BoundaryFormatError, BoundaryType, CandidateSource

AGENT_PAYLOAD_FORMAT = "recut_boundary_agent_request"
AGENT_RESPONSE_SCHEMA = {"selected_candidate": "integer index into candidates", "confidence": "number in [0, 1]"}
AGENT_RESPONSE_KEYS = frozenset({"selected_candidate", "confidence", "reason", "segment_labels"})
SEGMENT_LABELS = ("PREVIOUS_CONTENT", "CURRENT_GAME_SPECIFIC", "GENERAL_COMMENTARY", "NEXT_CONTENT", "UNCERTAIN",
                  # END of a KEPT game (end_policy.END_LABELS)
                  "WIN_REACTION", "GAME_SPECIFIC_RECAP", "GAME_STRATEGY", "GENERAL_DECK_TALK", "CHAT_INTERACTION",
                  "UNRELATED_COMMENTARY", "NEXT_GAME")

BOUNDARY_MODES = ("exact", "claude", "codex")
RESERVED_MODES = ("heuristic", "local")
REMOTE_MODES = frozenset({"claude", "codex"})  # these send text context to an external provider


class BoundaryResolver(Protocol):
    name: str

    def resolve(self, context: BoundaryContext) -> BoundaryDecision: ...


class ExactBoundaryResolver:
    """Baseline: START -> game start anchor, END -> result anchor (or EOF for a terminal partial)."""

    name = "exact"

    def resolve(self, context: BoundaryContext) -> BoundaryDecision:
        if context.boundary_type == BoundaryType.START:
            sources = [CandidateSource.GAME_START_ANCHOR]
        else:
            sources = [CandidateSource.RESULT_ANCHOR, CandidateSource.END_OF_FILE]
        index = context.candidate_index(sources)
        if index is not None:
            return BoundaryDecision(context.boundary_type, context.candidates[index].timestamp, index, 1.0, self.name,
                                    "gameplay anchor", self.name)
        anchor = context.settings.get("anchor")
        if not isinstance(anchor, (int, float)):
            raise BoundaryFormatError("Context has neither an anchor candidate nor an anchor setting")
        # Estimated anchors (missing VS/result) are candidates at the anchor time; dedup may move them <= 0.25 s.
        near = [i for i, c in enumerate(context.candidates) if abs(c.timestamp - float(anchor)) <= 0.3]
        if near:
            best = min(near, key=lambda i: abs(context.candidates[i].timestamp - float(anchor)))
            return BoundaryDecision(context.boundary_type, context.candidates[best].timestamp, best, 1.0, self.name,
                                    f"{context.settings.get('anchor_kind', 'anchor')} candidate", self.name)
        return BoundaryDecision(context.boundary_type, float(anchor), None, 1.0, self.name, "anchor (no candidate)",
                                self.name)


def agent_payload(context: BoundaryContext, *, feature_names: list[str] | None = None,
                  max_transcript_segments: int = 40) -> dict[str, Any]:
    """Machine-oriented JSON view of a context (all numeric features). See agents.py for the LLM prompt."""
    names = feature_names or [n for n in FEATURE_NAMES if not n.startswith("source_")]
    transcript = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text}
                  for s in context.transcript][:max_transcript_segments]
    return {
        "format": AGENT_PAYLOAD_FORMAT, "version": 1,
        "task": ("Select the candidate where the removed game's content " +
                 ("begins (START)." if context.boundary_type == BoundaryType.START else "ends (END).")),
        "boundary_type": context.boundary_type.value,
        "game_result": context.game.result, "game_termination": context.game.termination.value,
        **{k: (None if v is None else round(v, 3)) for k, v in context.anchors.items()},
        "window": [round(context.window.start, 3), round(context.window.end, 3)],
        "evidence_status": {"transcript": context.transcript_status.status, "audio": context.audio_status.status,
                            "visual": context.visual_status.status},
        "transcript": transcript,
        "silences": [[round(r.start, 2), round(r.end, 2)] for r in context.silences],
        "candidates": [{"index": i, "timestamp": c.timestamp, "sources": [s.value for s in c.sources],
                        "features": {n: (None if c.features.get(n) is None else round(float(c.features[n] or 0), 3))
                                     for n in names}}
                       for i, c in enumerate(context.candidates)],
        "response_schema": AGENT_RESPONSE_SCHEMA,
    }


def parse_agent_response(context: BoundaryContext, raw: str | Mapping[str, Any], *, resolver: str = "agent",
                         strict: bool = True, metadata: Mapping[str, Any] | None = None,
                         allowed_indices: Sequence[int] | None = None) -> BoundaryDecision:
    """Validate {"selected_candidate": int, "confidence": 0..1[, "reason": str, "segment_labels": [...]]}.

    strict=True also rejects unexpected keys. The agent can only select an
    existing candidate (and, with allowed_indices, only one it was shown); it can
    never introduce a timestamp of its own. segment_labels, when present, must
    use SEGMENT_LABELS; they are kept in metadata as the agent's reasoning trace.
    """
    try:
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError) as exc:
        raise BoundaryFormatError(f"Agent response is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BoundaryFormatError("Agent response must be a JSON object")
    if strict and set(value) - AGENT_RESPONSE_KEYS:
        raise BoundaryFormatError(f"Unexpected keys in agent response: {sorted(set(value) - AGENT_RESPONSE_KEYS)}")
    index, confidence = value.get("selected_candidate"), value.get("confidence")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(context.candidates):
        raise BoundaryFormatError("selected_candidate must index an existing candidate")
    if allowed_indices is not None and index not in set(allowed_indices):
        raise BoundaryFormatError("selected_candidate was not among the candidates shown to the agent")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise BoundaryFormatError("confidence must be a number in [0, 1]")
    reason = value.get("reason", "")
    if not isinstance(reason, str):
        raise BoundaryFormatError("reason must be a string")
    meta = dict(metadata or {})
    labels = value.get("segment_labels")
    if labels is not None:
        if not isinstance(labels, list) or any(label not in SEGMENT_LABELS for label in labels):
            raise BoundaryFormatError(f"segment_labels must be a list of {', '.join(SEGMENT_LABELS)}")
        meta["segment_labels"] = list(labels)
    return BoundaryDecision(context.boundary_type, context.candidates[index].timestamp, index, float(confidence),
                            resolver, reason[:500], resolver, False, meta)


@dataclass(frozen=True)
class ResolverOptions:
    """Options for create_resolver(); agent fields are ignored by local resolvers."""

    timeout_s: float = 120.0
    fallback: str | None = "exact"  # None: an agent failure raises instead of falling back
    model: str | None = None
    effort: str | None = None  # claude --effort / codex model_reasoning_effort
    executable: str | None = None  # explicit CLI path; default: AUTO_CUTTER_<NAME>_CLI or PATH lookup
    use_cache: bool = True
    cache_directory: str | Path | None = None
    log_path: str | Path | None = None  # local JSONL debug log; None = disabled
    end_policy: str = "balanced"  # END of kept games: tight | balanced | extended (see end_policy.py)
    max_concurrent: int = 4  # agent decisions in flight at once (pure waiting on the provider; 1 = sequential)


def create_resolver(mode: str, options: ResolverOptions | None = None) -> BoundaryResolver:
    options = options or ResolverOptions()
    if mode == "exact":
        return ExactBoundaryResolver()
    if mode in REMOTE_MODES:
        from .agents import ClaudeBoundaryResolver, CodexBoundaryResolver

        if options.fallback in REMOTE_MODES:
            raise ValueError("The fallback of an agent resolver must be local")
        fallback = None if options.fallback is None else create_resolver(options.fallback, options)
        resolver_type = ClaudeBoundaryResolver if mode == "claude" else CodexBoundaryResolver
        return resolver_type(options, fallback=fallback)
    if mode in RESERVED_MODES:
        raise ValueError(f"Boundary mode '{mode}' is reserved but not implemented yet")
    raise ValueError(f"Unknown boundary mode '{mode}' (choices: {', '.join(BOUNDARY_MODES)})")


def resolve_contexts(contexts: Sequence[BoundaryContext], resolver: BoundaryResolver) -> list[BoundaryDecision]:
    """One decision per context, in order; agent failures are already turned into fallbacks."""
    return [resolver.resolve(context) for context in contexts]
