"""Opt-in harmonic sensitivity, sharing the daily sweep status/cleanup contract.

Construction performs no I/O. Both lock-ins still step harmonics together.
Only roles with harmonic overrides are owned here; the other role retains the
legacy fixed/segment/autorange controller. Never issue SR830 AGAN.
"""
from __future__ import annotations

import time

from .sr830 import RESERVE_MODE_CODES, Sr830Error
from .sr830_settings import sensitivity_code, sensitivity_full_scale_v, time_constant_seconds


class HarmonicSensitivitySession:
    @classmethod
    def create(cls, xx, xy, sensitivity_setup, reserve_setup):
        if not (xx.harmonic_settings or xy.harmonic_settings):
            return None
        return cls(xx, xy, sensitivity_setup, reserve_setup)

    def __init__(self, xx, xy, sensitivity_setup, reserve_setup):
        self.configs = {"lockin_xx": xx, "lockin_xy": xy}
        self.ranges = sensitivity_setup["ranges"]
        self.reserves = reserve_setup["roles"]
        self.owned = {role for role, config in self.configs.items() if config.harmonic_settings}
        self.current_harmonic = 1
        self.expected_reserves = {role: value["configured_code"] for role, value in self.reserves.items()}

    def setting(self, role, harmonic):
        config = self.configs[role]
        return next((s for s in config.harmonic_settings if s.harmonic == harmonic), config)

    def _read(self, instruments, audit):
        """Append before reading, retaining partial results on communication failure."""
        result = {"captured_unix_s": time.time(), "roles": {}}
        audit.append(result)
        for role, instrument in instruments.items():
            values = result["roles"][role] = {}
            values["harmonic"] = instrument.read_harmonic()
            values["sensitivity_code"] = instrument.read_sensitivity()
            values["sensitivity_full_scale_v"] = sensitivity_full_scale_v(values["sensitivity_code"])
            values["reserve_code"] = instrument.read_reserve_mode()
            values["reserve_mode"] = next((k for k, v in RESERVE_MODE_CODES.items()
                                             if v == values["reserve_code"]), "unknown")
            values["time_constant_code"] = instrument.read_time_constant()
            values["time_constant_s"] = time_constant_seconds(values["time_constant_code"])
        return result

    def _check(self, reading, harmonic, targets=None):
        for role, actual in reading["roles"].items():
            sensitivity, reserve = (targets[role] if targets is not None else
                (self.ranges[role]["current_sensitivity_code"], self.expected_reserves[role]))
            expected = {"harmonic": harmonic[role] if isinstance(harmonic, dict) else harmonic, "sensitivity_code": sensitivity,
                        "reserve_code": reserve, "time_constant_s": self.configs[role].time_constant_s}
            for field, value in expected.items():
                if actual[field] != value:
                    raise Sr830Error(f"{role} harmonic setting readback {field}={actual[field]} != {value}")

    def verify(self, xx, xy, harmonic, record):
        audit = record.setdefault("harmonic_setting_checks", [])
        try:
            reading = self._read({"lockin_xx": xx, "lockin_xy": xy}, audit)
            self._check(reading, harmonic)
            return reading
        except BaseException as exc:
            record.setdefault("harmonic_setting_errors", []).append(str(exc) or type(exc).__name__)
            raise

    def _settle(self, xx, xy, harmonic, settle_s, event):
        from . import lockin_test as daily
        event["settle_interval_s"] = settle_s
        event["settle_intervals"] = 2
        time.sleep(settle_s)
        transition, problems = daily._consume_and_verify_harmonic_transition(
            xx, xy, harmonic=harmonic, settle_s=settle_s, allow_input_reserve_recheck=True)
        event["status"] = transition
        if problems:
            raise Sr830Error("Unsafe harmonic settings transition: " + "; ".join(problems))
        time.sleep(settle_s)

    def _apply(self, xx, xy, targets, harmonic, settle_s, event, *, cleanup=False):
        instruments = {"lockin_xx": xx, "lockin_xy": xy}
        before = self._read(instruments, event.setdefault("readbacks", []))
        commands = event.setdefault("commands", [])
        for role in self.configs:
            if role not in self.owned:
                continue
            actual = before["roles"][role]
            sensitivity, reserve = targets[role]
            # Widen first; reserve before narrowing. Append attempts before I/O.
            operations = []
            if sensitivity > actual["sensitivity_code"]:
                operations.append(("sensitivity", sensitivity))
            if reserve != actual["reserve_code"]:
                operations.append(("reserve_mode", reserve))
            if sensitivity < actual["sensitivity_code"]:
                operations.append(("sensitivity", sensitivity))
            for field, value in operations:
                command = {"role": role, "setting": field, "requested_code": value,
                           "attempted_unix_s": time.time(), "returned": False}
                commands.append(command)
                if field == "sensitivity":
                    self.ranges[role]["write_attempted"] = True
                else:
                    self.reserves[role]["write_attempted"] = True
                getattr(instruments[role], "set_" + field)(value)
                command["returned"] = True
        # Validate commands before consuming status, and again after full settling.
        after = self._read(instruments, event["readbacks"])
        self._check(after, harmonic, targets)
        for role, (sensitivity, reserve) in targets.items():
            self.ranges[role]["current_sensitivity_code"] = sensitivity
            self.expected_reserves[role] = reserve
        if commands:
            if cleanup:
                # Minimum source was requested first. Do not consume a mixed-HARM
                # pair as a sample: final cleanup validates statuses after HARM 1.
                event["settle_interval_s"] = settle_s
                event["settle_intervals"] = 2
                time.sleep(2 * settle_s)
            else:
                self._settle(xx, xy, harmonic, settle_s, event)
            self._check(self._read(instruments, event["readbacks"]), harmonic, targets)

    def _targets(self, harmonic):
        return {role: (sensitivity_code(self.setting(role, harmonic).sensitivity_full_scale_v),
                       RESERVE_MODE_CODES[self.setting(role, harmonic).reserve_mode.value])
                if role in self.owned else
                (self.ranges[role]["current_sensitivity_code"], self.expected_reserves[role])
                for role in self.configs}

    def prepare_bridge(self, xx, xy, settle_s, record, *, cleanup=False):
        """Use the widest configured range and most protective configured Reserve.

        Includes the baseline so cleanup can reuse this after a failed write.
        A bridge readback failure must prevent the subsequent HARM change.
        """
        event = {"stage": "bridge", "started_unix_s": time.time(), "readbacks": []}
        record.setdefault("harmonic_settings_transitions", []).append(event)
        try:
            reading = self._read({"lockin_xx": xx, "lockin_xy": xy}, event["readbacks"])
            harmonic = reading["roles"]["lockin_xx"]["harmonic"]
            if cleanup:
                harmonic = {role: values["harmonic"] for role, values in reading["roles"].items()}
                if all(value == 1 for value in harmonic.values()):
                    event["completed"] = True
                    event["skipped_already_h1"] = True
                    return
            targets = {}
            for role, actual in reading["roles"].items():
                sensitivity, reserve = actual["sensitivity_code"], actual["reserve_code"]
                if role in self.owned:
                    config = self.configs[role]
                    choices = (config, *config.harmonic_settings)
                    sensitivity = max(sensitivity, *(sensitivity_code(s.sensitivity_full_scale_v) for s in choices))
                    reserve = min(reserve, *(RESERVE_MODE_CODES[s.reserve_mode.value] for s in choices))
                targets[role] = sensitivity, reserve
            self._apply(xx, xy, targets, harmonic, settle_s, event, cleanup=cleanup)
            event["completed"] = True
        except BaseException as exc:
            event["error"] = str(exc) or type(exc).__name__
            raise

    def enter(self, xx, xy, *, harmonic, settle_s, record):
        event = {"stage": "harmonic_target", "harmonic": harmonic, "started_unix_s": time.time()}
        record.setdefault("harmonic_settings_transitions", []).append(event)
        try:
            self.verify(xx, xy, self.current_harmonic, record)
            if harmonic != self.current_harmonic:
                self.prepare_bridge(xx, xy, settle_s, record)
                event["harmonic_write_attempted"] = True
                xx.set_harmonic(harmonic)
                xy.set_harmonic(harmonic)
                self._settle(xx, xy, harmonic, settle_s, event)
                self.current_harmonic = harmonic
            self._apply(xx, xy, self._targets(harmonic), harmonic, settle_s,
                        event.setdefault("target_settings", {}))
            event["completed"] = True
        except BaseException as exc:
            event["error"] = str(exc) or type(exc).__name__
            raise
