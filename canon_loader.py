from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

LOCATION_ALIASES = {
    "passenger_ring 导航屏": "passenger_ring",
    "passenger_ring_导航屏": "passenger_ring",
}


def _dedupe_rules(rules: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for rule in rules:
        rule_id = rule.get("id", "")
        if not rule_id or rule_id in seen:
            continue
        seen.add(rule_id)
        result.append(rule)
    return result


class CanonStore:
    """Read-only access to Claude's machine-readable continuity canon."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        with self.path.open("r", encoding="utf-8") as handle:
            self.data: dict[str, Any] = json.load(handle)
        self._rule_index = self._build_rule_index()

    def _build_rule_index(self) -> dict[str, dict[str, Any]]:
        rules: list[dict[str, Any]] = []
        rules.extend(self.data.get("global_rules", []))
        rules.extend(self.data.get("camera", {}).get("rules", []))
        for character in self.data.get("characters", {}).values():
            rules.extend(character.get("rules", []))
        for location in self.data.get("locations", {}).values():
            rules.extend(location.get("rules", []))
        for attempt in self.data.get("attempts", {}).values():
            rules.extend(attempt.get("rules", []))
        for shot in self.data.get("shots", {}).values():
            rules.extend(shot.get("rules", []))
        return {rule["id"]: rule for rule in rules if rule.get("id")}

    @property
    def shot_ids(self) -> list[str]:
        return sorted(self.data.get("shots", {}).keys(), key=lambda value: int(value))

    @property
    def ledger(self) -> dict[str, Any]:
        return self.data.get("knowledge_ledger", {})

    @property
    def knowledge_ledger(self) -> dict[str, Any]:
        """Backward-compatible explicit name for the read-only ledger."""
        return self.ledger

    @property
    def anchor_plan(self) -> dict[str, Any]:
        return self.data.get("anchor_plan", {})

    @property
    def routing(self) -> dict[str, Any]:
        return self.data.get("routing", {})

    def get_shot(self, shot_id: str | int) -> dict[str, Any]:
        key = str(shot_id).zfill(2).lstrip("0") or "0"
        # Canon keys are unpadded strings such as "13".
        if key not in self.data.get("shots", {}):
            key = str(shot_id)
        if key not in self.data.get("shots", {}):
            raise KeyError(f"Shot {shot_id} is not present in canon.json")
        return self.data["shots"][key]

    def normalize_location(self, location: str | None) -> tuple[str | None, bool]:
        if not location:
            return None, False
        if location in self.data.get("locations", {}):
            return location, True
        if location in LOCATION_ALIASES:
            return LOCATION_ALIASES[location], True
        return location, False

    def characters_for_shot(self, shot_id: str | int) -> list[str]:
        number = int(shot_id)
        result = ["main_system"]  # The no-embodiment rule applies globally.
        if number not in (22, 26):
            result.append("daniel")
        if number in (15, 16, 19, 20, 21, 23, 24, 25):
            result.append("047")
        if number == 22:
            result.append("engineer")
        return result

    def anchor_for(self, shot_id: str | int) -> dict[str, Any]:
        return self.anchor_plan.get("shots", {}).get(
            str(int(shot_id)), {"anchor": "unknown"}
        )

    def knows_at(self, shot_id: str | int) -> dict[str, list[str]]:
        number = int(shot_id)
        result: dict[str, list[str]] = {"daniel": [], "audience": []}
        for fact_id, fact in self.ledger.get("facts", {}).items():
            for subject, field in (
                ("daniel", "daniel_knows_from"),
                ("audience", "audience_knows_from"),
            ):
                known_from = fact.get(field)
                if isinstance(known_from, int) and (known_from == 0 or known_from <= number):
                    result[subject].append(fact_id)
        return result

    def route(self, shot_id: str | int) -> dict[str, Any]:
        shot = self.get_shot(shot_id)
        if not shot.get("generation_required", True):
            tier = "post_only"
            reason = "纯后期镜头，不进入视频生成流程。"
        elif shot.get("identity_critical", False):
            tier = "primary"
            reason = "身份关键镜头：锁定单一主力模型，同批次生成，尽量不跨供应商。"
        else:
            tier = "economy"
            reason = "非身份关键镜头：使用最便宜的可用模型，失败重跑成本较低。"

        override = next(
            (
                item
                for item in self.routing.get("overrides", [])
                if int(item.get("shot", -1)) == int(shot_id)
            ),
            {},
        )
        return {
            "tier": tier,
            "reason": reason,
            "split_generation": bool(override.get("split_generation", False)),
            "override_note": override.get("note", ""),
        }

    def route_for_shot(self, shot_id: str | int) -> dict[str, Any]:
        return self.route(shot_id)

    def compile_rule_pack(self, shot_id: str | int) -> dict[str, Any]:
        shot = self.get_shot(shot_id)
        attempt_id = shot.get("attempt")
        raw_location = shot.get("location")
        location_key, location_known = self.normalize_location(raw_location)

        rules: list[dict[str, Any]] = []
        rules.extend(self.data.get("global_rules", []))
        rules.extend(self.data.get("camera", {}).get("rules", []))
        if location_key in self.data.get("locations", {}):
            rules.extend(self.data["locations"][location_key].get("rules", []))
        for character_name in self.characters_for_shot(shot_id):
            rules.extend(
                self.data.get("characters", {}).get(character_name, {}).get("rules", [])
            )
        if attempt_id:
            rules.extend(
                self.data.get("attempts", {}).get(str(attempt_id), {}).get("rules", [])
            )
        rules.extend(shot.get("rules", []))

        route = self.route(shot_id)
        return {
            "shot_id": str(shot_id),
            "attempt_id": attempt_id,
            "location": raw_location,
            "normalized_location": location_key,
            "location_known": location_known,
            "duration_sec": shot.get("duration_sec"),
            "camera": shot.get("camera", []),
            "identity_critical": bool(shot.get("identity_critical")),
            "generation_required": shot.get("generation_required", True),
            "must_show": shot.get("must_show", []),
            "must_not_show": shot.get("must_not_show", []),
            "reusable": shot.get("reusable"),
            "note": shot.get("note"),
            "anchor": self.anchor_for(shot_id),
            "routing_tier": route["tier"],
            "routing": route,
            "rules": [
                rule
                for rule in _dedupe_rules(rules)
                if shot.get("generation_required", True)
                and (
                    "only_shots" not in rule
                    or str(int(shot_id)) in {str(s) for s in rule["only_shots"]}
                )
                and (
                    "scope_locations" not in rule
                    or location_key in rule["scope_locations"]
                )
            ],
        }

    def narrative_audit(
        self, patch_overrides: dict[str, bool] | None = None
    ) -> list[dict[str, str]]:
        """Audit mechanical story causality without mutating the narrative canon."""
        findings: list[dict[str, str]] = []
        facts = self.ledger.get("facts", {})
        shots = self.data.get("shots", {})
        rule_defs = {
            item.get("id"): item
            for item in self.data.get("narrative_audit_rules", {}).get("rules", [])
        }

        def add(rule_id: str, message: str, level: str | None = None) -> None:
            rule = rule_defs.get(rule_id, {})
            findings.append(
                {
                    "level": level or rule.get("severity", "WARN"),
                    "id": rule_id,
                    "message": message,
                }
            )

        # NA-01 / NA-03 operate on explicit, human-authored fact references only.
        # The current canon has none; never infer them from prose or dialogue.
        for shot_key, shot in shots.items():
            number = int(shot_key)
            references = []
            for field in ("fact_refs", "uses_facts", "dialogue_fact_refs"):
                references.extend(shot.get(field, []))
            for fact_id in set(references):
                fact = facts.get(fact_id)
                if not fact:
                    continue
                established = fact.get("established_in")
                if isinstance(established, int) and established > number:
                    add(
                        "NA-01",
                        f"Shot {shot_key} 引用了尚未建立的 {fact_id}『{fact.get('text', '')}』；首次建立在 Shot {established}。",
                    )
                daniel_from = fact.get("daniel_knows_from")
                audience_from = fact.get("audience_knows_from")
                if (
                    isinstance(daniel_from, int)
                    and isinstance(audience_from, int)
                    and (daniel_from == 0 or daniel_from <= number)
                    and audience_from > number
                ):
                    add(
                        "NA-03",
                        f"Shot {shot_key} 中 Daniel 依据 {fact_id} 行动，但观众要到 Shot {audience_from} 才知道该事实。",
                    )

        # NA-02: the ledger may optionally record an exact paid_off_in. Until then,
        # validate that the setup itself is no later than its declared deadline.
        for fact_id, fact in facts.items():
            deadline = fact.get("must_pay_off_by")
            if not isinstance(deadline, int):
                continue
            payoff = fact.get("paid_off_in", deadline)
            if not isinstance(payoff, int) or payoff > deadline:
                add(
                    "NA-02",
                    f"{fact_id}『{fact.get('text', '')}』要求最晚在 Shot {deadline} 回收，但记录的回收镜头为 {payoff}。",
                )

        # NA-04: later attempts must retain all knowledge carried into earlier ones.
        attempt_state = self.ledger.get("attempt_state", {})
        ordered_attempts = sorted(attempt_state, key=lambda value: int(value))
        for previous_id, current_id in zip(ordered_attempts, ordered_attempts[1:]):
            previous = set(attempt_state[previous_id].get("daniel_enters_knowing", []))
            current = set(attempt_state[current_id].get("daniel_enters_knowing", []))
            missing = sorted(previous - current)
            if missing:
                add(
                    "NA-04",
                    f"Attempt {current_id} 丢失了 Attempt {previous_id} 已保留的事实：{', '.join(missing)}。",
                )

        # NA-05: chain anchors cannot cross an attempt or point forward.
        def attempt_for(shot_number: int) -> str | None:
            explicit = shots.get(str(shot_number), {}).get("attempt")
            if explicit is not None:
                return str(int(explicit))
            for attempt_id, state in attempt_state.items():
                if shot_number in state.get("shots", []):
                    return str(int(attempt_id))
            return None

        for shot_key, anchor in self.anchor_plan.get("shots", {}).items():
            if not str(anchor.get("anchor", "")).startswith("chain"):
                continue
            current = int(shot_key)
            source = anchor.get("from_shot")
            if not isinstance(source, int):
                add("NA-05", f"Shot {shot_key} 的 chain 锚点缺少有效 from_shot。")
                continue
            current_attempt = attempt_for(current)
            source_attempt = attempt_for(source)
            if current_attempt != source_attempt:
                add(
                    "NA-05",
                    f"Shot {shot_key} 从 Shot {source} 续接，但两者分属 Attempt {current_attempt}/{source_attempt}。",
                )
            if source >= current:
                add("NA-05", f"Shot {shot_key} 的锚点来源 Shot {source} 不是更早镜头。")
            if source != current - 1 and not anchor.get("skip_reason"):
                add(
                    "NA-05",
                    f"Shot {shot_key} 非紧邻续接 Shot {source}，但没有 skip_reason。",
                    level="WARN",
                )

        # NA-06: a mechanism must be demonstrated before it drives the plot.
        for mechanism_id, mechanism in self.ledger.get("mechanisms", {}).items():
            shown = mechanism.get("demonstrated_in")
            relied = mechanism.get("relied_on_in")
            if not isinstance(shown, int) or not isinstance(relied, int) or shown >= relied:
                add(
                    "NA-06",
                    f"{mechanism_id}『{mechanism.get('text', '')}』在 Shot {relied} 被依赖，但演示镜头为 {shown}。",
                )

        # NA-07: patch_overrides is read-only session state and never writes canon.
        patch_state = {
            item.get("id"): bool(item.get("applied", False))
            for item in self.data.get("post_production_patches_on_locked_shots", [])
        }
        patch_state.update(patch_overrides or {})
        for fact_id, fact in facts.items():
            patch_id = fact.get("audience_depends_on_patch")
            if patch_id and not patch_state.get(patch_id, False):
                add(
                    "NA-07",
                    f"{fact_id}『{fact.get('text', '')}』依赖补丁 {patch_id}，{patch_id} 未应用。",
                )

        # NA-08: prerequisite information must reach the audience earlier.
        # Exception: facts delivered by the SAME line in the same shot (a shared
        # delivery_bundle) cannot be ordered against each other — the audience
        # receives them simultaneously. Shot 19's 047 line carries four at once.
        for fact_id, fact in facts.items():
            reveal = fact.get("audience_knows_from")
            bundle = fact.get("delivery_bundle")
            for required_id in fact.get("requires_facts", []):
                required = facts.get(required_id, {})
                if bundle and bundle == required.get("delivery_bundle"):
                    continue
                prerequisite = required.get("audience_knows_from")
                if (
                    not isinstance(reveal, int)
                    or not isinstance(prerequisite, int)
                    or prerequisite >= reveal
                ):
                    add(
                        "NA-08",
                        f"{fact_id} 的揭示缺少更早的前置事实 {required_id}（观众知情镜头 {prerequisite} / {reveal}）。",
                    )

        return findings

    def rule(self, rule_id: str) -> dict[str, Any] | None:
        return self._rule_index.get(rule_id)

    def lint(self) -> list[dict[str, str]]:
        """Find contradictions or schema problems without changing the story."""
        warnings: list[dict[str, str]] = []
        shots = self.data.get("shots", {})

        expected = {str(number) for number in range(13, 27)}
        missing = sorted(expected - set(shots), key=int)
        if missing:
            warnings.append(
                {
                    "level": "ERROR",
                    "id": "CANON-MISSING-SHOTS",
                    "message": f"Missing shot definitions: {', '.join(missing)}",
                }
            )

        for shot_id, shot in shots.items():
            attempt = shot.get("attempt")
            if attempt:
                declared = (
                    self.data.get("attempts", {}).get(str(attempt), {}).get("shots", [])
                )
                if int(shot_id) not in declared:
                    warnings.append(
                        {
                            "level": "ERROR",
                            "id": "CANON-ATTEMPT-MISMATCH",
                            "message": f"Shot {shot_id} says Attempt {attempt}, but the attempt shot list disagrees.",
                        }
                    )

            raw_location = shot.get("location")
            normalized, known = self.normalize_location(raw_location)
            if not known and shot.get("generation_required", True):
                warnings.append(
                    {
                        "level": "WARN",
                        "id": "CANON-LOCATION-UNKNOWN",
                        "message": f"Shot {shot_id} uses an unrecognized location key: {raw_location}.",
                    }
                )
            elif raw_location != normalized:
                warnings.append(
                    {
                        "level": "INFO",
                        "id": "CANON-LOCATION-ALIAS",
                        "message": f"Shot {shot_id}: {raw_location} is normalized to {normalized} by the app.",
                    }
                )

        shot_21 = shots.get("21", {})
        if str(shot_21.get("attempt")) == "05" and any(
            "红光退去" in item for item in shot_21.get("must_show", [])
        ):
            warnings.append(
                {
                    "level": "WARN",
                    "id": "CANON-CONFLICT-RED",
                    "message": "Shot 21 requires ‘红光退去’, while LOOP-05B says Attempt 05 must never show red alert light. Narrative supervisor should choose ‘冷白闪烁停止’ or explicitly allow red.",
                }
            )

        shot_13_cameras = " ".join(shots.get("13", {}).get("camera", []))
        if "穿越" in shot_13_cameras:
            warnings.append(
                {
                    "level": "WARN",
                    "id": "CANON-CAMERA-SCOPE",
                    "message": "Shot 13 says ‘穿越’, while CAM-02 bans tracking/orbit/crane moves. Clarify whether CAM-02 applies only to Bay 07 or to the whole film.",
                }
            )

        identity_list = {
            str(value) for value in self.data.get("identity_critical_shots", [])
        }
        identity_flags = {
            shot_id for shot_id, shot in shots.items() if shot.get("identity_critical")
        }
        if identity_list != identity_flags:
            warnings.append(
                {
                    "level": "ERROR",
                    "id": "CANON-IDENTITY-LIST",
                    "message": "identity_critical_shots does not match per-shot identity_critical flags.",
                }
            )
        return warnings

    def rule_pack_markdown(self, shot_id: str | int) -> str:
        pack = self.compile_rule_pack(shot_id)
        anchor = pack["anchor"]
        route = pack["routing"]
        lines = [
            f"### Shot {pack['shot_id']} · Attempt {pack['attempt_id'] or '—'}",
            f"- 场景：`{pack['location']}`（内部映射：`{pack['normalized_location']}`）",
            f"- 时长：{pack['duration_sec']} 秒；身份关键镜头：{'是' if pack['identity_critical'] else '否'}",
            f"- 机位：{', '.join(pack['camera']) if pack['camera'] else '—'}",
            f"- 锚点：`{anchor.get('anchor', 'unknown')}`；{anchor.get('reason', '未提供理由')}",
            f"- 模型路由：`{route['tier']}`；{route['reason']}",
        ]
        if anchor.get("reference"):
            lines.append(f"- 参考资产：`{anchor['reference']}`")
        if anchor.get("crosscheck_reference"):
            lines.append(f"- 交叉校验资产：`{anchor['crosscheck_reference']}`")
        if route.get("override_note"):
            lines.append(f"- 拆分生成：{route['override_note']}")
        if not pack["generation_required"]:
            lines.append("- 本镜为纯后期镜头，不进入视频生成流程。")
        if pack.get("reusable"):
            lines.append(f"- 可复用：{pack['reusable']}")
        lines.append("\n**必须出现**")
        lines.extend(f"- {item}" for item in pack["must_show"])
        lines.append("\n**禁止出现**")
        lines.extend(f"- {item}" for item in pack["must_not_show"])
        lines.append(f"\n**本次载入 {len(pack['rules'])} 条可执行规则。**")
        lines.append("\n规则溯源（原始 canon 内容；仅在可见且适用时判断）：\n")
        for rule in pack["rules"]:
            lines.append(
                f"- `{rule['id']}`：{rule['text']}；依据：{rule.get('note', '未提供 note')}；级别：{rule['severity']}"
            )
        return "\n".join(lines)
