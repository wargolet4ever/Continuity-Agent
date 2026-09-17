"""没装 imageio-ffmpeg 的机器上，应用必须还能起来。

背景：video_audit 需要 ffmpeg，图片审查不需要。早期版本在顶层无条件
`import imageio_ffmpeg`，任何人 clone 下来忘了装依赖，看到的是一个
ModuleNotFoundError 的 traceback，整个应用起不来——包括跟视频毫无关系的
图片审查。

这组测试把 imageio_ffmpeg 从 sys.modules 里屏蔽掉再重新导入，覆盖的正是
那台没装依赖的机器。
"""

import builtins
import importlib
import sys
import unittest


class NoFfmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = {
            name: module
            for name, module in sys.modules.items()
            if name in {"video_audit", "app"} or name.startswith("imageio_ffmpeg")
        }
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.startswith("imageio_ffmpeg"):
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        for name in list(sys.modules):
            if name.startswith("imageio_ffmpeg") or name in {"video_audit", "app"}:
                del sys.modules[name]
        builtins.__import__ = blocked
        cls._real_import = real_import
        cls.video_audit = importlib.import_module("video_audit")
        cls.app = importlib.import_module("app")

    @classmethod
    def tearDownClass(cls):
        builtins.__import__ = cls._real_import
        for name in ("app", "video_audit"):
            sys.modules.pop(name, None)
        sys.modules.update(cls._saved)
        importlib.import_module("video_audit")
        importlib.import_module("app")

    def test_app_imports_without_ffmpeg(self):
        """最关键的一条：缺依赖不是崩溃，是降级。"""
        self.assertFalse(self.video_audit.VIDEO_SUPPORTED)
        self.assertIn("imageio-ffmpeg", self.video_audit.VIDEO_UNAVAILABLE_REASON)
        self.assertIsNotNone(self.app.demo)

    def test_image_audit_still_works(self):
        """视频那一半坏了，图片这一半必须照常。"""
        from PIL import Image

        head, _, detail, *_ = self.app.do_audit(
            "检查 Shot 21 的钥匙插槽位置对不对",
            Image.new("RGB", (64, 64), (60, 70, 80)),
            "本地规则（无需 API）",
            self.app.new_session(), "", "", "待定", "", "",
        )
        self.assertIn("重新生成", head)
        self.assertIn("RULE + PIXEL CHECK", detail)

    def test_demo_buttons_degrade_instead_of_crashing(self):
        """演示按钮依赖示例视频；没 ffmpeg 就退回纯文字审查，而不是抛异常。"""
        _, head, _, detail, *_ = self.app.run_demo("B", self.app.new_session())
        self.assertIn("重新生成", head)
        self.assertIn("我没看过画面", head)
        self.assertIn("USER-REPORTED", detail)
        self.assertIn("imageio-ffmpeg", detail)

    def test_no_video_callback_is_bound(self):
        """不展示无法使用的能力：视频回调不进事件表。"""
        bound = [
            fn.fn.__name__
            for fn in self.app.demo.fns.values()
            if getattr(fn, "fn", None) is not None and hasattr(fn.fn, "__name__")
        ]
        self.assertNotIn("preview_video", bound)
        self.assertNotIn("demo_media", bound)

    def test_sample_video_reports_the_real_reason(self):
        """直接调用也要说人话，而不是 NameError。"""
        with self.assertRaises(ValueError) as caught:
            self.video_audit.sample_video("anything.mp4")
        self.assertIn("imageio-ffmpeg", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
