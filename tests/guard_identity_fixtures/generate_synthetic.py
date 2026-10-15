"""Build deterministic, fabricated guard-history cases without real audit input.

Run this script to regenerate the adjacent JSON. Every identity, timestamp and
measurement is made up. The integer 18612 is required by the existing narrow
Windows-reuse contract; it does not identify any real process in these fixtures.
No instrument, process inventory, original experiment or network is accessed.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from xml.sax.saxutils import escape


RUN = "synthetic-process-guard-removal-342"
EXECUTION = "execution-synthetic-startup"
GUARD_ERROR = "ValueError: Prior/duplicate owner present"
ANALYSIS_ERROR = "ValueError: Analysis owner identities changed during recovery"
BASE = datetime(2020, 1, 2, 12, tzinfo=timezone.utc)


def utc(seconds=0):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def digest(label):
    return hashlib.sha256(("synthetic fixture: " + label).encode()).hexdigest()


def state():
    return {"error_code": 0, "error_message": "",
        "field": {"bx_t": 0.0, "bz_t": 0.0},
        "field_setpoint": {"bx_t": 0.0, "bz_t": 0.0},
        "temperature_control_enabled": True, "field_control_enabled": False,
        "sample_temperature_k": 12.5, "user_temperature_k": 12.5,
        "vti_temperature_k": 6.25}


def diagnostic(role):
    xx = role == "xx"
    return {"role": role, "identity": "Synthetic,SR830,FAKE-" + role.upper() + ",TEST",
        "amplitude_v": .00025 if xx else .0005,
        "x_v": .00025 if xx else .0005, "y_v": 0.0,
        "phase_deg": 0.0, "phase_shift_deg": 0.0,
        "frequency_hz": 200.0, "snapshot_frequency_hz": 200.0,
        "sine_output_v": .004, "harmonic": 1, "input_coupling": 0,
        "input_mode": 1, "shield_grounding": 0, "line_filter": 0,
        "filter_slope": 3, "time_constant": 10,
        "reference_mode": 1 if xx else 0, "reference_slope": 0 if xx else 1,
        "sensitivity": 26 if xx else 23, "reserve_mode": 2 if xx else 0,
        "error_status": 0, "lia_status": {"raw": 0, **{flag: False for flag in (
            "filter_overload", "frequency_range_changed", "input_or_reserve_overload",
            "output_overload", "reference_unlocked", "time_constant_changed", "triggered")}}}


def summary():
    return {"completion_message": "failed", "continued_overload_pairs": 0,
        "data_quality": "no_overload_recorded", "formal_pairs": 74,
        "overloaded_pairs_by_role": {"lockin_xx": 0, "lockin_xy": 0},
        "rejected_attempt_audit": {"formal_pairs": 0, "continued_overload_pairs": 0,
            "data_quality": "no_overload_recorded",
            "overloaded_pairs_by_role": {"lockin_xx": 0, "lockin_xy": 0}}}


def seq4():
    final = {"lockin_" + role: diagnostic(role) for role in ("xx", "xy")}
    actions = [{"module": "lockin", "audit_errors": [], "clean": False,
        "failure_requires_manual_review": True, "startup_completed": False,
        "result": {"attempted": True, "verified": True, "errors": [], "final": final}},
        {"module": "temperature", "audit_errors": [], "clean": False,
         "failure_requires_manual_review": True, "startup_completed": False,
         "result": {"attempted": False, "reason": "no writes attempted", "verified": True}}]
    cleanup = {"actions": actions, "clean": False, "communication_uncertain": False,
        "manual_verification_required": True,
        "errors": [module + ": cleanup not certified; review recorded actions"
                   for module in ("lockin", "temperature")]}
    frame = {"captured_at_utc": utc(4), "sample_id": "synthetic-frame-1",
        "state": state(), "heater_power": {"sample_w": .125, "vti_w": .25},
        "stage": "adopted_preflight", "telemetry_complete": True, "telemetry_version": 1}
    payloads = [
        ("launch_registered", {"pid": 4100, "run_id": RUN, "hostname": "SYNTHETIC-STATION",
            "config": "C:/Synthetic/config/local.toml", "database": "C:/Synthetic/audit.sqlite",
            "created_at_utc": utc()}),
        ("run_started", {"resume": True, "cleanup_only": False}),
        ("cryostat_state", {**deepcopy(frame), "heater_power": {"sample_w": None, "vti_w": None},
            "telemetry_complete": False}),
        ("temperature_telemetry", deepcopy(frame)),
        ("cryostat_connection_adopted", {"actual_resume_ack_verified": False,
            "begin_or_connect_replayed": False, "captured_at_utc": utc(8), "connected": True,
            "connection_owned": True, "existing_driver_object_id": 101,
            "initial_driver_object_id": 101, "fresh_preflight": deepcopy(frame), "pid": 4100,
            "qualification_context_reset": True, "same_exclusive_lease": True, "status": "adopted"}),
        ("recovery_environment_initial", {"state": state()}),
    ]
    def raw(role, method="read_diagnostic"):
        return ("lockin_raw_role", {"role": "lockin_" + role, "method": method,
            "reading": diagnostic(role)})
    payloads.extend([raw("xx"), raw("xy"),
        ("lockin_recovery_baseline", {"roles": deepcopy(final), "problems": []}),
        ("cleanup_started", {"module": "lockin"})])
    def command(method, role, arguments):
        payloads.extend([("lockin_command_attempt", {"method": method, "role": role, "arguments": arguments}),
            ("lockin_command_return", {"method": method, "role": role, "reading": None})])
    command("set_minimum_sine_output", "lockin_xx", [])
    command("set_reserve_mode", "lockin_xx", [2])
    command("set_reserve_mode", "lockin_xy", [0])
    payloads.extend([raw("xx", "read_harmonic_sample"), raw("xy", "read_harmonic_sample")])
    command("set_sensitivity", "lockin_xx", [26])
    command("set_sensitivity", "lockin_xy", [23])
    payloads.extend([raw("xx", "read_harmonic_sample"), raw("xy", "read_harmonic_sample"),
        raw("xx"), raw("xy"), ("cleanup_result", deepcopy(actions[0])),
        ("cleanup_started", {"module": "temperature"}), ("cleanup_result", deepcopy(actions[1])),
        ("run_finished", {"status": "failed", "error": GUARD_ERROR, "cleanup": deepcopy(cleanup)}),
        ("data_quality_summary", summary())])
    events = [{"event_id": 1001 + i, "created_at_utc": utc(i * 2), "event_type": name,
               "payload": {"execution_id": EXECUTION, **payload}}
              for i, (name, payload) in enumerate(payloads)]
    return {"synthetic_fixture": True, "utc": utc(65), "events": events,
        "execution": {"execution_id": EXECUTION, "sequence_index": 4, "status": "failed",
            "error": GUARD_ERROR, "finished_at_utc": utc(58),
            "parent_state_sha256": digest("parent"), "cleanup": cleanup}}


def windows():
    # The exact inventory command is a production contract, not a station identity.
    command = ("powershell.exe -NoProfile -NonInteractive -Command "
        "@(Get-CimInstance Win32_Process | ForEach-Object { @{pid=[int]$_.ProcessId; "
        "parent=[int]$_.ParentProcessId;name=$_.Name;command=$_.CommandLine;executable=$_.ExecutablePath;"
        "created=$_.CreationDate.ToUniversalTime().ToString('o')}}) | ConvertTo-Json -Compress")
    activity = "{11111111-2222-3333-4444-555555555555}"
    host = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    runspace = "11111111-aaaa-bbbb-cccc-222222222222"
    def xml(event_id, seconds, record_id, pid, provider, channel, data, correlated=False):
        fields = "".join("<Data>" + escape(str(value)) + "</Data>" for value in data)
        return ("<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
            f"<Provider Name='{provider}'/><EventID>{event_id}</EventID>"
            f"<TimeCreated SystemTime='{utc(seconds)}'/><EventRecordID>{record_id}</EventRecordID>"
            + (f"<Correlation ActivityID='{activity}'/>" if correlated else "<Correlation/>")
            + f"<Execution ProcessID='{pid}' ThreadID='1'/><Channel>{channel}</Channel>"
            "<Computer>SYNTHETIC-STATION</Computer><Security UserID='S-1-5-21-1-2-3-1000'/>"
            "</System><EventData>" + fields + "</EventData></Event>")
    operational = [xml(eid, sec, rec, 18612, "Microsoft-Windows-PowerShell",
        "Microsoft-Windows-PowerShell/Operational", data, True) for eid, sec, rec, data in (
            (40961, 17, 901, []), (53504, 17.1, 902, [18612, "DefaultAppDomain"]),
            (40962, 17.3, 903, []))]
    legacy = []
    for eid, seconds, record_id, states in ((400, 17.2, 501, ["Available", "None"]),
            (403, 17.4, 502, ["Stopped", "Available"])):
        fields = (f"HostName=ConsoleHost\nHostApplication={command}\nHostId={host}\nRunspaceId={runspace}")
        legacy.append(xml(eid, seconds, record_id, 0, "PowerShell", "Windows PowerShell", [*states, fields]))
    return {"synthetic_fixture": True, "version": 1, "kind": "windows-powershell-pid-reuse",
        "recorded_at_utc": utc(65), "run_id": RUN, "failed_execution_id": EXECUTION,
        "historical_pid": 18612, "historical_identity": {"pid": 18612, "parent": 3000,
            "name": "python.exe", "created": utc(-3600), "observed_utc": utc(-3599), "run_id": RUN,
            "command": '"C:/Users/Synthetic/python.exe" "C:/Synthetic/resume.py" --fixture',
            "operation_dir": "C:/Synthetic/retired/operation_001", "manifest_sha256": digest("manifest")},
        "operational_xml": operational, "legacy_xml": legacy, "cleanup_started_at_utc": utc(18),
        "primary_error": GUARD_ERROR, "full_failed_cim_snapshot_available": False,
        "process_parent_known": False, "hardware_state_inferred": False}


def seq5():
    execution = "execution-synthetic-analysis-failure"
    attribution = {"execution_id": execution, "condition_id": "condition-000037", "attempt_index": 0}
    actions = []
    for module in ("lockin", "temperature"):
        last = ({"preflight": [diagnostic("xx"), diagnostic("xy")], "point_record": {
            "source_v_rms": .25, "source_readback_v_rms": .25, "frequency_hz": 200.0,
            "samples": []}} if module == "lockin" else state())
        actions.append({"module": module, "audit_errors": [], "clean": False,
            "failure_requires_manual_review": True, "startup_completed": True,
            "result": {"attempted": False, "verified": False, "communication_uncertain": True,
                "manual_verification_required": True, "owner_guard_error": ANALYSIS_ERROR,
                "last_confirmed_state_not_current": last}})
    cleanup = {"actions": actions, "clean": False, "communication_uncertain": True,
        "manual_verification_required": True, "errors": [module + ": cleanup not certified; review recorded actions"
            for module in ("lockin", "temperature")]}
    rows = [("recovery_owner_verification_failed", {"error": ANALYSIS_ERROR,
                "further_instrument_writes_blocked": True, "hardware_zero_or_off_confirmed": False}),
        ("environment_after_read_failed", {"error": ANALYSIS_ERROR.removeprefix("ValueError: ")}),
        ("cleanup_started", {"module": "lockin"}), ("cleanup_result", actions[0]),
        ("cleanup_started", {"module": "temperature"}), ("cleanup_result", actions[1]),
        ("run_finished", {"status": "failed", "error": ANALYSIS_ERROR, "cleanup": cleanup}),
        ("data_quality_summary", summary())]
    events = [{"event_id": 2001 + i, "created_at_utc": utc(100 + 2 * i), "event_type": name,
        "payload_json": canonical(({"execution_id": execution} if name == "run_finished" else attribution) | payload)}
        for i, (name, payload) in enumerate(rows)]
    return {"synthetic_fixture": True, "kind": "synthetic-seq5-terminal-events", "run_id": RUN,
        "source": "Generated in generate_synthetic.py; no real experiment was read", "events": events}


if __name__ == "__main__":
    directory = Path(__file__).parent
    for name, value in (("seq4.json", seq4()), ("windows.json", windows()),
            ("seq5-analysis-owner-terminal-events.json", seq5())):
        (directory / name).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
