"""Audited VISA and 6221 serial relay transports; construction performs no I/O."""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time

from .nkt_config import NktError
from .nkt_control import utc_now


def clean_response(raw):
    if not isinstance(raw, str):
        raise NktError("Expected textual instrument response")
    # The 6221 forwards the 2182A's XON/XOFF bytes. Preserve them in the audit.
    value = raw.replace("\x11", "").replace("\x13", "").strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1].replace('""', '"').strip()
    if any(ord(c) < 32 for c in value):
        raise NktError("Unexpected control character in instrument response")
    return value


class VisaTransport:
    def __init__(self, resource, *, role, authorize_writes=False, on_event=None, clock=time.perf_counter):
        self.resource, self.role = resource, role
        self.authorize_writes = authorize_writes is True
        self.on_event = on_event
        self.clock = clock
        self.lock = threading.RLock()
        self.poisoned = False
        self.transcript = []

    def _io(self, command, *, query=False):
        if not isinstance(command, str) or any(c in command for c in "\r\n;"):
            raise NktError("Only one SCPI command per transaction is allowed")
        row = {"phase": "instrument_io", "role": self.role, "command": command,
               "query": query, "captured_at_utc": utc_now(), "response": None}
        with self.lock:
            row["started_monotonic_s"] = self.clock()
            self.transcript.append(row)
            try:
                row["response"] = self.resource.query(command) if query else self.resource.write(command)
                return clean_response(row["response"]) if query else None
            except (Exception, KeyboardInterrupt) as exc:
                self.poisoned = True
                row["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                row["finished_monotonic_s"] = self.clock()
                row["duration_s"] = row["finished_monotonic_s"] - row["started_monotonic_s"]
                row["finished_at_utc"] = utc_now()
                if self.on_event:
                    try:
                        self.on_event(dict(row))
                    except (Exception, KeyboardInterrupt) as exc:
                        self.poisoned = True
                        row["audit_error"] = str(exc)
                        if "error" not in row:
                            raise

    def query(self, command):
        if not command.endswith("?"):
            raise NktError("Expected query")
        if command.upper().lstrip(":") in ("READ?", "SYST:ERR?") and not self.authorize_writes:
            raise NktError("Trigger/error-queue consumption requires write authorization")
        if self.poisoned:
            raise NktError("Uncertain pending response; reopen and diagnose before continuing")
        return self._io(command, query=True)

    def write(self, command):
        if not self.authorize_writes:
            raise NktError("Instrument settings require explicit authorization")
        if self.poisoned:
            raise NktError("Transport failed; only best-effort shutdown is available")
        return self._io(command)

    def emergency_off(self, command):
        if not self.authorize_writes or command not in ("OUTP:STAT 0", "OUTP 0", "OUTP1:STAT 0", "OUTP2:STAT 0"):
            raise NktError("Invalid emergency shutdown")
        # A failed bus can still accept an off write. It does NOT confirm off.
        return self._io(command)


class Serial2182Transport:
    """One shared lock spans SEND + ENTER; never retry an uncertain query."""
    def __init__(self, host: VisaTransport, *, timeout_s=10, sleep=time.sleep,
                 clock=time.monotonic, poll_interval_s=.1):
        if (not 0 < timeout_s < float("inf") or
                not 0 < poll_interval_s <= .1):
            raise NktError("Serial timeout must be positive and poll interval within (0, 0.1] s")
        self.host, self.timeout_s, self.sleep, self.clock = host, timeout_s, sleep, clock
        self.poll_interval_s = poll_interval_s
        self.poisoned = False
        self.transcript = []

    @staticmethod
    def _send(command):
        if not command or any(c in command for c in "\r\n;"):
            raise NktError("Invalid serial command")
        return 'SYST:COMM:SER:SEND "' + command.replace('"', '""') + '"'

    def query(self, command):
        if not command.endswith("?"):
            raise NktError("Expected downstream query")
        if command.upper().lstrip(":") in ("READ?", "SYST:ERR?") and not self.host.authorize_writes:
            raise NktError("Downstream trigger/status consumption requires authorization")
        with self.host.lock:
            if self.poisoned or self.host.poisoned:
                raise NktError("Serial response ownership is uncertain; new session required")
            row = {"command": command, "responses": [], "started_monotonic_s": self.clock(),
                   "poll_wait_requested_s": 0.0}
            self.transcript.append(row)
            try:
                # SEND is a transport envelope; an IDN/settings query is still read-only.
                deadline = self.clock() + self.timeout_s
                self.host._io(self._send(command))
                interval = self.poll_interval_s
                while True:
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise TimeoutError("2182A serial response timed out")
                    wait = min(interval, remaining)
                    self.sleep(wait)
                    row["poll_wait_requested_s"] += wait
                    response = self.host.query("SYST:COMM:SER:ENT?")
                    row["responses"].append(response)
                    if self.clock() > deadline:
                        raise TimeoutError("2182A serial response timed out")
                    if response:
                        return response
                    interval = min(.1, interval * 2)
            except (Exception, KeyboardInterrupt) as exc:
                self.poisoned = True
                row["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                row["finished_monotonic_s"] = self.clock()
                row["duration_s"] = row["finished_monotonic_s"] - row["started_monotonic_s"]

    def write(self, command):
        with self.host.lock:
            if self.poisoned:
                raise NktError("Serial session failed")
            self.host.write(self._send(command))


@contextmanager
def visa_resources(addresses, *, timeout_ms, authorize_connection=False,
                   authorize_writes=False, on_event=None, manager_factory=None):
    """Explicit addresses only; exclusive VISA locks prevent another process taking over."""
    if authorize_connection is not True:
        raise NktError("Hardware connection requires authorization")
    if len(set(addresses.values())) != len(addresses):
        raise NktError("Duplicate VISA resources; relay must share the host session")
    if manager_factory is None:
        import pyvisa
        manager_factory = pyvisa.ResourceManager
    manager = manager_factory()
    opened = []
    try:
        transports = {}
        for role, address in addresses.items():
            resource = manager.open_resource(address, open_timeout=timeout_ms)
            opened.append(resource)
            resource.timeout = timeout_ms
            # Read the entire GPIB message through EOI. The 6221 serial relay
            # returns two trailing LFs; stopping at the first leaves a stale LF
            # that can be mistaken for the next query's response.
            resource.read_termination = None if address.upper().startswith("GPIB") else "\n"
            resource.write_termination = "\n"
            resource.lock_excl(timeout=timeout_ms)
            transports[role] = VisaTransport(resource, role=role,
                authorize_writes=authorize_writes, on_event=on_event)
        yield transports
    finally:
        errors = []
        for resource in reversed(opened):
            try:
                resource.close()
            except (Exception, KeyboardInterrupt) as exc:
                errors.append(str(exc))
        try:
            manager.close()
        except (Exception, KeyboardInterrupt) as exc:
            errors.append(str(exc))
        if errors:
            raise NktError("VISA close failed: " + "; ".join(errors))
