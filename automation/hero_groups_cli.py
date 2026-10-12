"""Minimal post-detection grouping CLI; no analysis/ASR/agent reruns."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TextIO


def add_parser(sub: Any, json_option: Any) -> None:
    parser = sub.add_parser("groups", help="Group detected games by bottom player portrait; reuse existing keeps.")
    parser.add_argument("project", help="Existing .autocutter project with Gameplay Editing results.")
    parser.add_argument(
        "--analyze", action="store_true", help="Sample portraits locally and save groups in the project."
    )
    parser.add_argument("--class-refs", type=Path, help="Optional local hero-power reference directory.")
    parser.add_argument("--export", metavar="hunter|mage|selected", help="Export one class or all selected classes.")
    parser.add_argument("--output-dir", type=Path, help="Destination for independent output projects/videos.")
    json_option(parser)


def execute(args: Any, progress: TextIO) -> dict[str, Any]:
    from automation.cli import _ctrl_c
    from automation.runtime import PipelineCancellation
    from automation.exporter import ExportCancellation
    from analysis.audio_service import AnalysisCancellation
    from analysis.hero_groups.recognition import HeroGroupingService, atomic_json
    from analysis.hero_groups.labels import class_id
    from analysis.hero_groups.projects import read_project, analyze_project, prepare_outputs, export_outputs
    from core.project_file import make_payload_portable

    project = Path(args.project).expanduser().resolve()
    payload = read_project(project)
    cancellation = PipelineCancellation()
    with _ctrl_c(cancellation):
        if args.analyze:
            token = AnalysisCancellation()
            cancellation.attach(token)
            try:
                payload = analyze_project(
                    payload,
                    service=HeroGroupingService(class_refs=args.class_refs),
                    cancellation=token,
                    on_progress=lambda p: print(f"[Portraits] {p}%", file=progress, flush=True),
                )
                cancellation.raise_if_cancelled()
                atomic_json(project, make_payload_portable(payload, project))
            finally:
                cancellation.detach(token)
        sources = {t["path"] for t in payload["tracks"] if t.get("gameplay_grouping")}
        if not sources:
            raise ValueError("No portrait groups saved. Use --analyze after Gameplay Editing.")
        rows = [
            {"source": t["path"], "groups": t["gameplay_grouping"]}
            for t in payload["tracks"]
            if t.get("gameplay_grouping")
        ]
        # Split tracks of one VOD share the same recognition data.
        rows = list({r["source"]: r for r in rows}.values())
        result: dict[str, Any] = {"project": str(project), "sources": rows}
        if args.export:
            if len(sources) != 1:
                raise ValueError("Export groups from a project containing one source VOD.")
            groups = None if args.export == "selected" else [class_id(args.export) or args.export.upper()]
            plans, warnings = prepare_outputs(
                payload, args.output_dir or project.parent / (project.stem + "_groups"), groups
            )
            result["warnings"] = warnings
            token_export = ExportCancellation()
            cancellation.attach(token_export)
            try:
                result["outputs"] = export_outputs(
                    plans,
                    cancellation=token_export,
                    on_progress=lambda p: print(f"[Export] {p}%", file=progress, flush=True),
                )
            finally:
                cancellation.detach(token_export)
        return result


def human(result: dict[str, Any], stream: TextIO) -> None:
    from analysis.hero_groups.labels import class_label, display_group

    for source in result["sources"]:
        print(source["source"], file=stream)
        for row in source["groups"]["games"]:
            print(
                f"  Game {row['game']['index']} {row['game']['result']} — {class_label(source['groups']['groups'].get(row.get('group'), {}).get('class_name'))} "
                f"similarity={row.get('similarity', 0):.3f} support={row.get('support', 0)}",
                file=stream,
            )
        for key, group in source["groups"]["groups"].items():
            print(f"  {display_group(group)}: {len(group['games'])} games / {group['wins']} WIN", file=stream)
    for warning in result.get("warnings", []):
        print("Warning: " + warning, file=stream)
    for output in result.get("outputs", []):
        print(f"Group {output['group']}: {output['output']}\nProject: {output['project']}", file=stream)
