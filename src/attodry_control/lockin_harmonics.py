"""Independent role/harmonic scheduling and sensitivity; construction has no I/O.

Fixed single-harmonic roles stay at their selected harmonic between points.
Status checks still cover both connected instruments at their actual harmonics.
Never issue SR830 AGAN. Cleanup remains minimum excitation then verified h1.
"""
from __future__ import annotations

import time
import math
from dataclasses import asdict

from .lockin_autorange import AutorangePolicy, AutorangeState
from .sr830_settings import SensitivityMode

from .sr830 import RESERVE_MODE_CODES, Sr830Error
from .sr830_settings import sensitivity_code, sensitivity_full_scale_v, time_constant_seconds


class HarmonicSensitivitySession:
    @classmethod
    def create(cls, xx, xy, sensitivity_setup, reserve_setup, harmonics_by_role=None):
        return cls(xx, xy, sensitivity_setup, reserve_setup, harmonics_by_role)

    def __init__(self, xx, xy, sensitivity_setup, reserve_setup, harmonics_by_role=None):
        self.configs = {"lockin_xx": xx, "lockin_xy": xy}
        self.sensitivity_setup = sensitivity_setup
        self.ranges = sensitivity_setup["ranges"]
        self.reserves = reserve_setup["roles"]
        selections = harmonics_by_role or {"xx": (1, 2, 3), "xy": (1, 2, 3)}
        self.selected = {"lockin_" + role: tuple(values) for role, values in selections.items()}
        self.owned = {role for role, config in self.configs.items()
                      if self.selected[role] and (config.harmonic_settings or
                          config.sensitivity_mode is SensitivityMode.BOUNDED_AUTO)}
        self.policies = {}
        self.states = {}
        for role in self.owned:
            for harmonic in self.selected[role]:
                setting = self.setting(role, harmonic)
                if setting.sensitivity_mode is SensitivityMode.BOUNDED_AUTO:
                    policy = AutorangePolicy(
                        setting.autorange_min_full_scale_v, setting.autorange_max_full_scale_v,
                        setting.autorange_target_occupancy, setting.autorange_stable_samples,
                        setting.autorange_full_scales_v)
                    self.policies[role, harmonic] = policy
                    # Begin at the largest approved auto range, not a blind narrow.
                    initial = (policy.maximum_full_scale_v if self.configs[role].harmonic_settings
                               else sensitivity_full_scale_v(self.ranges[role]["current_sensitivity_code"]))
                    self.states[role, harmonic] = AutorangeState(initial)
        self.current_harmonics = {role: 1 for role in self.configs}
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
            self.diagnose_overload(xx, xy, transition, settle_s, event)
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

    def _targets(self, harmonics, roles):
        targets = {}
        for role, harmonic in harmonics.items():
            if role in self.owned and role in roles:
                setting = self.setting(role, harmonic)
                state = self.states.get((role, harmonic))
                targets[role] = (sensitivity_code(state.current_full_scale_v if state else
                                                 setting.sensitivity_full_scale_v),
                                 RESERVE_MODE_CODES[setting.reserve_mode.value])
            else:
                targets[role] = self.ranges[role]["current_sensitivity_code"], self.expected_reserves[role]
        return targets

    def prepare_bridge(self, xx, xy, settle_s, record, *, cleanup=False, roles=None):
        """Use the widest configured range and most protective configured Reserve.

        Includes the baseline so cleanup can reuse this after a failed write.
        A bridge readback failure must prevent the subsequent HARM change.
        """
        event = {"stage": "bridge", "started_unix_s": time.time(), "readbacks": []}
        record.setdefault("harmonic_settings_transitions", []).append(event)
        try:
            reading = self._read({"lockin_xx": xx, "lockin_xy": xy}, event["readbacks"])
            harmonic = {role: values["harmonic"] for role, values in reading["roles"].items()}
            if cleanup:
                harmonic = {role: values["harmonic"] for role, values in reading["roles"].items()}
                if all(value == 1 for value in harmonic.values()):
                    event["completed"] = True
                    event["skipped_already_h1"] = True
                    return
            targets = {}
            for role, actual in reading["roles"].items():
                sensitivity, reserve = actual["sensitivity_code"], actual["reserve_code"]
                if role in self.owned and (roles is None or role in roles):
                    config = self.configs[role]
                    choices = (config, *config.harmonic_settings)
                    sensitivity = max(sensitivity, *(sensitivity_code(s.autorange_max_full_scale_v or s.sensitivity_full_scale_v) for s in choices))
                    reserve = min(reserve, *(RESERVE_MODE_CODES[s.reserve_mode.value] for s in choices))
                targets[role] = sensitivity, reserve
            self._apply(xx, xy, targets, harmonic, settle_s, event, cleanup=cleanup)
            event["completed"] = True
        except BaseException as exc:
            event["error"] = str(exc) or type(exc).__name__
            raise

    def initial_harmonics(self, frequency_hz):
        from .lockin_test import MAXIMUM_REFERENCE_FREQUENCY_HZ
        return {role: next((h for h in sorted(values)
                            if h * frequency_hz <= MAXIMUM_REFERENCE_FREQUENCY_HZ), 1)
                for role, values in self.selected.items()}

    def prepare_frequency(self, xx, xy, frequency_hz, settle_s, record):
        """Park only incompatible detectors before FREQ can reset/reject HARM."""
        from .lockin_test import MAXIMUM_REFERENCE_FREQUENCY_HZ
        targets = {role: 1 if h * frequency_hz > MAXIMUM_REFERENCE_FREQUENCY_HZ else h
                   for role, h in self.current_harmonics.items()}
        if targets != self.current_harmonics:
            record["harmonic_parking_reason"] = "detection frequency limit before FREQ"
            self.enter(xx, xy, harmonic=targets, settle_s=settle_s, record=record)

    def enter(self, xx, xy, *, harmonic, settle_s, record, roles=None):
        selected = set(self.configs if roles is None else ("lockin_" + role for role in roles))
        targets = dict(self.current_harmonics)
        if isinstance(harmonic, dict):
            targets.update(harmonic)
        else:
            targets.update({role: harmonic for role in selected})
        event = {"stage": "harmonic_target", "harmonic": harmonic,
                 "harmonics_by_role": targets, "started_unix_s": time.time(), "commands": []}
        record.setdefault("harmonic_settings_transitions", []).append(event)
        try:
            self.verify(xx, xy, self.current_harmonics, record)
            changed = {role for role, h in targets.items() if h != self.current_harmonics[role]}
            if changed:
                self.prepare_bridge(xx, xy, settle_s, record, roles=changed)
                event["harmonic_write_attempted"] = True
                for role, instrument in (("lockin_xx", xx), ("lockin_xy", xy)):
                    if role in changed:
                        attempt = {"role": role, "harmonic": targets[role], "returned": False}
                        event["commands"].append(attempt)
                        instrument.set_harmonic(targets[role])
                        attempt["returned"] = True
                self._settle(xx, xy, targets, settle_s, event)
                record.setdefault("harmonic_transition_status", []).append(event["status"])
                self.current_harmonics = targets
            self._apply(xx, xy, self._targets(targets, selected), targets, settle_s,
                        event.setdefault("target_settings", {}))
            event["completed"] = True
        except BaseException as exc:
            event["error"] = str(exc) or type(exc).__name__
            raise


    def qualify(self, xx, xy, *, harmonic, settle_s, target_frequency_hz,
                frequency_rel_tolerance, record, roles=None):
        """One existing bounded-auto controller, with state owned by (role, h)."""
        from . import lockin_test as daily
        selected = set(self.configs if roles is None else ("lockin_" + role for role in roles))
        policies = {role: policy for (role, h), policy in self.policies.items()
                    if h == harmonic and role in selected}
        if not policies:
            return
        states = {role: self.states[role, harmonic] for role in policies}
        audit = {"harmonic": harmonic, "started_unix_s": time.time(),
                 "policies": {role: asdict(policy) for role, policy in policies.items()}}
        record.setdefault("harmonic_autorange", []).append(audit)
        try:
            daily._apply_sweep_autorange(
                xx, xy, sensitivity_setup=self.sensitivity_setup, policies=policies, states=states,
                target_frequency_hz=target_frequency_hz, frequency_rel_tolerance=frequency_rel_tolerance,
                settle_s=settle_s, record=audit, harmonic=self.current_harmonics,
                on_input_overload=lambda status: self.diagnose_overload(xx, xy, status, settle_s, audit),
                verify_settings=lambda: self.verify(xx, xy, self.current_harmonics, audit))
            # Preserve the first legacy auto audit view; per-harmonic entries
            # remain authoritative when a role selects multiple harmonics.
            record.setdefault("autorange", audit["autorange"])
            audit["completed"] = True
        except BaseException as exc:
            audit["error"] = str(exc) or type(exc).__name__
            raise
        finally:
            for role, state in states.items():
                self.states[role, harmonic] = state
            audit["states"] = {role: asdict(state) for role, state in states.items()}

    def diagnose_overload(self, xx, xy, status, settle_s, record):
        """One best-effort h1 snapshot after a confirmed fixed-range input trip.

        This is an abort-only audit: no gain/source change, no retry of formal
        acquisition, no claim that saturated X/Y/R are valid signal estimates.
        Callers must raise the original failure after this method returns.
        """
        from . import lockin_test as daily
        latest = status.get("verification", status)
        for role, instrument in (("lockin_xx", xx), ("lockin_xy", xy)):
            prior = latest.get(role, {})
            if not prior.get("lia_status", {}).get("input_or_reserve_overload"):
                continue
            harmonic = prior["reading"]["harmonic"]
            if self.setting(role, harmonic).sensitivity_mode is SensitivityMode.BOUNDED_AUTO:
                continue
            audit = {"role": role, "reason": "confirmed input/reserve overload in fixed mode",
                     "started_unix_s": time.time(), "original_sample": prior,
                     "valid_for_analysis": False, "source_and_gain_changes_allowed": False,
                     "completed": False}
            record.setdefault("pre_abort_h1_diagnostics", []).append(audit)
            try:
                audit["source_readback_v_rms"] = xx.read_sine_output()
                audit["harmonic_before"] = instrument.read_harmonic()
                sensitivity = instrument.read_sensitivity()
                reserve = instrument.read_reserve_mode()
                audit["sensitivity_code"] = sensitivity
                audit["sensitivity_full_scale_v"] = sensitivity_full_scale_v(sensitivity)
                audit["reserve_code"] = reserve
                audit["reserve_mode"] = next(k for k, v in RESERVE_MODE_CODES.items() if v == reserve)
                if audit["harmonic_before"] != 1:
                    audit["harmonic_write_attempted"] = True
                    instrument.set_harmonic(1)
                    audit["harmonic_write_returned"] = True
                    audit["settle_s"] = 2 * settle_s
                    time.sleep(2 * settle_s)
                audit["h1_sample"] = daily._audited_harmonic_sample_record(instrument.read_harmonic_sample(1))
                audit["sensitivity_after"] = instrument.read_sensitivity()
                audit["reserve_after"] = instrument.read_reserve_mode()
                if audit["sensitivity_after"] != sensitivity or audit["reserve_after"] != reserve:
                    raise Sr830Error("Settings changed during abort-only h1 diagnosis")
                audit["completed"] = True
            except BaseException as exc:
                # Never mask the original overload or prevent outer cleanup.
                audit["error"] = str(exc) or type(exc).__name__

    def formal_problems(self, harmonic, xx, xy, roles=None):
        problems = []
        for role, sample in (("lockin_xx", xx), ("lockin_xy", xy)):
            if roles is not None and sample.reading.role.value not in roles:
                continue
            policy = self.policies.get((role, harmonic))
            if policy is None:
                continue
            amplitude = sample.reading.amplitude_v
            scale = sensitivity_full_scale_v(self.ranges[role]["current_sensitivity_code"])
            if not math.isfinite(amplitude) or amplitude < 0 or amplitude >= policy.target_occupancy * scale:
                problems.append(f"{role} h{harmonic} formal sample exceeds bounded-auto occupancy; retain rejected raw sample")
        return problems
