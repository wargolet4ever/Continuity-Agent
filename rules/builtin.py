"""Built-in deterministic rule plugins preserving the v0.1 behaviour."""

from __future__ import annotations

from dataclasses import dataclass

from visual_metrics import image_metrics

from .base import Issue, IssueFactory, RuleContext, RuleRegistry

OBSERVATION_RULES = {
    "small_screen": (
        "LOC-B03",
        "Prop Continuity",
        "画面出现了 canon 禁止的小型显示设备。",
    ),
    "console_deformation": (
        "LOC-B06",
        "Temporal Geometry",
        "按时间顺序对比关键帧，控制台或固定机械组件发生了形变／重构。",
    ),
    "key_wrong": ("SHOT-21A", "Scene + Prop", "钥匙插槽未固定在控制台右端。"),
    "both_hands_leave": (
        "SHOT-19A",
        "Action Continuity",
        "047 双手同时离开拉杆，破坏了核心机制。",
    ),
    "red_attempt05": (
        "LOOP-05B",
        "Loop Divergence",
        "Attempt 05 出现了属于 Attempt 04 的红色警报光。",
    ),
    "system_body": (
        "CHAR-S01",
        "System Embodiment",
        "主系统被赋予了 canon 禁止的视觉身体。",
    ),
    "daniel_identity": (
        "CHAR-D01",
        "Character Identity",
        "Daniel 与 Shot 02 基准帧不一致。",
    ),
    "047_identity": (
        "CHAR-0401",
        "Character Identity",
        "047 与 Shot 15 基准帧不一致。",
    ),
    "moving_camera": (
        "CAM-02",
        "Camera Continuity",
        "镜头包含 canon 禁止的机位移动。",
    ),
    "readable_text": (
        "TEXT-01",
        "UI/Text",
        "生成画面出现可读文字；应在 AE 中覆盖。",
    ),
    "performance_uncertain": (
        "CHAR-0403",
        "Performance",
        "047 的表演动机无法仅靠自动规则可靠判定。",
    ),
    "axis_wrong": (
        "CAM-01",
        "Axis Continuity",
        "047、Daniel 或钥匙插槽的左右关系越轴。",
    ),
    "lever_motion_wrong": (
        "SHOT-21B",
        "Action Continuity",
        "Shot 21 的拉杆没有缓缓升起。",
    ),
    "extra_person": (
        "LOC-B05",
        "Scene Continuity",
        "Bay 07 出现了第四个人物或人形轮廓。",
    ),
}


@dataclass(frozen=True)
class ObservationRulePlugin:
    plugin_id: str = "builtin.observations"
    priority: int = 100

    def evaluate(
        self, context: RuleContext, issue_factory: IssueFactory
    ) -> list[Issue]:
        issues: list[Issue] = []
        for key in sorted(context.observations):
            if key == "manual_pass_confirmed":
                continue
            mapping = OBSERVATION_RULES.get(key)
            if key == "performance_uncertain" and int(context.shot_id) == 16:
                mapping = (
                    "CHAR-0405",
                    "Performance",
                    "Shot 16 惊恐表演需要人工确认。",
                )
            if key == "small_screen" and int(context.shot_id) == 14:
                mapping = (
                    "LOC-X03",
                    "Prop Continuity",
                    "舱门外出现新增显示设备。",
                )
            if mapping:
                issues.append(issue_factory(*mapping, 1.0, ""))
        return issues


@dataclass(frozen=True)
class AttemptRedPixelPlugin:
    plugin_id: str = "builtin.attempt-red-pixel"
    priority: int = 200
    threshold: float = 0.35

    def evaluate(
        self, context: RuleContext, issue_factory: IssueFactory
    ) -> list[Issue]:
        if str(context.pack.get("attempt_id")) != "05":
            return []
        metrics = image_metrics(context.current_image)
        red_ratio = metrics.get("red_ratio", 0)
        if red_ratio < self.threshold:
            return []
        issue = issue_factory(
            "LOOP-05B",
            "Loop Divergence",
            f"自动色彩候选：饱和红像素占比 {red_ratio:.1%}，达到 {self.threshold:.0%} 阈值。颜色无法证明警报来源，需人工确认。",
            0.76,
            "",
        )
        issue["requires_confirmation"] = True
        return [issue]


def default_rule_registry() -> RuleRegistry:
    return RuleRegistry((ObservationRulePlugin(), AttemptRedPixelPlugin()))
