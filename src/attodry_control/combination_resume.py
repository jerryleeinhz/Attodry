"""File-only hardware recovery review. No resource, DLL or VISA is opened."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .combination_store import encode, open_readonly, recovery_parent_state, validate_accepted_records


# Reviewed 79ed5b4 source set, including the operator's schema-1 6/342 run.
# Both Git LF and the station's CRLF checkouts are deliberately supported.
LEGACY_SOURCE_SETS = {
    "3118322de0957fd5c76cb8f6cc419edba2cb8a3736f1e894ade7b05c098f4bb8",
    "8c29bd00d7b0549dc280143787e6f95b251860069f00d5efcf46ab74374efbd6",
}
RECOVERY_CONTRACT = "combination-condition-recovery-v1"


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def scan_contract(snapshot):
    """Exact ordered coordinates, including duplicate points and sample identity."""
    lockin = snapshot.get("hardware", {}).get("lockin", {})
    contract = {key: snapshot.get(key) for key in ("axes", "samples_per_condition", "repeats")} | {
        "mode": snapshot.get("mode"), "lockin_mode": snapshot.get("lockin_mode"),
        "harmonics_by_role": lockin.get("harmonics_by_role"),
        "skipped_harmonics_by_frequency": lockin.get("skipped_harmonics_by_frequency"),
    }
    return json.loads(encode(contract))


def _settings(snapshot):
    hardware = deepcopy(snapshot.get("hardware", {}))
    for module in ("temperature", "magnetic"):
        settings = hardware.get(module, {})
        if "temperature_scan" in settings and settings["temperature_scan"] is not None:
            settings["temperature_scan"] = {"points_k": settings["temperature_scan"]["points_k"]}
            settings.get("temperature_run", {}).pop("target_k", None)
        for key in ("pre_measure_wait_s", "interrupt_policy", "resume_recheck_s"):
            settings.get("temperature_run", {}).pop(key, None)  # unused by combination
        for key in ("run_name", "note", "output_directory", "segment_plan"):
            settings.get("run", {}).pop(key, None)
    hardware.get("effective_cryostat", {}).pop("project", None)
    lockin = hardware.get("lockin", {})
    for role in ("xx", "xy"):
        settings = lockin.get("lockin_" + role)
        if settings is None:
            continue
        overrides = {item["harmonic"]: item for item in settings.get("harmonic_settings", [])}
        fields = ("sensitivity_mode", "sensitivity_full_scale_v", "reserve_mode",
                  "autorange_min_full_scale_v", "autorange_max_full_scale_v",
                  "autorange_target_occupancy", "autorange_stable_samples", "autorange_full_scales_v")
        settings["harmonic_settings"] = {
            str(harmonic): {field: overrides.get(harmonic, {}).get(field, settings.get(field))
                            for field in fields}
            for harmonic in lockin.get("harmonics_by_role", {}).get(role, [])}
    sweep = hardware.get("lockin", {}).get("lockin_sweep", {})
    mode = snapshot.get("lockin_mode")
    for key in list(sweep):
        if key in {"run_name", "note", "output_directory", "samples_per_point"}:
            sweep.pop(key)
        elif key.endswith("_harmonics") or key == "skip_unsupported_harmonics":
            sweep.pop(key)  # resolved harmonics are frozen by scan_contract
        elif key in {"frequency_ranges", "excitation_ranges"}:
            sweep.pop(key)  # expanded specs below preserve per-point overrides
        elif mode == "excitation" and key.startswith("frequency_"):
            sweep.pop(key)
        elif mode == "frequency" and key.startswith("excitation_"):
            sweep.pop(key)
    return hardware


def _diff(before, after, path=()):
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            if key not in before or key not in after:
                yield path + (key,), before.get(key), after.get(key)
            else:
                yield from _diff(before[key], after[key], path + (key,))
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for index, (old, new) in enumerate(zip(before, after)):
            yield from _diff(old, new, path + (str(index),))
    elif before != after:
        yield path, before, after


def _allowed(path, old, new, hardware):
    leaf = path[-1]
    stability_fields = {"tolerance", "stable_range", "dwell_s", "minimum_samples",
                        "poll_interval_s", "wait_timeout_s"}
    if ("temperature_stability" in path or "stability" in path) and leaf in stability_fields:
        return "stability"
    if "magnet" in path and leaf == "readback_tolerance_t":
        return "stability"
    if path[:2] == ("magnetic", "run") and leaf == "max_step_t" and new <= old:
        return "transition_step"
    if path[:2] == ("temperature", "temperature_run") and leaf == "poll_interval_s":
        return "timing"
    if path[0] == "lockin":
        measurement = {"sensitivity_mode", "sensitivity_full_scale_v", "reserve_mode",
            "time_constant_s", "filter_slope_db_oct", "autorange_min_full_scale_v",
            "autorange_max_full_scale_v", "autorange_target_occupancy", "autorange_stable_samples"}
        if path[1] in {"lockin_xx", "lockin_xy"} and leaf in measurement:
            return "measurement"
        if "autorange_full_scales_v" in path:
            return "measurement"
        if path[1] == "lockin_sweep" and leaf in {
                "settle_time_constants", "sample_interval_time_constants", "overload_policy",
                "xx_full_scale_v", "xy_full_scale_v"}:
            return "measurement"
        if path[1] == "visa" and leaf == "timeout_ms":
            return "timing"
    if path[:2] == ("smu", "hardware"):
        device = hardware["smu"]["hardware"].get(path[2])
        if device and leaf == "nplc":
            return "measurement"
        counter = "max_abs_current_a" if device and device["source_mode"] == "voltage" else "max_abs_voltage_v"
        if device and leaf == counter:
            return "protection_increase" if new > old else "protection"
    if path == ("smu", "plan", "delay_s"):
        return "timing"
    return None


def configuration_changes(original, current):
    if scan_contract(original) != scan_contract(current):
        raise ValueError("Scan points/order/repeats/groups/harmonics changed; start a new run")
    changes = []
    hardware = _settings(current)
    for path, old, new in _diff(_settings(original), hardware):
        category = _allowed(path, old, new, hardware)
        if category is None:
            raise ValueError("Configuration change requires a new run: hardware." + ".".join(path))
        changes.append({"path": "hardware." + ".".join(path), "before": old,
                        "after": new, "category": category})
    return changes


def _cleanup_review(cleanup, modules=None):
    uncertain = cleanup.get("communication_uncertain") is not False
    unresolved = []
    actions = cleanup.get("actions", [])
    if not isinstance(actions, list):
        actions = []
        uncertain = True
    if modules is not None:
        for module in modules:
            if sum(isinstance(a, dict) and a.get("module") == module for a in actions) != 1:
                unresolved.append(module)
    if not actions:
        unresolved.append("missing cleanup actions")
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("result"), dict):
            unresolved.append("malformed cleanup action")
            continue
        result = action.get("result", {})
        verified = (result.get("verified") is True and not result.get("errors")) or (
            action.get("module") == "smu" and result.get("manual_verification_required") is False
            and not result.get("errors") and not result.get("communication_uncertain"))
        if not verified:
            unresolved.append(action.get("module", "unknown"))
        uncertain |= ("communication_uncertain" in result
                      and result["communication_uncertain"] is not False)
    return {"previous_clean": cleanup.get("clean") is True,
            "communication_uncertain": uncertain, "unresolved_modules": unresolved,
            "fresh_hardware_preflight_required": True,
            "manual_verification_required": uncertain or bool(unresolved)}


def build_recovery(config, database, run_id, *, acknowledge_failure=False,
                   manual_verification=False, approve_protection_changes=False):
    """Return an immutable-boundary review without modifying the database."""
    database = Path(database).resolve()
    if not run_id or run_id == "auto":
        raise ValueError("Recovery requires the exact original run ID")
    if (config.plan.samples_per_condition != config.snapshot["samples_per_condition"]
            or config.plan.repeats != config.snapshot["repeats"]):
        raise ValueError("Plan groups/repeats differ from its configuration snapshot")
    with closing(open_readonly(database)) as connection:
        connection.execute("BEGIN")
        state = recovery_parent_state(connection, run_id)
        original = json.loads(state["run"]["plan_json"])
        if state["run"]["schema_version"] != 1 or original.get("mode") != "hardware":
            raise ValueError("Unsupported legacy run/schema; hardware recovery refused")
        if state["run"]["status"] not in {"failed", "interrupted"}:
            raise ValueError("Recovery requires a terminal failed/interrupted run")
        if any(a["status"] == "started" for a in state["attempts"]):
            raise ValueError("Orphaned/started attempt requires manual owner review; recovery refused")
        if any(e["status"] == "active" for e in state["executions"]):
            raise ValueError("An execution remains active; recovery refused")
        if not state["conditions"]:
            raise ValueError("Archived conditions are missing")
        if state["conditions"] != config.plan.conditions():
            raise ValueError("Ordered condition records differ; start a new run")
        # A new implementation can accept only the reviewed schema-1 source set
        # or exactly its own source. Unknown code changes never bypass review.
        parent = (json.loads(state["executions"][-1]["plan_json"])
                  if state["executions"] else original)
        old_sources, new_sources = parent.get("source_sha256"), config.snapshot.get("source_sha256")
        if not old_sources or (old_sources != new_sources and digest(old_sources) not in LEGACY_SOURCE_SETS):
            raise ValueError("Archived source version has no reviewed recovery compatibility")
        changes = configuration_changes(parent, config.snapshot)
        accepted = set(state["accepted_condition_ids"])
        flags = [c["condition_id"] in accepted for c in state["conditions"]]
        first = next((i for i, flag in enumerate(flags) if not flag), len(flags))
        if any(flags[first:]):
            raise ValueError("Accepted conditions are not a contiguous prefix")
        # The same raw-evidence validator runs again inside the atomic DB claim.
        validate_accepted_records(connection, run_id, original)
        cleanup = json.loads(state["run"]["cleanup_json"] or "{}")
        cleanup_review = _cleanup_review(cleanup, [a["module"] for a in original["axes"]])
        blockers = []
        if not acknowledge_failure:
            blockers.append("Review original failure/cleanup and supply --acknowledge-failure")
        if cleanup_review["manual_verification_required"] and not manual_verification:
            blockers.append("Resolve unknown cleanup physically, then supply --confirm-manual-verification")
        if any(c["category"] == "protection_increase" for c in changes) and not approve_protection_changes:
            blockers.append("Approve the displayed SMU protection increase with --approve-protection-changes")
        review = {"contract": RECOVERY_CONTRACT, "acknowledged_failure": bool(acknowledge_failure),
            "manual_verification": bool(manual_verification),
            "protection_changes_approved": bool(approve_protection_changes),
            "configuration_changes": changes, "cleanup_review": cleanup_review,
            "original_error": state["run"]["error"], "ready": not blockers,
            "blockers": blockers, "config_sha256": config.snapshot["config_sha256"],
            "scan_contract_sha256": digest(scan_contract(original)),
            "source_compatibility": "reviewed-79ed5b4" if old_sources != new_sources else "unchanged"}
        return {"expected_parent_state_sha256": state["state_sha256"],
            "original_conditions": state["conditions"], "review": review,
            "next_condition": None if first == len(flags) else state["conditions"][first],
            "cleanup_only": first == len(flags), "accepted_conditions": first,
            "total_conditions": len(flags), "remaining_conditions": len(flags) - first,
            "last_condition": state["conditions"][-1]}


def recovery_text(recovery, database, run_id):
    review = recovery["review"]
    next_condition = recovery["next_condition"]
    lines = ["Hardware recovery review (files only)", f"Run ID: {run_id}",
        f"Database: {database}", f"Accepted: {recovery['accepted_conditions']}/{recovery['total_conditions']}",
        "Next: " + (next_condition["condition_id"] + " | " + str(next_condition["requested"])
                    if next_condition else "cleanup only; no formal acquisition"),
        "Original error: " + str(review["original_error"])]
    for change in review["configuration_changes"]:
        lines.append(f"{change['category']}: {change['path']}: {change['before']} -> {change['after']}")
    lines.extend("Required: " + reason for reason in review["blockers"])
    lines.append("File review ready: " + str(review["ready"]) + "; fresh exclusive hardware preflight required")
    return "\n".join(lines)
