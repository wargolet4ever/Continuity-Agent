"""把 CREATE 的结果打包成可下载的 Production Package。"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_BANNER = "AWAITING EXTERNAL GENERATION"


def _shot_plan_md(result: dict[str, Any]) -> str:
    story = result["story"]
    lines = [
        f"# {story.get('title', '')} · 分镜计划",
        "",
        f"**Logline**：{story.get('logline', '')}",
        f"**主角**：{story.get('protagonist', '')}",
        f"**冲突**：{story.get('conflict', '')}",
        f"**基调**：{story.get('tone', '')}",
        "",
        f"共 {len(result['shots'])} 镜，合计 "
        f"{sum(s['duration_sec'] for s in result['shots'])} 秒。",
        "",
        "| # | 拍点 | 场景 | 时长 | 机位 | 动作 | 锚点 | 路由 | 生成状态 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    anchors = result["canon_draft"]["anchor_plan"]["shots"]
    for shot in result["shots"]:
        sid = shot["shot_id"]
        anchor = anchors[sid]
        target = (
            f"续接 Shot {anchor['from_shot']}"
            if anchor["anchor"] == "chain"
            else f"回查 {anchor.get('reference', '')}"
        )
        lines.append(
            f"| {sid} | {shot['beat_name']} | `{shot['location']}` | {shot['duration_sec']}s "
            f"| {shot['camera']} | {shot['action']} | {target} "
            f"| {result['routing'][sid]['tier']} | {STATUS_BANNER} |"
        )
    lines += [
        "",
        "> 本计划未生成任何视频。每一镜的 `status` 都是 "
        f"`{STATUS_BANNER}`，需由外部生成平台执行。",
    ]
    return "\n".join(lines)


def _prompts_md(result: dict[str, Any]) -> str:
    lines = [f"# {result['story'].get('title', '')} · 视觉 Prompt", ""]
    for shot in result["shots"]:
        sid = shot["shot_id"]
        prompt = result["prompts"][sid]
        lines += [
            f"## Shot {sid} · {shot['beat_name']}（{shot['duration_sec']}s · "
            f"{result['routing'][sid]['tier']}）",
            "",
            "**正向**",
            "",
            "```",
            prompt["positive"],
            "```",
            "",
            "**负向**",
            "",
            "```",
            prompt["negative"],
            "```",
            "",
        ]
    return "\n".join(lines)


def _model_plan_md(result: dict[str, Any]) -> str:
    lines = [
        "# 模型策略",
        "",
        "二元规则：`identity_critical == true → primary_tier`，否则 `economy_tier`。",
        "**只输出决策，不做实时派发** —— 派发由人执行。",
        "",
        "| Shot | 层级 | 理由 |",
        "|---|---|---|",
    ]
    for sid, route in result["routing"].items():
        lines.append(f"| {sid} | {route['tier']} | {route['reason']} |")
    return "\n".join(lines)


def _audit_md(result: dict[str, Any]) -> str:
    findings = result["findings"]
    lines = ["# 草案因果审计（NA-01—NA-08）", ""]
    if not findings:
        lines.append("✅ 未发现可机械判定的因果问题。")
        lines.append("")
        lines.append("> 这只覆盖结构性检查，不评价故事好坏。")
    else:
        lines += ["| Level | ID | 说明 |", "|---|---|---|"]
        lines += [f"| {f['level']} | {f['id']} | {f['message']} |" for f in findings]
    return "\n".join(lines)


def _readme_txt(result: dict[str, Any]) -> str:
    trace = result["trace"]
    used = "已接入模型" if result["api_used"] else "本地降级（未配置模型 API）"
    return "\n".join(
        [
            f"{result['story'].get('title', '')} — Production Package",
            f"生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"运行模式：{used}",
            f"总耗时：{trace.total_ms / 1000:.1f}s",
            "",
            "包含：",
            "  shot_plan.md      分镜计划（含锚点与路由）",
            "  prompts.md        每镜正向/负向 prompt",
            "  model_plan.md     模型策略",
            "  narrative_audit.md 草案因果审计结果",
            "  canon_draft.json  可被 CanonStore 载入的 canon 草案",
            "  trace.json        本次执行的完整轨迹",
            "",
            "=" * 56,
            f"生成状态：{STATUS_BANNER}",
            "本包不包含任何视频。所有镜头的 status 均为",
            "AWAITING_EXTERNAL_GENERATION，需由外部生成平台执行。",
            "=" * 56,
        ]
    )


def build_package(result: dict[str, Any]) -> str:
    root = Path(result["workdir"])
    title = result["story"].get("title", "package") or "package"
    safe = "".join(c for c in title if c.isalnum() or c in "-_")[:20] or "package"
    target = root / f"production_package_{safe}.zip"

    files = {
        "shot_plan.md": _shot_plan_md(result),
        "prompts.md": _prompts_md(result),
        "model_plan.md": _model_plan_md(result),
        "narrative_audit.md": _audit_md(result),
        "canon_draft.json": json.dumps(
            result["canon_draft"], ensure_ascii=False, indent=2
        ),
        "trace.json": json.dumps(
            result["trace"].as_dict(), ensure_ascii=False, indent=2
        ),
        "README.txt": _readme_txt(result),
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return str(target)
