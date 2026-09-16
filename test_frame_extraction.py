"""Focused regression tests for the CPU-only ffmpeg frame extractor."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from video_audit import (
    FFMPEG_EXE,
    MAX_FRAME_HEIGHT,
    MAX_FRAME_WIDTH,
    VIDEO_SUPPORTED,
    sample_video,
)


@unittest.skipUnless(VIDEO_SUPPORTED, "ffmpeg is unavailable")
class FrameExtractionTests(unittest.TestCase):
    def _make_clip(self, directory: str, size: str = "320x180") -> Path:
        path = Path(directory) / "中文 shot sample.mp4"
        command = [
            FFMPEG_EXE,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=6:duration=1",
            "-an",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ]
        completed = subprocess.run(
            command, capture_output=True, check=False, timeout=30
        )
        if completed.returncode != 0:
            self.fail(completed.stderr.decode("utf-8", errors="replace"))
        return path

    def test_unicode_and_space_path_extracts_ordered_rgb_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = self._make_clip(directory)
            sample = sample_video(clip, frame_count=5)

        self.assertEqual(len(sample.frames), 5)
        self.assertEqual(sample.timestamps_s, sorted(sample.timestamps_s))
        self.assertTrue(all(frame.mode == "RGB" for frame in sample.frames))
        self.assertTrue(all(frame.size == (320, 180) for frame in sample.frames))

    def test_large_input_is_scaled_inside_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = self._make_clip(directory, size="2560x1440")
            sample = sample_video(clip, frame_count=3)

        self.assertEqual(len(sample.frames), 3)
        self.assertTrue(
            all(
                width <= MAX_FRAME_WIDTH and height <= MAX_FRAME_HEIGHT
                for width, height in (frame.size for frame in sample.frames)
            )
        )
        self.assertTrue(all(frame.size == (1280, 720) for frame in sample.frames))

    def test_frame_count_is_clamped_to_supported_range(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = self._make_clip(directory)
            minimum = sample_video(clip, frame_count=1)
            maximum = sample_video(clip, frame_count=99)

        self.assertEqual(len(minimum.frames), 3)
        self.assertEqual(len(maximum.frames), 7)

    def test_corrupt_video_reports_a_user_facing_error(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory) / "broken.mp4"
            clip.write_bytes(b"not a video")
            with self.assertRaisesRegex(ValueError, "无法读取视频时长或编码"):
                sample_video(clip)


if __name__ == "__main__":
    unittest.main(verbosity=2)
