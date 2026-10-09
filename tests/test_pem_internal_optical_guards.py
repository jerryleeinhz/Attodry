"""Real optical lifecycle on synthetic transports; no hardware imports."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_optical_points as optical_tests
from attodry_control.hardware_lease import station_hardware_lease
from attodry_control.nkt_config import NktError


class DiagnosticOpticalGuardTests(unittest.TestCase):
    def make(self):
        return optical_tests.OpticalPointSessionTests().make()

    def test_prepared_dark_keeps_pem_active_and_never_reads_dark_power(self):
        session, backend, pem, meter, _ = self.make()
        session.open()
        session.configure()
        session.set_point(0)
        original = meter.read_sample
        meter.read_sample = lambda *_: self.fail("Dark observation must not READ power")
        evidence = session.observe_dark()
        self.assertEqual(evidence["state"]["nkt"]["source"]["emission_state"], 0)
        self.assertTrue(evidence["state"]["pem"]["stable"])
        self.assertIs(pem.output_command, True)
        meter.read_sample = original
        self.assertTrue(session.cleanup()["off_confirmed"])

    def test_unexpected_emission_during_dark_rejects_without_accepting_power(self):
        session, backend, _, meter, _ = self.make()
        session.open()
        session.configure()
        session.set_point(0)
        backend.source["emission_state"] = 3
        with self.assertRaises(NktError):
            session.observe_dark()
        self.assertTrue(session.cleanup()["off_confirmed"])

    def test_monitor_requires_bracket_and_reuses_power_drift_guard_without_retune(self):
        session, backend, _, meter, _ = self.make()
        session.open()
        session.configure()
        session.set_point(0)
        with self.assertRaises(NktError):
            session.monitor_sample()
        session.qualify()
        session.begin_sample()
        before = len(backend.commands)
        meter.resource.power_provider = lambda: 1.0
        with self.assertRaises(NktError):
            session.monitor_sample()
        self.assertFalse(session.qualified)
        self.assertFalse(any(command[0] in ("level", "nd") for command in backend.commands[before:]))
        session.cleanup()

    def test_prepared_pem_instability_remains_hard_failure(self):
        session, _, pem, _, _ = self.make()
        session.open()
        session.configure()
        session.set_point(0)
        pem.transport.stable = 0
        with self.assertRaises(NktError):
            session.observe_dark()
        session.cleanup()


class StationLeaseTests(unittest.TestCase):
    def test_other_process_cannot_acquire_then_can_after_exception_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "station.local.toml"
            code = ("from attodry_control.hardware_lease import station_hardware_lease; import sys; "
                    "\ntry:\n with station_hardware_lease(sys.argv[1]): pass"
                    "\nexcept ValueError: sys.exit(7)")
            with self.assertRaisesRegex(RuntimeError, "intentional"):
                with station_hardware_lease(path):
                    child = subprocess.run([sys.executable, "-s", "-c", code, str(path)],
                                           capture_output=True, text=True, timeout=15)
                    self.assertEqual(child.returncode, 7, child.stderr)
                    raise RuntimeError("intentional")
            child = subprocess.run([sys.executable, "-s", "-c", code, str(path)],
                                   capture_output=True, text=True, timeout=15)
            self.assertEqual(child.returncode, 0, child.stderr)


if __name__ == "__main__":
    unittest.main()
