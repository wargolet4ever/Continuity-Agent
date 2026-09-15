"""双模式 Orchestrator：CREATE 与 AUDIT 共用一套轨迹记录与模型调用。

轨迹是这个模块存在的主要理由——每一步都要留下：
做了什么、用了多久、走的是模型还是本地、重试了几次、失败原因是什么。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import creator
from canon_loader import CanonStore

MODEL = "model"
LOCAL = "local"

# 证据来源。核心约束：仅凭一句话得出的结论，绝不能看起来像系统看过画面。
EVIDENCE_USER_REPORTED = "USER-REPORTED RULE TRIAGE"
EVIDENCE_RULE_PIXEL = "RULE + PIXEL CHECK"
EVIDENCE_VISUAL = "VISUAL AUDIT"
EVIDENCE_VIDEO_RULE = "VIDEO FRAME + RULE CHECK"
EVIDENCE_VIDEO_VISUAL = "VIDEO VISUAL AUDIT"

EVIDENCE_NOTE = {
    EVIDENCE_USER_REPORTED: "本结论来自你的文字描述与 canon 规则匹配，系统没有看过任何画面。",
    EVIDENCE_RULE_PIXEL: "系统读取了图片的颜色与像素统计，但没有用模型理解画面内容。",
    EVIDENCE_VISUAL: "多模态模型实际查看了图片并逐条核验 canon 规则。",
    EVIDENCE_VIDEO_RULE: "系统按时间顺序抽取了视频关键帧并读取像素；形变等语义结论仍来自你的文字描述，模型没有理解画面。",
    EVIDENCE_VIDEO_VISUAL: "系统按时间顺序抽取视频关键帧，多模态模型实际查看并跨帧核验 canon 规则。",
}


def evidence_source(image, uses_api: bool, api_reviewed: bool, video_frames=None) -> str:
    if video_frames:
        return EVIDENCE_VIDEO_VISUAL if uses_api and api_reviewed else EVIDENCE_VIDEO_RULE
    if image is None:
        return EVIDENCE_USER_REPORTED
    if uses_api and api_reviewed:
        return EVIDENCE_VISUAL
    return EVIDENCE_RULE_PIXEL


MAX_API_RETRIES = 2          # 网络层重试上限，与「内容重新生成」是两回事
RETRY_BACKOFF_S = (1, 3)


# ──────────────────────────────────────────────────────────────────────
# 轨迹
# ──────────────────────────────────────────────────────────────────────


class Trace:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self._started = time.monotonic()

    def record(
        self,
        name: str,
        kind: str,
        duration_ms: int,
        ok: bool = True,
        detail: str = "",
        retries: int = 0,
        source: str = "",
    ) -> None:
        self.steps.append(
            {
                "index": len(self.steps) + 1,
                "name": name,
                "kind": kind,
                "duration_ms": duration_ms,
                "ok": ok,
                "retries": retries,
                "source": source,
                "detail": detail,
            }
        )

    def step(self, name: str, kind: str, source: str = ""):
        return _StepContext(self, name, kind, source)

    @property
    def total_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    @property
    def retries(self) -> int:
        return sum(s["retries"] for s in self.steps)

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [s for s in self.steps if not s["ok"]]

    def rows(self) -> list[list[Any]]:
        icon = {True: "✓", False: "✗"}
        label = {MODEL: "模型", LOCAL: "本地"}
        return [
            [
                s["index"],
                s["name"],
                label.get(s["kind"], s["kind"]),
                s["source"] or "—",
                f"{s['duration_ms'] / 1000:.1f}s",
                icon[s["ok"]],
                s["retries"],
                s["detail"][:90],
            ]
            for s in self.steps
        ]

    def as_dict(self) -> dict[str, Any]:
        return {"total_ms": self.total_ms, "steps": self.steps}


class _StepContext:
    def __init__(self, trace: Trace, name: str, kind: str, source: str):
        self.trace, self.name, self.kind, self.source = trace, name, kind, source
        self.detail = ""
        self.retries = 0

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = int((time.monotonic() - self._t0) * 1000)
        self.trace.record(
            self.name,
            self.kind,
            elapsed,
            ok=exc is None,
            detail=self.detail or (str(exc) if exc else ""),
            retries=self.retries,
            source=self.source,
        )
        return False


# ──────────────────────────────────────────────────────────────────────
# 模型调用（带网络层重试；无配置时返回 None，调用方走本地降级）
# ──────────────────────────────────────────────────────────────────────


def api_configured() -> bool:
    return bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL"))


def model_name() -> str:
    return os.getenv("LLM_MODEL", "")


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def make_model_caller(step: _StepContext | None = None) -> Callable | None:
    """返回一个 prompt -> dict 的调用器；未配置 API 时返回 None。"""
    if not api_configured():
        return None

    def call(user_text: str) -> dict[str, Any] | None:
        base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        payload = {
            "model": os.environ["LLM_MODEL"],
            "temperature": 0.4,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a film development assistant. Output valid JSON only.",
                },
                {"role": "user", "content": user_text},
            ],
        }
        last_error = ""
        for attempt in range(MAX_API_RETRIES + 1):
            try:
                request = urllib.request.Request(
                    f"{base}/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={
                        "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=90) as response:
                    body = json.loads(response.read().decode())
                return _extract_json(body["choices"][0]["message"]["content"])
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                json.JSONDecodeError,
                KeyError,
                OSError,
                ValueError,
            ) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if step is not None:
                    step.retries = attempt + 1
                if attempt < MAX_API_RETRIES:
                    time.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
        if step is not None:
            step.detail = f"模型调用失败，已重试 {MAX_API_RETRIES} 次并降级为本地：{last_error}"[:200]
        return None

    return call


# ──────────────────────────────────────────────────────────────────────
# 意图分类
# ──────────────────────────────────────────────────────────────────────

_AUDIT_HINTS = ("检查", "审查", "审计", "看看", "对不对", "有没有问题", "复核", "audit", "check")
_SHOT_PATTERN = re.compile(r"(?:shot|镜头|第)\s*0*(\d{1,2})", re.I)


def classify_intent(text: str) -> str:
    lowered = (text or "").lower()
    if any(hint in lowered for hint in _AUDIT_HINTS) or _SHOT_PATTERN.search(lowered):
        return "AUDIT"
    return "CREATE"


OBSERVATION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "画面已人工核对无异常": ("没问题", "无异常", "都对", "核对过"),
    "出现未经设定的小型显示屏": ("小屏", "小型显示屏", "显示屏", "多了一块屏", "平板"),
    "操纵台／拉杆在镜头内发生形变": (
        "操纵台变形", "控制台变形", "操纵台整个", "控制台整个", "操纵台整体", "控制台整体", "拉杆变形", "突然变形", "结构变形", "形变", "变形",
    ),
    "钥匙插槽位置错误／靠近拉杆": ("钥匙", "插槽"),
    "047 双手同时离开拉杆": ("双手", "松手", "离开拉杆", "没压住"),
    "Attempt 05 出现红色警报光": ("红光", "红色", "警报光"),
    "系统被画成实体／全息人形": ("全息", "人形", "系统实体"),
    "Daniel 外貌或服装不一致": ("daniel", "主角外貌", "主角服装", "男主"),
    "047 外貌或服装不一致": ("047外貌", "047服装", "她的外貌", "她的服装"),
    "摄影机发生推轨、环绕或升降": ("推轨", "环绕", "升降", "运镜", "镜头动", "镜头在动"),
    "画面出现乱码或错误可读文字": ("乱码", "文字", "字幕", "错字"),
    "047 表演意图不确定": ("表演", "表情", "情绪", "得意", "惊恐"),
    "047 与 Daniel 左右轴线错误": ("轴线", "越轴", "左右反", "位置反"),
    "拉杆的运动方式错误": ("拉杆", "弹起", "锁死"),
    "Bay 07 出现多余人物": ("多余人物", "多了个人", "第三个人"),
}


def parse_audit_instruction(
    text: str, canon: CanonStore, observation_options: dict
) -> dict:
    """把自然语言指令解析成 {shot_id, observations}。确定性，不需要模型。

    用显式关键词表，而不是对选项标签做模糊匹配——后者不可审阅，
    而且中文标签正则切出来往往是一个长词，永远匹配不上。
    """
    lowered = (text or "").lower()
    match = _SHOT_PATTERN.search(lowered)
    shot_id = None
    if match and match.group(1).lstrip("0") in canon.shot_ids:
        shot_id = match.group(1).lstrip("0")

    hits = [
        label
        for label in observation_options
        if any(word in lowered for word in OBSERVATION_KEYWORDS.get(label, ()))
    ]
    clean = "画面已人工核对无异常"
    if clean in hits and len(hits) > 1:
        hits.remove(clean)
    return {"shot_id": shot_id, "observations": hits}


def run_create(idea: str, shot_count: int = 5, workdir: str | Path | None = None) -> dict[str, Any]:
    trace = Trace()
    source = model_name() if api_configured() else "本地规则"
    idea = (idea or "").strip()
    if not idea:
        raise ValueError("请先输入一句创意。")

    with trace.step("① 故事理解", MODEL if api_configured() else LOCAL, source) as step:
        story, used_model = creator.understand_story(idea, make_model_caller(step))
        if not used_model:
            step.kind = LOCAL
            step.detail = step.detail or "本地降级：关键词结构化，未做语义理解"
    trace.steps[-1]["kind"] = MODEL if used_model else LOCAL
    trace.steps[-1]["source"] = source if used_model else "本地模板"

    with trace.step("② 镜头规划", MODEL if api_configured() else LOCAL, source) as step:
        shots, used_model = creator.plan_shots(story, shot_count, make_model_caller(step))
        if not used_model:
            step.detail = step.detail or "本地降级：三幕骨架模板"
    trace.steps[-1]["kind"] = MODEL if used_model else LOCAL
    trace.steps[-1]["source"] = source if used_model else "本地模板"

    with trace.step("③ 装配 Canon 草案", LOCAL, "确定性规则") as step:
        canon_draft = creator.build_canon_draft(story, shots)
        step.detail = f"{len(canon_draft['shots'])} 镜、{len(canon_draft['locations'])} 个场景"

    with trace.step("④ 生成连续性约束", LOCAL, "确定性规则") as step:
        rule_count = (
            len(canon_draft["global_rules"])
            + len(canon_draft["camera"]["rules"])
            + sum(len(v["rules"]) for v in canon_draft["locations"].values())
            + sum(len(v.get("rules", [])) for v in canon_draft["characters"].values())
        )
        step.detail = f"{rule_count} 条可执行规则"

    with trace.step("⑤ 合成视觉 Prompt", MODEL if api_configured() else LOCAL, source) as step:
        prompts, used_model = creator.synthesize_prompts(story, shots, make_model_caller(step))
        if not used_model:
            step.detail = step.detail or "本地降级：模板合成"
    trace.steps[-1]["kind"] = MODEL if used_model else LOCAL
    trace.steps[-1]["source"] = source if used_model else "本地模板"

    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="pz_create_"))
    root.mkdir(parents=True, exist_ok=True)
    canon_path = root / "canon_draft.json"
    canon_path.write_text(
        json.dumps(canon_draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with trace.step("⑥ 模型策略", LOCAL, "二元规则") as step:
        store = CanonStore(canon_path)
        routing = {s["shot_id"]: store.route(s["shot_id"]) for s in shots}
        # 回填结构化字段，使每个镜头可以被 video_provider.task_from_shot 直接消费
        for shot in shots:
            shot["model_strategy"] = routing[shot["shot_id"]]["tier"]
            shot["prompt"] = prompts[shot["shot_id"]]["positive"]
        canon_draft["shots"] = {
            s["shot_id"]: {**canon_draft["shots"][s["shot_id"]],
                           "aspect_ratio": s["aspect_ratio"],
                           "resolution": s["resolution"],
                           "reference_images": s["reference_images"],
                           "model_strategy": s["model_strategy"],
                           "prompt": s["prompt"]}
            for s in shots
        }
        canon_path.write_text(
            json.dumps(canon_draft, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        primary = sum(1 for r in routing.values() if r["tier"] == "primary")
        step.detail = f"primary {primary} 镜 / economy {len(routing) - primary} 镜；只出决策，不派发"

    with trace.step("⑦ 草案因果审计", LOCAL, "NA-01—NA-08") as step:
        findings = store.narrative_audit()
        errors = sum(f["level"] == "ERROR" for f in findings)
        step.detail = f"{errors} errors / {len(findings) - errors} warnings"

    return {
        "mode": "CREATE",
        "idea": idea,
        "story": story,
        "shots": shots,
        "prompts": prompts,
        "routing": routing,
        "findings": findings,
        "canon_draft": canon_draft,
        "canon_path": str(canon_path),
        "workdir": str(root),
        "trace": trace,
        "generation_status": "AWAITING_EXTERNAL_GENERATION",
        "api_used": api_configured(),
    }


# ──────────────────────────────────────────────────────────────────────
# AUDIT 链路
# ──────────────────────────────────────────────────────────────────────


def run_audit_chain(
    instruction: str,
    analyzer,
    canon: CanonStore,
    observation_options: dict,
    image=None,
    mode: str = "本地规则（无需 API）",
    fallback_shot: str | None = None,
    video_frames: list[Any] | None = None,
    video_timestamps: list[float] | None = None,
) -> dict[str, Any]:
    trace = Trace()

    with trace.step("① 解析指令", LOCAL, "确定性关键词") as step:
        parsed = parse_audit_instruction(instruction, canon, observation_options)
        shot_id = parsed["shot_id"] or fallback_shot or canon.shot_ids[0]
        step.detail = (
            f"Shot {shot_id}"
            + (f"；识别到 {len(parsed['observations'])} 项观察" if parsed["observations"] else "；未识别到具体问题")
        )

    with trace.step("② 编译规则包", LOCAL, "canon") as step:
        pack = canon.compile_rule_pack(shot_id)
        step.detail = f"{len(pack['rules'])} 条规则；锚点 {pack['anchor'].get('anchor', '—')}"

    with trace.step("③ 模型路由", LOCAL, "二元规则") as step:
        route = canon.route(shot_id)
        step.detail = f"{route['tier']}：{route['reason'][:50]}"

    uses_api = mode.startswith("多模态")
    with trace.step(
        "④ Take 审计", MODEL if uses_api else LOCAL, model_name() if uses_api else "本地规则"
    ) as step:
        result = analyzer.audit(
            shot_id=shot_id,
            mode=mode,
            current_prompt=instruction,
            observations=parsed["observations"],
            current_image=image,
            video_frames=video_frames,
            video_timestamps=video_timestamps,
        )
        evidence = evidence_source(
            image, uses_api, bool(result.get("api_reviewed")), video_frames
        )
        step.retries = int(result.get("api_retries") or 0)
        step.detail = f"{result['decision']} · {result.get('score', '—')}/100 · {evidence}"
        if result.get("api_error"):
            step.detail += f"；{result['api_error'][:70]}，已降级为本地规则"

    return {
        "mode": "AUDIT",
        "instruction": instruction,
        "shot_id": shot_id,
        "parsed": parsed,
        "pack": pack,
        "routing": route,
        "result": result,
        "evidence_source": evidence,
        "evidence_note": EVIDENCE_NOTE[evidence],
        "trace": trace,
    }
