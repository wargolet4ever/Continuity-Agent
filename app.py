"""AI 短片连戏检查器 —— 界面层。

界面只说人话：一句话结论、哪里不对、怎么修、这个结论是怎么来的。
决策代号、评分、规则 ID、执行轨迹全部收进折叠起来的「详细报告」——
它们是对内的精确词汇，不是给第一次打开这个页面的人看的。

所有业务逻辑在 orchestrator / creator / analyzer / canon_loader，这里不写规则。
"""

from __future__ import annotations

import base64
import hmac
import io
import json
import os
from pathlib import Path
from typing import Any

import gradio as gr

import orchestrator as orch
import package
from analyzer import (
    OBSERVATION_OPTIONS,
    ContinuityAnalyzer,
    issues_rows,
    result_markdown,
)
from audit_report import build_audit_report, write_audit_report_bundle
from canon_loader import CanonStore
from deployment import validate_public_image, validate_public_image_batch
from take_log import FIELDS, TakeLog
from video_audit import (
    VIDEO_SUPPORTED,
    VIDEO_UNAVAILABLE_REASON,
    preview_video,
    sample_video,
)
from video_provider import (
    FAILED,
    HUMAN_REVIEW,
    RUNNING,
    SUBMITTED,
    SUCCEEDED,
    GenerationGuard,
    GenerationTask,
    ProviderError,
    estimated_cost_cny,
    get_provider,
    task_from_shot,
)
from workflow import rank_takes, review_after

# iPhone 默认拍的是 .heic，Pillow 不带 HEIF 解码器，用户一传就失败。
# 装了就注册，没装就算了——绝不能因为这个可选依赖让整个应用起不来。
try:  # pragma: no cover - 取决于部署环境是否装得上
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except Exception:  # noqa: BLE001
    HEIC_SUPPORTED = False

ROOT = Path(__file__).resolve().parent
CANON_PATH = Path(os.getenv("CANON_PATH", ROOT / "canon.json"))
LOG_PATH = Path(os.getenv("TAKE_LOG_PATH", ROOT / "data" / "take_log.csv"))
HISTORY_PATH = ROOT / "canon_history_2026-09-12.json"

canon = CanonStore(CANON_PATH)
analyzer = ContinuityAnalyzer(canon)
take_log = TakeLog(LOG_PATH)
canon_history = CanonStore(HISTORY_PATH) if HISTORY_PATH.is_file() else None

# 视频 Provider 默认为 None：公开环境不渲染任何生成控件，也不绑定任何生成回调。
# 只有同时设置 ENABLE_VIDEO_GENERATION=1、VIDEO_PROVIDER=minimax、访问码与密钥，
# 受控生成面板才会出现（见 video_provider.get_provider）。
video_provider = get_provider()
video_guard = GenerationGuard()

# ── 安全模式 ──────────────────────────────────────────────────────────
# 日志是全局共享的：一个进程一份 CSV，不区分用户。因此在公开环境里
# **默认不渲染原始日志表，也不绑定任何会返回日志行的回调**——否则任何
# 访客都能读到别人的 prompt、文件名和备注。
# 仅当运维明确设置 SHOW_RAW_LOGS=1（私有部署）时才放开。
SHOW_RAW_LOGS = os.getenv("SHOW_RAW_LOGS", "") == "1"

CUT_STATE = "当前成片（canon 1.4.2）"
PROD_STATE = "还原制作中状态（2026-09-12 的 canon）"

TRACE_HEADERS = ["#", "步骤", "执行方", "来源", "耗时", "结果", "重试", "说明"]
ISSUE_HEADERS = [
    "Rule", "Category", "Severity", "Confidence", "Evidence", "Minimum Fix", "Canon Note",
]

CREATE_EXAMPLES = [
    "一个潜水员在沉船里发现了自己的照片",
    "末班地铁上，只有她看得见第八节车厢",
    "养老院的机器人每天重复同一段对话，直到有人回答错了",
]
AUDIT_EXAMPLES = [
    "检查 Shot 21 的钥匙插槽位置对不对",
    "看看镜头 19 是不是多了一块小型显示屏",
]


def new_session() -> dict[str, Any]:
    return {
        "active_canon_path": str(CANON_PATH),
        "canon_label": f"成片 canon v{canon.data['meta']['version']}",
        "shot_ids": canon.shot_ids,
        "package_path": None,
        "generation_shots": {},
        "video_task": None,
        "video_reservation": None,
        "last_audit": {},
        "report_path": None,
    }


def active_store(state: dict[str, Any]) -> CanonStore:
    path = (state or {}).get("active_canon_path") or str(CANON_PATH)
    return canon if path == str(CANON_PATH) else CanonStore(path)


def analyzer_for(store: CanonStore) -> ContinuityAnalyzer:
    """绑定到本会话 canon 的 analyzer —— 不要拿别人的草案 canon 去判本会话的镜头。"""
    if str(analyzer.canon.path) == str(store.path):
        return analyzer
    return ContinuityAnalyzer(store)


# ──────────────────────────────────────────────────────────────────────
# CREATE
# ──────────────────────────────────────────────────────────────────────


def do_create(idea: str, shot_count: int, state: dict[str, Any]):
    state = state or new_session()
    # 正在跑的视频任务不能被一次新的 CREATE 抹掉——否则预算占用永远释放不掉。
    active_video_task = state.get("video_task")
    keep_video_task = bool(
        active_video_task and active_video_task.get("status") in {SUBMITTED, RUNNING}
    )
    active_reservation = state.get("video_reservation") if keep_video_task else None
    try:
        result = orch.run_create(idea, int(shot_count))
    except ValueError as exc:
        return (
            gr.update(value=f"### ⚠️ {exc}", visible=True),
            gr.update(visible=False), "", [], [], gr.update(visible=False), state,
        )

    trace = result["trace"]
    shots = result["shots"]
    anchors = result["canon_draft"]["anchor_plan"]["shots"]

    plan_rows = [
        [
            s["shot_id"], s["beat_name"], s["location"], f"{s['duration_sec']}s",
            s["camera"], s["action"][:44],
            "续接 Shot %s" % anchors[s["shot_id"]]["from_shot"]
            if anchors[s["shot_id"]]["anchor"] == "chain"
            else "回查 %s" % anchors[s["shot_id"]].get("reference", ""),
            result["routing"][s["shot_id"]]["tier"],
            "待外部生成",
        ]
        for s in shots
    ]

    prompt_text = "\n\n".join(
        f"### Shot {s['shot_id']} · {s['beat_name']}\n"
        f"**正向**\n```\n{result['prompts'][s['shot_id']]['positive']}\n```\n"
        f"**负向**\n```\n{result['prompts'][s['shot_id']]['negative']}\n```"
        for s in shots
    )

    findings = result["findings"]
    audit_rows = [[f["level"], f["id"], f["message"]] for f in findings]
    errors = sum(f["level"] == "ERROR" for f in findings)

    degraded = not result["api_used"]
    head = (
        f"## {result['story'].get('title', '')}\n\n"
        f"**{result['story'].get('logline', '')}**\n\n"
        f"{len(shots)} 镜 · 合计 {sum(s['duration_sec'] for s in shots)} 秒 · "
        f"用时 {trace.total_ms / 1000:.1f}s · "
        f"草案因果审计 {errors} errors\n\n"
        + (
            "> ⚠️ **本地降级运行**：未配置模型 API，故事理解／镜头规划／Prompt 走确定性模板，"
            "质量明显低于接入模型时。canon 装配、约束、路由、审计四步在任何情况下都是本地确定性的。\n\n"
            if degraded
            else f"> 故事理解／镜头规划／Prompt 由 `{orch.model_name()}` 生成；"
            "canon 装配、约束、路由、审计为本地确定性规则。\n\n"
        )
        + "### 🎬 AWAITING EXTERNAL GENERATION\n\n"
        "本产品包不含任何视频。"
        + (
            "本测试空间已启用受控视频生成：可在下方选择一个镜头、上传首帧并确认费用后提交。"
            if video_provider is not None
            else "每个镜头的状态都是待外部生成——公开环境不接入任何视频生成平台。"
        )
        + "\n\n---\n\n### 👉 下一步\n\n"
        "把下面每一镜的 prompt 拿去任意视频模型生成，然后回到 **「检查素材」** 页把结果传上来——"
        "**我会用刚刚给你做的这份规则去查**，而不是拿一套通用标准。"
    )

    state.update(
        {
            "active_canon_path": result["canon_path"],
            "canon_label": f"草案：{result['story'].get('title', '')[:12]}",
            "shot_ids": [s["shot_id"] for s in shots],
            "package_path": package.build_package(result),
            "generation_shots": {s["shot_id"]: s for s in shots},
            "video_task": active_video_task if keep_video_task else None,
            "video_reservation": active_reservation,
        }
    )
    return (
        gr.update(value=head, visible=True),
        gr.update(value=plan_rows, visible=True),
        prompt_text,
        audit_rows,
        trace.rows(),
        gr.update(value=state["package_path"], visible=True),
        state,
    )


# ──────────────────────────────────────────────────────────────────────
# VIDEO · 仅在显式启用的测试空间中出现
# ──────────────────────────────────────────────────────────────────────


def _image_data_url(image) -> str:
    if image is None:
        return ""
    width, height = image.size
    if min(width, height) <= 300:
        raise ValueError("首帧短边必须大于 300px。")
    ratio = width / height
    if not 0.4 <= ratio <= 2.5:
        raise ValueError("首帧宽高比必须在 2:5 到 5:2 之间。")
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=92)
    if buffer.tell() > 20 * 1024 * 1024:
        raise ValueError("首帧必须小于 20MB。")
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _restore_video_task(data: dict[str, Any] | None) -> GenerationTask | None:
    if not data:
        return None
    copied = dict(data)
    if isinstance(copied.get("error"), dict):
        copied["error"] = ProviderError(**copied["error"])
    return GenerationTask(**copied)


def prepare_video(shot_id: str, state: dict[str, Any]):
    state = state or new_session()
    shot = state.get("generation_shots", {}).get(str(shot_id))
    if not shot:
        return "", "### ⚠️ 请先运行一次创作，再选择有效镜头。"
    task = task_from_shot(shot, {})
    try:
        cost = estimated_cost_cny(task.model, task.resolution, task.duration)
        note = (
            f"### 预计费用：¥{cost:.2f}\n\n"
            f"`{task.model}` · {task.resolution} · {task.duration}秒 · "
            "提交后不会自动进行内容重生成。"
        )
        # 平台只支持固定的几个时长；分镜计划里的秒数不一定在其中。
        # 不静默替换——把差异写在用户看得见的地方。
        planned = shot.get("duration_sec")
        if planned is not None and int(planned) != task.duration:
            note += (
                f"\n\n⚠️ 分镜计划里这一镜是 {planned} 秒，但平台只接受固定时长，"
                f"本次将按 {task.duration} 秒提交。剪辑时需要自行处理这个差值。"
            )
    except ValueError as exc:
        note = f"### ⚠️ {exc}"
    return shot.get("prompt", ""), note


def submit_video(shot_id, prompt, first_frame, access_code, confirmed, state):
    state = state or new_session()
    if video_provider is None:
        return "### ⚠️ 视频 Provider 未启用。", gr.update(visible=False), state
    expected_code = os.getenv("VIDEO_ACCESS_CODE", "")
    if not access_code or not hmac.compare_digest(str(access_code), expected_code):
        return "### ⚠️ 视频生成访问码不正确。", gr.update(visible=False), state
    if not confirmed:
        return "### ⚠️ 请先确认本次生成会产生费用。", gr.update(visible=False), state
    if state.get("video_task") and state["video_task"].get("status") in {SUBMITTED, RUNNING}:
        return "### ⚠️ 当前会话已有任务运行中，请勿重复提交。", gr.update(visible=False), state
    shot = state.get("generation_shots", {}).get(str(shot_id))
    if not shot:
        return "### ⚠️ 请先运行一次创作。", gr.update(visible=False), state
    task = task_from_shot(shot, {})
    task.prompt = (prompt or task.prompt).strip()
    if not task.prompt:
        return "### ⚠️ Prompt 不能为空。", gr.update(visible=False), state
    try:
        image_url = _image_data_url(first_frame)
        task.reference_images = [image_url] if image_url else []
        estimated = estimated_cost_cny(task.model, task.resolution, task.duration)
        reservation = video_guard.reserve(estimated)
    except (ValueError, RuntimeError) as exc:
        return f"### ⚠️ {exc}", gr.update(visible=False), state

    task = video_provider.submit(task)
    if task.status != SUBMITTED:
        video_guard.release(reservation, rollback=True)
        error = task.error.message if task.error else "提交失败"
        return f"### {task.status}\n\n{error}", gr.update(visible=False), state

    # 任务建立后 MiniMax 不再需要首帧，不要把 base64 留在会话状态里。
    task.reference_images = []
    state["video_task"] = task.as_dict()
    state["video_reservation"] = reservation
    jobs, cost, active = video_guard.usage
    return (
        f"### SUBMITTED\n\n任务已安全提交。今日 {jobs} 个任务，"
        f"预计累计 ¥{cost:.2f}，运行中 {active} 个。",
        gr.update(visible=False),
        state,
    )


def refresh_video(state):
    state = state or new_session()
    if video_provider is None:
        return "### ⚠️ 视频 Provider 未启用。", gr.update(visible=False), state
    task = _restore_video_task(state.get("video_task"))
    if task is None:
        return "### 尚未提交视频任务", gr.update(visible=False), state
    if task.status in {SUBMITTED, RUNNING}:
        task = video_provider.poll(task)
    if task.status == SUCCEEDED and not task.video_url:
        task = video_provider.fetch_result(task)
    if task.status in {SUCCEEDED, FAILED, HUMAN_REVIEW}:
        video_guard.release(state.get("video_reservation"))
        state["video_reservation"] = None
    state["video_task"] = task.as_dict()
    if task.status == SUCCEEDED and task.video_url:
        return (
            "### SUCCEEDED\n\n视频已生成。请立即下载保存，平台下载地址可能过期。",
            gr.update(value=task.video_url, visible=True), state,
        )
    error = f"\n\n{task.error.message}" if task.error else ""
    return f"### {task.status}{error}", gr.update(visible=False), state


# ──────────────────────────────────────────────────────────────────────
# AUDIT
# ──────────────────────────────────────────────────────────────────────

EVIDENCE_STYLE = {
    orch.EVIDENCE_USER_REPORTED: ("🗣️", "**仅凭你的文字描述**"),
    orch.EVIDENCE_RULE_PIXEL: ("🎨", "**读取了像素，但模型没看画面**"),
    orch.EVIDENCE_VISUAL: ("👁️", "**模型实际看过画面**"),
    orch.EVIDENCE_VIDEO_RULE: ("🎞️", "**抽取了视频帧，但模型没理解画面**"),
    orch.EVIDENCE_VIDEO_VISUAL: ("🎬", "**模型实际跨帧检查了视频**"),
}

# ── 说人话 ────────────────────────────────────────────────────────────
# 决策代号和证据等级是对内的精确词汇，不是给第一次打开这个页面的人看的。
# 界面只说结论和下一步；代号、评分、规则 ID、执行轨迹全部收进「详细报告」。
DECISION_PLAIN: dict[str, tuple[str, str, str]] = {
    "PASS": ("✅", "看起来没问题", "在我能检查的范围内没发现违规。"),
    "LOCAL FIX": ("🔧", "后期能修，不用重拍",
                  "问题不影响动作和空间关系，后期擦掉就行——这一档是省钱的关键。"),
    "REGENERATE": ("🔁", "这条得重新生成", "是结构性错误，后期补不回来。"),
    "HUMAN REVIEW": ("🤔", "我不确定，你自己看一眼",
                     "涉及表演，或者证据不够，我不替你下结论。"),
    "SKIP": ("⏭️", "这镜不用查", "纯后期镜头，不做视觉审计。"),
}

EVIDENCE_PLAIN = {
    orch.EVIDENCE_USER_REPORTED:
        "我没看过画面——这个结论是按你写的描述去对规则推出来的。传张图或一段视频才算数。",
    orch.EVIDENCE_RULE_PIXEL: "我读了画面的颜色和像素，但没有理解画面内容。",
    orch.EVIDENCE_VIDEO_RULE: "我按时间顺序抽了几帧做对比，但「哪里不对」仍来自你的描述。",
    orch.EVIDENCE_VISUAL: "多模态模型实际看过这张画面，逐条核对了规则。",
    orch.EVIDENCE_VIDEO_VISUAL: "多模态模型逐帧看过这段素材，跨帧核对了规则。",
}


def plain_verdict(result: dict[str, Any], evidence_source: str) -> str:
    """一句话结论 + 怎么修 + 这个结论是怎么来的。没有代号，没有分数。"""
    icon, headline, gist = DECISION_PLAIN.get(
        result.get("decision", "HUMAN REVIEW"), ("•", "结果", "")
    )
    lines = [f"## {icon} {headline}", "", gist]
    issues = result.get("issues") or []
    if issues:
        top = issues[0]
        lines += ["", f"**发现**：{top.get('evidence') or top.get('rule_text') or '见下表'}"]
        if top.get("minimal_fix"):
            lines.append(f"**怎么修**：{top['minimal_fix']}")
        if len(issues) > 1:
            lines.append(f"\n还有 {len(issues) - 1} 处，见下表。")
    lines += ["", f"<sub>{EVIDENCE_PLAIN.get(evidence_source, '')}</sub>"]
    return "\n".join(lines)


def plain_issue_rows(result: dict[str, Any]) -> list[list[str]]:
    """两列就够了：哪里不对、怎么修。规则 ID 和置信度进详细报告。"""
    return [
        [issue.get("evidence") or issue.get("rule_text") or issue.get("rule_id", ""),
         issue.get("minimal_fix", "")]
        for issue in result.get("issues") or []
    ]


def do_audit(
    instruction, image, mode, state, model_name_in, filename, adoption, reason, notes,
    video=None,
):
    state = state or new_session()
    store = active_store(state)
    # 提前退出的分支必须和正常返回**同样长**（7 项），否则 Gradio 会把
    # 返回值错位塞进组件里。
    def _bail(message: str):
        return (
            f"### ⚠️ {message}",
            gr.update(value=[], visible=False),
            "",
            "",
            [],
            gr.update(visible=False),
            state,
        )

    if image is not None and video is not None:
        return _bail("请在“一张画面”和“一段视频”中只上传一种。")
    if image is not None:
        try:
            validate_public_image(image)
        except (OSError, ValueError) as exc:
            return _bail(str(exc))
    video_sample = None
    if video is not None:
        try:
            video_sample = sample_video(video)
        except ValueError as exc:
            return _bail(str(exc))
        image = video_sample.frames[len(video_sample.frames) // 2]
    chain = orch.run_audit_chain(
        instruction,
        analyzer_for(store),
        store,
        OBSERVATION_OPTIONS,
        image=image,
        mode=mode,
        video_frames=video_sample.frames if video_sample else None,
        video_timestamps=video_sample.timestamps_s if video_sample else None,
    )
    result = chain["result"]
    trace = chain["trace"]
    icon, phrase = EVIDENCE_STYLE[chain["evidence_source"]]

    banner = (
        f"> {icon} **证据来源：{chain['evidence_source']}** —— {phrase}\n>\n"
        f"> {chain['evidence_note']}\n\n"
    )
    if chain["evidence_source"] == orch.EVIDENCE_USER_REPORTED:
        banner += (
            "> 换句话说：下面的结论是「**如果**你描述的情况属实，canon 规则判定为此」，"
            "而不是系统已经确认画面有问题。上传图片并开启多模态审计才会变成 VISUAL AUDIT。\n\n"
        )

    take_log.append(
        result=result,
        model=model_name_in,
        prompt=instruction,
        reference_note=state.get("canon_label", ""),
        output_filename=filename or (video_sample.path.name if video_sample else ""),
        adoption=adoption,
        rejection_reason=reason,
        notes=notes,
        run_mode="AUDIT",
        evidence_source=chain["evidence_source"],
        duration_ms=trace.total_ms,
        retries=trace.retries,
        failure_reason=result.get("api_error", "") or "",
        model_source=(
            orch.model_name()
            if mode.startswith("多模态")
            else "本地规则 + 视频抽帧" if video_sample else "本地规则"
        ),
    )
    state["last_audit"] = result
    report = build_audit_report(
        result,
        evidence_source=chain["evidence_source"],
        evidence_note=chain["evidence_note"],
        trace=trace,
        source_filename=filename or (video_sample.path.name if video_sample else ""),
    )
    state["report_path"] = write_audit_report_bundle(report)
    # 注意：日志在服务端照常记录，但**不作为返回值**——否则等于把全部
    # 历史记录（含他人的 prompt 与备注）交给任何一个调用者。
    return (
        plain_verdict(result, chain["evidence_source"]),
        gr.update(value=plain_issue_rows(result), visible=bool(result.get("issues"))),
        banner + result_markdown(result),
        result["revised_prompt"],
        trace.rows(),
        gr.update(value=state["report_path"], visible=True),
        state,
    )


EXAMPLE_VIDEOS = {
    "A": "shot19_console_deformation.mp4",
    "B": "shot21_lever_key_error.mp4",
}

# 示例视频在仓库里搬过家（`assets/` → `assets/examples/`），两处都找一遍。
# 找不到就明确报出来，而不是让两个演示按钮抛一句「视频文件不可读取」——
# 那个报错会把「文件放错地方」误导成「视频坏了」。
EXAMPLE_DIRS = (ROOT / "assets" / "examples", ROOT / "assets", ROOT / "examples")


def example_video_path(name: str) -> Path:
    filename = EXAMPLE_VIDEOS[name]
    for directory in EXAMPLE_DIRS:
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    searched = "、".join(str(d.relative_to(ROOT)) for d in EXAMPLE_DIRS)
    raise FileNotFoundError(f"示例视频 {filename} 不在仓库里（找过：{searched}）。")


def run_demo(name: str, state):
    file = "shot19_local_fix.json" if name == "A" else "shot21_regenerate.json"
    data = json.loads((ROOT / "sample_data" / file).read_text(encoding="utf-8"))
    text = (
        "检查 Shot 19：操纵台整体在镜头内发生变形"
        if name == "A"
        else "检查 Shot 21：拉杆突然变形，钥匙取出位置错误"
    )
    # 没有 ffmpeg 也要能点：退回纯文字审查，证据等级会自动降到 USER-REPORTED。
    sample = sample_video(example_video_path(name)) if VIDEO_SUPPORTED else None
    state = state or new_session()
    state["active_canon_path"] = str(CANON_PATH)
    state["canon_label"] = f"成片 canon v{canon.data['meta']['version']}"
    chain = orch.run_audit_chain(
        text,
        analyzer,
        canon,
        OBSERVATION_OPTIONS,
        image=sample.frames[len(sample.frames) // 2] if sample else None,
        mode="本地规则（无需 API）",
        video_frames=sample.frames if sample else None,
        video_timestamps=sample.timestamps_s if sample else None,
    )
    result = chain["result"]
    icon, phrase = EVIDENCE_STYLE[chain["evidence_source"]]
    banner = (
        f"> **Demo {name}** · 期望 `{data['expected_decision']}` / `{data['expected_rule']}`"
        f" · 最小修复：{data['expected_fix']}\n>\n"
        f"> {icon} **证据来源：{chain['evidence_source']}** —— {phrase}。"
        + (
            "本例已按时间顺序抽取 5 帧；当前本地模式不做语义识别，"
            "错误类型来自人工观察并与 canon 匹配，未写入生产日志。\n\n"
            if sample
            else f"{VIDEO_UNAVAILABLE_REASON}本例因此只跑了文字描述与 canon 规则匹配，"
            "未写入生产日志。\n\n"
        )
    )
    report = build_audit_report(
        result,
        evidence_source=chain["evidence_source"],
        evidence_note=chain["evidence_note"],
        trace=chain["trace"],
        source_filename=example_video_path(name).name if sample else "",
    )
    state["report_path"] = write_audit_report_bundle(report)
    return (
        text,
        plain_verdict(result, chain["evidence_source"]),
        gr.update(value=plain_issue_rows(result), visible=bool(result.get("issues"))),
        banner + result_markdown(result),
        result["revised_prompt"],
        chain["trace"].rows(),
        gr.update(value=state["report_path"], visible=True),
        state,
    )


def rules_note(state) -> str:
    """审查用的是哪份规则——这是 CREATE 和 AUDIT 之间那条看不见的线。"""
    label = (state or {}).get("canon_label", "")
    if label.startswith("草案"):
        return (
            f"> 📐 **正在用你刚做的这份规则检查：{label}**　"
            "（它是「做分镜」那一步生成的；刷新或换标签页会回到示例规则）"
        )
    return (
        f"> 📐 当前用的是示例规则：**{label}**　"
        "——来自《第零号乘客》的成片。想用自己的规则，先去「做分镜」页跑一次。"
    )


def show_frames(value):
    """preview_video 的界面外壳：没帧就不占版面。"""
    gallery, note = preview_video(value)
    return gr.update(value=gallery, visible=bool(gallery)), note


def demo_media(name: str):
    if not VIDEO_SUPPORTED:
        return None, gr.update(visible=False), f"### ⚠️ {VIDEO_UNAVAILABLE_REASON}"
    video_path = example_video_path(name)
    sample = sample_video(video_path)
    return (
        str(video_path),
        gr.update(value=sample.gallery, visible=True),
        f"示例已抽取 {len(sample.frames)} 帧（{sample.duration_s:.1f} 秒）。",
    )


# ──────────────────────────────────────────────────────────────────────
# 技术面板
# ──────────────────────────────────────────────────────────────────────


def shot_detail(shot_id: str, state):
    store = active_store(state or new_session())
    if shot_id not in store.shot_ids:
        return "（该镜不在当前 canon 中）", "{}"
    pack = store.compile_rule_pack(shot_id)
    knowledge = store.knows_at(shot_id)
    anchor = pack["anchor"]
    target = (
        f"续接 Shot {anchor.get('from_shot')}"
        if str(anchor.get("anchor", "")).startswith("chain")
        else f"回查参考图 {anchor.get('reference', '—')}"
    )
    text = "\n".join(
        [
            f"**Shot {shot_id}** · {pack['duration_sec']}s · `{pack['location']}`",
            f"- 锚点：`{anchor.get('anchor', '—')}` → {target}",
            f"- 路由：`{pack['routing_tier']}`",
            f"- 规则包：{len(pack['rules'])} 条",
            f"- 进入本镜时：主角已知 {len(knowledge['daniel'])} 条事实，观众已知 {len(knowledge['audience'])} 条",
        ]
    )
    compact = {
        k: pack[k]
        for k in ("shot_id", "attempt_id", "location", "camera", "must_show", "must_not_show")
    }
    return text, json.dumps(compact, ensure_ascii=False, indent=2)


def canon_health(state):
    store = active_store(state or new_session())
    if str(store.path) != str(CANON_PATH):
        return "### 当前挂载的是 CREATE 草案\n\n结构检查（lint）只适用于成片 canon。", []
    warnings = store.lint()
    counts = {"ERROR": 0, "WARN": 0, "INFO": 0}
    for item in warnings:
        counts[item["level"]] = counts.get(item["level"], 0) + 1
    return (
        f"### Canon v{store.data['meta']['version']} · {len(store.shot_ids)} shots · "
        f"{counts['ERROR']} errors / {counts['WARN']} warnings",
        [[i["level"], i["id"], i["message"]] for i in warnings],
    )


def narrative_health(state_choice: str = CUT_STATE):
    historical = state_choice == PROD_STATE and canon_history is not None
    store = canon_history if historical else canon
    findings = store.narrative_audit()
    errors = sum(i["level"] == "ERROR" for i in findings)
    if historical:
        head = (
            f"### {errors} errors —— 制作中的真实发现\n\n"
            "这是 2026-09-12 那一版 canon，影片尚未完成。审计指出：结局的两个必答问题"
            "全部依赖两条**尚未执行的后期补丁**。\n\n"
            "**后来发生的事**：P1 计划在 Shot 05 的导航屏上叠加乘客清单，但该镜头没有可复用的"
            "导航屏素材，补丁做不了。因为这个依赖被显式报了出来，结尾被重新设计成两段纯画面，"
            "而不是在成片里静默失效。"
        )
    elif findings:
        head = f"### {errors} errors\n\n只覆盖可机械判定的 NA-01—NA-08。"
    else:
        head = (
            "### ✅ 成片的剧本因果链完整\n\n"
            "切到「还原制作中状态」可以看到这个工具在制作期实际报出了什么。"
        )
    return head, [[i["level"], i["id"], i["message"]] for i in findings]


def refresh_log():
    """仅在 SHOW_RAW_LOGS=1 时返回原始行。公开环境下这个函数根本不会被绑定。"""
    if not SHOW_RAW_LOGS:
        return []
    return take_log.rows()


def rerun_review(before, image, prompt, observations, previous, following):
    validate_public_image(image)
    if before and "_reference_snapshot" in before:
        previous, following = before["_reference_snapshot"]
    after, table, status = review_after(
        analyzer, before, image, prompt, observations, previous, following
    )
    return table, result_markdown(after) + "\n\n" + status, issues_rows(after), after


def batch_review(shot, mode, prompt, files, state):
    validate_public_image_batch(files)
    store = active_store(state or new_session())
    return rank_takes(analyzer_for(store), shot, mode, prompt, files)


# ──────────────────────────────────────────────────────────────────────
# 界面
# ──────────────────────────────────────────────────────────────────────

api_line = (
    f"已接入模型 `{orch.model_name()}`，CREATE 的理解与创作步骤走模型。"
    if orch.api_configured()
    else "**未配置模型 API** —— 所有功能仍可运行，理解与创作步骤走确定性模板并在轨迹上标注。"
)

with gr.Blocks(title="Passenger Zero · 连续性引擎", theme=gr.themes.Soft()) as demo:
    session = gr.State(new_session())
    audit_state = gr.State({})

    gr.Markdown(
        "# 🎬 AI 短片连戏检查器\n"
        "**上传一段 AI 生成的镜头，我告诉你它有没有连戏问题、要不要重拍。**\n\n"
        "AI 生成的片子里，有些错单看一帧根本发现不了——道具的形状在镜头里悄悄变了，"
        "同一个房间两次拍得不一样。这类错误不在任何一帧里，只在帧与帧之间。\n\n"
        "---\n\n"
        "**它在你的流程里的位置：**\n\n"
        "| | 做什么 | 在哪做 |\n"
        "|---|---|---|\n"
        "| ① | 一句话 → 分镜、每镜 prompt、**一份连戏规则** | 本工具「做分镜」页 |\n"
        "| ② | 拿 prompt 去生成视频 | **Veo／Sora／Runway／可灵／即梦，随便哪个——不在本工具里** |\n"
        "| ③ | 把生成结果传回来，用①那份规则检查 | 本工具「检查素材」页 |\n\n"
        "**①和③是同一份规则的两头**：做分镜时定下「这个空间长什么样」，"
        "检查时就拿它去对。已经有素材的话直接从③开始，那会用《第零号乘客》的成片规则当例子。"
        + (
            f"\n\n> ⚠️ **本实例启用了受控视频生成**（`{os.getenv('VIDEO_MODEL', 'MiniMax-Hailuo-2.3-Fast')}`）。"
            "每次提交都需要访问码并确认费用。"
            if video_provider is not None
            else ""
        )
    )

    with gr.Tabs():
        with gr.Tab("🔍 检查一段素材"):
            active_rules = gr.Markdown()
            gr.Markdown("**没有素材？点一个真实的例子看效果——**")
            with gr.Row():
                demo_a = gr.Button("例子一：控制台变形", size="lg")
                demo_b = gr.Button("例子二：手柄变形 + 钥匙错位", size="lg")

            gr.Markdown("---\n**或者传自己的素材：**")
            audit_text = gr.Textbox(
                label="哪里让你觉得不对？（可以不填）",
                lines=2,
                placeholder="比如：控制手柄的形状好像变了",
            )
            # 缺 ffmpeg 时不渲染视频控件——不展示无法使用的能力。
            # 图片审查那一半照常工作。
            with gr.Row():
                audit_image = gr.Image(type="filepath", label="一张画面", height=200)
                if VIDEO_SUPPORTED:
                    audit_video = gr.Video(
                        sources=["upload"],
                        label="或一段视频（最长 20 秒）",
                        max_length=20,
                        include_audio=False,
                        height=200,
                    )
            if VIDEO_SUPPORTED:
                # 没传素材之前不占版面
                audit_frames = gr.Gallery(
                    label="按时间顺序抽出来的帧",
                    columns=5, rows=1, height=150, type="pil",
                    allow_preview=True, show_download_button=False,
                    visible=False,
                )
                audit_media_note = gr.Markdown()
            else:
                audit_video = audit_frames = audit_media_note = None
                gr.Markdown(f"<sub>ℹ️ {VIDEO_UNAVAILABLE_REASON}</sub>")
            audit_button = gr.Button("检查", variant="primary", size="lg")

            audit_head = gr.Markdown()
            audit_issues = gr.Dataframe(
                headers=["哪里不对", "怎么修"], interactive=False,
                wrap=True, label="", visible=False,
            )
            audit_report_file = gr.File(
                label="⬇ 下载审计报告（JSON + Markdown）",
                visible=False,
            )
            with gr.Accordion("改好后的生成 Prompt", open=False):
                revised_prompt = gr.Textbox(lines=10, label="", show_label=False)
            with gr.Accordion("详细报告（决策代号、评分、规则 ID、执行轨迹）", open=False):
                audit_detail = gr.Markdown()
                audit_trace = gr.Dataframe(headers=TRACE_HEADERS, interactive=False)
            with gr.Accordion("更多选项（记录这次 take、复审、多候选排序）", open=False):
                audit_mode = gr.Radio(
                    ["本地规则（无需 API）", "多模态 API（图像 + canon）"],
                    value=(
                        "多模态 API（图像 + canon）"
                        if analyzer.api_configured
                        else "本地规则（无需 API）"
                    ),
                    label="审计模式",
                )
                model_name_in = gr.Textbox(label="生成该 Take 的模型", placeholder="Veo / Kling / 即梦 / Seedance…")
                filename_in = gr.Textbox(label="输出文件名", placeholder="S19_take03.mp4")
                adoption_in = gr.Radio(["待定", "采纳", "不采纳"], value="待定", label="是否采纳")
                reason_in = gr.Dropdown(
                    ["", "角色不一致", "空间关系错", "动作没做出来", "时长不对",
                     "光比不匹配", "文字/UI问题", "表演不成立"],
                    value="", label="未采纳原因",
                )
                notes_in = gr.Textbox(label="制作备注")
                gr.Markdown("---\n**重跑后复审**：改完 prompt 在外部重新生成，把新图传进来对比。")
                after_image = gr.Image(type="filepath", label="重跑后的 Take")
                after_obs = gr.CheckboxGroup(
                    choices=list(OBSERVATION_OPTIONS), label="仅针对新 Take 的人工观察"
                )
                after_button = gr.Button("复审")
                comparison = gr.Dataframe(
                    headers=["版本", "决策", "分数", "人工项", "Canon"], interactive=False
                )
                after_summary = gr.Markdown()
                after_issues = gr.Dataframe(headers=ISSUE_HEADERS, interactive=False)
                gr.Markdown("---\n**多 Take 排序**：同一镜头的多个候选，一次比完。")
                candidates = gr.File(
                    file_count="multiple",
                    file_types=[".png", ".jpg", ".jpeg", ".webp"],
                    type="filepath",
                    label="候选图片（最多 10 张）",
                )
                batch_shot = gr.Dropdown(canon.shot_ids, value="19", label="Shot")
                batch_button = gr.Button("排序")
                batch_summary = gr.Markdown()
                batch_table = gr.Dataframe(
                    headers=["排名", "文件", "决策", "分数", "需人工", "推荐", "错误"],
                    interactive=False,
                )
                batch_json = gr.JSON(label="逐 Take 审计证据")

        # ── 说明 ──────────────────────────────────────────────────
        # ── 创作 ──────────────────────────────────────────────────
        with gr.Tab("✍️ 做分镜（顺便生成规则）"):
            gr.Markdown("### 写一句话，拿一份可以直接开工的分镜方案")
            create_idea = gr.Textbox(
                label="",
                lines=2,
                placeholder="一个潜水员在沉船里发现了自己的照片",
                show_label=False,
            )
            gr.Examples(CREATE_EXAMPLES, inputs=create_idea, label="或者点一个试试")
            create_button = gr.Button("开始", variant="primary", size="lg")

            create_head = gr.Markdown(visible=False)
            create_plan = gr.Dataframe(
                headers=["#", "拍点", "场景", "时长", "机位", "动作", "锚点", "路由", "生成状态"],
                interactive=False,
                label="分镜计划",
                visible=False,
            )
            create_file = gr.File(label="⬇ 下载完整方案", visible=False)
            with gr.Accordion("看它是怎么一步步做出来的", open=False):
                create_trace = gr.Dataframe(headers=TRACE_HEADERS, interactive=False)
                gr.Markdown(
                    "<sub>「执行方」这一列是重点：**理解和创作可以交给模型，"
                    "但一致性永远由本地规则保证**。</sub>"
                )
            with gr.Accordion("每一镜的生成 Prompt", open=False):
                create_prompts = gr.Markdown()
            with gr.Accordion("方案的因果自检", open=False):
                create_audit = gr.Dataframe(
                    headers=["Level", "ID", "Message"], interactive=False
                )
            with gr.Accordion("更多选项", open=False):
                create_count = gr.Slider(3, 5, value=5, step=1, label="镜头数")
            # 未启用 Provider 时这一整块不存在——不展示无法使用的能力。
            if video_provider is not None:
                with gr.Accordion("受控视频生成（测试空间）", open=True):
                    gr.Markdown(
                        "只生成一个已规划镜头。API Key 不会显示；同一时间最多一个任务，"
                        "提交后请约每 10 秒刷新一次状态。"
                    )
                    video_shot = gr.Dropdown(["1", "2", "3", "4", "5"], value="1", label="Shot")
                    video_load = gr.Button("载入该镜头 Prompt")
                    video_prompt = gr.Textbox(lines=8, label="视频 Prompt")
                    video_first_frame = gr.Image(type="pil", label="首帧（2.3 Fast 必填）")
                    video_cost = gr.Markdown()
                    video_access_code = gr.Textbox(label="测试访问码", type="password")
                    video_confirm = gr.Checkbox(label="我确认本次调用会产生费用")
                    with gr.Row():
                        video_submit = gr.Button("确认并提交视频任务", variant="primary")
                        video_refresh = gr.Button("刷新生成状态")
                    video_status = gr.Markdown()
                    video_output = gr.Video(label="生成结果", visible=False)

        # ── 审查 ──────────────────────────────────────────────────
        with gr.Tab("ℹ️ 说明"):
            gr.Markdown(
                f"""
### 这个工具解决什么

拍电影有个岗位叫**场记**，专门盯「上一镜和这一镜对不对得上」——杯子在左手还是右手，
钥匙插在哪儿，灯是不是亮的。

AI 生成短片没有这个岗位。每一镜都是独立的一次生成，模型不记得上一次画了什么。
循环、闪回、多时间线的片子尤其严重：同一个空间要反复拍很多次。

本工具把**你锁定的剧情事实**编译成逐镜头的检查项，让这件事可以被机械验证。

### 四种结论

| | 意思 |
|---|---|
| ✅ 看起来没问题 | 在能检查的范围内没发现违规 |
| 🔧 后期能修 | 不影响动作和空间关系，擦掉就行，**不用重拍** |
| 🔁 得重新生成 | 结构性错误，后期补不回来 |
| 🤔 我不确定 | 涉及表演或证据不足，**不替你下结论** |

最后一档是刻意保留的。视觉模型会看错，如果没有「我不确定」这个出口，
它会自信地误判，而你三次之后就不再信任它的任何输出。

### 它会告诉你结论是怎么来的

没传素材的时候，结论只是「**如果**你描述的情况属实，规则判定为此」，
系统并没有看过画面——这一点会写在结果下面，不会含糊过去。

### 它不做什么

- **不生成视频**，不剪辑，不配乐
- **不改写剧情**——剧情事实由人维护，本工具只读
- 不保证 `✅` 等于「这一镜没问题」，只等于「在能检查的范围内没发现问题」

{api_line}

---

<sub>完整的架构说明、规则设计、测试报告、已知缺口和部署方法都在代码仓库里。
这一页只讲用得上的部分。</sub>
                """
            )

        # ── 技术细节 ───────────────────────────────────────────────
        with gr.Tab("🔧 技术细节"):
            gr.Markdown("<sub>普通使用不需要这一页。</sub>")
            with gr.Accordion("剧本因果审计（Agent 5）", open=True):
                audit_target = gr.Radio([CUT_STATE, PROD_STATE], value=CUT_STATE, label="审计对象")
                narrative_summary = gr.Markdown()
                narrative_table = gr.Dataframe(
                    headers=["Level", "ID", "Message"], interactive=False
                )
            with gr.Accordion("逐镜规则包", open=False):
                detail_shot = gr.Dropdown(canon.shot_ids, value="19", label="Shot")
                detail_text = gr.Markdown()
                detail_json = gr.Code(label="", language="json")
            with gr.Accordion("Canon 结构自检", open=False):
                health_button = gr.Button("重新检查")
                health_summary = gr.Markdown()
                health_table = gr.Dataframe(
                    headers=["Level", "ID", "Message"], interactive=False
                )
            with gr.Accordion("生产日志", open=False):
                if SHOW_RAW_LOGS:
                    gr.Markdown(
                        "⚠️ **原始日志已开启（`SHOW_RAW_LOGS=1`）。** "
                        "日志全进程共享，包含所有使用者的输入——只应在私有部署中开启。"
                    )
                    refresh_button = gr.Button("刷新")
                else:
                    gr.Markdown(
                        "**公开环境不展示原始日志。** 日志在服务端照常记录，但它是全进程"
                        "共享的一份 CSV，公开渲染等于泄露他人输入。下面只有聚合统计。"
                    )
                    refresh_button = gr.Button("刷新统计")
                statistics = gr.Markdown()
                reason_table = gr.Dataframe(headers=["未采纳原因", "数量"], interactive=False)
                log_table = (
                    gr.Dataframe(
                        headers=FIELDS[:-1],
                        datatype=["str"] * (len(FIELDS) - 1),
                        interactive=False,
                    )
                    if SHOW_RAW_LOGS
                    else None
                )

    # ── 事件绑定 ──────────────────────────────────────────────────
    create_button.click(
        do_create,
        inputs=[create_idea, create_count, session],
        outputs=[create_head, create_plan, create_prompts, create_audit, create_trace,
                 create_file, session],
    )
    # 同理：未启用 Provider 时这三个回调不进入事件表，也就不会出现在
    # /gradio_api/info —— 隐藏控件从来不等于访问控制，不绑定才是。
    if video_provider is not None:
        video_load.click(
            prepare_video,
            inputs=[video_shot, session],
            outputs=[video_prompt, video_cost],
        )
        video_submit.click(
            submit_video,
            inputs=[video_shot, video_prompt, video_first_frame, video_access_code,
                    video_confirm, session],
            outputs=[video_status, video_output, session],
        )
        video_refresh.click(
            refresh_video,
            inputs=session,
            outputs=[video_status, video_output, session],
        )
    audit_inputs = [audit_text, audit_image, audit_mode, session, model_name_in,
                    filename_in, adoption_in, reason_in, notes_in]
    if VIDEO_SUPPORTED:
        audit_inputs.append(audit_video)
    audit_button.click(
        do_audit,
        inputs=audit_inputs,
        outputs=[audit_head, audit_issues, audit_detail, revised_prompt, audit_trace,
                 audit_report_file, session],
    )
    if VIDEO_SUPPORTED:
        audit_video.change(
            show_frames,
            inputs=audit_video,
            outputs=[audit_frames, audit_media_note],
        )
    audit_button.click(take_log.stats, outputs=[statistics, reason_table])
    for button, key in ((demo_a, "A"), (demo_b, "B")):
        button.click(
            lambda state, key=key: run_demo(key, state),
            inputs=session,
            outputs=[audit_text, audit_head, audit_issues, audit_detail, revised_prompt,
                     audit_trace, audit_report_file, session],
        )
        if VIDEO_SUPPORTED:
            button.click(
                lambda key=key: demo_media(key),
                outputs=[audit_video, audit_frames, audit_media_note],
            )
    after_button.click(
        rerun_review,
        inputs=[audit_state, after_image, revised_prompt, after_obs, audit_image, audit_image],
        outputs=[comparison, after_summary, after_issues, audit_state],
    )
    batch_button.click(
        batch_review,
        inputs=[batch_shot, audit_mode, audit_text, candidates, session],
        outputs=[batch_summary, batch_table, batch_json],
    )
    detail_shot.change(shot_detail, inputs=[detail_shot, session], outputs=[detail_text, detail_json])
    audit_target.change(narrative_health, inputs=audit_target, outputs=[narrative_summary, narrative_table])
    health_button.click(canon_health, inputs=session, outputs=[health_summary, health_table])
    # 关键：公开环境下 refresh_log 根本不绑定，因此不会出现在 /gradio_api/info，
    # 也就无法通过公开 API 调用。聚合统计不含自由文本，可以保留。
    if SHOW_RAW_LOGS:
        refresh_button.click(refresh_log, outputs=log_table)
    refresh_button.click(take_log.stats, outputs=[statistics, reason_table])

    session.change(rules_note, inputs=session, outputs=active_rules)
    demo.load(rules_note, inputs=session, outputs=active_rules)
    demo.load(shot_detail, inputs=[detail_shot, session], outputs=[detail_text, detail_json])
    demo.load(narrative_health, inputs=audit_target, outputs=[narrative_summary, narrative_table])
    demo.load(canon_health, inputs=session, outputs=[health_summary, health_table])
    if SHOW_RAW_LOGS:
        demo.load(refresh_log, outputs=log_table)
    demo.load(take_log.stats, outputs=[statistics, reason_table])


# ── 托管环境兜底：不走 launch() 时补齐上传所需的属性 ──────────────────
# `Blocks.max_file_size` **只在 demo.launch() 里才被赋值**，它不是类属性。
# 而 `/gradio_api/upload` 路由会无条件读 `app.get_blocks().max_file_size`。
# 有些托管方式（包括创空间的部分启动路径）是 import 本文件再自行挂载 demo，
# 根本不执行 `if __name__ == "__main__"` 这一段，于是：页面能开、审查能跑、
# 例子能点，**唯独一上传文件就 500**。视频比图片更大，这里按视频上限取值。
# 真正走 launch() 时同名参数会覆盖它，两条路径一致。
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "60")) * 1024 * 1024
if not hasattr(demo, "max_file_size"):
    demo.max_file_size = MAX_UPLOAD_BYTES


def bypass_proxy_for_localhost() -> None:
    """让本机自检请求绕开系统代理。

    Gradio 启动后会自己请求一次 `http://localhost:<port>/gradio_api/startup-events`。
    如果机器上设了 HTTP(S)_PROXY（科学上网工具、公司代理都会设），这个**本机**
    请求也会被塞进代理，返回 503，于是应用直接启动失败：

        Couldn't start the app because 'http://localhost:7860/...' failed (code 503)

    报错里只说「检查网络或代理设置」，第一次遇到的人很难联想到是本机回环被代理了。
    这里把回环地址补进 NO_PROXY（追加，不覆盖用户已有的配置）。
    """
    loopback = ["localhost", "127.0.0.1", "0.0.0.0", "::1"]
    for name in ("NO_PROXY", "no_proxy"):
        current = [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]
        merged = current + [host for host in loopback if host not in current]
        os.environ[name] = ",".join(merged)


if __name__ == "__main__":
    bypass_proxy_for_localhost()
    # 默认 0.0.0.0 是给容器部署用的；本机跑不起来时可以 HOST=127.0.0.1。
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "7860"))
    # show_error 会把 traceback（含容器内绝对路径）推到浏览器，公开环境必须关。
    options = dict(
        server_port=port,
        show_error=SHOW_RAW_LOGS,
        max_file_size=MAX_UPLOAD_BYTES,
    )
    try:
        demo.launch(server_name=host, **options)
    except Exception as exc:  # noqa: BLE001
        # 有些 Windows 环境绑 0.0.0.0 之后自检仍然失败。与其让人对着
        # traceback 发愣，不如换回环地址自动再试一次，并说清楚发生了什么。
        if host == "0.0.0.0":
            print(f"\n⚠️  以 0.0.0.0 启动失败：{exc}")
            print("→ 改用 127.0.0.1 重试（只有本机能访问，本地使用没有影响）。")
            print("   如果仍然失败，多半是端口被占用：换一个，例如 PORT=7870。\n")
            demo.launch(server_name="127.0.0.1", **options)
        else:
            raise
