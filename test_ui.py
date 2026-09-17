"""In-process Gradio route/callback checks. No remote model calls."""

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from gradio.routes import App
from PIL import Image

import app as app_module
from app import (
    CUT_STATE,
    PROD_STATE,
    canon_health,
    demo,
    do_audit,
    do_create,
    narrative_health,
    new_session,
    run_demo,
)
import orchestrator as orch
from app import DECISION_PLAIN

PLAIN = {name: head for name, (_, head, _) in DECISION_PLAIN.items()}
from take_log import FIELDS, TakeLog


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # run_audit now auto-logs; keep tests out of the real production CSV.
        cls._tmp = tempfile.TemporaryDirectory()
        cls._real_log = app_module.take_log
        app_module.take_log = TakeLog(Path(cls._tmp.name) / "takes.csv")

    @classmethod
    def tearDownClass(cls):
        app_module.take_log = cls._real_log
        cls._tmp.cleanup()

    def test_routes(self):
        with TestClient(App.create_app(demo)) as client:
            for route in ["/", "/config", "/gradio_api/info"]:
                with self.subTest(route=route):
                    self.assertEqual(client.get(route).status_code, 200)

    def test_upload_works_without_launch(self):
        """回归：上传素材曾经在托管环境 500。

        `Blocks.max_file_size` 只在 `demo.launch()` 里赋值，而 `/gradio_api/upload`
        会无条件读它。平台若是 import 本文件再自行挂载 demo（不执行 __main__ 分支），
        页面一切正常、例子能点，唯独上传抛 AttributeError。这里刻意**不调用
        launch()**，直接建 app，覆盖的正是那条路径。图片和视频走同一个上传端点，
        所以两种都要覆盖。
        """
        self.assertTrue(
            hasattr(demo, "max_file_size"),
            "max_file_size 缺失——未 launch 的托管环境下上传必然 500",
        )
        uploads = []
        for mode, fmt, ctype, name in [
            ("RGB", "PNG", "image/png", "a.png"),
            ("RGBA", "PNG", "image/png", "b.png"),
            ("L", "PNG", "image/png", "c.png"),
            ("P", "PNG", "image/png", "d.png"),
            ("RGB", "JPEG", "image/jpeg", "e.jpg"),
            ("RGB", "WEBP", "image/webp", "f.webp"),
        ]:
            buffer = io.BytesIO()
            Image.new(mode, (640, 360)).save(buffer, format=fmt)
            uploads.append((f"{mode}/{fmt}", name, buffer.getvalue(), ctype))
        uploads.append(
            ("MP4", "clip.mp4",
             app_module.example_video_path("A").read_bytes(), "video/mp4")
        )

        with TestClient(App.create_app(demo)) as client:
            for label, name, payload, ctype in uploads:
                with self.subTest(kind=label):
                    response = client.post(
                        "/gradio_api/upload",
                        files={"files": (name, payload, ctype)},
                    )
                    self.assertEqual(response.status_code, 200, response.text[:200])
                    if label == "MP4":
                        continue
                    # 上传成功还不够，审查也要能吃下这个文件
                    head, issues, detail, *_ = do_audit(
                        "检查 Shot 21 的钥匙插槽位置对不对",
                        Image.open(response.json()[0]),
                        "本地规则（无需 API）",
                        new_session(), "", "", "待定", "", "",
                    )
                    # 面上说人话，代号只在折叠起来的详细报告里
                    self.assertIn("重新生成", head)
                    self.assertNotIn(orch.EVIDENCE_RULE_PIXEL, head)
                    self.assertIn(orch.EVIDENCE_RULE_PIXEL, detail)

    def test_example_videos_are_present_in_the_repo(self):
        """两个演示按钮依赖仓库里的真实视频；搬了目录也要还能找到。"""
        for key in ("A", "B"):
            with self.subTest(demo=key):
                path = app_module.example_video_path(key)
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 10_000)

    def test_demo_buttons_run_audit_in_one_click(self):
        for key, decision, rule in [("A", "REGENERATE", "LOC-B06"),
                                    ("B", "REGENERATE", "SHOT-21A")]:
            with self.subTest(demo=key):
                _, head, _, detail, _, trace, report, _ = run_demo(
                    key, new_session()
                )
                self.assertIn(PLAIN[decision], head)
                self.assertIn(decision, detail)
                self.assertIn(rule, detail)
                self.assertEqual(len(trace), 4)
                self.assertIn(orch.EVIDENCE_VIDEO_RULE, detail)
                self.assertTrue(Path(report["value"]).is_file())

    def test_demo_does_not_write_production_log(self):
        before = len(app_module.take_log.rows())
        run_demo("A", new_session())
        self.assertEqual(len(app_module.take_log.rows()), before)

    def test_audit_without_image_is_labelled_user_reported(self):
        head, _, detail, _, _, report, _ = do_audit(
            "检查 Shot 21 的钥匙插槽位置对不对", None, "本地规则（无需 API）",
            new_session(), "即梦", "s21.mp4", "待定", "", "")
        # 面上说人话，并且明说「我没看过画面」
        self.assertIn("重新生成", head)
        self.assertIn("我没看过画面", head)
        self.assertNotIn(orch.EVIDENCE_USER_REPORTED, head)
        # 代号和「系统没有看过任何画面」在详细报告里
        self.assertIn("REGENERATE", detail)
        self.assertIn(orch.EVIDENCE_USER_REPORTED, detail)
        self.assertIn("系统没有看过任何画面", detail)
        # 「证据来源：」那一行必须是 USER-REPORTED；正文里提到 VISUAL AUDIT
        # 是在告诉用户怎样才能升级，不算标签泄漏。
        label_line = next(l for l in detail.split("\n") if "证据来源：" in l)
        self.assertIn(orch.EVIDENCE_USER_REPORTED, label_line)
        self.assertNotIn(orch.EVIDENCE_VISUAL, label_line)
        self.assertTrue(Path(report["value"]).is_file())

    def test_audit_with_image_is_not_user_reported(self):
        image = Image.new("RGB", (32, 32), (60, 70, 80))
        _, _, head, *_ = do_audit("检查 Shot 21 的钥匙", image, "本地规则（无需 API）",
                                  new_session(), "", "", "待定", "", "")
        label_line = next(l for l in head.split("\n") if "证据来源：" in l)
        self.assertIn(orch.EVIDENCE_RULE_PIXEL, label_line)
        self.assertNotIn(orch.EVIDENCE_USER_REPORTED, label_line)

    def test_audit_auto_logs_with_new_fields(self):
        before = len(app_module.take_log.rows())
        do_audit("看看镜头19的小屏", None, "本地规则（无需 API）", new_session(),
                 "即梦", "s19.png", "不采纳", "文字/UI问题", "")
        rows = app_module.take_log.rows()
        self.assertEqual(len(rows), before + 1)
        header = FIELDS[:-1]
        row = dict(zip(header, rows[0]))
        self.assertEqual(row["run_mode"], "AUDIT")
        self.assertEqual(row["evidence_source"], orch.EVIDENCE_USER_REPORTED)
        self.assertTrue(row["duration_ms"])
        self.assertEqual(row["model_source"], "本地规则")

    def test_create_mounts_draft_canon_into_session(self):
        head, plan, prompts, audit, trace, filebox, state = do_create(
            "末班地铁上只有她看得见第八节车厢", 4, new_session())
        self.assertEqual(len(plan["value"]), 4)
        self.assertEqual(len(trace), 7)
        self.assertIn("AWAITING EXTERNAL GENERATION", head["value"])
        self.assertTrue(state["canon_label"].startswith("草案"))
        self.assertEqual(state["shot_ids"], ["1", "2", "3", "4"])
        self.assertTrue(state["package_path"].endswith(".zip"))

    def test_create_rejects_empty_idea(self):
        head, plan, prompts, audit, trace, filebox, state = do_create(
            "  ", 5, new_session()
        )
        self.assertIn("⚠️", head["value"])
        # Dataframe outputs must receive a row list.  Returning an empty string
        # makes Gradio treat it as a CSV filepath and raises FileNotFoundError.
        self.assertEqual(audit, [])
        self.assertEqual(trace, [])

    def test_empty_create_survives_gradio_postprocessing(self):
        """Reproduce the deployed failure, including Dataframe postprocessing."""
        create_fn_index = next(
            index
            for index, block_fn in demo.fns.items()
            if getattr(getattr(block_fn, "fn", None), "__name__", "") == "do_create"
        )
        result = asyncio.run(
            demo.process_api(
                create_fn_index,
                ["", 5, new_session()],
                state=None,
                request=None,
                iterator=None,
            )
        )
        self.assertIn("请先输入一句创意", result["data"][0]["value"])
        self.assertEqual(result["data"][3]["data"], [])
        self.assertEqual(result["data"][4]["data"], [])

    def test_health(self):
        self.assertEqual(canon_health(new_session())[1], [])

    def test_narrative_states(self):
        summary, rows = narrative_health(CUT_STATE)
        self.assertIn("因果链完整", summary)
        self.assertEqual(rows, [])
        summary, rows = narrative_health(PROD_STATE)
        self.assertIn("制作中的真实发现", summary)
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()


class WorkflowLinkTests(unittest.TestCase):
    """CREATE 和 AUDIT 是一条流水线的两头，界面必须说出这件事。

    做完分镜之后，审查用的应该是刚生成的那份草案规则，而不是示例规则——
    这条线一直是真的，但在界面上看不见，用户因此以为两个功能毫不相干。
    """

    @classmethod
    def setUpClass(cls):
        # do_audit 会写日志，别污染真正的生产 CSV
        cls._tmp = tempfile.TemporaryDirectory()
        cls._real_log = app_module.take_log
        app_module.take_log = TakeLog(Path(cls._tmp.name) / "takes.csv")

    @classmethod
    def tearDownClass(cls):
        app_module.take_log = cls._real_log
        cls._tmp.cleanup()

    def test_audit_uses_the_canon_that_create_just_made(self):
        state = new_session()
        self.assertIn("成片", app_module.rules_note(state))

        *_, state = do_create("末班地铁上只有她看得见第八节车厢", 4, state)
        note = app_module.rules_note(state)
        self.assertIn("正在用你刚做的这份规则", note)

        # 不只是文案：审查真的换了规则源
        _, _, detail, _, _, _, state = do_audit(
            "检查 Shot 2", None, "本地规则（无需 API）",
            state, "", "", "待定", "", "",
        )
        self.assertNotIn("Canon 1.4", detail)

    def test_fresh_session_falls_back_to_the_sample_canon(self):
        """换个会话不能串到别人的草案上。"""
        self.assertIn("示例规则", app_module.rules_note(new_session()))


class ProxyEnvTests(unittest.TestCase):
    """Windows + 系统代理下，Gradio 的本机自检会被代理拦成 503，应用直接起不来。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_loopback_is_added_when_unset(self):
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        app_module.bypass_proxy_for_localhost()
        for name in ("NO_PROXY", "no_proxy"):
            self.assertIn("127.0.0.1", os.environ[name])
            self.assertIn("localhost", os.environ[name])

    def test_existing_entries_are_kept_not_clobbered(self):
        """用户自己配的 NO_PROXY 不能被我们冲掉。"""
        os.environ["NO_PROXY"] = "internal.corp,10.0.0.0/8"
        app_module.bypass_proxy_for_localhost()
        self.assertIn("internal.corp", os.environ["NO_PROXY"])
        self.assertIn("10.0.0.0/8", os.environ["NO_PROXY"])
        self.assertIn("localhost", os.environ["NO_PROXY"])

    def test_running_twice_does_not_duplicate(self):
        os.environ["NO_PROXY"] = "localhost"
        app_module.bypass_proxy_for_localhost()
        app_module.bypass_proxy_for_localhost()
        self.assertEqual(os.environ["NO_PROXY"].count("localhost"), 1)
