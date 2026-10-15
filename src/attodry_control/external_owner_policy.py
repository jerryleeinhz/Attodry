"""File-pinned exceptions for separately approved, named Python applications.

This module never starts or stops a process. The inventory adapter can also be
passed to an older ``RecoveryOwner(inventory=...)`` without changing that owner.
Inventory providers must include the OS-reported ``executable`` path; argv[0]
alone does not establish which interpreter is running.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PYTHON_NAMES = {"python.exe", "pythonw.exe", "python3.exe"}
_ENTRY_KEYS = {"command", "interpreter_path", "interpreter_sha256",
               "script_path", "script_sha256"}
_PYTHON_FLAGS = {"-u", "-s", "-S", "-B", "-E", "-I", "-O", "-OO", "-q"}


def _digest(value, label):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"External owner {label} must be a lowercase SHA256")
    return value


def _text(value, label):
    if (not isinstance(value, str) or not value or
            any(ord(character) < 32 for character in value)):
        raise ValueError(f"External owner {label} must be nonempty text without control characters")
    return value


def _absolute_path(value, label):
    value = _text(value, label)
    drive, tail = ntpath.splitdrive(value)
    windows_absolute = bool(drive and tail.startswith(("/", "\\")))
    # A Windows drive-relative/root-relative path must not pass as a POSIX path.
    if not windows_absolute and (drive or value.startswith("\\") or not os.path.isabs(value)):
        raise ValueError(f"External owner {label} must be a fully absolute path")
    return value


def _path_identity(value):
    # Windows process executable paths are case insensitive. Do not normalize
    # full commands: only the exact reviewed command string is eligible.
    return ntpath.normcase(ntpath.normpath(value))


def _file_sha256(path, label):
    candidate = Path(path)
    try:
        if not candidate.is_file():
            raise ValueError(f"External owner {label} is not a regular file")
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"Cannot read external owner {label}") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("External owner policy contains duplicate JSON keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"External owner policy contains invalid JSON number: {value}")


def _windows_argv(command):
    """Decode Windows CRT quoting without using a shell or executing anything."""
    _text(command, "command")
    arguments = []
    index = 0
    while index < len(command):
        while index < len(command) and command[index] == " ":
            index += 1
        if index == len(command):
            break
        argument = []
        quoted = False
        while index < len(command) and (quoted or command[index] != " "):
            if command[index] == "\\":
                end = index
                while end < len(command) and command[end] == "\\":
                    end += 1
                count = end - index
                if end < len(command) and command[end] == '"':
                    argument.extend("\\" * (count // 2))
                    if count % 2:
                        argument.append('"')
                        index = end + 1
                        continue
                    index = end  # The quote below changes quoting state.
                else:
                    argument.extend("\\" * count)
                    index = end
                    continue
            if command[index] == '"':
                if quoted and index + 1 < len(command) and command[index + 1] == '"':
                    argument.append('"')
                    index += 2
                    continue
                quoted = not quoted
                index += 1
            else:
                argument.append(command[index])
                index += 1
        if quoted:
            raise ValueError("External owner command has unmatched quotes")
        arguments.append("".join(argument))
    return arguments


def _protected_process(row, own_pid):
    name = (row.get("name") or "").lower()
    command = row.get("command") or ""
    return ((own_pid is not None and row.get("pid") == own_pid) or name.startswith("jupyter") or
            bool(re.search(r"atto(?:dry|control)", name, re.I)) or
            bool(re.search(r"jupyter|ipykernel_launcher|attodry_control", command, re.I)))


def _validate_entry(entry):
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
        raise ValueError("External owner policy entry has unexpected or missing fields")
    command = _text(entry["command"], "command")
    interpreter = _absolute_path(entry["interpreter_path"], "interpreter path")
    script = _absolute_path(entry["script_path"], "script path")
    for key in ("interpreter_sha256", "script_sha256"):
        _digest(entry[key], key)
    if ntpath.basename(interpreter).lower() not in _PYTHON_NAMES:
        raise ValueError("External owner interpreter must be an explicitly named Python executable")
    if ntpath.splitext(script)[1].lower() not in {".py", ".pyw"}:
        raise ValueError("External owner policy requires a named Python script")
    argv = _windows_argv(command)
    if not argv or _path_identity(_absolute_path(argv[0], "command interpreter")) != _path_identity(interpreter):
        raise ValueError("External owner command does not bind the declared interpreter")
    index = 1
    while index < len(argv):
        if argv[index] in _PYTHON_FLAGS:
            index += 1
        elif argv[index:index + 2] == ["-X", "utf8"]:
            index += 2
        else:
            break
    if index < len(argv) and argv[index] == "--":
        index += 1
    if index >= len(argv) or argv[index].startswith("-"):
        raise ValueError("External owner policy refuses stdin, -c, -m, and unknown Python options")
    if _path_identity(_absolute_path(argv[index], "command script")) != _path_identity(script):
        raise ValueError("External owner command does not bind the declared script")
    if _protected_process({"name": ntpath.basename(interpreter), "command": command}, None):
        raise ValueError("External owner policy cannot exempt controller or analysis processes")
    return deepcopy(entry)


class ExternalOwnerPolicy:
    """Approve only exact commands and file bytes explicitly frozen by a caller.

    ``audit_sink`` receives each newly observed full identity before it can be
    excluded from the ownership inventory. A failing sink blocks the exception.
    Restarts are permitted only when command/executable/artifact checks all pass;
    evidence retains every distinct PID/creation identity.
    """

    def __init__(self, policy_path, expected_sha256, *, audit_sink=None):
        self.policy_path = Path(_absolute_path(str(policy_path), "policy path"))
        self.policy_sha256 = _digest(expected_sha256, "policy SHA256")
        if audit_sink is not None and not callable(audit_sink):
            raise ValueError("External owner audit sink must be callable")
        self._audit_sink = audit_sink
        self._seen = {}
        self._observations = []
        self._current = []
        self._entries = self._load()
        self._verify_files()

    def _load(self):
        try:
            raw = self.policy_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != self.policy_sha256:
                raise ValueError("External owner policy SHA256 changed or does not match")
            policy = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                parse_constant=_reject_constant)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Cannot read a valid external owner policy") from exc
        if (not isinstance(policy, dict) or set(policy) != {"version", "allowed_processes"} or
                type(policy["version"]) is not int or policy["version"] != 1):
            raise ValueError("External owner policy schema/version is invalid")
        values = policy["allowed_processes"]
        if not isinstance(values, list) or not values:
            raise ValueError("External owner policy requires explicit process entries")
        entries = [_validate_entry(value) for value in values]
        if len({entry["command"] for entry in entries}) != len(entries):
            raise ValueError("External owner policy commands must be distinct")
        return entries

    def _verify_files(self):
        if _file_sha256(self.policy_path, "policy") != self.policy_sha256:
            raise ValueError("External owner policy SHA256 changed")
        for entry in self._entries:
            for prefix in ("interpreter", "script"):
                if _file_sha256(entry[f"{prefix}_path"], prefix) != entry[f"{prefix}_sha256"]:
                    raise ValueError(f"External owner {prefix} SHA256 changed")

    @property
    def current_approvals(self):
        return deepcopy(self._current)

    @property
    def observations(self):
        return deepcopy(self._observations)

    def filter_inventory(self, rows, *, own_pid=None):
        """Keep all nonapproved rows, including every original protected owner."""
        if own_pid is None:
            own_pid = os.getpid()
        if type(own_pid) is not int or own_pid <= 0:
            raise ValueError("External owner inventory needs an explicit valid controller PID")
        self._current = []
        self._verify_files()  # Required even when the external application is absent.
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("External owner process inventory is malformed")
        pids = [row.get("pid") for row in rows]
        if (any(type(pid) is not int or pid < 0 for pid in pids) or
                len(set(pids)) != len(pids)):
            raise ValueError("External owner process inventory has invalid or duplicate PIDs")
        for row in rows:
            if row["pid"] == 0 and not (
                    {"pid", "parent", "name", "created", "command", "executable"} <= row.keys()
                    and row.get("name") == "System Idle Process" and type(row.get("parent")) is int
                    and row["parent"] == 0 and row.get("command") is None
                    and row.get("executable") is None and isinstance(row.get("created"), str)
                    and bool(row["created"])):
                raise ValueError("External owner PID zero is reserved for the native System Idle Process")
            if (not isinstance(row.get("name"), str) or not row["name"] or
                    any(row.get(key) is not None and not isinstance(row[key], str)
                        for key in ("command", "created", "executable"))):
                raise ValueError("External owner process inventory has malformed identity fields")
        commands = {entry["command"]: entry for entry in self._entries}
        kept, approved = [], []
        for row in rows:
            key = (row["pid"], row.get("created"))
            if key in self._seen and self._identity(row) != self._seen[key]["process"]:
                raise ValueError("Approved external owner identity changed within one process instance")
            if (_protected_process(row, own_pid) or
                    (row.get("name") or "").lower() not in _PYTHON_NAMES or
                    row.get("command") not in commands):
                kept.append(row)
                continue
            entry = commands[row["command"]]
            identity = self._identity(row)
            if _path_identity(identity["executable"]) != _path_identity(entry["interpreter_path"]):
                raise ValueError("External owner actual executable does not match the approved interpreter")
            if identity["name"].lower() != ntpath.basename(entry["interpreter_path"]).lower():
                raise ValueError("External owner process name and actual interpreter disagree")
            if key not in self._seen:
                event = {"event": "external_owner_approved", "policy_sha256": self.policy_sha256,
                         "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                         "process": identity,
                         "interpreter_sha256": entry["interpreter_sha256"],
                         "script_sha256": entry["script_sha256"]}
                if self._audit_sink is not None:
                    self._audit_sink(deepcopy(event))
                self._seen[key] = deepcopy(event)
                self._observations.append(deepcopy(event))
            approved.append(deepcopy(self._seen[key]))
        self._current = approved
        return kept

    @staticmethod
    def _identity(row):
        if type(row.get("parent")) is not int or row["parent"] < 0:
            raise ValueError("External owner parent PID is unknown")
        identity = {key: _text(row.get(key), key)
                    for key in ("name", "command", "created", "executable")}
        identity["executable"] = _absolute_path(identity["executable"], "actual executable")
        created = identity["created"]
        try:
            parsed = datetime.fromisoformat(created[:-1] + "+00:00" if created.endswith("Z") else created)
            if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                raise ValueError
        except (ValueError, OverflowError) as exc:
            raise ValueError("External owner creation time must be a known UTC timestamp") from exc
        return {"pid": row["pid"], "parent": row["parent"], **identity}

    def inventory_adapter(self, inventory, *, own_pid=None):
        """Wrap a read-only OS inventory for an unchanged RecoveryOwner."""
        if not callable(inventory):
            raise ValueError("External owner inventory provider must be callable")

        def adapted_inventory():
            return self.filter_inventory(inventory(), own_pid=own_pid)

        return adapted_inventory
