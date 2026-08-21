from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.lockin_test import _consume_sensitivity_transition, run
from attodry_control.lockin_autorange import (
    AutorangeAction,
    AutorangeDecision,
    AutorangeState,
)
from attodry_control.models import LockinRole
from attodry_control.sr830 import (
    AuthorizationRequired,
    DualSr830Controller,
    Sr830AcquisitionError,
    Sr830,
    Sr830Error,
    configure_fixed_settings_pair,
    configure_minimum_excitation_pair,
    decode_lia_status,
    execute_autorange_transition,
)
from attodry_control.sr830_settings import (
    ExternalReferenceEdge,
    InputCoupling,
    InputMode,
    ReferenceSource,
    ShieldGrounding,
    map_sr830_settings,
)


class FakeVisaResource:
    def __init__(
        self,
        responses: dict[str, str | list[str]],
        fail_write: str | None = None,
        *,
        name: str = "instrument",
        events: list[tuple[str, str, str]] | None = None,
    ):
        self.responses = responses
        self.fail_write = fail_write
        self.queries: list[str] = []
        self.writes: list[str] = []
        self.closed = False
        self.name = name
        self.events = events

    def query(self, command: str) -> str:
        self.queries.append(command)
        if self.events is not None:
            self.events.append((self.name, "query", command))
        try:
            response = self.responses[command]
            if isinstance(response, list):
                if not response:
                    raise AssertionError(f"No queued response remains for: {command}")
                return response.pop(0)
            return response
        except KeyError as exc:
            raise AssertionError(f"Unexpected query: {command}") from exc

    def write(self, command: str) -> None:
        self.writes.append(command)
        if self.events is not None:
            self.events.append((self.name, "write", command))
        if command == self.fail_write:
            raise OSError("injected VISA write failure")
        if command.startswith("HARM "):
            self.responses["HARM?"] = command.split()[1] + "\n"
        for prefix, query in (
            ("ISRC ", "ISRC?"),
            ("IGND ", "IGND?"),
            ("ICPL ", "ICPL?"),
            ("OFLT ", "OFLT?"),
            ("OFSL ", "OFSL?"),
            ("SENS ", "SENS?"),
        ):
            if command.startswith(prefix):
                self.responses[query] = command.split()[1] + "\n"

    def close(self) -> None:
        self.closed = True


class TrackingVisaResource(FakeVisaResource):
    def __init__(
        self,
        responses: dict[str, str | list[str]],
        *,
        shared_frequency: dict[str, float],
        name: str,
        frequency_scale: float = 1.0,
    ):
        super().__init__(responses, name=name)
        self.shared_frequency = shared_frequency
        self.frequency_scale = frequency_scale

    def query(self, command: str) -> str:
        if command == "FREQ?":
            self.queries.append(command)
            return f"{self.shared_frequency['hz'] * self.frequency_scale:g}\n"
        if command == "SNAP? 1,2,3,4,9":
            self.queries.append(command)
            frequency_hz = self.shared_frequency["hz"] * self.frequency_scale
            return f"1e-6,2e-7,1.0198e-6,11.31,{frequency_hz:g}\n"
        return super().query(command)

    def write(self, command: str) -> None:
        super().write(command)
        if command.startswith("FREQ "):
            self.shared_frequency["hz"] = float(command.split()[1])
        elif command.startswith("SLVL "):
            self.responses["SLVL?"] = command.split()[1] + "\n"
        elif command.startswith("SENS "):
            self.responses["SENS?"] = command.split()[1] + "\n"


class FakeResourceManager:
    def __init__(self, resources: dict[str, FakeVisaResource]):
        self.resources = resources
        self.opened: list[str] = []
        self.closed = False

    def list_resources(self) -> tuple[str, ...]:
        return tuple(self.resources)

    def open_resource(self, address: str) -> FakeVisaResource:
        self.opened.append(address)
        return self.resources[address]

    def close(self) -> None:
        self.closed = True


def responses(reference_mode: int, *, frequency_hz: float = 17.777) -> dict[str, str]:
    serial = "s/n00111" if reference_mode == 1 else "s/n00222"
    return {
        "*IDN?": f"Stanford_Research_Systems,SR830,{serial},ver1.000\n",
        "FMOD?": f"{reference_mode}\n",
        "RSLP?": "1\n",
        "FREQ?": f"{frequency_hz:g}\n",
        "HARM?": "1\n",
        "SLVL?": "0.004\n",
        "ISRC?": "1\n",
        "IGND?": "0\n",
        "ICPL?": "0\n",
        "ILIN?": "0\n",
        "SENS?": "23\n",
        "RMOD?": "1\n",
        "OFLT?": "10\n",
        "OFSL?": "3\n",
        "PHAS?": "7.5\n",
        "SNAP? 1,2,3,4,9": (
            f"1e-6,2e-7,1.0198e-6,11.31,{frequency_hz:g}\n"
        ),
        "LIAS?": "0\n",
        "ERRS?": "0\n",
    }


class Sr830Tests(unittest.TestCase):
    @staticmethod
    def _fixed_codes(*, external: bool):
        return map_sr830_settings(
            reference_source=(
                ReferenceSource.EXTERNAL_TTL
                if external
                else ReferenceSource.INTERNAL
            ),
            external_reference_edge=(
                ExternalReferenceEdge.RISING if external else None
            ),
            input_mode=InputMode.A_MINUS_B,
            shield_grounding=ShieldGrounding.FLOAT,
            input_coupling=InputCoupling.AC,
            time_constant_s=0.3,
            filter_slope_db_oct=24,
            sensitivity_full_scale_v=0.001,
        )

    def _hardware_config(self, *, frequency_hz: float = 17.777) -> Path:
        temporary = tempfile.NamedTemporaryFile(suffix=".toml", delete=False)
        temporary.close()
        path = Path(temporary.name)
        template = Path("config/hardware.example.toml").read_text(encoding="utf-8")
        configured = (
            template.replace(
                "CHANGE_ME_SR830_XX_VISA_ADDRESS", "GPIB0::8::INSTR"
            )
            .replace("CHANGE_ME_SR830_XY_VISA_ADDRESS", "GPIB0::9::INSTR")
            .replace("timeout_ms = 5000", "timeout_ms = 4321")
            .replace("frequency_hz = 17.777", f"frequency_hz = {frequency_hz:g}")
        )
        path.write_text(configured, encoding="utf-8")
        self.addCleanup(path.unlink)
        return path

    def test_diagnostic_without_status_only_sends_queries(self) -> None:
        resource = FakeVisaResource(responses(reference_mode=1))
        instrument = Sr830(resource, LockinRole.XX)

        diagnostic = instrument.read_diagnostic(consume_status_latches=False)

        self.assertEqual(diagnostic.role, LockinRole.XX)
        self.assertAlmostEqual(diagnostic.x_v, 1e-6)
        self.assertEqual(diagnostic.phase_shift_deg, 7.5)
        self.assertIn("PHAS?", resource.queries)
        self.assertIsNone(diagnostic.lia_status)
        self.assertNotIn("LIAS?", resource.queries)
        self.assertNotIn("ERRS?", resource.queries)
        self.assertEqual(resource.writes, [])

    def test_fixed_setting_readback_interfaces_are_query_only(self) -> None:
        resource = FakeVisaResource(responses(reference_mode=1))
        instrument = Sr830(resource, LockinRole.XX)

        self.assertEqual(instrument.read_sensitivity(), 23)
        self.assertEqual(instrument.read_time_constant(), 10)
        self.assertEqual(instrument.read_filter_slope(), 3)
        self.assertEqual(instrument.read_phase_shift(), 7.5)

        self.assertEqual(resource.writes, [])
        self.assertEqual(
            resource.queries, ["SENS?", "OFLT?", "OFSL?", "PHAS?"]
        )

    def test_fixed_setting_pair_refuses_unauthorized_writes_before_queries(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_resource = FakeVisaResource(responses(reference_mode=0))

        with self.assertRaises(AuthorizationRequired):
            configure_fixed_settings_pair(
                Sr830(xx_resource, LockinRole.XX),
                Sr830(xy_resource, LockinRole.XY),
                xx_settings=self._fixed_codes(external=False),
                xy_settings=self._fixed_codes(external=True),
                expected_frequency_hz=17.777,
                settle_s=1.5,
                sleeper=lambda _: None,
                authorize_writes=False,
                confirm_xy_sine_disconnected=True,
            )

        self.assertEqual(xx_resource.queries, [])
        self.assertEqual(xy_resource.queries, [])
        self.assertEqual(xx_resource.writes, [])
        self.assertEqual(xy_resource.writes, [])

    def test_fixed_setting_pair_requires_five_time_constants_before_queries(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_resource = FakeVisaResource(responses(reference_mode=0))

        with self.assertRaisesRegex(Sr830Error, "at least 1.5 s"):
            configure_fixed_settings_pair(
                Sr830(xx_resource, LockinRole.XX),
                Sr830(xy_resource, LockinRole.XY),
                xx_settings=self._fixed_codes(external=False),
                xy_settings=self._fixed_codes(external=True),
                expected_frequency_hz=17.777,
                settle_s=1.49,
                sleeper=lambda _: None,
                authorize_writes=True,
                confirm_xy_sine_disconnected=True,
            )

        self.assertEqual(xx_resource.queries, [])
        self.assertEqual(xy_resource.queries, [])
        self.assertEqual(xx_resource.writes, [])
        self.assertEqual(xy_resource.writes, [])

    def test_fixed_setting_pair_diagnoses_writes_and_verifies_without_auto_phase(self) -> None:
        events: list[tuple[str, str, str]] = []
        xx_resource = FakeVisaResource(
            responses(reference_mode=1), name="xx", events=events
        )
        xy_resource = FakeVisaResource(
            responses(reference_mode=0), name="xy", events=events
        )
        delays: list[float] = []

        result = configure_fixed_settings_pair(
            Sr830(xx_resource, LockinRole.XX),
            Sr830(xy_resource, LockinRole.XY),
            xx_settings=self._fixed_codes(external=False),
            xy_settings=self._fixed_codes(external=True),
            expected_frequency_hz=17.777,
            settle_s=1.5,
            sleeper=delays.append,
            authorize_writes=True,
            confirm_xy_sine_disconnected=True,
        )

        expected_writes = [
            "ISRC 1",
            "IGND 0",
            "ICPL 0",
            "OFLT 9",
            "OFSL 3",
            "SENS 17",
        ]
        self.assertEqual(xx_resource.writes, expected_writes)
        self.assertEqual(xy_resource.writes, expected_writes)
        self.assertEqual(delays, [1.5])
        first_write = next(index for index, event in enumerate(events) if event[1] == "write")
        preflight_identities = [
            event for event in events[:first_write] if event[1:] == ("query", "*IDN?")
        ]
        preflight_snapshots = [
            event
            for event in events[:first_write]
            if event[1:] == ("query", "SNAP? 1,2,3,4,9")
        ]
        self.assertEqual(len(preflight_identities), 2)
        self.assertEqual(len(preflight_snapshots), 2)
        self.assertEqual(result.after_xx.time_constant, 9)
        self.assertEqual(result.after_xx.sensitivity, 17)
        self.assertEqual(result.after_xy.sensitivity, 17)
        self.assertEqual(result.after_xx.phase_shift_deg, 7.5)
        self.assertEqual(result.after_xy.phase_shift_deg, 7.5)
        self.assertFalse(
            any("APHS" in command for command in xx_resource.writes + xy_resource.writes)
        )

    def test_fixed_setting_write_failure_minimizes_outputs_and_restores(self) -> None:
        xx_resource = FakeVisaResource(
            responses(reference_mode=1), fail_write="OFLT 9"
        )
        xy_resource = FakeVisaResource(responses(reference_mode=0))

        with self.assertRaisesRegex(OSError, "injected VISA write failure"):
            configure_fixed_settings_pair(
                Sr830(xx_resource, LockinRole.XX),
                Sr830(xy_resource, LockinRole.XY),
                xx_settings=self._fixed_codes(external=False),
                xy_settings=self._fixed_codes(external=True),
                expected_frequency_hz=17.777,
                settle_s=1.5,
                sleeper=lambda _: None,
                authorize_writes=True,
                confirm_xy_sine_disconnected=True,
            )

        self.assertIn("SLVL 0.004", xx_resource.writes)
        self.assertIn("SLVL 0.004", xy_resource.writes)
        self.assertIn("OFLT 10", xx_resource.writes)
        self.assertIn("SENS 23", xy_resource.writes)
        self.assertFalse(
            any("APHS" in command for command in xx_resource.writes + xy_resource.writes)
        )

    def test_fixed_setting_rejects_phase_shift_change_and_cleans_up(self) -> None:
        xx_responses = responses(reference_mode=1)
        xx_responses["PHAS?"] = ["7.5\n", "8.0\n", "7.5\n"]
        xx_resource = FakeVisaResource(xx_responses)
        xy_resource = FakeVisaResource(responses(reference_mode=0))

        with self.assertRaisesRegex(Sr830Error, "PHAS shift changed"):
            configure_fixed_settings_pair(
                Sr830(xx_resource, LockinRole.XX),
                Sr830(xy_resource, LockinRole.XY),
                xx_settings=self._fixed_codes(external=False),
                xy_settings=self._fixed_codes(external=True),
                expected_frequency_hz=17.777,
                settle_s=1.5,
                sleeper=lambda _: None,
                authorize_writes=True,
                confirm_xy_sine_disconnected=True,
            )

        self.assertIn("SLVL 0.004", xx_resource.writes)
        self.assertIn("SLVL 0.004", xy_resource.writes)
        self.assertFalse(
            any("APHS" in command for command in xx_resource.writes + xy_resource.writes)
        )

    def test_status_bits_decode_unlock_and_overloads(self) -> None:
        status = decode_lia_status(0b0000_1111)

        self.assertTrue(status.input_or_reserve_overload)
        self.assertTrue(status.filter_overload)
        self.assertTrue(status.output_overload)
        self.assertTrue(status.reference_unlocked)
        self.assertTrue(status.any_overload)

    def test_autorange_transition_requires_both_authorizations_before_io(self) -> None:
        resource = FakeVisaResource(responses(reference_mode=1))
        decision = AutorangeDecision(
            AutorangeAction.WIDEN,
            AutorangeState(0.02, 1, 0),
            0.85,
            "target occupancy reached",
        )
        with self.assertRaises(AuthorizationRequired):
            execute_autorange_transition(
                Sr830(resource, LockinRole.XX),
                decision=decision,
                previous_full_scale_v=0.01,
                settle_s=1.5,
                sleeper=lambda _: None,
                authorize_writes=True,
                authorize_status_latch_consumption=False,
            )
        self.assertEqual(resource.queries, [])
        self.assertEqual(resource.writes, [])

    def test_autorange_transition_settles_records_and_freezes_range(self) -> None:
        response_map = responses(reference_mode=1)
        response_map["SENS?"] = "20\n"
        response_map["LIAS?"] = ["0\n", "0\n"]
        resource = FakeVisaResource(response_map)
        delays: list[float] = []
        decision = AutorangeDecision(
            AutorangeAction.WIDEN,
            AutorangeState(0.02, 1, 0),
            0.85,
            "target occupancy reached",
        )
        result = execute_autorange_transition(
            Sr830(resource, LockinRole.XX),
            decision=decision,
            previous_full_scale_v=0.01,
            settle_s=1.5,
            sleeper=delays.append,
            authorize_writes=True,
            authorize_status_latch_consumption=True,
        )
        self.assertEqual(resource.writes, ["SENS 21"])
        self.assertEqual(delays, [1.5, 1.5])
        self.assertEqual(result.previous_sensitivity_code, 20)
        self.assertEqual(result.final_sensitivity_code, 21)
        self.assertTrue(result.formal_range_frozen)
        self.assertIsNotNone(result.transition_sample)
        self.assertIsNotNone(result.verification_sample)

    def test_autorange_final_overload_fails_and_restores_previous_range(self) -> None:
        response_map = responses(reference_mode=1)
        response_map["SENS?"] = "20\n"
        response_map["LIAS?"] = ["0\n", "4\n"]
        resource = FakeVisaResource(response_map)
        decision = AutorangeDecision(
            AutorangeAction.WIDEN,
            AutorangeState(0.02, 1, 0),
            0.85,
            "target occupancy reached",
        )
        with self.assertRaises(Sr830AcquisitionError):
            execute_autorange_transition(
                Sr830(resource, LockinRole.XX),
                decision=decision,
                previous_full_scale_v=0.01,
                settle_s=1.5,
                sleeper=lambda _: None,
                authorize_writes=True,
                authorize_status_latch_consumption=True,
            )
        self.assertIn("SLVL 0.004", resource.writes)
        self.assertEqual(resource.writes[-1], "SENS 20")

    def test_configuration_requires_both_explicit_authorizations(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_resource = FakeVisaResource(responses(reference_mode=0))

        with self.assertRaises(AuthorizationRequired):
            configure_minimum_excitation_pair(
                Sr830(xx_resource, LockinRole.XX),
                Sr830(xy_resource, LockinRole.XY),
                frequency_hz=17.777,
                authorize_writes=False,
                confirm_xy_sine_disconnected=True,
            )

        self.assertEqual(xx_resource.queries, [])
        self.assertEqual(xx_resource.writes, [])
        self.assertEqual(xy_resource.queries, [])
        self.assertEqual(xy_resource.writes, [])

    def test_configuration_minimizes_both_outputs_before_role_settings(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_resource = FakeVisaResource(responses(reference_mode=0))
        xx = Sr830(xx_resource, LockinRole.XX)
        xy = Sr830(xy_resource, LockinRole.XY)

        configure_minimum_excitation_pair(
            xx,
            xy,
            frequency_hz=17.777,
            authorize_writes=True,
            confirm_xy_sine_disconnected=True,
        )

        self.assertEqual(xx_resource.writes[0], "SLVL 0.004")
        self.assertEqual(xy_resource.writes[0], "SLVL 0.004")
        self.assertEqual(
            xx_resource.writes[1:],
            ["FMOD 1", "HARM 1", "FREQ 17.777"],
        )
        self.assertEqual(
            xy_resource.writes[1:],
            ["FMOD 0", "RSLP 1", "HARM 1"],
        )

    def test_configuration_rejects_wrong_idn_before_writes(self) -> None:
        bad = responses(reference_mode=1)
        bad["*IDN?"] = "OTHER,MODEL,0,0"
        xx_resource = FakeVisaResource(bad)
        xy_resource = FakeVisaResource(responses(reference_mode=0))

        with self.assertRaisesRegex(Sr830Error, "SR830"):
            configure_minimum_excitation_pair(
                Sr830(xx_resource, LockinRole.XX),
                Sr830(xy_resource, LockinRole.XY),
                frequency_hz=17.777,
                authorize_writes=True,
                confirm_xy_sine_disconnected=True,
            )

        self.assertEqual(xx_resource.writes, [])
        self.assertEqual(xy_resource.writes, [])

    def test_configuration_rejects_duplicate_physical_identity_before_writes(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        duplicate = responses(reference_mode=0)
        duplicate["*IDN?"] = responses(reference_mode=1)["*IDN?"]
        xy_resource = FakeVisaResource(duplicate)

        with self.assertRaisesRegex(Sr830Error, "same SR830 identity"):
            configure_minimum_excitation_pair(
                Sr830(xx_resource, LockinRole.XX),
                Sr830(xy_resource, LockinRole.XY),
                frequency_hz=17.777,
                authorize_writes=True,
                confirm_xy_sine_disconnected=True,
            )

        self.assertEqual(xx_resource.writes, [])
        self.assertEqual(xy_resource.writes, [])

    def test_write_failure_attempts_minimum_output_cleanup_on_both_units(self) -> None:
        xx_resource = FakeVisaResource(
            responses(reference_mode=1), fail_write="FREQ 17.777"
        )
        xy_resource = FakeVisaResource(responses(reference_mode=0))

        with self.assertRaisesRegex(OSError, "injected"):
            configure_minimum_excitation_pair(
                Sr830(xx_resource, LockinRole.XX),
                Sr830(xy_resource, LockinRole.XY),
                frequency_hz=17.777,
                authorize_writes=True,
                confirm_xy_sine_disconnected=True,
            )

        self.assertEqual(xx_resource.writes[-1], "SLVL 0.004")
        self.assertEqual(xy_resource.writes[-1], "SLVL 0.004")

    def test_cli_diagnose_never_writes_settings(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_resource = FakeVisaResource(responses(reference_mode=0))
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run(
                [
                    "diagnose",
                    "--xx-address",
                    "XX",
                    "--xy-address",
                    "XY",
                ],
                resource_manager_factory=lambda: manager,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertFalse(result["safety_status_complete"])
        self.assertTrue(result["limitations"])
        self.assertEqual(result["lockin_xx"]["role"], "xx")
        self.assertEqual(xx_resource.writes, [])
        self.assertEqual(xy_resource.writes, [])
        self.assertTrue(manager.closed)

    def test_cli_diagnose_uses_semantic_addresses_and_timeout_from_config(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_resource = FakeVisaResource(responses(reference_mode=0))
        manager = FakeResourceManager(
            {
                "GPIB0::8::INSTR": xx_resource,
                "GPIB0::9::INSTR": xy_resource,
            }
        )

        with redirect_stdout(io.StringIO()):
            exit_code = run(
                ["diagnose", "--config", str(self._hardware_config())],
                resource_manager_factory=lambda: manager,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(manager.opened, ["GPIB0::8::INSTR", "GPIB0::9::INSTR"])
        self.assertEqual(xx_resource.timeout, 4321)
        self.assertEqual(xy_resource.timeout, 4321)

    def test_cli_diagnose_accepts_one_millihertz_pair_readback_difference(self) -> None:
        xx_resource = FakeVisaResource(
            responses(reference_mode=1, frequency_hz=17.777)
        )
        xy_resource = FakeVisaResource(
            responses(reference_mode=0, frequency_hz=17.778)
        )
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})

        with redirect_stdout(io.StringIO()):
            exit_code = run(
                ["diagnose", "--xx-address", "XX", "--xy-address", "XY"],
                resource_manager_factory=lambda: manager,
            )

        self.assertEqual(exit_code, 0)

    def test_cli_diagnose_rejects_two_millihertz_pair_readback_difference(self) -> None:
        xx_resource = FakeVisaResource(
            responses(reference_mode=1, frequency_hz=17.777)
        )
        xy_resource = FakeVisaResource(
            responses(reference_mode=0, frequency_hz=17.779)
        )
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})

        with redirect_stdout(io.StringIO()):
            exit_code = run(
                ["diagnose", "--xx-address", "XX", "--xy-address", "XY"],
                resource_manager_factory=lambda: manager,
            )

        self.assertEqual(exit_code, 1)

    def test_cli_configuration_uses_frequency_from_config(self) -> None:
        frequency_hz = 19.0
        xx_resource = FakeVisaResource(
            responses(reference_mode=1, frequency_hz=frequency_hz)
        )
        xy_resource = FakeVisaResource(
            responses(reference_mode=0, frequency_hz=frequency_hz)
        )
        manager = FakeResourceManager(
            {
                "GPIB0::8::INSTR": xx_resource,
                "GPIB0::9::INSTR": xy_resource,
            }
        )

        with redirect_stdout(io.StringIO()):
            exit_code = run(
                [
                    "configure-minimum",
                    "--config",
                    str(self._hardware_config(frequency_hz=frequency_hz)),
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                ],
                resource_manager_factory=lambda: manager,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("FREQ 19", xx_resource.writes)

    def test_cli_refuses_unauthorized_configuration_before_opening_resources(self) -> None:
        manager = FakeResourceManager({})

        with self.assertRaises(AuthorizationRequired):
            run(
                [
                    "configure-minimum",
                    "--xx-address",
                    "XX",
                    "--xy-address",
                    "XY",
                    "--confirm-xy-sine-disconnected",
                ],
                resource_manager_factory=lambda: manager,
            )

        self.assertEqual(manager.opened, [])

    def test_cli_refuses_unauthorized_harmonics_before_opening_resources(self) -> None:
        manager = FakeResourceManager({})

        with self.assertRaises(AuthorizationRequired):
            run(
                [
                    "measure-harmonics",
                    "--xx-address",
                    "XX",
                    "--xy-address",
                    "XY",
                    "--confirm-xy-sine-disconnected",
                ],
                resource_manager_factory=lambda: manager,
            )

        self.assertEqual(manager.opened, [])

    def test_cli_harmonics_records_six_readings_and_restores_first_harmonic(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_resource = FakeVisaResource(responses(reference_mode=0))
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run(
                [
                    "measure-harmonics",
                    "--xx-address",
                    "XX",
                    "--xy-address",
                    "XY",
                    "--settle-s",
                    "0",
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                ],
                resource_manager_factory=lambda: manager,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertEqual(len(result["readings"]), 6)
        self.assertTrue(result["pair_reads_are_sequential"])
        self.assertTrue(
            all("captured_at_utc" in reading for reading in result["readings"])
        )
        self.assertTrue(
            all(reading["phase_shift_deg"] == 7.5 for reading in result["readings"])
        )
        self.assertEqual(result["restored_harmonic"], 1)
        self.assertEqual(xx_resource.responses["HARM?"], "1\n")
        self.assertEqual(xy_resource.responses["HARM?"], "1\n")
        self.assertEqual(
            [write for write in xx_resource.writes if write.startswith("HARM ")],
            ["HARM 1", "HARM 2", "HARM 3", "HARM 1"],
        )
        self.assertFalse(
            any(
                write.startswith(
                    ("FMOD ", "RSLP ", "FREQ ", "SENS ", "OFLT ", "OFSL ")
                )
                for write in xx_resource.writes + xy_resource.writes
            )
        )

    def test_cli_harmonic_preflight_unlock_stops_before_writes(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_responses = responses(reference_mode=0)
        xy_responses["LIAS?"] = "8\n"
        xy_resource = FakeVisaResource(xy_responses)
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})

        with self.assertRaisesRegex(Sr830Error, "preflight.*unlocked"):
            run(
                [
                    "measure-harmonics",
                    "--xx-address",
                    "XX",
                    "--xy-address",
                    "XY",
                    "--settle-s",
                    "0",
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                ],
                resource_manager_factory=lambda: manager,
            )

        self.assertEqual(xx_resource.writes, [])
        self.assertEqual(xy_resource.writes, [])

    def test_cli_harmonic_failure_records_partial_data_and_restores_safe_state(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_responses = responses(reference_mode=0)
        xy_responses["LIAS?"] = ["0\n", "8\n"]
        xy_resource = FakeVisaResource(xy_responses)
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(Sr830AcquisitionError):
            run(
                [
                    "measure-harmonics",
                    "--xx-address",
                    "XX",
                    "--xy-address",
                    "XY",
                    "--settle-s",
                    "0",
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                ],
                resource_manager_factory=lambda: manager,
            )

        result = json.loads(output.getvalue())
        self.assertFalse(result["completed"])
        self.assertEqual(len(result["partial_readings"]), 2)
        self.assertEqual(xx_resource.writes[-2:], ["HARM 1", "SLVL 0.004"])
        self.assertEqual(xy_resource.writes[-2:], ["HARM 1", "SLVL 0.004"])

    def test_cli_sweeps_refuse_unauthorized_writes_before_opening_resources(self) -> None:
        for command in ("sweep-frequency", "sweep-excitation"):
            manager = FakeResourceManager({})
            arguments = [command, "--xx-address", "XX", "--xy-address", "XY"]
            if command == "sweep-excitation":
                arguments.extend(
                    [
                        "--series-resistance-ohm", "100000",
                        "--device-resistance-ohm", "1000",
                        "--max-device-current-a", "0.005",
                        "--max-device-voltage-v", "5",
                    ]
                )
            with self.assertRaises(AuthorizationRequired):
                run(arguments, resource_manager_factory=lambda: manager)
            self.assertEqual(manager.opened, [])

    def test_cli_frequency_sweep_records_points_and_restores_baseline(self) -> None:
        shared_frequency = {"hz": 17.777}
        xx_resource = TrackingVisaResource(
            responses(reference_mode=1), shared_frequency=shared_frequency, name="xx"
        )
        xy_resource = TrackingVisaResource(
            responses(reference_mode=0), shared_frequency=shared_frequency, name="xy"
        )
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run(
                [
                    "sweep-frequency",
                    "--xx-address", "XX",
                    "--xy-address", "XY",
                    "--points-hz", "17.777,1000",
                    "--settle-s", "0",
                    "--samples-per-point", "1",
                    "--sample-interval-s", "0",
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                ],
                resource_manager_factory=lambda: manager,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertEqual(len(result["points"]), 2)
        self.assertEqual(
            xx_resource.writes,
            [
                "SENS 21",
                "FREQ 1000",
                "SLVL 0.004",
                "FREQ 17.777",
                "SENS 23",
            ],
        )
        self.assertEqual(xy_resource.writes, [])
        self.assertTrue(result["cleanup"]["verified"])
        self.assertEqual(result["temporary_xx_sensitivity_code"], 21)
        self.assertEqual(result["cleanup"]["final"]["lockin_xx"]["sensitivity"], 23)

    def test_cli_frequency_sweep_separates_transition_latches_from_sample_window(self) -> None:
        shared_frequency = {"hz": 17.777}
        xx_resource = TrackingVisaResource(
            responses(reference_mode=1), shared_frequency=shared_frequency, name="xx"
        )
        xy_responses = responses(reference_mode=0)
        xy_responses["LIAS?"] = [
            "0\n", "0\n", "26\n", "0\n", "24\n", "0\n", "0\n"
        ]
        xy_resource = TrackingVisaResource(
            xy_responses, shared_frequency=shared_frequency, name="xy"
        )
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run(
                [
                    "sweep-frequency",
                    "--xx-address", "XX",
                    "--xy-address", "XY",
                    "--points-hz", "17.777,25",
                    "--settle-s", "0",
                    "--samples-per-point", "1",
                    "--sample-interval-s", "0",
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                ],
                resource_manager_factory=lambda: manager,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        transition = result["points"][1]["transition_status"]
        self.assertEqual(transition["lockin_xy"]["lia_status"]["raw"], 26)
        self.assertEqual(
            result["points"][1]["samples"][0]["lockin_xy"]["lia_status"]["raw"],
            0,
        )
        self.assertEqual(result["cleanup"]["transition_status"]["lockin_xy"]["lia_status"]["raw"], 24)
        self.assertTrue(result["cleanup"]["verified"])

    def test_cli_frequency_sweep_accepts_locked_external_readback_within_100_ppm(self) -> None:
        shared_frequency = {"hz": 17.777}
        xx_resource = TrackingVisaResource(
            responses(reference_mode=1), shared_frequency=shared_frequency, name="xx"
        )
        xy_resource = TrackingVisaResource(
            responses(reference_mode=0),
            shared_frequency=shared_frequency,
            name="xy",
            frequency_scale=49.9973 / 50.0,
        )
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run(
                [
                    "sweep-frequency",
                    "--xx-address", "XX",
                    "--xy-address", "XY",
                    "--points-hz", "17.777,50",
                    "--settle-s", "0",
                    "--samples-per-point", "1",
                    "--sample-interval-s", "0",
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                ],
                resource_manager_factory=lambda: manager,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertAlmostEqual(
            result["points"][1]["frequency_readback_hz"]["lockin_xy"],
            49.9973,
            places=4,
        )

    def test_cli_excitation_sweep_checks_limits_and_restores_original_range(self) -> None:
        shared_frequency = {"hz": 17.777}
        xx_resource = TrackingVisaResource(
            responses(reference_mode=1), shared_frequency=shared_frequency, name="xx"
        )
        xy_resource = TrackingVisaResource(
            responses(reference_mode=0), shared_frequency=shared_frequency, name="xy"
        )
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run(
                [
                    "sweep-excitation",
                    "--xx-address", "XX",
                    "--xy-address", "XY",
                    "--points-v", "0.004,0.4",
                    "--series-resistance-ohm", "100000",
                    "--device-resistance-ohm", "1000",
                    "--max-device-current-a", "0.005",
                    "--max-device-voltage-v", "5",
                    "--settle-s", "0",
                    "--samples-per-point", "1",
                    "--sample-interval-s", "0",
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                    "--confirm-no-50ohm-termination",
                ],
                resource_manager_factory=lambda: manager,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertAlmostEqual(
            result["safety"]["nominal_maximum_current_a_rms"],
            0.4 / 101050.0,
        )
        self.assertEqual(
            xx_resource.writes,
            [
                "SENS 21",
                "SLVL 0.4",
                "SLVL 0.004",
                "SENS 23",
            ],
        )
        self.assertEqual(xy_resource.writes, [])
        self.assertEqual(result["cleanup"]["final"]["lockin_xx"]["sensitivity"], 23)

    def test_cli_excitation_sweep_clears_range_restoration_overload_before_final_status(self) -> None:
        shared_frequency = {"hz": 17.777}
        xx_responses = responses(reference_mode=1)
        xx_responses["LIAS?"] = ["0\n", "0\n", "0\n", "4\n", "0\n"]
        xx_resource = TrackingVisaResource(
            xx_responses, shared_frequency=shared_frequency, name="xx"
        )
        xy_resource = TrackingVisaResource(
            responses(reference_mode=0), shared_frequency=shared_frequency, name="xy"
        )
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run(
                [
                    "sweep-excitation",
                    "--xx-address", "XX",
                    "--xy-address", "XY",
                    "--points-v", "0.004,0.4",
                    "--series-resistance-ohm", "100000",
                    "--device-resistance-ohm", "1000",
                    "--max-device-current-a", "0.005",
                    "--max-device-voltage-v", "5",
                    "--settle-s", "0",
                    "--samples-per-point", "1",
                    "--sample-interval-s", "0",
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                    "--confirm-no-50ohm-termination",
                ],
                resource_manager_factory=lambda: manager,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        transition = result["cleanup"]["sensitivity_transition_status"]
        self.assertEqual(transition["lockin_xx"]["lia_status"]["raw"], 4)
        self.assertEqual(
            result["cleanup"]["final"]["lockin_xx"]["lia_status"]["raw"], 0
        )

    def test_sensitivity_transition_does_not_discard_xy_overload(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1), name="xx")
        xy_responses = responses(reference_mode=0)
        xy_responses["LIAS?"] = "4\n"
        xy_resource = FakeVisaResource(xy_responses, name="xy")

        record, problems = _consume_sensitivity_transition(
            Sr830(xx_resource, LockinRole.XX),
            Sr830(xy_resource, LockinRole.XY),
        )

        self.assertIn("lockin_xy overloaded during XX sensitivity restoration", problems)
        self.assertEqual(record["lockin_xy"]["lia_status"]["raw"], 4)

    def test_cli_excitation_overload_keeps_rejected_sample_and_cleans_up(self) -> None:
        shared_frequency = {"hz": 17.777}
        xx_responses = responses(reference_mode=1)
        xx_responses["LIAS?"] = ["0\n", "0\n", "1\n", "0\n", "0\n"]
        xx_resource = TrackingVisaResource(
            xx_responses, shared_frequency=shared_frequency, name="xx"
        )
        xy_resource = TrackingVisaResource(
            responses(reference_mode=0), shared_frequency=shared_frequency, name="xy"
        )
        manager = FakeResourceManager({"XX": xx_resource, "XY": xy_resource})
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaisesRegex(Sr830Error, "overload"):
            run(
                [
                    "sweep-excitation",
                    "--xx-address", "XX",
                    "--xy-address", "XY",
                    "--points-v", "0.004,0.4",
                    "--series-resistance-ohm", "100000",
                    "--device-resistance-ohm", "1000",
                    "--max-device-current-a", "0.005",
                    "--max-device-voltage-v", "5",
                    "--settle-s", "0",
                    "--samples-per-point", "1",
                    "--sample-interval-s", "0",
                    "--authorize-writes",
                    "--confirm-xy-sine-disconnected",
                    "--confirm-no-50ohm-termination",
                ],
                resource_manager_factory=lambda: manager,
            )

        result = json.loads(output.getvalue())
        self.assertFalse(result["completed"])
        self.assertEqual(len(result["points"]), 2)
        self.assertEqual(len(result["points"][1]["samples"]), 1)
        self.assertTrue(result["cleanup"]["verified"])
        self.assertEqual(xx_resource.writes[-2:], ["SLVL 0.004", "SENS 23"])

    def test_dual_controller_sets_both_harmonics_before_each_pair_snapshot(self) -> None:
        events: list[tuple[str, str, str]] = []
        xx_resource = FakeVisaResource(
            responses(reference_mode=1), name="xx", events=events
        )
        xy_resource = FakeVisaResource(
            responses(reference_mode=0), name="xy", events=events
        )
        controller = DualSr830Controller(
            Sr830(xx_resource, LockinRole.XX),
            Sr830(xy_resource, LockinRole.XY),
        )
        controller.configure_minimum(
            frequency_hz=17.777,
            authorize_writes=True,
            confirm_xy_sine_disconnected=True,
        )
        events.clear()

        result = controller.measure_harmonics(
            settle_s=0.0, sleeper=lambda _: None
        )

        self.assertEqual(len(result.readings), 6)
        self.assertEqual(
            {(reading.role, reading.harmonic) for reading in result.readings},
            {
                (role, harmonic)
                for role in (LockinRole.XX, LockinRole.XY)
                for harmonic in (1, 2, 3)
            },
        )
        for harmonic in (1, 2, 3):
            xx_write = events.index(("xx", "write", f"HARM {harmonic}"))
            xy_write = events.index(("xy", "write", f"HARM {harmonic}"))
            snapshots = [
                index
                for index, event in enumerate(events)
                if event[1:] == ("query", "SNAP? 1,2,3,4,9")
                and index > min(xx_write, xy_write)
            ]
            self.assertLess(xx_write, snapshots[0])
            self.assertLess(xy_write, snapshots[0])

    def test_dual_controller_accepts_one_millihertz_pair_readback_difference(
        self,
    ) -> None:
        xx_resource = FakeVisaResource(
            responses(reference_mode=1, frequency_hz=17.777)
        )
        xy_resource = FakeVisaResource(
            responses(reference_mode=0, frequency_hz=17.778)
        )
        controller = DualSr830Controller(
            Sr830(xx_resource, LockinRole.XX),
            Sr830(xy_resource, LockinRole.XY),
        )

        controller.configure_minimum(
            frequency_hz=17.777,
            authorize_writes=True,
            confirm_xy_sine_disconnected=True,
        )
        result = controller.measure_harmonics(settle_s=0.0, sleeper=lambda _: None)

        self.assertEqual(len(result.readings), 6)
        self.assertEqual(len(result.samples), 6)
        self.assertTrue(result.pair_reads_are_sequential)
        self.assertTrue(
            all(
                sample.captured_at_utc.utcoffset().total_seconds() == 0
                for sample in result.samples
            )
        )
        for index in range(0, len(result.samples), 2):
            self.assertLessEqual(
                result.samples[index].captured_at_utc,
                result.samples[index + 1].captured_at_utc,
            )
        self.assertTrue(
            all(reading.phase_shift_deg == 7.5 for reading in result.readings)
        )

    def test_dual_controller_unlock_fails_closed_with_partial_raw_readings(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_responses = responses(reference_mode=0)
        xy_responses["LIAS?"] = "8\n"
        xy_resource = FakeVisaResource(xy_responses)
        controller = DualSr830Controller(
            Sr830(xx_resource, LockinRole.XX),
            Sr830(xy_resource, LockinRole.XY),
        )
        controller.configure_minimum(
            frequency_hz=17.777,
            authorize_writes=True,
            confirm_xy_sine_disconnected=True,
        )

        with self.assertRaisesRegex(Sr830AcquisitionError, "unlocked") as caught:
            controller.measure_harmonics(settle_s=0.0, sleeper=lambda _: None)

        self.assertEqual(len(caught.exception.partial_readings), 2)
        self.assertEqual(xx_resource.writes[-1], "SLVL 0.004")
        self.assertEqual(xy_resource.writes[-1], "SLVL 0.004")

    def test_dual_controller_overload_fails_closed(self) -> None:
        xx_responses = responses(reference_mode=1)
        xx_responses["LIAS?"] = "1\n"
        xx_resource = FakeVisaResource(xx_responses)
        xy_resource = FakeVisaResource(responses(reference_mode=0))
        controller = DualSr830Controller(
            Sr830(xx_resource, LockinRole.XX),
            Sr830(xy_resource, LockinRole.XY),
        )
        controller.configure_minimum(
            frequency_hz=17.777,
            authorize_writes=True,
            confirm_xy_sine_disconnected=True,
        )

        with self.assertRaisesRegex(Sr830AcquisitionError, "overload"):
            controller.measure_harmonics(settle_s=0.0, sleeper=lambda _: None)

        self.assertEqual(xx_resource.writes[-1], "SLVL 0.004")


if __name__ == "__main__":
    unittest.main()
