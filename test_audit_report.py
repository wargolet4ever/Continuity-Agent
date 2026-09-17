"""Report generation is portable, verifiable, and honest about evidence."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from analyzer import ContinuityAnalyzer
from audit_report import (
    build_audit_report,
    render_markdown,
    validate_audit_report,
    write_audit_report_bundle,
)
from canon_loader import CanonStore
from contracts import ContractError
from orchestrator import EVIDENCE_NOTE, EVIDENCE_USER_REPORTED, Trace

ROOT = Path(__file__).resolve().parent


class AuditReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        analyzer = ContinuityAnalyzer(CanonStore(ROOT / "canon.json"))
        cls.result = analyzer.audit(
            "19",
            "本地规则（无需 API）",
            "检查小型显示屏",
            ["small_screen"],
        )

    def report(self):
        trace = Trace()
        trace.record("解析", "local", 4, source="canon")
        trace.record("检查", "local", 9, source="本地规则")
        return build_audit_report(
            self.result,
            evidence_source=EVIDENCE_USER_REPORTED,
            evidence_note=EVIDENCE_NOTE[EVIDENCE_USER_REPORTED],
            trace=trace,
            source_filename=r"C:\private\S19_take03.mp4",
            generated_at="2026-09-17T12:00:00+00:00",
        )

    def test_report_envelope_matches_validated_result(self):
        report = self.report()
        self.assertIs(validate_audit_report(report), report)
        self.assertEqual(report["summary"]["decision"], "LOCAL FIX")
        self.assertEqual(report["summary"]["issue_count"], 1)
        self.assertEqual(report["summary"]["severity_counts"]["local_fix"], 1)
        self.assertEqual(report["source"]["filename"], "S19_take03.mp4")
        self.assertNotIn("C:\\private", json.dumps(report, ensure_ascii=False))

    def test_markdown_keeps_evidence_limit_and_action(self):
        text = render_markdown(self.report())
        self.assertIn("USER-REPORTED RULE TRIAGE", text)
        self.assertIn("系统没有看过任何画面", text)
        self.assertIn("LOC-B03", text)
        self.assertIn("局部擦除小屏", text)
        self.assertIn("not a calibrated film-quality score", text)

    def test_bundle_contains_json_markdown_and_verified_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_audit_report_bundle(self.report(), directory)
            with zipfile.ZipFile(path) as bundle:
                self.assertEqual(
                    set(bundle.namelist()),
                    {"report.json", "report.md", "manifest.json"},
                )
                payload = json.loads(bundle.read("report.json"))
                manifest = json.loads(bundle.read("manifest.json"))
                self.assertEqual(payload["summary"]["shot_id"], "19")
                self.assertFalse(manifest["source_media_included"])
                for name in ("report.json", "report.md"):
                    self.assertEqual(
                        hashlib.sha256(bundle.read(name)).hexdigest(),
                        manifest["files"][name]["sha256"],
                    )

    def test_machine_readable_report_schema_tracks_runtime_constants(self):
        schema = json.loads(
            (ROOT / "schemas" / "audit-report-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["report_version"]["const"], "1.0")
        self.assertEqual(
            set(
                schema["properties"]["provenance"]["properties"]["evidence_source"][
                    "enum"
                ]
            ),
            {
                "USER-REPORTED RULE TRIAGE",
                "RULE + PIXEL CHECK",
                "VISUAL AUDIT",
                "VIDEO FRAME + RULE CHECK",
                "VIDEO VISUAL AUDIT",
            },
        )

    def test_tampered_summary_or_payload_digest_is_rejected(self):
        report = self.report()
        report["summary"]["issue_count"] = 99
        with self.assertRaisesRegex(ContractError, "summary"):
            validate_audit_report(report)

        report = self.report()
        report["audit_result"]["score"] = 1
        with self.assertRaisesRegex(ContractError, "summary|payload_sha256"):
            validate_audit_report(report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
