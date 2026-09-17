"""Config-driven, CPU-only visual continuity checks.

The checks in this module deliberately operate on explicit pixels, crops and
thresholds declared in canon.  They do not attempt object detection or infer
story semantics.  A semantic rule without a matching ``visual_checks`` entry
therefore remains a human/multimodal-review rule instead of receiving a fake
automatic verdict.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps

from visual_metrics import image_metrics

from .base import Issue, IssueFactory, RuleContext

FEATURE_SIZE = 32
HISTOGRAM_BINS = 16


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
    left, top, right, bottom = (float(value) for value in box)
    width, height = image.size
    left_px = min(width - 1, max(0, math.floor(left * width)))
    top_px = min(height - 1, max(0, math.floor(top * height)))
    right_px = max(left_px + 1, min(width, math.ceil(right * width)))
    bottom_px = max(top_px + 1, min(height, math.ceil(bottom * height)))
    return image.crop((left_px, top_px, right_px, bottom_px))


def _normalized_vector(image: Image.Image) -> list[float]:
    prepared = ImageOps.autocontrast(
        ImageOps.fit(
            image.convert("L"),
            (FEATURE_SIZE, FEATURE_SIZE),
            method=Image.Resampling.LANCZOS,
        )
    )
    if hasattr(prepared, "get_flattened_data"):
        raw = prepared.get_flattened_data()
    else:  # Pillow 10 compatibility.
        raw = prepared.getdata()
    values = [float(value) for value in raw]
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
    raw = sum(a * b for a, b in zip(first, second)) / (first_norm * second_norm)
    return max(0.0, min(1.0, (raw + 1.0) / 2.0))


def _color_histogram(image: Image.Image) -> list[float]:
    sample = ImageOps.fit(
        image.convert("RGB"),
        (64, 64),
        method=Image.Resampling.BILINEAR,
    )
    result: list[float] = []
    for channel in sample.split():
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
        result.extend(value / total / 3 for value in grouped)
    return result


def _histogram_intersection(first: list[float], second: list[float]) -> float:
    return max(0.0, min(1.0, sum(min(a, b) for a, b in zip(first, second))))


def reference_similarity(
    reference: Any,
    candidate: Any,
    reference_crop: Any = None,
    candidate_crop: Any = None,
) -> float:
    """Return 0..1 visual similarity for two explicitly selected regions."""

    reference_image = _image(reference)
    candidate_image = _image(candidate)
    if reference_image is None or candidate_image is None:
        raise ValueError("visual reference images must be readable")
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
    return round(0.55 * structure + 0.30 * color + 0.15 * edges, 4)


def temporal_structure_similarity(first: Any, second: Any, crop: Any = None) -> float:
    """Compare geometry while reducing sensitivity to global brightness changes."""

    first_image = _image(first)
    second_image = _image(second)
    if first_image is None or second_image is None:
        raise ValueError("temporal images must be readable")
    first_image = _crop(first_image, crop)
    second_image = _crop(second_image, crop)
    structure = _cosine(
        _normalized_vector(first_image), _normalized_vector(second_image)
    )
    edges = _cosine(
        _normalized_vector(first_image.filter(ImageFilter.FIND_EDGES)),
        _normalized_vector(second_image.filter(ImageFilter.FIND_EDGES)),
    )
    return round(0.65 * structure + 0.35 * edges, 4)


def _confidence(check: dict[str, Any], default: float) -> float:
    return round(float(check.get("confidence", default)), 2)


def _human_review(issue: Issue) -> Issue:
    issue["severity"] = "human_review"
    issue["requires_confirmation"] = True
    return issue


@dataclass(frozen=True)
class VisualContinuityPlugin:
    """Evaluate only canon-declared visual checks using Pillow on CPU."""

    plugin_id: str = "builtin.visual-continuity"
    priority: int = 400

    @staticmethod
    def _reference_path(context: RuleContext, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else context.canon.path.parent / path

    @staticmethod
    def _images(context: RuleContext) -> list[Image.Image]:
        values = list(context.video_frames) or [context.current_image]
        return [image for value in values if (image := _image(value)) is not None]

    def _pixel_range(
        self,
        context: RuleContext,
        check: dict[str, Any],
        issue_factory: IssueFactory,
    ) -> Issue | None:
        images = self._images(context)
        if not images:
            return None
        metric = str(check["metric"])
        values = [
            image_metrics(_crop(image, check.get("crop"))).get(metric)
            for image in images
        ]
        numbers = [float(value) for value in values if value is not None]
        if not numbers:
            return None
        statistic = str(check.get("statistic", "max"))
        aggregate = {
            "min": min,
            "max": max,
            "median": statistics.median,
        }[statistic](numbers)
        minimum = check.get("minimum")
        maximum = check.get("maximum")
        violated = (minimum is not None and aggregate < float(minimum)) or (
            maximum is not None and aggregate > float(maximum)
        )
        if not violated:
            return None
        bounds = []
        if minimum is not None:
            bounds.append(f">= {float(minimum):.3f}")
        if maximum is not None:
            bounds.append(f"<= {float(maximum):.3f}")
        issue = issue_factory(
            str(check["rule_id"]),
            str(check.get("category", "Color / Light Continuity")),
            (
                f"CPU 像素检查：{metric} 的 {statistic} 值为 {aggregate:.3f}，"
                f"要求 {' 且 '.join(bounds)}。该数值只证明像素越界，不证明其语义来源。"
            ),
            _confidence(check, 0.8),
            str(check.get("minimal_fix", "")),
        )
        if check.get("requires_confirmation", False):
            issue["requires_confirmation"] = True
        return issue

    def _reference(
        self,
        context: RuleContext,
        check: dict[str, Any],
        issue_factory: IssueFactory,
    ) -> Issue | None:
        images = self._images(context)
        if not images:
            return None
        reference_path = self._reference_path(context, str(check["asset_path"]))
        reference = _image(reference_path)
        if reference is None:
            return _human_review(
                issue_factory(
                    str(check["rule_id"]),
                    str(check.get("category", "Reference Continuity")),
                    f"已配置参考比对，但基准图不可读取：{reference_path.name}。",
                    0.5,
                    "补齐基准图后重新检查；本次不自动判失败。",
                )
            )
        scores = [
            reference_similarity(
                reference,
                image,
                check.get("reference_crop"),
                check.get("candidate_crop"),
            )
            for image in images
        ]
        score = min(scores)
        reject_below = float(check["reject_below"])
        review_below = float(check["review_below"])
        if score >= review_below:
            return None
        issue = issue_factory(
            str(check["rule_id"]),
            str(check.get("category", "Reference Continuity")),
            (
                f"基准区域与待检区域的最低视觉相似度为 {score:.3f}；"
                f"重做阈值 {reject_below:.3f}，人工复核阈值 {review_below:.3f}。"
            ),
            _confidence(check, 0.9 if score < reject_below else 0.55),
            str(check.get("minimal_fix", "")),
        )
        return issue if score < reject_below else _human_review(issue)

    def _temporal(
        self,
        context: RuleContext,
        check: dict[str, Any],
        issue_factory: IssueFactory,
    ) -> Issue | None:
        frames = [
            image
            for value in context.video_frames
            if (image := _image(value)) is not None
        ]
        if len(frames) < 2:
            return None
        if check.get("comparison", "adjacent") == "first":
            pairs = [(frames[0], frame) for frame in frames[1:]]
        else:
            pairs = list(pairwise(frames))
        scores = [
            temporal_structure_similarity(first, second, check.get("crop"))
            for first, second in pairs
        ]
        score = min(scores)
        reject_below = float(check["reject_below"])
        review_below = float(check["review_below"])
        if score >= review_below:
            return None
        issue = issue_factory(
            str(check["rule_id"]),
            str(check.get("category", "Temporal Geometry")),
            (
                f"显式裁剪区域的最低时序结构相似度为 {score:.3f}；"
                f"重做阈值 {reject_below:.3f}，人工复核阈值 {review_below:.3f}。"
            ),
            _confidence(check, 0.9 if score < reject_below else 0.55),
            str(check.get("minimal_fix", "")),
        )
        return issue if score < reject_below else _human_review(issue)

    def evaluate(
        self, context: RuleContext, issue_factory: IssueFactory
    ) -> list[Issue]:
        issues: list[Issue] = []
        handlers = {
            "pixel_range": self._pixel_range,
            "reference_similarity": self._reference,
            "temporal_stability": self._temporal,
        }
        for check in context.pack.get("visual_checks", []):
            issue = handlers[str(check["kind"])](context, check, issue_factory)
            if issue is not None:
                issues.append(issue)
        return issues
