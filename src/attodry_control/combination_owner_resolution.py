"""Ephemeral live evidence for narrowly classified ownership/startup failures.

Archived uncertain cleanup stays unchanged. A JSON certificate, CLI flag, or
failure string alone cannot create the live lease/readback capability below.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from .combination_ownership import RecoveryOwner
from .combination_store import encode
from .sr830 import RESERVE_MODE_CODES
from .sr830_settings import map_sr830_settings


CONTRACT = "live-owner-guard-resolution-v1"
INITIALISATION_CONTRACT = "live-initialisation-resolution-v1"
INITIALISATION_ERROR = "AttoDryDllError: isDeviceInitialised returned DLL error code 1556."
GUARD_IDENTITY_CONTRACT = "live-guard-identity-resolution-v1"
GUARD_IDENTITY_ERROR = "ValueError: Prior/duplicate owner present"
PROCESS_GUARD_REMOVAL_CONTRACT = "live-process-guard-removal-resolution-v1"
PROCESS_GUARD_REMOVAL_ERROR = "ValueError: Analysis owner identities changed during recovery"
MAX_AGE_S = 60.0
# A complete sequential cryostat/dual-SR830 frame can take close to one minute.
# Allow review and atomic claim under the same lease, while every observation
# independently retains the stricter 60-second freshness requirement.
CAP_MAX_AGE_S = 600.0
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_OWNER_ERROR = re.compile(r"ValueError: Competing or unknown Python owner: PID [1-9][0-9]*\Z")


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("Live owner resolution needs exact SHA256 bindings")
    return value


def _digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def _utc(value):
    if not isinstance(value, str):
        raise ValueError("Live owner resolution needs a known UTC timestamp")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise ValueError("Live owner resolution needs a known UTC timestamp") from exc
    return result


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _sources(old, new):
    if (not isinstance(old, dict) or not isinstance(new, dict) or len(old) != 79 or len(new) != 80
            or set(new) != set(old) | {"combination_owner_resolution.py"}):
        raise ValueError("Live owner resolution permits only the pinned 79-to-80 source transition")
    for name, value in [*old.items(), *new.items()]:
        if not isinstance(name, str) or not name.endswith(".py") or "/" in name or "\\" in name:
            raise ValueError("Live owner source map is malformed")
        _sha(value)
    changed = {name for name in old if old[name] != new[name]}
    if changed != {"combination_resume.py", "combination_store.py"}:
        raise ValueError("Live owner source transition changes unrelated implementation")


def _initialisation_sources(old, new):
    if (not isinstance(old, dict) or not isinstance(new, dict) or len(old) != 80
            or set(old) != set(new) or "combination_owner_resolution.py" not in old):
        raise ValueError("Initialisation resolution permits only a pinned 80-to-80 source transition")
    for name, value in [*old.items(), *new.items()]:
        if not isinstance(name, str) or not name.endswith(".py") or "/" in name or "\\" in name:
            raise ValueError("Initialisation source map is malformed")
        _sha(value)
    if {name for name in old if old[name] != new[name]} != {
            "combination_resume.py", "combination_store.py", "combination_owner_resolution.py"}:
        raise ValueError("Initialisation resolution changes unrelated implementation")


def _initialisation_references(evidence):
    names = {"operator_manifest": "manifest.json", "operator_result": "operator-result.json",
             "operator_progress": "operator_progress.jsonl", "machine_claim": "machine-claim-proof.json"}
    expected = {prefix + suffix for prefix in names for suffix in ("_path", "_sha256")}
    if not isinstance(evidence, dict) or set(evidence) != expected:
        raise ValueError("Initialisation resolution needs exact frozen startup evidence references")
    parents = set()
    for prefix, filename in names.items():
        path = Path(evidence[prefix + "_path"])
        if not path.is_absolute() or path.name != filename:
            raise ValueError("Initialisation evidence path is not the exact absolute artifact")
        _sha(evidence[prefix + "_sha256"])
        parents.add(path.parent)
    if len(parents) != 1:
        raise ValueError("Initialisation evidence must belong to one frozen prior operator")
    return deepcopy(evidence)


def _guard_identity_references(evidence):
    names = {"operator_manifest": "manifest.json", "operator_result": "operator-result.json",
             "operator_progress": "operator_progress.jsonl", "machine_claim": "machine-claim-proof.json",
             "adoption_proof": "connection-adoption-proof.json", "baseline_proof": "live-baseline-proof.json",
             "canonical_apply": "canonical-apply-result.json", "windows_event_proof": "windows-pid-reuse-proof.json",
             "historical_identity": "launch-identity.json"}
    if not isinstance(evidence, dict) or set(evidence) != {
            prefix + suffix for prefix in names for suffix in ("_path", "_sha256")}:
        raise ValueError("Guard identity resolution needs exact frozen machine evidence references")
    parents = set()
    for prefix, filename in names.items():
        path = Path(evidence[prefix + "_path"])
        if not path.is_absolute() or path.name != filename:
            raise ValueError("Guard identity evidence path is not the exact absolute artifact")
        _sha(evidence[prefix + "_sha256"])
        if prefix not in {"windows_event_proof", "historical_identity"}:
            parents.add(path.parent)
    if len(parents) != 1:
        raise ValueError("Guard identity operator evidence belongs to more than one prior attempt")
    return deepcopy(evidence)


def _windows_pid_reuse(proof, recorded_history, manifest, run_id, execution_id, cleanup_started_at):
    """Verify native archived records without inventing a missing CIM snapshot."""
    if (not isinstance(proof, dict) or proof.get("version") != 1
            or proof.get("kind") != "windows-powershell-pid-reuse" or proof.get("run_id") != run_id
            or proof.get("failed_execution_id") != execution_id or proof.get("primary_error") != GUARD_IDENTITY_ERROR
            or type(proof.get("historical_pid")) is not int or proof["historical_pid"] != 18612
            or proof.get("full_failed_cim_snapshot_available") is not False
            or proof.get("process_parent_known") is not False or proof.get("hardware_state_inferred") is not False
            or _utc(proof.get("cleanup_started_at_utc")) != _utc(cleanup_started_at)
            or not isinstance(proof.get("operational_xml"), list) or len(proof["operational_xml"]) != 3
            or not isinstance(proof.get("legacy_xml"), list) or len(proof["legacy_xml"]) != 2):
        raise ValueError("Guard identity resolution requires exact native Windows event evidence")
    history = proof.get("historical_identity")
    if (not isinstance(history, dict) or history.get("pid") != 18612 or history.get("name") != "python.exe"
            or history.get("run_id") != run_id or not isinstance(history.get("operation_dir"), str)):
        raise ValueError("Guard identity historical owner is not the frozen Python instance")
    if history != recorded_history:
        raise ValueError("Guard identity historical identity differs from the independently pinned launch record")
    state_path = Path(history["operation_dir"]).parent / "state.json"
    wanted = manifest.get("historical_evidence_sha256", {}).get(str(state_path).replace("\\", "/"))
    try:
        raw_state = state_path.read_bytes()
        state = json.loads(raw_state.decode("utf-8-sig"))
        if hashlib.sha256(raw_state).hexdigest() != _sha(wanted):
            raise ValueError("Guard identity original stability state changed")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Guard identity original stability state is unavailable") from exc
    if (type(state.get("adjustments_used")) is not int or state["adjustments_used"] != 1
            or not isinstance(state.get("history"), list) or len(state["history"]) != 1
            or type(state["history"][0].get("adjustment_number")) is not int or state["history"][0]["adjustment_number"] != 1
            or Path(state["history"][0].get("operation_dir", "")).resolve() != Path(history["operation_dir"]).resolve()
            or state["history"][0].get("old_stable_range_k") != .05 or state["history"][0].get("new_stable_range_k") != .1):
        raise ValueError("Guard identity historical launch does not belong to the frozen authorized stability operation")
    namespace = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    parsed = {}
    for group, provider, channel, ids in (("operational_xml", "Microsoft-Windows-PowerShell",
            "Microsoft-Windows-PowerShell/Operational", {40961, 53504, 40962}),
            ("legacy_xml", "PowerShell", "Windows PowerShell", {400, 403})):
        seen = set()
        for xml in proof[group]:
            try:
                document = ET.fromstring(xml)
                system = document.find("e:System", namespace)
                event_id = int(system.find("e:EventID", namespace).text)
                row = {"event_id": event_id,
                    "record_id": int(system.find("e:EventRecordID", namespace).text),
                    "pid": int(system.find("e:Execution", namespace).get("ProcessID")),
                    "utc": system.find("e:TimeCreated", namespace).get("SystemTime"),
                    "computer": system.find("e:Computer", namespace).text,
                    "activity": system.find("e:Correlation", namespace).get("ActivityID"),
                    "data": [element.text or "" for element in document.findall("e:EventData/e:Data", namespace)]}
                if (system.find("e:Provider", namespace).get("Name") != provider
                        or system.find("e:Channel", namespace).text != channel or event_id not in ids
                        or event_id in seen or row["record_id"] <= 0 or not row["computer"]):
                    raise ValueError
                _utc(row["utc"])
            except (ET.ParseError, AttributeError, TypeError, ValueError) as exc:
                raise ValueError("Guard identity native Windows XML is malformed or wrong scope") from exc
            seen.add(event_id)
            parsed[event_id] = row
        if seen != ids:
            raise ValueError("Guard identity native Windows event pair is incomplete")
    if (any(parsed[key]["pid"] != 18612 for key in (40961, 53504, 40962))
            or any(parsed[key]["pid"] != 0 for key in (400, 403))
            or len({row["computer"] for row in parsed.values()}) != 1
            or len({parsed[key]["activity"] for key in (40961, 53504, 40962)}) != 1
            or not parsed[40961]["activity"] or parsed[53504]["data"] != ["18612", "DefaultAppDomain"]
            or [parsed[key]["record_id"] for key in (40961, 53504, 40962)] != list(range(
                parsed[40961]["record_id"], parsed[40961]["record_id"] + 3))
            or parsed[403]["record_id"] != parsed[400]["record_id"] + 1):
        raise ValueError("Guard identity native Windows PID/activity/record linkage differs")
    inventory_command = ("powershell.exe -NoProfile -NonInteractive -Command "
        "@(Get-CimInstance Win32_Process | ForEach-Object { @{pid=[int]$_.ProcessId; "
        "parent=[int]$_.ParentProcessId;name=$_.Name;command=$_.CommandLine;executable=$_.ExecutablePath;"
        "created=$_.CreationDate.ToUniversalTime().ToString('o')}}) | ConvertTo-Json -Compress")
    hosts = []
    for event_id, state in ((400, ["Available", "None"]), (403, ["Stopped", "Available"])):
        data = parsed[event_id]["data"]
        if len(data) != 3 or data[:2] != state:
            raise ValueError("Guard identity legacy PowerShell engine lifecycle differs")
        text = data[2].replace("\\r\\n", "\n").replace("\\t", "\t").replace("\r\n", "\n")
        fields = dict(re.findall(r"(?m)^\s*([A-Za-z]+)=(.*)$", text))
        if (fields.get("HostApplication") != inventory_command or fields.get("HostName") != "ConsoleHost"
                or not re.fullmatch(r"[0-9a-f-]{36}", fields.get("HostId", ""))
                or not re.fullmatch(r"[0-9a-f-]{36}", fields.get("RunspaceId", ""))):
            raise ValueError("Guard identity Windows HostApplication is not the exact read-only inventory command")
        hosts.append((fields["HostId"], fields["RunspaceId"]))
    times = [_utc(parsed[key]["utc"]) for key in (40961, 53504, 400, 40962, 403)]
    cleanup_time = _utc(cleanup_started_at)
    if (hosts[0] != hosts[1] or times != sorted(times) or not _utc(history.get("created")) < times[0]
            or not 0 <= (cleanup_time - times[-1]).total_seconds() <= 1.0
            or not 0 <= (cleanup_time - times[0]).total_seconds() <= 2.0
            or _utc(proof.get("recorded_at_utc")) < cleanup_time):
        raise ValueError("Guard identity native Windows PID reuse timeline does not bind the failure")
    return {"historical_pid": 18612, "historical_identity": deepcopy(history),
        "native_process_name": "powershell.exe", "host_application": inventory_command,
        "host_id": hosts[0][0], "windows_records": [{key: row[key] for key in
            ("event_id", "record_id", "pid", "utc", "computer")} for row in parsed.values()],
        "full_failed_cim_snapshot_available": False, "process_parent_known": False,
        "hardware_state_inferred": False}


def validate_archived_initialisation_evidence(connection, run_id, failed_execution_id, config, old_sources, evidence):
    """Pure historical classification; frozen files alone never authorize resume."""
    evidence = _initialisation_references(evidence)
    records = {}
    for prefix in ("operator_manifest", "operator_result", "operator_progress", "machine_claim"):
        try:
            raw = Path(evidence[prefix + "_path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != evidence[prefix + "_sha256"]:
                raise ValueError("Frozen initialisation evidence changed")
            text = raw.decode("utf-8-sig")
            records[prefix] = ([json.loads(line) for line in text.splitlines() if line.strip()]
                               if prefix == "operator_progress" else json.loads(text))
            encode(records[prefix])  # Refuse nonfinite numbers anywhere in the evidence.
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Initialisation evidence is unavailable or malformed") from exc
    row = connection.execute("SELECT * FROM combination_executions WHERE run_id=? AND execution_id=?",
                             (run_id, failed_execution_id)).fetchone()
    if row is None:
        raise ValueError("Initialisation failed execution is absent")
    execution = dict(row)
    plan, review = json.loads(execution["plan_json"]), json.loads(execution["review_json"])
    cleanup = json.loads(execution["cleanup_json"] or "{}")
    if (execution["sequence_index"] != 3 or execution["status"] != "failed" or execution["error"] != INITIALISATION_ERROR
            or not execution["finished_at_utc"] or plan.get("source_sha256") != old_sources
            or plan.get("config_sha256") != config.snapshot["config_sha256"]
            or {axis["module"] for axis in plan["axes"]} != {"temperature", "lockin"}
            or review.get("manual_verification") is not False):
        raise ValueError("Initialisation evidence does not bind the exact preflight execution/source/config")
    expected_cleanup = {"actions": [
        {"audit_errors": [], "clean": False, "failure_requires_manual_review": True, "module": "lockin",
         "result": {"attempted": False, "reason": "not opened"}, "startup_completed": False},
        {"audit_errors": [], "clean": False, "failure_requires_manual_review": True, "module": "temperature",
         "result": {"attempted": False, "reason": "no writes attempted", "verified": True}, "startup_completed": False}],
        "clean": False, "communication_uncertain": False,
        "errors": ["lockin: cleanup not certified; review recorded actions", "temperature: cleanup not certified; review recorded actions"],
        "manual_verification_required": True}
    if cleanup != expected_cleanup:
        raise ValueError("Initialisation cleanup is not the exact unattempted startup failure")
    if connection.execute("SELECT COUNT(*) FROM combination_attempt_executions WHERE run_id=? AND execution_id=?",
                          (run_id, failed_execution_id)).fetchone()[0] != 0:
        raise ValueError("Initialisation resolution refuses any opened attempt/acquisition")
    events = []
    for row in connection.execute("SELECT * FROM combination_events WHERE run_id=? ORDER BY event_id", (run_id,)):
        event = dict(row)
        payload = json.loads(event["payload_json"])
        if payload.get("execution_id") == failed_execution_id:
            events.append((event, payload))
    expected_kinds = ["launch_registered", "run_started", "cleanup_started", "cleanup_result",
                      "cleanup_started", "cleanup_result", "run_finished", "data_quality_summary"]
    if [event["event_type"] for event, payload in events] != expected_kinds:
        raise ValueError("Initialisation resolution refuses preflight acknowledgement, attempts, or extra fault events")
    if (events[1][1].get("plan") != plan or events[1][1].get("resume") is not True
            or events[1][1].get("cleanup_only") is not False
            or events[2][1].get("module") != "lockin" or events[4][1].get("module") != "temperature"):
        raise ValueError("Initialisation raw startup plan/cleanup boundary differs")
    for index, action in ((3, expected_cleanup["actions"][0]), (5, expected_cleanup["actions"][1])):
        payload = {key: value for key, value in events[index][1].items() if key != "execution_id"}
        if payload != action:
            raise ValueError("Initialisation raw cleanup result differs")
    if (events[6][1].get("status") != "failed" or events[6][1].get("error") != INITIALISATION_ERROR
            or events[6][1].get("cleanup") != cleanup
            or events[7][1].get("rejected_attempt_audit", {}).get("formal_pairs") != 0):
        raise ValueError("Initialisation raw terminal/acquisition evidence differs")
    manifest, result, claim = records["operator_manifest"], records["operator_result"], records["machine_claim"]
    if (manifest.get("kind") != "laser_owner_proof_backed_resume" or manifest.get("run_id") != run_id
            or manifest.get("runtime_source_sha256") != old_sources
            or manifest.get("config_sha256") != config.snapshot["config_sha256"]
            or result.get("run_id") != run_id or result.get("manifest_sha256") != evidence["operator_manifest_sha256"]
            or result.get("cli_exit_code") != 2 or result.get("error") is not None or result.get("audit_errors") != []
            or result.get("historical_cleanup_rewritten") is not False or result.get("manual_verification_asserted") is not False
            or result.get("result", {}).get("execution_id") != failed_execution_id
            or result["result"].get("status") != "failed" or result["result"].get("error") != INITIALISATION_ERROR
            or result["result"].get("cleanup") != cleanup):
        raise ValueError("Initialisation operator result contains another failure or wrong scope")
    proof = review.get("live_owner_resolution")
    canonical = claim.get("proof_canonical_json")
    if (not isinstance(proof, dict) or proof.get("contract") != CONTRACT or not isinstance(canonical, str)
            or json.loads(canonical) != proof or _digest(proof) != claim.get("machine_proof_sha256")
            or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != claim.get("machine_proof_sha256")
            or claim.get("run_id") != run_id or claim.get("execution_id") != failed_execution_id
            or claim.get("sequence_index") != 3 or claim.get("config_sha256") != plan["config_sha256"]
            or claim.get("source_sha256") != old_sources or claim.get("historical_cleanup_rewritten") is not False
            or claim.get("actual_resume_ack_verified") is not False
            or proof.get("parent_state_sha256") != execution["parent_state_sha256"]
            or proof.get("new_source_sha256") != old_sources or proof.get("config_sha256") != plan["config_sha256"]):
        raise ValueError("Initialisation prior live owner-resolution claim is not intact")
    if _digest(proof.get("issued_proof")) != proof.get("issued_proof_sha256"):
        raise ValueError("Initialisation prior issued proof digest differs")
    previous = connection.execute("SELECT * FROM combination_executions WHERE run_id=? AND sequence_index=2", (run_id,)).fetchone()
    if (previous is None or proof.get("failed_execution_id") != previous["execution_id"]
            or proof.get("old_source_sha256") != json.loads(previous["plan_json"]).get("source_sha256")
            or previous["status"] != "failed" or proof.get("expected_primary_error") != previous["error"]
            or not _OWNER_ERROR.fullmatch(previous["error"] or "")):
        raise ValueError("Initialisation prior owner-resolution source lineage differs")
    previous_events = []
    for row in connection.execute("SELECT * FROM combination_events WHERE run_id=? ORDER BY event_id", (run_id,)):
        event = dict(row)
        if json.loads(event["payload_json"]).get("execution_id") == previous["execution_id"]:
            previous_events.append(event)
    if _digest(previous_events) != proof.get("terminal_event_sha256"):
        raise ValueError("Initialisation prior owner-resolution raw events changed")
    validate_archived_readback(proof["issued_proof"], config, proof["issued_at_utc"])
    validate_archived_readback(proof["claim_readbacks"], config, proof["claim_verified_at_utc"])
    identity = claim.get("identity")
    if (not isinstance(identity, dict) or identity != result.get("identity") or identity != proof.get("current_owner")
            or type(identity.get("pid")) is not int or identity["pid"] <= 0 or result.get("pid") != identity["pid"]
            or events[0][1].get("pid") != identity["pid"] or events[0][1].get("run_id") != run_id):
        raise ValueError("Initialisation failed operator full identity differs")
    progress = records["operator_progress"]
    if not progress or any(not isinstance(event, dict) or event.get("pid") != identity["pid"] for event in progress):
        raise ValueError("Initialisation raw I/O transcript identity differs")
    closed = [index for index, event in enumerate(progress) if event.get("event") == "validation_connections_closed"]
    if len(closed) != 1 or progress[closed[0]].get("closed") is not True or progress[closed[0]].get("errors") != []:
        raise ValueError("Initialisation prior validation connection close is not known successful")
    tail = progress[closed[0] + 1:]
    expected_returns = [("begin", 0), ("Connect", 0), ("isDeviceInitialised", 1556), ("Disconnect", 0), ("end", 1026)]
    if len(tail) != 10:
        raise ValueError("Initialisation resolution refuses extra I/O after the failed startup")
    returns = []
    for index, (symbol, code) in enumerate(expected_returns):
        attempt, returned = tail[2 * index:2 * index + 2]
        name = "AttoDRY_Interface_" + symbol
        if (attempt.get("event") != "dll_attempt" or returned.get("event") != "dll_return"
                or attempt.get("symbol") != name or returned.get("symbol") != name
                or type(returned.get("return_code")) is not int or returned["return_code"] != code
                or _utc(attempt.get("captured_at_utc")) > _utc(returned.get("captured_at_utc"))):
            raise ValueError("Initialisation DLL failure/unwind is not the exact 1556/1026 transcript")
        returns.append(deepcopy(returned))
    allowed_reads = {"begin", "Connect", "Disconnect", "end", "isDeviceInitialised", "getSampleTemperature",
        "getUserTemperature", "getVtiTemperature", "getSampleHeaterPower", "getVtiHeaterPower",
        "getMagneticFieldX", "getMagneticFieldZ", "getMagneticFieldSetPointX", "getMagneticFieldSetPointZ",
        "isControllingTemperature", "isControllingField", "getAttodryErrorStatus"}
    for event in progress[:closed[0]]:
        if event.get("event") in {"dll_attempt", "dll_return"}:
            if event.get("symbol") not in {"AttoDRY_Interface_" + name for name in allowed_reads}:
                raise ValueError("Initialisation resolution refuses prior DLL setting/unknown commands")
            if event["event"] == "dll_return" and (type(event.get("return_code")) is not int or event["return_code"] != 0):
                raise ValueError("Initialisation resolution refuses an earlier DLL fault")
        if re.search(r"failed|error|violation", event.get("event", "")):
            raise ValueError("Initialisation resolution refuses an earlier fault log")
        _secondary_errors(event, "")
        _archived_states(event, config)
    return {"initialisation_evidence": evidence, "prior_machine_resolution_sha256": _digest(proof),
            "startup_dll_returns": returns, "terminal_event_sha256": _digest([event for event, payload in events]),
            "historical_communication_certified": False, "historical_cleanup_rewritten": False,
            "failed_attempt_opened": False}


def validate_archived_guard_identity_evidence(connection, run_id, failed_execution_id, config, old_sources, evidence):
    """Classify one archived sequence-4 PID-reuse failure; never grant a lease."""
    evidence = _guard_identity_references(evidence)
    records = {}
    for prefix in (key[:-5] for key in evidence if key.endswith("_path")):
        try:
            raw = Path(evidence[prefix + "_path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != evidence[prefix + "_sha256"]:
                raise ValueError("Frozen guard identity evidence changed")
            text = raw.decode("utf-8-sig")
            records[prefix] = ([json.loads(line) for line in text.splitlines() if line.strip()]
                if prefix == "operator_progress" else json.loads(text))
            encode(records[prefix])
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Guard identity evidence is unavailable or malformed") from exc
    row = connection.execute("SELECT * FROM combination_executions WHERE run_id=? AND execution_id=?",
                             (run_id, failed_execution_id)).fetchone()
    if row is None:
        raise ValueError("Guard identity failed execution is absent")
    execution = dict(row)
    plan, review = json.loads(execution["plan_json"]), json.loads(execution["review_json"])
    cleanup = json.loads(execution["cleanup_json"] or "{}")
    if (execution["sequence_index"] != 4 or execution["status"] != "failed" or execution["error"] != GUARD_IDENTITY_ERROR
            or not execution["finished_at_utc"] or plan.get("source_sha256") != old_sources
            or plan.get("config_sha256") != config.snapshot["config_sha256"]
            or {axis["module"] for axis in plan["axes"]} != {"temperature", "lockin"}
            or review.get("manual_verification") is not False):
        raise ValueError("Guard identity evidence does not bind the exact startup execution/source/config")
    if connection.execute("SELECT COUNT(*) FROM combination_attempt_executions WHERE run_id=? AND execution_id=?",
                          (run_id, failed_execution_id)).fetchone()[0] != 0:
        raise ValueError("Guard identity resolution refuses an opened attempt/acquisition")
    events = []
    for row in connection.execute("SELECT * FROM combination_events WHERE run_id=? ORDER BY event_id", (run_id,)):
        event = dict(row)
        payload = json.loads(event["payload_json"])
        if payload.get("execution_id") == failed_execution_id:
            events.append((event, payload))
    expected_kinds = ["launch_registered", "run_started", "cryostat_state", "temperature_telemetry",
        "cryostat_connection_adopted", "recovery_environment_initial", "lockin_raw_role", "lockin_raw_role",
        "lockin_recovery_baseline", "cleanup_started", "lockin_command_attempt", "lockin_command_return",
        "lockin_command_attempt", "lockin_command_return", "lockin_command_attempt", "lockin_command_return",
        "lockin_raw_role", "lockin_raw_role", "lockin_command_attempt", "lockin_command_return",
        "lockin_command_attempt", "lockin_command_return", "lockin_raw_role", "lockin_raw_role",
        "lockin_raw_role", "lockin_raw_role", "cleanup_result", "cleanup_started", "cleanup_result",
        "run_finished", "data_quality_summary"]
    if [event["event_type"] for event, payload in events] != expected_kinds:
        raise ValueError("Guard identity resolution refuses acknowledgement, attempts, or unclassified raw events")
    if (events[1][1].get("plan") != plan or events[1][1].get("resume") is not True
            or events[1][1].get("cleanup_only") is not False or events[9][1].get("module") != "lockin"
            or events[27][1].get("module") != "temperature" or events[29][1].get("status") != "failed"
            or events[29][1].get("error") != GUARD_IDENTITY_ERROR or events[29][1].get("cleanup") != cleanup
            or events[30][1].get("rejected_attempt_audit", {}).get("formal_pairs") != 0):
        raise ValueError("Guard identity raw startup/terminal boundaries differ")
    actions = cleanup.get("actions", [])
    if (cleanup.get("clean") is not False or cleanup.get("communication_uncertain") is not False
            or cleanup.get("manual_verification_required") is not True or len(actions) != 2
            or [action.get("module") for action in actions] != ["lockin", "temperature"]
            or cleanup.get("errors") != ["lockin: cleanup not certified; review recorded actions",
                                         "temperature: cleanup not certified; review recorded actions"]):
        raise ValueError("Guard identity archived cleanup has unclassified uncertainty")
    for index, action in ((26, actions[0]), (28, actions[1])):
        if ({key: value for key, value in events[index][1].items() if key != "execution_id"} != action
                or action.get("startup_completed") is not False or action.get("clean") is not False
                or action.get("failure_requires_manual_review") is not True or action.get("audit_errors") != []):
            raise ValueError("Guard identity raw cleanup ownership differs")
    lockin_result = actions[0].get("result", {})
    if (lockin_result.get("attempted") is not True or lockin_result.get("verified") is not True
            or lockin_result.get("errors") != [] or actions[1].get("result") != {
                "attempted": False, "reason": "no writes attempted", "verified": True}):
        raise ValueError("Guard identity cleanup is not the exact verified Lock-in/no-temperature-write result")
    _secondary_errors(lockin_result, "")
    LiveOwnerResolution._diagnostics({role: lockin_result.get("final", {}).get(role) for role in
                                     ("lockin_xx", "lockin_xy")}, config, _utc(execution["finished_at_utc"]))
    commands = [("set_minimum_sine_output", "lockin_xx", []), ("set_reserve_mode", "lockin_xx", [2]),
                ("set_reserve_mode", "lockin_xy", [0]), ("set_sensitivity", "lockin_xx", [26]),
                ("set_sensitivity", "lockin_xy", [23])]
    for index, (method, role, arguments) in zip((10, 12, 14, 18, 20), commands):
        attempted, returned = events[index][1], events[index + 1][1]
        if (attempted.get("method") != method or attempted.get("role") != role or attempted.get("arguments") != arguments
                or returned.get("method") != method or returned.get("role") != role or returned.get("reading") is not None):
            raise ValueError("Guard identity resolution refuses unapproved cleanup commands")
    for index, (event, payload) in enumerate(events):
        _archived_states(payload, config)
        if index not in (26, 28, 29):
            _secondary_errors(payload, GUARD_IDENTITY_ERROR)
        if payload.get("problems") not in (None, []):
            raise ValueError("Guard identity resolution refuses recorded baseline problems")
    manifest, result, claim = records["operator_manifest"], records["operator_result"], records["machine_claim"]
    if (manifest.get("kind") != "temperature_resume_after_initialisation" or manifest.get("run_id") != run_id
            or manifest.get("runtime_source_sha256") != old_sources or manifest.get("config_sha256") != plan["config_sha256"]
            or result.get("run_id") != run_id or result.get("manifest_sha256") != evidence["operator_manifest_sha256"]
            or result.get("cli_exit_code") != 2 or result.get("error") is not None or result.get("audit_errors") != []
            or result.get("historical_cleanup_rewritten") is not False or result.get("manual_verification_asserted") is not False
            or result.get("result", {}).get("execution_id") != failed_execution_id or result["result"].get("status") != "failed"
            or result["result"].get("error") != GUARD_IDENTITY_ERROR or result["result"].get("cleanup") != cleanup):
        raise ValueError("Guard identity prior operator result is not the sole supported failure")
    proof = review.get("live_owner_resolution")
    canonical = claim.get("proof_canonical_json")
    if (not isinstance(proof, dict) or proof.get("contract") != INITIALISATION_CONTRACT or not isinstance(canonical, str)
            or json.loads(canonical) != proof or _digest(proof) != claim.get("machine_proof_sha256")
            or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != claim.get("machine_proof_sha256")
            or claim.get("run_id") != run_id or claim.get("execution_id") != failed_execution_id
            or claim.get("sequence_index") != 4 or claim.get("source_sha256") != old_sources
            or claim.get("config_sha256") != plan["config_sha256"] or claim.get("historical_cleanup_rewritten") is not False
            or claim.get("actual_resume_ack_verified") is not False or proof.get("new_source_sha256") != old_sources
            or proof.get("parent_state_sha256") != execution["parent_state_sha256"]
            or proof.get("initialisation_evidence") != manifest.get("initialisation_evidence")
            or _digest(proof.get("issued_proof")) != proof.get("issued_proof_sha256")):
        raise ValueError("Guard identity prior initialization live claim is not intact")
    previous = connection.execute("SELECT * FROM combination_executions WHERE run_id=? AND sequence_index=3", (run_id,)).fetchone()
    if (previous is None or proof.get("failed_execution_id") != previous["execution_id"]
            or proof.get("old_source_sha256") != manifest.get("parent_source_sha256")):
        raise ValueError("Guard identity previous initialization lineage differs")
    previous_summary = validate_archived_initialisation_evidence(connection, run_id, previous["execution_id"], config,
        proof["old_source_sha256"], manifest["initialisation_evidence"])
    if previous_summary["terminal_event_sha256"] != proof.get("terminal_event_sha256"):
        raise ValueError("Guard identity prior initialization raw history changed")
    validate_archived_readback(proof["issued_proof"], config, proof["issued_at_utc"])
    validate_archived_readback(proof["claim_readbacks"], config, proof["claim_verified_at_utc"])
    identity = claim.get("identity")
    if (not isinstance(identity, dict) or identity != result.get("identity") or identity != proof.get("current_owner")
            or type(identity.get("pid")) is not int or identity["pid"] <= 0 or result.get("pid") != identity["pid"]
            or events[0][1].get("pid") != identity["pid"] or events[0][1].get("run_id") != run_id):
        raise ValueError("Guard identity failed operator identity differs")
    baseline, repair, adoption = records["baseline_proof"], records["canonical_apply"], records["adoption_proof"]
    validate_archived_readback(baseline.get("live_readback"), config, baseline.get("live_readback", {}).get("captured_at_utc"))
    if (baseline.get("run_id") != run_id or baseline.get("identity") != identity
            or baseline.get("manifest_sha256") != evidence["operator_manifest_sha256"]
            or baseline.get("parent_state_sha256") != execution["parent_state_sha256"]
            or baseline.get("historical_cleanup_rewritten") is not False
            or repair != baseline["live_readback"]["baseline_restoration"]):
        raise ValueError("Guard identity raw canonical baseline proof differs")
    if (adoption.get("run_id") != run_id or adoption.get("manifest_sha256") != evidence["operator_manifest_sha256"]
            or adoption.get("status") != "adopted" or adoption.get("pid") != identity["pid"]
            or any(adoption.get(key) is not True for key in ("connected", "connection_owned", "same_exclusive_lease", "qualification_context_reset"))
            or adoption.get("begin_or_connect_replayed") is not False or adoption.get("actual_resume_ack_verified") is not False
            or type(adoption.get("existing_driver_object_id")) is not int
            or adoption["existing_driver_object_id"] != adoption.get("initial_driver_object_id")
            or {key: value for key, value in events[4][1].items() if key != "execution_id"} != {
                key: value for key, value in adoption.items() if key not in {"run_id", "manifest_sha256"}}
            or adoption.get("fresh_preflight", {}).get("telemetry_complete") is not True):
        raise ValueError("Guard identity did not retain one actual exclusive cryostat connection")
    _archived_states(adoption, config)
    powers = adoption["fresh_preflight"].get("heater_power", {})
    if any(not _finite(powers.get(key)) or powers[key] < 0 for key in ("sample_w", "vti_w")):
        raise ValueError("Guard identity adopted power readbacks are incomplete")
    if Path(evidence["historical_identity_path"]).parent.resolve() != Path(records["historical_identity"]["operation_dir"]).resolve():
        raise ValueError("Guard identity historical launch path differs from its recorded operation")
    windows = _windows_pid_reuse(records["windows_event_proof"], records["historical_identity"], manifest, run_id,
        failed_execution_id, events[9][0]["created_at_utc"])
    progress = records["operator_progress"]
    if not progress or any(not isinstance(event, dict) or event.get("pid") != identity["pid"] for event in progress):
        raise ValueError("Guard identity raw I/O transcript identity differs")
    allowed_reads = {"begin", "Connect", "Disconnect", "end", "isDeviceInitialised", "getSampleTemperature",
        "getUserTemperature", "getVtiTemperature", "getSampleHeaterPower", "getVtiHeaterPower",
        "getMagneticFieldX", "getMagneticFieldZ", "getMagneticFieldSetPointX", "getMagneticFieldSetPointZ",
        "isControllingTemperature", "isControllingField", "getAttodryErrorStatus"}
    lifecycle = []
    dll_rows, visa_writes = [], []
    for event in progress:
        if event.get("event") in {"dll_attempt", "dll_return"}:
            dll_rows.append(event)
            suffix = event.get("symbol", "").removeprefix("AttoDRY_Interface_")
            if suffix not in allowed_reads or not event.get("symbol", "").startswith("AttoDRY_Interface_"):
                raise ValueError("Guard identity resolution refuses prior DLL setting/unknown commands")
            if event["event"] == "dll_return":
                if type(event.get("return_code")) is not int or event["return_code"] != 0:
                    raise ValueError("Guard identity resolution refuses any DLL communication fault")
                if suffix in {"begin", "Connect", "Disconnect", "end"}:
                    lifecycle.append(suffix)
        if event.get("event") in {"visa_write_attempt", "visa_write_return"}:
            visa_writes.append(event)
        if (event.get("event") == "visa_query_return" and event.get("query") in {"LIAS?", "ERRS?"}
                and event.get("raw_response") != "0"):
            raise ValueError("Guard identity resolution refuses a hidden Lock-in status/error")
        if re.search(r"failed|error|violation", event.get("event", "")):
            raise ValueError("Guard identity resolution refuses secondary I/O faults")
        _secondary_errors(event, "")
        _archived_states(event, config)
    if len(dll_rows) % 2:
        raise ValueError("Guard identity resolution requires complete DLL call return evidence")
    for attempted, returned in zip(dll_rows[::2], dll_rows[1::2]):
        if (attempted.get("event") != "dll_attempt" or returned.get("event") != "dll_return"
                or attempted.get("symbol") != returned.get("symbol")
                or _utc(attempted.get("captured_at_utc")) > _utc(returned.get("captured_at_utc"))):
            raise ValueError("Guard identity resolution refuses an incomplete or reordered DLL call")
    expected_writes = [(0, "SLVL 0.004"), (0, "RMOD 2"), (1, "RMOD 0"), (0, "SENS 26"), (1, "SENS 23")]
    if len(visa_writes) != 10:
        raise ValueError("Guard identity resolution refuses extra or missing baseline cleanup writes")
    for index, (role, command) in enumerate(expected_writes):
        attempted, returned = visa_writes[2 * index:2 * index + 2]
        if (attempted.get("event") != "visa_write_attempt" or returned.get("event") != "visa_write_return"
                or attempted.get("resource_index") != role or returned.get("resource_index") != role
                or attempted.get("command") != command or returned.get("command") != command
                or type(returned.get("result")) is not int or returned["result"] <= 0
                or not _utc(events[9][0]["created_at_utc"]) <= _utc(attempted.get("captured_at_utc"))
                    <= _utc(returned.get("captured_at_utc"))):
            raise ValueError("Guard identity resolution refuses unapproved/uncertain electrical setting writes")
    closed = [event for event in progress if event.get("event") == "owned_cryostat_close"]
    if (lifecycle != ["begin", "Connect", "Disconnect", "end"] or len(closed) != 1
            or closed[0].get("closed") is not True or closed[0].get("adopted_by_scan") is not True
            or closed[0].get("error") is not None or closed[0].get("dll_teardown") != {
                "Disconnect": {"attempted": True, "return_code": 0, "error": None},
                "end": {"attempted": True, "return_code": 0, "error": None}}):
        raise ValueError("Guard identity cryostat lifecycle/close is not exactly known once")
    return {"guard_identity_evidence": evidence, "prior_machine_resolution_sha256": _digest(proof),
        "windows_pid_reuse": windows, "terminal_event_sha256": _digest([event for event, payload in events]),
        "historical_communication_certified": False, "historical_cleanup_rewritten": False,
        "failed_attempt_opened": False}


def _secondary_errors(value, primary):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"errors", "audit_errors"} and item:
                raise ValueError("Owner-guard resolution refuses secondary audit/cleanup errors")
            if (key == "error" or key.endswith("_error")) and item not in (None, "", primary):
                raise ValueError("Owner-guard resolution refuses a secondary fault")
            if key in {"error_code", "error_status"} and item is not None and (
                    type(item) is not int or item != 0):
                raise ValueError("Owner-guard resolution refuses an instrument error")
            _secondary_errors(item, primary)
    elif isinstance(value, list):
        for item in value:
            _secondary_errors(item, primary)


def _archived_states(value, config):
    """Check raw prior states without treating them as current/qualified reads."""
    if isinstance(value, dict):
        if "sample_temperature_k" in value and any(key in value for key in
                ("user_temperature_k", "field", "field_setpoint")):
            bounds = config.cryostat.cryostat
            if (type(value.get("error_code")) is not int or value["error_code"] != 0
                    or any(type(value.get(key)) is not bool for key in
                           ("temperature_control_enabled", "field_control_enabled"))):
                raise ValueError("Owner-guard resolution archived cryostat status is unknown or faulted")
            for key in ("sample_temperature_k", "user_temperature_k"):
                if not _finite(value.get(key)) or not bounds.temperature_min_k <= value[key] <= bounds.temperature_max_k:
                    raise ValueError("Owner-guard resolution archived temperature exceeds hard limits")
            if not _finite(value.get("vti_temperature_k")) or value["vti_temperature_k"] <= 0:
                raise ValueError("Owner-guard resolution archived VTI state is unknown")
            for key in ("field", "field_setpoint"):
                field = value.get(key)
                if (not isinstance(field, dict) or any(not _finite(field.get(axis)) for axis in ("bx_t", "bz_t"))
                        or math.hypot(field["bx_t"], field["bz_t"]) > 3.0):
                    raise ValueError("Owner-guard resolution archived vector field exceeds 3 T or is unknown")
        if "locked" in value and value["locked"] is not True:
            raise ValueError("Owner-guard resolution archived reference lock is unknown or lost")
        if "lia_status" in value:
            status = value["lia_status"]
            if (not isinstance(status, dict) or type(status.get("raw")) is not int
                    or not 0 <= status["raw"] <= 0x7F or status.get("reference_unlocked") is not False
                    or status.get("triggered") is not False):
                raise ValueError("Owner-guard resolution archived lock-in status is unknown or faulted")
        for item in value.values():
            _archived_states(item, config)
    elif isinstance(value, list):
        for item in value:
            _archived_states(item, config)


def validate_archived_readback(proof, config, observed_at_utc):
    """Pure final-audit validation; this never grants a live recovery capability."""
    return LiveOwnerResolution._validate_readback(proof, config, _utc(observed_at_utc))


class LiveOwnerResolution:
    """A single-use, current-process capability held under an active owner lease.

    ``live_readback`` must query the already exclusively owned instrument
    sessions. It runs at construction, file review, and the atomic DB claim.
    The capability is intentionally neither serializable nor copyable.
    """

    def __init__(self, owner, *, run_id, parent_state_sha256, failed_execution_id,
                 failed_condition_id, failed_attempt_index, expected_primary_error,
                 old_source_sha256, new_source_sha256, config_sha256, live_readback):
        if not isinstance(owner, RecoveryOwner) or not callable(live_readback):
            raise ValueError("Live owner resolution requires the actual owner and a live readback callable")
        initialisation = type(self) in (LiveInitialisationResolution, LiveGuardIdentityResolution)
        expected_startup_error = GUARD_IDENTITY_ERROR if type(self) is LiveGuardIdentityResolution else INITIALISATION_ERROR
        if (not all(isinstance(value, str) and value for value in
                    (run_id, failed_execution_id, failed_condition_id))
                or (failed_attempt_index is not None if initialisation else
                    type(failed_attempt_index) is not int or failed_attempt_index < 0)
                or (expected_primary_error != expected_startup_error if initialisation else
                    not isinstance(expected_primary_error, str) or not _OWNER_ERROR.fullmatch(expected_primary_error))):
            raise ValueError("Live owner resolution requires an exact supported failure identity")
        (_initialisation_sources if initialisation else _sources)(old_source_sha256, new_source_sha256)
        self._contract = (GUARD_IDENTITY_CONTRACT if type(self) is LiveGuardIdentityResolution else
                          INITIALISATION_CONTRACT if initialisation else CONTRACT)
        self._owner = owner
        self._readback = live_readback
        self._run_id = run_id
        self._parent = _sha(parent_state_sha256)
        self._execution = failed_execution_id
        self._condition = failed_condition_id
        self._attempt = failed_attempt_index
        self._error = expected_primary_error
        self._old = deepcopy(old_source_sha256)
        self._new = deepcopy(new_source_sha256)
        self._config_sha = _sha(config_sha256)
        self._used = False
        self._event_sha = None
        self._identity = None
        self._check_owner()
        self._issued_proof = self._fresh_readback()
        self._issued_at = datetime.now(timezone.utc)
        self._issued_proof_sha = _digest(self._issued_proof)

    def __reduce_ex__(self, protocol):
        raise TypeError("A live owner resolution cannot be serialized")

    def __copy__(self):
        raise TypeError("A live owner resolution cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("A live owner resolution cannot be copied")

    def _check_owner(self):
        if self._used or not self._owner.locked:
            raise ValueError("Live owner resolution is consumed or lacks an active exclusive lease")
        snapshot = self._owner.config.snapshot
        if snapshot.get("config_sha256") != self._config_sha or snapshot.get("source_sha256") != self._new:
            raise ValueError("Live owner resolution current source/config bindings changed")
        evidence = self._owner.check()
        identity = deepcopy(self._owner.identity)
        if (not isinstance(identity, dict) or identity.get("pid") != os.getpid()
                or type(identity.get("parent")) is not int or identity["parent"] < 0
                or not identity.get("name") or not identity.get("command")
                or evidence.get("pid") != os.getpid()
                or evidence.get("created") != identity.get("created")
                or evidence.get("command") != identity["command"]):
            raise ValueError("Live owner resolution current process identity is incomplete")
        _utc(identity.get("created"))
        if self._identity is not None and identity != self._identity:
            raise ValueError("Live owner resolution current process identity changed")
        self._identity = identity

    @staticmethod
    def _observation_times(value, now):
        start, finish = _utc(value.get("started_at_utc")), _utc(value.get("captured_at_utc"))
        if not start <= finish <= now or not 0 <= (now - start).total_seconds() <= MAX_AGE_S:
            raise ValueError("Live owner resolution actual observation timing is stale or unknown")

    @staticmethod
    def _diagnostics(roles, config, now, *, live=False):
        if not isinstance(roles, dict) or set(roles) != {"lockin_xx", "lockin_xy"}:
            raise ValueError("Live owner resolution needs both raw SR830 diagnostics")
        config = config.lockin
        if config is None:
            raise ValueError("Live owner resolution is limited to temperature and dual SR830")
        identities = []
        for role in ("lockin_xx", "lockin_xy"):
            diagnostic, target = roles[role], getattr(config, role)
            if not isinstance(diagnostic, dict) or getattr(target, "model", "SR830") != "SR830":
                raise ValueError("Live owner resolution requires known SR830 diagnostics")
            if live:
                LiveOwnerResolution._observation_times(diagnostic, now)
            codes = map_sr830_settings(**{key: getattr(target, key) for key in
                ("reference_source", "external_reference_edge", "input_mode", "shield_grounding",
                 "input_coupling", "time_constant_s", "filter_slope_db_oct", "sensitivity_full_scale_v")})
            expected = {"reference_mode": codes.reference_source, "input_mode": codes.input_mode,
                        "shield_grounding": codes.shield_grounding, "input_coupling": codes.input_coupling,
                        "time_constant": codes.time_constant, "filter_slope": codes.filter_slope,
                        "sensitivity": codes.sensitivity, "harmonic": 1,
                        "reserve_mode": RESERVE_MODE_CODES[target.reserve_mode.value]}
            if codes.external_reference_edge is not None:
                expected["reference_slope"] = codes.external_reference_edge
            if any(type(diagnostic.get(key)) is not int or diagnostic[key] != value
                   for key, value in expected.items()):
                raise ValueError("Live owner resolution SR830 baseline settings are not canonical")
            if (diagnostic.get("role") != role.removeprefix("lockin_")
                    or not isinstance(diagnostic.get("identity"), str) or not diagnostic["identity"]):
                raise ValueError("Live owner resolution SR830 role/identity is unknown")
            identities.append(diagnostic["identity"])
            if type(diagnostic.get("line_filter")) is not int or not 0 <= diagnostic["line_filter"] <= 3:
                raise ValueError("Live owner resolution line filter state is unknown")
            if (not _finite(diagnostic.get("sine_output_v"))
                    or abs(diagnostic["sine_output_v"] - .004) > 1e-9):
                raise ValueError("Live owner resolution requires actual dual 4 mVrms/h1")
            frequency = target.frequency_hz
            tolerance = max(.00101, frequency * 100e-6)
            for key in ("frequency_hz", "snapshot_frequency_hz"):
                if not _finite(diagnostic.get(key)) or abs(diagnostic[key] - frequency) > tolerance:
                    raise ValueError("Live owner resolution baseline frequency is not confirmed")
            if any(not _finite(diagnostic.get(key)) for key in
                   ("phase_shift_deg", "x_v", "y_v", "amplitude_v", "phase_deg")):
                raise ValueError("Live owner resolution SR830 readback is incomplete")
            status = diagnostic.get("lia_status")
            flags = ("input_or_reserve_overload", "filter_overload", "output_overload", "reference_unlocked",
                     "frequency_range_changed", "time_constant_changed", "triggered")
            if (not isinstance(status, dict) or type(status.get("raw")) is not int or status["raw"] != 0
                    or any(status.get(flag) is not False for flag in flags)
                    or type(diagnostic.get("error_status")) is not int or diagnostic["error_status"] != 0):
                raise ValueError("Live owner resolution requires clear baseline status/error and lock")
        if len(set(identities)) != 2:
            raise ValueError("Live owner resolution dual SR830 identities are not distinct")

    def _fresh_readback(self):
        proof = self._readback()
        # The sequential device frame may take time. A new owner/config/source
        # appearing during those reads must block the capability/claim as well.
        self._check_owner()
        return self._validate_readback(proof, self._owner.config, datetime.now(timezone.utc))

    @staticmethod
    def _validate_readback(proof, config, now):
        if not isinstance(proof, dict):
            raise ValueError("Live owner resolution callable did not return raw readbacks")
        # Refuse NaN/Infinity and detach raw data from mutable instrument adapters.
        proof = json.loads(encode(proof))
        age = (now - _utc(proof.get("captured_at_utc"))).total_seconds()
        if not 0 <= age <= MAX_AGE_S:
            raise ValueError("Live owner resolution readbacks are stale or future dated")
        cryostat = proof.get("cryostat", {})
        state, powers = cryostat.get("state"), cryostat.get("heater_power")
        if not isinstance(state, dict) or not isinstance(powers, dict):
            raise ValueError("Live owner resolution requires complete current cryostat/power readbacks")
        LiveOwnerResolution._observation_times(cryostat, now)
        cryostat_config = config.cryostat
        if cryostat_config is None or config.smu is not None or config.magnetic is not None:
            raise ValueError("Live owner resolution is limited to temperature and dual SR830")
        if (type(state.get("error_code")) is not int or state["error_code"] != 0
                or any(type(state.get(key)) is not bool for key in
                       ("temperature_control_enabled", "field_control_enabled"))):
            raise ValueError("Live owner resolution cryostat error/control state is unknown")
        for key in ("sample_temperature_k", "user_temperature_k"):
            if not _finite(state.get(key)) or not cryostat_config.cryostat.temperature_min_k <= state[key] <= cryostat_config.cryostat.temperature_max_k:
                raise ValueError("Live owner resolution actual temperature is outside hard limits")
        if not _finite(state.get("vti_temperature_k")) or state["vti_temperature_k"] <= 0:
            raise ValueError("Live owner resolution actual VTI temperature is unknown")
        if any(not _finite(powers.get(key)) or powers[key] < 0 for key in ("sample_w", "vti_w")):
            raise ValueError("Live owner resolution both heater powers are required")
        tolerance = cryostat_config.magnet.readback_tolerance_t
        tolerance = cryostat_config.magnet.field_tolerance_t if tolerance is None else tolerance
        for key, limit in (("field", tolerance), ("field_setpoint", cryostat_config.magnet.setpoint_ack_tolerance_t)):
            field = state.get(key)
            if (not isinstance(field, dict) or any(not _finite(field.get(axis)) for axis in ("bx_t", "bz_t"))
                    or math.hypot(field["bx_t"], field["bz_t"]) > min(limit, 3.0)):
                raise ValueError("Live owner resolution requires confirmed actual/register zero field")
        LiveOwnerResolution._diagnostics(proof.get("lockin"), config, now, live=True)
        restoration = proof.get("baseline_restoration", {})
        raw = restoration.get("raw", {})
        if (restoration.get("attempted") is not True or restoration.get("verified") is not True
                or restoration.get("errors") != [] or not isinstance(raw, dict)
                or raw.get("scan") != "apply_toml" or raw.get("completed") is not True
                or raw.get("outcome") != "completed" or raw.get("error") is not None
                or raw.get("communication_uncertain") is not False
                or raw.get("manual_verification_required") is not False
                or not isinstance(raw.get("actions"), list) or not raw["actions"]):
            raise ValueError("Live owner resolution requires the successful raw canonical repair result")
        _secondary_errors(raw["actions"], "")
        requested = raw.get("requested", {})
        if (type(requested.get("harmonic")) is not int or requested["harmonic"] != 1
                or requested.get("xx_source_voltage_v") != .004
                or requested.get("frequency_hz") != config.lockin.lockin_xx.frequency_hz
                or raw.get("lockin_safety_sha256") != config.snapshot["hardware"]["lockin"]["safety_sha256"]):
            raise ValueError("Live owner resolution canonical repair does not bind the current baseline/safety")
        LiveOwnerResolution._diagnostics(raw.get("after"), config, now)
        for role in ("lockin_xx", "lockin_xy"):
            after, current = raw["after"][role], proof["lockin"][role]
            if (after["identity"] != current["identity"] or after["line_filter"] != current["line_filter"]
                    or abs(after["phase_shift_deg"] - current["phase_shift_deg"]) > .001):
                raise ValueError("Live owner resolution instrument identity/filter/phase changed after canonical repair")
        return proof

    def verify(self, state, plan, connection, *, consume=False):
        """Revalidate lineage and fresh I/O, including inside BEGIN IMMEDIATE."""
        self._check_owner()
        if not 0 <= (datetime.now(timezone.utc) - self._issued_at).total_seconds() <= CAP_MAX_AGE_S:
            raise ValueError("Live owner resolution capability is stale")
        if _digest(self._issued_proof) != self._issued_proof_sha:
            raise ValueError("Live owner resolution original proof changed")
        if (state.get("state_sha256") != self._parent or state["run"].get("run_id") != self._run_id
                or state["run"].get("status") != "failed" or state["run"].get("error") != self._error
                or plan.get("config_sha256") != self._config_sha or plan.get("source_sha256") != self._new):
            raise ValueError("Live owner resolution parent/run/config/source changed")
        if not state["executions"]:
            raise ValueError("Live owner resolution requires an exactly owned failed execution")
        execution = state["executions"][-1]
        parent_plan = json.loads(execution["plan_json"])
        if (execution["execution_id"] != self._execution or execution["status"] != "failed"
                or execution["error"] != self._error or parent_plan.get("source_sha256") != self._old
                or parent_plan.get("config_sha256") != self._config_sha
                or execution["cleanup_json"] != state["run"]["cleanup_json"]):
            raise ValueError("Live owner resolution failed execution/source/config does not match")
        if {axis["module"] for axis in parent_plan["axes"]} != {"temperature", "lockin"}:
            raise ValueError("Live owner resolution is limited to temperature and dual SR830")
        if type(self) in (LiveInitialisationResolution, LiveGuardIdentityResolution):
            accepted = set(state["accepted_condition_ids"])
            pending = next((c for c in state["conditions"] if c["condition_id"] not in accepted), None)
            if pending is None or pending["condition_id"] != self._condition:
                raise ValueError("Initialisation resolution pending whole condition differs")
            if type(self) is LiveGuardIdentityResolution:
                if len(accepted) != 23 or len(state["conditions"]) != 342 or self._condition != "condition-000023":
                    raise ValueError("Guard identity resolution is limited to the original 23-of-342 pending condition")
                self._initialisation_summary = validate_archived_guard_identity_evidence(
                    connection, self._run_id, self._execution, self._owner.config, self._old,
                    self._guard_identity_evidence)
            else:
                self._initialisation_summary = validate_archived_initialisation_evidence(
                    connection, self._run_id, self._execution, self._owner.config, self._old,
                    self._initialisation_evidence)
            return self._finish(self._initialisation_summary["terminal_event_sha256"], consume=consume)
        accepted = set(state["accepted_condition_ids"])
        pending = next((condition for condition in state["conditions"] if condition["condition_id"] not in accepted), None)
        mapped = {(row["condition_id"], row["attempt_index"]): row["execution_id"] for row in state["attempt_executions"]}
        rejected = [row for row in state["attempts"] if row["status"] != "accepted"
                    and mapped.get((row["condition_id"], row["attempt_index"])) == self._execution]
        if (pending is None or pending["condition_id"] != self._condition or len(rejected) != 1
                or rejected[0]["condition_id"] != self._condition or rejected[0]["attempt_index"] != self._attempt
                or rejected[0]["status"] != "rejected" or rejected[0]["error"] != self._error):
            raise ValueError("Live owner resolution requires the sole failed whole pending attempt")
        cleanup = json.loads(state["run"]["cleanup_json"])
        actions = cleanup.get("actions", [])
        expected_errors = {f"{module}: cleanup not certified; review recorded actions" for module in ("temperature", "lockin")}
        if (cleanup.get("clean") is not False or cleanup.get("communication_uncertain") is not True
                or cleanup.get("manual_verification_required") is not True or len(actions) != 2
                or {action.get("module") for action in actions} != {"temperature", "lockin"}
                or set(cleanup.get("errors", [])) != expected_errors or len(cleanup["errors"]) != 2):
            raise ValueError("Live owner resolution archived cleanup is not the sole unattempted owner-guard failure")
        for action in actions:
            result = action.get("result", {})
            if (action.get("audit_errors") != [] or action.get("clean") is not False
                    or action.get("failure_requires_manual_review") is not True
                    or result.get("attempted") is not False or result.get("verified") is not False
                    or result.get("communication_uncertain") is not True
                    or result.get("manual_verification_required") is not True
                    or result.get("owner_guard_error") != self._error
                    or result.get("errors") not in (None, [])):
                raise ValueError("Live owner resolution refuses attempted, ambiguous, or secondary cleanup")
        events = []
        for row in connection.execute("SELECT * FROM combination_events WHERE run_id=? ORDER BY event_id", (self._run_id,)):
            event = dict(row)
            payload = json.loads(event["payload_json"])
            if payload.get("execution_id") == self._execution:
                events.append((event, payload))
        guards = [payload for event, payload in events if event["event_type"] == "recovery_owner_verification_failed"]
        finished = [payload for event, payload in events if event["event_type"] == "run_finished"]
        results = [payload for event, payload in events if event["event_type"] == "cleanup_result"]
        if (len(guards) != 1 or guards[0].get("error") != self._error
                or guards[0].get("further_instrument_writes_blocked") is not True
                or guards[0].get("hardware_zero_or_off_confirmed") is not False
                or len(finished) != 1 or finished[0].get("status") != "failed"
                or finished[0].get("error") != self._error or finished[0].get("cleanup") != cleanup
                or len(results) != 2):
            raise ValueError("Live owner resolution raw terminal/guard evidence does not match")
        for payload in results:
            raw_action = {key: value for key, value in payload.items()
                          if key not in {"execution_id", "condition_id", "attempt_index"}}
            if raw_action not in actions:
                raise ValueError("Live owner resolution raw cleanup result differs from terminal cleanup")
        for event, payload in events:
            _archived_states(payload, self._owner.config)
            kind = event["event_type"]
            if kind in {"recovery_owner_verification_failed", "cleanup_result", "run_finished"}:
                continue
            if re.search(r"failed|error|violation", kind):
                raise ValueError("Live owner resolution refuses a secondary fault event")
            _secondary_errors(payload, self._error)
        for row in connection.execute("SELECT payload_json FROM combination_samples WHERE run_id=? AND condition_id=? AND attempt_index=?",
                                      (self._run_id, self._condition, self._attempt)):
            payload = json.loads(row[0])
            _secondary_errors(payload, self._error)
            _archived_states(payload, self._owner.config)
        return self._finish(_digest([event for event, payload in events]), consume=consume)

    def _finish(self, event_sha, *, consume):
        if self._event_sha is not None and event_sha != self._event_sha:
            raise ValueError("Live owner resolution terminal event evidence changed")
        self._event_sha = event_sha
        current_proof = self._fresh_readback()
        if not 0 <= (datetime.now(timezone.utc) - self._issued_at).total_seconds() <= CAP_MAX_AGE_S:
            raise ValueError("Live owner resolution capability expired during the live frame")
        public = {"contract": self._contract, "run_id": self._run_id, "failed_execution_id": self._execution,
                  "failed_condition_id": self._condition, "failed_attempt_index": self._attempt,
                  "expected_primary_error": self._error, "parent_state_sha256": self._parent,
                  "old_source_sha256": deepcopy(self._old), "new_source_sha256": deepcopy(self._new),
                  "config_sha256": self._config_sha, "current_owner": deepcopy(self._identity),
                  "terminal_event_sha256": event_sha, "issued_at_utc": self._issued_at.isoformat(),
                  "issued_proof": deepcopy(self._issued_proof), "issued_proof_sha256": self._issued_proof_sha}
        if type(self) in (LiveInitialisationResolution, LiveGuardIdentityResolution):
            public.update(deepcopy(self._initialisation_summary))
        if consume:
            self._used = True
        return public, current_proof


class LiveInitialisationResolution(LiveOwnerResolution):
    """One exact sequence-3, pre-acquisition 1556/1026 startup resolution."""

    def __init__(self, owner, *, initialisation_evidence, **bindings):
        self._initialisation_evidence = _initialisation_references(initialisation_evidence)
        self._initialisation_summary = None
        super().__init__(owner, **bindings)


class LiveGuardIdentityResolution(LiveOwnerResolution):
    """One exact sequence-4 startup guard failure with native PID-reuse evidence."""

    def __init__(self, owner, *, guard_identity_evidence, **bindings):
        self._guard_identity_evidence = _guard_identity_references(guard_identity_evidence)
        self._initialisation_summary = None
        super().__init__(owner, **bindings)


def _process_guard_removal_sources(old, new):
    if (not isinstance(old, dict) or not isinstance(new, dict) or len(old) not in (80, 81)
            or set(old) != set(new)):
        raise ValueError("Process-guard removal requires the reviewed source transition")
    for name, value in [*old.items(), *new.items()]:
        if not isinstance(name, str) or not name.endswith(".py") or "/" in name or "\\" in name:
            raise ValueError("Process-guard removal source map is malformed")
        _sha(value)
    if {name for name in old if old[name] != new[name]} != {
            "combination_cli.py", "combination_hardware.py", "combination_resume.py",
            "combination_store.py", "combination_owner_resolution.py"}:
        raise ValueError("Process-guard removal changes unrelated implementation")


def _process_guard_secondary_errors(value):
    """Recognize the same raw guard exception with or without its type prefix."""
    primary = PROCESS_GUARD_REMOVAL_ERROR
    bare = primary.removeprefix("ValueError: ")

    def normalized(item):
        if isinstance(item, dict):
            return {key: (primary if (key == "error" or key.endswith("_error")) and raw == bare
                          else normalized(raw)) for key, raw in item.items()}
        if isinstance(item, list):
            return [normalized(raw) for raw in item]
        return item

    # The detached comparison view never changes raw events or their digest.
    _secondary_errors(normalized(value), primary)


def validate_process_guard_removal_parent(connection, run_id, config, old_source_sha256):
    """Pure raw review of the one analysis-process failure; grants no I/O right."""
    from .combination_store import recovery_parent_state, validate_accepted_records
    state = recovery_parent_state(connection, run_id)
    error = PROCESS_GUARD_REMOVAL_ERROR
    if (state["run"]["status"] != "failed" or state["run"]["error"] != error
            or not state["executions"] or len(state["conditions"]) != 342
            or state["accepted_condition_ids"] != [c["condition_id"] for c in state["conditions"][:37]]):
        raise ValueError("Process-guard removal requires the exact failed 37-of-342 boundary")
    execution = state["executions"][-1]
    parent = json.loads(execution["plan_json"])
    if (execution["sequence_index"] != 5 or execution["status"] != "failed"
            or execution["error"] != error or execution["cleanup_json"] != state["run"]["cleanup_json"]
            or parent.get("source_sha256") != old_source_sha256
            or parent.get("config_sha256") != config.snapshot["config_sha256"]
            or {a["module"] for a in parent["axes"]} != {"temperature", "lockin"}
            or state["conditions"] != config.plan.conditions()):
        raise ValueError("Process-guard removal parent execution/configuration differs")
    mapping = {(a["condition_id"], a["attempt_index"]): a["execution_id"] for a in state["attempt_executions"]}
    rejected = [a for a in state["attempts"] if a["status"] != "accepted"
                and mapping.get((a["condition_id"], a["attempt_index"])) == execution["execution_id"]]
    if (len(rejected) != 1 or rejected[0]["condition_id"] != "condition-000037"
            or rejected[0]["status"] != "rejected" or rejected[0]["error"] != error):
        raise ValueError("Process-guard removal requires the failed whole condition 37")
    cleanup = json.loads(execution["cleanup_json"])
    actions = cleanup.get("actions", [])
    expected_errors = {f"{m}: cleanup not certified; review recorded actions" for m in ("temperature", "lockin")}
    if (cleanup.get("clean") is not False or cleanup.get("communication_uncertain") is not True
            or cleanup.get("manual_verification_required") is not True or len(actions) != 2
            or {a.get("module") for a in actions} != {"temperature", "lockin"}
            or set(cleanup.get("errors", [])) != expected_errors or len(cleanup["errors"]) != 2):
        raise ValueError("Process-guard removal refuses ambiguous raw cleanup")
    for action in actions:
        raw = action.get("result", {})
        if (action.get("audit_errors") != [] or action.get("clean") is not False
                or action.get("failure_requires_manual_review") is not True
                or raw.get("attempted") is not False or raw.get("verified") is not False
                or raw.get("communication_uncertain") is not True
                or raw.get("manual_verification_required") is not True
                or raw.get("owner_guard_error") != error or raw.get("errors") not in (None, [])):
            raise ValueError("Process-guard removal refuses attempted or secondary cleanup")
    events = [(dict(row), json.loads(row["payload_json"])) for row in connection.execute(
        "SELECT * FROM combination_events WHERE run_id=? ORDER BY event_id", (run_id,))
        if json.loads(row["payload_json"]).get("execution_id") == execution["execution_id"]]
    by_kind = {}
    for event, payload in events:
        by_kind.setdefault(event["event_type"], []).append(payload)
    guards, finished, results = (by_kind.get(k, []) for k in
        ("recovery_owner_verification_failed", "run_finished", "cleanup_result"))
    terminal_kinds = ["recovery_owner_verification_failed", "environment_after_read_failed",
        "cleanup_started", "cleanup_result", "cleanup_started", "cleanup_result",
        "run_finished", "data_quality_summary"]
    if [e["event_type"] for e, _ in events[-8:]] != terminal_kinds:
        raise ValueError("Process-guard removal terminal event sequence differs")
    if (len(guards) != 1 or guards[0].get("error") != error
            or guards[0].get("further_instrument_writes_blocked") is not True
            or guards[0].get("hardware_zero_or_off_confirmed") is not False
            or len(finished) != 1 or finished[0].get("status") != "failed"
            or finished[0].get("error") != error or finished[0].get("cleanup") != cleanup or len(results) != 2):
        raise ValueError("Process-guard removal raw terminal/guard evidence differs")
    for event, payload in events[-8:]:
        if event["event_type"] == "run_finished":
            continue
        if (payload.get("condition_id") != rejected[0]["condition_id"]
                or payload.get("attempt_index") != rejected[0]["attempt_index"]):
            raise ValueError("Process-guard removal terminal attempt attribution differs")
    for payload in results:
        if {k: v for k, v in payload.items() if k not in {"execution_id", "condition_id", "attempt_index"}} not in actions:
            raise ValueError("Process-guard removal raw cleanup differs from terminal cleanup")
    for event, payload in events:
        kind = event["event_type"]
        _archived_states(payload, config)
        if kind in {"run_started", "cleanup_result", "run_finished"}:
            continue  # Archived plan/history and the separately checked raw cleanup.
        if kind == "environment_after_read_failed":
            if payload.get("error") != error.removeprefix("ValueError: "):
                raise ValueError("Process-guard removal refuses a secondary environment fault")
            payload = {**payload, "error": error}
        if kind not in {"recovery_owner_verification_failed", "environment_after_read_failed"} and re.search(r"failed|error|violation", kind):
            raise ValueError("Process-guard removal refuses a secondary fault event")
        _process_guard_secondary_errors(payload)
    for row in connection.execute(
            "SELECT payload_json FROM combination_samples WHERE run_id=? AND condition_id=? AND attempt_index=?",
            (run_id, rejected[0]["condition_id"], rejected[0]["attempt_index"])):
        payload = json.loads(row[0])
        _process_guard_secondary_errors(payload)
        _archived_states(payload, config)
    validate_accepted_records(connection, run_id, json.loads(state["run"]["plan_json"]))
    return {"parent_state_sha256": state["state_sha256"], "failed_execution_id": execution["execution_id"],
        "failed_condition_id": rejected[0]["condition_id"], "failed_attempt_index": rejected[0]["attempt_index"],
        "expected_primary_error": error, "terminal_event_sha256": _digest([e for e, _ in events]),
        "raw_cleanup_sha256": _digest(cleanup), "accepted_conditions": 37, "total_conditions": 342,
        "historical_cleanup_rewritten": False, "historical_communication_certified": False}


class LiveProcessGuardRemovalResolution:
    """Single-use fresh instrument proof, independent of every OS process check."""

    def __init__(self, config, *, run_id, parent_state_sha256, failed_execution_id,
                 failed_condition_id, failed_attempt_index, expected_primary_error,
                 old_source_sha256, new_source_sha256, config_sha256, live_readback):
        if (not callable(live_readback) or expected_primary_error != PROCESS_GUARD_REMOVAL_ERROR
                or failed_condition_id != "condition-000037" or type(failed_attempt_index) is not int
                or failed_attempt_index < 0 or not run_id or not failed_execution_id):
            raise ValueError("Process-guard removal requires an exact failure and live readback callable")
        _process_guard_removal_sources(old_source_sha256, new_source_sha256)
        self._config, self._readback = config, live_readback
        self._bindings = {"run_id": run_id, "parent_state_sha256": _sha(parent_state_sha256),
            "failed_execution_id": failed_execution_id, "failed_condition_id": failed_condition_id,
            "failed_attempt_index": failed_attempt_index, "expected_primary_error": expected_primary_error,
            "old_source_sha256": deepcopy(old_source_sha256), "new_source_sha256": deepcopy(new_source_sha256),
            "config_sha256": _sha(config_sha256)}
        self._used = False
        self._check_bindings()
        self._issued_proof = self._fresh_readback()
        self._issued_at = datetime.now(timezone.utc)
        self._issued_proof_sha = _digest(self._issued_proof)
        self._summary = None

    def __reduce_ex__(self, protocol):
        raise TypeError("A live process-guard removal resolution cannot be serialized")

    def __copy__(self):
        raise TypeError("A live process-guard removal resolution cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("A live process-guard removal resolution cannot be copied")

    def _check_bindings(self):
        b, config = self._bindings, self._config
        if (self._used or config.snapshot.get("config_sha256") != b["config_sha256"]
                or config.snapshot.get("source_sha256") != b["new_source_sha256"]
                or hashlib.sha256(config.path.read_bytes()).hexdigest() != b["config_sha256"]):
            raise ValueError("Process-guard removal capability consumed or configuration changed")
        sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")}
        if sources != b["new_source_sha256"]:
            raise ValueError("Process-guard removal source changed after review")
        if hashlib.sha256(config.path.with_name("lockin_safety.toml").read_bytes()).hexdigest() != config.snapshot["hardware"]["lockin"]["safety_sha256"]:
            raise ValueError("Process-guard removal safety changed after review")

    def _fresh_readback(self):
        proof = self._readback()
        self._check_bindings()
        return LiveOwnerResolution._validate_readback(proof, self._config, datetime.now(timezone.utc))

    def verify(self, state, plan, connection, *, consume=False):
        self._check_bindings()
        if not 0 <= (datetime.now(timezone.utc) - self._issued_at).total_seconds() <= CAP_MAX_AGE_S:
            raise ValueError("Process-guard removal capability is stale")
        if _digest(self._issued_proof) != self._issued_proof_sha:
            raise ValueError("Process-guard removal original proof changed")
        b = self._bindings
        if (state["state_sha256"] != b["parent_state_sha256"] or plan.get("source_sha256") != b["new_source_sha256"]
                or plan.get("config_sha256") != b["config_sha256"]):
            raise ValueError("Process-guard removal parent/source/configuration changed")
        summary = validate_process_guard_removal_parent(connection, b["run_id"], self._config, b["old_source_sha256"])
        if any(summary.get(k) != b[k] for k in ("parent_state_sha256", "failed_execution_id",
                "failed_condition_id", "failed_attempt_index", "expected_primary_error")):
            raise ValueError("Process-guard removal failed execution or attempt differs")
        if self._summary is not None and summary != self._summary:
            raise ValueError("Process-guard removal terminal evidence changed")
        self._summary = summary
        proof = self._fresh_readback()
        if not 0 <= (datetime.now(timezone.utc) - self._issued_at).total_seconds() <= CAP_MAX_AGE_S:
            raise ValueError("Process-guard removal capability expired during the live frame")
        public = {**deepcopy(b), **deepcopy(summary), "contract": PROCESS_GUARD_REMOVAL_CONTRACT,
            "process_guards_disabled": True, "physical_checks_unchanged": True,
            "issued_at_utc": self._issued_at.isoformat(), "issued_proof": deepcopy(self._issued_proof),
            "issued_proof_sha256": self._issued_proof_sha}
        if consume:
            self._used = True
        return public, proof
