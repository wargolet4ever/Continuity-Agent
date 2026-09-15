"""Short-video validation and deterministic frame sampling for AUDIT.

The public callback never keeps the uploaded video in global state.  Gradio owns
the temporary upload; this module decodes five ordered stills in memory and
returns only those stills to the caller.
"""

from __future__ import annotations

import io
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

# 视频抽帧要 ffmpeg，图片审查不要。缺了它就只关掉视频那一半，
# 而不是让整个应用起不来——有人 clone 下来忘了装依赖，看到 traceback
# 和看到「视频功能不可用，图片审查可以用」，是两种结局。
#
# 注意：**能 import 这个包 ≠ ffmpeg 二进制可用。** 包里带的 exe 可能被杀毒软件
# 删掉，pip 也可能装到不含二进制的变体上；而 `IMAGEIO_FFMPEG_EXE` 这个环境变量
# 被 imageio-ffmpeg 直接信任、根本不校验，指错地方就会在真正抽帧时抛 WinError 2。
# 所以这里必须**真的把二进制跑一次**，而不是只看 import 成没成功。
FFMPEG_EXE: str | None = None


def _probe_ffmpeg() -> tuple[bool, str]:
    try:
        import imageio_ffmpeg as _mod
    except Exception:  # noqa: BLE001
        return False, (
            "未安装 imageio-ffmpeg，视频审查不可用；图片审查不受影响。"
            "装上即可开启：pip install -r requirements.txt"
        )
    globals()["imageio_ffmpeg"] = _mod
    try:
        exe = _mod.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        return False, f"找不到 ffmpeg（{exc}），视频审查不可用；图片审查不受影响。"
    try:
        done = subprocess.run([exe, "-version"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, (
            f"ffmpeg 找到了但跑不起来（{type(exc).__name__}），视频审查不可用；"
            "图片审查不受影响。常见原因：杀毒软件删掉了它，或 IMAGEIO_FFMPEG_EXE "
            "指向了不存在的路径。诊断：python diagnose.py"
        )
    if done.returncode != 0:
        return False, (
            f"ffmpeg 返回错误码 {done.returncode}，视频审查不可用；图片审查不受影响。"
            "诊断：python diagnose.py"
        )
    globals()["FFMPEG_EXE"] = exe
    return True, ""


VIDEO_SUPPORTED, VIDEO_UNAVAILABLE_REASON = _probe_ffmpeg()


MAX_VIDEO_DURATION_S = 20.0
MAX_VIDEO_SIZE_MB = 50
DEFAULT_FRAME_COUNT = 5
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}


@dataclass
class VideoSample:
    path: Path
    duration_s: float
    timestamps_s: list[float]
    frames: list[Image.Image]

    @property
    def gallery(self) -> list[tuple[Image.Image, str]]:
        return [
            (frame, f"{timestamp:.2f}s")
            for frame, timestamp in zip(self.frames, self.timestamps_s)
        ]


def _video_path(value: Any) -> Path:
    if isinstance(value, (tuple, list)) and value:
        value = value[0]
    if isinstance(value, dict):
        value = value.get("path") or value.get("name")
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("请先上传一段视频。")
    path = Path(value)
    if not path.is_file():
        raise ValueError("上传的视频文件不可读取，请重新上传。")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError("仅支持 MP4、MOV、M4V 或 WebM 视频。")
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > MAX_VIDEO_SIZE_MB:
        raise ValueError(f"视频不能超过 {MAX_VIDEO_SIZE_MB}MB（当前 {size_mb:.1f}MB）。")
    return path


def _duration(path: Path) -> float:
    try:
        _, duration = imageio_ffmpeg.count_frames_and_secs(str(path))
    except FileNotFoundError as exc:
        # ffmpeg 二进制在启动之后被删了／被杀软隔离了
        raise ValueError(
            "ffmpeg 不见了，视频功能暂时不可用（图片审查照常）。诊断：python diagnose.py"
        ) from exc
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise ValueError("无法读取视频时长或编码，请转为 H.264 MP4 后重试。") from exc
    duration = float(duration or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("视频时长无效。")
    if duration > MAX_VIDEO_DURATION_S:
        raise ValueError(
            f"视频最长 {MAX_VIDEO_DURATION_S:.0f} 秒（当前 {duration:.1f} 秒）；"
            "请先裁出需要审查的单个镜头。"
        )
    return duration


def _extract_frame(path: Path, timestamp: float) -> Image.Image:
    command = [
        FFMPEG_EXE,
        "-v",
        "error",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=25,
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "ffmpeg 不见了，视频功能暂时不可用（图片审查照常）。诊断：python diagnose.py"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("视频解码失败，请转为 H.264 MP4 后重试。") from exc
    if completed.returncode or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace")[-160:]
        raise ValueError(f"视频关键帧提取失败。{detail}")
    try:
        frame = Image.open(io.BytesIO(completed.stdout)).convert("RGB")
        frame.thumbnail((1280, 720))
        return frame.copy()
    except (OSError, ValueError) as exc:
        raise ValueError("提取出的关键帧无法读取。") from exc


def sample_video(value: Any, frame_count: int = DEFAULT_FRAME_COUNT) -> VideoSample:
    """Decode evenly spaced, ordered frames from one short uploaded clip."""

    if not VIDEO_SUPPORTED:
        raise ValueError(VIDEO_UNAVAILABLE_REASON)
    path = _video_path(value)
    duration = _duration(path)
    count = max(3, min(7, int(frame_count)))
    # Avoid asking the decoder for the exact container end timestamp.
    end = max(0.0, duration - min(0.08, duration / 20))
    timestamps = [round(end * index / (count - 1), 3) for index in range(count)]
    frames = [_extract_frame(path, timestamp) for timestamp in timestamps]
    return VideoSample(path, round(duration, 3), timestamps, frames)


def preview_video(value: Any):
    """Gradio change callback: show sampled frames without writing production logs."""

    if value is None:
        return [], ""
    try:
        sample = sample_video(value)
    except ValueError as exc:
        return [], f"### ⚠️ {exc}"
    return (
        sample.gallery,
        f"已抽取 {len(sample.frames)} 帧："
        + " / ".join(f"{value:.2f}s" for value in sample.timestamps_s)
        + "。这些是同一段素材的时间序列，不是不同镜头。",
    )
