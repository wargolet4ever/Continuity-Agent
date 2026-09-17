"""Portable, evidence-aware reports for one Continuity Agent audit.

The UI is deliberately concise.  This module preserves the complete result and
execution provenance in a deterministic JSON document, renders a human-readable
Markdown companion, and packages both with an integrity manifest.  Source media
is never copied into the report bundle.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import ContractError, validate_audit_result

REPORT_VERSION = "1.0"
EVIDENCE_SOURCES = frozenset(
    {
        "USER-REPORTED RULE TRIAGE",
        "RULE + PIXEL CHECK",
        "VISUAL AUDIT",
        "VIDEO FRAME + RULE CHECK",
        "VIDEO VISUAL AUDIT",
    }
)
SEVERITIES = ("regenerate", "local_fix", "human_review")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _clean_source_name(value: Any) -> str:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return ""
    # A report may record the current user's filename, but never the server path.
    return Path(str(value).replace("\\", "/")).name


def _trace_payload(trace: Any) -> dict[str, Any]:
    if hasattr(trace, "as_dict"):
        payload = trace.as_dict()
    elif isinstance(trace, dict):
        payload = copy.deepcopy(trace)
    else:
        raise TypeError("trace must be a Trace instance or an object")
    if not isinstance(payload.get("total_ms"), int) or payload["total_ms"] < 0:
        raise ValueError("trace.total_ms must be a non-negative integer")
    if not isinstance(payload.get("steps"), list):
        raise TypeError("trace.steps must be an array")
    required = {
        "index",
        "name",
        "kind",
        "duration_ms",
        "ok",
        "retries",
        "source",
        "detail",
    }
    for index, step in enumerate(payload["steps"]):
        if not isinstance(step, dict):
            raise TypeError(f"trace.steps[{index}] must be an object")
        missing = required - step.keys()
        if missing:
            raise ValueError(
                f"trace.steps[{index}] is missing: {', '.join(sorted(missing))}"
            )
        if (
            isinstance(step["index"], bool)
            or not isinstance(step["index"], int)
            or step["index"] < 1
        ):
            raise ValueError(f"trace.steps[{index}].index must be a positive integer")
        if not isinstance(step["name"], str) or not step["name"].strip():
            raise ValueError(f"trace.steps[{index}].name must be a non-empty string")
        if step["kind"] not in {"local", "model"}:
            raise ValueError(f"trace.steps[{index}].kind must be local or model")
        for field in ("duration_ms", "retries"):
            value = step[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"trace.steps[{index}].{field} must be a non-negative integer"
                )
        if not isinstance(step["ok"], bool):
            raise TypeError(f"trace.steps[{index}].ok must be a boolean")
        for field in ("source", "detail"):
            if not isinstance(step[field], str):
                raise TypeError(f"trace.steps[{index}].{field} must be a string")
    return payload


def build_audit_report(
    result: dict[str, Any],
    *,
    evidence_source: str,
    evidence_note: str,
    trace: Any,
    source_filename: Any = "",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build and validate a portable report without mutating the audit result."""

    result_copy = copy.deepcopy(result)
    validate_audit_result(result_copy)
    if evidence_source not in EVIDENCE_SOURCES:
        raise ValueError(f"unsupported evidence source: {evidence_source!r}")
    if not isinstance(evidence_note, str) or not evidence_note.strip():
        raise ValueError("evidence_note must be a non-empty string")

    created = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    issues = result_copy.get("issues", [])
    severity_counts = {
        severity: sum(item.get("severity") == severity for item in issues)
        for severity in SEVERITIES
    }
    report = {
        "report_version": REPORT_VERSION,
        "generated_at": created,
        "source": {
            "filename": _clean_source_name(source_filename),
            "media_kind": result_copy["media_kind"],
        },
        "provenance": {
            "evidence_source": evidence_source,
            "evidence_note": evidence_note.strip(),
            "api_reviewed": result_copy["api_reviewed"],
            "canon_version": result_copy["canon_version"],
        },
        "summary": {
            "shot_id": result_copy["shot_id"],
            "decision": result_copy["decision"],
            "score": result_copy["score"],
            "issue_count": len(issues),
            "severity_counts": severity_counts,
            "needs_human_review": result_copy["needs_human_review"],
        },
        "audit_result": result_copy,
        "execution_trace": _trace_payload(trace),
    }
    report["integrity"] = {
        "algorithm": "sha256",
        "payload_sha256": _sha256(_canonical_bytes(report)),
    }
    return validate_audit_report(report)


def validate_audit_report(report: Any) -> dict[str, Any]:
    """Validate the report envelope and its cross-field integrity assertions."""

    if not isinstance(report, dict):
        raise ContractError("report", "must be an object")
    required = {
        "report_version",
        "generated_at",
        "source",
        "provenance",
        "summary",
        "audit_result",
        "execution_trace",
        "integrity",
    }
    missing = sorted(required - report.keys())
    if missing:
        raise ContractError("report", f"missing required fields: {', '.join(missing)}")
    if report["report_version"] != REPORT_VERSION:
        raise ContractError("report.report_version", f"must be {REPORT_VERSION!r}")
    try:
        generated_at = datetime.fromisoformat(
            str(report["generated_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ContractError(
            "report.generated_at", "must be an ISO-8601 timestamp"
        ) from exc
    if generated_at.tzinfo is None:
        raise ContractError("report.generated_at", "must include a timezone")

    source = report["source"]
    provenance = report["provenance"]
    summary = report["summary"]
    trace = report["execution_trace"]
    integrity = report["integrity"]
    for name, value in (
        ("source", source),
        ("provenance", provenance),
        ("summary", summary),
        ("execution_trace", trace),
        ("integrity", integrity),
    ):
        if not isinstance(value, dict):
            raise ContractError(f"report.{name}", "must be an object")

    result = validate_audit_result(report["audit_result"])
    if not isinstance(source.get("filename"), str):
        raise ContractError("report.source.filename", "must be a string")
    evidence_source = provenance.get("evidence_source")
    if evidence_source not in EVIDENCE_SOURCES:
        raise ContractError(
            "report.provenance.evidence_source",
            f"must be one of {sorted(EVIDENCE_SOURCES)}",
        )
    if (
        not isinstance(provenance.get("evidence_note"), str)
        or not provenance["evidence_note"].strip()
    ):
        raise ContractError(
            "report.provenance.evidence_note", "must be a non-empty string"
        )
    if provenance.get("api_reviewed") is not result["api_reviewed"]:
        raise ContractError(
            "report.provenance.api_reviewed", "does not match audit_result"
        )
    if provenance.get("canon_version") != result["canon_version"]:
        raise ContractError(
            "report.provenance.canon_version", "does not match audit_result"
        )
    if source.get("media_kind") != result["media_kind"]:
        raise ContractError("report.source.media_kind", "does not match audit_result")

    expected_counts = {
        severity: sum(item.get("severity") == severity for item in result["issues"])
        for severity in SEVERITIES
    }
    expected_summary = {
        "shot_id": result["shot_id"],
        "decision": result["decision"],
        "score": result["score"],
        "issue_count": len(result["issues"]),
        "severity_counts": expected_counts,
        "needs_human_review": result["needs_human_review"],
    }
    if summary != expected_summary:
        raise ContractError("report.summary", "does not match audit_result")
    _trace_payload(trace)

    if integrity.get("algorithm") != "sha256":
        raise ContractError("report.integrity.algorithm", "must be 'sha256'")
    digest = integrity.get("payload_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ContractError(
            "report.integrity.payload_sha256", "must be a SHA-256 hex digest"
        )
    unsigned = copy.deepcopy(report)
    unsigned.pop("integrity")
    if digest != _sha256(_canonical_bytes(unsigned)):
        raise ContractError(
            "report.integrity.payload_sha256", "does not match report payload"
        )
    return report


def _md_cell(value: Any) -> str:
    return (
        str(value if value is not None else "—")
        .replace("|", "\\|")
        .replace("\n", "<br>")
    )


def render_markdown(report: dict[str, Any]) -> str:
    """Render a report that is useful to an editor without hiding uncertainty."""

    validate_audit_report(report)
    summary = report["summary"]
    result = report["audit_result"]
    provenance = report["provenance"]
    decision_labels = {
        "PASS": "Looks fine within checked scope",
        "LOCAL FIX": "Fix locally; keep the take",
        "REGENERATE": "Regenerate this take",
        "HUMAN REVIEW": "Human review required",
        "SKIP": "Visual audit skipped",
    }
    lines = [
        f"# Continuity audit · Shot {summary['shot_id']}",
        "",
        f"**Decision:** `{summary['decision']}` — {decision_labels[summary['decision']]}",
        f"**Rule-triage score:** {summary['score'] if summary['score'] is not None else '—'}/100",
        f"**Evidence source:** `{provenance['evidence_source']}`",
        f"**Canon:** `{provenance['canon_version']}`",
        f"**Generated:** {report['generated_at']}",
    ]
    if report["source"].get("filename"):
        lines.append(f"**Source file:** `{report['source']['filename']}`")
    lines += [
        "",
        f"> {provenance['evidence_note']}",
        "",
        "The score is a rule-triage score, not a calibrated film-quality score.",
        "",
        "## Findings",
        "",
    ]
    if not result["issues"]:
        lines.append("No issue was reported within the observable scope of this run.")
    else:
        lines += [
            "| Rule | Severity | Confidence | Evidence | Minimum fix |",
            "|---|---|---:|---|---|",
        ]
        for issue in result["issues"]:
            lines.append(
                "| "
                + " | ".join(
                    _md_cell(value)
                    for value in (
                        issue["rule_id"],
                        issue["severity"],
                        f"{issue['confidence']:.2f}",
                        issue["evidence"],
                        issue["minimal_fix"],
                    )
                )
                + " |"
            )

    lines += [
        "",
        "## Scope and provenance",
        "",
        f"- Media kind: `{result['media_kind']}`",
        f"- Loaded rules: {result['rule_count']} (loaded does not mean visually verified)",
        f"- Multimodal review completed: {'yes' if result['api_reviewed'] else 'no'}",
        f"- Human review needed: {'yes' if result['needs_human_review'] else 'no'}",
        f"- API retries: {result['api_retries']}",
    ]
    if result["video_timestamps_s"]:
        lines.append(
            "- Sampled video timestamps: "
            + ", ".join(f"{value:.2f}s" for value in result["video_timestamps_s"])
        )
    if result.get("api_error"):
        lines.append(f"- API degradation: {_md_cell(result['api_error'])}")

    lines += [
        "",
        "## Execution trace",
        "",
        "| # | Step | Executor | Source | Time | Result | Retries |",
        "|---:|---|---|---|---:|---|---:|",
    ]
    for step in report["execution_trace"]["steps"]:
        lines.append(
            "| "
            + " | ".join(
                _md_cell(value)
                for value in (
                    step.get("index"),
                    step.get("name"),
                    step.get("kind"),
                    step.get("source") or "—",
                    f"{int(step.get('duration_ms', 0))} ms",
                    "OK" if step.get("ok") else "FAILED",
                    step.get("retries", 0),
                )
            )
            + " |"
        )

    if result.get("revised_prompt"):
        prompt = str(result["revised_prompt"]).replace("```", "` ` `")
        lines += ["", "## Revised generation prompt", "", "```text", prompt, "```"]
    lines += [
        "",
        "## Interpretation limits",
        "",
        "- PASS means no supported violation was found in the checked evidence; it is not full-film approval.",
        "- Sampled video frames cannot prove every event between timestamps or assess audio.",
        "- Source media is referenced by name only and is not embedded in this bundle.",
        "",
    ]
    return "\n".join(lines)


def write_audit_report_bundle(
    report: dict[str, Any], directory: str | Path | None = None
) -> str:
    """Write report.json, report.md, and a checksum manifest to one ZIP."""

    validate_audit_report(report)
    root = (
        Path(directory)
        if directory
        else Path(tempfile.mkdtemp(prefix="continuity_report_"))
    )
    root.mkdir(parents=True, exist_ok=True)
    json_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    markdown_bytes = render_markdown(report).encode("utf-8")
    files = {"report.json": json_bytes, "report.md": markdown_bytes}
    manifest = {
        "report_version": REPORT_VERSION,
        "files": {
            name: {"sha256": _sha256(payload), "bytes": len(payload)}
            for name, payload in files.items()
        },
        "source_media_included": False,
    }
    files["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    safe_shot = (
        re.sub(r"[^A-Za-z0-9_-]+", "_", str(report["summary"]["shot_id"])) or "unknown"
    )
    path = root / f"continuity-audit-shot-{safe_shot}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in files.items():
            bundle.writestr(name, payload)
    return str(path)
