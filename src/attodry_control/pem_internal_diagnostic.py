"""Gated, noncoherent PEM/SR865A diagnostic; imports never open instruments."""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

from .pem_internal_config import load_diagnostic_config
from .pem_internal_analysis import summarize_capture


def utc():
    return datetime.now(UTC).isoformat()


def plain(value):
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (Path, datetime)):
        return str(value) if isinstance(value, Path) else value.isoformat()
    return value


def error_text(exc):
    return f"{type(exc).__name__}: {exc}"


def validate_station(config, station):
    """Review the entire referenced station and this deliberately separate mode."""
    config.validate()
    lockin = station.lockin
    if (station.reference_topology != "pem_xy_xx_sine" or lockin is None
            or lockin.lockin_xx.model != "SR830" or lockin.lockin_xy.model != "SR865A"):
        raise ValueError("Diagnostic requires the reviewed XX SR830 / XY SR865A SINE reference station")
    if station.optical is None or station.optical.pem is None or station.optical.pm is None:
        raise ValueError("Diagnostic requires NKT, PEM and power-meter configuration")
    if station.smu is not None or station.cryostat is not None:
        raise ValueError("Diagnostic station must select only optical and lockin axes")
    if lockin.cleanup_source_voltage_v != 0.004:
        raise ValueError("This isolated-XX diagnostic requires the approved 4 mV protection setting")
    if max(config.optical_point_indices) >= len(station.optical.points):
        raise ValueError("optical_point_indices exceeds the station's ordered optical grid")
    from .lockin_backend import capabilities_for
    for frequency in config.internal_frequencies_hz:
        if not lockin.reference_min_hz <= frequency <= lockin.reference_max_hz:
            raise ValueError("Internal diagnostic frequency left the station's approved interval")
        for harmonic in config.harmonics:
            capabilities_for("SR865A").validate_detection(frequency, harmonic)
        capabilities_for("SR830").validate_detection(frequency, 1)
    for harmonic in config.harmonics:
        capabilities_for("SR865A").validate_detection(lockin.reference_max_hz, harmonic)
    for tau in config.time_constants_s:
        minimum_rate = max(config.minimum_capture_rate_hz, 4 / (2 * math.pi * tau))
        if config.capture_duration_s * minimum_rate * 8 > 4096 * 1024:
            raise ValueError("Diagnostic duration/rate cannot fit the SR865A 4 MiB XY buffer")
    return station


def load_reviewed(config):
    from .combination_hardware import load_hardware_combination
    return validate_station(config, load_hardware_combination(config.station_path))


def _manager(config):
    import pyvisa
    return pyvisa.ResourceManager() if config.backend == "default" else pyvisa.ResourceManager(config.backend)


def _native(settings):
    value = plain(settings.native_settings)
    value.pop("raw", None)
    return value


def _same(actual, expected):
    if isinstance(expected, float):
        return isinstance(actual, (int, float)) and math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6)
    return actual == expected


def _source_guard(xx, xy, lockin, saved_xy_source, saved_xx_identity=None):
    if saved_xx_identity is not None and xx.read_identity() != saved_xx_identity:
        raise ValueError("XX identity changed after takeover")
    state = xx.read_source_state()
    if (state["source_voltage_v"] > lockin.cleanup_source_voltage_v + 1e-9
            or state["dc_offset_v"] != 0 or state["dc_mode"] != lockin.source.dc_mode):
        raise ValueError("XX protection setting changed; sample isolation cannot be inferred from software")
    xy_state = xy.read_source_state()
    if any(not _same(xy_state.get(k), v) for k, v in saved_xy_source.items()):
        raise ValueError("XY reference SINE output/DC changed")
    return {"xx": state, "xy": xy_state}


def _status_guard(xy, lockin, *, intentional_configuration=False):
    status = xy.read_reference_status(current_status_supported=True)
    native = status.native_status
    known_boolean_fields = (status.locked, status.input_overload, status.output_scale_overload,
        native.reference_unlock_latched, native.input_overload_latched, native.output_scale_overload_latched,
        native.filter_fault_latched, native.configuration_changed_latched, native.power_on_latched)
    hard = (any(type(value) is not bool for value in known_boolean_fields)
            or status.instrument_error is not False or native.unknown_status_bits
            or native.power_on_latched is not False or native.filter_fault_latched is not False
            or (native.configuration_changed_latched is not False and not intentional_configuration))
    if hard:
        raise ValueError("SR865A instrument/settings/unknown status fault: " + json.dumps(plain(status)))
    reasons = []
    if status.locked is not True or native.reference_unlock_latched is not False:
        if lockin.reference_unlock_policy != "record_continue":
            raise ValueError("SR865A internal reference unlock; abort policy")
        reasons.append("reference_unlock_recorded")
    if (status.input_overload is not False or status.output_scale_overload is not False
            or native.input_overload_latched is not False or native.output_scale_overload_latched is not False):
        if lockin.overload_policy != "record_continue":
            raise ValueError("SR865A overload; abort policy")
        reasons.append("overload_recorded")
    return plain(status), reasons


def _xx_status_guard(xx, lockin):
    status = xx.read_reference_status(current_status_supported=False)
    lias, errors = status.native_status
    if status.instrument_error is not False or errors != 0 or lias is None or lias.raw & 0x80:
        raise ValueError("XX instrument/unknown status fault")
    if lias.any_overload and lockin.overload_policy != "record_continue":
        raise ValueError("XX overload; abort policy")
    if lias.reference_unlocked and lockin.reference_unlock_policy != "record_continue":
        raise ValueError("XX reference unlock; abort policy")
    # XX is an isolated protection/monitor role here, not a captured Hall channel.
    # Its RANGE latch can accompany XY's deliberately changed reference.
    return plain(status)


def _settings_guard(xy, expected, frequency):
    observed = xy.read_settings()
    values = _native(observed)
    for key, value in expected.items():
        if not _same(values.get(key), value):
            raise ValueError(f"SR865A unexpected settings change: {key}")
    actual = xy.read_internal_frequency()
    if not math.isclose(actual, frequency, rel_tol=1e-6, abs_tol=0.0001):
        raise ValueError("SR865A internal frequency changed during capture")
    # The adapter checks detection frequency against its internal oscillator,
    # never against the intentionally independent PEM frequency.
    sample = xy.read_sample(consume_status_latches=False, current_status_supported=True)
    if (sample.status.instrument_error is True or sample.status.native_status.unknown_status_bits
            or any(type(value) is not bool for value in (sample.status.locked,
                        sample.status.input_overload, sample.status.output_scale_overload))):
        raise ValueError("SR865A instantaneous unknown/instrument status fault")
    if not math.isclose(sample.reference_frequency_hz, actual, rel_tol=1e-6, abs_tol=0.0001):
        raise ValueError("SR865A internal-reference readback disagreement")
    return {"settings": plain(observed), "sample": plain(sample)}


def assert_no_active_run(database):
    """File-only check of older runs, in addition to the shared OS lease."""
    database = Path(database)
    if not database.exists():
        return
    from .combination_store import open_readonly
    with closing(open_readonly(database)) as connection:
        exists = connection.execute("SELECT name FROM sqlite_master WHERE name='combination_runs'").fetchone()
        if exists and connection.execute("SELECT run_id FROM combination_runs WHERE status='active' LIMIT 1").fetchone():
            raise ValueError("Station database still contains an active run; verify its owner before proceeding")


class Archive:
    def __init__(self, directory, config, station, *, simulated=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.result = {"schema_version": 1, "mode": "pem_internal_diagnostic",
            "status": "active", "stage": "preflight", "started_at_utc": utc(),
            "simulated": simulated, "formal_hall_eligible": False,
            "config": config.snapshot(), "station_snapshot": plain(station.snapshot),
            "captures": [], "frequency_observations": [], "error": None,
            "cleanup": {}, "audit_errors": []}
        for name, path in (("diagnostic-config.toml", config.config_path), ("station-config.toml", config.station_path),
                           ("lockin-safety.toml", Path(station.lockin.safety_path))):
            data = Path(path).read_bytes()
            expected = {"diagnostic-config.toml": config.config_sha256,
                        "station-config.toml": station.snapshot["config_sha256"],
                        "lockin-safety.toml": station.lockin.safety_sha256}[name]
            if expected and hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f"{name} changed before archival; no hardware connection")
            (self.directory / name).write_bytes(data)
            self.result.setdefault("file_sha256", {})[name] = hashlib.sha256(data).hexdigest()
        source_root = Path(__file__).resolve().parent
        sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(source_root.glob("*.py"))}
        (self.directory / "source-manifest.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
        self.result["source_manifest_sha256"] = hashlib.sha256((self.directory / "source-manifest.json").read_bytes()).hexdigest()
        self.save()

    def save(self):
        data = json.dumps(plain(self.result), ensure_ascii=False, allow_nan=False, indent=2)
        temporary = self.directory / "result.json.tmp"
        temporary.write_text(data, encoding="utf-8")
        # A file-only Windows monitor can briefly hold a handle without delete
        # sharing. Retry only that bounded file race; persistent audit failure
        # still propagates and stops acquisition.
        for attempt in range(4):
            try:
                temporary.replace(self.directory / "result.json")
                break
            except PermissionError as exc:
                if attempt == 3 or getattr(exc, "winerror", None) not in (5, 32):
                    raise
                time.sleep(0.05)

    def event(self, kind, evidence):
        payload = {"at_utc": utc(), "kind": kind, "evidence": plain(evidence)}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()

    def stage(self, value):
        self.result["stage"] = value
        self.save()

    def capture_file(self, row):
        data = row.get("capture", {})
        path = self.directory / (row["capture_id"] + ".csv")
        with path.open("x", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("sample_index", "elapsed_s", "x_v", "y_v", "r_v"))
            for index, values in enumerate(zip(data.get("t_s", ()), data.get("x_v", ()),
                                              data.get("y_v", ()), data.get("r_v", ()), strict=True)):
                writer.writerow((index, *values))
        row["raw_csv"] = path.name
        row["raw_csv_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def _frequency(state):
    value = state["pem"]["frequency_hz"]
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid prepared PEM frequency")
    return value


def run_diagnostic(config, station, directory, *, authorize_optical=False,
                   confirm_optical_route=False, manager_factory=_manager,
                   backend_factory=None, optical_factory=None,
                   clock=time.monotonic, sleep=time.sleep):
    """One station owner. All opened resources survive until bounded cleanup."""
    validate_station(config, station)
    if not (authorize_optical is True and confirm_optical_route is True
            and config.xx_excitation_disconnected is True and config.capture_buffer_available is True):
        raise ValueError("Real diagnostic requires optical authorization, route, physical XX isolation and available capture buffer")
    if station.optical.nkt.backend == "simulation":
        raise ValueError("Real diagnostic cannot use simulation optics; use simulate")
    if hashlib.sha256(station.path.read_bytes()).hexdigest() != station.snapshot["config_sha256"]:
        raise ValueError("Station configuration changed since offline validation")
    if hashlib.sha256(Path(station.lockin.safety_path).read_bytes()).hexdigest() != station.lockin.safety_sha256:
        raise ValueError("Lockin safety configuration changed since offline validation")
    # Dependencies must fail before instrument ownership or settings change.
    import numpy  # noqa: F401
    from .hardware_lease import station_hardware_lease
    from .lockin_backend import create_lockin_backend
    from .optical_points import OpticalPointSession
    from .sr865a import Sr865aCaptureError
    factory = backend_factory or create_lockin_backend
    optical_factory = optical_factory or OpticalPointSession
    lockin = station.lockin
    with station_hardware_lease(station.path):
        assert_no_active_run(station.database_path)
        archive = Archive(directory, config, station)
        manager = xx = xy = optical = None
        resources = []
        saved = saved_config = saved_frequency = saved_xy_source = None
        xy_touched = False
        saved_xx_identity = None
        cleanup = {"errors": [], "xx_protection_verified": False,
                   "xy_restored": False, "resources_closed": False, "verified": False}
        try:
            manager = manager_factory(lockin.visa)
            for role, role_config in (("lockin_xx", lockin.lockin_xx), ("lockin_xy", lockin.lockin_xy)):
                resource = manager.open_resource(role_config.address)
                resources.append((role, resource))
                resource.timeout = lockin.visa.timeout_ms
                resource.read_termination = "\n"
                resource.write_termination = "\n"
                backend = factory(model=role_config.model, role=role, resource=resource)
                if role == "lockin_xx":
                    xx = backend
                else:
                    xy = backend
                identity = backend.read_identity()
                if role == "lockin_xx":
                    saved_xx_identity = identity
            xx.set_source_amplitude(lockin.cleanup_source_voltage_v,
                minimum_v=lockin.minimum_source_voltage_v, maximum_v=lockin.maximum_source_voltage_v,
                expected_dc_mode=lockin.source.dc_mode, authorize_writes=True, protect=True)
            saved = _native(xy.read_settings())
            saved_frequency = xy.read_internal_frequency()
            xy_resource = resources[-1][1]
            if (not callable(getattr(xy_resource, "query_binary_values", None))
                    or str(getattr(xy_resource, "resource_name", "")).upper().startswith("ASRL")):
                raise ValueError("XY diagnostic requires binary-capable USB/GPIB/VXI-11 transport")
            if xy.read_capture_status() & 1:
                raise ValueError("An active SR865A capture belongs to another owner; no XY takeover")
            if (saved["reference_source"] != "external" or saved["filter_slope_db_oct"] != 24
                    or saved["external_reference_edge"] not in ("rising", "falling")
                    or saved["advanced_filter"] or saved["synchronous_filter"]
                    or saved["input_mode"] != "a_minus_b" or saved["input_coupling"] != "ac"):
                raise ValueError("Baseline must be external reference with A-B/AC/ordinary 24 dB RC before diagnostic takeover")
            saved_config = replace(lockin.lockin_xy, **{key: saved[key] for key in
                ("time_constant_s", "sensitivity_full_scale_v", "input_range_v_peak", "phase_shift_deg", "shield_grounding")})
            saved_xy_source = xy.read_source_state()
            output = lockin.reference_output
            if (output is None or not _same(saved_xy_source["source_voltage_v"], output.amplitude_v_rms)
                    or saved_xy_source["dc_mode"] != output.dc_mode
                    or saved_xy_source["dc_offset_v"] != output.dc_offset_v):
                raise ValueError("XY SINE reference output does not match reviewed station contract")
            _source_guard(xx, xy, lockin, saved_xy_source, saved_xx_identity)
            initial_status, initial_exclusions = _status_guard(xy, lockin)
            archive.result["baseline"] = {"xy": saved, "stored_internal_frequency_hz": saved_frequency,
                "xy_source": saved_xy_source, "status": initial_status,
                "historical_exclusions": initial_exclusions,
                "xx_status": _xx_status_guard(xx, lockin)}
            archive.save()
            optical = optical_factory(station.optical, archive.event, clock=clock, sleep=sleep)
            optical.open(authorize_writes=True, confirm_manual_route=True)
            optical.configure()
            for point_index in config.optical_point_indices:
                archive.stage(f"point-{point_index}:prepare_dark")
                optical.set_point(point_index)
                trace = {"optical_point_index": point_index, "samples": []}
                archive.result["frequency_observations"].append(trace)
                start = clock()
                until = start + config.pem_frequency_observation_s
                # The guard remains OFF and requires a prepared/stable PEM.
                while True:
                    evidence = optical.observe_dark(phase="frequency_observation")
                    frequency = _frequency(evidence["state"])
                    if not lockin.reference_min_hz <= frequency <= lockin.reference_max_hz:
                        raise ValueError("Prepared PEM frequency left the approved interval")
                    sample = {"elapsed_s": clock() - start, "at_utc": utc(),
                              "frequency_hz": frequency, "stable": evidence["state"]["pem"]["stable"]}
                    trace["samples"].append(sample)
                    archive.event("frequency_observation", sample)
                    archive.save()
                    if clock() >= until:
                        break
                    sleep(max(0, min(config.monitor_interval_s, until - clock())))
                pem_frequency = sum(s["frequency_hz"] for s in trace["samples"]) / len(trace["samples"])
                frequencies = list(config.internal_frequencies_hz)
                if config.include_pem_frequency and not any(math.isclose(f, pem_frequency, abs_tol=0.0001, rel_tol=0) for f in frequencies):
                    frequencies.append(pem_frequency)
                for frequency in frequencies:
                    for tau in config.time_constants_s:
                        for harmonic in config.harmonics:
                            optical.observe_dark(phase="before_internal_settings")
                            xy_touched = True  # A command can execute even if its reply fails.
                            actual_frequency = xy.configure_internal_reference(frequency, authorize_writes=True)
                            if not lockin.reference_min_hz <= actual_frequency <= lockin.reference_max_hz:
                                raise ValueError("Actual internal frequency left the approved interval")
                            settings = replace(lockin.lockin_xy, time_constant_s=tau)
                            xy.configure_fixed_measurement(settings, authorize_writes=True)
                            xy.set_harmonic(harmonic, authorize_writes=True)
                            expected = _native(xy.read_settings())
                            if expected["reference_source"] != "internal" or expected["harmonic"] != harmonic:
                                raise ValueError("Internal diagnostic setup readback mismatch")
                            expected.update(time_constant_s=tau, filter_slope_db_oct=24,
                                            advanced_filter=False, synchronous_filter=False)
                            _settings_guard(xy, expected, actual_frequency)
                            setup_status, setup_exclusions = _status_guard(xy, lockin, intentional_configuration=True)
                            archive.event("internal_setup", {"settings": expected, "status": setup_status,
                                "setup_exclusions": setup_exclusions, "requested_frequency_hz": frequency,
                                "actual_frequency_hz": actual_frequency})
                            # Settle the ordinary RC chain; no laser emission here.
                            sleep(max(tau * lockin.settle_time_constants, 0.02))
                            for illumination in ("dark", "light"):
                                row = {"capture_id": f"capture-{len(archive.result['captures']):04d}",
                                    "illumination": illumination, "optical_point_index": point_index,
                                    "requested_internal_frequency_hz": frequency, "internal_frequency_hz": actual_frequency,
                                    "time_constant_s": tau, "harmonic": harmonic, "pem_frequency_hz": pem_frequency,
                                    "exclusion_reasons": [], "guards": [], "error": None}
                                archive.result["captures"].append(row)
                                archive.stage(row["capture_id"] + ":" + illumination)
                                if illumination == "light":
                                    optical.qualify()
                                    optical.begin_sample()
                                else:
                                    optical.observe_dark(phase="before_dark_capture")
                                guard_epoch = clock()
                                deadline = guard_epoch + config.capture_timeout_s
                                effective_rate = max(config.minimum_capture_rate_hz,
                                    4 / (2 * math.pi * tau), 4 * abs(harmonic * (pem_frequency - actual_frequency)))
                                row["effective_minimum_capture_rate_hz"] = effective_rate

                                def guard():
                                    started = clock()
                                    evidence = (optical.monitor_sample(deadline=deadline) if illumination == "light"
                                                else optical.observe_dark(phase="dark_capture", deadline=deadline))
                                    live_pem_frequency = _frequency(evidence["state"])
                                    if not lockin.reference_min_hz <= live_pem_frequency <= lockin.reference_max_hz:
                                        raise ValueError("Live PEM frequency left the approved interval")
                                    if abs(harmonic * (live_pem_frequency - actual_frequency)) >= effective_rate / 2:
                                        raise ValueError("PEM beat exceeded the planned buffer Nyquist margin")
                                    sources = _source_guard(xx, xy, lockin, saved_xy_source, saved_xx_identity)
                                    settings_evidence = _settings_guard(xy, expected, actual_frequency)
                                    status, exclusions = _status_guard(xy, lockin)
                                    instantaneous = settings_evidence["sample"]["status"]
                                    if instantaneous["locked"] is not True:
                                        if lockin.reference_unlock_policy != "record_continue":
                                            raise ValueError("SR865A instantaneous reference unlock")
                                        exclusions.append("reference_unlock_recorded")
                                    if instantaneous["input_overload"] is not False or instantaneous["output_scale_overload"] is not False:
                                        if lockin.overload_policy != "record_continue":
                                            raise ValueError("SR865A instantaneous overload")
                                        exclusions.append("overload_recorded")
                                    xx_status = _xx_status_guard(xx, lockin)
                                    row["exclusion_reasons"].extend(exclusions)
                                    reading = {"started_elapsed_s": started - guard_epoch, "finished_elapsed_s": clock() - guard_epoch,
                                        "optical": evidence, "sources": sources, "lockin": settings_evidence,
                                        "status": status, "xx_status": xx_status}
                                    row["guards"].append(reading)
                                    archive.event("capture_guard", {"capture_id": row["capture_id"], **reading})

                                capture_error = None
                                try:
                                    captured = xy.capture_xy(config.capture_duration_s, effective_rate,
                                        timeout_s=config.capture_timeout_s, authorize_writes=True,
                                        clock=clock, sleep=sleep, on_poll=guard,
                                        poll_interval_s=config.monitor_interval_s)
                                    row["capture"] = plain(captured)
                                    if (not captured.completed or captured.cleanup_errors
                                            or not captured.stop_verified or not captured.capture_configuration_restored):
                                        raise ValueError("SR865A capture/stop/configuration restoration not verified")
                                except BaseException as exc:
                                    capture_error = exc
                                    row["error"] = error_text(exc)
                                    row["exclusion_reasons"].append(row["error"])
                                    if isinstance(exc, Sr865aCaptureError):
                                        row["capture"] = plain(exc.result)
                                finally:
                                    if illumination == "light" and optical.sample_active:
                                        try:
                                            optical.end_sample()
                                        except BaseException as exc:
                                            row["exclusion_reasons"].append(error_text(exc))
                                            capture_error = capture_error or exc
                                    try:
                                        optical.suspend()
                                    except BaseException as exc:
                                        row["exclusion_reasons"].append(error_text(exc))
                                        capture_error = capture_error or exc
                                    if row.get("capture"):
                                        data = row["capture"]
                                        data["sample_rate_hz"] = data["actual_rate_hz"]
                                        data["validity"] = bool(data["completed"] and not row["exclusion_reasons"] and capture_error is None)
                                        data["exclusion_reasons"] = list(dict.fromkeys(row["exclusion_reasons"]))
                                        frequencies_observed = [_frequency(g["optical"]["state"]) for g in row["guards"]]
                                        row["prepared_pem_frequency_hz"] = pem_frequency
                                        if frequencies_observed:
                                            row["pem_frequency_hz"] = sum(frequencies_observed) / len(frequencies_observed)
                                            row["guard_observed_pem_frequency_range_hz"] = [min(frequencies_observed), max(frequencies_observed)]
                                        row["pem_frequency_estimate_source"] = "mean of before/during/after buffer guard readbacks, including download; not every hardware sample"
                                        starts = [g["started_elapsed_s"] for g in row["guards"]]
                                        row["maximum_guard_start_interval_s"] = max((b-a for a,b in zip(starts, starts[1:])), default=None)
                                        row["analysis"] = summarize_capture(data, internal_frequency_hz=actual_frequency,
                                            harmonic=harmonic, time_constant_s=tau, pem_frequency_hz=row["pem_frequency_hz"])
                                        archive.capture_file(row)
                                    archive.save()
                                if capture_error:
                                    raise capture_error
            archive.result["status"] = "completed"
        except BaseException as exc:
            archive.result["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) or isinstance(exc.__cause__, KeyboardInterrupt) else "failed"
            archive.result["error"] = error_text(exc)
        finally:
            archive.result["stage"] = "cleanup"
            for row in archive.result["captures"]:
                data = row.get("capture", {})
                if data.get("cleanup_errors"):
                    cleanup["errors"].extend(row["capture_id"] + ": " + item for item in data["cleanup_errors"])
                if data.get("capture_owned") and (not data.get("stop_verified") or not data.get("capture_configuration_restored")):
                    cleanup["errors"].append(row["capture_id"] + ": capture ownership cleanup unverified")
            # OFF precedes electrical protection and reference restoration.
            if optical:
                try:
                    cleanup["optical_off"] = optical.cleanup(finish_pem=False, close_meter=False)
                except BaseException as exc:
                    cleanup["errors"].append("optical_off: " + error_text(exc))
            if xx:
                try:
                    if xx.read_identity() != saved_xx_identity:
                        raise ValueError("XX identity changed; protection writes prohibited")
                    cleanup["xx"] = xx.set_source_amplitude(lockin.cleanup_source_voltage_v,
                        minimum_v=lockin.minimum_source_voltage_v, maximum_v=lockin.maximum_source_voltage_v,
                        expected_dc_mode=lockin.source.dc_mode, authorize_writes=True, protect=True)
                    cleanup["xx_protection_verified"] = (cleanup["xx"]["source_voltage_v"] <= lockin.cleanup_source_voltage_v + 1e-9
                        and cleanup["xx"]["dc_offset_v"] == 0 and cleanup["xx"]["dc_mode"] == lockin.source.dc_mode)
                    if not cleanup["xx_protection_verified"]:
                        raise ValueError("XX source protection unverified")
                except BaseException as exc:
                    cleanup["errors"].append("xx: " + error_text(exc))
            if xy_touched:
                try:
                    if not cleanup["xx_protection_verified"]:
                        raise ValueError("XX protection unverified; leave reference active/unknown")
                    if any(row.get("capture", {}).get("capture_owned") and not row["capture"].get("stop_verified")
                           for row in archive.result["captures"]):
                        raise ValueError("Owned capture stop unverified; reference restoration prohibited")
                    if xy.read_identity() != saved["identity"]:
                        raise ValueError("XY identity changed; restoration writes prohibited")
                    # Restore harmonic while the owned internal frequency is
                    # valid; the original external clock may still be unlocked.
                    xy.set_harmonic(saved["harmonic"], authorize_writes=True)
                    xy.configure_external_reference(edge=saved["external_reference_edge"],
                        input_impedance_ohm=saved["reference_input_impedance_ohm"], sync_output_mode="preserve",
                        authorize_writes=True)
                    xy.restore_stored_internal_frequency(saved_frequency, authorize_writes=True)
                    xy.configure_fixed_measurement(saved_config, authorize_writes=True)
                    restored = _native(xy.read_settings())
                    if any(not _same(restored.get(k), v) for k, v in saved.items()):
                        raise ValueError("XY baseline settings restoration mismatch")
                    _source_guard(xx, xy, lockin, saved_xy_source, saved_xx_identity)
                    cleanup["xy_restored"] = True
                except BaseException as exc:
                    cleanup["errors"].append("xy_restore: " + error_text(exc))
            else:
                cleanup["xy_restored"] = saved is not None
            if optical:
                try:
                    cleanup["optical_final"] = optical.close(finish_pem=cleanup["xx_protection_verified"])
                    final = cleanup["optical_final"]
                    if (not final.get("verified") or final.get("reference_cleanup_pending")
                            or final.get("meter_cleanup_pending") or final.get("errors")):
                        cleanup["errors"].append("Final optical protection/close not verified")
                except BaseException as exc:
                    cleanup["errors"].append("optical_final: " + error_text(exc))
            for role, backend in (("lockin_xx", xx), ("lockin_xy", xy)):
                if backend:
                    try:
                        archive.event("lockin_audit", {"role": role, "raw": plain(backend.audit)})
                    except BaseException as exc:
                        archive.result["audit_errors"].append(error_text(exc))
            close_errors = []
            for role, resource in resources:
                try:
                    resource.close()
                except BaseException as exc:
                    close_errors.append(role + ": " + error_text(exc))
            if manager:
                try:
                    manager.close()
                except BaseException as exc:
                    close_errors.append("visa_manager: " + error_text(exc))
            cleanup["errors"].extend(close_errors)
            cleanup["resources_closed"] = manager is not None and not close_errors
            cleanup["verified"] = bool(cleanup["xx_protection_verified"] and cleanup["xy_restored"]
                and cleanup["resources_closed"] and not cleanup["errors"] and not archive.result["audit_errors"]
                and cleanup.get("optical_final", {}).get("verified"))
            cleanup["manual_verification_required"] = not cleanup["verified"]
            archive.result["cleanup"] = cleanup
            if not cleanup["verified"] and archive.result["status"] == "completed":
                archive.result["status"] = "failed"
                archive.result["error"] = "Final protection/restoration/audit not verified"
            archive.result["finished_at_utc"] = utc()
            archive.result["stage"] = "terminal"
            archive.save()
        return archive.result


def analyze(directory):
    """Recompute summaries from archived data without instrument access."""
    path = Path(directory) / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("status") == "active":
        raise ValueError("Analyze only a terminal run; monitor is read-only")
    summaries = []
    for row in result["captures"]:
        if row.get("capture"):
            data = dict(row["capture"])
            reasons = list(data.get("exclusion_reasons", ()))
            if result.get("status") != "completed":
                reasons.append("run_terminal_failed")
            if not result.get("simulated") and result.get("cleanup", {}).get("verified") is not True:
                reasons.append("final_cleanup_not_verified")
            data["exclusion_reasons"] = reasons
            summaries.append({"capture_id": row["capture_id"], "illumination": row["illumination"],
                "run_status": result.get("status"), "simulated": result.get("simulated", False),
                **summarize_capture(data, internal_frequency_hz=row["internal_frequency_hz"],
                    harmonic=row["harmonic"], time_constant_s=row["time_constant_s"], pem_frequency_hz=row["pem_frequency_hz"])})
    target = Path(directory) / ("analysis-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    target.write_text(json.dumps(summaries, allow_nan=False, indent=2), encoding="utf-8")
    return target


def cleanup_summary(result):
    cleanup = result.get("cleanup", {})
    return {key: cleanup.get(key) for key in ("verified", "xx_protection_verified", "xy_restored",
        "resources_closed", "manual_verification_required", "errors", "hardware_opened", "simulated")}


def simulate(config, station, directory):
    """Synthetic beat matrix for offline CLI/plot validation, no instrument API."""
    import numpy as np
    archive = Archive(directory, config, station, simulated=True)
    pem_frequency = 50027.755
    for point in config.optical_point_indices:
        frequencies = list(config.internal_frequencies_hz)
        if config.include_pem_frequency and pem_frequency not in frequencies:
            frequencies.append(pem_frequency)
        archive.result["frequency_observations"].append({"optical_point_index": point,
            "samples": [{"elapsed_s": float(t), "frequency_hz": pem_frequency, "stable": True, "at_utc": utc()}
                        for t in range(int(config.pem_frequency_observation_s) + 1)]})
        for frequency in frequencies:
            for tau in config.time_constants_s:
                for harmonic in config.harmonics:
                    rate = max(config.minimum_capture_rate_hz, 4 / (2 * math.pi * tau), 4 * abs(harmonic * (pem_frequency - frequency)))
                    t = np.arange(max(2, int(config.capture_duration_s * rate))) / rate
                    for illumination in ("dark", "light"):
                        amplitude = 1e-7 if illumination == "dark" else 1e-5
                        z = amplitude * np.exp(2j * math.pi * harmonic * (pem_frequency - frequency) * t)
                        data = {"x_v": z.real.tolist(), "y_v": z.imag.tolist(), "r_v": np.abs(z).tolist(),
                            "t_s": t.tolist(), "actual_rate_hz": rate, "sample_rate_hz": rate,
                            "completed": True, "validity": True, "simulated": True}
                        row = {"capture_id": f"capture-{len(archive.result['captures']):04d}",
                            "illumination": illumination, "optical_point_index": point,
                            "internal_frequency_hz": frequency, "time_constant_s": tau, "harmonic": harmonic,
                            "pem_frequency_hz": pem_frequency, "capture": data, "exclusion_reasons": [],
                            "analysis": summarize_capture(data, internal_frequency_hz=frequency,
                                harmonic=harmonic, time_constant_s=tau, pem_frequency_hz=pem_frequency)}
                        archive.capture_file(row)
                        archive.result["captures"].append(row)
    archive.result.update(status="completed", stage="terminal", finished_at_utc=utc(),
                          cleanup={"hardware_opened": False, "simulated": True})
    archive.save()
    return archive.result


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("describe", "validate-config", "run", "simulate"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name in ("run", "simulate"):
            command.add_argument("--output-directory", type=Path)
        if name == "run":
            command.add_argument("--authorize-optical", action="store_true")
            command.add_argument("--confirm-optical-route", action="store_true")
    for name in ("monitor", "analyze", "plot"):
        sub.add_parser(name).add_argument("--run-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "monitor":
        result = json.loads((args.run_directory / "result.json").read_text(encoding="utf-8"))
        print(json.dumps({key: result.get(key) for key in ("status", "stage", "error", "simulated")}
                         | {"captures": len(result["captures"]), "cleanup": cleanup_summary(result)}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "analyze":
        print(analyze(args.run_directory))
        return 0
    if args.command == "plot":
        from .pem_internal_plot import plot_diagnostic
        for path in plot_diagnostic(args.run_directory):
            print(path)
        return 0
    config_hash = hashlib.sha256(args.config.read_bytes()).hexdigest()
    config = load_diagnostic_config(args.config)
    station = load_reviewed(config)
    if args.command in ("describe", "validate-config"):
        print(json.dumps({"config": config.snapshot(), "optical_points": [station.optical.points[i] for i in config.optical_point_indices],
            "formal_hall_eligible": False, "real_run_requires_physical_xx_isolation": True,
            "rate_policy": "max(config minimum, 4/(2*pi*tau), 4*abs(harmonic*(PEM-internal)))"}, ensure_ascii=False, indent=2))
        return 0
    # Review source files immediately before ownership; snapshots are immutable.
    if hashlib.sha256(station.path.read_bytes()).hexdigest() != station.snapshot["config_sha256"]:
        raise ValueError("Station configuration changed during launch")
    if hashlib.sha256(Path(station.lockin.safety_path).read_bytes()).hexdigest() != station.lockin.safety_sha256:
        raise ValueError("Lockin safety configuration changed during launch")
    if hashlib.sha256(config.config_path.read_bytes()).hexdigest() != config_hash:
        raise ValueError("Diagnostic configuration changed during launch")
    directory = args.output_directory or config.output_directory / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    if args.command == "simulate":
        result = simulate(config, station, directory)
    else:
        result = run_diagnostic(config, station, directory,
            authorize_optical=args.authorize_optical, confirm_optical_route=args.confirm_optical_route)
    print(json.dumps({"run_directory": str(directory), "status": result["status"], "error": result["error"],
        "captures": len(result["captures"]), "cleanup": cleanup_summary(result)}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 2


def main():
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise SystemExit(error_text(exc)) from exc


if __name__ == "__main__":
    main()
