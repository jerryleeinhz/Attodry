import unittest

from attodry_control.gates import (
    GateLeakageTrip,
    GatePreflightRejected,
    GatePreflightState,
    GateReadbackMismatch,
    GateSafetyError,
    GateSafetyLimits,
    GateWriteNotAuthorized,
    SafeGateController,
    ramp_values,
)


class FakeGateBackend:
    def __init__(self) -> None:
        self.voltage = 0.0
        self.current = 0.0
        self.output = False
        self.compliance = None
        self.closed = False
        self.voltage_commands: list[float] = []
        self.fail_measure_voltage = False
        self.readback_offset = 0.0

    def set_current_compliance(self, current_a: float) -> None:
        self.compliance = current_a

    def preflight(self) -> GatePreflightState:
        return GatePreflightState(
            identity="FAKE,GATE,1",
            output_enabled=self.output,
            source_setpoint_v=self.voltage,
            voltage_read_v=self.voltage + self.readback_offset,
            current_read_a=self.current,
            status="0,No error",
        )

    def set_output(self, enabled: bool) -> None:
        self.output = enabled

    def set_voltage(self, voltage_v: float) -> None:
        self.voltage = voltage_v
        self.voltage_commands.append(voltage_v)

    def measure_voltage(self) -> float:
        if self.fail_measure_voltage:
            raise OSError("injected read failure")
        return self.voltage + self.readback_offset

    def measure_current(self) -> float:
        return self.current

    def close(self) -> None:
        self.closed = True


def controller(backend: FakeGateBackend, *, authorized: bool = True):
    return SafeGateController(
        role="top",
        backend=backend,
        limits=GateSafetyLimits(
            max_abs_voltage_v=2.0,
            compliance_a=10e-9,
            leakage_limit_a=5e-9,
            ramp_step_v=0.25,
            readback_tolerance_v=0.01,
        ),
        writes_authorized=authorized,
        sleep=lambda _: None,
    )


class GateTests(unittest.TestCase):
    def test_ramp_never_exceeds_step_and_includes_target(self) -> None:
        values = ramp_values(-0.1, 0.8, 0.25)
        points = (-0.1,) + values
        self.assertEqual(values[-1], 0.8)
        self.assertTrue(all(abs(b - a) <= 0.25 for a, b in zip(points, values)))

    def test_writes_require_explicit_authorization(self) -> None:
        backend = FakeGateBackend()
        with self.assertRaises(GateWriteNotAuthorized):
            controller(backend, authorized=False).enable_output()
        self.assertFalse(backend.output)

    def test_enable_sets_compliance_and_ramp_is_verified(self) -> None:
        backend = FakeGateBackend()
        gate = controller(backend)
        gate.enable_output()
        gate.set_voltage(0.6)
        self.assertEqual(backend.compliance, 10e-9)
        self.assertTrue(backend.output)
        self.assertEqual(backend.voltage, 0.6)
        self.assertTrue(all(abs(value) <= 0.6 for value in backend.voltage_commands))
        self.assertEqual(gate.last_confirmed_state.voltage_read_v, 0.6)

    def test_enabled_or_nonzero_preflight_is_rejected_without_writing(self) -> None:
        backend = FakeGateBackend()
        backend.output = True
        with self.assertRaises(GatePreflightRejected):
            controller(backend).enable_output()
        self.assertEqual(backend.voltage_commands, [])

    def test_target_above_explicit_limit_is_rejected_before_write(self) -> None:
        backend = FakeGateBackend()
        gate = controller(backend)
        with self.assertRaisesRegex(GateSafetyError, "absolute limit"):
            gate.set_voltage(2.1)
        self.assertEqual(backend.voltage_commands, [])

    def test_leakage_trip_ramps_zero_and_disables_output(self) -> None:
        backend = FakeGateBackend()
        gate = controller(backend)
        gate.enable_output()
        backend.current = 6e-9
        with self.assertRaises(GateLeakageTrip):
            gate.set_voltage(0.5)
        self.assertEqual(backend.voltage, 0.0)
        self.assertFalse(backend.output)

    def test_readback_mismatch_fails_closed(self) -> None:
        backend = FakeGateBackend()
        gate = controller(backend)
        gate.enable_output()
        backend.readback_offset = 0.1
        with self.assertRaises(GateReadbackMismatch):
            gate.set_voltage(0.25)
        self.assertEqual(backend.voltage, 0.0)
        self.assertFalse(backend.output)

    def test_read_failure_does_not_overwrite_last_confirmed_state(self) -> None:
        backend = FakeGateBackend()
        gate = controller(backend)
        gate.enable_output()
        confirmed = gate.last_confirmed_state
        backend.fail_measure_voltage = True
        with self.assertRaises(OSError):
            gate.set_voltage(0.25)
        self.assertIs(gate.last_confirmed_state, confirmed)
        self.assertFalse(backend.output)

    def test_disable_attempts_zero_before_output_off(self) -> None:
        backend = FakeGateBackend()
        gate = controller(backend)
        gate.enable_output()
        gate.set_voltage(0.5)
        gate.disable_output()
        self.assertEqual(backend.voltage, 0.0)
        self.assertFalse(backend.output)


if __name__ == "__main__":
    unittest.main()
