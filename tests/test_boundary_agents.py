"""Agent boundary resolvers with a mocked subprocess; no external service is ever called."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from analysis.boundaries.agents import (PROMPT_VERSION, RESPONSE_SCHEMA, ClaudeBoundaryResolver, CodexBoundaryResolver,
                                        build_prompt)
from analysis.boundaries.models import BoundaryFormatError, BoundaryType, CandidateSource
from analysis.boundaries.resolvers import (ExactBoundaryResolver, ResolverOptions, create_resolver,
                                           parse_agent_response, resolve_contexts)
from analysis.boundaries.shortlist import shortlist
from tests.test_boundaries import game, gameplay, run_analyzer


@pytest.fixture
def contexts():
    result, _ = run_analyzer(gameplay([game(1, 10, 100, "LOSS"), game(2, 140, 200, "WIN")]), 1)
    return result.contexts


@pytest.fixture
def end(contexts):
    return contexts[BoundaryType.END]


@pytest.fixture
def cli(tmp_path):
    executable = tmp_path / "fake-cli.exe"
    executable.write_text("")
    return executable


def options(tmp_path, cli, **kwargs):
    return ResolverOptions(executable=str(cli), cache_directory=tmp_path / "cache", timeout_s=5.0, **kwargs)


class FakePopen:
    """Stands in for subprocess.Popen; behaviour chosen per test."""

    calls: list[dict] = []
    behaviour: dict = {}

    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs, self.returncode, self.pid = cmd, kwargs, 0, 4242
        FakePopen.calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})

    def communicate(self, input=None, timeout=None):
        FakePopen.calls[-1]["input"] = input
        b = FakePopen.behaviour
        if b.get("timeout"):
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        self.returncode = b.get("returncode", 0)
        if "stdout_fn" in b:
            return b["stdout_fn"](input), ""
        if "codex_answer" in b:
            Path(self.cmd[self.cmd.index("-o") + 1]).write_text(b["codex_answer"], encoding="utf-8")
        return b.get("stdout", ""), b.get("stderr", "")

    def kill(self):
        pass


@pytest.fixture(autouse=True)
def fake_process():
    FakePopen.calls, FakePopen.behaviour = [], {}
    with patch("analysis.boundaries.agents.subprocess.Popen", FakePopen), \
         patch("analysis.boundaries.agents._kill_tree") as kill:
        yield kill


def claude_stdout(answer, **envelope):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": answer,
                       "result": json.dumps(answer), "total_cost_usd": 0.01, **envelope})


# ---------- valid decisions ----------

def test_claude_valid_decision_selects_candidate(tmp_path, cli, end):
    pick = shortlist(end)[1]
    FakePopen.behaviour = {"stdout": claude_stdout({"selected_candidate": pick, "confidence": 0.8, "reason": "pausa"})}
    decision = create_resolver("claude", options(tmp_path, cli)).resolve(end)
    assert (decision.candidate_index, decision.timestamp, decision.confidence) == (pick, end.candidates[pick].timestamp, 0.8)
    assert decision.metadata["shown_candidates"] == shortlist(end)
    assert decision.resolver == decision.requested_resolver == "claude" and not decision.fallback
    assert decision.metadata["prompt_version"] == PROMPT_VERSION and decision.metadata["cost_usd"] == 0.01
    cmd = FakePopen.calls[0]["cmd"]
    assert cmd[:4] == [str(cli), "-p", "--output-format", "json"]
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == RESPONSE_SCHEMA
    assert FakePopen.calls[0]["input"] == build_prompt(end)  # prompt via stdin, never via files


def test_codex_valid_decision_selects_candidate(tmp_path, cli, end):
    pick = shortlist(end)[0]
    FakePopen.behaviour = {"codex_answer": json.dumps({"selected_candidate": pick, "confidence": 0.6, "reason": "x"})}
    decision = create_resolver("codex", options(tmp_path, cli, model="m1", effort="low")).resolve(end)
    assert decision.candidate_index == pick and decision.resolver == "codex" and not decision.fallback
    cmd = FakePopen.calls[0]["cmd"]
    assert cmd[1] == "exec" and cmd[cmd.index("--sandbox") + 1] == "read-only" and cmd[-1] == "-"
    assert {"--ephemeral", "--skip-git-repo-check", "--ignore-rules", "--output-schema"} <= set(cmd)
    assert cmd[cmd.index("-m") + 1] == "m1" and "model_reasoning_effort=low" in cmd
    workdir = Path(cmd[cmd.index("-C") + 1])
    assert FakePopen.calls[0]["cwd"] == workdir and workdir.name == "empty"  # no repository as working root
    assert FakePopen.calls[0]["input"].endswith(build_prompt(end))


def test_agents_run_from_an_empty_temporary_directory(tmp_path, cli, end):
    FakePopen.behaviour = {"stdout": claude_stdout({"selected_candidate": 0, "confidence": 0.5, "reason": ""})}
    create_resolver("claude", options(tmp_path, cli)).resolve(end)
    cwd = Path(FakePopen.calls[0]["cwd"])
    assert cwd.name == "empty" and not cwd.exists()  # removed after the call
    assert Path.cwd() not in cwd.parents


# ---------- failures fall back to exact ----------

@pytest.mark.parametrize("behaviour,code", [
    ({"stdout": "not json"}, "invalid_output"),
    ({"stdout": claude_stdout({"selected_candidate": 999, "confidence": 0.5, "reason": ""})}, "invalid_output"),
    ({"stdout": claude_stdout({"selected_candidate": 1, "confidence": 1.7, "reason": ""})}, "invalid_output"),
    ({"stdout": claude_stdout({"selected_candidate": 1, "confidence": 0.5, "reason": "", "timestamp": 3.0})},
     "invalid_output"),
    ({"stdout": claude_stdout(None, structured_output=None, result=None)}, "invalid_output"),
    ({"stdout": json.dumps({"is_error": True, "subtype": "error_max_turns"})}, "process_failed"),
    ({"returncode": 2, "stderr": "auth required\n"}, "process_failed"),
    ({"timeout": True}, "timeout"),
])
def test_claude_failures_fall_back_to_exact(tmp_path, cli, end, behaviour, code, fake_process):
    FakePopen.behaviour = behaviour
    decision = create_resolver("claude", options(tmp_path, cli)).resolve(end)
    exact = ExactBoundaryResolver().resolve(end)
    assert decision.fallback and decision.requested_resolver == "claude" and decision.resolver == "exact"
    assert (decision.candidate_index, decision.timestamp) == (exact.candidate_index, exact.timestamp)
    assert decision.metadata["error_code"] == code and decision.metadata["prompt_version"] == PROMPT_VERSION
    assert fake_process.called == (code == "timeout")  # timeout kills the whole process tree


@pytest.mark.parametrize("answer", ["{bad", '{"selected_candidate": -1, "confidence": 0.5, "reason": ""}',
                                    '{"selected_candidate": 1, "confidence": -0.1, "reason": ""}'])
def test_codex_invalid_answers_fall_back(tmp_path, cli, end, answer):
    FakePopen.behaviour = {"codex_answer": answer}
    decision = create_resolver("codex", options(tmp_path, cli)).resolve(end)
    assert decision.fallback and decision.metadata["error_code"] == "invalid_output"


def test_codex_without_final_message_falls_back(tmp_path, cli, end):
    decision = create_resolver("codex", options(tmp_path, cli)).resolve(end)
    assert decision.fallback and decision.metadata["error_code"] == "invalid_output"


def test_missing_cli_falls_back_without_spawning(tmp_path, end):
    resolver = create_resolver("codex", ResolverOptions(executable=str(tmp_path / "nope"),
                                                        cache_directory=tmp_path / "c"))
    with patch("analysis.boundaries.agents.shutil.which", return_value=None):
        decision = resolver.resolve(end)
    assert decision.fallback and decision.metadata["error_code"] == "cli_missing" and FakePopen.calls == []


def test_missing_cli_is_found_on_path_or_env(tmp_path, cli, monkeypatch):
    monkeypatch.setenv("AUTO_CUTTER_CLAUDE_CLI", str(cli))
    assert ClaudeBoundaryResolver(ResolverOptions()).locate() == str(cli)
    monkeypatch.delenv("AUTO_CUTTER_CLAUDE_CLI")
    with patch("analysis.boundaries.agents.shutil.which", return_value="/bin/claude") as which:
        assert ClaudeBoundaryResolver(ResolverOptions()).locate() == "/bin/claude"
    which.assert_called_with("claude")


def test_without_fallback_failures_raise(tmp_path, cli, end):
    FakePopen.behaviour = {"timeout": True}
    with pytest.raises(RuntimeError, match="timeout"):
        create_resolver("claude", options(tmp_path, cli, fallback=None)).resolve(end)


# ---------- cache ----------

def test_cache_hit_does_not_call_subprocess_and_is_per_provider(tmp_path, cli, end):
    first_pick, second_pick = shortlist(end)[1], shortlist(end)[0]
    FakePopen.behaviour = {"stdout": claude_stdout({"selected_candidate": first_pick, "confidence": 0.8, "reason": ""})}
    first = create_resolver("claude", options(tmp_path, cli)).resolve(end)
    second = create_resolver("claude", options(tmp_path, cli)).resolve(end)
    assert len(FakePopen.calls) == 1 and second.metadata["cache_hit"] and not first.metadata["cache_hit"]
    assert second.candidate_index == first.candidate_index == first_pick
    FakePopen.behaviour = {"codex_answer": json.dumps({"selected_candidate": second_pick, "confidence": 0.5, "reason": ""})}
    assert create_resolver("codex", options(tmp_path, cli)).resolve(end).candidate_index == second_pick  # no claude reuse
    assert len(FakePopen.calls) == 2
    assert sorted(p.name for p in (tmp_path / "cache").iterdir()) == ["claude", "codex"]


def test_cache_key_changes_with_context_model_and_failures_are_not_cached(tmp_path, cli, contexts, end):
    FakePopen.behaviour = {"timeout": True}
    create_resolver("claude", options(tmp_path, cli)).resolve(end)
    FakePopen.behaviour = {"stdout": claude_stdout({"selected_candidate": 0, "confidence": 0.5, "reason": ""})}
    create_resolver("claude", options(tmp_path, cli)).resolve(end)
    create_resolver("claude", options(tmp_path, cli, model="other")).resolve(end)
    create_resolver("claude", options(tmp_path, cli)).resolve(contexts[BoundaryType.START])
    assert len(FakePopen.calls) == 4
    create_resolver("claude", options(tmp_path, cli, use_cache=False)).resolve(end)
    assert len(FakePopen.calls) == 5


# ---------- prompt ----------

def test_start_and_end_prompts_differ(contexts):
    start, end = build_prompt(contexts[BoundaryType.START]), build_prompt(contexts[BoundaryType.END])
    assert "BOUNDARY: START" in start and "BOUNDARY: END" in end
    assert "before the VS screen" in start and "before the VS screen" not in end
    assert "reactions and comments about it must go" in end and "reactions and comments about it must go" not in start
    for label in ("PREVIOUS_CONTENT", "CURRENT_GAME_SPECIFIC", "GENERAL_COMMENTARY", "NEXT_CONTENT", "UNCERTAIN"):
        assert label in start and label in end
    assert "SAME deck" in end  # subtle topic shifts must be reasoned about, not keyword-matched
    assert PROMPT_VERSION in start and PROMPT_VERSION in end


def test_prompt_contains_context_but_no_repository_or_private_metadata(end):
    prompt = build_prompt(end)
    assert "parlato." in prompt and "TRANSCRIPT" in prompt and "SILENCES" in prompt
    assert all(f"#{i} t=" in prompt for i in shortlist(end))
    assert not any(f"#{i} t=" in prompt for i in set(range(len(end.candidates))) - set(shortlist(end)))
    assert "RESULT_ANCHOR" in prompt and "board_disappeared" in prompt
    for forbidden in ("v.mp4", end.game.id, "analysis/", "fingerprint", "feature_names", "settings",
                      str(Path.cwd()), "source_count"):
        assert forbidden not in prompt
    assert len(prompt) < 6000


def test_terminal_partial_prompt_mentions_video_end():
    result, _ = run_analyzer(gameplay([game(1, 10, 100, "LOSS"), game(2, 420, None, "UNKNOWN")]), 2)
    assert "the video ends while it is still being played" in build_prompt(result.contexts[BoundaryType.END])


# ---------- parser, factory, log ----------

def test_strict_parser_rejects_unexpected_keys_and_non_objects(end):
    with pytest.raises(BoundaryFormatError, match="Unexpected"):
        parse_agent_response(end, {"selected_candidate": 0, "confidence": 0.5, "timestamp": 1.0})
    with pytest.raises(BoundaryFormatError):
        parse_agent_response(end, "[1, 2]")
    assert parse_agent_response(end, {"selected_candidate": 0, "confidence": 1, "x": 1}, strict=False).confidence == 1.0


def test_factory_modes(tmp_path):
    assert create_resolver("exact").name == "exact"
    assert isinstance(create_resolver("claude"), ClaudeBoundaryResolver)
    assert isinstance(create_resolver("codex"), CodexBoundaryResolver)
    for mode in ("heuristic", "local"):
        with pytest.raises(ValueError, match="not implemented"):
            create_resolver(mode)
    with pytest.raises(ValueError, match="Unknown"):
        create_resolver("gpt")
    with pytest.raises(ValueError, match="must be local"):
        create_resolver("claude", ResolverOptions(fallback="codex"))


def test_debug_log_is_local_and_excludes_transcript(tmp_path, cli, contexts):
    log = tmp_path / "log" / "decisions.jsonl"
    # index 0 is a valid START candidate but is not shown for END (before the result anchor) -> END falls back.
    FakePopen.behaviour = {"stdout": claude_stdout({"selected_candidate": 0, "confidence": 0.5, "reason": "r"})}
    decisions = resolve_contexts(list(contexts.values()), create_resolver("claude", options(tmp_path, cli, log_path=log)))
    records = {r["boundary_type"]: r for r in map(json.loads, log.read_text(encoding="utf-8").splitlines())}
    assert len(records) == len(decisions) == 2
    assert records["START"]["fallback"] is False and records["START"]["selected_candidate"] == 0
    assert records["END"]["fallback"] is True and records["END"]["error_code"] == "invalid_output"
    assert all(r["prompt_version"] == PROMPT_VERSION for r in records.values())
    assert "parlato" not in log.read_text(encoding="utf-8")


def test_cli_compares_resolvers_on_saved_contexts(tmp_path, cli, contexts, capsys, monkeypatch):
    from analysis.boundaries.__main__ import main

    saved = tmp_path / "contexts.json"
    saved.write_text(json.dumps({"format": "recut_boundary_debug", "version": 1, "games": [
        {"contexts": {k.value: v.to_mapping() for k, v in contexts.items()}}]}))
    monkeypatch.setenv("AUTO_CUTTER_CLAUDE_CLI", str(cli))
    pick = shortlist(contexts[BoundaryType.END])[1]
    FakePopen.behaviour = {"stdout": claude_stdout({"selected_candidate": pick, "confidence": 0.7, "reason": "ok"})}
    out = tmp_path / "decisions.json"
    assert main(["--contexts", str(saved), "--select", "1:end", "--resolver", "exact", "--resolver", "claude",
                 "--decisions-output", str(out), "--no-agent-cache"]) == 0
    text = capsys.readouterr().out
    assert "sends the boundary context text" in text and "game 1 END" in text and "game 1 START" not in text
    rows = json.loads(out.read_text())["decisions"]
    assert [(r["requested_resolver"], r["candidate_index"]) for r in rows] == [
        ("exact", contexts[BoundaryType.END].candidate_index([CandidateSource.RESULT_ANCHOR])), ("claude", pick)]


# ---------- prompt v2: shortlist, labels, neighbours ----------

def test_shortlist_hides_candidates_that_would_leave_gameplay(contexts):
    start, end = contexts[BoundaryType.START], contexts[BoundaryType.END]
    assert all(end.candidates[i].timestamp >= 100.0 - 0.3 for i in shortlist(end))
    assert all(start.candidates[i].timestamp <= 10.0 + 0.3 for i in shortlist(start))
    assert end.candidate_index([CandidateSource.RESULT_ANCHOR]) in shortlist(end)


def test_shortlist_cap_drops_only_weak_candidates(end):
    from dataclasses import replace as dc_replace
    from analysis.boundaries.models import CandidateBoundary

    weak = [CandidateBoundary(101.0 + i * 0.5, BoundaryType.END, (CandidateSource.SILENCE_MID,)) for i in range(30)]
    crowded = dc_replace(end, candidates=end.candidates + weak)
    kept = shortlist(crowded, max_candidates=10)
    strong = [i for i in shortlist(crowded, max_candidates=1000)
              if set(crowded.candidates[i].sources) - {CandidateSource.SILENCE_MID}]
    assert set(strong) <= set(kept)  # anchors/edges are never dropped
    assert len(kept) == 10


def test_segment_labels_are_validated_and_kept(end):
    allowed = shortlist(end)
    decision = parse_agent_response(end, {"selected_candidate": allowed[0], "confidence": 0.5, "reason": "",
                                          "segment_labels": ["CURRENT_GAME_SPECIFIC"]}, allowed_indices=allowed)
    assert decision.metadata["segment_labels"] == ["CURRENT_GAME_SPECIFIC"]
    with pytest.raises(BoundaryFormatError):
        parse_agent_response(end, {"selected_candidate": allowed[0], "confidence": 0.5, "segment_labels": ["LOSS"]})
    hidden = next(i for i in range(len(end.candidates)) if i not in allowed)
    with pytest.raises(BoundaryFormatError, match="not among"):
        parse_agent_response(end, {"selected_candidate": hidden, "confidence": 0.5}, allowed_indices=allowed)


def test_prompt_v2_mentions_neighbours_and_schema_requires_labels(contexts):
    prompt = build_prompt(contexts[BoundaryType.END])
    assert "next game: game 2 result WIN" in prompt and "KEPT in the video" in prompt
    assert PROMPT_VERSION == "boundary_agent_prompt_v3"
    assert RESPONSE_SCHEMA["required"] == ["segment_labels", "selected_candidate", "confidence", "reason"]


def test_keep_mode_prompt_reasons_about_ownership_of_the_kept_game():
    result, _ = run_analyzer(gameplay([game(1, 10, 100, "WIN"), game(2, 140, 200, "LOSS")]), 1, require_removable=False)
    start, end = build_prompt(result.contexts[BoundaryType.START]), build_prompt(result.contexts[BoundaryType.END])
    assert "which is KEPT in the final video" in start and "which is KEPT in the final video" in end
    assert "everything before START is removed" in start and "everything after END is removed" in end
    assert "Never include PREVIOUS_CONTENT" in start and "Keep the reaction to the win" in end
    assert "chatter not owned by a kept game is removed" in start
    assert "Step 1: label every transcript segment" in start and "Step 2: choose the candidate" in end
    assert "MODE balanced" in end and "Sentence completeness never outweighs relevance" in end  # END policy v4
    assert "next game: game 2 result LOSS" in end and "also REMOVED" in end
