"""Reasoning-agent adapters for packaging: the user's local `claude` or `codex` CLI.

Built on the boundary resolvers' machinery (CLI lookup, AUTO_CUTTER_*_CLI env
vars, run_agent_process with timeout and process-tree kill, AgentCallError).
Each call runs non-interactively in a fresh temporary workspace whose working
directory is EMPTY; only copies of the images selected for this task are placed
next to it. Text goes through stdin, never through files the agent could browse.

* claude: `-p --input-format stream-json --output-format stream-json --verbose`
  with base64 image content blocks, `--json-schema`, `--tools ""` (no tools),
  `--strict-mcp-config` (no MCP servers), `--no-session-persistence`.
* codex: `exec --sandbox read-only --ephemeral --skip-git-repo-check
  --ignore-rules --output-schema FILE -o FILE --image=FILE ... -- -` with the
  empty directory as working root.

PRIVACY: the prompt (transcript excerpts, timings, evidence) and the attached
images are sent to the provider configured in that CLI. Nothing is sent by the
local stages. Logs never contain prompts, images or credentials.

Answers are validated by the caller; one repair attempt is made with the
validation error. Valid answers are cached by provider, model, effort, task,
prompt version and the exact prompt/schema/image bytes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from analysis.boundaries.agents import AgentCallError, ClaudeBoundaryResolver, CodexBoundaryResolver, run_agent_process
from analysis.boundaries.resolvers import ResolverOptions

from .cache import PackagingCache, stable_key

PROVIDERS = ("claude", "codex")
PRIVACY_NOTE = ("NOTE: packaging sends transcript excerpts, timing evidence and selected video frames/crops to the "
                "provider configured in the {provider} CLI. Local analysis sends nothing. Use --analyze-only to stay "
                "fully local.")
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


@dataclass(frozen=True)
class AgentSettings:
    provider: str = "codex"
    model: str | None = None
    effort: str | None = None
    timeout_s: float = 900.0
    executable: str | None = None
    use_cache: bool = True
    max_attempts: int = 2  # 1 call + 1 repair attempt on invalid output
    max_images: int = 16
    log_path: str | None = None
    max_prompt_chars: int | None = None  # input budget: refuse to send a larger prompt
    max_budget_usd: float | None = None  # claude --max-budget-usd (codex has no cost cap; tokens are logged)


@dataclass
class AgentResult:
    answer: Any
    meta: dict[str, Any] = field(default_factory=dict)


Validator = Callable[[Any], Any]


def codex_tokens(stderr: str) -> int | None:
    """`codex exec` prints "tokens used" followed by the total on stderr; None when absent."""
    lines = [line.strip() for line in (stderr or "").splitlines()]
    for i, line in enumerate(lines):
        if line.lower().startswith("tokens used"):
            tail = line[len("tokens used"):].strip(" :") or (lines[i + 1] if i + 1 < len(lines) else "")
            digits = tail.replace(",", "").replace(".", "").strip()
            if digits.isdigit():
                return int(digits)
    return None


class PackagingAgent:
    def __init__(self, settings: AgentSettings, cache: PackagingCache) -> None:
        if settings.provider not in PROVIDERS:
            raise ValueError(f"provider must be one of {PROVIDERS}")
        self.settings = settings
        self.cache = cache
        options = ResolverOptions(executable=settings.executable, model=settings.model, effort=settings.effort)
        locator = ClaudeBoundaryResolver if settings.provider == "claude" else CodexBoundaryResolver
        self._locator = locator(options)
        self.calls: list[dict[str, Any]] = []

    # ---- availability ----------------------------------------------------------------------------
    def locate(self) -> str | None:
        return self._locator.locate()

    def require(self) -> str:
        executable = self.locate()
        if executable is None:
            raise AgentCallError("cli_missing", f"'{self.settings.provider}' CLI not found: install it and log in, or "
                                 f"set {self._locator.env_var} to its path")
        return executable

    # ---- commands --------------------------------------------------------------------------------
    def command(self, executable: str, workdir: Path, schema: dict[str, Any], schema_path: Path, output_path: Path,
                system_prompt: str, images: list[Path]) -> list[str]:
        s = self.settings
        if s.provider == "claude":
            cmd = [executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                   "--json-schema", json.dumps(schema), "--tools", "", "--strict-mcp-config",
                   "--no-session-persistence", "--system-prompt", system_prompt]
            if s.model:
                cmd += ["--model", s.model]
            if s.effort:
                cmd += ["--effort", s.effort]
            if s.max_budget_usd:
                cmd += ["--max-budget-usd", f"{s.max_budget_usd:.2f}"]
            return cmd
        cmd = [executable, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--ignore-rules",
               "--color", "never", "-C", str(workdir), "--output-schema", str(schema_path), "-o", str(output_path)]
        if s.model:
            cmd += ["-m", s.model]
        if s.effort:
            cmd += ["-c", f"model_reasoning_effort={s.effort}"]
        cmd += [f"--image={image}" for image in images]
        return cmd + ["--", "-"]

    def stdin(self, prompt: str, system_prompt: str, images: list[Path]) -> str:
        if self.settings.provider == "codex":
            return system_prompt + "\n\n" + prompt  # codex exec has no separate system-prompt flag
        content: list[dict[str, Any]] = []
        for image in images:
            content.append({"type": "image", "source": {"type": "base64", "media_type": MEDIA_TYPES[image.suffix.lower()],
                                                        "data": base64.b64encode(image.read_bytes()).decode("ascii")}})
        content.append({"type": "text", "text": prompt})
        return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"

    def read_answer(self, stdout: str, output_path: Path) -> tuple[Any, dict[str, Any]]:
        if self.settings.provider == "codex":
            try:
                text = output_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise AgentCallError("invalid_output", "codex wrote no final message") from exc
            try:
                return json.loads(text), {}
            except ValueError as exc:
                raise AgentCallError("invalid_output", f"codex final message is not JSON: {exc}") from exc
        result = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                result = event
        if result is None:
            raise AgentCallError("invalid_output", "claude produced no result event")
        if result.get("is_error") or result.get("subtype") not in (None, "success"):
            raise AgentCallError("process_failed", str(result.get("subtype") or result.get("result", ""))[:200])
        usage = result.get("usage") or {}
        extra = {"cost_usd": result.get("total_cost_usd"), "agent_duration_ms": result.get("duration_ms"),
                 "agent_model": next(iter(result.get("modelUsage", {}) or {}), None),
                 "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                 "cache_read_tokens": usage.get("cache_read_input_tokens")}
        answer = result.get("structured_output")
        if answer is None and isinstance(result.get("result"), str):
            try:
                answer = json.loads(result["result"])
            except ValueError:
                answer = None
        if answer is None:
            raise AgentCallError("invalid_output", "claude returned no structured output")
        return answer, extra

    # ---- calls -----------------------------------------------------------------------------------
    def cache_key(self, task: str, prompt_version: str, prompt: str, schema: dict[str, Any], system_prompt: str,
                  images: list[Path]) -> str:
        digests = [hashlib.sha256(Path(image).read_bytes()).hexdigest() for image in images]
        s = self.settings
        return stable_key("agent", task, prompt_version, s.provider, s.model, s.effort,
                          hashlib.sha256((system_prompt + "\x00" + prompt).encode("utf-8")).hexdigest(),
                          hashlib.sha256(json.dumps(schema, sort_keys=True).encode("utf-8")).hexdigest(), digests)

    def run(self, task: str, *, prompt: str, schema: dict[str, Any], system_prompt: str, prompt_version: str,
            images: list[Path] | None = None, validate: Validator | None = None,
            repair: Callable[[Any, str], str] | None = None) -> AgentResult:
        """One validated answer. `repair(previous_answer, errors)` builds a MINIMAL correction prompt (sent
        without images); without it the original prompt is re-sent with the errors appended."""
        images = list(images or [])[: self.settings.max_images]
        for image in images:
            if image.suffix.lower() not in MEDIA_TYPES:
                raise ValueError(f"unsupported image type {image.suffix}")
        if self.settings.max_prompt_chars and len(prompt) > self.settings.max_prompt_chars:
            raise AgentCallError("budget_exceeded", f"prompt has {len(prompt)} characters, budget "
                                 f"{self.settings.max_prompt_chars}")
        key = self.cache_key(task, prompt_version, prompt, schema, system_prompt, images)
        meta: dict[str, Any] = {"task": task, "provider": self.settings.provider, "model": self.settings.model,
                                "effort": self.settings.effort, "prompt_version": prompt_version, "images": len(images),
                                "image_bytes": sum(Path(i).stat().st_size for i in images),
                                "prompt_chars": len(prompt), "cache_hit": False, "attempts": 0}
        started = time.perf_counter()
        if self.settings.use_cache:
            cached = self.cache.load_json("agent", key)
            if isinstance(cached, dict) and "answer" in cached:
                answer = validate(cached["answer"]) if validate else cached["answer"]
                meta.update(cached.get("call", {}), cache_hit=True, runtime_s=round(time.perf_counter() - started, 3))
                self._record(meta)
                return AgentResult(answer, meta)
        executable = self.require()
        call_prompt, call_images = prompt, images
        last_error: AgentCallError | None = None
        totals = {"cost_usd": 0.0, "tokens_used": 0, "input_tokens": 0, "output_tokens": 0}
        for attempt in range(1, max(1, self.settings.max_attempts) + 1):
            meta["attempts"] = attempt
            raw: Any = None
            try:
                raw, extra = self._call(executable, call_prompt, schema, system_prompt, call_images)
                for k, v in extra.items():
                    if k in totals and isinstance(v, (int, float)):
                        totals[k] += v
                    elif v is not None:
                        meta[k] = v
                answer = validate(raw) if validate else raw
            except (AgentCallError, ValueError) as error:
                last_error = error if isinstance(error, AgentCallError) else AgentCallError("invalid_output", str(error))
                if last_error.code != "invalid_output":
                    break
                detail = str(error) if isinstance(error, ValueError) else error.detail
                if repair is not None and raw is not None:
                    call_prompt, call_images = repair(raw, detail), []  # minimal correction: no images, no transcript
                else:
                    call_prompt = (prompt + "\n\nYOUR PREVIOUS ANSWER WAS REJECTED BY VALIDATION: " + detail[:600] +
                                   "\nReturn a corrected JSON object that satisfies every rule.")
                meta.setdefault("repair_prompt_chars", []).append(len(call_prompt))
                continue
            meta.update({k: (round(v, 6) if isinstance(v, float) else v) for k, v in totals.items() if v})
            meta["runtime_s"] = round(time.perf_counter() - started, 3)
            if self.settings.use_cache:
                self.cache.store_json("agent", key, {"answer": raw, "call": {k: v for k, v in meta.items()
                                                                              if k not in ("cache_hit", "runtime_s")}})
            self._record(meta)
            return AgentResult(answer, meta)
        meta.update({k: v for k, v in totals.items() if v})
        meta.update(runtime_s=round(time.perf_counter() - started, 3), error_code=last_error.code if last_error else "",
                    error_detail=last_error.detail if last_error else "")
        self._record(meta)
        assert last_error is not None
        raise last_error

    def _call(self, executable: str, prompt: str, schema: dict[str, Any], system_prompt: str,
              images: list[Path]) -> tuple[Any, dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix=f"recut-pack-{self.settings.provider}-") as folder:
            root = Path(folder)
            workdir = root / "empty"
            workdir.mkdir()
            inputs = root / "inputs"
            inputs.mkdir()
            copies = []
            for n, image in enumerate(images):  # only the artifacts needed by this task, by copy
                copy = inputs / f"image_{n:02d}{image.suffix.lower()}"
                shutil.copyfile(image, copy)
                copies.append(copy)
            schema_path, output_path = root / "schema.json", root / "answer.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            cmd = self.command(executable, workdir, schema, schema_path, output_path, system_prompt, copies)
            stdout, stderr = run_agent_process(cmd, self.stdin(prompt, system_prompt, copies), workdir,
                                               self.settings.timeout_s)
            answer, extra = self.read_answer(stdout, output_path)
            if self.settings.provider == "codex":
                extra["tokens_used"] = codex_tokens(stderr)
            return answer, extra

    def _record(self, meta: dict[str, Any]) -> None:
        self.calls.append(dict(meta))
        if not self.settings.log_path:
            return
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **{k: meta.get(k) for k in (
            "task", "provider", "model", "effort", "prompt_version", "images", "prompt_chars", "cache_hit", "attempts",
            "runtime_s", "cost_usd", "input_tokens", "output_tokens", "tokens_used", "image_bytes",
            "repair_prompt_chars", "error_code")}}
        path = Path(self.settings.log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
