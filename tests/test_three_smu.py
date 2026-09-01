from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.keithley2400 import (
    KeithleyConfigurationReadback,
    KeithleyPreflight,
    KeithleyReading,
)
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
            source_mode=SourceMode.VOLTAGE,
            max_abs_voltage_v=10.0,
            max_abs_current_a=1e-3,
            nplc=1.0,
            source_auto_range=True,
            measure_auto_range=True,
            four_wire=False,
        )
    return ThreeSmuHardwareConfig(
        item("smu_bias", "FAKE::1"),
        item("gate_top", "FAKE::2"),
        item("gate_bottom", "FAKE::3"),
    )


def current_source_gate_hardware() -> ThreeSmuHardwareConfig:
    configured = hardware()
    return replace(
        configured,
        gate_top=replace(
            configured.gate_top,
            source_mode=SourceMode.CURRENT,
        ),
    )


def fixed_plan(
    *,
    bias: float = 0.0,
    top: float | None = None,
    finish: FinishAction = FinishAction.ZERO_DISABLE,
) -> ThreeSmuScanPlan:
    off = ChannelPlan(ChannelRole.OFF, False)
    return ThreeSmuScanPlan(
        mode=ScanMode.TIME_TRACE,
        samples_per_point=1,
        delay_s=0.0,
        serpentine=False,
        finish_action=finish,
        point_count=1,
        pulse_high_s=0.0,
        pulse_period_s=0.0,
        smu_bias=ChannelPlan(ChannelRole.FIXED, False, fixed=bias),
        gate_top=(
            off
            if top is None
            else ChannelPlan(ChannelRole.FIXED, False, fixed=top)
        ),
        gate_bottom=off,
    )


class FakeAdapter:
    def __init__(
        self,
        role: str,
        log: list[tuple],
        hardware_config: SmuHardwareConfig,
        *,
        identity: str | None = None,
        active_output: bool = False,
    ) -> None:
        self.role = role
        self.log = log
        self.hardware_config = hardware_config
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
        self.voltage_override: float | None = None
        self.current_override: float | None = None

    def preflight(self) -> KeithleyPreflight:
        self.log.append(("preflight", self.role))
        voltage_v: float | None = None
        current_a: float | None = None
        if self.output:
            voltage_v = (
                self.source
                if self.hardware_config.source_mode is SourceMode.VOLTAGE
                else 0.0
            )
            current_a = (
                self.source
                if self.hardware_config.source_mode is SourceMode.CURRENT
                else self.source * 1e-3 if self.role == "smu_bias" else 1e-9
            )
        return KeithleyPreflight(
            self.identity,
            self.hardware_config.source_mode,
            self.source,
            self.output,
            voltage_v=voltage_v,
            current_a=current_a,
            status="0,No error",
            status_query_consumed=True,
        )

    def zero_residual(self, mode) -> None:
        self.log.append(("zero_residual", self.role, mode.value))
        self.source = 0.0

    def configure(self, config: SmuHardwareConfig) -> KeithleyConfigurationReadback:
        self.log.append(("configure", self.role))
        self.config = config
        return KeithleyConfigurationReadback(
            compliance_limit=(
                float(config.max_abs_current_a)
                if config.source_mode is SourceMode.VOLTAGE
                else float(config.max_abs_voltage_v)
            ),
            source_range=1.0,
            measure_range=1.0,
        )

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
        if self.hardware_config.source_mode is SourceMode.VOLTAGE:
            voltage = self.source
            current = self.source * 1e-3 if self.role == "smu_bias" else 1e-9
        else:
            voltage = 0.0
            current = self.source
        if self.leak_on_read == self.read_count:
            current = 2e-6
        if self.voltage_override is not None:
            voltage = self.voltage_override
        if self.current_override is not None:
            current = self.current_override
        return KeithleyReading(
            voltage_v=voltage,
            current_a=current,
            source_setpoint=self.source + self.readback_offset,
            output_enabled=self.output,
            compliance_trip=self.trip_on_read == self.read_count,
            status="0,No error",
            status_query_consumed=True,
        )

    def close(self) -> None:
        self.log.append(("close", self.role))


def factory_set(
    *,
    active_role: str | None = None,
    duplicate_identity: bool = False,
    initial_source: float = 0.0,
) -> tuple[dict[str, FakeAdapter], list[tuple], object]:
    log: list[tuple] = []
    adapters: dict[str, FakeAdapter] = {}

    def factory(role: str, config: SmuHardwareConfig) -> FakeAdapter:
        identity = "SAME" if duplicate_identity else None
        adapter = FakeAdapter(
            role,
            log,
            config,
            identity=identity,
            active_output=role == active_role,
        )
        adapter.source = initial_source
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
                fixed_plan(top=0.0),
                authorize_writes=True,
                authorize_status_consumption=True,
                adapter_factory=factory,
            )
        self.assertFalse(any(item[0] in {"source", "output", "configure", "zero_residual"} for item in log))

    def test_nonzero_output_off_preflight_is_taken_over_by_direct_zero(self) -> None:
        _adapters, log, factory = factory_set(initial_source=0.1)
        session = ThreeSmuSession.open(
            hardware(),
            fixed_plan(),
            authorize_writes=True,
            authorize_status_consumption=True,
            adapter_factory=factory,
        )
        session.close()
        self.assertFalse(any(item[0] in {"source", "output", "configure", "zero_residual"} for item in log))

    def test_status_consumption_authorization_is_checked_before_factory(self) -> None:
        calls: list[str] = []

        def factory(role, _config):
            calls.append(role)
            raise AssertionError("must not be called")

        with self.assertRaisesRegex(ThreeSmuWriteNotAuthorized, "status_consumption"):
            ThreeSmuSession.open(
                hardware(),
                fixed_plan(),
                authorize_writes=True,
                adapter_factory=factory,
            )
        self.assertEqual(calls, [])

    def test_duplicate_physical_identity_stops_before_writes(self) -> None:
        _adapters, log, factory = factory_set(duplicate_identity=True)
        with self.assertRaisesRegex(ThreeSmuSafetyError, "not distinct"):
            ThreeSmuSession.open(
                hardware(),
                fixed_plan(top=0.0),
                authorize_writes=True,
                authorize_status_consumption=True,
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
                authorize_status_consumption=True,
                adapter_factory=factory,
                sleep=lambda _: None,
            ) as session:
                samples = list(session.run(output_dir=directory))
                run_dir = session.last_run_dir
            self.assertEqual(len(samples), 1)
            self.assertIsNotNone(run_dir)
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["schema_version"], 5)
            self.assertEqual(metadata["active_roles"], ["smu_bias", "gate_top"])
            self.assertEqual(metadata["off_roles"], ["gate_bottom"])
            self.assertEqual(metadata["status"], "completed")
            self.assertTrue(metadata["accepted"])
            self.assertIn("code_version", metadata)
            self.assertEqual(metadata["cleanup"]["result"], "confirmed_safe")
            self.assertTrue((run_dir / "raw.jsonl").is_file())
            self.assertIn(
                '"event": "preflight"',
                (run_dir / "raw.jsonl").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '"configuration_readback"',
                (run_dir / "raw.jsonl").read_text(encoding="utf-8"),
            )
            self.assertEqual(len((run_dir / "data.csv").read_text(encoding="utf-8").splitlines()), 2)
        cleanup_off = [
            item[1]
            for item in log
            if len(item) == 3 and item[0] == "output" and item[2] is False
        ]
        self.assertEqual(cleanup_off[-2:], ["smu_bias", "gate_top"])
        self.assertNotIn("gate_bottom", adapters)
        self.assertTrue(all(not adapter.output for adapter in adapters.values()))
        self.assertEqual(
            [item[2] for item in log if item[:2] == ("source", "smu_bias")].count(0.5),
            1,
        )

    def test_bottom_only_run_never_constructs_or_calls_off_roles(self) -> None:
        configured = hardware()
        bottom_only_hardware = ThreeSmuHardwareConfig(
            gate_bottom=configured.gate_bottom
        )
        bottom_only_plan = ThreeSmuScanPlan(
            mode=ScanMode.BOTTOM_GATE_TRANSFER,
            samples_per_point=1,
            delay_s=0.0,
            serpentine=False,
            finish_action=FinishAction.ZERO_DISABLE,
            point_count=1,
            pulse_high_s=0.0,
            pulse_period_s=0.0,
            smu_bias=ChannelPlan(ChannelRole.OFF, False),
            gate_top=ChannelPlan(ChannelRole.OFF, False),
            gate_bottom=ChannelPlan(
                ChannelRole.SWEEP, False, points=(-1.0, 0.0, 1.0)
            ),
        )
        adapters, log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with ThreeSmuSession.open(
                bottom_only_hardware,
                bottom_only_plan,
                authorize_writes=True,
                authorize_status_consumption=True,
                adapter_factory=factory,
                sleep=lambda _: None,
            ) as session:
                samples = list(session.run(output_dir=directory))
                metadata = json.loads(
                    (session.last_run_dir / "metadata.json").read_text(encoding="utf-8")
                )
        self.assertEqual(set(adapters), {"gate_bottom"})
        self.assertEqual({item[1] for item in log if len(item) > 1}, {"gate_bottom"})
        self.assertEqual(len(samples), 3)
        self.assertTrue(
            all(set(sample.readings) == {"gate_bottom"} for sample in samples)
        )
        self.assertEqual(metadata["active_roles"], ["gate_bottom"])
        self.assertEqual(metadata["off_roles"], ["smu_bias", "gate_top"])

    def test_current_source_gate_uses_current_unit_ramp_and_readback(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with ThreeSmuSession.open(
                current_source_gate_hardware(),
                fixed_plan(top=5e-4),
                authorize_writes=True,
                authorize_status_consumption=True,
                adapter_factory=factory,
                sleep=lambda _: None,
            ) as session:
                samples = list(session.run(output_dir=directory))
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].readings["gate_top"].reading.current_a, 5e-4)
        self.assertFalse(adapters["gate_top"].output)

    def test_bias_current_absolute_limit_rejects_readback(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ThreeSmuSafetyError, "max_abs_current_a"):
                with ThreeSmuSession.open(
                    hardware(),
                    fixed_plan(),
                    authorize_writes=True,
                    authorize_status_consumption=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].current_override = 2e-3
                    list(session.run(output_dir=directory))

    def test_current_source_gate_voltage_absolute_limit_rejects_readback(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ThreeSmuSafetyError, "max_abs_voltage_v"):
                with ThreeSmuSession.open(
                    current_source_gate_hardware(),
                    fixed_plan(top=5e-4),
                    authorize_writes=True,
                    authorize_status_consumption=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["gate_top"].voltage_override = 11.0
                    list(session.run(output_dir=directory))

    def test_gate_current_below_max_abs_is_recorded_without_separate_leakage_limit(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with ThreeSmuSession.open(
                hardware(),
                fixed_plan(top=0.5),
                authorize_writes=True,
                authorize_status_consumption=True,
                adapter_factory=factory,
                sleep=lambda _: None,
            ) as session:
                adapters["gate_top"].current_override = 2e-6
                list(session.run(output_dir=directory))
            metadata = json.loads(
                (session.last_run_dir / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "completed")
            self.assertTrue(metadata["accepted"])
            self.assertFalse(adapters["gate_top"].output)

    def test_compliance_trip_formal_sample_is_retained_as_problem(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ThreeSmuSafetyError, "compliance trip"):
                with ThreeSmuSession.open(
                    hardware(),
                    fixed_plan(),
                authorize_writes=True,
                authorize_status_consumption=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].trip_on_read = 2
                    list(session.run(output_dir=directory))
            csv_text = (session.last_run_dir / "data.csv").read_text(encoding="utf-8")
            self.assertIn("smu_bias compliance trip", csv_text)

    def test_source_readback_difference_is_recorded_without_tolerance_rejection(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with ThreeSmuSession.open(
                hardware(),
                fixed_plan(),
                authorize_writes=True,
                authorize_status_consumption=True,
                adapter_factory=factory,
                sleep=lambda _: None,
            ) as session:
                adapters["smu_bias"].readback_offset = 0.1
                samples = list(session.run(output_dir=directory))
            self.assertEqual(
                samples[0].readings["smu_bias"].reading.source_setpoint,
                0.1,
            )
            self.assertFalse(adapters["smu_bias"].output)

    def test_communication_failure_preserves_last_confirmed_and_requires_manual_check(self) -> None:
        adapters, _log, factory = factory_set()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                with ThreeSmuSession.open(
                    hardware(),
                    fixed_plan(),
                authorize_writes=True,
                authorize_status_consumption=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].fail_on_read = 2
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
                authorize_status_consumption=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].fail_on_read = 3
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
                authorize_status_consumption=True,
                    adapter_factory=factory,
                    sleep=lambda _: None,
                ) as session:
                    adapters["smu_bias"].interrupt_on_read = 2
                    list(session.run(output_dir=directory))
            metadata = json.loads(
                (session.last_run_dir / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["status"], "interrupted")
            self.assertFalse(adapters["smu_bias"].output)


if __name__ == "__main__":
    unittest.main()
