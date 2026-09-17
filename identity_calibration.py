"""Reproducible CPU-only calibration for the identity consistency rule.

The command scores labelled image pairs with ``identity_similarity`` and
recommends the two decision thresholds.  It never edits canon.json.  Reports
from synthetic or undersized datasets are explicitly marked provisional so
test fixtures cannot be mistaken for production calibration evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rules.identity import IdentityConsistencyPlugin, identity_similarity

CONTRACT_VERSION = "1.0"
LABELS = ("mismatch", "ambiguous", "match")
SPLITS = ("calibration", "validation")
READY_MINIMUMS = {
    "calibration": {"mismatch": 20, "ambiguous": 10, "match": 20},
    "validation": {"mismatch": 10, "ambiguous": 5, "match": 10},
}
READY_QUALITY = {
    "minimum_macro_recall": 0.8,
    "maximum_validation_critical_errors": 0,
}


class BenchmarkError(ValueError):
    """Raised when the benchmark manifest or one of its assets is invalid."""


@dataclass(frozen=True)
class ScoredCase:
    case_id: str
    label: str
    split: str
    score: float
    structure: float
    color: float
    edges: float


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BenchmarkError(f"{path} must be an object")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError(f"{path} must be a non-empty string")
    return value


def _crop(value: Any, path: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise BenchmarkError(f"{path} must contain four normalized numbers")
    numbers: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise BenchmarkError(f"{path}[{index}] must be a number")
        number = float(item)
        if not 0 <= number <= 1:
            raise BenchmarkError(f"{path}[{index}] must be between 0 and 1")
        numbers.append(number)
    left, top, right, bottom = numbers
    if left >= right or top >= bottom:
        raise BenchmarkError(f"{path} must satisfy left < right and top < bottom")
    return numbers


def _reject_unknown_keys(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise BenchmarkError(f"{path} has unknown fields: {unknown}")


def validate_benchmark_document(payload: Any) -> dict[str, Any]:
    """Validate and return an identity benchmark manifest unchanged."""

    benchmark = _object(payload, "benchmark")
    _reject_unknown_keys(
        benchmark,
        {"contract_version", "benchmark_id", "source_kind", "description", "cases"},
        "benchmark",
    )
    if benchmark.get("contract_version") != CONTRACT_VERSION:
        raise BenchmarkError(f"benchmark.contract_version must be {CONTRACT_VERSION!r}")
    _string(benchmark.get("benchmark_id"), "benchmark.benchmark_id")
    source_kind = _string(benchmark.get("source_kind"), "benchmark.source_kind")
    if source_kind not in {"real", "synthetic"}:
        raise BenchmarkError("benchmark.source_kind must be 'real' or 'synthetic'")
    if "description" in benchmark:
        _string(benchmark["description"], "benchmark.description")
    cases = benchmark.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BenchmarkError("benchmark.cases must be a non-empty array")

    seen_ids: set[str] = set()
    seen_pairs: set[tuple[Any, ...]] = set()
    for index, raw_case in enumerate(cases):
        path = f"benchmark.cases[{index}]"
        case = _object(raw_case, path)
        _reject_unknown_keys(
            case,
            {
                "id",
                "label",
                "split",
                "reference_path",
                "candidate_path",
                "reference_crop",
                "candidate_crop",
                "character_id",
                "shot_id",
                "notes",
            },
            path,
        )
        case_id = _string(case.get("id"), f"{path}.id")
        if case_id in seen_ids:
            raise BenchmarkError(f"{path}.id duplicates {case_id!r}")
        seen_ids.add(case_id)
        label = _string(case.get("label"), f"{path}.label")
        if label not in LABELS:
            raise BenchmarkError(f"{path}.label must be one of {list(LABELS)}")
        split = _string(case.get("split"), f"{path}.split")
        if split not in SPLITS:
            raise BenchmarkError(f"{path}.split must be one of {list(SPLITS)}")
        reference_path = _string(case.get("reference_path"), f"{path}.reference_path")
        candidate_path = _string(case.get("candidate_path"), f"{path}.candidate_path")
        reference_crop = None
        candidate_crop = None
        if "reference_crop" in case:
            reference_crop = _crop(case["reference_crop"], f"{path}.reference_crop")
        if "candidate_crop" in case:
            candidate_crop = _crop(case["candidate_crop"], f"{path}.candidate_crop")
        for field in ("character_id", "shot_id", "notes"):
            if field in case:
                _string(case[field], f"{path}.{field}")
        pair = (
            reference_path,
            candidate_path,
            tuple(reference_crop or ()),
            tuple(candidate_crop or ()),
        )
        if pair in seen_pairs:
            raise BenchmarkError(f"{path} duplicates an existing image pair and crops")
        seen_pairs.add(pair)
    return benchmark


def load_benchmark(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(
            f"Cannot read benchmark manifest: {manifest_path}"
        ) from exc
    return validate_benchmark_document(payload)


def _resolve_asset(manifest_path: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else manifest_path.parent / path


def score_benchmark(
    benchmark: dict[str, Any], manifest_path: str | Path
) -> list[ScoredCase]:
    """Score every benchmark case using the production identity metric."""

    root = Path(manifest_path)
    scored: list[ScoredCase] = []
    for case in benchmark["cases"]:
        reference_path = _resolve_asset(root, case["reference_path"])
        candidate_path = _resolve_asset(root, case["candidate_path"])
        try:
            similarity = identity_similarity(
                reference_path,
                candidate_path,
                case.get("reference_crop"),
                case.get("candidate_crop"),
            )
        except ValueError as exc:
            raise BenchmarkError(f"Case {case['id']!r}: {exc}") from exc
        scored.append(
            ScoredCase(
                case_id=case["id"],
                label=case["label"],
                split=case["split"],
                score=similarity.score,
                structure=similarity.structure,
                color=similarity.color,
                edges=similarity.edges,
            )
        )
    return scored


def _predict(score: float, reject_below: float, review_below: float) -> str:
    if score < reject_below:
        return "mismatch"
    if score < review_below:
        return "ambiguous"
    return "match"


def _metrics(
    cases: Sequence[ScoredCase], reject_below: float, review_below: float
) -> dict[str, Any]:
    confusion = {actual: {predicted: 0 for predicted in LABELS} for actual in LABELS}
    correct = 0
    critical_errors = 0
    for case in cases:
        predicted = _predict(case.score, reject_below, review_below)
        confusion[case.label][predicted] += 1
        correct += predicted == case.label
        critical_errors += (case.label, predicted) in {
            ("mismatch", "match"),
            ("match", "mismatch"),
        }

    recalls: dict[str, float | None] = {}
    for label in LABELS:
        total = sum(confusion[label].values())
        recalls[label] = round(confusion[label][label] / total, 4) if total else None
    available_recalls = [value for value in recalls.values() if value is not None]
    total_cases = len(cases)
    return {
        "total": total_cases,
        "accuracy": round(correct / total_cases, 4) if total_cases else None,
        "macro_recall": (
            round(sum(available_recalls) / len(available_recalls), 4)
            if available_recalls
            else None
        ),
        "critical_errors": critical_errors,
        "per_label_recall": recalls,
        "confusion": confusion,
    }


def _threshold_candidates(cases: Sequence[ScoredCase]) -> list[float]:
    values = sorted({case.score for case in cases})
    return sorted({0.0, 1.0, *values})


def _threshold_rank(
    scores_by_label: dict[str, list[float]],
    reject_below: float,
    review_below: float,
) -> tuple[float, int, float]:
    confusion: dict[str, dict[str, int]] = {}
    for label, scores in scores_by_label.items():
        reject_index = bisect_left(scores, reject_below)
        review_index = bisect_left(scores, review_below)
        confusion[label] = {
            "mismatch": reject_index,
            "ambiguous": review_index - reject_index,
            "match": len(scores) - review_index,
        }
    recalls = [
        confusion[label][label] / len(scores_by_label[label]) for label in LABELS
    ]
    correct = sum(confusion[label][label] for label in LABELS)
    total = sum(len(scores) for scores in scores_by_label.values())
    critical_errors = confusion["mismatch"]["match"] + confusion["match"]["mismatch"]
    return sum(recalls) / len(recalls), critical_errors, correct / total


def choose_thresholds(
    cases: Sequence[ScoredCase],
    default_reject: float = 0.45,
    default_review: float = 0.62,
) -> dict[str, float] | None:
    """Choose deterministic thresholds from calibration cases only.

    Macro recall is the primary objective.  Cross-extreme mistakes are the
    first tie-breaker; closeness to the conservative defaults is the final
    stability tie-breaker.
    """

    calibration = [case for case in cases if case.split == "calibration"]
    if any(not any(case.label == label for case in calibration) for label in LABELS):
        return None

    scores_by_label = {
        label: sorted(case.score for case in calibration if case.label == label)
        for label in LABELS
    }
    best: tuple[tuple[float, int, float, float, float, float], float, float] | None = (
        None
    )
    candidates = _threshold_candidates(calibration)
    for reject_below in candidates:
        for review_below in candidates:
            if reject_below >= review_below:
                continue
            macro_recall, critical_errors, accuracy = _threshold_rank(
                scores_by_label, reject_below, review_below
            )
            distance = abs(reject_below - default_reject) + abs(
                review_below - default_review
            )
            rank = (
                macro_recall,
                -critical_errors,
                accuracy,
                -distance,
                -reject_below,
                -review_below,
            )
            if best is None or rank > best[0]:
                best = (rank, reject_below, review_below)
    if best is None:
        return None
    return {
        "reject_below": round(best[1], 6),
        "review_below": round(best[2], 6),
    }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 4)


def _distribution(cases: Sequence[ScoredCase]) -> dict[str, Any]:
    values = [case.score for case in cases]
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "p25": _percentile(values, 0.25),
        "median": round(statistics.median(values), 4) if values else None,
        "p75": _percentile(values, 0.75),
        "max": max(values) if values else None,
    }


def _distributions(cases: Sequence[ScoredCase]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for split in ("all", *SPLITS):
        split_cases = (
            list(cases)
            if split == "all"
            else [case for case in cases if case.split == split]
        )
        result[split] = {
            label: _distribution([case for case in split_cases if case.label == label])
            for label in LABELS
        }
    return result


def _readiness_reasons(
    benchmark: dict[str, Any], cases: Sequence[ScoredCase], thresholds: Any
) -> list[str]:
    reasons: list[str] = []
    if benchmark["source_kind"] != "real":
        reasons.append("source_kind is not real")
    if thresholds is None:
        reasons.append("calibration split must contain all three labels")
    for split, minimums in READY_MINIMUMS.items():
        for label, minimum in minimums.items():
            count = sum(case.split == split and case.label == label for case in cases)
            if count < minimum:
                reasons.append(
                    f"{split}.{label} has {count} cases; requires at least {minimum}"
                )
    if thresholds is not None:
        for split in SPLITS:
            metrics = _metrics(
                [case for case in cases if case.split == split],
                thresholds["reject_below"],
                thresholds["review_below"],
            )
            macro_recall = metrics["macro_recall"]
            if (
                macro_recall is None
                or macro_recall < READY_QUALITY["minimum_macro_recall"]
            ):
                reasons.append(
                    f"{split}.macro_recall is {macro_recall}; requires at least "
                    f"{READY_QUALITY['minimum_macro_recall']}"
                )
            if (
                split == "validation"
                and metrics["critical_errors"]
                > READY_QUALITY["maximum_validation_critical_errors"]
            ):
                reasons.append(
                    f"validation.critical_errors is {metrics['critical_errors']}; "
                    "requires 0"
                )
    return reasons


def _dataset_fingerprint(benchmark: dict[str, Any], manifest_path: str | Path) -> str:
    digest = hashlib.sha256(
        json.dumps(
            benchmark, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    root = Path(manifest_path)
    asset_values = {
        case[key]
        for case in benchmark["cases"]
        for key in ("reference_path", "candidate_path")
    }
    for asset_value in sorted(asset_values):
        asset_path = _resolve_asset(root, asset_value)
        try:
            content = asset_path.read_bytes()
        except OSError as exc:
            raise BenchmarkError(f"Cannot read benchmark asset: {asset_path}") from exc
        digest.update(asset_value.replace("\\", "/").encode("utf-8"))
        digest.update(hashlib.sha256(content).digest())
    return f"sha256:{digest.hexdigest()}"


def calibrate_identity_benchmark(
    benchmark: dict[str, Any], manifest_path: str | Path
) -> dict[str, Any]:
    """Score a manifest and return a reproducible calibration report."""

    validate_benchmark_document(benchmark)
    scored = score_benchmark(benchmark, manifest_path)
    defaults = IdentityConsistencyPlugin()
    thresholds = choose_thresholds(
        scored,
        default_reject=defaults.default_reject_below,
        default_review=defaults.default_review_below,
    )
    reasons = _readiness_reasons(benchmark, scored, thresholds)
    report: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "benchmark_id": benchmark["benchmark_id"],
        "source_kind": benchmark["source_kind"],
        "dataset_fingerprint": _dataset_fingerprint(benchmark, manifest_path),
        "status": "ready" if not reasons else "provisional",
        "status_reasons": reasons,
        "method": "identity_similarity.v1",
        "threshold_policy": {
            "below_reject": "mismatch",
            "below_review": "ambiguous",
            "otherwise": "match",
        },
        "current_defaults": {
            "reject_below": defaults.default_reject_below,
            "review_below": defaults.default_review_below,
        },
        "recommended_thresholds": thresholds,
        "readiness_minimums": READY_MINIMUMS,
        "readiness_quality": READY_QUALITY,
        "score_distributions": _distributions(scored),
        "metrics": None,
        "cases": [asdict(case) for case in scored],
        "canon_updated": False,
    }
    if thresholds is not None:
        report["metrics"] = {
            split: _metrics(
                [case for case in scored if case.split == split],
                thresholds["reject_below"],
                thresholds["review_below"],
            )
            for split in SPLITS
        }
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate identity thresholds from a labelled image benchmark."
    )
    parser.add_argument("manifest", type=Path, help="Benchmark JSON manifest")
    parser.add_argument(
        "--output",
        type=Path,
        help="Report path (default: <manifest>.calibration.json)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output or args.manifest.with_suffix(".calibration.json")
    try:
        benchmark = load_benchmark(args.manifest)
        report = calibrate_identity_benchmark(benchmark, args.manifest)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except BenchmarkError as exc:
        print(f"Calibration failed: {exc}", file=sys.stderr)
        return 2

    thresholds = report["recommended_thresholds"]
    print(f"Identity calibration: {report['status'].upper()}")
    print(f"Cases: {len(report['cases'])}")
    if thresholds is None:
        print("Recommended thresholds: unavailable")
    else:
        print(
            "Recommended thresholds: "
            f"reject_below={thresholds['reject_below']}, "
            f"review_below={thresholds['review_below']}"
        )
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
