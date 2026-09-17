"""Tests for config-driven CPU visual continuity rules."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageDraw

from analyzer import ContinuityAnalyzer
from canon_loader import CanonStore
from contracts import ContractError, validate_canon_document
from rules.visual import reference_similarity, temporal_structure_similarity

ROOT = Path(__file__).resolve().parent


def striped(horizontal: bool = False, offset: int = 0) -> Image.Image:
    image = Image.new("RGB", (128, 128), (20 + offset, 30 + offset, 40 + offset))
    draw = ImageDraw.Draw(image)
    for position in range(0, 128, 16):
        color = (130 + offset, 150 + offset, 170 + offset)
        if horizontal:
            draw.rectangle((0, position, 127, position + 7), fill=color)
        else:
            draw.rectangle((position, 0, position + 7, 127), fill=color)
    return image


class VisualMetricTests(unittest.TestCase):
    def test_reference_similarity_is_bounded_and_identical_is_one(self):
        image = striped()
        self.assertEqual(reference_similarity(image, image), 1.0)
        score = reference_similarity(image, striped(horizontal=True))
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_temporal_structure_ignores_global_brightness_shift(self):
        self.assertGreater(
            temporal_structure_similarity(striped(), striped(offset=40)), 0.98
        )

    def test_temporal_structure_detects_geometry_change(self):
        same = temporal_structure_similarity(striped(), striped(offset=20))
        changed = temporal_structure_similarity(striped(), striped(horizontal=True))
        self.assertGreater(same, changed)
        self.assertLess(changed, 0.9)


class VisualContractTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((ROOT / "canon.json").read_text(encoding="utf-8"))

    def test_unknown_rule_reference_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload["shots"]["16"]["visual_checks"] = [
            {
                "kind": "pixel_range",
                "rule_id": "NOT-A-RULE",
                "metric": "brightness",
                "maximum": 0.8,
            }
        ]
        with self.assertRaisesRegex(ContractError, "references unknown rule"):
            validate_canon_document(payload)

    def test_temporal_check_requires_explicit_crop(self):
        payload = copy.deepcopy(self.payload)
        payload["shots"]["16"]["visual_checks"] = [
            {
                "kind": "temporal_stability",
                "rule_id": "LOC-B06",
                "reject_below": 0.6,
                "review_below": 0.8,
            }
        ]
        with self.assertRaisesRegex(ContractError, "crop.*required"):
            validate_canon_document(payload)

    def test_similarity_threshold_order_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload["shots"]["16"]["visual_checks"] = [
            {
                "kind": "reference_similarity",
                "rule_id": "LOC-B06",
                "asset_path": "reference.png",
                "reject_below": 0.9,
                "review_below": 0.8,
            }
        ]
        with self.assertRaisesRegex(ContractError, "reject_below must be lower"):
            validate_canon_document(payload)


class VisualPluginTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((ROOT / "canon.json").read_text(encoding="utf-8"))

    @contextmanager
    def analyzer_for(
        self,
        shot_id: str,
        check: dict,
        reference: Image.Image | None = None,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = copy.deepcopy(self.payload)
            payload["shots"][shot_id]["visual_checks"] = [check]
            if reference is not None:
                reference.save(root / "reference.png")
            canon_path = root / "canon.json"
            canon_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            yield ContinuityAnalyzer(CanonStore(canon_path))

    def test_default_attempt_check_is_scoped_and_reports_red_candidate(self):
        store = CanonStore(ROOT / "canon.json")
        self.assertEqual(store.compile_rule_pack("16")["visual_checks"], [])
        self.assertEqual(
            store.compile_rule_pack("20")["visual_checks"][0]["rule_id"],
            "LOOP-05B",
        )
        result = ContinuityAnalyzer(store).audit(
            "20",
            "本地规则（无需 API）",
            "",
            [],
            current_image=Image.new("RGB", (96, 96), (200, 40, 40)),
        )
        issue = result["issues"][0]
        self.assertEqual(issue["rule_id"], "LOOP-05B")
        self.assertEqual(issue["confidence"], 0.76)
        self.assertTrue(issue["requires_confirmation"])

    def test_pixel_range_can_be_limited_to_explicit_crop(self):
        check = {
            "kind": "pixel_range",
            "rule_id": "LOC-B04",
            "metric": "red_ratio",
            "maximum": 0.1,
            "crop": [0.25, 0.25, 0.75, 0.75],
        }
        image = Image.new("RGB", (100, 100), (200, 30, 30))
        ImageDraw.Draw(image).rectangle((25, 25, 75, 75), fill=(20, 80, 120))
        with self.analyzer_for("16", check) as analyzer:
            result = analyzer.audit(
                "16", "本地规则（无需 API）", "", [], current_image=image
            )
        self.assertFalse(any(i["rule_id"] == "LOC-B04" for i in result["issues"]))

    def test_reference_match_passes_and_clear_mismatch_uses_rule_severity(self):
        check = {
            "kind": "reference_similarity",
            "rule_id": "LOC-B06",
            "asset_path": "reference.png",
            "reject_below": 0.75,
            "review_below": 0.9,
        }
        reference = striped()
        with self.analyzer_for("16", check, reference) as analyzer:
            matching = analyzer.audit(
                "16", "本地规则（无需 API）", "", [], current_image=reference
            )
            mismatch = analyzer.audit(
                "16",
                "本地规则（无需 API）",
                "",
                [],
                current_image=striped(horizontal=True),
            )
        self.assertFalse(any(i["rule_id"] == "LOC-B06" for i in matching["issues"]))
        issue = next(i for i in mismatch["issues"] if i["rule_id"] == "LOC-B06")
        self.assertEqual(issue["severity"], "regenerate")

    def test_missing_reference_routes_to_human_review(self):
        check = {
            "kind": "reference_similarity",
            "rule_id": "LOC-B06",
            "asset_path": "missing.png",
            "reject_below": 0.6,
            "review_below": 0.8,
        }
        with self.analyzer_for("16", check) as analyzer:
            result = analyzer.audit(
                "16", "本地规则（无需 API）", "", [], current_image=striped()
            )
        issue = next(i for i in result["issues"] if i["rule_id"] == "LOC-B06")
        self.assertEqual(issue["severity"], "human_review")
        self.assertTrue(issue["requires_confirmation"])

    def test_temporal_stability_flags_geometry_not_brightness(self):
        check = {
            "kind": "temporal_stability",
            "rule_id": "LOC-B06",
            "crop": [0.0, 0.0, 1.0, 1.0],
            "reject_below": 0.8,
            "review_below": 0.95,
        }
        with self.analyzer_for("16", check) as analyzer:
            brightness_only = analyzer.audit(
                "16",
                "本地规则（无需 API）",
                "",
                [],
                current_image=striped(),
                video_frames=[striped(), striped(offset=20), striped(offset=40)],
                video_timestamps=[0.0, 1.0, 2.0],
            )
            changed = analyzer.audit(
                "16",
                "本地规则（无需 API）",
                "",
                [],
                current_image=striped(),
                video_frames=[striped(), striped(horizontal=True)],
                video_timestamps=[0.0, 1.0],
            )
        self.assertFalse(
            any(i["rule_id"] == "LOC-B06" for i in brightness_only["issues"])
        )
        issue = next(i for i in changed["issues"] if i["rule_id"] == "LOC-B06")
        self.assertEqual(issue["severity"], "regenerate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
