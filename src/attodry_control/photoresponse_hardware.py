"""Strict hardware configuration and electrical orchestration for photoresponse."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from .nkt_config import NktError, integer, keys, number, text
from .photoresponse_devices import CurrentSource6221, Voltmeter2182, Meter2400
from .photoresponse_keithley import _flag
from .photoresponse_transport import Serial2182Transport


TABLES = {"photoresponse_hardware", "photoresponse_6221", "photoresponse_2182",
          "photoresponse_2400", "photoresponse_environment", "photoresponse_storage", "cld1010"}


def identity(value, model):
    text(value, "expected_idn")
    parts = value.split(",")
    if len(parts) < 4 or parts[1].strip() != "MODEL " + model or "CHANGE_ME" in value:
        raise NktError("Configure an exact confirmed full " + model + " IDN")


def address(value):
    text(value, "VISA address")
    if "CHANGE_ME" in value or "::INSTR" not in value.upper():
        raise NktError("Configure an explicit VISA instrument address")


@dataclass(frozen=True)
class ElectricalHardwareConfig:
    timeout_ms: int
    wiring_confirmed: bool
    no_excitation_isolated: bool
    detector_transport: str
    source: dict | None
    detector: dict

    def validate(self, config):
        integer(self.timeout_ms, "timeout_ms", 1000, 120000)
        if type(self.wiring_confirmed) is not bool or type(self.no_excitation_isolated) is not bool:
            raise NktError("Wiring confirmations must be booleans")
        if self.detector_transport not in ("visa", "via_6221"):
            raise NktError("detector_transport must be visa or via_6221")
        if self.detector_transport == "via_6221" and config.detector != "keithley2182a":
            raise NktError("6221 serial forwarding is only for 2182A")
        needs_source = config.excitation == "dc_current" or self.detector_transport == "via_6221"
        if needs_source != (self.source is not None):
            raise NktError("6221 role/serial host does not match measurement mode")
        if self.source is not None:
            s = self.source
            required = {"address", "expected_idn"}
            if config.excitation == "dc_current":
                required |= {"compliance_v", "max_step_a", "settle_s", "tolerance_a"}
            keys(s, "photoresponse_6221", required)
            address(s["address"])
            identity(s["expected_idn"], "6221")
            if config.excitation == "dc_current":
                number(config.max_abs_current_a, "6221 current limit", 1e-13, .105)
                number(s["compliance_v"], "6221 compliance_v", .1, min(105, config.max_abs_voltage_v))
                number(s["max_step_a"], "max_step_a", 1e-13, config.max_abs_current_a)
                number(s["settle_s"], "settle_s", 0, 3600)
                number(s["tolerance_a"], "tolerance_a", 0, s["max_step_a"] / 2)
        d = self.detector
        required = {"expected_idn", "voltage_range_v", "nplc"}
        if self.detector_transport == "visa":
            required.add("address")
        if config.detector == "keithley2400":
            required |= {"current_range_a", "compliance", "max_step_v", "settle_s", "tolerance_v"}
        keys(d, "photoresponse detector", required)
        if self.detector_transport == "visa":
            address(d["address"])
        identity(d["expected_idn"], "2182A" if config.detector == "keithley2182a" else "2400")
        number(d["nplc"], "nplc", .01, 10 if config.detector == "keithley2400" else 50)
        # Readback plus autozero can take multiple integration periods.
        if self.timeout_ms / 1000 < d["nplc"] / 50 * 4 + 1:
            raise NktError("VISA timeout is too short for the selected integration time")
        if config.detector == "keithley2182a":
            if d["voltage_range_v"] not in (.01, .1, 1., 10., 100.):
                raise NktError("Invalid 2182A voltage range")
        else:
            if d["voltage_range_v"] not in (.2, 2., 20., 200.):
                raise NktError("Invalid 2400 voltage range")
            if d["current_range_a"] not in (1e-6, 10e-6, 100e-6, .001, .01, .1, 1.):
                raise NktError("Invalid 2400 current range")
            number(config.max_abs_current_a, "2400 current limit", 1e-12, d["current_range_a"])
            number(d["max_step_v"], "max_step_v", 1e-6, config.max_abs_voltage_v)
            number(d["settle_s"], "settle_s", 0, 3600)
            number(d["tolerance_v"], "tolerance_v", 0, d["max_step_v"] / 2)
            if config.quantity == "current_a":
                number(d["compliance"], "2400 current compliance", 1e-9, config.max_abs_current_a)
            else:
                number(d["compliance"], "2400 voltmeter compliance", .02, 210)
                if d["compliance"] <= config.max_abs_voltage_v:
                    raise NktError("2400 voltmeter compliance must exceed maximum expected voltage")
        number(config.max_abs_voltage_v, "voltage bound", 1e-15, d["voltage_range_v"])
        addresses = self.addresses()
        if len(set(addresses.values())) != len(addresses):
            raise NktError("Source and detector cannot use the same direct VISA address")
        # Validate the exact device contracts before a resource factory can run.
        if config.excitation == "dc_current":
            CurrentSource6221(None, **{k:v for k,v in self.source.items() if k != "address"},
                              max_current_a=config.max_abs_current_a)
        if config.detector == "keithley2400":
            Meter2400(None, **{k:v for k,v in d.items() if k != "address"},
                      current_mode=config.quantity == "current_a", max_voltage_v=config.max_abs_voltage_v,
                      max_current_a=config.max_abs_current_a)

    def require_run(self, config):
        self.validate(config)
        if not self.wiring_confirmed:
            raise NktError("Confirm mode-specific sample wiring before hardware run")
        if config.excitation == "none" and not self.no_excitation_isolated:
            raise NktError("No-excitation measurement requires confirmed excitation-path isolation")

    def addresses(self):
        result = {}
        if self.source is not None:
            result["6221"] = self.source["address"]
        if self.detector_transport == "visa":
            result["detector"] = self.detector["address"]
        return result


def load_electrical_hardware(doc, config):
    h = doc.get("photoresponse_hardware", {})
    keys(h, "photoresponse_hardware", {"timeout_ms", "wiring_confirmed", "no_excitation_isolated", "detector_transport"})
    needs_source = config.excitation == "dc_current" or h["detector_transport"] == "via_6221"
    result = ElectricalHardwareConfig(**h,
        source=deepcopy(doc.get("photoresponse_6221", {})) if needs_source else None,
        detector=deepcopy(doc.get("photoresponse_2182" if config.detector == "keithley2182a" else "photoresponse_2400", {})))
    result.validate(config)
    return result


class HardwareElectrical:
    is_hardware = True

    def __init__(self, config, transports, *, sleep=None):
        self.config, self.transports = config, transports
        h = config.hardware
        h.require_run(config)
        self.source = None
        options = {} if sleep is None else {"sleep": sleep}
        if config.excitation == "dc_current":
            self.source = CurrentSource6221(transports["6221"],
                **{k: v for k, v in h.source.items() if k != "address"},
                max_current_a=config.max_abs_current_a, **options)
        detector_transport = transports.get("detector")
        if h.detector_transport == "via_6221":
            detector_transport = Serial2182Transport(transports["6221"], timeout_s=h.timeout_ms / 1000,
                **options)
        values = {k: v for k, v in h.detector.items() if k != "address"}
        if config.detector == "keithley2182a":
            self.detector = Voltmeter2182(detector_transport, **values, max_voltage_v=config.max_abs_voltage_v)
        else:
            self.detector = Meter2400(detector_transport, **values,
                current_mode=config.quantity == "current_a", max_voltage_v=config.max_abs_voltage_v,
                max_current_a=config.max_abs_current_a, **options)
        self.started = False
        self.cleanup_details = []

    def _host_guard(self):
        if self.config.hardware.detector_transport != "via_6221":
            return
        host = self.transports["6221"]
        if host.query("*IDN?") != self.config.hardware.source["expected_idn"]:
            raise NktError("6221 serial host identity mismatch")
        for mode in ("DELT", "PDEL", "DCON"):
            if _flag(host.query(f"SOUR:{mode}:ARM?")):
                raise NktError("6221 coupled acquisition is armed; serial forwarding is unavailable")
        if self.source is None and _flag(host.query("OUTP:STAT?")):
            raise NktError("No-excitation serial host has active output")

    def preflight(self):
        """Passive ownership check, also required before any environment motion."""
        self._host_guard()
        # Reject all active outputs before any instrument is configured.
        if self.source:
            state = self.source.snapshot()
            if state["output_enabled"] or state["source_current_setpoint_a"] != 0:
                raise NktError("6221 is not zero/off")
        if isinstance(self.detector, Meter2400):
            state = self.detector.snapshot()
            if state["output_enabled"] or state["setpoint"] != 0:
                raise NktError("2400 is not zero/off")
        else:
            self.detector._identity(True)

    def begin(self):
        self.preflight()
        self.started = True  # cleanup owns only instruments whose own begin succeeded
        self.detector.begin()
        if self.source:
            self.source.begin()

    def snapshot(self):
        self._host_guard()
        source = None
        if self.source:
            readback = self.source.snapshot()
            if self.source.ready:
                self.source.check(readback)
            source = {"model": "6221", "output_enabled": readback["output_enabled"],
                      "setpoint": readback["source_current_setpoint_a"], "readback": readback}
        if self.started:
            meter = self.detector.verify()
        elif isinstance(self.detector, Meter2400):
            meter = self.detector.snapshot()
        else:
            meter = self.detector.snapshot(authorize_queries=True)
        if self.config.quantity == "current_a":
            source = {"model": "2400", "output_enabled": meter["output_enabled"],
                      "setpoint": meter["setpoint"], "readback": meter}
        return {"detector": self.config.detector, "excitation": self.config.excitation,
                "source": source, "detector_readback": meter,
                "compliance_tripped": bool(source and source["readback"].get("compliance_tripped")),
                "error": None}

    def prepare(self, bias):
        if self.source:
            self.source.set_bias(bias)
        if isinstance(self.detector, Meter2400):
            self.detector.set_bias(bias if self.config.quantity == "current_a" else None)

    def read(self, *, illuminated):
        reading = self.detector.read_values() if isinstance(self.detector, Meter2400) else self.detector.read_voltage()
        reading["state"] = self.snapshot()
        return reading

    def cleanup(self):
        errors = []
        for device in (self.source, self.detector if isinstance(self.detector, Meter2400) else None):
            if device is not None and device.owned:
                try:
                    result = device.cleanup()
                except (Exception, KeyboardInterrupt) as exc:
                    result = {"safe": False, "errors": [f"{type(device).__name__}: {exc}"]}
                self.cleanup_details.append(result)
                errors.extend(result["errors"])
        if errors:
            raise NktError("; ".join(errors))
        # A voltmeter is intentionally not restored to a possibly armed setup.
        source = None
        if self.source:
            state = self.source.snapshot()
            source = {"model": "6221", "setpoint": state["source_current_setpoint_a"],
                      "output_enabled": state["output_enabled"]}
        elif self.config.quantity == "current_a":
            state = self.detector.snapshot()
            source = {"model": "2400", "setpoint": state["setpoint"], "output_enabled": state["output_enabled"]}
        self._host_guard()
        return {"detector": self.config.detector, "excitation": self.config.excitation,
                "source": source, "compliance_tripped": False, "error": None,
                "cleanup_details": deepcopy(self.cleanup_details)}

    def close(self):
        # VISA context owns and closes all resources exactly once (including relay host).
        pass
