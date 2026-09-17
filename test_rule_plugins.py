"""Tests for the deterministic rule-plugin interface and built-ins."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from analyzer import ContinuityAnalyzer
from canon_loader import CanonStore
from rules import RuleContext, RuleRegistry, default_rule_registry

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class FakePlugin:
    plugin_id: str
    priority: int
    rule_id: str
    confidence: float

    def evaluate(self, context, issue_factory):
        return [
            issue_factory(
                self.rule_id,
                "Test",
                f"finding from {self.plugin_id}",
                self.confidence,
                "test fix",
            )
        ]


class RuleRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canon = CanonStore(ROOT / "canon.json")

    def context(self, shot_id="19"):
        return RuleContext(
            shot_id=shot_id,
            canon=self.canon,
            pack=self.canon.compile_rule_pack(shot_id),
            observations=frozenset(),
        )

    def issue_factory(self, rule_id, category, evidence, confidence, minimal_fix):
        return ContinuityAnalyzer(self.canon)._issue_from_rule(
            rule_id, category, evidence, confidence, minimal_fix
        )

    def test_default_registry_has_stable_order(self):
        registry = default_rule_registry()
        self.assertEqual(
            [plugin.plugin_id for plugin in registry.plugins],
            [
                "builtin.observations",
                "builtin.attempt-red-pixel",
                "builtin.identity-consistency",
            ],
        )

    def test_duplicate_plugin_id_is_rejected(self):
        first = FakePlugin("same", 10, "LOC-B03", 0.5)
        with self.assertRaisesRegex(ValueError, "Duplicate rule plugin id"):
            RuleRegistry((first, first))

    def test_registry_keeps_highest_confidence_for_one_rule(self):
        registry = RuleRegistry(
            (
                FakePlugin("low", 10, "LOC-B03", 0.4),
                FakePlugin("high", 20, "LOC-B03", 0.9),
            )
        )
        issues = registry.evaluate(self.context(), self.issue_factory)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["confidence"], 0.9)
        self.assertIn("high", issues[0]["evidence"])

    def test_registry_drops_rule_outside_compiled_shot_scope(self):
        registry = RuleRegistry((FakePlugin("scope", 10, "LOC-B03", 1.0),))
        self.assertEqual(registry.evaluate(self.context("14"), self.issue_factory), [])


class BuiltinPluginRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canon = CanonStore(ROOT / "canon.json")
        cls.analyzer = ContinuityAnalyzer(cls.canon)

    def test_observation_plugin_preserves_shot_override(self):
        result = self.analyzer.audit(
            "14", "本地规则（无需 API）", "", ["出现未经设定的小型显示屏"]
        )
        self.assertEqual(result["issues"][0]["rule_id"], "LOC-X03")

    def test_pixel_plugin_preserves_attempt05_red_candidate(self):
        result = self.analyzer.audit(
            "20",
            "本地规则（无需 API）",
            "",
            [],
            current_image=Image.new("RGB", (96, 96), (200, 40, 40)),
        )
        issue = result["issues"][0]
        self.assertEqual(issue["rule_id"], "LOOP-05B")
        self.assertEqual(issue["confidence"], 0.76)
        self.assertTrue(issue["requires_confirmation"])

    def test_custom_registry_can_be_injected_without_editing_analyzer(self):
        analyzer = ContinuityAnalyzer(
            self.canon,
            RuleRegistry((FakePlugin("custom", 10, "LOC-B03", 0.88),)),
        )
        result = analyzer.audit("19", "本地规则（无需 API）", "", [])
        self.assertEqual(result["issues"][0]["rule_id"], "LOC-B03")
        self.assertEqual(result["issues"][0]["confidence"], 0.88)


if __name__ == "__main__":
    unittest.main(verbosity=2)
