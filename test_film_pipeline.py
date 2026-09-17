"""Module 8: six-shot loop, blocker-only repair, and honest demo evidence."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import video_audit
from continuity_cli import main
from film_pipeline import (
    EXIT_BLOCKERS_REMAIN,
    EXIT_FFMPEG_UNAVAILABLE,
    EXIT_OK,
    FilmPipelineError,
    FixtureBlockerAuditor,
    FixtureVideoVendor,
    run_film,
    validate_film_report,
)
from orchestrator import run_create


class SixShotPlanningTests(unittest.TestCase):
    def test_film_mode_allows_six_without_changing_create_default_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            film = run_create("一个宇航员发现舷窗外的自己", 6, directory, max_shots=6)
            self.assertEqual(len(film["shots"]), 6)
        with tempfile.TemporaryDirectory() as directory:
            regular = run_create("一句话", 99, directory)
            self.assertEqual(len(regular["shots"]), 5)

    def test_film_planning_stays_offline_even_if_llm_credentials_exist(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {"LLM_API_KEY": "must-not-be-used", "LLM_MODEL": "paid-model"},
            ),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError),
        ):
            result = run_create(
                "一个宇航员发现舷窗外的自己",
                6,
                directory,
                max_shots=6,
                allow_model=False,
            )
        self.assertFalse(result["api_used"])
        self.assertEqual(len(result["shots"]), 6)


@unittest.skipUnless(video_audit.VIDEO_SUPPORTED, "ffmpeg is unavailable")
class FilmPipelineTests(unittest.TestCase):
    def test_empty_demo_blocker_set_finishes_without_repairs(self):
        with tempfile.TemporaryDirectory() as directory:
            run = run_film(
                "一个宇航员发现舷窗外的自己",
                directory,
                vendor=FixtureVideoVendor(blocker_shots=set()),
                auditor=FixtureBlockerAuditor(),
            )
            self.assertEqual(run.exit_code, EXIT_OK)
            self.assertEqual(run.report["summary"]["total_generation_count"], 6)
            self.assertEqual(run.report["summary"]["repaired_shot_ids"], [])

    def test_repairs_only_blockers_then_concatenates_six_latest_takes(self):
        with tempfile.TemporaryDirectory() as directory:
            run = run_film(
                "一个宇航员发现舷窗外的自己",
                directory,
                vendor=FixtureVideoVendor(blocker_shots={"2", "5"}),
                auditor=FixtureBlockerAuditor(),
            )
            self.assertEqual(run.exit_code, EXIT_OK)
            self.assertTrue(run.video_path.is_file())
            self.assertTrue(run.report_json.is_file())
            self.assertTrue(run.report_html.is_file())
            summary = run.report["summary"]
            self.assertEqual(summary["repaired_shot_ids"], ["2", "5"])
            self.assertEqual(summary["total_generation_count"], 8)
            self.assertFalse(summary["whole_film_rerun"])
            counts = {str(i): 0 for i in range(1, 7)}
            for event in run.report["generation_events"]:
                counts[event["shot_id"]] += 1
                self.assertEqual(event["provider"], "fixture-ffmpeg")
            self.assertEqual(counts, {"1": 1, "2": 2, "3": 1, "4": 1, "5": 2, "6": 1})
            stored = json.loads(run.report_json.read_text(encoding="utf-8"))
            self.assertEqual(stored["audit"]["mode"], "scripted-fixture-blocker-check")
            self.assertIn(
                "no visual model inspection", stored["audit"]["evidence_note"]
            )

    def test_persistent_blocker_stops_after_two_repair_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            run = run_film(
                "一名潜水员在沉船里找到自己的照片",
                directory,
                vendor=FixtureVideoVendor(
                    blocker_shots={"2"}, persistent_blocker_shots={"2"}
                ),
                auditor=FixtureBlockerAuditor(),
            )
            self.assertEqual(run.exit_code, EXIT_BLOCKERS_REMAIN)
            self.assertIsNone(run.video_path)
            self.assertEqual(run.report["summary"]["repair_rounds_used"], 2)
            self.assertEqual(run.report["summary"]["remaining_blocker_shot_ids"], ["2"])
            self.assertEqual(run.report["summary"]["total_generation_count"], 8)

    def test_report_validator_rejects_non_blocker_regeneration(self):
        with tempfile.TemporaryDirectory() as directory:
            run = run_film(
                "一个宇航员发现舷窗外的自己",
                directory,
                vendor=FixtureVideoVendor(blocker_shots={"2"}),
                auditor=FixtureBlockerAuditor(),
            )
            tampered = copy.deepcopy(run.report)
            tampered["generation_events"].append(
                {
                    "round": 1,
                    "shot_id": "1",
                    "attempt": 2,
                    "provider": "fixture-ffmpeg",
                    "file": "clips/shot-01-take-02.mp4",
                }
            )
            tampered["summary"]["total_generation_count"] += 1
            with self.assertRaisesRegex(FilmPipelineError, "not a blocker"):
                validate_film_report(tampered)


class FilmCliTests(unittest.TestCase):
    def test_missing_ffmpeg_has_stable_exit_code(self):
        error = FilmPipelineError("ffmpeg unavailable", EXIT_FFMPEG_UNAVAILABLE)
        with mock.patch("continuity_cli.FixtureVideoVendor", side_effect=error):
            code = main(["film", "一句话", "--output", "unused"])
        self.assertEqual(code, EXIT_FFMPEG_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
