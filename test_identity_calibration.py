"""Tests for the identity benchmark and threshold calibration module."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from identity_calibration import (
    READY_MINIMUMS,
    BenchmarkError,
    ScoredCase,
    calibrate_identity_benchmark,
    choose_thresholds,
    main,
    validate_benchmark_document,
)


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


class CalibrationFixture:
    def __init__(self, root: Path):
        self.root = root
        reference = reference_portrait()
        reference.save(root / "reference.png")
        ImageEnhance.Brightness(reference).enhance(1.05).save(root / "match.png")
        ambiguous_portrait().save(root / "ambiguous.png")
        Image.new("RGB", (128, 128), "black").save(root / "mismatch.png")

    def manifest(
        self,
        source_kind: str = "synthetic",
        counts: dict[str, dict[str, int]] | None = None,
    ) -> dict:
        counts = counts or {
            "calibration": {"mismatch": 2, "ambiguous": 2, "match": 2},
            "validation": {"mismatch": 1, "ambiguous": 1, "match": 1},
        }
        cases = []
        for split, labels in counts.items():
            for label, count in labels.items():
                for index in range(count):
                    candidate_path = f"{split}-{label}-{index}.png"
                    with Image.open(self.root / f"{label}.png") as candidate:
                        candidate.save(self.root / candidate_path)
                    cases.append(
                        {
                            "id": f"{split}-{label}-{index}",
                            "label": label,
                            "split": split,
                            "reference_path": "reference.png",
                            "candidate_path": candidate_path,
                        }
                    )
        return {
            "contract_version": "1.0",
            "benchmark_id": "fixture-identity-v1",
            "source_kind": source_kind,
            "cases": cases,
        }


class BenchmarkContractTests(unittest.TestCase):
    def test_duplicate_case_id_is_rejected(self):
        payload = {
            "contract_version": "1.0",
            "benchmark_id": "duplicates",
            "source_kind": "real",
            "cases": [
                {
                    "id": "same",
                    "label": "match",
                    "split": "calibration",
                    "reference_path": "a.png",
                    "candidate_path": "b.png",
                },
                {
                    "id": "same",
                    "label": "mismatch",
                    "split": "validation",
                    "reference_path": "a.png",
                    "candidate_path": "c.png",
                },
            ],
        }
        with self.assertRaisesRegex(BenchmarkError, "duplicates"):
            validate_benchmark_document(payload)

    def test_duplicate_image_pair_is_rejected(self):
        payload = {
            "contract_version": "1.0",
            "benchmark_id": "duplicate-pair",
            "source_kind": "real",
            "cases": [
                {
                    "id": "first",
                    "label": "match",
                    "split": "calibration",
                    "reference_path": "a.png",
                    "candidate_path": "b.png",
                },
                {
                    "id": "second",
                    "label": "match",
                    "split": "validation",
                    "reference_path": "a.png",
                    "candidate_path": "b.png",
                },
            ],
        }
        with self.assertRaisesRegex(BenchmarkError, "image pair"):
            validate_benchmark_document(payload)

    def test_invalid_crop_geometry_is_rejected(self):
        payload = {
            "contract_version": "1.0",
            "benchmark_id": "invalid-crop",
            "source_kind": "real",
            "cases": [
                {
                    "id": "case-1",
                    "label": "match",
                    "split": "calibration",
                    "reference_path": "a.png",
                    "candidate_path": "b.png",
                    "candidate_crop": [0.8, 0.1, 0.2, 0.9],
                }
            ],
        }
        with self.assertRaisesRegex(BenchmarkError, "left < right"):
            validate_benchmark_document(payload)


class ThresholdSelectionTests(unittest.TestCase):
    def test_validation_cases_do_not_influence_threshold_selection(self):
        calibration = [
            ScoredCase("low", "mismatch", "calibration", 0.1, 0, 0, 0),
            ScoredCase("mid", "ambiguous", "calibration", 0.5, 0, 0, 0),
            ScoredCase("high", "match", "calibration", 0.9, 0, 0, 0),
        ]
        adversarial_validation = [
            ScoredCase("v-low", "match", "validation", 0.05, 0, 0, 0),
            ScoredCase("v-high", "mismatch", "validation", 0.95, 0, 0, 0),
        ]
        self.assertEqual(
            choose_thresholds(calibration),
            choose_thresholds(calibration + adversarial_validation),
        )

    def test_incomplete_calibration_labels_have_no_recommendation(self):
        cases = [
            ScoredCase("low", "mismatch", "calibration", 0.1, 0, 0, 0),
            ScoredCase("high", "match", "calibration", 0.9, 0, 0, 0),
        ]
        self.assertIsNone(choose_thresholds(cases))


class CalibrationReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.fixture = CalibrationFixture(self.root)
        self.manifest_path = self.root / "benchmark.json"

    def tearDown(self):
        self.directory.cleanup()

    def test_synthetic_benchmark_is_always_provisional(self):
        manifest = self.fixture.manifest()
        report = calibrate_identity_benchmark(manifest, self.manifest_path)
        thresholds = report["recommended_thresholds"]

        self.assertEqual(report["status"], "provisional")
        self.assertIn("source_kind is not real", report["status_reasons"])
        self.assertIsNotNone(thresholds)
        self.assertLess(thresholds["reject_below"], thresholds["review_below"])
        self.assertEqual(report["metrics"]["validation"]["accuracy"], 1.0)
        self.assertFalse(report["canon_updated"])

    def test_ready_status_requires_real_data_and_minimum_counts(self):
        manifest = self.fixture.manifest(
            source_kind="real",
            counts=READY_MINIMUMS,
        )
        report = calibrate_identity_benchmark(manifest, self.manifest_path)

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["status_reasons"], [])
        self.assertEqual(report["metrics"]["validation"]["accuracy"], 1.0)

    def test_poor_validation_quality_cannot_be_ready(self):
        manifest = self.fixture.manifest(
            source_kind="real",
            counts=READY_MINIMUMS,
        )
        for case in manifest["cases"]:
            if case["split"] == "validation" and case["label"] == "mismatch":
                reference_portrait().save(self.root / case["candidate_path"])

        report = calibrate_identity_benchmark(manifest, self.manifest_path)

        self.assertEqual(report["status"], "provisional")
        self.assertTrue(
            any("critical_errors" in reason for reason in report["status_reasons"])
        )

    def test_fingerprint_is_portable_across_directories(self):
        manifest = self.fixture.manifest()
        first = calibrate_identity_benchmark(manifest, self.manifest_path)
        with tempfile.TemporaryDirectory() as second_directory:
            second_root = Path(second_directory)
            second_fixture = CalibrationFixture(second_root)
            second_manifest = second_fixture.manifest()
            second = calibrate_identity_benchmark(
                second_manifest,
                second_root / "benchmark.json",
            )
        self.assertEqual(first["dataset_fingerprint"], second["dataset_fingerprint"])

    def test_cli_writes_report_without_changing_canon(self):
        manifest = self.fixture.manifest()
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        output = self.root / "report.json"
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = main([str(self.manifest_path), "--output", str(output)])

        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertIn("PROVISIONAL", stdout.getvalue())
        self.assertFalse(report["canon_updated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
