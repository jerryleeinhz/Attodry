"""Detector-only SR830 XX / SR865A XY session for an external AC source.

The source module owns current, frequency, output and cleanup. This session owns
only detector/reference settings and reuses the existing formal pair, sensitivity
and settling engine. Construction and plan validation perform no instrument I/O.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, dataclass
import math
from pathlib import Path
from types import SimpleNamespace

from . import config as cfg
from . import lockin_test as daily
from .combination_store import utc_now
from .electrical_lockin_backend import Sr865aReceiver, native_status_problems
from .lockin_harmonics import HarmonicSensitivitySession
from .lockin_points import LockinPointSession, _AuditedInstrument
from .lockin_overload import OVERLOAD_POLICIES
from .models import LockinRole
from .sr830 import Sr830, Sr830Error, AuthorizationRequired


class _ExternalReferenceFrequencyMismatch(Sr830Error):
    """A finite in-range coordinate is not yet within the existing target gate."""
    def __init__(self, message, evidence):
        super().__init__(message)
        self.frequency_evidence = evidence
        self.evidence = evidence
        self.raw = tuple(record for observation in evidence.get('observations', {}).values()
                         for record in observation.get('raw', ()))


@dataclass(frozen=True)
class ExternalLockinAcquisitionConfig:
    xx_harmonics: tuple[int, ...]
    xy_harmonics: tuple[int, ...]
    settle_time_constants: float
    sample_interval_time_constants: float
    overload_policy: str
    skip_unsupported_harmonics: bool = False


def parse_external_lockin_acquisition(table, xx, xy):
    name = "lockin_acquisition"
    fields = {"xx_harmonics", "xy_harmonics", "settle_time_constants",
              "sample_interval_time_constants", "overload_policy", "skip_unsupported_harmonics"}
    cfg._strict_keys_with_optional(table, name, fields, set())
    selected = {}
    for role in ("xx", "xy"):
        values = table[role + "_harmonics"]
        if (not isinstance(values, list) or any(type(h) is not int or h not in (1, 2, 3) for h in values)
                or len(set(values)) != len(values)):
            raise cfg.ConfigError(f"{name}.{role}_harmonics requires unique integers from 1, 2, 3")
        selected[role] = tuple(values)
    if not selected["xx"] and not selected["xy"]:
        raise cfg.ConfigError("External acquisition must select at least one role/harmonic")
    settle = cfg._positive_number(table["settle_time_constants"], name + ".settle_time_constants")
    if settle < 5:
        raise cfg.ConfigError("External acquisition requires at least 5 time constants of settling")
    interval = cfg._number(table["sample_interval_time_constants"], name + ".sample_interval_time_constants")
    if interval < 0:
        raise cfg.ConfigError("External sample interval cannot be negative")
    policy = table["overload_policy"]
    if policy not in OVERLOAD_POLICIES:
        raise cfg.ConfigError("Unknown external Lock-in overload policy")
    skip = cfg._boolean(table["skip_unsupported_harmonics"], name + ".skip_unsupported_harmonics")
    if xx.model != "SR830" or xy.model != "SR865A":
        raise cfg.ConfigError("6221 acquisition currently requires SR830 XX and SR865A XY")
    if not xy.sr865a.current_status_supported:
        raise cfg.ConfigError("External SR865A acquisition requires known current-status query support")
    for role in (xx, xy):
        if role.reference_source.value != "external_ttl" or role.sine_output_connected:
            raise cfg.ConfigError("Both external detectors require external TTL and disconnected SINE OUT")
    return ExternalLockinAcquisitionConfig(selected["xx"], selected["xy"], settle, interval, policy, skip)


class _ExternalSr830(Sr830):
    """Explicit external topology; the legacy SR830 adapter remains unchanged."""
    def set_fixed_setting(self, field, code):
        if field in {"reference_mode", "reference_slope"}:
            allowed = (0,) if field == "reference_mode" else (1, 2)
            if type(code) is not int or code not in allowed:
                raise ValueError("External SR830 requires external TTL reference")
            self._resource.write(f"{'FMOD' if field == 'reference_mode' else 'RSLP'} {code}")
            return
        return super().set_fixed_setting(field, code)

    def set_sine_output(self, *args, **kwargs):
        raise Sr830Error("External detector cannot write source amplitude")

    def set_minimum_sine_output(self, *args, **kwargs):
        raise Sr830Error("External detector cannot write source amplitude")

    def set_internal_reference_frequency(self, *args, **kwargs):
        raise Sr830Error("External detector cannot write source frequency")


class _ExternalSr865a(Sr865aReceiver):
    source_active = False

    def set_harmonic(self, harmonic):
        # With the external source verified OFF, reference frequency may be zero.
        # Only h1 parking is allowed then; no frequency-dependent measurement.
        if not self.source_active and harmonic == 1:
            self._authorized()
            if self.read_harmonic() != 1:
                self._call(self._driver._write_verified_code, "HARM", 1)
            return 1
        if not self.source_active:
            raise Sr830Error("Cannot select measurement harmonics without the source reference")
        return super().set_harmonic(harmonic)


class _ExternalHarmonicSession(HarmonicSensitivitySession):
    source_transition_pending = False
    source_transition_status = None

    def open_transition_window(self, xx, xy, record):
        if not self.source_transition_pending:
            return super().open_transition_window(xx, xy, record)
        changes = list(self.settling.pending)
        if not changes:
            return None
        evidence = self.source_transition_status()
        event = {"changes": changes, "status": evidence["roles"],
                 "formal_sample": False, "external_source_transition": True}
        record.setdefault("settling_transition_windows", []).append(event)
        self.settling.window_generation = len(self.settling.changes)
        return event

    def diagnose_overload(self, xx, xy, status, settle_s, record):
        # The original optional diagnostic reads XX SINE OUT as the excitation.
        # In external mode preserve the formal evidence without that false claim
        # or any extra abort-time setting changes.
        record.setdefault("pre_abort_diagnostics", []).append({
            "captured_at_utc": utc_now(), "status": deepcopy(status),
            "excitation_source": "keithley6221_ac", "valid_for_analysis": False,
            "additional_instrument_commands": False})


class ExternalLockinPointSession(LockinPointSession):
    def __init__(self, config_path, config, event, *, manager_factory=None,
                 authorize_writes=False, monotonic=None, sleep=None, **kwargs):
        self.config, self.event = config, event
        self.mode = "detector"
        acquisition = config.lockin_acquisition
        slowest = max(config.lockin_xx.time_constant_s, config.lockin_xy.time_constant_s)
        self.args = SimpleNamespace(settle_s=slowest * acquisition.settle_time_constants,
                                    sample_interval_s=slowest * acquisition.sample_interval_time_constants)
        self.settings = {"config": config, "config_path": Path(config_path).resolve(),
                         "xx_address": config.lockin_xx.address, "xy_address": config.lockin_xy.address,
                         "timeout_ms": config.visa.timeout_ms, "authorize_writes": authorize_writes is True}
        daily._validate_distinct_addresses(config.lockin_xx.address, config.lockin_xy.address)
        for address in (config.lockin_xx.address, config.lockin_xy.address):
            if not address.strip() or "CHANGE_ME" in address:
                raise ValueError("External detectors require explicit station-local addresses")
        self.harmonics_by_role = {"xx": acquisition.xx_harmonics, "xy": acquisition.xy_harmonics}
        self.harmonics = tuple(sorted(set(acquisition.xx_harmonics + acquisition.xy_harmonics)))
        self.skip_unsupported = acquisition.skip_unsupported_harmonics
        self.skipped_by_frequency = {}
        for frequency in config.excitation_6221.frequency_points_hz:
            _, skipped = daily._harmonics_for_frequency(frequency, self.harmonics,
                skip_unsupported=self.skip_unsupported, config=config, harmonics_by_role=self.harmonics_by_role)
            self.skipped_by_frequency[str(frequency)] = skipped
        self.grid = ()
        self.base_sleep = sleep or daily.time.sleep
        self.monitor_wait_context = None
        self.clock = {"monotonic": monotonic, "sleep": self._wait}
        self.manager_factory = manager_factory
        self.context = self.pair = self.preflight = self.harmonic_control = None
        self.fixed_setup = self.frequency_setup = self.sensitivity_setup = self.reserve_setup = None
        self.point_record = self.range_profile = self.range_profile_record = None
        self.range_profile_failed = self.writes_started = False
        self.sample_count = 0
        self.target_frequency_hz = config.excitation_6221.frequency_points_hz[0]
        self.point_harmonics = self.harmonics
        self.source_active = False
        self.source_off_proof = None
        self.detectors = ()
        self.source_baseline = {}

    def _status(self, *, allow_unlock=False, transition=False, acquisition=False, initial_baseline=False):
        if initial_baseline and (self.source_active or self.writes_started):
            raise Sr830Error("Initial detector baseline cannot be reused after setup")
        result, problems = {}, []
        for role, instrument in zip(("lockin_xx", "lockin_xy"), self.pair):
            status, error = instrument.read_status_latches()
            result[role] = {"lia_status": asdict(status), "error_status": error}
            if error != 0:
                problems.append(role + " instrument error or unknown status")
            if daily.model_name(status) == "SR865A":
                problems.extend(role + " " + p for p in native_status_problems(status))
                if initial_baseline and status.native_status.input_overload is not False:
                    problems.append(role + " current input overload or unknown input state")
                if initial_baseline and status.native_status.output_scale_overload is not False:
                    problems.append(role + " current scale overload or unknown scale state")
            elif status.raw < 0 or status.raw & ~0x7f:
                problems.append(role + " unknown native status")
            if status.reference_unlocked and not allow_unlock:
                problems.append(role + " reference unlocked")
            if not (transition or initial_baseline) and status.any_overload:
                overloads = []
                if status.input_or_reserve_overload:
                    overloads.append(role + " input/reserve overload")
                if status.filter_overload:
                    overloads.append(role + " filter overload")
                if acquisition:
                    blocking, _ = self.harmonic_control.overload.partition(overloads)
                    problems.extend(blocking)
                else:
                    problems.extend(overloads)
                if status.output_overload and (role == "lockin_xy" or not acquisition):
                    problems.append(role + " output overload")
            if not (transition or initial_baseline) and (status.time_constant_changed or status.frequency_range_changed):
                problems.append(role + " unexpected detector setting change")
        evidence = {"captured_at_utc": utc_now(), "source_inactive_or_transition": allow_unlock,
                    "initial_latch_baseline": initial_baseline,
                    "formal_sample": False, "roles": result, "problems": problems}
        self.event("external_lockin_status", evidence)
        if problems:
            error = Sr830Error("External detector status rejected: " + "; ".join(problems))
            error.status_evidence = deepcopy(evidence)
            raise error
        return evidence

    def _settings(self, instrument, role):
        record = {}
        try:
            record["identity"] = instrument.query_identity()
            identity_fields = tuple(part.strip() for part in record["identity"].split(","))
            expected_model = "SR830" if role == "lockin_xx" else "SR865A"
            if (len(identity_fields) != 4 or identity_fields[:2] != ("Stanford_Research_Systems", expected_model)
                    or not identity_fields[2] or not identity_fields[3]):
                raise Sr830Error(role + " unexpected physical identity")
            for field, method in (("harmonic", "read_harmonic"), ("sensitivity", "read_sensitivity"),
                                  ("reserve_mode", "read_reserve_mode"), ("sine_output_v", "read_sine_output"),
                                  ("phase_shift_deg", "read_phase_shift")):
                record[field] = getattr(instrument, method)()
            for field in ("reference_mode", "reference_slope", *daily._SWEEP_FIXED_SETTING_FIELDS):
                record[field] = instrument.read_fixed_setting(field)
            if role == "lockin_xy":
                record["native_settings"] = asdict(instrument._driver.read_settings())
        except BaseException as exc:
            record["error"] = str(exc) or type(exc).__name__
            raise
        finally:
            self.event("external_lockin_settings", {"role": role, "captured_at_utc": utc_now(), "settings": record})
        return record

    def open(self, *, recovery=False):
        if self.settings["authorize_writes"] is not True:
            raise AuthorizationRequired("External detector access requires explicit acquisition authorization")
        stack = ExitStack()
        self.context = stack
        manager = (self.manager_factory or daily._load_resource_manager_factory())()
        stack.callback(manager.close)
        resources = []
        for address in (self.settings["xx_address"], self.settings["xy_address"]):
            resource = daily._open_resource(manager, address, self.settings["timeout_ms"])
            stack.callback(resource.close)
            resources.append(resource)
        self.detectors = (_ExternalSr830(resources[0], LockinRole.XX),
                          _ExternalSr865a(resources[1], config=self.config.lockin_xy, authorize_writes=True))
        self.pair = tuple(_AuditedInstrument(i, self.event) for i in self.detectors)
        self.preflight = {role: self._settings(i, role) for role, i in
                          zip(("lockin_xx", "lockin_xy"), self.pair)}
        if self.preflight["lockin_xx"]["identity"] == self.preflight["lockin_xy"]["identity"]:
            raise Sr830Error("Duplicate detector identity")
        self.source_baseline = {role: {k: v for k, v in record.items() if k in {"sine_output_v", "phase_shift_deg"}}
                                for role, record in self.preflight.items()}
        # These latches precede our configuration and settling window. Retain
        # them as history, never as formal evidence. SR865A current input status
        # remains blocking; SR830 latches are checked afresh after settling.
        self._status(allow_unlock=True, initial_baseline=True)
        return {"roles": deepcopy(self.preflight), "reference_policy": "external_6221_ttl",
                "source_inactive": True, "recovery": recovery}

    def configure(self, *, recovery=False, cleanup_only=False):
        self.writes_started = True
        self.fixed_setup = {"verified": False, "roles": {}}
        self.reserve_setup = daily._new_sweep_reserve_setup(self.config.lockin_xx, self.config.lockin_xy,
            original_xx_reserve_mode=self.preflight["lockin_xx"]["reserve_mode"],
            original_xy_reserve_mode=self.preflight["lockin_xy"]["reserve_mode"])
        self.sensitivity_setup = daily._new_sweep_sensitivity_setup(
            lockin_xx_config=self.config.lockin_xx, lockin_xy_config=self.config.lockin_xy,
            original_xx_sensitivity=self.preflight["lockin_xx"]["sensitivity"],
            original_xy_sensitivity=self.preflight["lockin_xy"]["sensitivity"])
        for role, instrument in zip(("lockin_xx", "lockin_xy"), self.pair):
            target = getattr(self.config, role)
            edge = 1 if target.external_reference_edge.value == "rising" else 2
            if role == "lockin_xy":
                self.fixed_setup["receiver_setup"] = instrument.configure_receiver()
            else:
                for field, code in (("reference_slope", edge), ("reference_mode", 0)):
                    self._set_verified(instrument, field, code)
            expected = daily._setting_codes(target)
            for field in daily._SWEEP_FIXED_SETTING_FIELDS:
                self._set_verified(instrument, field, getattr(expected, field))
            sensitivity = daily.sensitivity_code_for(target, target.sensitivity_full_scale_v)
            if instrument.read_sensitivity() != sensitivity:
                self.sensitivity_setup["ranges"][role]["write_attempted"] = True
                instrument.set_sensitivity(sensitivity)
            if instrument.read_sensitivity() != sensitivity:
                raise Sr830Error(role + " sensitivity was not confirmed")
            self.sensitivity_setup["ranges"][role]["current_sensitivity_code"] = sensitivity
            reserve = daily.reserve_code_for(target)
            if reserve is not None and instrument.read_reserve_mode() != reserve:
                self.reserve_setup["roles"][role]["write_attempted"] = True
                instrument.set_reserve_mode(reserve)
            if instrument.read_reserve_mode() != reserve:
                raise Sr830Error(role + " reserve was not confirmed")
            if instrument.read_harmonic() != 1:
                instrument.set_harmonic(1)
            if instrument.read_harmonic() != 1:
                raise Sr830Error(role + " first harmonic was not confirmed")
        self._status(allow_unlock=True, transition=True)
        self.fixed_setup["verified"] = True
        self.frequency_setup = {"source_owned_by": "excitation", "source_write_attempted": False}
        self.harmonic_control = _ExternalHarmonicSession.create(self.config.lockin_xx, self.config.lockin_xy,
            self.sensitivity_setup, self.reserve_setup, self.harmonics_by_role,
            overload_policy=self.config.lockin_acquisition.overload_policy, **self.clock)
        self.harmonic_control.source_transition_status = lambda: self._status(allow_unlock=True, transition=True)
        self.pair = self.harmonic_control.track_pair(*self.pair)
        # These source-inactive latches are already preserved above. A fresh
        # controlled transition and deadline begin when the source actually runs.
        self.harmonic_control.settling.window_generation = len(self.harmonic_control.settling.changes)
        self.event("lockin_configured", {"fixed": self.fixed_setup, "sensitivity": self.sensitivity_setup,
                                         "reserve": self.reserve_setup, "frequency": self.frequency_setup})

    @staticmethod
    def _set_verified(instrument, field, code):
        if instrument.read_fixed_setting(field) != code:
            instrument.set_fixed_setting(field, code)
        if instrument.read_fixed_setting(field) != code:
            raise Sr830Error(f"External detector {field} readback mismatch")

    def prepare_source_change(self, frequency_hz):
        daily._harmonics_for_frequency(frequency_hz, self.harmonics,
            skip_unsupported=self.skip_unsupported, config=self.config, harmonics_by_role=self.harmonics_by_role)
        if self.source_active and frequency_hz != self.target_frequency_hz:
            self.harmonic_control.enter(*self.pair, harmonic=1, settle_s=self.args.settle_s,
                                         record=self.point_record)

    def source_changed(self, current_rms_a, frequency_hz, index=0, source_record=None):
        self._require_range_profile_ready()
        if isinstance(current_rms_a, bool) or not math.isfinite(current_rms_a) or current_rms_a <= 0:
            raise ValueError("External current RMS must be positive and finite")
        changed = (not self.source_active or self.point_record is None
                   or current_rms_a != self.point_record["excitation_current_rms_a"]
                   or frequency_hz != self.target_frequency_hz)
        self.target_frequency_hz = frequency_hz
        self.point_harmonics, skipped = daily._harmonics_for_frequency(frequency_hz, self.harmonics,
            skip_unsupported=self.skip_unsupported, config=self.config, harmonics_by_role=self.harmonics_by_role)
        self.point_record = {"point_index": index, "target_frequency_hz": frequency_hz,
            "excitation_current_rms_a": current_rms_a, "excitation_frequency_hz": frequency_hz,
            "excitation_source": deepcopy(source_record), "skipped_harmonics": skipped,
            "samples": [], "harmonic_transition_status": []}
        if self.range_profile is not None:
            self.point_record["lockin_range_rule"] = self.range_context()
        self.source_active = True
        self.source_off_proof = None
        self.detectors[1].source_active = True
        if changed:
            self.harmonic_control.settling.changed("excitation.source_setting")
            self.harmonic_control.source_transition_pending = True
            self.point_record["source_transition_status"] = self._status(allow_unlock=True, transition=True)
            self.harmonic_control.settling.window_generation = len(self.harmonic_control.settling.changes)
        self.sample_count = 0
        self.event("external_excitation_applied_to_detectors", deepcopy(self.point_record))

    def qualify(self):
        if not self.source_active or self.point_record is None:
            raise Sr830Error("External source has not been established")
        prior_recheck = self.point_record.get("output_latch_requalification")
        if prior_recheck is not None and prior_recheck.get("completed") is not True:
            raise Sr830Error("Recovered output latch requalification failed or incomplete; point cannot be reused")
        prior_reference = self.point_record.get("reference_requalification")
        if (prior_reference is not None and prior_reference.get("frequency_pending_observed")
                and prior_reference.get("completed") is not True):
            raise Sr830Error("External reference frequency qualification failed or incomplete; point cannot be reused")
        controlled_transition = self.harmonic_control.source_transition_pending
        # Choose final detector settings before the shared deadline. Controlled
        # source-change unlock is permitted only in this nonformal window.
        self.harmonic_control.enter(*self.pair,
            harmonic=self.harmonic_control.initial_harmonics(self.target_frequency_hz),
            settle_s=self.args.settle_s, record=self.point_record)
        if self.harmonic_control.source_transition_pending:
            self._requalify_reference()
        self.harmonic_control.ensure_settled(*self.pair, self.args.settle_s, self.point_record)
        self.harmonic_control.source_transition_pending = False
        self._read_coordinates()
        try:
            self._status(acquisition=True)
        except Sr830Error as exc:
            evidence = getattr(exc, "status_evidence", None)
            if (not controlled_transition or "output_latch_requalification" in self.point_record
                    or not self._recovered_output_latch_candidate(evidence)):
                raise
            self._requalify_recovered_output_latch(evidence)
        self.event("lockin_point_qualification", deepcopy(self.point_record))

    def _recovered_output_latch_candidate(self, evidence):
        """Only a known recovered SR865A output latch may open one new window."""
        if (self.config.lockin_acquisition.overload_policy != "record_continue"
                or not isinstance(evidence, dict)
                or evidence.get("problems") != ["lockin_xy output overload"]):
            return False
        roles = evidence["roles"]
        xx, xy = roles["lockin_xx"], roles["lockin_xy"]
        native = xy["lia_status"].get("native_status")
        if (xx["error_status"] != 0 or xx["lia_status"]["raw"] != 0
                or xy["error_status"] != 0 or not isinstance(native, dict)):
            return False
        latch = native["lia_status_raw"]
        # CH1/CH2 and the four data-channel scale bits are the only eligible
        # history. In particular, do not borrow record_continue's input/filter
        # exception or treat a current overload, unlock or unknown as recovered.
        return (type(latch) is int and latch > 0 and not latch & ~0xF03
                and native["current_status_raw"] == 0
                and native["locked"] is True
                and native["input_overload"] is False
                and native["output_scale_overload"] is False
                and native["instrument_error"] is False
                and native["error_status_raw"] == 0
                and native["standard_event_status_raw"] == 0
                and native["unknown_status_bits"] == 0
                and native["consumed_status_latches"] is True)

    def _requalify_recovered_output_latch(self, evidence):
        """One fresh full settled window, without reopening a clearing loop."""
        settling = self.harmonic_control.settling
        proof = {"policy": "external-recovered-output-latch-once-v1",
                 "formal_sample": False, "overload_policy": "record_continue",
                 "candidate": deepcopy(evidence), "completed": False,
                 "started_at_utc": utc_now(), "started_monotonic_s": settling.monotonic(),
                 "required_settle_s": self.args.settle_s}
        # Consume the point's one-shot budget before any wait or further I/O.
        self.point_record["output_latch_requalification"] = proof
        wait_start = len(self.point_record.get("settling_waits", []))
        try:
            settling.changed("external_output_latch.recovered")
            # The candidate read already consumed and archived the old latch.
            # Do not let open_transition_window consume new faults in this window.
            settling.window_generation = len(settling.changes)
            self.harmonic_control.ensure_settled(*self.pair, self.args.settle_s, self.point_record)
            self._read_coordinates()
            proof["recheck"] = self._status()
            proof["completed"] = True
        except BaseException as exc:
            proof["error"] = str(exc) or type(exc).__name__
            if getattr(exc, "status_evidence", None) is not None:
                proof["recheck"] = deepcopy(exc.status_evidence)
            raise
        finally:
            proof["settling_waits"] = deepcopy(self.point_record.get("settling_waits", [])[wait_start:])
            proof["finished_at_utc"] = utc_now()
            proof["finished_monotonic_s"] = settling.monotonic()
            try:
                self.event("external_output_latch_requalification", deepcopy(proof))
            except BaseException as exc:
                proof["completed"] = False
                proof["audit_error"] = str(exc) or type(exc).__name__
                raise

    def _requalify_reference(self):
        """Bound the deliberate source transition, then begin a clean window.

        SR865A CUROVLDSTAT is current; LIAS is read-to-clear history. SR830
        exposes only the latch here, so it must produce a fresh clear read.
        Neither distinction changes strict settled/formal status validation.
        """
        settling = self.harmonic_control.settling
        started = settling.monotonic()
        history = [self.point_record['source_transition_status']['roles']]
        history += [w['status'] for w in self.point_record.get('settling_transition_windows', [])]
        observed = any(r['lia_status']['reference_unlocked'] for h in history for r in h.values())
        proof = {'policy': 'external-reference-relock-v2', 'formal_sample': False,
                 'started_monotonic_s': started, 'timeout_s': 30.0, 'polls': [],
                 'transition_unlock_observed': observed, 'completed': False,
                 'frequency_pending_observed': False, 'frequency_checks': []}
        self.point_record['reference_requalification'] = proof
        try:
            for poll in range(301):
                evidence = self._status(allow_unlock=True, transition=True)
                proof['polls'].append(evidence)
                roles = evidence['roles']
                proof['transition_unlock_observed'] |= any(
                    r['lia_status']['reference_unlocked'] for r in roles.values())
                if proof['frequency_pending_observed']:
                    self._verify_frequency_pending_status(evidence)
                    self.harmonic_control.verify(*self.pair,
                        self.harmonic_control.current_harmonics, self.point_record)
                # Unknown native current/error bits have already failed in _status.
                locked = (not roles['lockin_xx']['lia_status']['reference_unlocked']
                          and roles['lockin_xy']['lia_status']['native_status']['locked'] is True)
                if locked and settling.monotonic() <= started + proof['timeout_s']:
                    try:
                        self._read_coordinates(
                            capture_frequency_evidence=proof['frequency_pending_observed'])
                    except _ExternalReferenceFrequencyMismatch as exc:
                        proof['frequency_pending_observed'] = True
                        proof['frequency_checks'].append(deepcopy(exc.frequency_evidence))
                        self._verify_frequency_pending_status(evidence)
                        self.harmonic_control.verify(*self.pair,
                            self.harmonic_control.current_harmonics, self.point_record)
                    else:
                        if proof['frequency_pending_observed']:
                            proof['frequency_checks'].append(
                                deepcopy(self.point_record['frequency_readback_evidence']))
                            if settling.monotonic() > started + proof['timeout_s']:
                                raise Sr830Error('External reference relock timeout; no formal acquisition')
                        if proof['transition_unlock_observed'] or proof['frequency_pending_observed']:
                            settling.changed('external_reference.frequency_confirmed'
                                if proof['frequency_pending_observed'] else 'external_reference.relocked')
                        # The latches above are the final transitional baseline. Do
                        # not clear them again after the fresh settling window starts.
                        settling.window_generation = len(settling.changes)
                        self.harmonic_control.source_transition_pending = False
                        proof.update(completed=True, confirmed_monotonic_s=settling.monotonic())
                        return
                remaining = started + proof['timeout_s'] - settling.monotonic()
                if remaining <= 0 or poll == 300:
                    raise Sr830Error('External reference relock timeout; no formal acquisition')
                self._wait(min(0.1, remaining))
        except BaseException as exc:
            proof['error'] = str(exc) or type(exc).__name__
            raise
        finally:
            try:
                self.event('external_reference_requalification', deepcopy(proof))
            except BaseException as exc:
                if proof['frequency_pending_observed']:
                    proof['completed'] = False
                    proof['audit_error'] = str(exc) or type(exc).__name__
                raise

    @staticmethod
    def _verify_frequency_pending_status(evidence):
        """Reobserve only a clean current state, with optional old XY unlock."""
        xx, xy = (evidence['roles'][role] for role in ('lockin_xx', 'lockin_xy'))
        native = xy['lia_status']['native_status']
        if (evidence['problems'] or xx['error_status'] != 0 or xx['lia_status']['raw'] != 0
                or xy['error_status'] != 0 or native['current_status_raw'] != 0
                or native['lia_status_raw'] not in (0, 8)
                or native['error_status_raw'] != 0 or native['standard_event_status_raw'] != 0
                or native['unknown_status_bits'] != 0 or native['locked'] is not True
                or native['consumed_status_latches'] is not True):
            raise Sr830Error("External reference frequency recheck blocked by unsafe/unknown detector status")

    def _read_coordinates(self, *, verify_companion=True, capture_frequency_evidence=False):
        if not self.source_active:
            raise Sr830Error("Cannot acquire without established external source")
        frequencies, observations = {}, {}
        for role, instrument in zip(("lockin_xx", "lockin_xy"), self.pair):
            began = utc_now()
            target = getattr(self.config, role)
            expected_mode = 0 if role == "lockin_xx" else 1
            edge = 1 if target.external_reference_edge.value == "rising" else 2
            if instrument.read_fixed_setting("reference_mode") != expected_mode or instrument.read_fixed_setting("reference_slope") != edge:
                raise Sr830Error(role + " external reference setting changed")
            frequencies[role] = instrument.read_reference_frequency()
            observations[role] = {'started_at_utc': began, 'completed_at_utc': utc_now(),
                'frequency_hz': frequencies[role]}
        # Only a valid coordinate mismatch can remain pending during controlled
        # relock. Invalid values and transport/setting faults still fail at once.
        daily._validate_frequency_observations(self.target_frequency_hz, *frequencies.values())
        evidence = {'captured_at_utc': utc_now(), 'requested_frequency_hz': self.target_frequency_hz,
                    'frequency_readback_hz': deepcopy(frequencies), 'observations': observations,
                    'matched': False}
        try:
            daily._verify_frequency_readbacks(self.target_frequency_hz, *frequencies.values(),
                rel_tolerance=daily.SWEEP_FREQUENCY_REL_TOLERANCE,
                absolute_tolerance_hz=daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ)
        except Sr830Error as exc:
            # Read the native audit only on a mismatch; normal coordinate reads
            # must not copy the driver's growing history on every point.
            observations['lockin_xy']['raw'] = [asdict(r)
                for r in self.detectors[1]._driver.audit[-2:]]
            evidence['error'] = str(exc)
            mismatch = _ExternalReferenceFrequencyMismatch(str(exc), evidence)
            try:
                self.event('external_reference_frequency_mismatch', deepcopy(evidence))
            except BaseException as audit_exc:
                audit_exc.frequency_audit_failed = True
                audit_exc.frequency_evidence = mismatch.frequency_evidence
                audit_exc.evidence, audit_exc.raw = mismatch.evidence, mismatch.raw
                raise
            raise mismatch from exc
        evidence['matched'] = True
        if capture_frequency_evidence:
            observations['lockin_xy']['raw'] = [asdict(r)
                for r in self.detectors[1]._driver.audit[-2:]]
        self.point_harmonics, skipped = daily._harmonics_for_frequency(
            max(self.target_frequency_hz, *frequencies.values()), self.harmonics,
            skip_unsupported=self.skip_unsupported, config=self.config,
            harmonics_by_role=self.harmonics_by_role)
        self.point_record["skipped_harmonics"] = skipped
        self.point_record["frequency_readback_hz"] = frequencies
        if capture_frequency_evidence:
            self.point_record["frequency_readback_evidence"] = evidence
        self.point_record["actual_frequency_hz"] = frequencies["lockin_xx"]
        return {}  # Source module owns the two excitation coordinates.

    def _read_pre_capture_coordinates(self):
        """Reobserve counters before capture; never replay a formal measurement."""
        point, ordinal = self.point_record, self.sample_count
        prior = next((p for p in point.get('pre_capture_frequency_rechecks', ())
                      if p['sample_index'] == ordinal), None)
        if prior is not None and prior.get('completed') is not True:
            raise Sr830Error('Pre-capture frequency recheck failed or incomplete; sample cannot be reused')
        try:
            return self._read_coordinates()
        except _ExternalReferenceFrequencyMismatch as exc:
            if prior is not None:
                raise
            initial = exc.frequency_evidence
            last_mismatch = exc
        except BaseException as exc:
            if getattr(exc, 'frequency_audit_failed', False):
                point.setdefault('pre_capture_frequency_rechecks', []).append({
                    'policy': 'external-pre-capture-frequency-recheck-v1',
                    'formal_sample': False, 'sample_index': ordinal, 'completed': False,
                    'checks': [deepcopy(exc.frequency_evidence)],
                    'error': str(exc) or type(exc).__name__,
                    'audit_error': str(exc) or type(exc).__name__, 'failed_at_utc': utc_now()})
            raise

        settling = self.harmonic_control.settling
        context = {'point_index': point['point_index'],
                   'current_rms_a': point['excitation_current_rms_a'],
                   'frequency_hz': self.target_frequency_hz,
                   'source': deepcopy(point['excitation_source']),
                   'harmonics': dict(self.harmonic_control.current_harmonics),
                   'setting_generation': len(settling.changes)}
        proof = {'policy': 'external-pre-capture-frequency-recheck-v1',
                 'formal_sample': False, 'sample_index': ordinal,
                 'maximum_additional_observations': 2, 'retry_wait_s': 1.0,
                 'checks': [deepcopy(initial)], 'status_checks': [], 'waits': [],
                 'completed': False, 'source_context': deepcopy(context),
                 'started_at_utc': utc_now(), 'started_monotonic_s': settling.monotonic()}
        point.setdefault('pre_capture_frequency_rechecks', []).append(proof)
        setting_start = len(point.get('harmonic_setting_checks', ()))

        def verify_context():
            if (self.point_record is not point or self.sample_count != ordinal or not self.source_active
                    or self.target_frequency_hz != context['frequency_hz']
                    or point['point_index'] != context['point_index']
                    or point['excitation_current_rms_a'] != context['current_rms_a']
                    or point['excitation_source'] != context['source']
                    or self.harmonic_control.current_harmonics != context['harmonics']
                    or len(settling.changes) != context['setting_generation']):
                raise Sr830Error('Source or point changed during pre-capture frequency recheck')

        try:
            for _ in range(proof['maximum_additional_observations']):
                verify_context()
                proof['status_checks'].append(self._status())
                self.harmonic_control.verify(*self.pair, context['harmonics'], point)
                wait = {'requested_s': 1.0, 'completed': False,
                        'started_monotonic_s': settling.monotonic()}
                proof['waits'].append(wait)
                self._wait(1.0)
                wait.update(completed=True, finished_monotonic_s=settling.monotonic())
                verify_context()
                proof['status_checks'].append(self._status())
                self.harmonic_control.verify(*self.pair, context['harmonics'], point)
                try:
                    result = self._read_coordinates(capture_frequency_evidence=True)
                except _ExternalReferenceFrequencyMismatch as exc:
                    last_mismatch = exc
                    proof['checks'].append(deepcopy(exc.frequency_evidence))
                else:
                    proof['checks'].append(deepcopy(point['frequency_readback_evidence']))
                    verify_context()
                    proof['completed'] = True
                    return result
            raise last_mismatch
        except BaseException as exc:
            proof['error'] = str(exc) or type(exc).__name__
            if getattr(exc, 'status_evidence', None) is not None:
                proof['status_checks'].append(deepcopy(exc.status_evidence))
            audit = daily._native_error_audit(exc)
            if audit:
                proof.update(audit)
            raise
        finally:
            proof['setting_checks'] = deepcopy(point.get('harmonic_setting_checks', ())[setting_start:])
            proof['finished_at_utc'] = utc_now()
            proof['finished_monotonic_s'] = settling.monotonic()
            try:
                self.event('external_pre_capture_frequency_recheck', deepcopy(proof))
            except BaseException as exc:
                proof['completed'] = False
                proof['audit_error'] = str(exc) or type(exc).__name__
                raise

    def _capture_frequency(self, actual):
        return self.target_frequency_hz

    def _formal_pair_recorded(self, payload):
        payload["excitation_source"] = deepcopy(self.point_record["excitation_source"])
        payload["sweep_point"].update({k: self.point_record[k] for k in
            ("excitation_current_rms_a", "excitation_frequency_hz")})
        super()._formal_pair_recorded(payload)

    def disturbed(self, module):
        if self.harmonic_control is not None:
            self.harmonic_control.settling.changed(module + ".axis_setting")
            if self.source_active:
                self.harmonic_control.open_transition_window(*self.pair, self.point_record)
            else:
                self.harmonic_control.settling.window_generation = len(self.harmonic_control.settling.changes)

    def source_stopped(self, proof):
        if not isinstance(proof, dict) or proof.get("verified") is not True or proof.get("output_off_confirmed") is not True:
            raise Sr830Error("Detector cleanup requires verified external source OFF evidence")
        self.source_off_proof = deepcopy(proof)
        self.source_active = False
        if len(self.detectors) > 1:
            self.detectors[1].source_active = False

    def cleanup(self):
        record = {"attempted": self.writes_started, "verified": False, "errors": [],
                  "source_output_owned": False, "source_off_proof": deepcopy(self.source_off_proof), "final": {}}
        if not self.writes_started:
            record["verified"] = True
            return record
        if self.source_active or self.source_off_proof is None:
            record["errors"].append("External source OFF was not verified; detector cleanup not certified")
            return record
        for role, instrument in zip(("lockin_xx", "lockin_xy"), self.pair):
            try:
                target = getattr(self.config, role)
                if instrument.read_harmonic() != 1:
                    instrument.set_harmonic(1)
                if instrument.read_harmonic() != 1:
                    raise Sr830Error("h1 cleanup readback mismatch")
                code = daily.sensitivity_code_for(target, target.sensitivity_full_scale_v)
                if instrument.read_sensitivity() != code:
                    instrument.set_sensitivity(code)
                reserve = daily.reserve_code_for(target)
                if reserve is not None and instrument.read_reserve_mode() != reserve:
                    instrument.set_reserve_mode(reserve)
                final = record["final"][role] = self._settings(instrument, role)
                expected = daily._setting_codes(target)
                for field in (*daily._SWEEP_FIXED_SETTING_FIELDS, "sensitivity"):
                    if final[field] != getattr(expected, field):
                        raise Sr830Error(role + " final " + field + " mismatch")
                edge = 1 if target.external_reference_edge.value == "rising" else 2
                if (final["reserve_mode"] != reserve
                        or final["reference_mode"] != (0 if role == "lockin_xx" else 1)
                        or final["reference_slope"] != edge):
                    raise Sr830Error(role + " final reference/reserve mismatch")
                for key, value in self.source_baseline[role].items():
                    if final[key] != value:
                        raise Sr830Error(role + " disconnected output/phase changed")
                if role == "lockin_xy":
                    instrument.read_receiver_invariants()
            except BaseException as exc:
                record["errors"].append(role + ": " + str(exc))
        try:
            record["transition_status"] = self._status(allow_unlock=True, transition=True)
            self._wait(self.args.settle_s)
            record["status"] = self._status(allow_unlock=True)
        except BaseException as exc:
            record["errors"].append(str(exc))
        record["verified"] = not record["errors"]
        self.event("external_lockin_cleanup", deepcopy(record))
        return record
