"""OS ownership and file-only process checks for explicit hardware recovery."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.parse import urlparse


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError("Jupyter owner verification refuses HTTP redirects")


def urlopen(request, *, timeout):
    # Local authentication must never be forwarded through an environment proxy
    # or a redirect. Keep a small injectable boundary for offline tests.
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


@contextmanager
def resource_mutexes(config):
    """Windows leases cover overlapping resources even in another checkout."""
    if sys.platform != "win32":
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    resources = []
    if config.cryostat is not None:
        resources.append("cryostat:" + config.cryostat.cryostat.com_port.strip().upper())
    if config.lockin is not None:
        resources.extend("visa:" + device.address.strip().upper()
                         for device in (config.lockin.lockin_xx, config.lockin.lockin_xy))
    if config.smu is not None:
        resources.extend("visa:" + device.address.strip().upper()
                         for device in config.smu.hardware.by_role().values())
    handles = []
    try:
        for resource in sorted(set(resources)):
            name = "Global\\AttoDRY_combination_" + hashlib.sha256(resource.encode()).hexdigest()
            handle = kernel.CreateMutexW(None, False, name)
            if not handle:
                raise ValueError("Unable to reserve an exclusive hardware resource")
            result = kernel.WaitForSingleObject(handle, 0)
            if result not in {0, 0x80}:  # WAIT_OBJECT_0 / WAIT_ABANDONED
                kernel.CloseHandle(handle)
                raise ValueError("Another command owns a selected hardware resource")
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


def process_inventory():
    if sys.platform != "win32":
        raise ValueError("Real recovery owner verification currently requires Windows")
    command = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        "@(Get-CimInstance Win32_Process | ForEach-Object {"
        "@{pid=$_.ProcessId;parent=$_.ParentProcessId;name=$_.Name;command=$_.CommandLine;"
        "created=$_.CreationDate.ToUniversalTime().ToString('o')}}) | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or not rows:
        raise ValueError("Process ownership inventory is unavailable")
    return rows


class _IdleKernels(set):
    """Kernel IDs plus the verified server/launcher process tree (no tokens)."""

    allowed_server_ids = frozenset()


def _idle_jupyter(rows):
    """Authenticate only to localhost runtime files; tokens never enter audit."""
    runtime = Path(os.environ.get("APPDATA", "")) / "jupyter" / "runtime"
    idle_kernels = _IdleKernels()
    by_pid = {row["pid"]: row for row in rows}
    candidates = {row["pid"] for row in rows if
        row["name"].lower().startswith("jupyter") or
        (row["name"].lower().startswith("python") and
         re.search(r"jupyter(?:-lab|\.exe).*\blab\b|jupyter-lab", row.get("command") or "", re.I))}
    servers = set()
    for row in rows:
        name, command = row["name"].lower(), row.get("command") or ""
        if not name.startswith("python") or not re.search(r"jupyter(?:-lab|\.exe).*\blab\b|jupyter-lab", command, re.I):
            continue
        path = runtime / f"jpserver-{row['pid']}.json"
        if not path.is_file():
            continue  # Launcher wrappers are verified against the server tree below.
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("pid", row["pid"]) != row["pid"]:
                raise ValueError("Jupyter runtime identity does not match its live process")
            parsed = urlparse(record["url"])
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.scheme != "http":
                raise ValueError("Jupyter owner URL is not local")
            request = Request(record["url"].rstrip("/") + "/api/kernels",
                              headers={"Authorization": "token " + record["token"]})
            with urlopen(request, timeout=5) as response:
                kernels = json.load(response)
            if not isinstance(kernels, list) or any(k.get("execution_state") != "idle" for k in kernels):
                raise ValueError("Analysis kernels must be idle before hardware recovery")
            idle_kernels.update(k["id"] for k in kernels)
            servers.add(row["pid"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Cannot verify Jupyter analysis owner is idle") from exc
    ancestors = set()
    for pid in servers:
        visited = set()
        while pid in by_pid and pid not in visited:
            visited.add(pid)
            ancestors.add(pid)
            pid = by_pid[pid].get("parent")
    if candidates - ancestors:
        raise ValueError("Unknown Jupyter launcher/server without a verified idle process tree")
    idle_kernels.allowed_server_ids = frozenset(candidates & ancestors)
    return idle_kernels


def validate_inventory(rows, *, own_pid, idle_kernels):
    """Unknown Python and instrument GUIs are blockers, never terminated."""
    by_pid = {row["pid"]: row for row in rows}
    if own_pid not in by_pid or not by_pid[own_pid].get("created") or not by_pid[own_pid].get("command"):
        raise ValueError("Current recovery process identity is unknown")
    python_names = {"python.exe", "pythonw.exe", "python3.exe"}
    evidence = []
    for row in rows:
        name, command = row.get("name", "").lower(), row.get("command") or ""
        if re.search(r"atto(?:dry|control)", name, re.I):
            raise ValueError(f"Instrument GUI is active: PID {row['pid']}")
        if name not in python_names and not name.startswith("jupyter"):
            continue
        if row["pid"] == own_pid:
            evidence.append(row)
            continue
        if not row.get("created") or not command:
            raise ValueError(f"Unknown Python/Jupyter identity: PID {row['pid']}")
        monitor = re.search(r"-m\s+attodry_control\.(?:combination_cli\s+monitor\b|"
                            r"(?:lockin|temperature|magnetic_field)_progress_monitor\b|magnetic_field_monitor\b)", command)
        kernel = re.search(r"-m\s+ipykernel_launcher\b.*kernel-([a-zA-Z0-9-]+)\.json", command)
        server = re.search(r"jupyter(?:-lab|\.exe).*\blab\b|jupyter-lab", command, re.I)
        allowed_servers = getattr(idle_kernels, "allowed_server_ids", frozenset())
        if monitor or (kernel and kernel.group(1) in idle_kernels) or (server and row["pid"] in allowed_servers):
            evidence.append(row)
            continue
        raise ValueError(f"Competing or unknown Python owner: PID {row['pid']}")
    return evidence


class RecoveryOwner:
    def __init__(self, config, *, inventory=process_inventory, idle_reader=_idle_jupyter):
        self.config = config
        self.inventory = inventory
        self.idle_reader = idle_reader
        self.identity = None
        self.last_evidence = None
        self.locked = False
        self.analysis_identities = None
        self.analysis_kernels = None

    def check(self):
        if not self.locked:
            raise ValueError("Recovery must hold the exclusive station lock")
        snapshot = self.config.snapshot
        if hashlib.sha256(self.config.path.read_bytes()).hexdigest() != snapshot["config_sha256"]:
            raise ValueError("Configuration changed after recovery review")
        paths = {p.name: p for p in Path(__file__).parent.glob("*.py")}
        current = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
        if current != snapshot["source_sha256"]:
            raise ValueError("Source changed after recovery review")
        if self.config.lockin is not None:
            safety = self.config.path.with_name("lockin_safety.toml")
            if hashlib.sha256(safety.read_bytes()).hexdigest() != snapshot["hardware"]["lockin"]["safety_sha256"]:
                raise ValueError("Safety configuration changed after recovery review")
        rows = self.inventory()
        if self.analysis_identities is None:
            kernels = self.idle_reader(rows)
        else:
            kernels = self.analysis_kernels
        evidence = validate_inventory(rows, own_pid=os.getpid(), idle_kernels=kernels)
        analysis = {row["pid"]: row for row in evidence if row["pid"] != os.getpid()
                    and ("jupyter" in row["name"].lower()
                         or re.search(r"jupyter|ipykernel_launcher", row["command"], re.I))}
        if self.analysis_identities is not None and analysis != self.analysis_identities:
            raise ValueError("Analysis owner identities changed during recovery")
        if self.analysis_identities is None:
            # Idle is required at handoff. The exact known file-analysis owners
            # may subsequently plot; newly created/replaced kernels are blocked.
            self.analysis_identities = deepcopy(analysis)
            self.analysis_kernels = deepcopy(kernels)
        own = next(row for row in evidence if row["pid"] == os.getpid())
        if self.identity is not None and own != self.identity:
            raise ValueError("Recovery process identity changed")
        self.identity = deepcopy(own)
        self.last_evidence = {"pid": own["pid"], "created": own["created"],
                              "command": own["command"], "verified_owners": deepcopy(evidence)}
        return self.last_evidence

    @contextmanager
    def acquire(self, *, verify=True):
        with resource_mutexes(self.config):
            with self._acquire_file(verify=verify):
                yield self

    @contextmanager
    def _acquire_file(self, *, verify):
        directory = self.config.path.parent
        root = directory.parent if directory.name.lower() == "config" else directory
        destination = root / "run_data" / ".combination-station.lock"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a+b") as file:
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("Another hardware command holds the exclusive station lock") from exc
            self.locked = True
            try:
                if verify:
                    self.check()
                yield self
            finally:
                self.locked = False
                file.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file, fcntl.LOCK_UN)
