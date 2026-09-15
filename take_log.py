from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import Counter
from threading import RLock
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OBSERVABILITY_FIELDS = [
    "run_mode",
    "evidence_source",
    "duration_ms",
    "retries",
    "failure_reason",
    "model_source",
]

FIELDS = [
    "timestamp_utc",
    "shot_id",
    "attempt_id",
    "scene",
    "model",
    "routing_tier",
    "prompt",
    "reference_note",
    "output_filename",
    "decision",
    "score",
    "adoption",
    "rejection_reason",
    "notes",
    "canon_version",
    "audit_mode",
    "api_reviewed",
    "needs_human_review",
    *OBSERVABILITY_FIELDS,
    "issues_json",
]

LOG_LOCK = RLock()


class TakeLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8-sig") as handle:
                csv.DictWriter(handle, fieldnames=FIELDS).writeheader()
        else:
            with LOG_LOCK, self.path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                old_fields = reader.fieldnames or []
                rows = list(reader)
            if old_fields != FIELDS:
                # Preserve original bytes before an atomic, additive schema migration.
                backup = self.path.with_suffix(".csv.pre-v1.1")
                if not backup.exists():
                    backup.write_bytes(self.path.read_bytes())
                fields = FIELDS + [f for f in old_fields if f not in FIELDS]
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8-sig",
                    newline="",
                    dir=self.path.parent,
                    delete=False,
                ) as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                    temporary = handle.name
                os.replace(temporary, self.path)

    def append(
        self,
        result: dict[str, Any],
        model: str,
        prompt: str,
        reference_note: str,
        output_filename: str,
        adoption: str,
        rejection_reason: str,
        notes: str,
        run_mode: str = "AUDIT",
        evidence_source: str = "",
        duration_ms: int | str = "",
        retries: int | str = 0,
        failure_reason: str = "",
        model_source: str = "",
    ) -> None:
        row = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "shot_id": result.get("shot_id", ""),
            "attempt_id": result.get("attempt_id", ""),
            "scene": result.get("location", ""),
            "model": model,
            "routing_tier": result.get("routing_tier", ""),
            "prompt": result.get("input_prompt", prompt),
            "reference_note": reference_note,
            "output_filename": output_filename,
            "decision": result.get("decision", ""),
            "score": result.get("score", ""),
            "adoption": adoption,
            "rejection_reason": rejection_reason,
            "notes": notes,
            "canon_version": result.get("canon_version", ""),
            "audit_mode": result.get("mode", ""),
            "api_reviewed": result.get("api_reviewed", False),
            "needs_human_review": result.get("needs_human_review", False),
            "run_mode": run_mode,
            "evidence_source": evidence_source,
            "duration_ms": duration_ms,
            "retries": retries,
            "failure_reason": failure_reason or result.get("api_error", "") or "",
            "model_source": model_source,
            "issues_json": json.dumps(result.get("issues", []), ensure_ascii=False),
        }
        with LOG_LOCK:
            with self.path.open(newline="", encoding="utf-8-sig") as handle:
                fields = next(csv.reader(handle))
            with self.path.open("a", newline="", encoding="utf-8-sig") as handle:
                csv.DictWriter(handle, fieldnames=fields).writerow(row)

    def stats(self) -> tuple[str, list[list[Any]]]:
        with LOG_LOCK, self.path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        accepted = sum(r["adoption"] == "采纳" for r in rows)
        rejected = [r for r in rows if r["adoption"] == "不采纳"]
        decided = accepted + len(rejected)
        rate = f"{accepted / decided:.1%}" if decided else "—"
        reasons = Counter(r["rejection_reason"] or "未填写" for r in rejected)
        return (
            f"共 {len(rows)} 条记录 · 已决策 {decided} · 采纳 {accepted} · 未采纳 {len(rejected)} · 待定 {len(rows) - decided} · 采纳率 {rate}（分母仅已决策记录）",
            [[k, v] for k, v in reasons.most_common()],
        )

    def rows(self, limit: int = 100) -> list[list[str]]:
        with self.path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        rows = rows[-limit:][::-1]
        return [[row.get(field, "") for field in FIELDS[:-1]] for row in rows]
