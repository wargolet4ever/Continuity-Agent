"""CREATE 模式的产物构造。

分工原则：模型负责理解与创作（故事理解、镜头规划、视觉 prompt），
规则负责保证一致性（canon 装配、连续性约束、模型策略）。
一致性绝不交给模型——那正是这个项目存在的理由。

无 API 时全部步骤仍然跑通，①②⑤ 走确定性模板并在轨迹上标注「本地降级」。
"""

from __future__ import annotations

import json
import re
from typing import Any

from contracts import CONTRACT_VERSION

# 五拍骨架。少于 5 镜时按 BEAT_SUBSETS 取子集，保证仍是完整的一条因果链。
BEATS = [
    ("establish", "建立", "交代人物身处何地、正在做什么"),
    ("escalate", "升级", "出现一个他无法忽视的变化"),
    ("turn", "转折", "他做出一个改变处境的动作"),
    ("cost", "代价", "这个动作立刻产生了后果"),
    ("settle", "收束", "留下一个可被重新理解的画面"),
]
BEAT_SUBSETS = {3: [0, 2, 4], 4: [0, 1, 2, 4], 5: [0, 1, 2, 3, 4]}

# 每镜默认时长（秒）。短片节奏：开场稍长，转折最短。
BEAT_SECONDS = {"establish": 8, "escalate": 7, "turn": 6, "cost": 7, "settle": 9}

CAMERA_BY_BEAT = {
    "establish": "WIDE-固定",
    "escalate": "MEDIUM-固定",
    "turn": "CLOSE-固定",
    "cost": "INSERT-固定",
    "settle": "WIDE-固定",
}

# 与主 canon 同源的通用约束，任何题材都成立。
UNIVERSAL_RULES = [
    {
        "id": "DRAFT-CHAR-01",
        "text": "主角的外貌、服装与体貌特征在所有镜头中完全一致",
        "check": "与第一镜的基准帧比对面部、发型、服装剪裁与颜色",
        "severity": "regenerate",
    },
    {
        "id": "DRAFT-CAM-01",
        "text": "同一场景内的左右轴线不得翻转",
        "check": "主体与关键道具在画面中的左右关系是否与前一镜一致",
        "severity": "regenerate",
    },
    {
        "id": "DRAFT-CAM-02",
        "text": "跨镜头共享几何的场景中不做推轨、环绕、升降",
        "check": "画面是否存在明显的机位位移或旋转",
        "severity": "regenerate",
        "note": "镜头一动，模型就会重新编造这个空间。",
    },
    {
        "id": "DRAFT-TEXT-01",
        "text": "画面中生成出来的任何可读文字一律后期覆盖，不因此重新生成",
        "check": "画面中是否存在可读文字",
        "severity": "local_fix",
    },
    {
        "id": "DRAFT-LOC-01",
        "text": "不得出现草案未登记的场景",
        "check": "画面所处空间是否在 locations 清单内",
        "severity": "regenerate",
    },
]

_STOP = set("的了是在和与及也就都很更最一个这那有被把从对为以及不我你他她它们".split())


def _keywords(text: str, limit: int = 8) -> list[str]:
    """中英混排的朴素关键词抽取。只用于本地降级，不假装是语义理解。"""
    tokens = re.findall(r"[A-Za-z]{3,}|[一-鿿]{2,6}", text)
    seen: list[str] = []
    for token in tokens:
        if token in _STOP or token in seen:
            continue
        seen.append(token)
    return seen[:limit]


def _slug(text: str, limit: int = 24) -> str:
    cleaned = re.sub(r"[^\w一-鿿]+", "_", text).strip("_")
    return (cleaned[:limit] or "untitled").lower()


# ──────────────────────────────────────────────────────────────────────
# ① 故事理解
# ──────────────────────────────────────────────────────────────────────

STORY_SCHEMA = {
    "title": "四到八个字的片名",
    "logline": "一句话讲清楚谁、在哪、遇到什么、必须做什么",
    "protagonist": "主角是谁，一句话",
    "world": "故事发生的世界，一句话",
    "conflict": "核心冲突，一句话",
    "tone": "视觉基调，例如「冷白工业、低饱和」",
    "locations": ["场景名，2-4 个，英文小写下划线"],
}


def understand_story(idea: str, call_model=None) -> tuple[dict[str, Any], bool]:
    """返回 (故事结构, 是否用了模型)。"""
    if call_model:
        instruction = {
            "task": "Read the one-line film idea and return its story structure.",
            "idea": idea,
            "output_schema": STORY_SCHEMA,
            "constraints": [
                "Return JSON only.",
                "Write every field in the same language as the idea.",
                "Do not invent shots — structure only.",
                "locations must be 2-4 short lowercase english identifiers.",
            ],
        }
        parsed = call_model(json.dumps(instruction, ensure_ascii=False))
        if isinstance(parsed, dict) and parsed.get("logline"):
            parsed.setdefault("title", idea[:8])
            locations = parsed.get("locations") or []
            parsed["locations"] = [_slug(x, 20) for x in locations][:4] or ["scene_a"]
            return parsed, True

    # 本地降级：不假装理解，只把输入结构化，并如实标注。
    words = _keywords(idea)
    return {
        "title": idea[:8].strip() or "未命名",
        "logline": idea.strip(),
        "protagonist": "（本地降级：未解析主角，请在草案中手动补充）",
        "world": "、".join(words[:3]) or "（未解析）",
        "conflict": "（本地降级：未解析冲突，请在草案中手动补充）",
        "tone": "低饱和、冷调、写实",
        "locations": [_slug(w, 20) for w in words[:2]] or ["scene_a", "scene_b"],
        "_degraded": True,
    }, False


# ──────────────────────────────────────────────────────────────────────
# ② 镜头规划
# ──────────────────────────────────────────────────────────────────────


def plan_shots(
    story: dict[str, Any], shot_count: int = 5, call_model=None
) -> tuple[list[dict[str, Any]], bool]:
    shot_count = max(3, min(5, int(shot_count)))
    indices = BEAT_SUBSETS[shot_count]
    skeleton = [BEATS[i] for i in indices]
    locations = story.get("locations") or ["scene_a"]

    if call_model:
        instruction = {
            "task": "Plan one shot per story beat for a short film.",
            "story": {k: v for k, v in story.items() if not k.startswith("_")},
            "beats": [{"key": k, "name": n, "job": j} for k, n, j in skeleton],
            "available_locations": locations,
            "output_schema": {
                "shots": [
                    {
                        "beat": "beat key",
                        "location": "one of available_locations",
                        "action": "what the subject physically does — one sentence",
                        "visual": "what the audience sees — one sentence",
                        "purpose": "the single job this shot does",
                    }
                ]
            },
            "constraints": [
                "Return JSON only.",
                f"Exactly {shot_count} shots, in beat order.",
                "Same language as the story fields.",
                "Physical actions only. No dialogue. No camera movement.",
                "Reuse available_locations; do not invent new ones.",
            ],
        }
        parsed = call_model(json.dumps(instruction, ensure_ascii=False))
        planned = (parsed or {}).get("shots") if isinstance(parsed, dict) else None
        if isinstance(planned, list) and len(planned) == shot_count:
            shots = []
            for index, (item, (key, name, job)) in enumerate(zip(planned, skeleton), 1):
                location = item.get("location")
                shots.append(
                    _shot(
                        index,
                        key,
                        name,
                        location if location in locations else locations[0],
                        item.get("action", job),
                        item.get("visual", ""),
                        item.get("purpose", job),
                    )
                )
            return shots, True

    # 本地降级：三幕骨架模板
    shots = []
    for index, (key, name, job) in enumerate(skeleton, 1):
        shots.append(
            _shot(
                index,
                key,
                name,
                locations[(index - 1) % len(locations)],
                f"（本地降级模板）{job}",
                f"（本地降级模板）{name}拍：{job}",
                job,
            )
        )
    return shots, False


def _shot(index, beat, beat_name, location, action, visual, purpose) -> dict[str, Any]:
    return {
        "shot_id": str(index),
        "beat": beat,
        "beat_name": beat_name,
        "location": location,
        "action": action,
        "visual": visual,
        "purpose": purpose,
        "duration_sec": BEAT_SECONDS[beat],
        "camera": CAMERA_BY_BEAT[beat],
        # 身份关键 = 主体在画面中足够大、换脸会被看出来的镜头
        "identity_critical": beat in ("turn", "cost"),
        # ── 视频生成扩展口：接入 Provider 时无需再改数据结构 ──
        "aspect_ratio": "16:9",
        "resolution": "768P",
        "reference_images": [],
        "model_strategy": None,   # 由 build 阶段按 routing 回填
        "prompt": "",             # 由 ⑤ 合成后回填
        "status": "AWAITING_EXTERNAL_GENERATION",
        "task_id": None,
        "video_url": None,
        "provider": None,
        "generation_retries": 0,
        "generation_error": None,
    }


# ──────────────────────────────────────────────────────────────────────
# ③④ canon 草案装配 ＋ 连续性约束（本地确定性，不经过模型）
# ──────────────────────────────────────────────────────────────────────


def build_canon_draft(story: dict[str, Any], shots: list[dict[str, Any]]) -> dict[str, Any]:
    numbers = [int(s["shot_id"]) for s in shots]
    locations = sorted({s["location"] for s in shots})

    facts = {
        "F01": {
            "text": story.get("logline", ""),
            "established_in": numbers[0],
            "daniel_knows_from": numbers[0],
            "audience_knows_from": numbers[0],
        },
        "F02": {
            "text": story.get("conflict", ""),
            "established_in": numbers[1] if len(numbers) > 1 else numbers[0],
            "daniel_knows_from": numbers[1] if len(numbers) > 1 else numbers[0],
            "audience_knows_from": numbers[1] if len(numbers) > 1 else numbers[0],
        },
    }

    anchor_shots = {}
    for position, shot in enumerate(shots):
        number = int(shot["shot_id"])
        previous = shots[position - 1] if position else None
        if previous and previous["location"] == shot["location"]:
            anchor_shots[shot["shot_id"]] = {
                "anchor": "chain",
                "from_shot": int(previous["shot_id"]),
                "reason": "同一场景、时间连续，续接上一镜末帧",
            }
        else:
            anchor_shots[shot["shot_id"]] = {
                "anchor": "canonical_lookup",
                "reference": f"REF_{shot['location'].upper()}",
                "reason": "切换到另一个场景，没有可续接的链条，必须回查该场景的基准参考图",
            }

    return {
        "_readme": "CREATE 模式生成的 canon 草案。结构与成片 canon 一致，可被 CanonStore 载入并跑因果审计。它是骨架，不是成品——视觉规则的密度远低于人工维护的 canon。",
        "meta": {
            "contract_version": CONTRACT_VERSION,
            "version": "draft-1",
            "script_version": f"CREATE 草案 · {story.get('title', '')}",
            "principle": "每条规则都必须能从一张截图上证伪。",
            "severity_to_decision": {
                "local_fix": "可后期覆盖或小范围重绘",
                "regenerate": "结构性错误，必须重新生成",
                "human_review": "涉及表演或剧作判断，交人工",
            },
        },
        "film": {
            "title": story.get("title", ""),
            "logline": story.get("logline", ""),
            "total_runtime_sec": sum(s["duration_sec"] for s in shots),
        },
        "characters": {
            "protagonist": {
                "rules": [UNIVERSAL_RULES[0]],
                "desc": story.get("protagonist", ""),
            }
        },
        "locations": {
            name: {
                "description": f"{story.get('tone', '')}｜{name}",
                "rules": [UNIVERSAL_RULES[4]],
            }
            for name in locations
        },
        "camera": {"rules": [UNIVERSAL_RULES[1], UNIVERSAL_RULES[2]]},
        "global_rules": [UNIVERSAL_RULES[3]],
        "attempts": {"01": {"shots": numbers, "rules": []}},
        "shots": {
            s["shot_id"]: {
                "attempt": "01",
                "duration_sec": s["duration_sec"],
                "location": s["location"],
                "camera": [s["camera"]],
                "identity_critical": s["identity_critical"],
                "generation_required": True,
                "must_show": [s["action"], s["visual"]],
                "must_not_show": ["可读文字", "未登记的场景", "机位推拉摇移"],
                "note": s["purpose"],
                "status": s["status"],
                "task_id": s["task_id"],
                "video_url": s["video_url"],
            }
            for s in shots
        },
        "identity_critical_shots": [
            int(s["shot_id"]) for s in shots if s["identity_critical"]
        ],
        "non_identity_shots": [
            int(s["shot_id"]) for s in shots if not s["identity_critical"]
        ],
        "knowledge_ledger": {
            "facts": facts,
            "attempt_state": {"01": {"shots": numbers, "daniel_enters_knowing": []}},
            "mechanisms": {},
        },
        "anchor_plan": {
            "reference_library": {
                f"REF_{name.upper()}": {
                    "desc": f"{name} 的空间基准图",
                    "status": "待建立",
                    "blocking": True,
                }
                for name in locations
            },
            "shots": anchor_shots,
        },
        "routing": {
            "rule": "identity_critical == true → primary_tier；否则 → economy_tier",
            "tiers": {
                "primary_tier": {
                    "purpose": "身份关键镜头",
                    "shots": [int(s["shot_id"]) for s in shots if s["identity_critical"]],
                },
                "economy_tier": {
                    "purpose": "非身份镜头",
                    "shots": [
                        int(s["shot_id"]) for s in shots if not s["identity_critical"]
                    ],
                },
                "post_only": {"purpose": "不进入生成流程", "shots": []},
            },
            "overrides": [],
            "_scope_honesty": "只输出路由决策，不做实时派发。",
        },
        "narrative_audit_rules": {
            "rules": [
                {"id": "NA-01", "name": "过早知情", "severity": "ERROR"},
                {"id": "NA-02", "name": "伏笔未回收", "severity": "WARN"},
                {"id": "NA-03", "name": "观众落后于主角", "severity": "WARN"},
                {"id": "NA-04", "name": "循环记忆倒退", "severity": "ERROR"},
                {"id": "NA-05", "name": "锚点断链", "severity": "ERROR"},
                {"id": "NA-06", "name": "机制未经演示即被依赖", "severity": "ERROR"},
                {"id": "NA-07", "name": "事实依赖未应用的后期补丁", "severity": "ERROR"},
                {"id": "NA-08", "name": "揭示缺少前置事实", "severity": "ERROR"},
            ]
        },
        "post_production_patches_on_locked_shots": [],
        "reusable_assets": {},
        "story_facts_not_checkable": [
            story.get("conflict", ""),
            story.get("tone", ""),
        ],
        "decision_policy": {
            "default_by_severity": True,
            "always_local_fix": ["DRAFT-TEXT-01"],
            "always_regenerate": ["DRAFT-CHAR-01", "DRAFT-CAM-01", "DRAFT-CAM-02"],
            "always_human_review": [],
        },
    }


# ──────────────────────────────────────────────────────────────────────
# ⑤ 视觉 prompt
# ──────────────────────────────────────────────────────────────────────

NEGATIVE = (
    "镜头运动, 推轨, 环绕, 升降, 变焦, 鱼眼, 广角变形, 文字, 字幕, UI界面, "
    "水印, 多余人物, 手指变形, 慢动作, 卡通, 插画"
)


def synthesize_prompts(
    story: dict[str, Any], shots: list[dict[str, Any]], call_model=None
) -> tuple[dict[str, dict[str, str]], bool]:
    tone = story.get("tone", "低饱和、冷调、写实")

    if call_model:
        instruction = {
            "task": "Write one image-to-video prompt per shot.",
            "story": {k: v for k, v in story.items() if not k.startswith("_")},
            "shots": [
                {
                    "shot_id": s["shot_id"],
                    "location": s["location"],
                    "action": s["action"],
                    "visual": s["visual"],
                    "camera": s["camera"],
                }
                for s in shots
            ],
            "output_schema": {"prompts": [{"shot_id": "1", "prompt": "..."}]},
            "constraints": [
                "Return JSON only.",
                "Same language as the story fields.",
                "Describe subject, action, light, texture, framing — in that order.",
                "Camera must be locked. Never describe dolly, orbit, crane or zoom.",
                "One single action per prompt. No dialogue. No on-screen text.",
                f"Keep every prompt consistent with this look: {tone}",
            ],
        }
        parsed = call_model(json.dumps(instruction, ensure_ascii=False))
        items = (parsed or {}).get("prompts") if isinstance(parsed, dict) else None
        if isinstance(items, list) and len(items) == len(shots):
            return (
                {
                    str(item.get("shot_id", index)): {
                        "positive": item.get("prompt", ""),
                        "negative": NEGATIVE,
                    }
                    for index, item in enumerate(items, 1)
                },
                True,
            )

    # 本地降级：模板合成
    return (
        {
            s["shot_id"]: {
                "positive": (
                    f"{s['visual']}。主体动作：{s['action']}。"
                    f"场景：{s['location']}。{tone}。"
                    f"{s['camera']}，镜头完全静止，没有推拉摇移。浅景深，写实质感。"
                ),
                "negative": NEGATIVE,
            }
            for s in shots
        },
        False,
    )
