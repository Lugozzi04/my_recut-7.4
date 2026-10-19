from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import math
import os
import re
import signal
import sys
import time
import webbrowser
from collections.abc import Callable, Iterator, Sequence
from typing import Any, NoReturn, TextIO


EXIT_SUCCESS = 0
EXIT_ARGUMENTS = 2
EXIT_DOWNLOAD = 10
EXIT_ANALYSIS = 20
EXIT_EXPORT = 30
EXIT_UPLOAD = 40
EXIT_AUTH = 50
EXIT_FILESYSTEM = 60
EXIT_CANCELLED = 70
EXIT_BUSY = 75


def parse_time(value: str) -> float:
    """Seconds, MM:SS or HH:MM:SS become finite, nonnegative seconds."""
    try:
        parts = str(value).strip().split(":")
        if len(parts) not in {1, 2, 3} or any(not part for part in parts):
            raise ValueError
        numbers = [float(part) for part in parts]
        if any(not math.isfinite(number) or number < 0 for number in numbers):
            raise ValueError
        if any(not number.is_integer() for number in numbers[:-1]):
            raise ValueError
        if len(numbers) > 1 and numbers[-1] >= 60:
            raise ValueError
        if len(numbers) == 3 and numbers[1] >= 60:
            raise ValueError
        seconds = sum(number * 60 ** index for index, number in enumerate(reversed(numbers)))
        if not math.isfinite(seconds):
            raise ValueError
        return seconds
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("Time must be seconds, MM:SS or HH:MM:SS (for example 90, 01:30, 01:02:30).") from exc


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


class _ArgumentError(ValueError):
    pass


def _youtube_video_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise argparse.ArgumentTypeError("YouTube video ID must contain exactly 11 letters, digits, underscores or hyphens.")
    return value


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="Write one machine-readable JSON result to stdout; progress stays on stderr.")


def _processing_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", type=parse_time, help="Range start: seconds, MM:SS or HH:MM:SS.")
    boundaries = parser.add_mutually_exclusive_group()
    boundaries.add_argument("--end", type=parse_time, help="Range end (default: source end).")
    boundaries.add_argument("--duration", type=parse_time, help="Length measured from --start; alternative to --end.")
    parser.add_argument("--preset", help="A real GUI preset name; default: preferred catalog default.")
    destinations = parser.add_mutually_exclusive_group()
    destinations.add_argument("--output", help="Output file; existing files get a numeric suffix unless --force.")
    destinations.add_argument("--output-dir", help="Output directory (default: user Videos/Auto Cutter/Exports).")
    parser.add_argument("--quality", choices=("maximum", "very_high", "high", "balanced", "compact"), help="Override saved GUI export quality.")
    parser.add_argument("--youtube", action="store_true", help="Upload the validated export to YouTube PRIVATE (authenticate first).")
    parser.add_argument("--title", help="YouTube title (default: source title).")
    parser.add_argument("--description", default="", help="YouTube description.")
    parser.add_argument("--thumbnail", help="Optional thumbnail image for the private YouTube video.")
    parser.add_argument("--keep-source", action="store_true", help="Keep the downloaded VOD after DONE; original local files are always retained.")
    parser.add_argument("--force", action="store_true", help="Create a fresh job and allow replacing the explicit output; sources are protected.")
    parser.add_argument("--dry-run", action="store_true", help="Show metadata, range, preset and destination without processing or creating a job.")
    gameplay = parser.add_argument_group("smart gameplay editing (optional)")
    gameplay.add_argument("--gameplay", choices=("hearthstone",), help="Detect games and remove unwanted ones before the classic cuts.")
    gameplay.add_argument("--keep-games", choices=("wins",), help="Games to publish (default with --gameplay: wins = complete WINs only).")
    gameplay.add_argument("--boundary-mode", choices=("exact", "claude", "codex"),
                          help="Editorial boundary resolver (default exact). claude/codex send the local transcript context "
                               "around game boundaries to the provider configured in that CLI; exact sends nothing.")
    gameplay.add_argument("--agent-timeout", type=float, help="Seconds per agent decision (default 120); failures fall back to exact.")
    gameplay.add_argument("--agent-model", help="Model passed to the claude/codex CLI.")
    gameplay.add_argument("--agent-effort", help="claude --effort / codex model_reasoning_effort.")
    gameplay.add_argument("--end-policy", choices=("tight", "balanced", "extended"),
                          help="END of kept games with claude/codex (default balanced): tight = reaction + essential "
                               "conclusion; balanced = reaction + short game-specific recap; extended = longer "
                               "game-specific explanation. Chat, unrelated talk and next-game transitions are cut.")
    gameplay.add_argument("--report", help="Write the gameplay edit report JSON here (default: next to the job project).")
    packaging = parser.add_argument_group("YouTube packaging after the export (optional)")
    packaging.add_argument("--packaging", action="store_true",
                           help="After a successful export, generate titles and thumbnails from the job's project "
                                "(same as `packaging --project`). Sends transcript excerpts and frames to the provider.")
    packaging.add_argument("--packaging-provider", choices=("codex", "claude"),
                           help="Agent for titles/concepts (default codex).")
    packaging.add_argument("--packaging-language", help="Language of titles and thumbnail text (default it).")
    packaging.add_argument("--thumbnail-engine", choices=("gen", "v3", "v2"),
                           help="gen (default): image model composes the thumbnails; v3/v2: local render only.")
    packaging.add_argument("--packaging-output", help="Packaging folder (default: <export>_packaging next to the video).")
    packaging.add_argument("--no-interactive", action="store_true",
                           help="With --packaging: do not open the thumbnails nor ask for edits at the end.")
    parser.add_argument("--analyze-only", action="store_true",
                        help="Run analysis and cut composition, write the project/report, but do not render; resume to export later.")
    _json_option(parser)


COMMANDS = {"concat", "groups", "presets", "jobs", "process", "vod", "resume", "auth", "packaging", "packaging-style", "packaging-feedback",
            "packaging-edit", "clips", "short"}


VIDEO_SUFFIXES = (".mp4", ".mkv", ".mov", ".flv", ".ts", ".webm", ".m4v", ".avi")


def _implicit_command(arguments: list[str]) -> list[str]:
    """`AutoCutter VIDEO ...` means `process VIDEO ...`; a Twitch VOD URL means `vod URL ...`; a Twitch clip
    URL means `short URL ...`.

    Only an existing file, a video file name or a Twitch URL is promoted, so
    unknown words keep producing the usual argument error.
    """
    if not arguments or arguments[0].startswith("-") or arguments[0] in COMMANDS:
        return arguments
    first = arguments[0]
    if first.lower().startswith(("https://www.twitch.tv/videos/", "https://twitch.tv/videos/")):
        return ["vod", *arguments]
    from analysis.clips.short import is_clip_url
    if is_clip_url(first):
        return ["short", *arguments]
    if os.path.isfile(first) or first.lower().endswith(VIDEO_SUFFIXES):
        return ["process", *arguments]
    return arguments


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="AutoCutter", description="Run Auto Cutter through the same pipeline used by its GUI.")
    _json_option(parser)
    parser.add_argument("--version", action="store_true", help="Show application version.")
    sub = parser.add_subparsers(dest="command")
    concat = sub.add_parser("concat", help="Join two videos in order; copy compatible inputs and normalize others.")
    concat.add_argument("first", help="First local video.")
    concat.add_argument("second", help="Second local video.")
    concat.add_argument("-o", "--output", required=True, help="New .mp4/.mov/.mkv output; existing files are never replaced.")
    _json_option(concat)
    presets = sub.add_parser("presets", help="List the actual GUI presets.")
    _json_option(presets)
    jobs = sub.add_parser("jobs", help="List persisted jobs, or inspect one.")
    jobs.add_argument("--job", help="Inspect one job by ID.")
    _json_option(jobs)
    local = sub.add_parser("process", help="Analyze, cut, export and validate a local video.")
    local.add_argument("source", help="Local video path.")
    _processing_options(local)
    vod = sub.add_parser("vod", help="Download and process a selected Twitch VOD interval.")
    vod.add_argument("url", help="https://www.twitch.tv/videos/VIDEO_ID")
    _processing_options(vod)
    resume = sub.add_parser("resume", help="Resume or retry an existing job using its saved preset snapshot.")
    resume.add_argument("job_id")
    resume_mode = resume.add_mutually_exclusive_group()
    resume_mode.add_argument("--dry-run", action="store_true", help="Inspect the saved job without executing it.")
    resume_mode.add_argument("--youtube-video-id", type=_youtube_video_id,
                             help="Record the matching PRIVATE video ID manually verified in YouTube Studio before resuming an unknown upload outcome.")
    _json_option(resume)
    auth = sub.add_parser("auth", help="Authenticate Twitch or YouTube, inspect status, or log out.")
    _json_option(auth)
    providers = auth.add_subparsers(dest="provider", required=True)
    for provider in ("twitch", "youtube"):
        provider_parser = providers.add_parser(provider)
        operation = provider_parser.add_mutually_exclusive_group()
        operation.add_argument("--logout", action="store_true", help="Delete locally stored credentials and invalidate pending authorization.")
        operation.add_argument("--status", action="store_true", help="Validate existing credentials without starting login.")
        if provider == "twitch":
            provider_parser.add_argument("--client-id", help="Twitch app client ID (or environment/saved GUI client ID).")
        else:
            provider_parser.add_argument("--client-config", help="Google desktop OAuth client JSON (or AUTO_CUTTER_YOUTUBE_CLIENT_CONFIG).")
        _json_option(provider_parser)
    from automation.packaging_cli import add_parsers as add_packaging_parsers  # argparse only; no engine import
    add_packaging_parsers(sub, _json_option)
    from automation.hero_groups_cli import add_parser as add_groups_parser
    add_groups_parser(sub, _json_option)
    from automation.clips_cli import add_parser as add_clips_parser
    add_clips_parser(sub, _json_option)
    from automation.short_cli import add_parser as add_short_parser
    add_short_parser(sub, _json_option)
    return parser


class ProgressPrinter:
    _names = {"download": "Download", "analysis": "Analysis", "cuts": "Cuts",
              "export": "Export", "validation": "Validation", "youtube": "YouTube PRIVATE",
              "warning": "Warning", "done": "DONE"}

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self._last: dict[str, int] = {}

    def __call__(self, event: dict[str, Any]) -> None:
        from utils.redaction import redact_secrets
        stage = str(event.get("stage", "pipeline"))
        name = self._names.get(stage, stage)
        if "cuts_count" in event:
            print(f"[{name}] {event['cuts_count']} cuts; {float(event.get('removed_s', 0)) / 60:.1f} minutes removed", file=self.stream)
        elif "progress" in event:
            progress = int(event["progress"])
            previous = self._last.get(stage, -10)
            if progress not in {0, 100} and progress - previous < 5:
                return
            if previous == progress:
                return
            self._last[stage] = progress
            print(f"[{name}] {progress}%", file=self.stream)
        elif event.get("message"):
            message = redact_secrets(str(event["message"]))
            if stage == "export":
                if message.startswith("automation_export_config effective="):
                    try:
                        settings = json.loads(message.split("effective=", 1)[1])
                        message = f"codec: {settings['codec']}; method: {settings['method']}"
                    except (ValueError, KeyError):
                        return
                elif message.startswith("export_summary "):
                    method = re.search(r"\bmethod=(\S+)", message)
                    codec = re.search(r"\bcodec=(\S+)", message)
                    message = f"{method.group(1) if method else 'complete'}; {codec.group(1) if codec else ''}"
                elif not message.startswith("automation_export_codec_fallback"):
                    return
            print(f"[{name}] {message}", file=self.stream)
        self.stream.flush()


@contextlib.contextmanager
def _ctrl_c(cancellation: Any, *, on_cancel: Callable[[], None] | None = None) -> Iterator[None]:
    def interrupt(_signum: int, _frame: Any) -> None:
        cancellation.cancel()
        if on_cancel is not None:
            on_cancel()
    try:
        previous = signal.signal(signal.SIGINT, interrupt)
    except ValueError:  # Library invocation from a non-main thread.
        yield
        return
    try:
        yield
    except Exception as exc:
        if cancellation.cancelled:
            from automation.runtime import PipelineRuntimeError
            raise PipelineRuntimeError("cancelled", "Operation cancelled.", EXIT_CANCELLED) from exc
        raise
    finally:
        signal.signal(signal.SIGINT, previous)


def _human_plan(plan: dict[str, Any], stream: TextIO) -> None:
    media = plan["media"]
    selected = plan["range"]
    print(f"Source: {plan['source']}\nTitle: {media['title']}\nDuration: {media['duration_s']:.3f}s", file=stream)
    print(f"Requested range: {json.dumps(plan['requested_range'])}", file=stream)
    print(f"Actual range: {selected['start_s']:.3f}s to {selected['end_s']:.3f}s\nPreset: {plan['preset']['name']}", file=stream)
    print(f"Output: {plan['delivery']['output_path']}\nYouTube: {'PRIVATE' if plan['delivery']['youtube'] else 'disabled'}", file=stream)
    if plan.get("gameplay"):
        g = plan["gameplay"]
        print(f"Gameplay: {g['game']} keep={g['keep_games']} boundaries={g['boundary_mode']}", file=stream)
    for warning in plan.get("warnings", []):
        print(f"Warning: {warning}", file=stream)


def _auth_twitch(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    from automation.runtime import PipelineCancellation, PipelineRuntimeError
    from automation.twitch import TwitchApiClient, TwitchAuthorizationPending
    from automation.twitch_auth import TwitchSession, TwitchTokenStore
    from core.config import get_setting, set_setting
    if args.logout:
        # Logout does not need a configured application ID or a network request.
        TwitchTokenStore().delete()
        return {"authenticated": False, "logged_out": True}
    client_id = str(args.client_id or os.environ.get("AUTO_CUTTER_TWITCH_CLIENT_ID") or get_setting("automation/twitch/client_id", "")).strip()
    if not client_id:
        raise PipelineRuntimeError("twitch_client_id_missing", "Set --client-id, AUTO_CUTTER_TWITCH_CLIENT_ID, or the client ID in the GUI.", EXIT_AUTH)
    session = TwitchSession(TwitchApiClient(client_id))
    if args.status:
        _token, identity = session.validated_context()
        return {"authenticated": True, "login": identity.login, "user_id": identity.user_id}
    cancellation = PipelineCancellation()
    with _ctrl_c(cancellation, on_cancel=session.cancel_authorization):
        authorization = session.begin_device_authorization()
        print(f"Open: {authorization.verification_uri}\nEnter this temporary authorization code: {authorization.user_code}", file=stream, flush=True)
        webbrowser.open(authorization.verification_uri)
        deadline = time.monotonic() + authorization.expires_in
        while True:
            cancellation.raise_if_cancelled()
            if time.monotonic() >= deadline:
                session.cancel_authorization()
                raise PipelineRuntimeError("authorization_expired", "Twitch authorization expired. Run auth twitch again.", EXIT_AUTH)
            try:
                session.complete_device_authorization(authorization)
                break
            except TwitchAuthorizationPending:
                # Event waits allow Ctrl+C to interrupt polling promptly.
                remaining = max(1, authorization.interval)
                for _tick in range(remaining * 10):
                    cancellation.raise_if_cancelled()
                    time.sleep(.1)
        cancellation.raise_if_cancelled()
        _token, identity = session.validated_context()
        set_setting("automation/twitch/client_id", client_id)
        return {"authenticated": True, "login": identity.login, "user_id": identity.user_id}


def _auth_youtube(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    from automation.runtime import PipelineCancellation, PipelineRuntimeError
    try:
        module = importlib.import_module("integrations.youtube.auth")
    except ImportError as exc:
        raise PipelineRuntimeError("youtube_dependency_missing", "Install the YouTube OAuth runtime dependencies.", EXIT_AUTH) from exc
    session = module.YouTubeSession(client_config_path=args.client_config)
    if args.logout:
        session.disconnect()
        return {"authenticated": False, "logged_out": True}
    if args.status:
        session.validated_credentials()
        return {"authenticated": True}
    cancellation = PipelineCancellation()
    with _ctrl_c(cancellation, on_cancel=session.cancel_authorization):
        session.connect(cancellation=cancellation,
                        on_authorization_url=lambda _url: print("Complete Google OAuth in the opened browser.", file=stream, flush=True))
        cancellation.raise_if_cancelled()
    return {"authenticated": True}


def run_cli(argv: Sequence[str] | None = None, *, runtime_factory: Callable[..., Any] | None = None,
            stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Parse first, import only the requested core, and emit one stable result."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    for standard in (sys.stdout, sys.stderr):
        reconfigure = getattr(standard, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    output = stdout or sys.stdout
    progress = stderr or sys.stderr
    arguments = _implicit_command(arguments)
    json_mode = "--json" in arguments
    command = ""
    parser = build_parser()
    captured_help = io.StringIO() if json_mode and any(arg in {"--help", "-h"} for arg in arguments) else None
    try:
        # argparse help/version must never import the analysis engines or GUI.
        with contextlib.redirect_stdout(captured_help or output), contextlib.redirect_stderr(progress):
            args = parser.parse_args(arguments)
        json_mode = getattr(args, "json", False)
        command = args.command or ""
        if args.version:
            from utils.app_version import app_version
            result: dict[str, Any] = {"version": app_version()}
        elif not command:
            raise _ArgumentError("Choose a command; use --help to list commands.")
        else:
            # Existing third-party/worker debug output belongs on stderr even in JSON mode.
            with contextlib.redirect_stdout(progress):
                result = _execute(args, progress, runtime_factory)
        payload = {"ok": True, "command": command or "version", **result}
        if json_mode:
            print(json.dumps(payload, ensure_ascii=True, allow_nan=False), file=output)
        else:
            _human_result(command, result, output)
        return EXIT_SUCCESS
    except SystemExit as exc:
        if captured_help is not None and not exc.code:
            print(json.dumps({"ok": True, "help": captured_help.getvalue()}, ensure_ascii=True), file=output)
        return int(exc.code or 0)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt,)) or str(getattr(exc, "code", "")).endswith("cancelled"):
            code, error_code, message = EXIT_CANCELLED, "cancelled", "Operation cancelled."
        elif isinstance(exc, _ArgumentError):
            code, error_code, message = EXIT_ARGUMENTS, "invalid_arguments", str(exc)
        elif isinstance(exc, Exception):
            code, error_code, message = _error_info(exc, command)
        else:
            raise
        from utils.redaction import redact_secrets
        clean = redact_secrets(message)
        if json_mode:
            print(json.dumps({"ok": False, "command": command, "error": {"code": error_code, "message": clean}, "exit_code": code}, ensure_ascii=True), file=output)
        print(f"Error [{error_code}]: {clean}", file=progress)
        return code


def _execute(args: argparse.Namespace, progress: TextIO, runtime_factory: Callable[..., Any] | None) -> dict[str, Any]:
    command = args.command
    if command == "concat":
        from export.concat import concat_videos
        joined = concat_videos(args.first, args.second, args.output,
                               on_status=lambda message: print(message, file=progress, flush=True))
        return {"output": str(joined.path), "duration_s": joined.duration_s,
                "video_mode": "reencoded" if joined.video_reencoded else "copy",
                "audio_mode": "reencoded" if joined.audio_reencoded else "copy"}
    if command == "groups":
        from automation.hero_groups_cli import execute as execute_groups
        return execute_groups(args, progress)
    if command == "clips":
        from automation.clips_cli import execute as execute_clips
        return execute_clips(args, progress)
    if command == "short":
        from automation.short_cli import execute as execute_short
        return execute_short(args, progress)
    if command == "presets":
        from core.presets import PresetRepository
        return {"presets": PresetRepository().load()}
    if command == "jobs":
        from automation.manager import PipelineManager
        manager = PipelineManager()
        if args.job:
            return {"job": manager.get(args.job).to_mapping()}
        return {"jobs": [job.to_mapping() for job in manager.list_jobs()]}
    if command == "auth":
        return {"provider": args.provider, **(_auth_twitch(args, progress) if args.provider == "twitch" else _auth_youtube(args, progress))}
    if command in ("packaging", "packaging-style", "packaging-feedback", "packaging-edit"):
        from automation.packaging_cli import execute as execute_packaging
        return execute_packaging(args, progress)
    from automation.runtime import PipelineCancellation, PipelineRuntime
    from utils.ffmpeg import cancellable_probe_scope
    factory = runtime_factory or PipelineRuntime
    runtime = factory(on_event=ProgressPrinter(progress))
    if command == "resume":
        job = runtime.manager.get(args.job_id)
        if args.dry_run:
            return {"dry_run": True, "job": job.to_mapping()}
        cancellation = PipelineCancellation()
        with _ctrl_c(cancellation):
            with runtime.manager.execution_lock(job.id):
                if args.youtube_video_id:
                    runtime.manager.record_youtube_video_id(job.id, args.youtube_video_id)
                completed = runtime.run(job.id, resume=True, cancellation=cancellation)
        return {"job": completed.to_mapping(), "output": completed.export_path, "youtube_video_id": completed.youtube_video_id}
    options = {key: getattr(args, key) for key in (
        "start", "end", "duration", "preset", "output", "output_dir", "youtube", "title",
        "description", "thumbnail", "keep_source", "force", "quality",
    )}
    gameplay_flags = ("keep_games", "boundary_mode", "agent_timeout", "agent_model", "agent_effort", "end_policy",
                      "report")
    if args.gameplay is None and any(getattr(args, key) is not None for key in gameplay_flags):
        raise _ArgumentError("--keep-games/--boundary-mode/--agent-*/--report require --gameplay.")
    packaging_flags = ("packaging_provider", "packaging_language", "thumbnail_engine", "packaging_output")
    if not args.packaging and any(getattr(args, key) is not None for key in packaging_flags):
        raise _ArgumentError("--packaging-*/--thumbnail-engine require --packaging.")
    if args.packaging and args.analyze_only:
        raise _ArgumentError("--packaging needs an export; it cannot be combined with --analyze-only.")
    if args.gameplay is not None:
        options["gameplay"] = {"game": args.gameplay, "keep_games": args.keep_games or "wins",
                               "boundary_mode": args.boundary_mode or "exact", "agent_timeout": args.agent_timeout,
                               "agent_model": args.agent_model, "agent_effort": args.agent_effort,
                               "end_policy": args.end_policy, "report": args.report}
        if options["gameplay"]["boundary_mode"] in ("claude", "codex"):
            print(f"NOTE: --boundary-mode {options['gameplay']['boundary_mode']} sends the local transcript/timing context "
                  "around the boundaries of the kept games to the provider configured in that CLI. Use --boundary-mode exact to send nothing.",
                  file=progress, flush=True)
    cancellation = PipelineCancellation()
    with _ctrl_c(cancellation), cancellable_probe_scope(cancellation):
        try:
            plan = runtime.plan_vod(args.url, **options) if command == "vod" else runtime.plan_local(args.source, **options)
        except Exception:
            cancellation.raise_if_cancelled()
            raise
        cancellation.raise_if_cancelled()
        if args.dry_run:
            if not getattr(args, "json", False):
                _human_plan(plan, progress)
            return {"dry_run": True, "plan": plan}
        job = runtime.create_job(plan)
        print(f"Job: {job.id}", file=progress, flush=True)
        print(f"Source: {plan['media']['title']}\nPreset: {plan['preset']['name']}", file=progress, flush=True)
        run_options = {"stop_after_analysis": True} if args.analyze_only else {}
        packaging = None
        packaging_error: str | None = None
        if args.packaging:
            # analysis -> packaging local stage -> [export || provider calls] -> packaging finish
            analysed = runtime.run(job.id, cancellation=cancellation, stop_after_analysis=True)
            from automation.packaging_cli import ParallelPackaging

            try:
                packaging = ParallelPackaging(
                    project=analysed.project_path, video=plan.get("delivery", {}).get("output_path"),
                    output=args.packaging_output, provider=args.packaging_provider or "codex",
                    language=args.packaging_language or "it", thumbnail_engine=args.thumbnail_engine or "gen",
                    progress=progress)
                packaging.prepare()
                packaging.start()
            except Exception as exc:  # the video still gets exported
                packaging, packaging_error = None, f"{type(exc).__name__}: {exc}"
                print(f"[Packaging] FAILED before the export: {packaging_error}", file=progress, flush=True)
        try:
            completed = runtime.run(job.id, cancellation=cancellation, **run_options)
        except BaseException:
            if packaging is not None:
                packaging.cancel()
            raise
        packaging_result: dict[str, Any] | None = None
        if packaging is not None:
            try:
                packaging_result = packaging.finish(check_cancelled=cancellation.raise_if_cancelled)
            except Exception as exc:
                if cancellation.cancelled:
                    raise
                packaging_error = f"{type(exc).__name__}: {exc}"
                print(f"[Packaging] FAILED: {packaging_error} (the video export is complete)", file=progress,
                      flush=True)
    if args.analyze_only:
        return {"analyze_only": True, "job": completed.to_mapping(), "project": completed.project_path,
                "report": completed.metadata.get("gameplay_report_path"),
                "summary": completed.metadata.get("analysis_summary", {})}
    result = {"job": completed.to_mapping(), "output": completed.export_path,
              "youtube_video_id": completed.youtube_video_id,
              "report": completed.metadata.get("gameplay_report_path"),
              "summary": completed.metadata.get("analysis_summary", {})}
    if args.packaging:
        result["packaging"] = packaging_result if packaging_result is not None else {
            "status": "failed", "error": packaging_error or "packaging did not run"}
        from automation.packaging_cli import human, interactive_session, wants_interactive

        if packaging_result and packaging_result.get("status") == "done" and wants_interactive(args):
            print(f"DONE\nOutput:\n{completed.export_path}", file=progress, flush=True)
            human("packaging", {"packaging": packaging_result}, progress)
            result["thumbnail_session"] = interactive_session(packaging_result["output"], progress)
    return result


def _error_info(exc: Exception, command: str) -> tuple[int, str, str]:
    code = getattr(exc, "code", type(exc).__name__)
    explicit = getattr(exc, "exit_code", None)
    if explicit is not None:
        return int(explicit), str(code), str(exc)
    name = type(exc).__name__
    if str(code).endswith("cancelled") or "Cancelled" in name:
        return EXIT_CANCELLED, "cancelled", str(exc)
    if name == "PipelineJobBusyError" or name == "FileLockBusyError":
        return EXIT_BUSY, "job_busy", str(exc)
    if name == "PipelineJobNotFoundError":
        return EXIT_ARGUMENTS, "job_not_found", str(exc)
    if isinstance(exc, OSError) or name.endswith("StoreError"):
        return EXIT_FILESYSTEM, str(code), str(exc)
    if command == "auth" or name in {"TwitchAuthRequired", "TwitchApiError"} or "Auth" in name:
        return EXIT_AUTH, str(code), str(exc)
    if "Download" in name:
        return EXIT_DOWNLOAD, str(code), str(exc)
    if "Analysis" in name:
        return EXIT_ANALYSIS, str(code), str(exc)
    if "Export" in name:
        return EXIT_EXPORT, str(code), str(exc)
    if "Upload" in name or "YouTube" in name:
        return EXIT_UPLOAD, str(code), str(exc)
    if isinstance(exc, (ValueError, KeyError)):
        return EXIT_ARGUMENTS, str(code), str(exc)
    return EXIT_ARGUMENTS, str(code), str(exc)


def _human_result(command: str, result: dict[str, Any], stream: TextIO) -> None:
    if command == "groups":
        from automation.hero_groups_cli import human as human_groups
        human_groups(result, stream)
    elif command == "clips":
        from automation.clips_cli import human as human_clips
        human_clips(result, stream)
    elif command == "short":
        from automation.short_cli import human as human_short
        human_short(result, stream)
    elif command.startswith("packaging"):
        from automation.packaging_cli import human
        human(command, result, stream)
    elif "version" in result:
        print(result["version"], file=stream)
    elif command == "presets":
        for name in result["presets"]:
            print(name, file=stream)
    elif command == "jobs" and "jobs" in result:
        for job in result["jobs"]:
            print(f"{job['id']}  {job['state']:14}  {job['source_title']}", file=stream)
    elif result.get("dry_run"):
        if "job" in result:
            print(json.dumps(result["job"], ensure_ascii=False, indent=2), file=stream)
        else:
            print("Dry run complete. No job was created.", file=stream)
    elif result.get("analyze_only"):
        summary = result.get("summary") or {}
        print(f"ANALYZED (not rendered)\nProject:\n{result['project']}", file=stream)
        if result.get("report"):
            print(f"Gameplay report:\n{result['report']}", file=stream)
        if summary:
            print(json.dumps(summary, ensure_ascii=False, indent=2), file=stream)
        print(f"Render later with: resume {result['job']['id']}", file=stream)
    elif "output" in result:
        print(f"DONE\nOutput:\n{result['output']}", file=stream)
        if result.get("report"):
            print(f"Gameplay report:\n{result['report']}", file=stream)
        if result.get("youtube_video_id"):
            print(f"YouTube video (PRIVATE):\n{result['youtube_video_id']}", file=stream)
        if result.get("thumbnail_session"):  # summary already shown before the interactive session
            final = next((a["approved"]["final"] for a in result["thumbnail_session"].get("actions", [])[::-1]
                          if "approved" in a), None)
            if final:
                print(f"Thumbnail confermata:\n{final}", file=stream)
        elif result.get("packaging"):
            if result["packaging"].get("status") == "failed" and "output" not in result["packaging"]:
                print(f"PACKAGING FAILED: {result['packaging'].get('error')}", file=stream)
            else:
                from automation.packaging_cli import human
                human("packaging", {"packaging": result["packaging"]}, stream)
    elif "job" in result:
        print(json.dumps(result["job"], ensure_ascii=False, indent=2), file=stream)
    elif command == "auth":
        print(f"{result['provider']}: {'authenticated' if result['authenticated'] else 'logged out'}", file=stream)
