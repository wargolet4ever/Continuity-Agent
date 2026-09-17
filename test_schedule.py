import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from continuity_cli import main
from schedule import (
    EXIT_SCHEDULE_INCOMPLETE,
    EXIT_SCHEDULE_INVALID,
    HARD_GATE_DAYS,
    ScheduleError,
    evaluate_schedule,
    load_schedule,
    validate_schedule,
)

ROOT = Path(__file__).resolve().parent
SCHEDULE_PATH = ROOT / "roadmap" / "v1-14-day.json"


class ScheduleContractTests(unittest.TestCase):
    def test_repository_schedule_has_fourteen_ordered_days_and_exact_gates(self):
        schedule = load_schedule(SCHEDULE_PATH)
        schema = json.loads(
            (ROOT / "schemas" / "v1-schedule-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual([day["day"] for day in schedule["days"]], list(range(1, 15)))
        self.assertEqual(
            tuple(day["day"] for day in schedule["days"] if day["gate"]),
            HARD_GATE_DAYS,
        )
        self.assertEqual(schema["properties"]["schedule_version"]["const"], "1.0")
        self.assertEqual(schema["properties"]["duration_days"]["const"], 14)

    def test_current_repository_is_complete_through_day_nine(self):
        evaluation = evaluate_schedule(load_schedule(SCHEDULE_PATH), ROOT)

        self.assertEqual(evaluation["complete_days"], 9)
        self.assertEqual(evaluation["next_day"], 10)
        self.assertEqual(evaluation["days"][4]["status"], "DONE_WITH_FALLBACK")
        self.assertEqual(evaluation["days"][6]["status"], "DONE")
        self.assertEqual(evaluation["days"][8]["status"], "DONE")
        self.assertEqual(evaluation["days"][10]["status"], "BLOCKED")

    def test_external_user_gate_has_no_fallback(self):
        schedule = load_schedule(SCHEDULE_PATH)
        gate = schedule["days"][10]["gate"]

        self.assertEqual([item["outcome"] for item in gate["evidence_sets"]], ["pass"])
        self.assertIn("real external user", schedule["success_metric"].lower())

    def test_validator_rejects_path_escape(self):
        schedule = copy.deepcopy(load_schedule(SCHEDULE_PATH))
        schedule["days"][0]["deliverables"][0]["path"] = "../secret"

        with self.assertRaisesRegex(ScheduleError, "inside the repository"):
            validate_schedule(schedule)

    def test_json_command_is_machine_readable_and_does_not_leak_absolute_root(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["schedule", "--json"])

        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["next_day"], 10)
        self.assertNotIn(str(ROOT), stdout.getvalue())

    def test_strict_command_returns_incomplete_exit_code(self):
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = main(["schedule", "--strict"])

        self.assertEqual(exit_code, EXIT_SCHEDULE_INCOMPLETE)

    def test_invalid_command_contract_returns_invalid_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text("{}", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                exit_code = main(["schedule", "--file", str(path)])

        self.assertEqual(exit_code, EXIT_SCHEDULE_INVALID)


if __name__ == "__main__":
    unittest.main()
