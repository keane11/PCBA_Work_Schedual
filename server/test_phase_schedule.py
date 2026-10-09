"""Phase boundaries and draft day arithmetic."""

import unittest

from server.phase_schedule import PHASE_TEMPLATES, draft_dates, phase_template, plan_nodes


class PhaseScheduleTests(unittest.TestCase):
    def test_phase_templates_keep_sampling_separate_from_factory_runs(self):
        rd = [name for name, _ in PHASE_TEMPLATES["rd"]]
        pp = [name for name, _ in PHASE_TEMPLATES["pp"]]
        mp = [name for name, _ in PHASE_TEMPLATES["mp"]]
        self.assertIn("嘉立创打样下单", rd)
        self.assertNotIn("板厂签收", rd)
        self.assertIn("板厂签收", pp)
        self.assertIn("板厂签收", mp)
        steps = phase_template("rd")
        steps[0]["name"] = "changed"
        self.assertEqual(phase_template("rd")[0]["name"], "需求与打样目标")

    def test_half_day_plan_skips_weekend_or_uses_natural_days(self):
        self.assertEqual(draft_dates("2026-09-25", ["0.5", "0.5", "1"], "workday"), [
            ("2026-09-25", "2026-09-25"),
            ("2026-09-25", "2026-09-28"),
            ("2026-09-28", "2026-09-29"),
        ])
        self.assertEqual(draft_dates("2026-09-25", ["0.5", "0.5"], "natural")[-1],
                         ("2026-09-25", "2026-09-26"))

    def test_company_holiday_callback_and_invalid_duration(self):
        work = lambda day: day.weekday() < 5 and day.isoformat() != "2026-09-28"
        self.assertEqual(draft_dates("2026-09-25", ["1"], is_workday=work),
                         [("2026-09-25", "2026-09-29")])
        with self.assertRaises(ValueError):
            draft_dates("2026-09-25", ["0.25"])

    def test_weekend_start_and_weekend_arrival_shift_draft_to_monday(self):
        self.assertEqual(draft_dates("2026-09-26", ["0.5"], "workday"),
                         [("2026-09-28", "2026-09-28")])
        steps = plan_nodes("2026-09-25", [
            {"stage_type": "milestone", "duration_days": "0", "actual_material_at": "2026-09-27"},
            {"duration_days": "0.5"},
        ])
        self.assertEqual(steps[1]["planned_start"], "2026-09-28")


if __name__ == "__main__":
    unittest.main()
