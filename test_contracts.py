"""Regression tests for the frozen Continuity Agent v1 data contracts."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from analyzer import ContinuityAnalyzer
from canon_loader import CanonStore
from contracts import (
    CONTRACT_VERSION,
    DECISIONS,
    ContractError,
    validate_audit_request,
    validate_audit_result,
    validate_canon_document,
)
from creator import build_canon_draft

ROOT = Path(__file__).resolve().parent


class CanonContractTests(unittest.TestCase):
    def test_current_and_historical_canons_validate(self):
        for filename in ("canon.json", "canon_history_2026-09-12.json"):
            with self.subTest(filename=filename):
                payload = json.loads((ROOT / filename).read_text(encoding="utf-8"))
                self.assertIs(validate_canon_document(payload), payload)

    def test_create_mode_draft_uses_the_same_contract(self):
        story = {
            "title": "Contract Test",
            "logline": "A stable test story.",
            "conflict": "The scene changes unexpectedly.",
            "protagonist": "A technician",
            "tone": "cold science fiction",
        }
        shots = [
            {
                "shot_id": "1",
                "location": "lab",
                "duration_sec": 5,
                "camera": "WIDE-fixed",
                "identity_critical": True,
                "action": "enters",
                "visual": "locked console",
                "purpose": "establish",
                "status": "AWAITING_EXTERNAL_GENERATION",
                "task_id": None,
                "video_url": None,
            },
            {
                "shot_id": "2",
                "location": "corridor",
                "duration_sec": 4,
                "camera": "MEDIUM-fixed",
                "identity_critical": False,
                "action": "leaves",
                "visual": "dark corridor",
                "purpose": "turn",
                "status": "AWAITING_EXTERNAL_GENERATION",
                "task_id": None,
                "video_url": None,
            },
        ]
        draft = build_canon_draft(story, shots)
        self.assertEqual(draft["meta"]["contract_version"], CONTRACT_VERSION)
        self.assertIs(validate_canon_document(draft), draft)

    def test_conflicting_rule_definitions_are_rejected(self):
        payload = json.loads((ROOT / "canon.json").read_text(encoding="utf-8"))
        broken = copy.deepcopy(payload)
        broken["shots"]["19"].setdefault("rules", []).append(
            {
                "id": "LOC-B03",
                "text": "Conflicts with the registered definition",
                "severity": "regenerate",
            }
        )
        with self.assertRaisesRegex(ContractError, "conflicting definitions"):
            validate_canon_document(broken)


class AuditRequestContractTests(unittest.TestCase):
    def test_video_and_image_sequence_request_validates(self):
        request = {
            "contract_version": CONTRACT_VERSION,
            "job_id": "job-001",
            "canon_path": "canon.json",
            "shots": [
                {
                    "shot_id": "19",
                    "media": {"kind": "video", "paths": ["shot19.mp4"]},
                },
                {
                    "shot_id": "21",
                    "media": {
                        "kind": "image_sequence",
                        "paths": ["21_001.png", "21_002.png"],
                        "timestamps_s": [0.0, 1.5],
                    },
                },
            ],
            "options": {"sample_count": 5},
        }
        self.assertIs(validate_audit_request(request), request)

    def test_duplicate_shot_ids_are_rejected(self):
        request = {
            "contract_version": CONTRACT_VERSION,
            "job_id": "job-duplicate",
            "canon_path": "canon.json",
            "shots": [
                {"shot_id": "19", "media": {"kind": "video", "paths": ["a.mp4"]}},
                {"shot_id": "19", "media": {"kind": "video", "paths": ["b.mp4"]}},
            ],
        }
        with self.assertRaisesRegex(ContractError, "duplicate shot id"):
            validate_audit_request(request)

    def test_unknown_contract_version_is_rejected(self):
        request = {
            "contract_version": "2.0",
            "job_id": "job-newer",
            "canon_path": "canon.json",
            "shots": [
                {"shot_id": "19", "media": {"kind": "video", "paths": ["a.mp4"]}}
            ],
        }
        with self.assertRaisesRegex(ContractError, "unsupported version"):
            validate_audit_request(request)


class AuditResultContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = CanonStore(ROOT / "canon.json")
        cls.analyzer = ContinuityAnalyzer(cls.store)

    def test_live_analyzer_result_is_versioned_and_valid(self):
        result = self.analyzer.audit(
            "19",
            "本地规则（无需 API）",
            "检查小型显示屏",
            ["small_screen"],
        )
        self.assertEqual(result["contract_version"], CONTRACT_VERSION)
        self.assertEqual(result["issues"][0]["evidence_ids"], [])
        self.assertEqual(result["evidence_assets"], [])
        self.assertIs(validate_audit_result(result), result)
        json.dumps(result, ensure_ascii=False)

    def test_issue_cannot_reference_missing_evidence(self):
        result = self.analyzer.audit(
            "19",
            "本地规则（无需 API）",
            "检查小型显示屏",
            ["small_screen"],
        )
        broken = copy.deepcopy(result)
        broken["issues"][0]["evidence_ids"] = ["missing-frame"]
        with self.assertRaisesRegex(ContractError, "unknown evidence asset"):
            validate_audit_result(broken)

    def test_machine_readable_schema_matches_runtime_constants(self):
        schema = json.loads(
            (ROOT / "schemas" / "continuity-v1.schema.json").read_text(encoding="utf-8")
        )
        definitions = schema["$defs"]
        self.assertEqual(
            definitions["AuditRequest"]["properties"]["contract_version"]["const"],
            CONTRACT_VERSION,
        )
        self.assertEqual(
            set(definitions["AuditResult"]["properties"]["decision"]["enum"]),
            set(DECISIONS),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
