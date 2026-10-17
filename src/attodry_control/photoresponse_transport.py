"""Audited VISA transport reused for CLD; construction performs no I/O."""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time

from .nkt_config import NktError
from .nkt_control import utc_now


def clean_response(raw):
    if not isinstance(raw, str):
        raise NktError("Expected textual instrument response")
    # Preserve the raw frame in the audit; remove optional flow-control bytes
    # only from the value presented to the parser.
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

@contextmanager
def visa_resources(addresses, *, timeout_ms, authorize_connection=False,
                   authorize_writes=False, on_event=None, manager_factory=None):
    """Explicit addresses only; exclusive VISA locks prevent another process taking over."""
    if authorize_connection is not True:
        raise NktError("Hardware connection requires authorization")
    if len(set(addresses.values())) != len(addresses):
        raise NktError("Duplicate VISA resources")
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
            # GPIB uses message EOI; the supported USB CLD uses LF termination.
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
