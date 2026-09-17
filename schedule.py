"""Executable contract for the Continuity Agent v1 fourteen-day schedule."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEDULE_VERSION = "1.0"
HARD_GATE_DAYS = (5, 7, 11)
COMPLETE_STATUSES = frozenset({"DONE", "DONE_WITH_FALLBACK"})

EXIT_SCHEDULE_OK = 0
EXIT_SCHEDULE_INCOMPLETE = 30
EXIT_SCHEDULE_INVALID = 31


class ScheduleError(ValueError):
    """Raised when the public schedule contract is invalid."""


def load_schedule(path: str | Path) -> dict[str, Any]:
    schedule = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_schedule(schedule)


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError(f"{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ScheduleError(f"{field} must stay inside the repository")
    return path.as_posix()


def validate_schedule(schedule: Any) -> dict[str, Any]:
    if not isinstance(schedule, dict):
        raise ScheduleError("schedule must be an object")
    if schedule.get("schedule_version") != SCHEDULE_VERSION:
        raise ScheduleError("unsupported schedule_version")
    if schedule.get("duration_days") != 14:
        raise ScheduleError("duration_days must be 14")
    if schedule.get("day_labels_are_relative") is not True:
        raise ScheduleError(
            "day labels must be relative, not fabricated calendar dates"
        )
    for name in ("title", "success_metric"):
        if not isinstance(schedule.get(name), str) or not schedule[name].strip():
            raise ScheduleError(f"{name} must be a non-empty string")

    days = schedule.get("days")
    if not isinstance(days, list) or len(days) != 14:
        raise ScheduleError("days must contain exactly 14 entries")
    numbers = [day.get("day") for day in days if isinstance(day, dict)]
    if numbers != list(range(1, 15)):
        raise ScheduleError("days must be uniquely ordered from 1 through 14")

    gates: list[int] = []
    for index, day in enumerate(days):
        field = f"days[{index}]"
        for name in ("title", "objective"):
            if not isinstance(day.get(name), str) or not day[name].strip():
                raise ScheduleError(f"{field}.{name} must be a non-empty string")
        tasks = day.get("tasks")
        if (
            not isinstance(tasks, list)
            or not tasks
            or not all(isinstance(item, str) and item.strip() for item in tasks)
        ):
            raise ScheduleError(f"{field}.tasks must be a non-empty string array")
        depends_on = day.get("depends_on")
        if not isinstance(depends_on, list) or any(
            not isinstance(item, int) or item < 1 or item >= day["day"]
            for item in depends_on
        ):
            raise ScheduleError(f"{field}.depends_on may reference earlier days only")
        if len(depends_on) != len(set(depends_on)):
            raise ScheduleError(f"{field}.depends_on contains duplicates")
        deliverables = day.get("deliverables")
        if not isinstance(deliverables, list) or not deliverables:
            raise ScheduleError(f"{field}.deliverables must not be empty")
        for deliverable_index, deliverable in enumerate(deliverables):
            if not isinstance(deliverable, dict):
                raise ScheduleError(
                    f"{field}.deliverables[{deliverable_index}] must be an object"
                )
            _relative_path(
                deliverable.get("path"),
                f"{field}.deliverables[{deliverable_index}].path",
            )
            if not isinstance(deliverable.get("description"), str):
                raise ScheduleError(
                    f"{field}.deliverables[{deliverable_index}].description must be a string"
                )

        gate = day.get("gate")
        if gate is None:
            continue
        if not isinstance(gate, dict) or gate.get("hard") is not True:
            raise ScheduleError(f"{field}.gate must be null or a hard gate")
        gates.append(day["day"])
        evidence_sets = gate.get("evidence_sets")
        if not isinstance(evidence_sets, list) or not evidence_sets:
            raise ScheduleError(f"{field}.gate.evidence_sets must not be empty")
        outcomes: set[str] = set()
        for evidence_index, evidence in enumerate(evidence_sets):
            evidence_field = f"{field}.gate.evidence_sets[{evidence_index}]"
            if not isinstance(evidence, dict):
                raise ScheduleError(f"{evidence_field} must be an object")
            outcome = evidence.get("outcome")
            if outcome not in {"pass", "fallback"} or outcome in outcomes:
                raise ScheduleError(
                    f"{evidence_field}.outcome is invalid or duplicated"
                )
            outcomes.add(outcome)
            if (
                not isinstance(evidence.get("label"), str)
                or not evidence["label"].strip()
            ):
                raise ScheduleError(f"{evidence_field}.label must be non-empty")
            paths = evidence.get("paths")
            if not isinstance(paths, list) or not paths:
                raise ScheduleError(f"{evidence_field}.paths must not be empty")
            for path_index, path in enumerate(paths):
                _relative_path(path, f"{evidence_field}.paths[{path_index}]")
        if (
            not isinstance(gate.get("failure_action"), str)
            or not gate["failure_action"].strip()
        ):
            raise ScheduleError(f"{field}.gate.failure_action must be non-empty")

    if tuple(gates) != HARD_GATE_DAYS:
        raise ScheduleError("hard gates must be exactly D5, D7, and D11")
    if schedule.get("hard_gate_days") != list(HARD_GATE_DAYS):
        raise ScheduleError("hard_gate_days does not match the day definitions")
    return schedule


def _paths_exist(root: Path, paths: list[str]) -> bool:
    return all((root / path).exists() for path in paths)


def evaluate_schedule(
    schedule: dict[str, Any], repository_root: str | Path
) -> dict[str, Any]:
    validate_schedule(schedule)
    root = Path(repository_root).resolve()
    results: list[dict[str, Any]] = []

    for day in schedule["days"]:
        dependencies_complete = all(
            results[number - 1]["status"] in COMPLETE_STATUSES
            for number in day["depends_on"]
        )
        missing = [
            deliverable["path"]
            for deliverable in day["deliverables"]
            if not (root / deliverable["path"]).exists()
        ]
        gate_outcome = None
        gate = day.get("gate")
        if gate:
            for evidence in gate["evidence_sets"]:
                if _paths_exist(root, evidence["paths"]):
                    gate_outcome = evidence["outcome"]
                    break

        if not dependencies_complete:
            status = "BLOCKED"
        elif gate and gate_outcome is None:
            status = "GATE_PENDING"
        elif missing:
            status = "READY"
        elif gate_outcome == "fallback":
            status = "DONE_WITH_FALLBACK"
        else:
            status = "DONE"

        results.append(
            {
                "day": day["day"],
                "title": day["title"],
                "status": status,
                "missing_deliverables": missing,
                "gate_outcome": gate_outcome,
                "failure_action": gate.get("failure_action") if gate else None,
            }
        )

    next_day = next(
        (item["day"] for item in results if item["status"] not in COMPLETE_STATUSES),
        None,
    )
    return {
        "schedule_version": SCHEDULE_VERSION,
        "duration_days": 14,
        "complete_days": sum(item["status"] in COMPLETE_STATUSES for item in results),
        "next_day": next_day,
        "all_complete": next_day is None,
        "hard_gates": [item for item in results if item["day"] in HARD_GATE_DAYS],
        "days": results,
    }


def render_schedule_status(evaluation: dict[str, Any]) -> str:
    icons = {
        "DONE": "✓",
        "DONE_WITH_FALLBACK": "△",
        "READY": "→",
        "GATE_PENDING": "!",
        "BLOCKED": "·",
    }
    lines = [
        "Continuity Agent · v1 14-day schedule",
        f"Progress: {evaluation['complete_days']}/14 days",
        "",
    ]
    for day in evaluation["days"]:
        lines.append(
            f"{icons[day['status']]} D{day['day']:02d} {day['status']:<18} {day['title']}"
        )
    lines.append("")
    if evaluation["next_day"] is None:
        lines.append("Next: release gate complete")
    else:
        day = evaluation["days"][evaluation["next_day"] - 1]
        lines.append(f"Next: D{day['day']:02d} · {day['title']}")
        if day["missing_deliverables"]:
            lines.append("Missing: " + ", ".join(day["missing_deliverables"]))
    lines.append(
        "Legend: ✓ done · △ explicit fallback · → ready · ! gate pending · · blocked"
    )
    return "\n".join(lines)
