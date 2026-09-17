import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from continuity_cli import main
from deployment import (
    EXIT_DEPLOYMENT_UNSAFE,
    DeploymentCheck,
    inspect_deployment,
    validate_public_image,
    validate_public_image_batch,
)

ROOT = Path(__file__).resolve().parent
FFMPEG_OK = DeploymentCheck("ffmpeg", "PASS", "available")


def make_repository(root: Path) -> None:
    for relative in ("app.py", "canon.json", "requirements.txt"):
        (root / relative).write_text("fixture", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "deployment-cost.md").write_text("fixture", encoding="utf-8")
    (root / "data").mkdir()


class DeploymentPreflightTests(unittest.TestCase):
    def test_default_public_profile_is_zero_api_cost_and_ready(self):
        report = inspect_deployment(ROOT, environment={}, ffmpeg_check=FFMPEG_OK)

        self.assertEqual(report["status"], "READY")
        self.assertEqual(report["cost_boundary"]["cpu_validator_api_cost"], "zero")
        self.assertEqual(report["cost_boundary"]["video_generation_api"], "disabled")

    def test_public_raw_logs_block_deployment(self):
        report = inspect_deployment(
            ROOT, environment={"SHOW_RAW_LOGS": "1"}, ffmpeg_check=FFMPEG_OK
        )

        self.assertEqual(report["status"], "UNSAFE")
        self.assertEqual(report["exit_code"], EXIT_DEPLOYMENT_UNSAFE)

    def test_incomplete_paid_video_configuration_is_unsafe(self):
        report = inspect_deployment(
            ROOT,
            environment={"ENABLE_VIDEO_GENERATION": "1"},
            ffmpeg_check=FFMPEG_OK,
        )

        self.assertEqual(report["status"], "UNSAFE")

    def test_controlled_paid_video_requires_caps_and_never_prints_secrets(self):
        secret = "do-not-print-this-secret"
        environment = {
            "ENABLE_VIDEO_GENERATION": "1",
            "VIDEO_PROVIDER": "minimax",
            "MINIMAX_VIDEO_API_KEY": secret,
            "VIDEO_ACCESS_CODE": secret,
            "MAX_ACTIVE_VIDEO_JOBS": "1",
            "MAX_DAILY_VIDEO_JOBS": "3",
            "VIDEO_BUDGET_CNY": "10",
        }
        report = inspect_deployment(
            ROOT,
            target="modelscope",
            environment=environment,
            ffmpeg_check=FFMPEG_OK,
        )

        self.assertEqual(report["status"], "READY")
        self.assertNotIn(secret, json.dumps(report))

    def test_missing_required_file_blocks_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_repository(root)
            (root / "canon.json").unlink()
            report = inspect_deployment(root, environment={}, ffmpeg_check=FFMPEG_OK)

        self.assertEqual(report["status"], "UNSAFE")
        self.assertIn("canon.json", json.dumps(report))

    def test_json_cli_is_machine_readable_without_absolute_repository_path(self):
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("deployment.probe_ffmpeg", return_value=FFMPEG_OK),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(["deploy-check", "--target", "huggingface", "--json"])

        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["target"], "huggingface")
        self.assertNotIn(str(ROOT), stdout.getvalue())

    def test_unsafe_cli_returns_stable_exit_code(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("deployment.probe_ffmpeg", return_value=FFMPEG_OK),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(["deploy-check", "--root", directory])

        self.assertEqual(exit_code, EXIT_DEPLOYMENT_UNSAFE)

    def test_image_longest_edge_is_capped(self):
        with self.assertRaisesRegex(ValueError, "1280px"):
            validate_public_image(Image.new("RGB", (1281, 720)))

    def test_image_file_size_is_capped_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.png"
            with path.open("wb") as output:
                output.truncate(10 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(ValueError, "10MB"):
                validate_public_image(path)

    def test_public_batch_is_capped_at_ten_images(self):
        with self.assertRaisesRegex(ValueError, "最多 10 张"):
            validate_public_image_batch([Image.new("RGB", (1, 1))] * 11)


if __name__ == "__main__":
    unittest.main()
