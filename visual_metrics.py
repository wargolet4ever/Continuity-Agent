from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image


def _as_image(value: Any) -> Image.Image | None:
    if value is None:
        return None
    if isinstance(value, Image.Image):
        return value.convert("RGB")
    if isinstance(value, (str, Path)):
        return Image.open(value).convert("RGB")
    return None


def image_metrics(value: Any) -> dict[str, float]:
    image = _as_image(value)
    if image is None:
        return {}
    sample = image.resize((96, 96))
    pixels = list(sample.getdata())
    total = max(1, len(pixels))
    red_pixels = sum(
        1
        for r, g, b in pixels
        if r > 120 and g < 90 and b < 90 and r > g * 1.8 and r > b * 1.8
    )
    brightness = sum((r + g + b) / 3 for r, g, b in pixels) / total / 255
    return {
        "red_ratio": round(red_pixels / total, 4),
        "brightness": round(brightness, 4),
    }


def image_similarity(first: Any, second: Any) -> float | None:
    image_a = _as_image(first)
    image_b = _as_image(second)
    if image_a is None or image_b is None:
        return None
    image_a = image_a.resize((32, 32))
    image_b = image_b.resize((32, 32))
    diff = 0
    for pixel_a, pixel_b in zip(image_a.getdata(), image_b.getdata()):
        diff += sum(abs(a - b) for a, b in zip(pixel_a, pixel_b))
    similarity = 1 - diff / (32 * 32 * 3 * 255)
    return round(max(0.0, min(1.0, similarity)), 4)
