"""Agent boundary resolvers through the user's local `claude` / `codex` CLIs.

1 context -> 1 non-interactive CLI call -> 1 validated decision; no retries or
voting. The agent sees a compact, versioned text prompt built from the
BoundaryContext (window transcript, silences, markers, candidates) and may only
return the index of an existing candidate. It never receives the video, file
paths or repository access: the CLI runs in an empty temporary directory,
Claude with every built-in tool disabled, Codex in a read-only sandbox.

PRIVACY: these modes send the window transcript and timing evidence to the
provider configured in the user's CLI. The exact (and future local) modes
send nothing. No analytics; the optional debug log is local and excludes text.

Any failure (missing CLI, non-zero exit, timeout, malformed or invalid output)
becomes a fallback decision (default: exact) with the reason in metadata.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from .models import (BoundaryContext, BoundaryDecision, BoundaryFormatError, BoundaryType, GameDisposition,
                     GameTermination)
from .resolvers import BoundaryResolver, ResolverOptions, parse_agent_response
from .shortlist import shortlist

# v3: content ownership for KEPT games (keep-games wins: only complete WIN islands survive).
# v2 (removal semantics) and v1 decisions are never reused: the version is part of the cache key.
PROMPT_VERSION = "boundary_agent_prompt_v3"
# END of a KEPT game: editorial relevance labels + tight/balanced/extended (end_policy.py). Only these
# contexts change meaning; START and removed games keep v3 and their cached decisions.
END_PROMPT_VERSION = "boundary_agent_prompt_v4_end"
_LOG_LOCK = threading.Lock()  # decisions may run concurrently (gameplay_editing): one JSONL line each

LABELS = ("PREVIOUS_CONTENT", "CURRENT_GAME_SPECIFIC", "GENERAL_COMMENTARY", "NEXT_CONTENT", "UNCERTAIN")

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "segment_labels": {"type": "array", "items": {"type": "string", "enum": list(LABELS)},
                           "description": "one label per transcript segment S0, S1, ... in order"},
        "selected_candidate": {"type": "integer", "description": "index of ONE listed candidate"},
        "confidence": {"type": "number", "description": "0..1"},
        "reason": {"type": "string", "description": "one or two short sentences"},
    },
    "required": ["segment_labels", "selected_candidate", "confidence", "reason"],
    "additionalProperties": False,
}

def _schema(labels: tuple[str, ...]) -> dict[str, Any]:
    return {**RESPONSE_SCHEMA, "properties": {**RESPONSE_SCHEMA["properties"], "segment_labels": {
        **RESPONSE_SCHEMA["properties"]["segment_labels"], "items": {"type": "string", "enum": list(labels)}}}}


def is_kept_end(context: BoundaryContext) -> bool:
    return context.boundary_type == BoundaryType.END and context.game.disposition == GameDisposition.KEEP


def prompt_version(context: BoundaryContext) -> str:
    return END_PROMPT_VERSION if is_kept_end(context) else PROMPT_VERSION


SYSTEM_PROMPT = (
    "You assist a video editor that removes some gameplay matches from a long livestream recording "
    "(the streamer talks over the game). For ONE cut boundary you receive local evidence (speech "
    "transcript with timestamps, silences, gameplay markers, visual changes) and a numbered list of "
    "candidate cut points. First decide what each transcript segment is about, then choose exactly one "
    "listed candidate. Never invent a timestamp. Do not use tools, files or commands. Reply only with "
    "the requested JSON object."
)

LABEL_GUIDE = [
    "PREVIOUS_CONTENT: belongs to what comes before THIS game: the previous game's result, reactions and "
    "comments about it, or a previous unrelated activity.",
    "CURRENT_GAME_SPECIFIC: owned by THIS game: its introduction ('let's see what we face'), queue talk "
    "leading into it, play-by-play, the reaction to its result, comments on its plays, opponent, hand or turn "
    "('here I should have played X', 'this one could not be won').",
    "GENERAL_COMMENTARY: not tied to a specific game: 'against this matchup in general you should...', "
    "'this list is very strong in my opinion', event or stream talk.",
    "NEXT_CONTENT: what follows THIS game: a transition ('ok, let's see the next one'), queueing again, the "
    "introduction of the next game ('against Warrior...'), a new activity (opening packs, deck building).",
    "UNCERTAIN: cannot tell.",
]

GOALS = {
    ("KEEP", BoundaryType.START): [
        "THIS game is KEPT. The final video keeps only the region from this START cut to this game's END "
        "cut; everything before START is removed.",
        "Step 1: label every transcript segment. Step 2: find where content owned by THIS game begins. "
        "Step 3: choose the candidate that best separates what precedes it from this game's content.",
        "Never include PREVIOUS_CONTENT (reactions/comments about the previous game or unrelated talk).",
        "The introduction of this game may begin before the VS screen; then START goes before it. Include "
        "GENERAL_COMMENTARY just before it only when it clearly leads into this game; otherwise leave it "
        "out: chatter not owned by a kept game is removed from the final video.",
        "A sentence that starts before the VS and continues into the game must not be split.",
    ],
    ("KEEP", BoundaryType.END): [
        "THIS game is KEPT. The final video keeps only the region from this game's START cut to this END "
        "cut; everything after END is removed.",
        "Step 1: label every transcript segment. Step 2: find where content owned by THIS game ends. "
        "Step 3: choose the candidate that best separates this game's content from what follows.",
        "Include the reaction to this game's result and comments specific to this game.",
        "Include GENERAL_COMMENTARY only while it directly continues this game's discussion without a break "
        "(e.g. a remark on the deck prompted by this game). Stop before a new topic, a transition to the next "
        "game, or NEXT_CONTENT.",
    ],
    ("REMOVE", BoundaryType.START): [
        "THIS game is REMOVED: the region from this START cut to its END cut is cut out; everything before "
        "START stays in the video.",
        "Place START where content owned by THIS game begins; do not cut off PREVIOUS_CONTENT or "
        "GENERAL_COMMENTARY.",
        "Its introduction may begin before the VS screen; a sentence crossing the VS must not be split.",
    ],
    ("REMOVE", BoundaryType.END): [
        "THIS game is REMOVED: the region from its START cut to this END cut is cut out; everything after END "
        "stays in the video.",
        "Place END right after the last content owned by THIS game (reactions and comments about it must go); "
        "keep GENERAL_COMMENTARY and NEXT_CONTENT.",
    ],
}

COMMON_GOALS = [
    "The streamer often plays the SAME deck for many games, so ownership shifts are subtle: decide from "
    "whether a sentence refers to THIS specific game (its events, result, plays, opponent) or makes a general "
    "point. Do not rely on class, deck, matchup or keyword changes.",
    "Prefer a candidate at a pause or sentence boundary so the remaining speech flows naturally.",
    "Only candidates that keep the whole game inside the region are listed (START at or before the VS "
    "anchor, END at or after the result anchor).",
    "The ASR transcript may contain recognition errors. If the evidence is insufficient, choose the "
    "anchor candidate with low confidence.",
]

ANCHOR_KINDS = {
    "game_start": "observed VS screen", "game_end": "observed result screen",
    "estimated_board_start": "ESTIMATED from board appearance (VS not observed)",
    "estimated_board_end": "ESTIMATED from board disappearance (result not observed)",
    "previous_game_end": "previous game's end (VS not observed)", "next_game_start": "next game's start (result not observed)",
    "end_of_file": "end of the video", "start_of_file": "start of the video",
}


def _rel(t: float | None, anchor: float) -> str:
    return "n/a" if t is None else f"{t:.2f}s ({t - anchor:+.2f})"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _neighbor(label: str, item: Any, anchor: float) -> str:
    if not isinstance(item, dict):
        return f"- {label}: none in the video"
    fate = "KEPT in the video" if item.get("disposition") == "KEEP" else "also REMOVED"
    return (f"- {label}: game {item.get('index')} result {item.get('result')} ({item.get('termination')}), {fate}; "
            f"start {_rel(item.get('start'), anchor)}, end {_rel(item.get('end'), anchor)}")


def _segment_id(context: BoundaryContext, start: float | None) -> str:
    if start is None:
        return "-"
    for i, segment in enumerate(context.transcript):
        if abs(segment.start - start) < 0.01:
            return f"S{i}"
    return "-"


KEPT_END_GOALS = [
    "THIS game is KEPT and was WON. The final video keeps this game up to this END cut; everything after END is "
    "removed. The video is a YouTube edit: the viewer wants the game and the reaction to it, not the stream.",
    "Decide in this order of priority: (1) relevance of each sentence for the YouTube video; (2) whether it "
    "belongs to the game that just ended; (3) how much useless content would be kept; (4) how natural the cut "
    "point is. Sentence completeness never outweighs relevance.",
    "Step 1: label every transcript segment with the labels below. Step 2: choose the candidate right after the "
    "last relevant segment. Keep the reaction to the win; keep a recap or strategy point only when it is "
    "specific to THIS game and adds value; cut chat small talk, personal or unrelated talk, thanks, generic "
    "deck/stream talk and the transition to the next game - even when it is mixed with relevant sentences later.",
    "If irrelevant talk starts and goes on, it is better to cut in the middle of that sentence (a WORD_GAP "
    "candidate) than to keep 15-30 more seconds of it. Prefer a pause or a sentence end when one is close.",
    "Never cut the reaction to the win. Keep at least a moment of the result screen.",
]
KEPT_END_COMMON = [
    "Only candidates at or after the result anchor are listed. WORD_GAP candidates are pauses between two words "
    "inside speech.",
    "The ASR transcript may contain recognition errors; label garbled text UNCERTAIN.",
]


def build_prompt(context: BoundaryContext, *, indices: list[int] | None = None, max_transcript_segments: int = 80,
                 max_text_chars: int = 500, end_policy: str | None = None) -> str:
    """Versioned prompt: anchors, neighbours, full window transcript, evidence, shortlisted candidates."""
    from .end_policy import LABEL_GUIDE as END_LABEL_GUIDE
    from .end_policy import MODE_PROMPT, mode as end_mode

    c = context
    kept_end = is_kept_end(c)
    shown = indices if indices is not None else shortlist(c)
    anchor = float(c.settings.get("anchor", c.window.start))
    anchor_kind = str(c.settings.get("anchor_kind", "game_start" if c.boundary_type == BoundaryType.START else "game_end"))
    terminal = c.game.termination == GameTermination.IN_PROGRESS_AT_EOF
    mode = "KEEP" if c.game.disposition == GameDisposition.KEEP else "REMOVE"
    fate = "KEPT in the final video" if mode == "KEEP" else "REMOVED from the final video"
    lines = [f"PROMPT_VERSION: {prompt_version(c)}",
             f"BOUNDARY: {c.boundary_type.value} of game {c.game.index}, which is {fate} "
             f"(result {c.game.result}, "
             f"completeness {c.game.termination.value}"
             f"{', the video ends while it is still being played' if terminal else ''}). Policy decision: {c.game.reason}",
             f"ANCHOR: {anchor:.2f}s = {ANCHOR_KINDS.get(anchor_kind, anchor_kind)}. Offsets below are relative to it.",
             "", "TASK:"]
    if kept_end:
        lines += [f"- {goal}" for goal in KEPT_END_GOALS + [MODE_PROMPT[end_mode(end_policy).name]] + KEPT_END_COMMON]
        lines += ["", "SEGMENT LABELS (label every transcript segment first; speech before the result is the game "
                      "itself - label it GAME_SPECIFIC_RECAP):"] + [f"- {item}" for item in END_LABEL_GUIDE]
    else:
        lines += [f"- {goal}" for goal in GOALS[(mode, c.boundary_type)] + COMMON_GOALS]
        lines += ["", "SEGMENT LABELS (label every transcript segment first):"] + [f"- {item}" for item in LABEL_GUIDE]
    a = c.anchors
    lines += ["", "TIMELINE (seconds; offsets relative to the anchor):",
              f"- this game start (VS): {_rel(a.get('game_start'), anchor)}",
              f"- this game result: {_rel(a.get('game_end'), anchor)}",
              _neighbor("previous game", c.neighbors.get("previous"), anchor),
              _neighbor("next game", c.neighbors.get("next"), anchor),
              f"- end of video: {_rel(a.get('end_of_file'), anchor)}",
              f"- analysed window: {_rel(c.window.start, anchor)} to {_rel(c.window.end, anchor)}",
              "", "EVIDENCE AVAILABLE: "
              f"transcript={c.transcript_status.status}, audio={c.audio_status.status}, visual={c.visual_status.status}",
              "", "TRANSCRIPT (this window only; segment ids S0, S1, ...):"]
    if not c.transcript:
        lines.append("- (no transcribed speech)")
    for i, s in enumerate(c.transcript[:max_transcript_segments]):
        lines.append(f"S{i} [{s.start - anchor:+.2f} .. {s.end - anchor:+.2f}] {s.text[:max_text_chars]}")
    if len(c.transcript) > max_transcript_segments:
        lines.append(f"- ... {len(c.transcript) - max_transcript_segments} more segments omitted (label them UNCERTAIN)")
    lines += ["", "SILENCES (no voice, offsets): " + (", ".join(
        f"[{r.start - anchor:+.2f} .. {r.end - anchor:+.2f}]" for r in c.silences) or "none")]
    markers = ", ".join(f"{m.kind.lower()}{' (estimated anchor)' if m.detail.get('estimated_anchor') else ''} "
                        f"{m.timestamp - anchor:+.2f}" for m in c.gameplay_markers)
    peaks = sorted(c.visual, key=lambda v: v.change, reverse=True)[:6]
    lines += ["GAMEPLAY MARKERS: " + (markers or "none"),
              "STRONGEST VISUAL CHANGES (0-1): " + (", ".join(
                  f"{v.timestamp - anchor:+.2f}={v.change:.2f}" for v in sorted(peaks, key=lambda v: v.timestamp))
                  or "none"),
              "", f"CANDIDATES (choose one index; {len(shown)} of {len(c.candidates)} shown):"]
    for i in shown:
        cand = c.candidates[i]
        f, ev = cand.features, cand.evidence
        before = _segment_id(c, (ev.get("transcript_segment_before") or {}).get("start"))
        after = _segment_id(c, (ev.get("transcript_segment_after") or {}).get("start"))
        inside = _segment_id(c, (ev.get("transcript_segment_containing") or {}).get("start"))
        lines.append(
            f"#{i} t={cand.timestamp:.2f} ({cand.timestamp - anchor:+.2f}) "
            f"sources={'+'.join(s.value for s in cand.sources)} | "
            f"segment before/inside/after={before}/{inside}/{after} | "
            f"silence before/after={_num(f.get('silence_before_s'))}/{_num(f.get('silence_after_s'))}s | "
            f"speech 5s before/after={_pct(f.get('speech_density_before'))}/{_pct(f.get('speech_density_after'))} | "
            f"sentence boundary={'yes' if f.get('is_sentence_boundary') == 1.0 else 'no'} | "
            f"visual change={_num(f.get('visual_change_score'))}")
    lines += ["", 'ANSWER with JSON only: {"segment_labels": [<one label per S0..Sn>], "selected_candidate": '
              '<index>, "confidence": <0..1>, "reason": "<short>"}']
    return "\n".join(lines)


def context_digest(context: BoundaryContext) -> str:
    canonical = json.dumps(context.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def apply_end_policy(context: BoundaryContext, decision: BoundaryDecision, policy: str | None,
                     choose: Any = None) -> BoundaryDecision:
    """Deterministic END policy over the agent's own segment labels (kept games only); see end_policy.py."""
    from .end_policy import choose_end
    from .shortlist import eligible

    labels = list(decision.metadata.get("segment_labels") or [])
    if not labels:
        return decision
    index, trace = (choose or choose_end)(context, labels, decision.candidate_index, eligible(context), policy)
    meta = {**decision.metadata, "end_policy_trace": trace}
    if index is None or index == decision.candidate_index:
        return replace(decision, metadata=meta)
    detail = (f"{decision.detail} | end policy {trace.get('mode')}: moved to #{index} "
              f"(relevant speech ends {trace.get('target')}, stopped by {trace.get('stopped_by')})")
    return replace(decision, timestamp=context.candidates[index].timestamp, candidate_index=index,
                   detail=detail[:500], metadata=meta)


class AgentCallError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail[:300]


def _kill_tree(process: subprocess.Popen[str]) -> None:
    try:
        if sys.platform == "win32":  # .cmd shims spawn node children that kill() alone would orphan
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            process.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
        process.communicate(timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def run_agent_process(cmd: list[str], stdin_text: str, workdir: Path, timeout_s: float) -> tuple[str, str]:
    """One non-interactive CLI run in workdir; AgentCallError on launch failure, timeout or non-zero exit.

    Shared by the boundary resolvers and AI packaging (analysis.packaging.agent).
    """
    try:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=workdir, text=True, encoding="utf-8", errors="replace",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise AgentCallError("cli_missing", str(exc)) from exc
    try:
        stdout, stderr = process.communicate(stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(process)
        raise AgentCallError("timeout", f"no answer within {timeout_s}s") from exc
    if process.returncode != 0:
        tail = (stderr or "").strip().splitlines()[-1:] or [f"exit code {process.returncode}"]
        raise AgentCallError("process_failed", tail[0])
    return stdout or "", stderr or ""


class AgentCliResolver:
    """Shared machinery; subclasses define the CLI command and how to read its answer."""

    name = "agent"
    executable_names: tuple[str, ...] = ()
    env_var = ""

    def __init__(self, options: ResolverOptions | None = None, *, fallback: BoundaryResolver | None = None) -> None:
        self.options = options or ResolverOptions()
        self.fallback = fallback
        directory = self.options.cache_directory
        if directory is None:
            from utils.runtime_paths import cache_root
            directory = cache_root() / "boundaries" / "agent_decisions"
        self.cache_directory = Path(directory) / self.name  # never shared between providers

    # ---- CLI specifics -------------------------------------------------------------------------
    def locate(self) -> str | None:
        configured = self.options.executable or os.environ.get(self.env_var)
        if configured:
            return configured if Path(configured).is_file() else shutil.which(configured)
        for name in self.executable_names:
            found = shutil.which(name)
            if found:
                return found
        return None

    def command(self, executable: str, workdir: Path, schema_path: Path, output_path: Path) -> list[str]:
        raise NotImplementedError

    def stdin(self, prompt: str) -> str:
        return prompt

    def read_answer(self, stdout: str, output_path: Path) -> tuple[Any, dict[str, Any]]:
        raise NotImplementedError

    # ---- shared flow ---------------------------------------------------------------------------
    def cache_key(self, digest: str, version: str | None = None, end_policy: str | None = None) -> str:
        value: dict[str, Any] = {"context": digest, "resolver": self.name, "prompt_version": version or PROMPT_VERSION,
                                 "model": self.options.model, "effort": self.options.effort}
        if end_policy is not None:  # only kept-END prompts depend on the editorial mode
            value["end_policy"] = end_policy
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()

    def call(self, prompt: str, schema: dict[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
        executable = self.locate()
        if executable is None:
            raise AgentCallError("cli_missing", f"'{self.executable_names[0]}' not found (set {self.env_var})")
        with tempfile.TemporaryDirectory(prefix=f"recut-{self.name}-") as folder:
            root = Path(folder)
            workdir = root / "empty"
            workdir.mkdir()
            schema_path, output_path = root / "schema.json", root / "answer.json"
            schema_path.write_text(json.dumps(schema or RESPONSE_SCHEMA), encoding="utf-8")
            cmd = self.command(executable, workdir, schema_path, output_path)
            stdout, _ = run_agent_process(cmd, self.stdin(prompt), workdir, self.options.timeout_s)
            return self.read_answer(stdout, output_path)

    def resolve(self, context: BoundaryContext) -> BoundaryDecision:
        from .end_policy import END_LABELS, choose_end, mode as end_mode

        started = time.perf_counter()
        digest = context_digest(context)
        kept_end = is_kept_end(context)
        version = prompt_version(context)
        policy = end_mode(self.options.end_policy).name if kept_end else None
        meta: dict[str, Any] = {"prompt_version": version, "context_id": digest[:16], "model": self.options.model,
                                "effort": self.options.effort, "cache_hit": False}
        if policy:
            meta["end_policy"] = policy
        key = self.cache_key(digest, version, policy)
        cache_file = self.cache_directory / f"{key}.json"
        try:
            answer, extra = None, {}
            if self.options.use_cache and cache_file.is_file():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    if cached.get("key") == key and cached.get("prompt_version") == version:
                        answer, extra, meta["cache_hit"] = cached["answer"], dict(cached.get("call", {})), True
                except (OSError, ValueError, KeyError):
                    answer = None
            indices = shortlist(context)
            meta["shown_candidates"] = indices
            if answer is None:
                answer, extra = self.call(build_prompt(context, indices=indices, end_policy=policy),
                                          _schema(END_LABELS) if kept_end else _schema(LABELS))
            meta.update(extra)
            meta["runtime_s"] = round(time.perf_counter() - started, 3)
            try:
                decision = parse_agent_response(context, answer if isinstance(answer, (str, dict)) else json.dumps(answer),
                                                resolver=self.name, metadata=meta, allowed_indices=indices)
            except BoundaryFormatError as exc:
                raise AgentCallError("invalid_output", str(exc)) from exc
            if self.options.use_cache and not meta["cache_hit"]:
                self._write_cache(cache_file, key, answer, extra, version)
            if kept_end:
                decision = apply_end_policy(context, decision, policy, choose_end)
        except AgentCallError as error:
            decision = self._fallback(context, error, meta, started)
        self._log(context, decision)
        return decision

    def _fallback(self, context: BoundaryContext, error: AgentCallError, meta: dict[str, Any],
                  started: float) -> BoundaryDecision:
        if self.fallback is None:
            raise error
        base = self.fallback.resolve(context)
        meta = {**meta, "error_code": error.code, "error_detail": error.detail,
                "runtime_s": round(time.perf_counter() - started, 3)}
        return replace(base, requested_resolver=self.name, fallback=True,
                       detail=f"{self.name} failed ({error.code}); used {base.resolver}", metadata=meta)

    def _write_cache(self, path: Path, key: str, answer: Any, extra: dict[str, Any],
                     version: str | None = None) -> None:
        from analysis.gameplay.cache import atomic_write_json

        try:
            atomic_write_json(path, {"format": "recut_boundary_agent_decision_cache", "key": key,
                                     "resolver": self.name, "prompt_version": version or PROMPT_VERSION,
                                     "answer": answer, "call": extra})
        except OSError:
            pass

    def _log(self, context: BoundaryContext, decision: BoundaryDecision) -> None:
        if self.options.log_path is None:
            return
        meta = decision.metadata
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "context_id": meta.get("context_id"),
                  "game_index": context.game.index, "boundary_type": context.boundary_type.value,
                  "requested_resolver": self.name, "resolver": decision.resolver,
                  "selected_candidate": decision.candidate_index, "timestamp": decision.timestamp,
                  "confidence": decision.confidence, "fallback": decision.fallback,
                  "error_code": meta.get("error_code"), "runtime_s": meta.get("runtime_s"),
                  "cache_hit": meta.get("cache_hit"), "prompt_version": meta.get("prompt_version"),
                  "model": meta.get("model"), "cost_usd": meta.get("cost_usd")}
        path = Path(self.options.log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK, path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")


class ClaudeBoundaryResolver(AgentCliResolver):
    """`claude -p` with structured output; all built-in tools and MCP servers disabled, no session saved."""

    name = "claude"
    executable_names = ("claude",)
    env_var = "AUTO_CUTTER_CLAUDE_CLI"

    def command(self, executable: str, workdir: Path, schema_path: Path, output_path: Path) -> list[str]:
        schema = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.is_file() else RESPONSE_SCHEMA
        cmd = [executable, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--system-prompt", SYSTEM_PROMPT]
        if self.options.model:
            cmd += ["--model", self.options.model]
        if self.options.effort:
            cmd += ["--effort", self.options.effort]
        return cmd

    def read_answer(self, stdout: str, output_path: Path) -> tuple[Any, dict[str, Any]]:
        try:
            envelope = json.loads(stdout)
        except ValueError as exc:
            raise AgentCallError("invalid_output", f"claude output is not JSON: {exc}") from exc
        if not isinstance(envelope, dict):
            raise AgentCallError("invalid_output", "claude output is not a JSON object")
        if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
            raise AgentCallError("process_failed", str(envelope.get("subtype") or envelope.get("result", ""))[:200])
        extra = {"cost_usd": envelope.get("total_cost_usd"), "agent_model": next(iter(envelope.get("modelUsage", {}) or {}), None),
                 "agent_duration_ms": envelope.get("duration_ms")}
        answer = envelope.get("structured_output")
        if answer is None:
            answer = envelope.get("result")
        if answer is None:
            raise AgentCallError("invalid_output", "claude returned no structured output")
        return answer, extra


class CodexBoundaryResolver(AgentCliResolver):
    """`codex exec` with --output-schema, read-only sandbox, ephemeral session, empty working directory."""

    name = "codex"
    executable_names = ("codex",)
    env_var = "AUTO_CUTTER_CODEX_CLI"

    def command(self, executable: str, workdir: Path, schema_path: Path, output_path: Path) -> list[str]:
        cmd = [executable, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
               "--ignore-rules", "--color", "never", "-C", str(workdir), "--output-schema", str(schema_path),
               "-o", str(output_path)]
        if self.options.model:
            cmd += ["-m", self.options.model]
        if self.options.effort:
            cmd += ["-c", f"model_reasoning_effort={self.options.effort}"]
        return cmd + ["-"]

    def stdin(self, prompt: str) -> str:
        return SYSTEM_PROMPT + "\n\n" + prompt  # codex exec has no separate system-prompt flag

    def read_answer(self, stdout: str, output_path: Path) -> tuple[Any, dict[str, Any]]:
        try:
            text = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AgentCallError("invalid_output", "codex wrote no final message") from exc
        try:
            return json.loads(text), {}
        except ValueError as exc:
            raise AgentCallError("invalid_output", f"codex final message is not JSON: {exc}") from exc
