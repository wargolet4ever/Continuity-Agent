"""CPU-only conservative identity consistency rule.

This baseline intentionally uses no face-recognition model or downloaded
weights.  It compares explicitly configured character crops using appearance
and structure features.  It catches gross drift; ambiguous scores are routed
to human review rather than presented as biometric identity proof.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps

from .base import Issue, IssueFactory, RuleContext

FEATURE_SIZE = 32
HISTOGRAM_BINS = 16


@dataclass(frozen=True)
class IdentitySimilarity:
    score: float
    structure: float
    color: float
    edges: float


def _image(value: Any) -> Image.Image | None:
    if isinstance(value, Image.Image):
        return value.convert("RGB")
    if isinstance(value, (str, Path)):
        try:
            with Image.open(value) as opened:
                return opened.convert("RGB")
        except (OSError, ValueError):
            return None
    return None


def _crop(image: Image.Image, box: Any) -> Image.Image:
    if box is None:
        return image
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("identity crop must contain four normalized numbers")
    try:
        left, top, right, bottom = (float(value) for value in box)
    except (TypeError, ValueError) as exc:
        raise ValueError("identity crop must contain four normalized numbers") from exc
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError(
            "normalized identity crop must satisfy 0 <= left/top < right/bottom <= 1"
        )
    width, height = image.size
    left_px = min(width - 1, max(0, math.floor(left * width)))
    top_px = min(height - 1, max(0, math.floor(top * height)))
    right_px = max(left_px + 1, min(width, math.ceil(right * width)))
    bottom_px = max(top_px + 1, min(height, math.ceil(bottom * height)))
    pixels = (
        left_px,
        top_px,
        right_px,
        bottom_px,
    )
    return image.crop(pixels)


def _normalized_vector(image: Image.Image) -> list[float]:
    prepared = ImageOps.autocontrast(
        ImageOps.fit(
            image.convert("L"),
            (FEATURE_SIZE, FEATURE_SIZE),
            method=Image.Resampling.LANCZOS,
        )
    )
    if hasattr(prepared, "get_flattened_data"):
        pixel_values = prepared.get_flattened_data()
    else:  # Pillow 10 compatibility.
        pixel_values = prepared.getdata()
    values = [float(value) for value in pixel_values]
    mean = sum(values) / len(values)
    centered = [value - mean for value in values]
    norm = math.sqrt(sum(value * value for value in centered))
    if norm <= 1e-9:
        return [0.0] * len(centered)
    return [value / norm for value in centered]


def _cosine(first: list[float], second: list[float]) -> float:
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if first_norm <= 1e-9 and second_norm <= 1e-9:
        return 1.0
    if first_norm <= 1e-9 or second_norm <= 1e-9:
        return 0.0
    value = sum(a * b for a, b in zip(first, second)) / (first_norm * second_norm)
    return max(0.0, min(1.0, (value + 1.0) / 2.0))


def _color_histogram(image: Image.Image) -> list[float]:
    sample = ImageOps.fit(
        image.convert("RGB"),
        (64, 64),
        method=Image.Resampling.BILINEAR,
    )
    channels = sample.split()
    result: list[float] = []
    for channel in channels:
        raw = channel.histogram()
        grouped = [
            sum(
                raw[
                    index * (256 // HISTOGRAM_BINS) : (index + 1)
                    * (256 // HISTOGRAM_BINS)
                ]
            )
            for index in range(HISTOGRAM_BINS)
        ]
        total = max(1, sum(grouped))
        result.extend(value / total / len(channels) for value in grouped)
    return result


def _histogram_intersection(first: list[float], second: list[float]) -> float:
    return max(0.0, min(1.0, sum(min(a, b) for a, b in zip(first, second))))


def identity_similarity(
    reference: Any,
    candidate: Any,
    reference_crop: Any = None,
    candidate_crop: Any = None,
) -> IdentitySimilarity:
    """Return a deterministic 0..1 appearance similarity for explicit crops."""

    reference_image = _image(reference)
    candidate_image = _image(candidate)
    if reference_image is None or candidate_image is None:
        raise ValueError("identity images must be readable")
    reference_image = _crop(reference_image, reference_crop)
    candidate_image = _crop(candidate_image, candidate_crop)

    structure = _cosine(
        _normalized_vector(reference_image), _normalized_vector(candidate_image)
    )
    color = _histogram_intersection(
        _color_histogram(reference_image), _color_histogram(candidate_image)
    )
    edges = _cosine(
        _normalized_vector(reference_image.filter(ImageFilter.FIND_EDGES)),
        _normalized_vector(candidate_image.filter(ImageFilter.FIND_EDGES)),
    )
    score = 0.55 * structure + 0.30 * color + 0.15 * edges
    return IdentitySimilarity(
        score=round(score, 4),
        structure=round(structure, 4),
        color=round(color, 4),
        edges=round(edges, 4),
    )


def _threshold(value: Any, default: float, name: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return number


@dataclass(frozen=True)
class IdentityConsistencyPlugin:
    plugin_id: str = "builtin.identity-consistency"
    priority: int = 300
    default_reject_below: float = 0.45
    default_review_below: float = 0.62

    def _reference_path(self, context: RuleContext, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return context.canon.path.parent / path

    def _missing_reference_issue(
        self,
        character_id: str,
        rule_id: str,
        path: Path,
        issue_factory: IssueFactory,
    ) -> Issue:
        issue = issue_factory(
            rule_id,
            "Character Identity",
            f"角色 {character_id} 已配置身份检查，但基准图不可读取：{path.name}。",
            0.5,
            "补齐可读取的角色基准图后重新检查；本次交人工，不自动判身份失败。",
        )
        issue["severity"] = "human_review"
        issue["requires_confirmation"] = True
        return issue

    def evaluate(
        self, context: RuleContext, issue_factory: IssueFactory
    ) -> list[Issue]:
        shot = context.canon.get_shot(context.shot_id)
        crop_map = shot.get("identity_crops", {})
        if not isinstance(crop_map, dict) or not crop_map:
            return []

        candidates = list(context.video_frames) or [context.current_image]
        candidates = [
            candidate for candidate in candidates if _image(candidate) is not None
        ]
        if not candidates:
            return []

        issues: list[Issue] = []
        characters = context.canon.data.get("characters", {})
        for character_id, candidate_crop in sorted(crop_map.items()):
            character = characters.get(character_id, {})
            config = character.get("identity_reference", {})
            if not isinstance(config, dict) or not config:
                continue
            rule_id = config.get("rule_id")
            asset_path = config.get("asset_path")
            if not isinstance(rule_id, str) or not rule_id:
                raise ValueError(
                    f"characters.{character_id}.identity_reference.rule_id is required"
                )
            if not isinstance(asset_path, str) or not asset_path:
                raise ValueError(
                    f"characters.{character_id}.identity_reference.asset_path is required"
                )

            reference_path = self._reference_path(context, asset_path)
            reference = _image(reference_path)
            if reference is None:
                issues.append(
                    self._missing_reference_issue(
                        character_id, rule_id, reference_path, issue_factory
                    )
                )
                continue

            reject_below = _threshold(
                config.get("reject_below"),
                self.default_reject_below,
                "reject_below",
            )
            review_below = _threshold(
                config.get("review_below"),
                self.default_review_below,
                "review_below",
            )
            if reject_below >= review_below:
                raise ValueError("reject_below must be lower than review_below")

            similarities = [
                identity_similarity(
                    reference,
                    candidate,
                    config.get("crop"),
                    candidate_crop,
                )
                for candidate in candidates
            ]
            median_score = statistics.median(item.score for item in similarities)
            if median_score >= review_below:
                continue
            median_structure = statistics.median(
                item.structure for item in similarities
            )
            median_color = statistics.median(item.color for item in similarities)
            median_edges = statistics.median(item.edges for item in similarities)
            issue = issue_factory(
                rule_id,
                "Character Identity",
                (
                    f"角色 {character_id} 与基准裁剪的身份外观相似度 {median_score:.3f}；"
                    f"结构 {median_structure:.3f}、颜色 {median_color:.3f}、边缘 {median_edges:.3f}。"
                ),
                0.9 if median_score < reject_below else 0.55,
                "回查角色基准图与裁剪框；确认身份漂移后使用同一角色参考重新生成。",
            )
            if median_score >= reject_below:
                issue["severity"] = "human_review"
                issue["requires_confirmation"] = True
            issues.append(issue)
        return issues
