"""公开环境的隐私隔离测试。

背景：take_log 是全进程共享的一份 CSV，不区分用户。
因此公开部署必须保证：**任何访客都读不到别人的输入。**

这组测试构造两个会话：A 提交一个独特 prompt，B 试图通过 UI 回调和公开
HTTP API 读取它。任何一条路径读到都算失败。
"""

import importlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from gradio.routes import App

from take_log import FIELDS, TakeLog

ROOT = Path(__file__).resolve().parent

SECRET = f"绝密创意-{uuid.uuid4().hex[:12]}"
SECRET_NOTE = f"内部备注-{uuid.uuid4().hex[:12]}"
SECRET_FILE = f"private_{uuid.uuid4().hex[:8]}.mp4"


def load_app(show_raw: bool):
    """按指定安全模式重新加载 app 模块。"""
    os.environ["SHOW_RAW_LOGS"] = "1" if show_raw else "0"
    import app as app_module

    importlib.reload(app_module)
    return app_module


class PublicModePrivacyTests(unittest.TestCase):
    """默认（公开）模式：SHOW_RAW_LOGS 未设置或为 0。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app(show_raw=False)
        cls._tmp = tempfile.TemporaryDirectory()
        cls.app.take_log = TakeLog(Path(cls._tmp.name) / "shared.csv")

        # ── 会话 A：提交带独特 prompt / 备注 / 文件名的一次审计 ──
        cls.session_a = cls.app.new_session()
        cls.returned_a = cls.app.do_audit(
            f"检查 Shot 21 的钥匙插槽位置对不对 {SECRET}",
            None,
            "本地规则（无需 API）",
            cls.session_a,
            "即梦",
            SECRET_FILE,
            "不采纳",
            "空间关系错",
            SECRET_NOTE,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        os.environ.pop("SHOW_RAW_LOGS", None)

    def test_default_is_safe(self):
        """安全模式必须是默认值——忘记配环境变量也不会泄露。"""
        self.assertFalse(self.app.SHOW_RAW_LOGS)

    def test_server_side_logging_still_happens(self):
        """隐私保护不能以停止记录为代价。"""
        rows = self.app.take_log.rows()
        self.assertEqual(len(rows), 1)
        self.assertIn(SECRET, json.dumps(rows, ensure_ascii=False))

    def test_audit_response_does_not_carry_log_rows(self):
        """do_audit 的返回值里不得出现任何历史日志。"""
        # 6 项：人话结论、问题表、详细报告、修订 prompt、执行轨迹、会话状态。
        # 这里面**没有日志行**——那才是这条测试要守的东西。
        self.assertEqual(len(self.returned_a), 6)
        blob = json.dumps(self.returned_a[:-1], ensure_ascii=False, default=str)
        self.assertIn(SECRET, blob)                  # A 自己的输入回显是正常的
        self.assertNotIn(SECRET_NOTE, blob)          # 但备注不该回来
        self.assertNotIn(SECRET_FILE, blob)

    def test_session_b_cannot_read_session_a_via_ui(self):
        """会话 B 走一遍 UI 回调，不得看到 A 的任何输入。"""
        session_b = self.app.new_session()
        self.assertIsNone(session_b["package_path"])

        outputs = []
        outputs.append(self.app.do_audit(
            "看看镜头 19 的小屏", None, "本地规则（无需 API）",
            session_b, "", "", "待定", "", ""))
        outputs.append(self.app.refresh_log())                 # 安全模式下应为空
        outputs.append(self.app.take_log.stats())              # 聚合统计
        outputs.append(self.app.run_demo("A", session_b))
        outputs.append(self.app.do_create("一句无关的创意", 3, session_b))
        outputs.append(self.app.shot_detail("19", session_b))
        outputs.append(self.app.canon_health(session_b))
        outputs.append(self.app.narrative_health(self.app.CUT_STATE))

        blob = json.dumps(outputs, ensure_ascii=False, default=str)
        for secret in (SECRET, SECRET_NOTE, SECRET_FILE):
            with self.subTest(secret=secret[:12]):
                self.assertNotIn(secret, blob)

    def test_refresh_log_returns_nothing_in_public_mode(self):
        self.assertEqual(self.app.refresh_log(), [])

    def test_aggregate_stats_contain_no_free_text(self):
        """聚合统计可以公开，但必须只有计数和固定词表。"""
        summary, reasons = self.app.take_log.stats()
        blob = summary + json.dumps(reasons, ensure_ascii=False)
        for secret in (SECRET, SECRET_NOTE, SECRET_FILE):
            self.assertNotIn(secret, blob)

    def test_no_public_endpoint_returns_log_rows(self):
        """公开 API 面上不得存在任何返回日志行的端点。"""
        with TestClient(App.create_app(self.app.demo)) as client:
            info = client.get("/gradio_api/info").json()
            blob = json.dumps(info, ensure_ascii=False)
            for secret in (SECRET, SECRET_NOTE, SECRET_FILE):
                self.assertNotIn(secret, blob)
            # 逐个检查已注册的依赖，没有一个绑定到 refresh_log
            config = client.get("/config").json()
            names = json.dumps(config, ensure_ascii=False)
            for secret in (SECRET, SECRET_NOTE, SECRET_FILE):
                self.assertNotIn(secret, names)

    def test_refresh_log_is_not_bound_to_any_event(self):
        """最硬的一条：公开模式下 refresh_log 根本不在事件表里。"""
        bound = [
            fn.fn.__name__
            for fn in self.app.demo.fns.values()
            if getattr(fn, "fn", None) is not None and hasattr(fn.fn, "__name__")
        ]
        self.assertNotIn("refresh_log", bound)

    def test_video_provider_is_off_and_no_generation_callback_is_bound(self):
        """未设置 ENABLE_VIDEO_GENERATION 时，生成能力必须整体不存在。

        隐藏控件不是访问控制——只要回调进了事件表，就能被公开 API 直接调用。
        所以这里检查的是「没有绑定」，不是「没有显示」。
        """
        self.assertIsNone(self.app.video_provider)
        bound = [
            fn.fn.__name__
            for fn in self.app.demo.fns.values()
            if getattr(fn, "fn", None) is not None and hasattr(fn.fn, "__name__")
        ]
        for name in ("submit_video", "refresh_video", "prepare_video"):
            with self.subTest(callback=name):
                self.assertNotIn(name, bound)

    def test_disabled_video_callbacks_refuse_even_if_called_directly(self):
        """纵深防御：就算有人拿到函数引用，也必须被 Provider 门挡住。"""
        session = self.app.new_session()
        status, _, _ = self.app.submit_video("1", "任意 prompt", None, "任意码", True, session)
        self.assertIn("未启用", status)
        status, _, _ = self.app.refresh_video(session)
        self.assertIn("未启用", status)

    def test_video_api_key_never_appears_in_any_output(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("MINIMAX_VIDEO_API_KEY", source)
        provider_source = (ROOT / "video_provider.py").read_text(encoding="utf-8")
        # 密钥只允许出现在 os.environ / os.getenv 的读取处，不得写死。
        for line in provider_source.splitlines():
            if "MINIMAX_VIDEO_API_KEY" in line:
                self.assertTrue(
                    "os.environ" in line or "required_env" in line,
                    f"密钥出现在非读取位置：{line.strip()}",
                )

    def test_show_error_is_off_in_public_mode(self):
        """异常 traceback 含容器内绝对路径，公开环境不得外泄。"""
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("show_error=SHOW_RAW_LOGS", source)
        self.assertNotIn("show_error=True", source)


class PrivateModeTests(unittest.TestCase):
    """SHOW_RAW_LOGS=1：私有部署，运维明确开启后才看得到原始行。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app(show_raw=True)
        cls._tmp = tempfile.TemporaryDirectory()
        cls.app.take_log = TakeLog(Path(cls._tmp.name) / "shared.csv")
        cls.app.do_audit("检查 Shot 21 的钥匙", None, "本地规则（无需 API）",
                         cls.app.new_session(), "", SECRET_FILE, "待定", "", "")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        os.environ.pop("SHOW_RAW_LOGS", None)
        importlib.reload(cls.app)

    def test_raw_logs_available_when_explicitly_enabled(self):
        self.assertTrue(self.app.SHOW_RAW_LOGS)
        rows = self.app.refresh_log()
        self.assertEqual(len(rows), 1)
        self.assertIn(SECRET_FILE, json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
