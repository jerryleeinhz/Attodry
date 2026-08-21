import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.keithley2400 import KeithleyPreflight, KeithleyReading
from attodry_control.three_smu import (
    ThreeSmuSafetyError,
    ThreeSmuSession,
    ThreeSmuWriteNotAuthorized,
    UnknownActiveOutput,
)
from attodry_control.three_smu_config import (
    ChannelPlan,
    ChannelRole,
    FinishAction,
    ScanMode,
    SmuHardwareConfig,
    SourceMode,
    ThreeSmuHardwareConfig,
    ThreeSmuScanPlan,
)


def hardware() -> ThreeSmuHardwareConfig:
    def item(role: str, address: str) -> SmuHardwareConfig:
        return SmuHardwareConfig(
            role=role,
            model="Keithley2400",
            address=address,
            timeout_ms=1000,
            source_mode=SourceMode.VOLTAGE,
            compliance_current_a=1e-3,
            compliance_voltage_v=10.0,
            source_min=-2.0,
            source_max=2.0,
            ramp_step=0.25,
            readback_tolerance=1e-6,
            settle_s=0.0,
            nplc=1.0,
            source_auto_range=True,
            measure_auto_range=True,
            four_wire=False,
            leakage_limit_a=None if role == "smu_bias" else 1e-6,
        )
    return ThreeSmuHardwareConfig(
        item("smu_bias", "FAKE::1"),
        item("gate_top", "FAKE::2"),
        item("gate_bottom", "FAKE::3"),
    )


def fixed_plan(
    *,
    bias: float = 0.0,
    top: float | None = None,
    finish: FinishAction = FinishAction.ZERO_DISABLE,
) -> ThreeSmuScanPlan:
    off = ChannelPlan(ChannelRole.OFF, 0.0, 0.0, 0.0, 1.0)
    return ThreeSmuScanPlan(
        mode=ScanMode.TIME_TRACE,
        samples_per_point=1,
        delay_s=0.0,
        bidirectional=False,
        serpentine=False,
        finish_action=finish,
        point_count=1,
        pulse_high_s=0.0,
        pulse_period_s=0.0,
        smu_bias=ChannelPlan(ChannelRole.FIXED, bias, 0.0, 0.0, 1.0),
        gate_top=(
            off
            if top is None
            else ChannelPlan(ChannelRole.FIXED, top, 0.0, 0.0, 1.0)
        ),
        gate_bottom=off,
    )


class FakeAdapter:
    def __init__(
        self,
        role: str,
        log: list[tuple],
        *,
        identity: str | None = None,
        active_output: bool = False,
    ) -> None:
        self.role = role
        self.log = log
        self.identity = identity or f"KEITHLEY,2400,{role},1"
        self.output = active_output
        self.source = 0.0
        self.config = None
        self.read_count = 0
        self.fail_on_read: int | None = None
        self.interrupt_on_read: int | None = None
        self.trip_on_read: int | None = None
        self.leak_on_read: int | None = None
        self.readback_offset = 0.0

    def preflight(self) -> KeithleyPreflight:
        self.log.append(("preflight", self.role))
        return KeithleyPreflight(
            self.identity, SourceMode.VOLTAGE, self.source, self.output
        )

    def zero_residual(self, mode) -> None:
        self.log.append(("zero_residual", self.role, mode.value))
        self.source = 0.0

    def configure(self, config: SmuHardwareConfig) -> None:
        self.log.append(("configure", self.role))
        self.config = config

    def set_source(self, value: float) -> None:
        self.log.append(("source", self.role, value))
        self.source = value

    def set_output(self, enabled: bool) -> None:
        self.log.append(("output", self.role, enabled))
        self.output = enabled

    def read(self) -> KeithleyReading:
        self.read_count += 1
        if self.interrupt_on_read == self.read_count:
            raise KeyboardInterrupt()
        if self.fail_on_read is not None and self.read_count >= self.fail_on_read:
            raise OSError(f"injected {self.role} communication failure")
        current = self.source * 1e-3 if self.role == "smu_bias" else 1e-9
        if self.leak_on_read == self.read_count:
            current = 2e-6
        return KeithleyReading(
            voltage_v=self.source,
            current_a=current,
            source_setpoint=self.source + self.readback_offset,
            output_enabled=self.output,
            compliance_trip=self.trip_on_read == self.read_count,
            near_compliance=False,
            status="0,No error",
        )

    def close(self) -> None:
        self.log.append(("close", self.role))


def factory_set(
    *,
    active_role: str | None = None,
    duplicate_identity: bool = False,
) -> tuple[dict[str, FakeAdapter], list[tuple], object]:
    log: list[tuple] = []
    adapters: dict[str, FakeAdapter] = {}

    def factory(role: str, _config: SmuHardwareConfig) -> FakeAdapter:
        identity = "SAME" if duplicate_identity else None
        adapter = FakeAdapter(
            role,
            log,
            identity=identity,
            active_output=role == active_role,
        )
        adapters[role] = adapter
        return adapter

    return adapters, log, factory


class ThreeSmuSessionTests(unittest.TestCase):
    def test_authorization_is_checked_before_factory_or_qcodes_import(self) -> None:
        calls: list[str] = []

        def factory(role, _config):
            calls.append(role)
            raise AssertionError("must not be called")

        with self.assertRaises(ThreeSmuWriteNotAuthorized):
            ThreeSmuSession.open(
                hardware(),
                fixed_plan(),
                authorize_writes=False,
                adapter_factory=factory,
            )
        self.assertEqual(calls, [])

    def test_unknown_active_output_stops_without_any_setting_write(self) -> None:
        _adapters, log, factory = factory_set(active_role="gate_top")
        with self.assertRaises(UnknownActiveOutput):
            ThreeSmuSession.open(
                hardware(),
                fixed_plan(),
                authorize_writes=True,
                adapter_factory=factory,
            )
        self.assertFalse(any(item[0] in {"source", "output", "configure", "zero_residual"} for item in log))

    def test_duplicate_physical_identity_stops_before_writes(self) -> None:
        _adapters, log, factory = factory_set(duplicate_identity=True)
        with self.assertRaisesRegex(ThreeSmuSafetyError, "not distinct"):
            ThreeSmuSession.open(
                hardware(),
                fixed_plan(),
                authorize_writes=True,
                adapter_factory=factory,
            )
        self.assertFalse(any(item[0] == "configure" for item in log))

    def test_successful_run_writes_audit_files_and_cleans_up_in_role_order(self) -> None:
        adapters, log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with ThreeSmuSession.open(
                hardware(),
                fixed_plan(bias=0.5, top=0.5),
                authorize_writes=True,
                adapter_factory=factory,
                sleep=lambda _: None,
            ) as session:
                samples = list(session.run(output_dir=directory))
                run_dir = session.last_run_dir
            self.assertEqual(len(samples), 1)
            self.assertIsNotNone(run_dir)
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "completed")
            self.assertTrue(metadata["accepted"])
            self.assertIn("code_version", metadata)
            self.assertEqual(metadata["cleanup"]["result"], "confirmed_safe")
            self.assertTrue((run_dir / "raw.jsonl").is_file())
            self.assertIn(
                '"event": "preflight"',
                (run_dir / "raw.jsonl").read_text(encoding="utf-8"),
            )
            self.assertEqual(len((run_dir / "data.csv").read_text(encoding="utf-8").splitlines()), 2)
        cleanup_off = [
            item[1]
            for item in log
            if len(item) == 3 and item[0] == "output" and item[2] is False
        ]
        self.assertEqual(cleanup_off[-3:], ["smu_bias", "gate_top", "gate_bottom"])
        self.assertTrue(all(not adapter.output for adapter in adapters.values()))

    def test_gate_leakage_failure_is_rejected_and_cleaned_up(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ThreeSmuSafetyError, "leakage"):
                with ThreeSmuSession.open(
                    hardware(),
                    fixed_plan(top=0.5),
                    authorize_writes=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["gate_top"].leak_on_read = 4
                    list(session.run(output_dir=directory))
            metadata = json.loads(
                (session.last_run_dir / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "rejected")
            self.assertFalse(metadata["accepted"])
            self.assertFalse(adapters["gate_top"].output)

    def test_compliance_trip_formal_sample_is_retained_as_problem(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ThreeSmuSafetyError, "compliance trip"):
                with ThreeSmuSession.open(
                    hardware(),
                    fixed_plan(),
                    authorize_writes=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].trip_on_read = 3
                    list(session.run(output_dir=directory))
            csv_text = (session.last_run_dir / "data.csv").read_text(encoding="utf-8")
            self.assertIn("smu_bias compliance trip", csv_text)

    def test_readback_mismatch_fails_closed(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ThreeSmuSafetyError, "readback mismatch"):
                with ThreeSmuSession.open(
                    hardware(),
                    fixed_plan(),
                    authorize_writes=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].readback_offset = 0.1
                    list(session.run(output_dir=directory))
            self.assertFalse(adapters["smu_bias"].output)

    def test_communication_failure_preserves_last_confirmed_and_requires_manual_check(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                with ThreeSmuSession.open(
                    hardware(),
                    fixed_plan(),
                    authorize_writes=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].fail_on_read = 3
                    list(session.run(output_dir=directory))
            self.assertIn("smu_bias", session.last_confirmed)
            metadata = json.loads(
                (session.last_run_dir / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertTrue(metadata["cleanup"]["manual_verification_required"])
            self.assertEqual(
                metadata["cleanup"]["result"], "manual_verification_required"
            )

    def test_unconfirmed_normal_cleanup_rejects_otherwise_clean_run(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ThreeSmuSafetyError, "cleanup could not confirm"
            ):
                with ThreeSmuSession.open(
                    hardware(),
                    fixed_plan(),
                    authorize_writes=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].fail_on_read = 4
                    list(session.run(output_dir=directory))
            metadata = json.loads(
                (session.last_run_dir / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "rejected")
            self.assertFalse(metadata["accepted"])
            self.assertTrue(metadata["cleanup"]["manual_verification_required"])

    def test_keyboard_interrupt_is_audited_and_cleanup_runs(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KeyboardInterrupt):
                with ThreeSmuSession.open(
                    hardware(),
                    fixed_plan(),
                    authorize_writes=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].interrupt_on_read = 3
                    list(session.run(output_dir=directory))
            metadata = json.loads(
                (session.last_run_dir / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "interrupted")
            self.assertFalse(adapters["smu_bias"].output)


if __name__ == "__main__":
    unittest.main()
