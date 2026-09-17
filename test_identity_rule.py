"""Calibration and integration tests for the CPU-only identity rule."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from analyzer import ContinuityAnalyzer
from canon_loader import CanonStore
from contracts import ContractError, validate_canon_document
from rules import identity_similarity

ROOT = Path(__file__).resolve().parent


def reference_portrait() -> Image.Image:
    image = Image.new("RGB", (128, 128), (20, 30, 60))
    draw = ImageDraw.Draw(image)
    draw.ellipse((24, 12, 104, 92), fill=(210, 150, 120))
    draw.rectangle((38, 84, 90, 124), fill=(40, 80, 150))
    draw.ellipse((45, 42, 55, 52), fill="black")
    draw.ellipse((75, 42, 85, 52), fill="black")
    draw.arc((48, 50, 82, 78), 0, 180, fill="black", width=3)
    return image


def ambiguous_portrait() -> Image.Image:
    image = Image.new("RGB", (128, 128), (20, 30, 60))
    draw = ImageDraw.Draw(image)
    for x in range(0, 128, 16):
        draw.rectangle((x, 0, x + 7, 127), fill=(30, 210, 80))
    draw.polygon([(10, 110), (64, 10), (118, 110)], fill=(230, 230, 40))
    return image


class IdentityMetricTests(unittest.TestCase):
    def test_same_crop_scores_one(self):
        image = reference_portrait()
        similarity = identity_similarity(image, image)
        self.assertEqual(similarity.score, 1.0)

    def test_small_brightness_change_remains_stable(self):
        image = reference_portrait()
        brighter = ImageEnhance.Brightness(image).enhance(1.05)
        similarity = identity_similarity(image, brighter)
        self.assertGreater(similarity.score, 0.95)

    def test_invalid_crop_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "normalized"):
            identity_similarity(
                reference_portrait(),
                reference_portrait(),
                reference_crop=[0.8, 0.1, 0.2, 0.9],
            )

    def test_edge_crop_on_tiny_image_stays_inside_canvas(self):
        image = Image.new("RGB", (2, 2), "white")
        similarity = identity_similarity(
            image,
            image,
            reference_crop=[0.9, 0.9, 1.0, 1.0],
            candidate_crop=[0.9, 0.9, 1.0, 1.0],
        )
        self.assertEqual(similarity.score, 1.0)

    def test_canon_contract_rejects_invalid_identity_crop(self):
        data = copy.deepcopy(CanonStore(ROOT / "canon.json").data)
        data["shots"]["19"]["identity_crops"] = {"daniel": [0.8, 0.1, 0.2, 0.9]}
        with self.assertRaisesRegex(ContractError, "left < right"):
            validate_canon_document(data)


class IdentityPluginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        reference_portrait().save(self.root / "daniel_reference.png")
        data = copy.deepcopy(CanonStore(ROOT / "canon.json").data)
        data["characters"]["daniel"]["identity_reference"] = {
            "asset_path": "daniel_reference.png",
            "crop": [0.0, 0.0, 1.0, 1.0],
            "rule_id": "CHAR-D01",
            "reject_below": 0.45,
            "review_below": 0.62,
        }
        data["shots"]["19"]["identity_crops"] = {"daniel": [0.0, 0.0, 1.0, 1.0]}
        path = self.root / "canon.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.analyzer = ContinuityAnalyzer(CanonStore(path))

    def tearDown(self):
        self.directory.cleanup()

    def audit(self, image, video_frames=None):
        return self.analyzer.audit(
            "19",
            "本地规则（无需 API）",
            "",
            ["manual_pass_confirmed"],
            current_image=image,
            video_frames=video_frames,
            video_timestamps=[0.0, 1.0, 2.0] if video_frames else None,
        )

    def test_matching_reference_passes(self):
        result = self.audit(reference_portrait())
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["issues"], [])

    def test_obvious_mismatch_requires_regeneration(self):
        result = self.audit(Image.new("RGB", (128, 128), "black"))
        issue = result["issues"][0]
        self.assertEqual(result["decision"], "REGENERATE")
        self.assertEqual(issue["rule_id"], "CHAR-D01")
        self.assertEqual(issue["severity"], "regenerate")
        self.assertGreaterEqual(issue["confidence"], 0.9)

    def test_ambiguous_score_routes_to_human_review(self):
        result = self.audit(ambiguous_portrait())
        issue = result["issues"][0]
        self.assertEqual(result["decision"], "HUMAN REVIEW")
        self.assertEqual(issue["severity"], "human_review")
        self.assertTrue(issue["requires_confirmation"])

    def test_video_uses_median_frame_score(self):
        matching = reference_portrait()
        mismatch = Image.new("RGB", (128, 128), "black")
        result = self.audit(
            matching,
            video_frames=[matching, matching, mismatch],
        )
        self.assertEqual(result["decision"], "PASS")

    def test_configured_but_missing_reference_routes_to_review(self):
        (self.root / "daniel_reference.png").unlink()
        result = self.audit(reference_portrait())
        self.assertEqual(result["decision"], "HUMAN REVIEW")
        self.assertIn("基准图不可读取", result["issues"][0]["evidence"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
