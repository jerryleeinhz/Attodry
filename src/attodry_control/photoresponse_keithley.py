"""Injected-resource query contracts, verified against Keithley manuals offline.

No resource factory, open, write, reset, trigger or error-queue consumption exists
here. These adapters are NOT a commissioned hardware acquisition path. In
particular, a 6221 settings snapshot does not establish that waveform/Delta modes
are disarmed, and an existing 2182 reading has no demonstrated freshness.

References: 622x-901-01 Rev C (2008), 2182A-900-01 Rev B (2017).
SOUR:CURR? follows the 6221 general query syntax; verify it during the separately
authorized real read-only commissioning before using the adapter operationally.
"""
from __future__ import annotations

from copy import deepcopy
import math

from .nkt_config import NktError, number
from .nkt_control import utc_now


def _float(raw):
    value = float(raw.strip())
    if not math.isfinite(value):
        raise NktError("Non-finite instrument response")
    return value


def _flag(raw):
    if raw.strip() not in ("0", "1", "+0", "+1"):
        raise NktError(f"Unknown boolean instrument response: {raw!r}")
    return bool(int(raw))


def _word(raw):
    value = _float(raw)
    if not value.is_integer() or not 0 <= value <= 65535:
        raise NktError("Malformed status word")
    return int(value)


class _Queries:
    is_hardware = True

    def __init__(self, resource, *, expected_idn):
        if not isinstance(expected_idn, str) or not expected_idn.strip():
            raise NktError("An operator-confirmed full identity is required")
        self.resource, self.expected_idn = resource, expected_idn.strip()
        self.last_confirmed_state = None
        self.state_current = False
        self.partial_raw = {}

    def _query(self, command):
        raw = self.resource.query(command)
        self.partial_raw[command] = raw
        if not isinstance(raw, str):
            raise NktError("Expected textual SCPI response")
        return raw.strip()

    def _identity(self, authorize_queries):
        if authorize_queries is not True:
            raise NktError("Queries require explicit authorization")
        self.state_current = False
        self.partial_raw = {}
        identity = self._query("*IDN?")
        if identity != self.expected_idn:
            raise NktError("Instrument identity differs from the expected full IDN")
        return identity

    def _retain(self, state):
        state.update(captured_at_utc=utc_now(), raw_responses=deepcopy(self.partial_raw),
                     acquisition_ready=False, error_queue_consumed=False)
        self.last_confirmed_state = deepcopy(state)
        self.state_current = True
        return state


class Keithley6221Queries(_Queries):
    def snapshot(self, *, authorize_queries=False):
        identity = self._identity(authorize_queries)
        if len(identity.split(",")) < 2 or identity.split(",")[1].strip() != "MODEL 6221":
            raise NktError("6221 adapter requires an explicitly identified MODEL 6221")
        output = _flag(self._query("OUTP:STAT?"))
        current = _float(self._query("SOUR:CURR?"))
        compliance = _float(self._query("SOUR:CURR:COMP?"))
        source_range = _float(self._query("SOUR:CURR:RANG?"))
        autorange = _flag(self._query("SOUR:CURR:RANG:AUTO?"))
        status = _word(self._query("STAT:MEAS:COND?"))
        number(current, "6221 current setting", -0.105, 0.105)
        number(compliance, "6221 voltage compliance", 0.1, 105)
        number(source_range, "6221 source range", 1e-15, 0.105)
        return self._retain({"identity": identity, "output_enabled": output,
                             "source_current_setpoint_a": current, "measured_current_a": None,
                             "voltage_compliance_v": compliance, "current_range_a": source_range,
                             "current_autorange": autorange, "measurement_condition": status,
                             "compliance_tripped": bool(status & 8), "dc_takeover_verified": False})


class Keithley2182Queries(_Queries):
    def snapshot(self, *, authorize_queries=False):
        identity = self._identity(authorize_queries)
        if len(identity.split(",")) < 2 or identity.split(",")[1].strip() not in ("MODEL 2182", "MODEL 2182A"):
            raise NktError("2182 adapter requires an explicitly identified MODEL 2182 or MODEL 2182A")
        function = self._query("SENS:FUNC?")
        channel = _word(self._query("SENS:CHAN?"))
        voltage_range = _float(self._query("SENS:VOLT:DC:RANG?"))
        autorange = _flag(self._query("SENS:VOLT:DC:RANG:AUTO?"))
        nplc = _float(self._query("SENS:VOLT:DC:NPLC?"))
        if channel not in (1, 2):
            raise NktError("Unsupported 2182 voltage channel")
        number(voltage_range, "2182 voltage range", 0.01, 100)
        # The 50 Hz limit is lower; mains/trigger readiness is deliberately unknown.
        number(nplc, "2182 NPLC", 0.01, 60)
        return self._retain({"identity": identity, "function": function, "channel": channel,
                             "voltage_range_v": voltage_range, "voltage_autorange": autorange,
                             "nplc": nplc, "trigger_and_processing_verified": False})

    def fetch_existing_voltage(self, *, authorize_queries=False, max_abs_voltage_v):
        """Diagnostic value only: FETCh? does not demonstrate a new conversion."""
        number(max_abs_voltage_v, "max_abs_voltage_v", 1e-15, 100)
        state = self.snapshot(authorize_queries=authorize_queries)
        if state["function"].strip("\"'").upper() != "VOLT:DC" or state["channel"] != 1:
            raise NktError("Existing reading requires explicitly confirmed DC voltage channel 1")
        self.state_current = False
        raw = self._query("FETC?")
        value = _float(raw)
        # The finite overload sentinel +9.9E37 must never become a measurement.
        number(value, "existing voltage", -max_abs_voltage_v, max_abs_voltage_v)
        return {"voltage_v": value, "raw_response": raw, "fresh": False,
                "accepted": False, "processing_verified": False, "settings_snapshot": state}
