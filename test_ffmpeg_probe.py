"""ffmpeg 二进制不可用时的探测与降级。

用户报的现象（Windows）：图片审查也会抛
`FileNotFoundError: [WinError 2] The system cannot find the file specified`。

根因：旧逻辑只检查 `import imageio_ffmpeg` 成不成功，**没检查二进制到底在不在**。
包能导入不代表 exe 存在——杀毒软件会删它，而 `IMAGEIO_FFMPEG_EXE` 这个环境变量
被 imageio-ffmpeg 直接信任、根本不校验（源码注释原话：
"Dont test it: the user is explicit here!"），指错路径就会在真正抽帧时才炸。
"""

import importlib
import os
import subprocess
import sys
import unittest
from unittest import mock


def reload_video_audit():
    sys.modules.pop("video_audit", None)
    return importlib.import_module("video_audit")


class FfmpegProbeTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        os.environ.pop("IMAGEIO_FFMPEG_EXE", None)
        reload_video_audit()

    def test_bad_exe_path_is_caught_at_import_not_at_first_use(self):
        """这正是用户踩的那条：环境变量指向不存在的文件。"""
        with mock.patch.dict(os.environ, {"IMAGEIO_FFMPEG_EXE": "/nonexistent/ffmpeg.exe"}):
            va = reload_video_audit()
        self.assertFalse(va.VIDEO_SUPPORTED)
        self.assertIn("跑不起来", va.VIDEO_UNAVAILABLE_REASON)
        # 不是抛 WinError 2，而是一句人话
        with self.assertRaises(ValueError) as caught:
            va.sample_video("whatever.mp4")
        self.assertIn("视频审查不可用", str(caught.exception))

    def test_working_ffmpeg_records_the_resolved_path(self):
        """能用的时候，路径在探测阶段就定下来，不在每次抽帧时重新解析。"""
        os.environ.pop("IMAGEIO_FFMPEG_EXE", None)
        va = reload_video_audit()
        if not va.VIDEO_SUPPORTED:
            self.skipTest("本机没有可用的 ffmpeg")
        self.assertTrue(va.FFMPEG_EXE)
        done = subprocess.run([va.FFMPEG_EXE, "-version"], capture_output=True, timeout=20)
        self.assertEqual(done.returncode, 0)

    def test_nonzero_exit_code_also_degrades(self):
        """二进制在、但跑起来报错，也算不可用。"""
        os.environ.pop("IMAGEIO_FFMPEG_EXE", None)
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"")
        with mock.patch("subprocess.run", return_value=fake):
            va = reload_video_audit()
        self.assertFalse(va.VIDEO_SUPPORTED)
        self.assertIn("错误码 1", va.VIDEO_UNAVAILABLE_REASON)


if __name__ == "__main__":
    unittest.main()
