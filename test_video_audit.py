"""Video AUDIT tests.  Remote model calls are mocked and incur no cost."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app as app_module
import orchestrator as orch
from analyzer import OBSERVATION_OPTIONS, ContinuityAnalyzer
from canon_loader import CanonStore
from take_log import TakeLog
from video_audit import preview_video, sample_video


ROOT = Path(__file__).resolve().parent
# 走和 app 同一个解析器：示例视频搬过目录，两边不能各写各的路径。
from app import example_video_path

SHOT19 = example_video_path("A")
SHOT21 = example_video_path("B")


class VideoAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canon = CanonStore(ROOT / "canon.json")
        cls.analyzer = ContinuityAnalyzer(cls.canon)

    def test_bundled_examples_extract_five_ordered_frames(self):
        for path in (SHOT19, SHOT21):
            with self.subTest(path=path.name):
                sample = sample_video(path)
                self.assertEqual(len(sample.frames), 5)
                self.assertEqual(sample.timestamps_s, sorted(sample.timestamps_s))
                self.assertGreater(sample.duration_s, 4.9)
                self.assertLess(sample.duration_s, 5.2)
                self.assertTrue(all(frame.mode == "RGB" for frame in sample.frames))

    def test_preview_returns_captioned_gallery(self):
        gallery, note = preview_video(SHOT19)
        self.assertEqual(len(gallery), 5)
        self.assertTrue(all(caption.endswith("s") for _, caption in gallery))
        self.assertIn("同一段素材的时间序列", note)

    def test_shot19_video_audit_uses_real_error_and_logs_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            real_log = app_module.take_log
            app_module.take_log = TakeLog(Path(directory) / "takes.csv")
            try:
                head, _, detail, _, trace, _, state = app_module.do_audit(
                    "检查 Shot 19：操纵台整体在镜头内发生变形",
                    None,
                    "本地规则（无需 API）",
                    app_module.new_session(),
                    "即梦",
                    "",
                    "不采纳",
                    "空间关系错",
                    "",
                    str(SHOT19),
                )
                row = app_module.take_log.rows()[0]
            finally:
                app_module.take_log = real_log
        self.assertIn("重新生成", head)                      # 面上说人话
        self.assertIn(orch.EVIDENCE_VIDEO_RULE, detail)      # 代号在详细报告
        self.assertIn("REGENERATE", detail)
        # 规则 ID 断言底层结果，不依赖界面表格有几列
        self.assertEqual(state["last_audit"]["issues"][0]["rule_id"], "LOC-B06")
        self.assertEqual(len(trace), 4)
        self.assertEqual(state["last_audit"]["media_kind"], "video")
        self.assertIn(SHOT19.name, row)

    def test_shot21_video_routes_all_three_observed_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            real_log = app_module.take_log
            app_module.take_log = TakeLog(Path(directory) / "takes.csv")
            try:
                head, issues, detail, _, _, _, state = app_module.do_audit(
                    "检查 Shot 21：拉杆突然变形，钥匙取出位置错误",
                    None,
                    "本地规则（无需 API）",
                    app_module.new_session(),
                    "即梦",
                    "shot21.mp4",
                    "不采纳",
                    "空间关系错",
                    "",
                    str(SHOT21),
                )
            finally:
                app_module.take_log = real_log
        ids = {issue["rule_id"] for issue in state["last_audit"]["issues"]}
        self.assertIn("重新生成", head)
        self.assertIn("REGENERATE", detail)
        self.assertEqual(ids, {"LOC-B06", "SHOT-21A", "SHOT-21B"})
        # 界面表格只有两列：哪里不对 / 怎么修
        self.assertTrue(all(len(row) == 2 for row in issues["value"]))

    def test_image_and_video_together_are_rejected_before_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            real_log = app_module.take_log
            app_module.take_log = TakeLog(Path(directory) / "takes.csv")
            try:
                head, issues, _, _, trace, _, _ = app_module.do_audit(
                    "检查 Shot 19",
                    Image.new("RGB", (8, 8)),
                    "本地规则（无需 API）",
                    app_module.new_session(),
                    "",
                    "",
                    "待定",
                    "",
                    "",
                    str(SHOT19),
                )
                self.assertEqual(app_module.take_log.rows(), [])
            finally:
                app_module.take_log = real_log
        self.assertIn("只上传一种", head)
        self.assertFalse(issues["visible"])
        self.assertEqual(trace, [])

    def test_multimodal_payload_contains_ordered_video_frames(self):
        sample = sample_video(SHOT21)
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            body = {"choices": [{"message": {"content": '{"issues": []}'}}]}
            return io.BytesIO(json.dumps(body).encode("utf-8"))

        with (
            patch.dict(
                os.environ,
                {"LLM_API_KEY": "test-not-a-secret", "LLM_MODEL": "mock-vision"},
            ),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            result = self.analyzer.audit(
                "21",
                "多模态 API（图像 + canon）",
                "检查跨帧几何结构",
                [],
                current_image=sample.frames[2],
                video_frames=sample.frames,
                video_timestamps=sample.timestamps_s,
            )
        content = captured["payload"]["messages"][1]["content"]
        labels = [item["text"] for item in content if item["type"] == "text"]
        images = [item for item in content if item["type"] == "image_url"]
        self.assertEqual(len(images), 5)
        self.assertEqual(sum(label.startswith("VIDEO FRAME") for label in labels), 5)
        self.assertTrue(result["api_reviewed"])
        self.assertEqual(result["media_kind"], "video")

    def test_video_multimodal_429_retries_twice_then_degrades(self):
        sample = sample_video(SHOT21)
        error = urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions",
            429,
            "rate",
            {},
            io.BytesIO(b"{}"),
        )
        with (
            patch.dict(
                os.environ,
                {"LLM_API_KEY": "test-not-a-secret", "LLM_MODEL": "mock-vision"},
            ),
            patch("urllib.request.urlopen", side_effect=error) as call,
            patch("analyzer.time.sleep"),
        ):
            chain = orch.run_audit_chain(
                "检查 Shot 21：拉杆突然变形，钥匙取出位置错误",
                self.analyzer,
                self.canon,
                OBSERVATION_OPTIONS,
                image=sample.frames[2],
                mode="多模态 API（图像 + canon）",
                video_frames=sample.frames,
                video_timestamps=sample.timestamps_s,
            )
        self.assertEqual(call.call_count, 3)
        self.assertEqual(chain["result"]["api_retries"], 2)
        self.assertFalse(chain["result"]["api_reviewed"])
        self.assertEqual(chain["evidence_source"], orch.EVIDENCE_VIDEO_RULE)
        self.assertIn("已降级为本地规则", chain["trace"].steps[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
