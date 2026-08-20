from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.lockin_test import run
from attodry_control.models import LockinRole
from attodry_control.sr830 import (
    AuthorizationRequired,
    DualSr830Controller,
    Sr830AcquisitionError,
    Sr830,
    Sr830Error,
    configure_minimum_excitation_pair,
    decode_lia_status,
)


class FakeVisaResource:
    def __init__(
        self,
        responses: dict[str, str],
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
            return self.responses[command]
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

    def close(self) -> None:
        self.closed = True


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
        "SNAP? 1,2,3,4,9": (
            f"1e-6,2e-7,1.0198e-6,11.31,{frequency_hz:g}\n"
        ),
        "LIAS?": "0\n",
        "ERRS?": "0\n",
    }


class Sr830Tests(unittest.TestCase):
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
        self.assertIsNone(diagnostic.lia_status)
        self.assertNotIn("LIAS?", resource.queries)
        self.assertNotIn("ERRS?", resource.queries)
        self.assertEqual(resource.writes, [])

    def test_status_bits_decode_unlock_and_overloads(self) -> None:
        status = decode_lia_status(0b0000_1111)

        self.assertTrue(status.input_or_reserve_overload)
        self.assertTrue(status.filter_overload)
        self.assertTrue(status.output_overload)
        self.assertTrue(status.reference_unlocked)
        self.assertTrue(status.any_overload)

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
        self.assertEqual(result["restored_harmonic"], 1)
        self.assertEqual(xx_resource.responses["HARM?"], "1\n")
        self.assertEqual(xy_resource.responses["HARM?"], "1\n")
        self.assertEqual(
            [write for write in xx_resource.writes if write.startswith("HARM ")],
            ["HARM 1", "HARM 1", "HARM 2", "HARM 3", "HARM 1"],
        )
        self.assertFalse(
            any(
                write.startswith(("SENS ", "OFLT ", "OFSL "))
                for write in xx_resource.writes + xy_resource.writes
            )
        )

    def test_cli_harmonic_failure_records_partial_data_and_restores_safe_state(self) -> None:
        xx_resource = FakeVisaResource(responses(reference_mode=1))
        xy_responses = responses(reference_mode=0)
        xy_responses["LIAS?"] = "8\n"
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
