import tempfile
import unittest
import io
import json
import os
import copy
from unittest.mock import patch
from pathlib import Path

from analyzer import ContinuityAnalyzer
from canon_loader import CanonStore
from take_log import FIELDS, TakeLog
from PIL import Image
from visual_metrics import image_metrics
from workflow import review_after, rank_takes

ROOT = Path(__file__).resolve().parent


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canon = CanonStore(ROOT / "canon.json")
        cls.analyzer = ContinuityAnalyzer(cls.canon)

    def test_shot_19_small_screen_is_local_fix(self):
        result = self.analyzer.audit(
            "19",
            "本地规则（无需 API）",
            "test prompt",
            ["出现未经设定的小型显示屏"],
        )
        self.assertEqual(result["decision"], "LOCAL FIX")
        self.assertEqual(result["issues"][0]["rule_id"], "LOC-B03")

    def test_shot_21_wrong_key_requires_regeneration(self):
        result = self.analyzer.audit(
            "21",
            "本地规则（无需 API）",
            "test prompt",
            ["钥匙插槽位置错误／靠近拉杆"],
        )
        self.assertEqual(result["decision"], "REGENERATE")
        self.assertEqual(result["issues"][0]["rule_id"], "SHOT-21A")

    def test_bay07_temporal_geometry_deformation_requires_regeneration(self):
        result = self.analyzer.audit(
            "19",
            "本地规则（无需 API）",
            "test prompt",
            ["操纵台／拉杆在镜头内发生形变"],
        )
        self.assertEqual(result["decision"], "REGENERATE")
        self.assertEqual(result["issues"][0]["rule_id"], "LOC-B06")

    def test_manual_pass(self):
        result = self.analyzer.audit(
            "22",
            "本地规则（无需 API）",
            "test prompt",
            ["画面已人工核对无异常"],
        )
        self.assertEqual(result["decision"], "PASS")

    def test_canon_lint_resolved(self):
        self.assertEqual(self.canon.lint(), [])
        self.assertEqual(self.canon.data["meta"]["version"], "1.4.2")

    def test_four_component_data_and_compiler(self):
        self.assertTrue(self.canon.ledger["facts"])
        self.assertTrue(self.canon.anchor_plan["shots"])
        self.assertTrue(self.canon.routing["tiers"])
        pack = self.canon.compile_rule_pack("19")
        self.assertEqual(pack["anchor"]["anchor"], "canonical_lookup")
        self.assertEqual(pack["anchor"]["reference"], "BAY07_A")
        self.assertEqual(pack["routing_tier"], "primary")
        self.assertIn("F15", self.canon.knows_at("19")["audience"])
        self.assertEqual(
            self.canon.anchor_plan["reference_library"]["BAY07_A"]["status"],
            "已有",
        )
        self.assertFalse(
            self.canon.anchor_plan["reference_library"]["BAY07_A"]["blocking"]
        )

    def test_binary_routing_and_overrides(self):
        self.assertEqual(self.canon.route("13")["tier"], "economy")
        self.assertEqual(self.canon.route("15")["tier"], "primary")
        self.assertTrue(self.canon.route("16")["split_generation"])
        self.assertTrue(self.canon.route("21")["split_generation"])
        # 成片中 Shot 26 是两段真实素材（Passenger Ring ＋ 空驾驶室），不再是纯后期镜头。
        self.assertEqual(self.canon.route("26")["tier"], "economy")

    def test_narrative_audit_clean_on_finished_cut(self):
        """成片状态下因果链应当完整：P1 作废、P2/P3/P4 已完成。"""
        before = copy.deepcopy(self.canon.data)
        self.assertEqual(self.canon.narrative_audit(), [])
        self.assertEqual(self.canon.data, before, "审计不得改写 canon")

    def test_narrative_audit_reproduces_production_finding(self):
        """历史快照必须重现制作期真实报出的三条 NA-07（结局依赖未应用的 P1/P2）。"""
        history = CanonStore(ROOT / "canon_history_2026-09-12.json")
        findings = history.narrative_audit()
        self.assertEqual(len(findings), 3)
        self.assertEqual({item["id"] for item in findings}, {"NA-07"})
        self.assertEqual(
            {t for item in findings for t in ("P1", "P2") if t in item["message"]},
            {"P1", "P2"},
        )
        self.assertEqual(history.narrative_audit({"P1": True, "P2": True}), [])

    def test_narrative_anchor_and_memory_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            data = copy.deepcopy(self.canon.data)
            data["anchor_plan"]["shots"]["17"] = {
                "anchor": "chain",
                "from_shot": 16,
                "reason": "invalid cross-attempt chain",
            }
            data["knowledge_ledger"]["attempt_state"]["05"][
                "daniel_enters_knowing"
            ].remove("F12")
            path = Path(directory) / "canon.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            findings = CanonStore(path).narrative_audit({"P1": True, "P2": True})
            self.assertIn("NA-04", {item["id"] for item in findings})
            self.assertIn("NA-05", {item["id"] for item in findings})

    def test_small_screen_with_similar_previous(self):
        image = Image.new("RGB", (120, 80), (200, 210, 225))
        r = self.analyzer.audit(
            "19", "本地", "", ["出现未经设定的小型显示屏"], image, image
        )
        self.assertEqual(r["decision"], "LOCAL FIX")
        self.assertNotIn("LOOP-05A", [i["rule_id"] for i in r["issues"]])

    def test_warm_colors_not_red(self):
        for color in [
            (220, 170, 150),
            (230, 150, 60),
            (180, 120, 70),
            (200, 210, 225),
            (0, 0, 0),
            (255, 255, 255),
            (40, 60, 200),
            (120, 80, 100),
        ]:
            with self.subTest(color=color):
                image = Image.new("RGB", (96, 96), color)
                self.assertEqual(image_metrics(image)["red_ratio"], 0)
                r = self.analyzer.audit("20", "本地", "", [], current_image=image)
                self.assertNotIn("LOOP-05B", [i["rule_id"] for i in r["issues"]])

    def test_red_alert(self):
        r = self.analyzer.audit(
            "20",
            "本地",
            "",
            [],
            current_image=Image.new("RGB", (96, 96), (200, 40, 40)),
        )
        self.assertEqual(r["decision"], "LOCAL FIX")
        self.assertEqual(r["issues"][0]["rule_id"], "LOOP-05B")

    def test_priority_and_review_flag(self):
        r = self.analyzer.audit(
            "19", "本地", "", ["出现未经设定的小型显示屏", "047 表演意图不确定"]
        )
        self.assertEqual(r["decision"], "LOCAL FIX")
        self.assertTrue(r["needs_human_review"])
        self.assertEqual(r["human_review_count"], 1)

    def test_scope(self):
        ids = lambda shot: {
            r["id"] for r in self.canon.compile_rule_pack(shot)["rules"]
        }
        self.assertFalse(any(x.startswith("LOC-B") for x in ids("14")))
        self.assertTrue({"LOC-X01", "LOC-X02", "LOC-X03"} <= ids("14"))
        self.assertNotIn("CAM-02", ids("13"))
        self.assertIn("CAM-05", ids("13"))
        self.assertIn("CAM-02", ids("19"))
        self.assertNotIn("CHAR-0403", ids("16"))
        self.assertIn("CHAR-0405", ids("16"))
        self.assertNotIn("CHAR-0405", ids("19"))
        self.assertTrue(self.canon.get_shot("25")["identity_critical"])

    def test_local_scope_and_shot16_performance(self):
        r = self.analyzer.audit("14", "本地", "", ["钥匙插槽位置错误／靠近拉杆"])
        self.assertEqual(r["issues"], [])
        r = self.analyzer.audit("16", "本地", "", ["047 表演意图不确定"])
        self.assertEqual(r["issues"][0]["rule_id"], "CHAR-0405")

    def test_skip_does_not_call_any_checker(self):
        # Shot 26 became real footage in the finished cut (canon >= 1.4), so the
        # post-only path is exercised by flipping the flag on an in-memory copy.
        shot = self.canon.data["shots"]["26"]
        original = shot.get("generation_required", True)
        shot["generation_required"] = False
        try:
            with (
                patch.object(
                    self.analyzer, "_remote_review", side_effect=AssertionError
                ),
                patch.object(
                    self.analyzer, "_local_issues", side_effect=AssertionError
                ),
            ):
                r = self.analyzer.audit("26", "多模态", "", [])
        finally:
            shot["generation_required"] = original
        self.assertEqual(r["decision"], "SKIP")
        self.assertIsNone(r["score"])
        self.assertEqual(r["rule_count"], 0)

    def api_result(self, payload, image=True):
        body = {"choices": [{"message": {"content": json.dumps(payload)}}]}
        response = io.BytesIO(json.dumps(body).encode())
        with (
            patch.dict(
                os.environ,
                {"LLM_API_KEY": "test-not-a-secret", "LLM_MODEL": "mock-vision"},
            ),
            patch("urllib.request.urlopen", return_value=response),
        ):
            return self.analyzer.audit(
                "19",
                "多模态",
                "",
                [],
                current_image=Image.new("RGB", (16, 16), "white") if image else None,
            )

    def test_api_success_empty_issues_pass(self):
        r = self.api_result({"issues": []})
        self.assertEqual(r["decision"], "PASS")
        self.assertTrue(r["api_reviewed"])

    def test_invalid_api_responses_never_pass(self):
        for payload in [
            {},
            [],
            {"issues": None},
            {"issues": [{}]},
            {"issues": [{"rule_id": "FAKE", "evidence": "test", "confidence": 1}]},
            {"issues": [{"rule_id": "LOC-B03", "evidence": "test", "confidence": 2}]},
        ]:
            with self.subTest(payload=payload):
                r = self.api_result(payload)
                self.assertEqual(r["decision"], "HUMAN REVIEW")
                self.assertFalse(r["api_reviewed"])

    def test_no_current_image_never_api_pass(self):
        self.assertEqual(
            self.api_result({"issues": []}, image=False)["decision"], "HUMAN REVIEW"
        )

    def test_low_confidence_review(self):
        r = self.api_result(
            {
                "issues": [
                    {
                        "rule_id": "LOC-B03",
                        "evidence": "unclear screen",
                        "confidence": 0.6,
                    }
                ]
            }
        )
        self.assertEqual(r["decision"], "HUMAN REVIEW")

    def test_prompt_preserves_traversal(self):
        r = self.analyzer.audit("13", "本地", "", [])
        self.assertNotIn("no dolly", r["revised_prompt"])
        self.assertIn("单一方向", r["revised_prompt"])

    def test_note_exposed(self):
        r = self.analyzer.audit("21", "本地", "", ["钥匙插槽位置错误／靠近拉杆"])
        self.assertTrue(r["issues"][0]["note"])
        self.assertIn("规则溯源", self.canon.rule_pack_markdown("21"))

    def test_after_does_not_inherit_observations(self):
        before = self.analyzer.audit("19", "本地", "", ["出现未经设定的小型显示屏"])
        after, rows, status = review_after(
            self.analyzer, before, Image.new("RGB", (16, 16), "white"), "new prompt", []
        )
        self.assertEqual(after["decision"], "HUMAN REVIEW")
        self.assertEqual(after["issues"], [])
        self.assertEqual(len(rows), 2)
        self.assertIn("尚无足够证据", status)

    def test_batch_api_rank_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "take.png"
            Image.new("RGB", (16, 16), "white").save(path)
            with patch.object(self.analyzer, "_remote_review", return_value=[]):
                summary, rows, _ = rank_takes(
                    self.analyzer,
                    "19",
                    "多模态",
                    "",
                    [str(path), str(Path(directory) / "bad.png")],
                )
            self.assertIn("推荐", rows[0][5])
            self.assertEqual(rows[1][2], "ERROR")
            self.assertIn("take.png", summary)

    def test_log_statistics_and_migration(self):
        import csv

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.csv"
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["adoption", "rejection_reason"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"adoption": "采纳"},
                        {"adoption": "不采纳", "rejection_reason": "空间关系错"},
                        {"adoption": "待定"},
                    ]
                )
            log = TakeLog(path)
            text, rows = log.stats()
            self.assertIn("50.0%", text)
            self.assertEqual(rows, [["空间关系错", 1]])
            self.assertTrue(path.with_suffix(".csv.pre-v1.1").exists())

    def test_take_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = TakeLog(Path(directory) / "takes.csv")
            result = self.analyzer.audit(
                "19", "本地规则（无需 API）", "prompt", ["出现未经设定的小型显示屏"]
            )
            log.append(
                result,
                "即梦",
                "prompt",
                "ref",
                "take.mp4",
                "不采纳",
                "文字/UI问题",
                "test",
            )
            self.assertEqual(len(log.rows()), 1)
            self.assertEqual(log.rows()[0][FIELDS.index("routing_tier")], "primary")


if __name__ == "__main__":
    unittest.main()
