"""视频生成 Provider、状态机与安全护栏。

MiniMax Hailuo 使用 V1 异步流程：提交任务 → 查询状态 → 取得 file_id → 获取下载
地址。功能默认关闭；只有显式设置 ENABLE_VIDEO_GENERATION=1、选择 minimax 并配置
独立的视频 API Key 时，应用才会显示受控生成面板。
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar
from uuid import uuid4

# ── 生成状态机 ─────────────────────────────────────────────────────────
# CREATE 产出的镜头从 AWAITING_EXTERNAL_GENERATION 出发；接入 Provider 之后
# 沿下面的转移图前进。未接入时永远停在起点。
AWAITING = "AWAITING_EXTERNAL_GENERATION"
SUBMITTED = "SUBMITTED"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
HUMAN_REVIEW = "HUMAN_REVIEW"

TRANSITIONS: dict[str, tuple[str, ...]] = {
    AWAITING: (SUBMITTED,),
    SUBMITTED: (RUNNING, FAILED, HUMAN_REVIEW),
    RUNNING: (SUCCEEDED, FAILED, HUMAN_REVIEW),
    SUCCEEDED: (AWAITING,),        # 内容重新生成 = 回到起点，需用户二次确认
    FAILED: (AWAITING, HUMAN_REVIEW),
    HUMAN_REVIEW: (AWAITING,),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, ())


# ── 错误分类 ───────────────────────────────────────────────────────────
# 只有 TRANSIENT 才允许 API 请求重试；其余一律停止并如实上报。
TRANSIENT = "transient"          # 限流、超时、5xx —— 可重试
ENTITLEMENT = "entitlement"      # 套餐/额度/权限不足 —— 重试无意义
REQUEST = "request"              # 参数或结构错误 —— 重试无意义
AUTH = "auth"                    # 密钥无效
UNKNOWN = "unknown"


@dataclass
class ProviderError:
    """跨平台统一的错误表示。normalize_error 的产物。"""

    kind: str
    message: str
    http_status: int | None = None
    provider_code: str | None = None
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "http_status": self.http_status,
            "provider_code": self.provider_code,
            "retryable": self.retryable,
        }


@dataclass
class GenerationTask:
    """一次生成任务。字段与 take_log / trace 的既有列对齐。"""

    shot_id: str
    prompt: str
    duration: int
    aspect_ratio: str
    resolution: str
    model: str
    provider: str
    reference_images: list[str] = field(default_factory=list)
    task_id: str | None = None
    file_id: str | None = None
    status: str = AWAITING
    video_url: str | None = None
    retries: int = 0
    error: ProviderError | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "error"}
        data["error"] = self.error.as_dict() if self.error else None
        return data


class VideoGenerationProvider(ABC):
    """所有视频平台适配器的统一契约。

    实现类只需要四个方法。凭据、模型名与端点一律从环境变量读取，
    不得写死在代码里。
    """

    name: str = "abstract"

    # 每个实现自己声明需要哪些环境变量，供 available() 统一校验。
    required_env: tuple[str, ...] = ()

    @classmethod
    def available(cls) -> bool:
        return all(os.getenv(key) for key in cls.required_env)

    @abstractmethod
    def submit(self, task: GenerationTask) -> GenerationTask:
        """提交生成任务。成功时填入 task_id 并把 status 置为 SUBMITTED。

        必须立即返回，不得在此阻塞等待生成完成。
        """

    @abstractmethod
    def poll(self, task: GenerationTask) -> GenerationTask:
        """查询异步任务状态，更新 status 为 RUNNING / SUCCEEDED / FAILED。"""

    @abstractmethod
    def fetch_result(self, task: GenerationTask) -> GenerationTask:
        """取回最终视频地址或文件，填入 video_url。"""

    @abstractmethod
    def normalize_error(self, exc: Exception) -> ProviderError:
        """把平台各自的错误形态归一成 ProviderError。

        判定 retryable 的唯一依据是 kind == TRANSIENT。
        """


class NullProvider(VideoGenerationProvider):
    """占位实现。当前唯一注册的 Provider —— 它什么也不做。

    存在的意义是让状态机和 UI 有一个明确的"未接入"分支，
    而不是在代码里散落 if provider is None。
    """

    name = "none"
    required_env = ()

    def submit(self, task: GenerationTask) -> GenerationTask:
        task.status = AWAITING
        task.error = ProviderError(
            kind=ENTITLEMENT,
            message="未接入任何视频生成 Provider；本工具不生成视频。",
            retryable=False,
        )
        return task

    def poll(self, task: GenerationTask) -> GenerationTask:
        return task

    def fetch_result(self, task: GenerationTask) -> GenerationTask:
        return task

    def normalize_error(self, exc: Exception) -> ProviderError:
        return ProviderError(kind=UNKNOWN, message=str(exc), retryable=False)


class MiniMaxAPIError(Exception):
    """保留 MiniMax 的 HTTP 状态和业务码，但不包含 Authorization Header。"""

    def __init__(self, message: str, http_status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code


class MiniMaxHailuoProvider(VideoGenerationProvider):
    """MiniMax Hailuo V1 视频适配器。

    submit 不自动重试：POST 超时后服务端可能已经创建了付费任务，盲目重发可能
    产生重复费用。poll/fetch 是幂等 GET，临时错误最多重试两次。
    """

    name = "minimax"
    required_env = ("MINIMAX_VIDEO_API_KEY",)
    retryable_statuses: ClassVar[set[int]] = {408, 409, 425, 429, 500, 502, 503, 504}
    max_get_retries = 2

    def __init__(self):
        self.api_key = os.environ["MINIMAX_VIDEO_API_KEY"]
        self.base_url = os.getenv("MINIMAX_VIDEO_BASE_URL", "https://api.minimaxi.com").rstrip("/")
        self.timeout = float(os.getenv("VIDEO_HTTP_TIMEOUT_SEC", "30"))

    def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, retries: int = 0,
    ) -> tuple[dict[str, Any], int]:
        query = "?" + urllib.parse.urlencode(params) if params else ""
        url = f"{self.base_url}{path}{query}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        attempts = 0
        while True:
            try:
                request = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
                base = parsed.get("base_resp") or {}
                if int(base.get("status_code", 0) or 0) != 0:
                    raise MiniMaxAPIError(
                        base.get("status_msg") or "MiniMax 返回业务错误",
                        code=str(base.get("status_code")),
                    )
                return parsed, attempts
            except urllib.error.HTTPError as exc:
                message = f"MiniMax HTTP {exc.code}"
                try:
                    data = json.loads(exc.read().decode("utf-8"))
                    base = data.get("base_resp") or {}
                    message = base.get("status_msg") or data.get("message") or message
                    code = str(base.get("status_code") or data.get("code") or "") or None
                except Exception:
                    code = None
                if attempts < retries and exc.code in self.retryable_statuses:
                    attempts += 1
                    continue
                raise MiniMaxAPIError(message, http_status=exc.code, code=code) from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempts < retries:
                    attempts += 1
                    continue
                raise MiniMaxAPIError(type(exc).__name__, http_status=None) from exc

    def submit(self, task: GenerationTask) -> GenerationTask:
        payload: dict[str, Any] = {
            "model": task.model,
            "prompt": task.prompt[:2000],
            "duration": task.duration,
            "resolution": task.resolution,
            "prompt_optimizer": False,
            "aigc_watermark": False,
        }
        if task.reference_images:
            payload["first_frame_image"] = task.reference_images[0]
        elif task.model.endswith("Fast"):
            task.status = HUMAN_REVIEW
            task.error = ProviderError(
                kind=REQUEST,
                message="MiniMax-Hailuo-2.3-Fast 只支持图生视频，请先上传首帧。",
            )
            return task
        try:
            data, retries = self._request("POST", "/v1/video_generation", payload=payload)
            task.retries += retries
            task.task_id = str(data["task_id"])
            task.status = SUBMITTED
        except Exception as exc:
            task.error = self.normalize_error(exc)
            task.status = HUMAN_REVIEW if task.error.kind in {AUTH, ENTITLEMENT, UNKNOWN} else FAILED
        return task

    def poll(self, task: GenerationTask) -> GenerationTask:
        if not task.task_id:
            task.status = HUMAN_REVIEW
            task.error = ProviderError(kind=REQUEST, message="缺少 task_id，无法查询任务。")
            return task
        try:
            data, retries = self._request(
                "GET", "/v1/query/video_generation",
                params={"task_id": task.task_id}, retries=self.max_get_retries,
            )
            task.retries += retries
            status = data.get("status")
            if status in {"Preparing", "Queueing", "Processing"}:
                task.status = RUNNING
            elif status == "Success":
                task.status = SUCCEEDED
                task.file_id = str(data["file_id"])
            elif status == "Fail":
                task.status = FAILED
                task.error = ProviderError(
                    kind=UNKNOWN,
                    message=data.get("error_message") or "MiniMax 视频生成失败。",
                )
            else:
                task.status = HUMAN_REVIEW
                task.error = ProviderError(kind=UNKNOWN, message=f"未知任务状态：{status}")
        except Exception as exc:
            task.error = self.normalize_error(exc)
            task.status = HUMAN_REVIEW
        return task

    def fetch_result(self, task: GenerationTask) -> GenerationTask:
        if not task.file_id:
            task.status = HUMAN_REVIEW
            task.error = ProviderError(kind=REQUEST, message="缺少 file_id，无法取得视频。")
            return task
        try:
            data, retries = self._request(
                "GET", "/v1/files/retrieve",
                params={"file_id": task.file_id}, retries=self.max_get_retries,
            )
            task.retries += retries
            task.video_url = data["file"]["download_url"]
            task.status = SUCCEEDED
        except Exception as exc:
            task.error = self.normalize_error(exc)
            task.status = HUMAN_REVIEW
        return task

    def normalize_error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, MiniMaxAPIError):
            status = exc.http_status
            message = str(exc)
            lowered = message.lower()
            if status == 401:
                kind = AUTH
            elif status in self.retryable_statuses or message in {"TimeoutError", "URLError"}:
                kind = TRANSIENT
            elif any(word in lowered for word in ("insufficient", "balance", "quota", "permission")) or any(
                word in message for word in ("余额", "额度", "套餐", "权限")
            ):
                kind = ENTITLEMENT
            elif status == 400:
                kind = REQUEST
            else:
                kind = UNKNOWN
            return ProviderError(
                kind=kind, message=message, http_status=status,
                provider_code=exc.code, retryable=kind == TRANSIENT,
            )
        return ProviderError(kind=UNKNOWN, message=type(exc).__name__, retryable=False)


class GenerationGuard:
    """单进程测试空间的并发、次数与估算费用硬门槛。"""

    def __init__(self):
        self.max_active = int(os.getenv("MAX_ACTIVE_VIDEO_JOBS", "1"))
        self.max_daily = int(os.getenv("MAX_DAILY_VIDEO_JOBS", "3"))
        self.max_budget = float(os.getenv("VIDEO_BUDGET_CNY", "10"))
        self._day = datetime.now(timezone.utc).date()
        self._daily_jobs = 0
        self._daily_cost = 0.0
        self._active: dict[str, float] = {}
        self._lock = threading.RLock()

    def reserve(self, estimated_cost: float) -> str:
        with self._lock:
            today = datetime.now(timezone.utc).date()
            if today != self._day:
                self._day = today
                self._daily_jobs = 0
                self._daily_cost = 0.0
                self._active.clear()
            if len(self._active) >= self.max_active:
                raise RuntimeError("已有视频任务运行中，请等待完成后再提交。")
            if self._daily_jobs >= self.max_daily:
                raise RuntimeError("已达到今日视频任务上限。")
            if self._daily_cost + estimated_cost > self.max_budget:
                raise RuntimeError("预计费用将超过今日预算上限。")
            token = uuid4().hex
            self._active[token] = estimated_cost
            self._daily_jobs += 1
            self._daily_cost += estimated_cost
            return token

    def release(self, token: str | None, *, rollback: bool = False) -> None:
        if not token:
            return
        with self._lock:
            cost = self._active.pop(token, None)
            if rollback and cost is not None:
                self._daily_jobs = max(0, self._daily_jobs - 1)
                self._daily_cost = max(0.0, self._daily_cost - cost)

    @property
    def usage(self) -> tuple[int, float, int]:
        with self._lock:
            return self._daily_jobs, self._daily_cost, len(self._active)


def estimated_cost_cny(model: str, resolution: str, duration: int) -> float:
    fixed = {
        ("MiniMax-Hailuo-2.3-Fast", "768P", 6): 1.35,
        ("MiniMax-Hailuo-2.3-Fast", "768P", 10): 2.25,
        ("MiniMax-Hailuo-2.3-Fast", "1080P", 6): 2.31,
        ("MiniMax-Hailuo-2.3", "768P", 6): 2.00,
        ("MiniMax-Hailuo-2.3", "768P", 10): 4.00,
        ("MiniMax-Hailuo-2.3", "1080P", 6): 3.50,
        ("MiniMax-Hailuo-02", "512P", 6): 0.60,
        ("MiniMax-Hailuo-02", "512P", 10): 1.00,
        ("MiniMax-Hailuo-02", "768P", 6): 2.00,
        ("MiniMax-Hailuo-02", "768P", 10): 4.00,
        ("MiniMax-Hailuo-02", "1080P", 6): 3.50,
    }
    if (model, resolution, duration) not in fixed:
        raise ValueError("当前模型、分辨率和时长组合没有已验证的价格，拒绝提交。")
    return fixed[(model, resolution, duration)]


# 注册表。接入新平台 = 在这里加一行，其余代码不动。
PROVIDERS: dict[str, type[VideoGenerationProvider]] = {
    "none": NullProvider,
    "minimax": MiniMaxHailuoProvider,
}


def get_provider() -> VideoGenerationProvider | None:
    """返回当前可用的 Provider；没有则返回 None。

    UI 必须以 `get_provider() is None` 作为"不渲染生成按钮"的唯一判据。
    """
    if os.getenv("ENABLE_VIDEO_GENERATION", "").strip() != "1":
        return None
    if not os.getenv("VIDEO_ACCESS_CODE"):
        return None
    name = os.getenv("VIDEO_PROVIDER", "")
    cls = PROVIDERS.get(name)
    if cls is None or cls is NullProvider or not cls.available():
        return None
    return cls()


def task_from_shot(shot: dict[str, Any], routing: dict[str, Any]) -> GenerationTask:
    """把 CREATE 产出的镜头直接变成一个待提交任务。

    这是"扩展口"的具体形态：镜头字段已经齐备，接入适配器时不必改数据结构。
    """
    return GenerationTask(
        shot_id=shot["shot_id"],
        prompt=shot.get("prompt", ""),
        duration=int(os.getenv("VIDEO_DURATION", "6")),
        aspect_ratio=shot.get("aspect_ratio", "16:9"),
        resolution=os.getenv("VIDEO_RESOLUTION", shot.get("resolution", "768P")),
        model=os.getenv("VIDEO_MODEL", "MiniMax-Hailuo-2.3-Fast"),
        provider=os.getenv("VIDEO_PROVIDER", "none"),
        reference_images=list(shot.get("reference_images", [])),
        status=shot.get("status", AWAITING),
    )
