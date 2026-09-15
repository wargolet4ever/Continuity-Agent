"""双模式 Orchestrator 测试。全部在本地降级下运行，不调用任何外部 API。"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

import creator
import orchestrator
import package
from analyzer import OBSERVATION_OPTIONS, ContinuityAnalyzer
from canon_loader import CanonStore

ROOT = Path(__file__).resolve().parent


class IntentTests(unittest.TestCase):
    def test_classify(self):
        for text, expected in [
            ("一个宇航员在空间站发现自己的备份", "CREATE"),
            ("深海考古队发现会说话的沉船", "CREATE"),
            ("检查 Shot 21 的钥匙插槽位置", "AUDIT"),
            ("看看镜头19的小屏", "AUDIT"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(orchestrator.classify_intent(text), expected)


class CreateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.result = orchestrator.run_create(
            "一个潜水员在沉船里发现了自己的照片", 5, workdir=cls._tmp.name
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_all_steps_ran_and_succeeded(self):
        trace = self.result["trace"]
        self.assertEqual(len(trace.steps), 7)
        self.assertEqual(trace.failures, [])
        for step in trace.steps:
            self.assertIn(step["kind"], (orchestrator.MODEL, orchestrator.LOCAL))
            self.assertGreaterEqual(step["duration_ms"], 0)

    def test_consistency_steps_are_never_delegated_to_a_model(self):
        """canon 装配、约束、路由、审计必须永远是本地确定性的。"""
        deterministic = {"③ 装配 Canon 草案", "④ 生成连续性约束", "⑥ 模型策略", "⑦ 草案因果审计"}
        for step in self.result["trace"].steps:
            if step["name"] in deterministic:
                self.assertEqual(step["kind"], orchestrator.LOCAL, step["name"])

    def test_shot_count_and_clamping(self):
        self.assertEqual(len(self.result["shots"]), 5)
        with tempfile.TemporaryDirectory() as directory:
            small = orchestrator.run_create("一句话", 3, workdir=directory)
            self.assertEqual(len(small["shots"]), 3)
        with tempfile.TemporaryDirectory() as directory:
            clamped = orchestrator.run_create("一句话", 99, workdir=directory)
            self.assertEqual(len(clamped["shots"]), 5)

    def test_draft_canon_is_consumable_by_canonstore(self):
        store = CanonStore(self.result["canon_path"])
        self.assertEqual(len(store.shot_ids), 5)
        self.assertTrue(store.compile_rule_pack("1")["rules"])
        self.assertIn(store.route("1")["tier"], ("primary", "economy", "post_only"))
        self.assertIn(store.anchor_for("2")["anchor"], ("chain", "canonical_lookup"))
        self.assertEqual(store.narrative_audit(), [])

    def test_every_shot_has_a_prompt_and_awaits_generation(self):
        for shot in self.result["shots"]:
            self.assertTrue(self.result["prompts"][shot["shot_id"]]["positive"])
            self.assertTrue(self.result["prompts"][shot["shot_id"]]["negative"])
            self.assertEqual(shot["status"], "AWAITING_EXTERNAL_GENERATION")
            self.assertIsNone(shot["task_id"])
        self.assertEqual(
            self.result["generation_status"], "AWAITING_EXTERNAL_GENERATION"
        )

    def test_package_contents(self):
        path = package.build_package(self.result)
        with zipfile.ZipFile(path) as bundle:
            names = set(bundle.namelist())
            self.assertEqual(
                names,
                {
                    "shot_plan.md",
                    "prompts.md",
                    "model_plan.md",
                    "narrative_audit.md",
                    "canon_draft.json",
                    "trace.json",
                    "README.txt",
                },
            )
            readme = bundle.read("README.txt").decode()
            self.assertIn("AWAITING EXTERNAL GENERATION", readme)
            self.assertNotIn("已生成", readme)
            trace = json.loads(bundle.read("trace.json"))
            self.assertEqual(len(trace["steps"]), 7)

    def test_empty_idea_rejected(self):
        with self.assertRaises(ValueError):
            orchestrator.run_create("   ")


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canon = CanonStore(ROOT / "canon.json")
        cls.analyzer = ContinuityAnalyzer(cls.canon)

    def run_chain(self, text):
        return orchestrator.run_audit_chain(
            text, self.analyzer, self.canon, OBSERVATION_OPTIONS
        )

    def test_natural_language_reaches_all_four_decisions(self):
        cases = [
            ("检查 Shot 21 的钥匙插槽位置对不对", "21", "REGENERATE", "SHOT-21A"),
            ("看看镜头19是不是多了一块小型显示屏", "19", "LOCAL FIX", "LOC-B03"),
            ("第16镜 047 的表情我拿不准", "16", "HUMAN REVIEW", "CHAR-0405"),
            ("Shot 22 我看过了没问题", "22", "PASS", None),
        ]
        for text, shot, decision, rule in cases:
            with self.subTest(text=text):
                chain = self.run_chain(text)
                self.assertEqual(chain["shot_id"], shot)
                self.assertEqual(chain["result"]["decision"], decision)
                if rule:
                    self.assertIn(
                        rule, [i["rule_id"] for i in chain["result"]["issues"]]
                    )

    def test_trace_has_four_steps(self):
        chain = self.run_chain("检查 Shot 21 的钥匙")
        self.assertEqual(len(chain["trace"].steps), 4)
        self.assertEqual(chain["trace"].failures, [])

    def test_unknown_shot_falls_back_without_crashing(self):
        chain = self.run_chain("检查一下那个镜头")
        self.assertIn(chain["shot_id"], self.canon.shot_ids)

    def test_clean_flag_is_dropped_when_a_problem_is_also_mentioned(self):
        parsed = orchestrator.parse_audit_instruction(
            "Shot 19 我核对过了，但好像多了一块小屏", self.canon, OBSERVATION_OPTIONS
        )
        self.assertNotIn("画面已人工核对无异常", parsed["observations"])
        self.assertIn("出现未经设定的小型显示屏", parsed["observations"])


class RetryPolicyTests(unittest.TestCase):
    def test_api_retry_limit_is_two(self):
        self.assertEqual(orchestrator.MAX_API_RETRIES, 2)

    def test_no_model_caller_without_credentials(self):
        self.assertIsNone(orchestrator.make_model_caller())


if __name__ == "__main__":
    unittest.main()


class MultimodalRetryTests(unittest.TestCase):
    """AUDIT 的真实多模态路径：临时错误重试 ≤2 → 仍失败则降级并转人工。"""

    @classmethod
    def setUpClass(cls):
        cls.canon = CanonStore(ROOT / "canon.json")

    def _analyzer_with(self, side_effect):
        import analyzer as analyzer_module

        az = ContinuityAnalyzer(self.canon)
        patcher = mock.patch.object(analyzer_module.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        opener = mock.patch.object(
            analyzer_module.urllib.request, "urlopen", side_effect=side_effect
        )
        opener.start()
        self.addCleanup(opener.stop)
        env = mock.patch.dict(
            os.environ, {"LLM_API_KEY": "test-key", "LLM_MODEL": "test-vision"}
        )
        env.start()
        self.addCleanup(env.stop)
        return az

    @staticmethod
    def _http_error(code):
        return urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions", code, "err", {}, io.BytesIO(b"{}")
        )

    def _run(self, az):
        return orchestrator.run_audit_chain(
            "检查 Shot 21 的钥匙插槽位置对不对",
            az,
            self.canon,
            OBSERVATION_OPTIONS,
            image=Image.new("RGB", (16, 16), (60, 70, 80)),
            mode="多模态 API（图像 + canon）",
        )

    def test_429_retries_twice_then_degrades(self):
        az = self._analyzer_with(lambda *a, **k: (_ for _ in ()).throw(self._http_error(429)))
        chain = self._run(az)
        result = chain["result"]
        self.assertEqual(result["api_retries"], 2)          # 上限 2 次
        self.assertFalse(result["api_reviewed"])            # 未完成视觉审计
        self.assertIn("429", result["api_error"])
        self.assertIn("已重试 2 次仍失败", result["api_error"])
        self.assertNotEqual(chain["evidence_source"], orchestrator.EVIDENCE_VISUAL)
        step = chain["trace"].steps[-1]
        self.assertEqual(step["retries"], 2)
        self.assertIn("已降级为本地规则", step["detail"])

    def test_timeout_retries_then_degrades(self):
        az = self._analyzer_with(lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
        chain = self._run(az)
        self.assertEqual(chain["result"]["api_retries"], 2)
        self.assertIn("TimeoutError", chain["result"]["api_error"])
        self.assertFalse(chain["result"]["api_reviewed"])

    def test_400_is_not_retried(self):
        """业务错误重试没有意义，必须一次就停。"""
        az = self._analyzer_with(lambda *a, **k: (_ for _ in ()).throw(self._http_error(400)))
        chain = self._run(az)
        self.assertEqual(chain["result"]["api_retries"], 0)
        self.assertIn("未重试", chain["result"]["api_error"])

    def test_degraded_audit_is_logged_with_retries_and_reason(self):
        import app as app_module
        from take_log import FIELDS, TakeLog

        az = self._analyzer_with(lambda *a, **k: (_ for _ in ()).throw(self._http_error(429)))
        with tempfile.TemporaryDirectory() as directory:
            real_log, real_az = app_module.take_log, app_module.analyzer
            app_module.take_log = TakeLog(Path(directory) / "t.csv")
            app_module.analyzer = az
            try:
                app_module.do_audit(
                    "检查 Shot 21 的钥匙插槽位置对不对",
                    Image.new("RGB", (16, 16), (60, 70, 80)),
                    "多模态 API（图像 + canon）",
                    app_module.new_session(),
                    "即梦", "s21.png", "待定", "", "",
                )
                row = dict(zip(FIELDS[:-1], app_module.take_log.rows()[0]))
            finally:
                app_module.take_log, app_module.analyzer = real_log, real_az
        self.assertEqual(row["retries"], "2")
        self.assertIn("429", row["failure_reason"])
        self.assertEqual(row["model_source"], "test-vision")
        self.assertEqual(row["evidence_source"], orchestrator.EVIDENCE_RULE_PIXEL)


class VideoExtensionPointTests(unittest.TestCase):
    """MiniMax 视频适配器全用 mock，测试不会产生任何外部请求或费用。"""

    class FakeResponse:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.payload

    @staticmethod
    def task(vp, *, image=True):
        return vp.GenerationTask(
            shot_id="1",
            prompt="固定镜头，人物缓慢抬头",
            duration=6,
            aspect_ratio="16:9",
            resolution="768P",
            model="MiniMax-Hailuo-2.3-Fast",
            provider="minimax",
            reference_images=["data:image/jpeg;base64,AAAA"] if image else [],
        )

    @staticmethod
    def provider(vp):
        with mock.patch.dict(os.environ, {"MINIMAX_VIDEO_API_KEY": "test-only"}):
            return vp.MiniMaxHailuoProvider()

    def test_no_provider_is_available(self):
        import video_provider as vp

        with mock.patch.dict(
            os.environ,
            {"VIDEO_PROVIDER": "minimax", "MINIMAX_VIDEO_API_KEY": "test-only"},
            clear=False,
        ):
            os.environ.pop("ENABLE_VIDEO_GENERATION", None)
            self.assertIsNone(vp.get_provider())

    def test_provider_requires_explicit_feature_flag(self):
        import video_provider as vp

        with mock.patch.dict(
            os.environ,
            {
                "ENABLE_VIDEO_GENERATION": "1",
                "VIDEO_PROVIDER": "minimax",
                "MINIMAX_VIDEO_API_KEY": "test-only",
                "VIDEO_ACCESS_CODE": "test-access",
            },
        ):
            self.assertIsInstance(vp.get_provider(), vp.MiniMaxHailuoProvider)

    def test_shot_carries_everything_needed_to_submit(self):
        import video_provider as vp

        with tempfile.TemporaryDirectory() as directory:
            result = orchestrator.run_create("一句创意", 3, workdir=directory)
            for shot in result["shots"]:
                for key in ("shot_id", "prompt", "duration_sec", "aspect_ratio",
                            "resolution", "reference_images", "model_strategy"):
                    self.assertIn(key, shot)
                self.assertTrue(shot["prompt"])
                self.assertIn(shot["model_strategy"], ("primary", "economy", "post_only"))
                task = vp.task_from_shot(shot, result["routing"][shot["shot_id"]])
                self.assertEqual(task.status, vp.AWAITING)
                self.assertEqual(task.duration, 6)
                self.assertTrue(task.prompt)

    def test_status_machine(self):
        import video_provider as vp

        self.assertTrue(vp.can_transition(vp.AWAITING, vp.SUBMITTED))
        self.assertTrue(vp.can_transition(vp.SUBMITTED, vp.RUNNING))
        self.assertTrue(vp.can_transition(vp.RUNNING, vp.SUCCEEDED))
        self.assertTrue(vp.can_transition(vp.RUNNING, vp.FAILED))
        self.assertTrue(vp.can_transition(vp.FAILED, vp.HUMAN_REVIEW))
        self.assertFalse(vp.can_transition(vp.AWAITING, vp.SUCCEEDED))
        self.assertFalse(vp.can_transition(vp.SUCCEEDED, vp.RUNNING))

    def test_credentials_are_never_hardcoded(self):
        source = (ROOT / "video_provider.py").read_text(encoding="utf-8")
        self.assertNotIn("sk-", source)
        for key in ("MINIMAX_VIDEO_API_KEY", "MINIMAX_VIDEO_BASE_URL", "VIDEO_MODEL"):
            self.assertIn(key, source)
        self.assertIn("os.getenv", source)

    def test_fast_model_requires_first_frame_and_never_calls_network(self):
        import video_provider as vp

        provider = self.provider(vp)
        with mock.patch.object(vp.urllib.request, "urlopen") as opener:
            task = provider.submit(self.task(vp, image=False))
        opener.assert_not_called()
        self.assertEqual(task.status, vp.HUMAN_REVIEW)
        self.assertEqual(task.error.kind, vp.REQUEST)

    def test_submit_uses_v1_payload_and_returns_task_id(self):
        import video_provider as vp

        provider = self.provider(vp)
        response = self.FakeResponse(
            {"task_id": "task-123", "base_resp": {"status_code": 0, "status_msg": "success"}}
        )
        with mock.patch.object(vp.urllib.request, "urlopen", return_value=response) as opener:
            task = provider.submit(self.task(vp))
        request = opener.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertTrue(request.full_url.endswith("/v1/video_generation"))
        self.assertEqual(payload["model"], "MiniMax-Hailuo-2.3-Fast")
        self.assertIn("first_frame_image", payload)
        self.assertNotIn("aspect_ratio", payload)
        self.assertFalse(payload["prompt_optimizer"])
        self.assertEqual(task.task_id, "task-123")
        self.assertEqual(task.status, vp.SUBMITTED)

    def test_poll_retries_two_429s_then_succeeds_and_fetches_url(self):
        import video_provider as vp

        provider = self.provider(vp)
        task = self.task(vp)
        task.task_id = "task-123"
        task.status = vp.SUBMITTED

        def rate_limit():
            return urllib.error.HTTPError(
                "https://example.invalid", 429, "rate", {},
                io.BytesIO(b'{"base_resp":{"status_code":429,"status_msg":"rate limit"}}'),
            )

        poll_success = self.FakeResponse(
            {
                "task_id": "task-123", "status": "Success", "file_id": "file-456",
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )
        fetch_success = self.FakeResponse(
            {
                "file": {"file_id": "file-456", "download_url": "https://cdn.example/video.mp4"},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )
        with mock.patch.object(
            vp.urllib.request, "urlopen",
            side_effect=[rate_limit(), rate_limit(), poll_success, fetch_success],
        ) as opener:
            task = provider.poll(task)
            self.assertEqual(task.retries, 2)
            self.assertEqual(task.status, vp.SUCCEEDED)
            self.assertEqual(task.file_id, "file-456")
            task = provider.fetch_result(task)
        self.assertEqual(opener.call_count, 4)
        self.assertEqual(task.video_url, "https://cdn.example/video.mp4")

    def test_submit_permission_error_is_not_retried(self):
        import video_provider as vp

        provider = self.provider(vp)
        error = urllib.error.HTTPError(
            "https://example.invalid", 400, "bad", {},
            io.BytesIO(b'{"base_resp":{"status_code":1004,"status_msg":"permission denied"}}'),
        )
        with mock.patch.object(vp.urllib.request, "urlopen", side_effect=error) as opener:
            task = provider.submit(self.task(vp))
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(task.status, vp.HUMAN_REVIEW)
        self.assertEqual(task.error.kind, vp.ENTITLEMENT)
        self.assertFalse(task.error.retryable)

    def test_cost_table_and_guard(self):
        import video_provider as vp

        self.assertEqual(
            vp.estimated_cost_cny("MiniMax-Hailuo-2.3-Fast", "768P", 6), 1.35
        )
        with mock.patch.dict(
            os.environ,
            {"MAX_ACTIVE_VIDEO_JOBS": "1", "MAX_DAILY_VIDEO_JOBS": "1", "VIDEO_BUDGET_CNY": "2"},
        ):
            guard = vp.GenerationGuard()
        token = guard.reserve(1.35)
        with self.assertRaises(RuntimeError):
            guard.reserve(1.35)
        guard.release(token)
        with self.assertRaises(RuntimeError):
            guard.reserve(1.35)
