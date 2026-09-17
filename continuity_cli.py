"""Command-line entry point for portfolio workflows."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from film_pipeline import (
    EXIT_PIPELINE_ERROR,
    FilmPipelineError,
    FixtureBlockerAuditor,
    FixtureVideoVendor,
    run_film,
)


def _shot_ids(value: str) -> set[str]:
    try:
        result = {str(int(item.strip())) for item in value.split(",") if item.strip()}
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use comma-separated shot numbers") from exc
    if any(not 1 <= int(item) <= 6 for item in result):
        raise argparse.ArgumentTypeError("shot numbers must be between 1 and 6")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="continuity")
    subcommands = parser.add_subparsers(dest="command", required=True)
    film = subcommands.add_parser(
        "film",
        help="run the offline six-shot prompt-to-film repair loop",
    )
    film.add_argument("idea", help="one-sentence film idea")
    film.add_argument(
        "--output",
        type=Path,
        default=Path("continuity-film-output"),
        help="artifact directory (default: continuity-film-output)",
    )
    film.add_argument(
        "--vendor",
        choices=("fixture",),
        default="fixture",
        help="single vendor used for the entire run",
    )
    film.add_argument(
        "--demo-blockers",
        type=_shot_ids,
        default={"2", "5"},
        metavar="IDS",
        help="fixture shots that fail their first take (default: 2,5)",
    )
    film.add_argument(
        "--max-repair-rounds",
        type=int,
        choices=(0, 1, 2),
        default=2,
        help="hard cap; passing shots are never regenerated",
    )
    film.add_argument(
        "--json",
        action="store_true",
        help="print only the final machine-readable summary",
    )
    return parser


def _print_human(run) -> None:
    report = run.report
    summary = report["summary"]
    initial = [event for event in report["generation_events"] if event["round"] == 0]
    print("Continuity Film · six-shot demo")
    print(f"[plan] canon-constrained shotlist: {len(report['shots'])} shots")
    print(
        f"[generate] vendor={report['vendor']} generated {len(initial)} initial shots"
    )
    first_audit = [
        event
        for event in report["audit_events"]
        if event["round"] == 0 and event["decision"] == "REGENERATE"
    ]
    first_ids = ", ".join(event["shot_id"] for event in first_audit) or "none"
    print(f"[audit 0] blocker shots: {first_ids}")
    for repair_round in range(1, summary["repair_rounds_used"] + 1):
        ids = [
            event["shot_id"]
            for event in report["generation_events"]
            if event["round"] == repair_round
        ]
        print(f"[repair {repair_round}] regenerated only: {', '.join(ids)}")
    print(
        f"[result] {summary['status']} · {summary['total_generation_count']} total generations · "
        "whole-film rerun: no"
    )

    def display(path: Path) -> str:
        try:
            return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            return str(path)

    if run.video_path:
        print(f"[film] {display(run.video_path)}")
    print(f"[report] {display(run.report_html)}")
    print(f"[json] {display(run.report_json)}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "film":
        return EXIT_PIPELINE_ERROR
    try:
        vendor = FixtureVideoVendor(blocker_shots=args.demo_blockers)
        run = run_film(
            args.idea,
            args.output,
            vendor=vendor,
            auditor=FixtureBlockerAuditor(),
            max_repair_rounds=args.max_repair_rounds,
        )
    except (FilmPipelineError, ValueError) as exc:
        code = getattr(exc, "exit_code", EXIT_PIPELINE_ERROR)
        print(f"continuity film: {exc}", file=sys.stderr)
        return int(code)
    if args.json:
        print(
            json.dumps(
                {
                    "status": run.report["summary"]["status"],
                    "exit_code": run.exit_code,
                    "film": str(run.video_path) if run.video_path else None,
                    "report": str(run.report_json),
                },
                ensure_ascii=False,
            )
        )
    else:
        _print_human(run)
    return run.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
