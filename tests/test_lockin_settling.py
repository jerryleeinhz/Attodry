"""Elapsed-time and acquisition regressions, using a deterministic clock."""
import unittest

from attodry_control.lockin_settling import LockinSettling
from tests import test_lockin_harmonics as sweeps


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class SettlingClockTests(unittest.TestCase):
    def test_credit_elapsed_wait_and_do_not_wait_again(self):
        clock = Clock()
        settling = LockinSettling(monotonic=clock.monotonic, sleep=clock.sleep)
        settling.changed("smu.source_set")
        clock.now += 9
        record = {}
        settling.wait({"lockin_xx": 15, "lockin_xy": 15}, record)
        settling.wait({"lockin_xx": 15, "lockin_xy": 15}, record)
        self.assertEqual(clock.sleeps, [6])
        self.assertEqual([a["sleep_requested_s"] for a in record["settling_waits"]], [6, 0])

    def test_independent_role_changes_share_the_later_deadline(self):
        clock = Clock()
        settling = LockinSettling(monotonic=clock.monotonic, sleep=clock.sleep)
        settling.changed("source")
        clock.now = 12
        settling.changed("xx.range", ("lockin_xx",))
        settling.wait({"lockin_xx": 5, "lockin_xy": 15}, {})
        self.assertEqual(clock.now, 17)
        self.assertEqual(clock.sleeps, [5])


class SweepSettlingTests(unittest.TestCase):
    setUp = sweeps.HarmonicSweepTests.setUp
    _hardware_config = sweeps.HarmonicSweepTests._hardware_config
    execute = sweeps.HarmonicSweepTests.execute

    def test_normal_harmonics_each_wait_once_after_final_settings(self):
        clock = Clock()
        code, result, _, _, _ = self.execute(clock=clock)
        self.assertEqual(code, 0, result.get("error"))
        settle = result["settle_interval_s"]
        for point in result["points"]:
            waits = point["settling_waits"]
            self.assertEqual(len(waits), 3)
            self.assertEqual([w["sleep_requested_s"] for w in waits], [settle] * 3)
            for wait in waits:
                self.assertGreaterEqual(wait["finished_monotonic_s"],
                    max(wait["deadline_monotonic_s_by_role"].values()))
            self.assertEqual(len(point["samples"]), 3)
            self.assertFalse(any(e["stage"] == "bridge" for e in point["harmonic_settings_transitions"]))

    def test_source_step_and_first_harmonic_share_one_deadline(self):
        clock = Clock()
        code, result, _, _, _ = self.execute("sweep-excitation", clock=clock)
        self.assertEqual(code, 0, result.get("error"))
        for point in result["points"]:
            self.assertEqual(len(point["settling_waits"]), 3)
            self.assertEqual(sum(w["sleep_requested_s"] for w in point["settling_waits"]),
                             3 * result["settle_interval_s"])
        self.assertTrue(any(c["reason"].endswith("set_sine_output")
            for w in result["points"][1]["settling_transition_windows"] for c in w["changes"]))

    def test_different_fixed_harmonics_on_two_roles_share_one_wait(self):
        from tests.test_lockin_role_harmonics import select_roles
        clock = Clock()
        code, result, _, _, _ = self.execute("sweep-excitation", clock=clock,
                                             config_edit=select_roles)
        self.assertEqual(code, 0, result.get("error"))
        for point in result["points"]:
            self.assertEqual([w["sleep_requested_s"] for w in point["settling_waits"]],
                             [result["settle_interval_s"], 0])
            self.assertEqual([s["harmonic"] for s in point["samples"]], [1, 2])

    def test_auto_range_changes_reopen_a_settled_window(self):
        clock = Clock()
        code, result, _, _, _ = self.execute(extra=sweeps.AUTO, clock=clock)
        self.assertEqual(code, 0, result.get("error"))
        auto = [a for p in result["points"] for a in p["harmonic_autorange"]]
        changed = [a for a in auto if a.get("settling_waits")]
        self.assertTrue(changed)
        for audit in changed:
            for wait in audit["settling_waits"]:
                self.assertEqual(wait["sleep_requested_s"], result["settle_interval_s"])
                self.assertGreaterEqual(wait["finished_monotonic_s"],
                    max(wait["deadline_monotonic_s_by_role"].values()))
