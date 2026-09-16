"""Versioned, dependency-free data contracts for Continuity Agent.

The validator deliberately covers only module boundaries: canon input, batch
audit input, and one-shot audit output.  Internal routing and UI state remain
implementation details and are not part of the public v1 contract.
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "1.0"
DECISIONS = frozenset({"PASS", "LOCAL FIX", "REGENERATE", "HUMAN REVIEW", "SKIP"})
RULE_SEVERITIES = frozenset({"local_fix", "regenerate", "human_review"})
INPUT_MEDIA_KINDS = frozenset({"video", "image_sequence"})
RESULT_MEDIA_KINDS = frozenset({"text", "image", "image_sequence", "video"})
EVIDENCE_KINDS = frozenset({"source_frame", "canon_frame", "comparison"})
ANCHOR_KINDS = frozenset({"chain", "canonical_lookup", "chain_with_crosscheck", "none"})


class ContractError(ValueError):
    """Raised when a value crosses a public boundary with an invalid shape."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _fail(path: str, message: str) -> None:
    raise ContractError(path, message)


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "must be a boolean")
    return value


def _integer(value: Any, path: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "must be an integer")
    if minimum is not None and value < minimum:
        _fail(path, f"must be >= {minimum}")
    return value


def _number(value: Any, path: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        _fail(path, f"must be between {minimum} and {maximum}")
    return number


def _required(obj: dict[str, Any], keys: Iterable[str], path: str) -> None:
    for key in keys:
        if key not in obj:
            _fail(f"{path}.{key}", "is required")


def _version(obj: dict[str, Any], path: str) -> None:
    _required(obj, ("contract_version",), path)
    version = _string(obj["contract_version"], f"{path}.contract_version")
    if version != CONTRACT_VERSION:
        _fail(
            f"{path}.contract_version",
            f"unsupported version {version!r}; expected {CONTRACT_VERSION!r}",
        )


def _validate_rule(
    rule: Any, path: str, seen: dict[str, tuple[str, str, str, str]]
) -> None:
    item = _object(rule, path)
    _required(item, ("id", "text", "severity"), path)
    rule_id = _string(item["id"], f"{path}.id")
    _string(item["text"], f"{path}.text")
    severity = _string(item["severity"], f"{path}.severity")
    if severity not in RULE_SEVERITIES:
        _fail(f"{path}.severity", f"must be one of {sorted(RULE_SEVERITIES)}")
    signature = (
        item["text"],
        severity,
        str(item.get("check", "")),
        str(item.get("note", "")),
    )
    if rule_id in seen and seen[rule_id] != signature:
        _fail(f"{path}.id", f"rule id {rule_id!r} has conflicting definitions")
    seen[rule_id] = signature


def _validate_rule_list(
    value: Any, path: str, seen: dict[str, tuple[str, str, str, str]]
) -> None:
    for index, rule in enumerate(_array(value, path)):
        _validate_rule(rule, f"{path}[{index}]", seen)


def validate_canon_document(payload: Any) -> dict[str, Any]:
    """Validate the stable core of a canon document and return it unchanged."""

    canon = _object(payload, "canon")
    _required(canon, ("meta", "characters", "locations", "shots"), "canon")

    meta = _object(canon["meta"], "canon.meta")
    _version(meta, "canon.meta")
    _string(meta.get("version"), "canon.meta.version")

    characters = _object(canon["characters"], "canon.characters")
    locations = _object(canon["locations"], "canon.locations")
    shots = _object(canon["shots"], "canon.shots")
    if not shots:
        _fail("canon.shots", "must contain at least one shot")

    seen_rules: dict[str, tuple[str, str, str, str]] = {}
    _validate_rule_list(canon.get("global_rules", []), "canon.global_rules", seen_rules)

    camera = _object(canon.get("camera", {}), "canon.camera")
    _validate_rule_list(camera.get("rules", []), "canon.camera.rules", seen_rules)

    for group_name, group in (("characters", characters), ("locations", locations)):
        for entity_id, raw_entity in group.items():
            _string(entity_id, f"canon.{group_name} key")
            entity = _object(raw_entity, f"canon.{group_name}.{entity_id}")
            _validate_rule_list(
                entity.get("rules", []),
                f"canon.{group_name}.{entity_id}.rules",
                seen_rules,
            )

    attempts = _object(canon.get("attempts", {}), "canon.attempts")
    for attempt_id, raw_attempt in attempts.items():
        attempt = _object(raw_attempt, f"canon.attempts.{attempt_id}")
        _validate_rule_list(
            attempt.get("rules", []),
            f"canon.attempts.{attempt_id}.rules",
            seen_rules,
        )

    for shot_id, raw_shot in shots.items():
        _string(shot_id, "canon.shots key")
        shot = _object(raw_shot, f"canon.shots.{shot_id}")
        if "location" in shot and shot["location"] is not None:
            location = _string(shot["location"], f"canon.shots.{shot_id}.location")
            if location not in locations:
                _fail(
                    f"canon.shots.{shot_id}.location",
                    f"references unknown location {location!r}",
                )
        _validate_rule_list(
            shot.get("rules", []), f"canon.shots.{shot_id}.rules", seen_rules
        )

    anchor_plan = _object(canon.get("anchor_plan", {}), "canon.anchor_plan")
    reference_library = _object(
        anchor_plan.get("reference_library", {}),
        "canon.anchor_plan.reference_library",
    )
    for reference_id, raw_reference in reference_library.items():
        _string(reference_id, "canon.anchor_plan.reference_library key")
        reference = _object(
            raw_reference, f"canon.anchor_plan.reference_library.{reference_id}"
        )
        _string(
            reference.get("desc"),
            f"canon.anchor_plan.reference_library.{reference_id}.desc",
        )
        has_source = (
            bool(reference.get("asset_path"))
            or reference.get("source_shot") is not None
        )
        if not has_source and not reference.get("blocking", False):
            _fail(
                f"canon.anchor_plan.reference_library.{reference_id}",
                "must provide asset_path/source_shot or be marked blocking",
            )

    anchors = _object(anchor_plan.get("shots", {}), "canon.anchor_plan.shots")
    for shot_id, raw_anchor in anchors.items():
        if shot_id not in shots:
            _fail(f"canon.anchor_plan.shots.{shot_id}", "references an unknown shot")
        anchor = _object(raw_anchor, f"canon.anchor_plan.shots.{shot_id}")
        kind = _string(
            anchor.get("anchor"), f"canon.anchor_plan.shots.{shot_id}.anchor"
        )
        if kind not in ANCHOR_KINDS:
            _fail(
                f"canon.anchor_plan.shots.{shot_id}.anchor",
                f"must be one of {sorted(ANCHOR_KINDS)}",
            )
        if kind == "chain" and anchor.get("from_shot") is None:
            _fail(f"canon.anchor_plan.shots.{shot_id}.from_shot", "is required")
        if kind == "canonical_lookup":
            reference_id = _string(
                anchor.get("reference"),
                f"canon.anchor_plan.shots.{shot_id}.reference",
            )
            if reference_id not in reference_library:
                _fail(
                    f"canon.anchor_plan.shots.{shot_id}.reference",
                    f"references unknown canon asset {reference_id!r}",
                )
        if kind == "chain_with_crosscheck":
            if anchor.get("from_shot") is None:
                _fail(f"canon.anchor_plan.shots.{shot_id}.from_shot", "is required")
            reference_id = _string(
                anchor.get("crosscheck_reference"),
                f"canon.anchor_plan.shots.{shot_id}.crosscheck_reference",
            )
            if reference_id not in reference_library:
                _fail(
                    f"canon.anchor_plan.shots.{shot_id}.crosscheck_reference",
                    f"references unknown canon asset {reference_id!r}",
                )

    return canon


def validate_audit_request(payload: Any) -> dict[str, Any]:
    """Validate one batch request containing videos and/or image sequences."""

    request = _object(payload, "request")
    _version(request, "request")
    _required(request, ("job_id", "canon_path", "shots"), "request")
    _string(request["job_id"], "request.job_id")
    _string(request["canon_path"], "request.canon_path")
    shots = _array(request["shots"], "request.shots")
    if not shots:
        _fail("request.shots", "must contain at least one shot")

    seen_shots: set[str] = set()
    for index, raw_shot in enumerate(shots):
        path = f"request.shots[{index}]"
        shot = _object(raw_shot, path)
        _required(shot, ("shot_id", "media"), path)
        shot_id = _string(shot["shot_id"], f"{path}.shot_id")
        if shot_id in seen_shots:
            _fail(f"{path}.shot_id", f"duplicate shot id {shot_id!r}")
        seen_shots.add(shot_id)

        media = _object(shot["media"], f"{path}.media")
        _required(media, ("kind", "paths"), f"{path}.media")
        kind = _string(media["kind"], f"{path}.media.kind")
        if kind not in INPUT_MEDIA_KINDS:
            _fail(f"{path}.media.kind", f"must be one of {sorted(INPUT_MEDIA_KINDS)}")
        paths = _array(media["paths"], f"{path}.media.paths")
        if not paths:
            _fail(f"{path}.media.paths", "must not be empty")
        for media_index, source in enumerate(paths):
            _string(source, f"{path}.media.paths[{media_index}]")
        if kind == "video" and len(paths) != 1:
            _fail(f"{path}.media.paths", "video input requires exactly one path")

        timestamps = media.get("timestamps_s")
        if timestamps is not None:
            values = _array(timestamps, f"{path}.media.timestamps_s")
            if kind == "image_sequence" and len(values) != len(paths):
                _fail(
                    f"{path}.media.timestamps_s",
                    "image_sequence timestamps must match the number of paths",
                )
            previous = -1.0
            for timestamp_index, timestamp in enumerate(values):
                number = _number(
                    timestamp,
                    f"{path}.media.timestamps_s[{timestamp_index}]",
                    0.0,
                    float("inf"),
                )
                if number < previous:
                    _fail(f"{path}.media.timestamps_s", "must be ordered")
                previous = number

    options = request.get("options", {})
    options = _object(options, "request.options")
    if "sample_count" in options:
        sample_count = _integer(options["sample_count"], "request.options.sample_count")
        if not 3 <= sample_count <= 7:
            _fail("request.options.sample_count", "must be between 3 and 7")

    return request


def _validate_evidence_assets(value: Any) -> set[str]:
    evidence_ids: set[str] = set()
    for index, raw_asset in enumerate(_array(value, "result.evidence_assets")):
        path = f"result.evidence_assets[{index}]"
        asset = _object(raw_asset, path)
        _required(asset, ("evidence_id", "kind", "path", "label"), path)
        evidence_id = _string(asset["evidence_id"], f"{path}.evidence_id")
        if evidence_id in evidence_ids:
            _fail(f"{path}.evidence_id", f"duplicate evidence id {evidence_id!r}")
        evidence_ids.add(evidence_id)
        kind = _string(asset["kind"], f"{path}.kind")
        if kind not in EVIDENCE_KINDS:
            _fail(f"{path}.kind", f"must be one of {sorted(EVIDENCE_KINDS)}")
        _string(asset["path"], f"{path}.path")
        _string(asset["label"], f"{path}.label")
        if "timestamp_s" in asset:
            _number(asset["timestamp_s"], f"{path}.timestamp_s", 0.0, float("inf"))
    return evidence_ids


def validate_audit_result(payload: Any) -> dict[str, Any]:
    """Validate one shot result, including issue-to-evidence references."""

    result = _object(payload, "result")
    _version(result, "result")
    _required(
        result,
        (
            "shot_id",
            "canon_version",
            "decision",
            "score",
            "issues",
            "evidence_assets",
            "media_kind",
            "api_reviewed",
            "needs_human_review",
            "metrics",
            "video_timestamps_s",
            "api_retries",
            "rule_count",
        ),
        "result",
    )
    _string(result["shot_id"], "result.shot_id")
    _string(result["canon_version"], "result.canon_version")
    decision = _string(result["decision"], "result.decision")
    if decision not in DECISIONS:
        _fail("result.decision", f"must be one of {sorted(DECISIONS)}")
    if result["score"] is not None:
        _integer(result["score"], "result.score", 0)
        if result["score"] > 100:
            _fail("result.score", "must be <= 100")

    media_kind = _string(result["media_kind"], "result.media_kind")
    if media_kind not in RESULT_MEDIA_KINDS:
        _fail("result.media_kind", f"must be one of {sorted(RESULT_MEDIA_KINDS)}")
    _boolean(result["api_reviewed"], "result.api_reviewed")
    _boolean(result["needs_human_review"], "result.needs_human_review")
    _object(result["metrics"], "result.metrics")
    _integer(result["api_retries"], "result.api_retries", 0)
    _integer(result["rule_count"], "result.rule_count", 0)

    timestamps = _array(result["video_timestamps_s"], "result.video_timestamps_s")
    previous = -1.0
    for index, timestamp in enumerate(timestamps):
        number = _number(
            timestamp,
            f"result.video_timestamps_s[{index}]",
            0.0,
            float("inf"),
        )
        if number < previous:
            _fail("result.video_timestamps_s", "must be ordered")
        previous = number
    if media_kind != "video" and timestamps:
        _fail("result.video_timestamps_s", "must be empty for non-video results")

    evidence_ids = _validate_evidence_assets(result["evidence_assets"])
    issues = _array(result["issues"], "result.issues")
    if decision in {"PASS", "SKIP"} and issues:
        _fail("result.issues", f"must be empty when decision is {decision}")
    for index, raw_issue in enumerate(issues):
        path = f"result.issues[{index}]"
        issue = _object(raw_issue, path)
        _required(
            issue,
            (
                "rule_id",
                "category",
                "severity",
                "confidence",
                "evidence",
                "rule",
                "minimal_fix",
                "evidence_ids",
            ),
            path,
        )
        _string(issue["rule_id"], f"{path}.rule_id")
        _string(issue["category"], f"{path}.category")
        severity = _string(issue["severity"], f"{path}.severity")
        if severity not in RULE_SEVERITIES:
            _fail(f"{path}.severity", f"must be one of {sorted(RULE_SEVERITIES)}")
        _number(issue["confidence"], f"{path}.confidence", 0.0, 1.0)
        _string(issue["evidence"], f"{path}.evidence")
        _string(issue["rule"], f"{path}.rule")
        _string(issue["minimal_fix"], f"{path}.minimal_fix")
        references = _array(issue["evidence_ids"], f"{path}.evidence_ids")
        for reference_index, reference in enumerate(references):
            evidence_id = _string(reference, f"{path}.evidence_ids[{reference_index}]")
            if evidence_id not in evidence_ids:
                _fail(
                    f"{path}.evidence_ids[{reference_index}]",
                    f"references unknown evidence asset {evidence_id!r}",
                )
        if "requires_confirmation" in issue:
            _boolean(issue["requires_confirmation"], f"{path}.requires_confirmation")

    return result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    path = Path(args[0]) if args else Path(__file__).with_name("canon.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate_canon_document(payload)
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        print(f"INVALID: {exc}")
        return 1
    print(f"Continuity contract {CONTRACT_VERSION}: OK ({path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
