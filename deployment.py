"""Deployment preflight and public-space resource boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image

DEPLOYMENT_CHECK_VERSION = "1.0"
EXIT_DEPLOYMENT_OK = 0
EXIT_DEPLOYMENT_UNSAFE = 40

MAX_PUBLIC_IMAGE_FILES = 10
MAX_PUBLIC_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PUBLIC_IMAGE_EDGE = 1280

TARGETS = ("local", "huggingface", "modelscope")
REQUIRED_FILES = (
    "app.py",
    "canon.json",
    "requirements.txt",
    "docs/deployment-cost.md",
)


@dataclass(frozen=True)
class DeploymentCheck:
    name: str
    status: str
    message: str


def _check(name: str, status: str, message: str) -> DeploymentCheck:
    return DeploymentCheck(name=name, status=status, message=message)


def _integer_limit(
    environment: Mapping[str, str], name: str, default: int, maximum: int
) -> DeploymentCheck:
    raw = environment.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        return _check(name, "FAIL", "must be an integer")
    if value < 1 or value > maximum:
        return _check(name, "FAIL", f"must stay between 1 and {maximum}")
    return _check(name, "PASS", f"capped at {value}")


def _budget_limit(environment: Mapping[str, str]) -> DeploymentCheck:
    raw = environment.get("VIDEO_BUDGET_CNY", "10")
    try:
        value = float(raw)
    except ValueError:
        return _check("VIDEO_BUDGET_CNY", "FAIL", "must be a number")
    if value <= 0 or value > 10:
        return _check("VIDEO_BUDGET_CNY", "FAIL", "must stay above 0 and at most ¥10")
    return _check("VIDEO_BUDGET_CNY", "PASS", f"daily estimate capped at ¥{value:.2f}")


def probe_ffmpeg() -> DeploymentCheck:
    """Run the packaged ffmpeg binary once without exposing its host path."""
    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001 - every probe failure degrades safely
        return _check(
            "ffmpeg",
            "WARN",
            f"video audit disabled ({type(exc).__name__}); image audit remains available",
        )
    if completed.returncode != 0:
        return _check(
            "ffmpeg",
            "WARN",
            "video audit disabled (probe returned non-zero); image audit remains available",
        )
    return _check("ffmpeg", "PASS", "video frame sampling available")


def inspect_deployment(
    repository_root: str | Path,
    *,
    target: str = "local",
    environment: Mapping[str, str] | None = None,
    ffmpeg_check: DeploymentCheck | None = None,
) -> dict[str, Any]:
    """Evaluate a deployment without printing secrets or absolute host paths."""
    if target not in TARGETS:
        raise ValueError(f"target must be one of: {', '.join(TARGETS)}")
    root = Path(repository_root)
    env = dict(os.environ if environment is None else environment)
    checks: list[DeploymentCheck] = []

    checks.append(
        _check(
            "python",
            "PASS" if sys.version_info >= (3, 10) else "FAIL",
            f"{sys.version_info.major}.{sys.version_info.minor}; requires 3.10+",
        )
    )
    missing = [path for path in REQUIRED_FILES if not (root / path).is_file()]
    checks.append(
        _check(
            "repository_files",
            "FAIL" if missing else "PASS",
            "missing: " + ", ".join(missing) if missing else "required files present",
        )
    )

    data_directory = root / "data"
    writable_target = data_directory if data_directory.exists() else root
    writable = os.access(writable_target, os.W_OK)
    checks.append(
        _check(
            "ephemeral_log_storage",
            "PASS" if writable else "FAIL",
            "writable; production log is non-persistent"
            if writable
            else "repository data path is not writable",
        )
    )
    checks.append(
        _check(
            "public_log_privacy",
            "FAIL" if env.get("SHOW_RAW_LOGS") == "1" else "PASS",
            "SHOW_RAW_LOGS must not be 1 on a public deployment"
            if env.get("SHOW_RAW_LOGS") == "1"
            else "raw shared logs are not exposed",
        )
    )

    model_enabled = bool(env.get("LLM_API_KEY") and env.get("LLM_MODEL"))
    checks.append(
        _check(
            "multimodal_api",
            "WARN" if model_enabled else "PASS",
            "optional paid model configured; provider billing applies"
            if model_enabled
            else "not configured; deterministic CPU path has zero API cost",
        )
    )

    video_enabled = env.get("ENABLE_VIDEO_GENERATION") == "1"
    if not video_enabled:
        checks.append(
            _check(
                "paid_video_generation",
                "PASS",
                "disabled; no video-generation API spend is possible",
            )
        )
    else:
        required_secrets = ("MINIMAX_VIDEO_API_KEY", "VIDEO_ACCESS_CODE")
        missing_secrets = [name for name in required_secrets if not env.get(name)]
        provider_valid = env.get("VIDEO_PROVIDER") == "minimax"
        checks.append(
            _check(
                "paid_video_generation",
                "FAIL" if missing_secrets or not provider_valid else "WARN",
                "incomplete paid-video configuration"
                if missing_secrets or not provider_valid
                else "enabled behind access code; provider billing applies",
            )
        )
        checks.append(_integer_limit(env, "MAX_ACTIVE_VIDEO_JOBS", 1, 1))
        checks.append(_integer_limit(env, "MAX_DAILY_VIDEO_JOBS", 3, 3))
        checks.append(_budget_limit(env))

    checks.append(ffmpeg_check or probe_ffmpeg())
    serialized = [asdict(item) for item in checks]
    unsafe = any(item.status == "FAIL" for item in checks)
    return {
        "deployment_check_version": DEPLOYMENT_CHECK_VERSION,
        "target": target,
        "status": "UNSAFE" if unsafe else "READY",
        "exit_code": EXIT_DEPLOYMENT_UNSAFE if unsafe else EXIT_DEPLOYMENT_OK,
        "cost_boundary": {
            "cpu_validator_api_cost": "zero",
            "multimodal_api": "enabled-paid" if model_enabled else "disabled",
            "video_generation_api": "enabled-paid" if video_enabled else "disabled",
        },
        "public_limits": {
            "max_image_files": MAX_PUBLIC_IMAGE_FILES,
            "max_image_bytes": MAX_PUBLIC_IMAGE_BYTES,
            "max_image_edge_px": MAX_PUBLIC_IMAGE_EDGE,
        },
        "checks": serialized,
    }


def render_deployment_check(report: dict[str, Any]) -> str:
    icons = {"PASS": "✓", "WARN": "△", "FAIL": "✗"}
    lines = [
        f"Continuity Agent · deployment preflight ({report['target']})",
        f"Status: {report['status']}",
        "",
    ]
    lines.extend(
        f"{icons[item['status']]} {item['name']}: {item['message']}"
        for item in report["checks"]
    )
    lines.extend(
        [
            "",
            "Public image limits: 10 files · 10MB each · 1280px longest edge",
            "Default validator API cost: zero",
        ]
    )
    return "\n".join(lines)


def validate_public_image(value: Any) -> None:
    """Reject oversized public image inputs before analysis or API encoding."""
    if value is None:
        return
    if isinstance(value, (str, os.PathLike)):
        path = Path(value)
        if path.stat().st_size > MAX_PUBLIC_IMAGE_BYTES:
            raise ValueError("每张图片不能超过 10MB。")
        with Image.open(path) as image:
            width, height = image.size
    elif isinstance(value, Image.Image):
        width, height = value.size
    else:
        raise TypeError("无法读取上传图片。")
    if max(width, height) > MAX_PUBLIC_IMAGE_EDGE:
        raise ValueError("图片最长边不能超过 1280px。")


def validate_public_image_batch(values: Sequence[Any] | None) -> None:
    if not values:
        return
    if len(values) > MAX_PUBLIC_IMAGE_FILES:
        raise ValueError("每批最多 10 张图片。")
    for value in values:
        validate_public_image(value)
