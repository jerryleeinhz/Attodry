"""Versioned, additive SQLite audit for arbitrary module combinations.

No hardware imports. Old RunStore tables and standalone files are not migrated.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any
import uuid


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def open_readonly(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def recovery_parent_state(connection: sqlite3.Connection, run_id: str) -> dict:
    """Read the resume claim inputs inside the caller's consistent transaction.

    The fingerprint covers the original run and conditions, all attempt rows,
    sample keys/content hashes and execution ownership. Ordinary append-only
    observation events are deliberately outside this compare-and-claim token.
    This helper performs SELECT only and accepts original schema-1 databases.
    """
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM combination_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Unknown combination run")
    condition_rows = [dict(r) for r in connection.execute(
        "SELECT * FROM combination_conditions WHERE run_id=? ORDER BY sequence_index", (run_id,)
    )]
    attempts = [dict(r) for r in connection.execute(
        "SELECT * FROM combination_attempts WHERE run_id=? ORDER BY condition_id, attempt_index",
        (run_id,),
    )]
    sample_manifest = [
        {"condition_id": r["condition_id"], "attempt_index": r["attempt_index"],
         "sample_index": r["sample_index"],
         "payload_sha256": hashlib.sha256(r["payload_json"].encode("utf-8")).hexdigest()}
        for r in connection.execute(
            "SELECT * FROM combination_samples WHERE run_id=? "
            "ORDER BY condition_id, attempt_index, sample_index", (run_id,))
    ]
    executions = [dict(r) for r in connection.execute(
        "SELECT * FROM combination_executions WHERE run_id=? ORDER BY sequence_index", (run_id,)
    )] if _has_table(connection, "combination_executions") else []
    mapping = [dict(r) for r in connection.execute(
        "SELECT * FROM combination_attempt_executions WHERE run_id=? "
        "ORDER BY condition_id, attempt_index", (run_id,)
    )] if _has_table(connection, "combination_attempt_executions") else []
    inputs = {"run": dict(row), "conditions": condition_rows, "attempts": attempts,
              "sample_manifest": sample_manifest, "executions": executions,
              "attempt_executions": mapping}
    accepted = {a["condition_id"] for a in attempts if a["status"] == "accepted"}
    return {**inputs,
            "state_sha256": hashlib.sha256(encode(inputs).encode("utf-8")).hexdigest(),
            "conditions": [json.loads(r["context_json"]) for r in condition_rows],
            "accepted_condition_ids": [r["condition_id"] for r in condition_rows
                                       if r["condition_id"] in accepted]}


def hardware_recovery_state(path: str | Path, run_id: str) -> dict:
    """File-only snapshot used by resume --check; never creates/migrates a DB."""
    with closing(open_readonly(path)) as connection:
        connection.execute("BEGIN")
        return recovery_parent_state(connection, run_id)


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _record_time(value, start, finish):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0 or not start <= parsed <= finish:
        raise ValueError("formal timestamp is missing, non-UTC or outside its sample")
    return parsed


def _index_lockin_ranges(snapshot):
    schedule = snapshot.get("lockin_range_rules")
    if schedule is None:
        return None
    bindings = {row["condition_id"]: row for row in schedule["condition_bindings"]}
    preview = {tuple(sorted(row["outer_indices"].items())): row for row in schedule["preview"]}
    if len(bindings) != len(schedule["condition_bindings"]) or len(preview) != len(schedule["preview"]):
        raise ValueError("Duplicate archived Lock-in range condition/outer visit")
    return bindings, preview


def _condition_lockin_range(snapshot, condition, index=None):
    """Reproduce a compiled selection from its raw rule and exact planned visit."""
    schedule = snapshot.get("lockin_range_rules")
    if schedule is None:
        return None
    if schedule.get("contract") != "outer-condition-lockin-range-v1":
        raise ValueError("Unknown archived Lock-in range rule contract")
    modules = [axis["module"] for axis in snapshot["axes"]]
    outer = modules[:modules.index("lockin")]
    if schedule.get("outer_modules") != outer or not outer:
        raise ValueError("Archived Lock-in range outer axes differ from the plan")
    indices = {module: condition["axes"][module]["index"] for module in outer}
    coordinates = {key: value for axis in snapshot["axes"] if axis["module"] in outer
                   for key, value in axis["points"][indices[axis["module"]]]["values"].items()}
    matches = [rule for rule in schedule["rules"]
               if all(key in coordinates and coordinates[key] in values
                      for key, values in rule.get("match", {}).items())
               and all(module in indices and indices[module] in values
                       for module, values in rule.get("point_indices", {}).items())]
    if len(matches) != 1:
        raise ValueError("Archived condition does not match exactly one Lock-in range rule")
    rule = matches[0]
    ranges = {role: rule[role + "_full_scale_v"] for role in ("lockin_xx", "lockin_xy")}
    if any(not _finite(value) or value <= 0 for value in ranges.values()):
        raise ValueError("Archived Lock-in range rule has an invalid full scale")
    bindings, previews = _index_lockin_ranges(snapshot) if index is None else index
    binding = bindings.get(condition["condition_id"])
    preview = previews.get(tuple(sorted(indices.items())))
    if (binding is None or preview is None
            or binding["repeat_index"] != condition["repeat_index"]
            or binding["requested"] != condition["requested"]
            or binding["outer_indices"] != indices
            or preview["requested"] != coordinates
            or any(row["rule_name"] != rule["name"] or row["full_scales_v"] != ranges
                   for row in (binding, preview))):
        raise ValueError("Archived Lock-in range bindings differ from the raw rule/condition")
    return {"rule_name": rule["name"], "full_scales_v": ranges}


def _validate_lockin_record(reading, snapshot, condition, start, finish, range_index=None):
    """Check archived formal pairs rather than trusting an accepted/clean label."""
    from .lockin_overload import OverloadPolicy
    from .lockin_model_settings import sensitivity_full_scale_for, time_constant_seconds_for
    config = snapshot["hardware"]["lockin"]
    selections = config["harmonics_by_role"]
    selections = {("lockin_" + role if role in {"xx", "xy"} else role): tuple(values)
                  for role, values in selections.items()}
    if set(selections) != {"lockin_xx", "lockin_xy"}:
        raise ValueError("formal Lock-in role contract is missing")
    status = reading["status"]
    selection = _condition_lockin_range(snapshot, condition, range_index)
    if selection is not None:
        context = status.get("lockin_range_rule", {})
        if (context.get("verified") is not True or context.get("rule_name") != selection["rule_name"]
                or context.get("requested_full_scales_v") != selection["full_scales_v"]
                or context.get("confirmed_full_scales_v") != selection["full_scales_v"]):
            raise ValueError("Formal Lock-in range context differs from its compiled condition")
    elif "lockin_range_rule" in status:
        raise ValueError("Formal Lock-in range context has no archived rule schedule")
    requested_hz = condition["requested"]["lockin_frequency_hz"]
    skipped = config.get("skipped_harmonics_by_frequency", {}).get(str(requested_hz), [])
    skipped_keys = {(item.get("role"), item["harmonic"]) for item in skipped}
    expected = {(role, harmonic) for role, values in selections.items() for harmonic in values
                if (None, harmonic) not in skipped_keys and (role, harmonic) not in skipped_keys}
    if not expected:
        raise ValueError("formal Lock-in harmonic contract is empty")
    actual_pairs = set()
    measured = {}
    for pair in status["samples"]:
        if selection is not None:
            context = pair.get("lockin_range_rule", {})
            if (context.get("verified") is not True or context.get("rule_name") != selection["rule_name"]
                    or context.get("requested_full_scales_v") != selection["full_scales_v"]
                    or context.get("confirmed_full_scales_v") != selection["full_scales_v"]):
                raise ValueError("Formal Lock-in pair range context differs from its compiled condition")
        harmonic = pair["harmonic"]
        selected = tuple("lockin_" + role for role in pair["selected_roles"])
        if (pair.get("sampling_policy") != "independent_roles" or pair.get("sample_index") != 0
                or pair.get("settings_verified") is not True or len(set(selected)) != len(selected)
                or set(selected) != {role for role, h in expected if h == harmonic}
                or not selected):
            raise ValueError("formal Lock-in pair selection/settings are incomplete")
        policy = pair.get("overload_policy")
        if policy not in {"abort", "continue_unselected", "record_continue"}:
            raise ValueError("formal Lock-in overload policy is unknown")
        if (pair.get("blocking_problems") != [] or OverloadPolicy(policy, selections).partition(
                pair.get("problems", ()))[0]):
            raise ValueError("formal Lock-in pair contains a blocking fault")
        before, after = pair["settings_before"]["roles"], pair["settings_after"]["roles"]
        if set(before) != {"lockin_xx", "lockin_xy"} or set(after) != set(before):
            raise ValueError("formal Lock-in settings omit a physical role")
        for role in ("lockin_xx", "lockin_xy"):
            raw = pair[role]
            value = raw["reading"]
            _record_time(raw["captured_at_utc"], start, finish)
            flags = raw["lia_status"]
            if (type(raw.get("error_status")) is not int or raw["error_status"] != 0
                    or value.get("locked") is not True or value.get("role") != role.removeprefix("lockin_")
                    or type(flags.get("raw")) is not int
                    or any(flags.get(key) is not False for key in
                           ("reference_unlocked", "frequency_range_changed", "time_constant_changed"))
                    or any(type(flags.get(key)) is not bool for key in
                           ("input_or_reserve_overload", "filter_overload", "output_overload"))):
                raise ValueError("formal Lock-in status is missing or unsafe")
            target_harmonic = pair["harmonics_by_role"][role]
            if value.get("harmonic") != target_harmonic:
                raise ValueError("formal Lock-in raw harmonic differs from selected detector")
            for setting in ("harmonic", "sensitivity_code", "reserve_code", "time_constant_s"):
                if before[role].get(setting) != after[role].get(setting):
                    raise ValueError("formal Lock-in settings changed during capture")
            if (before[role].get("harmonic") != target_harmonic
                    or not _finite(before[role].get("sensitivity_full_scale_v"))
                    or before[role]["sensitivity_full_scale_v"] <= 0
                    or before[role].get("sensitivity_full_scale_v") != after[role].get("sensitivity_full_scale_v")):
                raise ValueError("formal Lock-in settings readback is incomplete")
            role_config = config[role]
            model = role_config.get("model", "SR830")
            if model == "SR830":
                masks = {"input_or_reserve_overload": 1, "filter_overload": 2,
                         "output_overload": 4, "reference_unlocked": 8,
                         "frequency_range_changed": 16, "time_constant_changed": 32}
                if (not 0 <= flags["raw"] <= 127 or any(
                        flags[name] != bool(flags["raw"] & mask) for name, mask in masks.items())):
                    raise ValueError("formal SR830 raw status contradicts decoded flags")
            elif (flags.get("status_known") is not True or flags.get("unknown_status_bits") != 0
                    or any(flags.get(key) is not False for key in
                           ("configuration_changed_latched", "power_on_latched", "instrument_error"))):
                raise ValueError("formal native Lock-in status is incomplete or unsafe")
            if (before[role].get("model", "SR830") != model
                    or sensitivity_full_scale_for(model, before[role]["sensitivity_code"])
                    != before[role]["sensitivity_full_scale_v"]
                    or time_constant_seconds_for(model, before[role]["time_constant_code"])
                    != before[role]["time_constant_s"]):
                raise ValueError("formal Lock-in setting codes do not reproduce physical readbacks")
            if before[role].get("time_constant_s") != role_config["time_constant_s"]:
                raise ValueError("formal Lock-in time constant differs from its archived configuration")
            if (selection is not None and before[role]["sensitivity_full_scale_v"]
                    != selection["full_scales_v"][role]):
                raise ValueError("formal Lock-in sensitivity differs from its compiled range rule")
            if (before[role].get("model", "SR830") == "SR830"
                    and before[role].get("reserve_code") not in {0, 1, 2}):
                raise ValueError("formal Lock-in Reserve readback is unknown")
            for metric in ("x_v", "y_v", "amplitude_v", "frequency_hz"):
                if not _finite(value.get(metric)):
                    raise ValueError("formal Lock-in raw numeric readback is incomplete")
            if value["amplitude_v"] < 0 or abs(value["frequency_hz"] - requested_hz) > max(.00101, abs(requested_hz) * 1e-4):
                raise ValueError("formal Lock-in amplitude/frequency readback is invalid")
            if role not in selected:
                continue
            setting = next((s for s in role_config.get("harmonic_settings", [])
                            if s["harmonic"] == harmonic), None)
            effective = {**role_config, **(setting or {})}
            scale = before[role]["sensitivity_full_scale_v"]
            if effective.get("sensitivity_mode", "fixed") == "bounded_auto":
                if not effective["autorange_min_full_scale_v"] <= scale <= effective["autorange_max_full_scale_v"]:
                    raise ValueError("formal Lock-in range exceeds its archived autorange bounds")
            else:
                configured = effective["sensitivity_full_scale_v"]
                point_scale = status.get("range_spec", {}).get(role.removeprefix("lockin_") + "_full_scale_v")
                if setting is None and point_scale is not None:
                    configured = point_scale
                if selection is not None:
                    configured = selection["full_scales_v"][role]
                if scale != configured:
                    raise ValueError("formal Lock-in sensitivity differs from its archived fixed setting")
            if model == "SR830":
                expected_reserve = {"high_reserve": 0, "normal": 1, "low_noise": 2}[effective["reserve_mode"]]
                if before[role]["reserve_code"] != expected_reserve:
                    raise ValueError("formal Lock-in Reserve differs from its archived setting")
            key = (role, harmonic)
            if key in actual_pairs or key not in expected or target_harmonic != harmonic:
                raise ValueError("formal Lock-in role/harmonic is duplicated or unexpected")
            actual_pairs.add(key)
            for metric in ("x_v", "y_v", "amplitude_v", "phase_deg"):
                number = value.get(metric)
                if (metric == "phase_deg" and number is None and value.get("model") == "SR865A"
                        and value["x_v"] == value["y_v"] == value["amplitude_v"] == 0):
                    continue
                if not _finite(number):
                    raise ValueError("formal Lock-in selected channel is incomplete")
                measured[f"{role}_h{harmonic}_{metric}"] = number
    if actual_pairs != expected or measured != reading["measurements"]:
        raise ValueError("formal Lock-in pairs do not reproduce the archived measurements")


def _validate_environment_record(reading, module, snapshot, start, finish):
    from .models import VectorField
    from .safety import FieldReadbackPolicy, MagnetLimits, validate_vector_field
    hardware = snapshot.get("hardware", {})
    cryostat = hardware.get("effective_cryostat", hardware.get(module, {}))
    limits = cryostat.get("cryostat", {})
    field_limits = cryostat.get("magnet", {}).get("limits")
    policy = (FieldReadbackPolicy.from_snapshot(snapshot["field_readback_policy"])
              if "field_readback_policy" in snapshot else None)
    status = reading["status"]
    samples = status["samples"]
    if status.get("sampling") != "synchronous_brackets_not_continuous" or len(samples) < 2:
        raise ValueError("formal environment window lacks synchronous brackets")
    for raw in samples:
        _record_time(raw["captured_at_utc"], start, finish)
        state = raw["state"]
        if (type(state.get("error_code")) is not int or state["error_code"] != 0
                or any(type(state.get(key)) is not bool for key in
                       ("temperature_control_enabled", "field_control_enabled"))
                or state["temperature_control_enabled" if module == "temperature"
                         else "field_control_enabled"] is not True):
            raise ValueError("formal environment control/error state is unknown or unsafe")
        for key in ("sample_temperature_k", "user_temperature_k", "vti_temperature_k"):
            if not _finite(state.get(key)) or state[key] <= 0:
                raise ValueError("formal environment temperature is incomplete")
        for key in ("sample_temperature_k", "user_temperature_k"):
            if not limits.get("temperature_min_k", 0) <= state[key] <= limits.get("temperature_max_k", float("inf")):
                raise ValueError("formal environment temperature exceeds its archived hard bounds")
        for key in ("field", "field_setpoint"):
            if set(state[key]) != {"bx_t", "bz_t"} or not all(_finite(v) for v in state[key].values()):
                raise ValueError("formal environment field state is incomplete")
            field = VectorField(**state[key])
            if policy is not None:
                (policy.validate_readback if key == "field" else policy.validate_target)(field)
            elif field_limits is not None:
                validate_vector_field(field, MagnetLimits(**field_limits))
    if module == "temperature":
        if reading["actual"] != {"temperature_k": status["temperature_mean_k"]}:
            raise ValueError("formal temperature differs from its archived window")
    elif reading["actual"] != status["field_mean"]:
        raise ValueError("formal field differs from its archived window")


def _validate_smu_record(reading, condition, start, finish):
    roles = {key.rsplit("_", 1)[0] for key in condition["requested"]
             if key.startswith(("smu_", "gate_"))}
    status = reading["status"]
    if (status.get("clean") is not True or status.get("problems") not in ([], ())
            or set(status["readings"]) != roles):
        raise ValueError("formal SMU readback is incomplete or unsafe")
    measured = {}
    actual = {}
    for role in roles:
        timed = status["readings"][role]
        _record_time(timed["timestamp"], start, finish)
        raw = timed["reading"]
        if (raw.get("output_enabled") is not True or raw.get("compliance_trip") is not False
                or raw.get("status_query_consumed") is not True or not isinstance(raw.get("status"), str)
                or any(not _finite(raw.get(key)) for key in ("voltage_v", "current_a", "source_setpoint"))):
            raise ValueError("formal SMU status/compliance is missing or unsafe")
        measured.update({role + "_voltage_v": raw["voltage_v"], role + "_current_a": raw["current_a"]})
        for suffix, metric in (("_v", "voltage_v"), ("_a", "current_a")):
            if role + suffix in condition["requested"]:
                actual[role + suffix] = raw[metric]
    if measured != reading["measurements"] or actual != reading["actual"]:
        raise ValueError("formal SMU raw readbacks differ from archived measurements")


def validate_accepted_records(connection: sqlite3.Connection, run_id: str, original=None) -> dict:
    """Require complete, traceable raw evidence before skipping accepted leaves.

    Each attempt uses its own execution's archived configuration. This does not
    reclassify overload-invalid data or reinterpret it under the new settings.
    """
    from .lockin_overload import reading_allows_continuation
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT plan_json FROM combination_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown combination run")
    original = json.loads(row[0]) if original is None else original
    configurations = {}
    if _has_table(connection, "combination_executions"):
        configurations = {r["execution_id"]: json.loads(r["plan_json"]) for r in connection.execute(
            "SELECT execution_id,plan_json FROM combination_executions WHERE run_id=?", (run_id,))}
    mapping = ({(r["condition_id"], r["attempt_index"]): r["execution_id"] for r in connection.execute(
        "SELECT * FROM combination_attempt_executions WHERE run_id=?", (run_id,))}
        if _has_table(connection, "combination_attempt_executions") else {})
    accepted_count = sample_count = 0
    range_indexes = {}
    for attempt in connection.execute("SELECT a.*,c.context_json FROM combination_attempts a "
        "JOIN combination_conditions c ON c.run_id=a.run_id AND c.condition_id=a.condition_id "
        "WHERE a.run_id=? AND a.status='accepted' ORDER BY c.sequence_index", (run_id,)):
        condition = json.loads(attempt["context_json"])
        execution_id = mapping.get((attempt["condition_id"], attempt["attempt_index"]))
        if configurations and execution_id not in configurations:
            raise ValueError("Accepted condition has no archived execution configuration")
        snapshot = configurations.get(execution_id, original)
        if execution_id not in range_indexes:
            range_indexes[execution_id] = _index_lockin_ranges(snapshot)
        modules = [a["module"] for a in snapshot["axes"]]
        rows = connection.execute("SELECT sample_index,payload_json FROM combination_samples "
            "WHERE run_id=? AND condition_id=? AND attempt_index=? ORDER BY sample_index",
            (run_id, attempt["condition_id"], attempt["attempt_index"])).fetchall()
        try:
            if [r["sample_index"] for r in rows] != list(range(snapshot["samples_per_condition"])):
                raise ValueError("incomplete sample indices")
            for row in rows:
                sample = json.loads(row["payload_json"])
                start, finish = datetime.fromisoformat(sample["started_at_utc"]), datetime.fromisoformat(sample["finished_at_utc"])
                _record_time(sample["started_at_utc"], start, finish)
                _record_time(sample["finished_at_utc"], start, finish)
                attempt_start = datetime.fromisoformat(attempt["started_at_utc"])
                attempt_finish = datetime.fromisoformat(attempt["finished_at_utc"])
                if not attempt_start <= start <= finish <= attempt_finish:
                    raise ValueError("sample timestamps are outside the accepted attempt")
                if sample.get("acquisition_accepted") is not True or sample.get("simulated") is not False:
                    raise ValueError("accepted flag lacks hardware acquisition evidence")
                if (set(sample["actual"]) != set(condition["requested"])
                        or not all(_finite(v) for v in (*sample["actual"].values(), *sample["measurements"].values()))
                        or [r["module"] for r in sample["reads"]] != modules):
                    raise ValueError("actual coordinates/module reads are incomplete")
                actual, measured = {}, {}
                for reading in sample["reads"]:
                    _record_time(reading["captured_at_utc"], start, finish)
                    if not reading_allows_continuation(reading):
                        raise ValueError("module read is not acquisition-qualified")
                    actual.update(reading["actual"])
                    measured.update(reading["measurements"])
                    module = reading["module"]
                    expected_actual = ({"temperature_k"} if module == "temperature" else
                        {"field_x_t", "field_z_t"} if module == "magnetic" else
                        {"lockin_excitation_v_rms", "lockin_frequency_hz"} if module == "lockin" else
                        {key for key in condition["requested"] if key.startswith(("smu_", "gate_"))})
                    if set(reading["actual"]) != expected_actual:
                        raise ValueError("module actual coordinate ownership differs from the plan")
                    if module == "lockin":
                        _validate_lockin_record(reading, snapshot, condition, start, finish,
                                                range_indexes[execution_id])
                    elif module in {"temperature", "magnetic"}:
                        _validate_environment_record(reading, module, snapshot, start, finish)
                    elif module == "smu":
                        _validate_smu_record(reading, condition, start, finish)
                if actual != sample["actual"] or measured != sample["measurements"]:
                    raise ValueError("sample summary does not reproduce its raw module readings")
                sample_count += 1
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Accepted condition {attempt['condition_id']} has incomplete/invalid raw evidence: {exc}") from exc
        accepted_count += 1
    return {"accepted_conditions": accepted_count, "accepted_samples": sample_count}


class CombinationStore:
    """One writer; FULL-synchronous WAL. Each raw append precedes promotion."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.current_execution_id: str | None = None
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS combination_runs (
                run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                plan_json TEXT NOT NULL, created_at_utc TEXT NOT NULL,
                status TEXT NOT NULL, cleanup_json TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS combination_conditions (
                run_id TEXT NOT NULL REFERENCES combination_runs(run_id),
                condition_id TEXT NOT NULL, sequence_index INTEGER NOT NULL,
                context_json TEXT NOT NULL,
                PRIMARY KEY (run_id, condition_id), UNIQUE(run_id, sequence_index)
            );
            CREATE TABLE IF NOT EXISTS combination_attempts (
                run_id TEXT NOT NULL, condition_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL, status TEXT NOT NULL
                    CHECK(status IN ('started', 'accepted', 'rejected')),
                error TEXT, started_at_utc TEXT NOT NULL, finished_at_utc TEXT,
                PRIMARY KEY(run_id, condition_id, attempt_index),
                FOREIGN KEY(run_id, condition_id)
                    REFERENCES combination_conditions(run_id, condition_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS combination_one_accepted
                ON combination_attempts(run_id, condition_id) WHERE status='accepted';
            CREATE TABLE IF NOT EXISTS combination_samples (
                run_id TEXT NOT NULL, condition_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL, sample_index INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, condition_id, attempt_index, sample_index),
                FOREIGN KEY(run_id, condition_id, attempt_index)
                    REFERENCES combination_attempts(run_id, condition_id, attempt_index)
            );
            CREATE TABLE IF NOT EXISTS combination_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES combination_runs(run_id),
                created_at_utc TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS combination_executions (
                run_id TEXT NOT NULL REFERENCES combination_runs(run_id),
                execution_id TEXT NOT NULL, sequence_index INTEGER NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL,
                plan_json TEXT NOT NULL, review_json TEXT NOT NULL,
                parent_state_sha256 TEXT, created_at_utc TEXT NOT NULL,
                finished_at_utc TEXT, cleanup_json TEXT, error TEXT,
                PRIMARY KEY(run_id, execution_id), UNIQUE(run_id, sequence_index)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS combination_one_active_execution
                ON combination_executions(run_id) WHERE status='active';
            CREATE TABLE IF NOT EXISTS combination_attempt_executions (
                run_id TEXT NOT NULL, condition_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL, execution_id TEXT NOT NULL,
                PRIMARY KEY(run_id, condition_id, attempt_index),
                FOREIGN KEY(run_id, condition_id, attempt_index)
                    REFERENCES combination_attempts(run_id, condition_id, attempt_index),
                FOREIGN KEY(run_id, execution_id)
                    REFERENCES combination_executions(run_id, execution_id)
            );
        """)

    def __enter__(self) -> "CombinationStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.connection.close()

    def begin_hardware_execution(self, run_id: str, plan: dict, conditions: list[dict], *,
                                 recovery: dict | None = None) -> set[str]:
        """Atomically claim one reviewed execution; preserve every earlier segment.

        A terminal legacy audit is adopted without rewriting its original plan,
        conditions, attempts or samples. Recovery claims bind the complete parent
        fingerprint and repeat only whole pending conditions. Active/started
        orphans cannot be resolved by guessing process liveness.
        """
        if self.current_execution_id is not None:
            raise ValueError("This store already claimed an execution")
        if plan.get("mode") != "hardware":
            raise ValueError("Hardware execution requires a hardware snapshot")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            prior = self.connection.execute(
                "SELECT * FROM combination_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            accepted: set[str] = set()
            parent_hash = None
            review = {}
            kind = "initial"
            sequence = 0
            if prior is None:
                if recovery is not None:
                    raise ValueError("Cannot recover an unknown run")
                self.connection.execute(
                    "INSERT INTO combination_runs VALUES (?, ?, ?, ?, 'active', NULL, NULL)",
                    (run_id, SCHEMA_VERSION, encode(plan), utc_now()),
                )
                for condition in conditions:
                    self.connection.execute(
                        "INSERT INTO combination_conditions VALUES (?, ?, ?, ?)",
                        (run_id, condition["condition_id"], condition["sequence_index"],
                         encode(condition)),
                    )
            else:
                if recovery is None:
                    raise ValueError("Run already exists; never overwrite an audit")
                state = recovery_parent_state(self.connection, run_id)
                parent_hash = recovery.get("expected_parent_state_sha256")
                if not parent_hash or parent_hash != state["state_sha256"]:
                    raise ValueError("Recovery parent changed after review")
                if prior["schema_version"] != SCHEMA_VERSION:
                    raise ValueError("Unsupported recovery schema")
                if json.loads(prior["plan_json"]).get("mode") != "hardware":
                    raise ValueError("Cannot hardware-resume a simulation")
                if prior["status"] not in {"failed", "interrupted"}:
                    raise ValueError("Recovery requires terminal failed/interrupted parent")
                if any(a["status"] == "started" for a in state["attempts"]):
                    raise ValueError("Started/orphan attempt requires explicit manual recovery")
                if any(e["status"] == "active" for e in state["executions"]):
                    raise ValueError("An execution remains active; manual owner review required")
                original = recovery.get("original_conditions")
                if (not isinstance(original, list) or original != state["conditions"]
                        or conditions != original):
                    raise ValueError("Recovery must retain every original ordered condition")
                accepted = set(state["accepted_condition_ids"])
                flags = [c["condition_id"] in accepted for c in original]
                first_pending = next((i for i, flag in enumerate(flags) if not flag), len(flags))
                if any(flags[first_pending:]):
                    raise ValueError("Accepted conditions are not a contiguous prefix")
                cleanup_only = first_pending == len(flags)
                next_condition = None if cleanup_only else original[first_pending]
                supplied_next = recovery.get("next_condition")
                if isinstance(supplied_next, str):
                    supplied_next = next((c for c in original
                                          if c["condition_id"] == supplied_next), None)
                if (recovery.get("cleanup_only") is not cleanup_only
                        or supplied_next != next_condition):
                    raise ValueError("Recovery boundary differs from reviewed parent")
                review = recovery.get("review")
                if not isinstance(review, dict) or not review:
                    raise ValueError("Recovery requires an explicit archived review")
                from .combination_resume import (
                    LEGACY_SOURCE_SETS, RECOVERY_CONTRACT, _cleanup_review,
                    configuration_changes, digest, scan_contract,
                )
                original_plan = json.loads(prior["plan_json"])
                parent_plan = (json.loads(state["executions"][-1]["plan_json"])
                               if state["executions"] else original_plan)
                if (review.get("contract") != RECOVERY_CONTRACT
                        or review.get("ready") is not True
                        or review.get("acknowledged_failure") is not True):
                    raise ValueError("Recovery review is not ready and explicitly acknowledged")
                if (review.get("config_sha256") != plan.get("config_sha256")
                        or review.get("scan_contract_sha256") != digest(scan_contract(original_plan))):
                    raise ValueError("Recovery review does not bind the current configuration/scan")
                changes = configuration_changes(parent_plan, plan)
                if review.get("configuration_changes") != changes:
                    raise ValueError("Recovery configuration changes differ from review")
                old_sources, new_sources = parent_plan.get("source_sha256"), plan.get("source_sha256")
                live_resolution = recovery.get("_live_owner_resolution")
                live_resolution_review = None
                if live_resolution is not None or review.get("live_owner_resolution") is not None:
                    from .combination_owner_resolution import (
                        LiveOwnerResolution, LiveInitialisationResolution, LiveGuardIdentityResolution,
                        LiveProcessGuardRemovalResolution,
                    )
                    if (type(live_resolution) not in (LiveOwnerResolution, LiveInitialisationResolution, LiveGuardIdentityResolution, LiveProcessGuardRemovalResolution)
                            or review.get("manual_verification") is not False):
                        raise ValueError("Live owner resolution requires the ephemeral capability, never a file/manual flag")
                    live_resolution_review, _ = live_resolution.verify(state, plan, self.connection)
                    if review.get("live_owner_resolution") != live_resolution_review:
                        raise ValueError("Live owner resolution review differs from current capability")
                if (not old_sources or not new_sources
                        or old_sources != new_sources and digest(old_sources) not in LEGACY_SOURCE_SETS
                        and live_resolution_review is None):
                    raise ValueError("Archived source version has no reviewed recovery compatibility")
                cleanup_review = _cleanup_review(
                    json.loads(prior["cleanup_json"] or "{}"),
                    [axis["module"] for axis in original_plan["axes"]],
                )
                if (review.get("cleanup_review") != cleanup_review
                        or cleanup_review["manual_verification_required"]
                        and review.get("manual_verification") is not True and live_resolution_review is None):
                    raise ValueError("Unresolved cleanup requires explicit manual verification")
                if (any(c["category"] == "protection_increase" for c in changes)
                        and review.get("protection_changes_approved") is not True):
                    raise ValueError("SMU protection increase requires explicit approval")
                validate_accepted_records(self.connection, run_id, original_plan)
                if live_resolution_review is not None:
                    # Repeat live guard/lineage/readback validation immediately
                    # before any writes, within the same atomic parent claim.
                    claimed_review, claim_proof = live_resolution.verify(state, plan, self.connection, consume=True)
                    if claimed_review != live_resolution_review:
                        raise ValueError("Live owner resolution changed during the atomic claim")
                    review = json.loads(encode(review))
                    review["live_owner_resolution"]["claim_readbacks"] = claim_proof
                    review["live_owner_resolution"]["claim_verified_at_utc"] = utc_now()
                if not state["executions"]:
                    # Original schema-1 run remains byte-for-byte in the legacy
                    # execution. Its original top-level plan is never replaced.
                    legacy_id = "legacy-" + uuid.uuid4().hex
                    finished_event = self.connection.execute(
                        "SELECT created_at_utc FROM combination_events WHERE run_id=? "
                        "AND event_type='run_finished' ORDER BY event_id DESC LIMIT 1", (run_id,)
                    ).fetchone()
                    self.connection.execute(
                        "INSERT INTO combination_executions VALUES "
                        "(?, ?, 0, 'legacy', ?, ?, ?, NULL, ?, ?, ?, ?)",
                        (run_id, legacy_id, prior["status"], prior["plan_json"],
                         encode({"adopted_schema": prior["schema_version"],
                                 "original_run": dict(prior)}),
                         prior["created_at_utc"], finished_event[0] if finished_event else None,
                         prior["cleanup_json"], prior["error"]),
                    )
                    self.connection.execute(
                        "INSERT INTO combination_attempt_executions "
                        "SELECT run_id, condition_id, attempt_index, ? "
                        "FROM combination_attempts WHERE run_id=?", (legacy_id, run_id),
                    )
                    sequence = 1
                else:
                    sequence = max(e["sequence_index"] for e in state["executions"]) + 1
                    unmapped = self.connection.execute(
                        "SELECT COUNT(*) FROM combination_attempts a WHERE a.run_id=? AND "
                        "NOT EXISTS (SELECT 1 FROM combination_attempt_executions x WHERE "
                        "x.run_id=a.run_id AND x.condition_id=a.condition_id AND "
                        "x.attempt_index=a.attempt_index)", (run_id,),
                    ).fetchone()[0]
                    if unmapped:
                        raise ValueError("Recovery audit has unmapped attempts")
                kind = "cleanup_only" if cleanup_only else "resume"
                self.connection.execute(
                    "UPDATE combination_runs SET status='active', cleanup_json=NULL, error=NULL "
                    "WHERE run_id=?", (run_id,),
                )
            execution_id = "execution-" + uuid.uuid4().hex
            self.connection.execute(
                "INSERT INTO combination_executions VALUES "
                "(?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL, NULL, NULL)",
                (run_id, execution_id, sequence, kind, encode(plan), encode(review),
                 parent_hash, utc_now()),
            )
            self.connection.commit()
            self.current_execution_id = execution_id
            return accepted
        except BaseException:
            self.connection.rollback()
            raise

    def begin_run(self, run_id: str, plan: dict, conditions: list[dict], *,
                  resume: bool = False) -> set[str]:
        # Reserve the run atomically before any station operation.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            prior = self.connection.execute(
                "SELECT * FROM combination_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if prior is None:
                if resume:
                    raise ValueError("Cannot resume an unknown run")
                self.connection.execute(
                    "INSERT INTO combination_runs VALUES (?, ?, ?, ?, 'active', NULL, NULL)",
                    (run_id, SCHEMA_VERSION, encode(plan), utc_now()),
                )
                for condition in conditions:
                    self.connection.execute(
                        "INSERT INTO combination_conditions VALUES (?, ?, ?, ?)",
                        (run_id, condition["condition_id"], condition["sequence_index"],
                         encode(condition)),
                    )
                accepted: set[str] = set()
            else:
                if not resume:
                    raise ValueError("Run already exists; never overwrite an audit")
                if prior["schema_version"] != SCHEMA_VERSION or prior["plan_json"] != encode(plan):
                    raise ValueError("Resume plan/schema differs from archived plan")
                if prior["status"] not in {"failed", "interrupted"}:
                    raise ValueError("Only a terminal failed/interrupted run can resume")
                if any(axis["module"] == "magnetic" for axis in plan["axes"]):
                    raise ValueError("Magnetic history requires an explicit recovery plan; no auto-resume")
                if not json.loads(prior["cleanup_json"] or "{}").get("clean", False):
                    raise ValueError("Unverified cleanup requires manual recovery")
                accepted = {row[0] for row in self.connection.execute(
                    "SELECT condition_id FROM combination_attempts "
                    "WHERE run_id=? AND status='accepted'", (run_id,)
                )}
                self.connection.execute(
                    "UPDATE combination_runs SET status='active', cleanup_json=NULL, error=NULL "
                    "WHERE run_id=?", (run_id,),
                )
            self.connection.commit()
            return accepted
        except BaseException:
            self.connection.rollback()
            raise

    def event(self, run_id: str, event_type: str, payload: dict) -> None:
        if self.current_execution_id is not None:
            payload = {**payload, "execution_id": self.current_execution_id}
        with self.connection:
            self.connection.execute(
                "INSERT INTO combination_events(run_id, created_at_utc, event_type, payload_json) "
                "VALUES (?, ?, ?, ?)", (run_id, utc_now(), event_type, encode(payload)),
            )

    def begin_attempt(self, run_id: str, condition_id: str) -> int:
        with self.connection:
            if self.current_execution_id is not None:
                self._require_active_execution(run_id)
                if self.connection.execute(
                    "SELECT 1 FROM combination_attempts WHERE run_id=? AND condition_id=? "
                    "AND status IN ('accepted', 'started')", (run_id, condition_id),
                ).fetchone():
                    raise ValueError("Cannot repeat an accepted or started condition")
            index = self.connection.execute(
                "SELECT COALESCE(MAX(attempt_index), -1)+1 FROM combination_attempts "
                "WHERE run_id=? AND condition_id=?", (run_id, condition_id),
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO combination_attempts VALUES (?, ?, ?, 'started', NULL, ?, NULL)",
                (run_id, condition_id, index, utc_now()),
            )
            if self.current_execution_id is not None:
                self.connection.execute(
                    "INSERT INTO combination_attempt_executions VALUES (?, ?, ?, ?)",
                    (run_id, condition_id, index, self.current_execution_id),
                )
        return index

    def _require_active_execution(self, run_id: str) -> None:
        row = self.connection.execute(
            "SELECT status FROM combination_executions WHERE run_id=? AND execution_id=?",
            (run_id, self.current_execution_id),
        ).fetchone()
        if row is None or row["status"] != "active":
            raise ValueError("The claimed execution is not active")

    def sample(self, run_id: str, condition_id: str, attempt: int,
               sample_index: int, payload: dict) -> None:
        with self.connection:
            if self.current_execution_id is not None:
                self._require_owned_attempt(run_id, condition_id, attempt)
            self.connection.execute(
                "INSERT INTO combination_samples VALUES (?, ?, ?, ?, ?)",
                (run_id, condition_id, attempt, sample_index, encode(payload)),
            )

    def finish_attempt(self, run_id: str, condition_id: str, attempt: int, *,
                       expected_samples: int, error: str | None = None) -> None:
        with self.connection:
            if self.current_execution_id is not None:
                self._require_owned_attempt(run_id, condition_id, attempt)
            if error is None:
                samples = self.connection.execute(
                    "SELECT payload_json FROM combination_samples "
                    "WHERE run_id=? AND condition_id=? AND attempt_index=?",
                    (run_id, condition_id, attempt),
                ).fetchall()
                from .lockin_overload import reading_allows_continuation
                payloads = [json.loads(row[0]) for row in samples]
                if len(samples) != expected_samples or not all(
                    p.get("clean") is True or (
                        p.get("acquisition_accepted") is True and p.get("reads")
                        and all(reading_allows_continuation(r) for r in p["reads"]))
                    for p in payloads
                ):
                    raise ValueError("Cannot promote incomplete or unclean samples")
            cursor = self.connection.execute(
                "UPDATE combination_attempts SET status=?, error=?, finished_at_utc=? "
                "WHERE run_id=? AND condition_id=? AND attempt_index=? AND status='started'",
                ("accepted" if error is None else "rejected", error, utc_now(),
                 run_id, condition_id, attempt),
            )
            if cursor.rowcount != 1:
                raise ValueError("Attempt must exist and be started")

    def _require_owned_attempt(self, run_id: str, condition_id: str, attempt: int) -> None:
        self._require_active_execution(run_id)
        row = self.connection.execute(
            "SELECT execution_id FROM combination_attempt_executions "
            "WHERE run_id=? AND condition_id=? AND attempt_index=?",
            (run_id, condition_id, attempt),
        ).fetchone()
        if row is None or row[0] != self.current_execution_id:
            raise ValueError("Attempt does not belong to this execution")

    def finish_run(self, run_id: str, status: str, cleanup: dict,
                   error: str | None) -> None:
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("Invalid terminal run status")
        if status == "completed" and (cleanup.get("clean") is not True or error):
            raise ValueError("Completed run requires verified cleanup")
        with self.connection:
            if self.current_execution_id is not None:
                self._require_active_execution(run_id)
            if status == "completed":
                pending = self.connection.execute(
                    "SELECT COUNT(*) FROM combination_conditions c WHERE c.run_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM combination_attempts a WHERE "
                    "a.run_id=c.run_id AND a.condition_id=c.condition_id AND a.status='accepted')",
                    (run_id,),
                ).fetchone()[0]
                if pending:
                    raise ValueError("Cannot complete a run with pending conditions")
            if self.current_execution_id is not None:
                self.connection.execute(
                    "UPDATE combination_executions SET status=?, cleanup_json=?, error=?, "
                    "finished_at_utc=? WHERE run_id=? AND execution_id=? AND status='active'",
                    (status, encode(cleanup), error, utc_now(), run_id,
                     self.current_execution_id),
                )
            self.connection.execute(
                "UPDATE combination_runs SET status=?, cleanup_json=?, error=? WHERE run_id=?",
                (status, encode(cleanup), error, run_id),
            )
            self.connection.execute(
                "INSERT INTO combination_events(run_id, created_at_utc, event_type, payload_json) "
                "VALUES (?, ?, 'run_finished', ?)",
                (run_id, utc_now(), encode({"status": status, "cleanup": cleanup, "error": error,
                                          **({"execution_id": self.current_execution_id}
                                             if self.current_execution_id else {})})),
            )


def _last_formal_measurement(connection, run_id, plan):
    """Project the last accepted formal sample, retaining its execution origin.

    Current raw/rejected samples and instrument transcripts never fill this view.
    A resumed run may legitimately retain its last accepted result from a prior
    execution; its own archived sample count and execution ID are reported.
    """
    row = connection.execute(
        "SELECT s.condition_id,s.attempt_index,s.sample_index,a.status AS attempt_status,"
        "c.context_json,json_extract(s.payload_json,'$.started_at_utc') AS started_at_utc,"
        "json_extract(s.payload_json,'$.finished_at_utc') AS finished_at_utc "
        "FROM combination_samples s JOIN combination_attempts a ON a.run_id=s.run_id "
        "AND a.condition_id=s.condition_id AND a.attempt_index=s.attempt_index "
        "JOIN combination_conditions c ON c.run_id=s.run_id AND c.condition_id=s.condition_id "
        "WHERE s.run_id=? AND a.status='accepted' "
        "ORDER BY a.finished_at_utc DESC,c.sequence_index DESC,a.attempt_index DESC,s.sample_index DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    execution_id = None
    if _has_table(connection, "combination_attempt_executions"):
        owner = connection.execute(
            "SELECT e.execution_id,e.plan_json FROM combination_attempt_executions x "
            "JOIN combination_executions e ON e.run_id=x.run_id AND e.execution_id=x.execution_id "
            "WHERE x.run_id=? AND x.condition_id=? AND x.attempt_index=?",
            (run_id,row["condition_id"],row["attempt_index"])).fetchone()
        if owner is not None:
            execution_id, archived = owner
            plan = json.loads(archived)
    readings = {}
    for reading in connection.execute(
        "SELECT json_extract(r.value,'$.module') AS module,"
        "json_extract(r.value,'$.captured_at_utc') AS captured_at_utc,"
        "json_extract(r.value,'$.actual') AS actual,"
        "json_extract(r.value,'$.measurements') AS measurements,"
        "json_extract(r.value,'$.clean') AS clean "
        "FROM combination_samples s,json_each(s.payload_json,'$.reads') r "
        "WHERE s.run_id=? AND s.condition_id=? AND s.attempt_index=? AND s.sample_index=? "
        "AND json_extract(r.value,'$.module') IN ('temperature','magnetic','smu','lockin')",
        (run_id,row["condition_id"],row["attempt_index"],row["sample_index"]),
    ):
        readings[reading["module"]] = {
            "module":reading["module"], "captured_at_utc":reading["captured_at_utc"],
            "actual":json.loads(reading["actual"] or "{}"),
            "measurements":json.loads(reading["measurements"] or "{}"),
            "clean":bool(reading["clean"]) if reading["clean"] is not None else None}
    result = {key:row[key] for key in ("condition_id","attempt_index","sample_index",
              "attempt_status","started_at_utc","finished_at_utc")}
    return {**result,"captured_at_utc":row["finished_at_utc"],
            "context":json.loads(row["context_json"]), "execution_id":execution_id,
            "expected_samples":plan.get("samples_per_condition"),"modules":readings}


def run_snapshot(path: str | Path, run_id: str) -> dict:
    """Read an internally consistent snapshot; active is not a liveness claim."""
    with closing(open_readonly(path)) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT * FROM combination_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown combination run")
        accepted = connection.execute(
            "SELECT COUNT(*) FROM combination_attempts WHERE run_id=? AND status='accepted'",
            (run_id,),
        ).fetchone()[0]
        total = connection.execute(
            "SELECT COUNT(*) FROM combination_conditions WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        event = connection.execute(
            "SELECT * FROM combination_events WHERE run_id=? ORDER BY event_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT condition_id, attempt_index, status FROM combination_attempts "
            "WHERE run_id=? ORDER BY started_at_utc DESC, rowid DESC LIMIT 1", (run_id,),
        ).fetchone()
        last_confirmed = {}
        for raw in connection.execute(
            "SELECT payload_json FROM combination_events WHERE run_id=? "
            "AND event_type='raw_reading' ORDER BY event_id DESC LIMIT 4", (run_id,)
        ):
            reading = json.loads(raw[0])["reading"]
            last_confirmed.setdefault(reading["module"], reading)
        executions = []
        if _has_table(connection, "combination_executions"):
            for execution in connection.execute(
                "SELECT * FROM combination_executions WHERE run_id=? ORDER BY sequence_index",
                (run_id,),
            ):
                item = dict(execution)
                item.update({"plan": json.loads(item["plan_json"]),
                             "review": json.loads(item["review_json"]),
                             "cleanup": json.loads(item["cleanup_json"])
                             if item["cleanup_json"] else None})
                item["accepted_conditions"] = connection.execute(
                    "SELECT COUNT(*) FROM combination_attempts a "
                    "JOIN combination_attempt_executions x ON x.run_id=a.run_id "
                    "AND x.condition_id=a.condition_id AND x.attempt_index=a.attempt_index "
                    "WHERE a.run_id=? AND x.execution_id=? AND a.status='accepted'",
                    (run_id, item["execution_id"]),
                ).fetchone()[0]
                executions.append(item)
        # New executions have their own archived effective parameters. Never
        # relabel an earlier segment's phase/visit as this segment's preflight.
        plan = executions[-1]["plan"] if executions else json.loads(row["plan_json"])
        current_execution = executions[-1]["execution_id"] if executions else None
        if current_execution is not None:
            attempt = connection.execute(
                "SELECT a.condition_id,a.attempt_index,a.status FROM combination_attempts a "
                "JOIN combination_attempt_executions x ON x.run_id=a.run_id "
                "AND x.condition_id=a.condition_id AND x.attempt_index=a.attempt_index "
                "WHERE a.run_id=? AND x.execution_id=? "
                "ORDER BY a.started_at_utc DESC,a.rowid DESC LIMIT 1",
                (run_id,current_execution)).fetchone()
        from .monitor_progress import phase_snapshot, read_progress
        from .monitor_environment import read_environment
        phase_event = connection.execute(
            "SELECT created_at_utc,payload_json FROM combination_events WHERE run_id=? "
            "AND event_type='scan_phase' ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
        if phase_event is not None and current_execution is not None:
            try:
                payload = json.loads(phase_event["payload_json"])
            except (TypeError, ValueError):
                payload = None
            if not isinstance(payload, dict) or payload.get("execution_id") != current_execution:
                phase_event = None
        progress = read_progress(connection, run_id, plan, dict(attempt) if attempt else None)
        phase = phase_snapshot(dict(phase_event) if phase_event else None, row["status"],
                               dict(event) if event else None)
        environment = read_environment(connection, run_id, plan, progress, phase,
                                       execution_id=current_execution)
        timing_event = connection.execute(
            "SELECT created_at_utc,payload_json FROM combination_events WHERE run_id=? "
            "AND event_type='measurement_timing_summary' ORDER BY event_id DESC LIMIT 1",
            (run_id,)).fetchone()
        timing_summary = None
        if timing_event is not None:
            payload = json.loads(timing_event["payload_json"])
            if (isinstance(payload, dict) and (current_execution is None
                    or payload.get("execution_id") == current_execution)):
                timing_summary = {**payload, "recorded_at_utc": timing_event["created_at_utc"]}
        range_event = connection.execute(
            "SELECT event_type,created_at_utc,payload_json FROM combination_events WHERE run_id=? "
            "AND event_type IN ('lockin_range_selected','lockin_range_rule_apply_started',"
            "'lockin_range_rule_failed','lockin_range_rule_applied') "
            "ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
        last_range = ({"event_type": range_event["event_type"],
                       "captured_at_utc": range_event["created_at_utc"],
                       **json.loads(range_event["payload_json"])} if range_event else None)
        if (last_range is not None and executions
                and last_range["captured_at_utc"] < executions[-1]["created_at_utc"]):
            last_range = None
        if last_range is not None:
            cleanup_started = connection.execute(
                "SELECT 1 FROM combination_events WHERE run_id=? AND event_type='cleanup_started' "
                "AND created_at_utc>=? AND json_extract(payload_json,'$.module')='lockin' LIMIT 1",
                (run_id, last_range["captured_at_utc"])).fetchone()
            last_range["superseded_by_cleanup"] = cleanup_started is not None
        return {
            "run_id": run_id, "status": row["status"],
            "accepted_conditions": accepted, "total_conditions": total,
            "current_attempt": dict(attempt) if attempt else None,
            "progress": progress, "phase": phase, "environment": environment,
            "last_formal_measurement": _last_formal_measurement(connection, run_id, plan),
            "measurement_timing_summary": timing_summary,
            "cleanup": json.loads(row["cleanup_json"]) if row["cleanup_json"] else None,
            "error": row["error"], "process_liveness": "unknown",
            "last_recorded_readings": last_confirmed,
            **({"last_recorded_lockin_ranges": last_range} if range_event else {}),
            "last_event": {**dict(event), "payload": json.loads(event["payload_json"])}
                if event else None,
            "executions": executions,
            "current_execution_id": executions[-1]["execution_id"] if executions else None,
            "current_execution": executions[-1] if executions else None,
        }
