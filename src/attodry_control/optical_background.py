"""Matched complex lock-in dark subtraction, with no instrument dependencies.

The acquisition owner supplies the complete stable condition/reference context.
This module never infers gate, field, temperature, sample wiring or phase matches.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import statistics
import time

from .nkt_config import NktError, integer, keys, number


@dataclass(frozen=True)
class BackgroundConfig:
    enabled: bool = False
    samples: int = 3
    max_age_s: float = 600.0

    def validate(self):
        if type(self.enabled) is not bool:
            raise NktError("optical_background.enabled must be boolean")
        integer(self.samples, "optical_background.samples", 1, 10000)
        number(self.max_age_s, "optical_background.max_age_s", 0.001, 86400)


def load_background_config(doc):
    table = doc.get("optical_background", {})
    keys(table, "optical_background", set(), {"enabled", "samples", "max_age_s"})
    config = BackgroundConfig(**table)
    config.validate()
    return config


_CHANNEL = re.compile(r"^lockin_(xx|xy)_h([1-9][0-9]*)_(x_v|y_v|amplitude_v|phase_deg)$")
_DELTA = re.compile(r"^lockin_(xx|xy)_h[1-9][0-9]*_delta_")
_METRICS = {"x_v", "y_v", "amplitude_v", "phase_deg"}


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NktError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise NktError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise NktError(f"{name} must be a finite number")
    return result


def _context(context):
    if type(context) is not dict or not context:
        raise NktError("A nonempty complete optical-background context is required")
    try:
        encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise NktError("Optical-background context must contain finite JSON-compatible values") from exc
    return deepcopy(context), hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _channels(reading):
    if type(reading) is not dict or reading.get("module") != "lockin":
        raise NktError("Optical background requires lock-in readings")
    measured = reading.get("measurements")
    if type(measured) is not dict:
        raise NktError("Lock-in reading is missing its measurements")
    if any(_DELTA.match(name) for name in measured):
        raise NktError("Optical background requires original, unsubtracted lock-in readings")
    channels = {}
    for name, value in measured.items():
        match = _CHANNEL.match(name)
        if match:
            role, harmonic, metric = match.groups()
            channel = channels.setdefault(f"{role}:h{harmonic}",
                {"role": role, "harmonic": int(harmonic)})
            channel[metric] = _finite(value, name)
    if not channels:
        raise NktError("No lock-in X/Y channels exist for optical-background subtraction")
    for name, channel in channels.items():
        if not _METRICS <= channel.keys():
            raise NktError(f"Missing lock-in channel metrics for {name}")
        if channel["amplitude_v"] < 0:
            raise NktError(f"Negative lock-in amplitude for {name}")
    return channels


def _quality(reading, channel):
    # Be conservative when historical/global quality is absent. Precise raw
    # per-role flags remain attached below, rather than inventing a clean state.
    clean = reading.get("clean") is True
    valid = clean and reading.get("valid_for_analysis") is True
    role_key = "lockin_" + channel["role"]
    role_evidence = []
    status = reading.get("status") or {}
    for entry in status.get("samples", []):
        selected = entry.get("selected_harmonics", {})
        if (entry.get("harmonic") != channel["harmonic"] and
                selected.get(channel["role"]) != channel["harmonic"]):
            continue
        flags = entry.get("valid_for_analysis_by_role", {})
        if role_key in flags:
            role_evidence.append({"valid_for_analysis": flags[role_key],
                "problems": deepcopy(entry.get("problems_by_role", {}).get(role_key, []))})
            valid = valid and flags[role_key] is True
    return {"clean": clean, "valid_for_analysis": valid,
            "problems": deepcopy(reading.get("problems", [])), "role_evidence": role_evidence}


class MatchedLockinBackground:
    def __init__(self, config, *, clock=time.monotonic):
        config.validate()
        self.config, self.clock = config, clock
        self.baseline = None
        self.invalidation_reason = None

    def invalidate(self, reason):
        if type(reason) is not str or not reason.strip():
            raise NktError("Optical-background invalidation requires a reason")
        self.baseline = None
        self.invalidation_reason = reason

    def capture(self, readings, context):
        if not self.config.enabled:
            raise NktError("Optical background is disabled")
        # A failed capture cannot leave an older baseline silently available.
        self.baseline = None
        self.invalidation_reason = "new baseline capture incomplete"
        if type(readings) not in (list, tuple) or len(readings) != self.config.samples:
            raise NktError("Dark baseline requires exactly optical_background.samples independent readings")
        saved_context, fingerprint = _context(context)
        parsed = [_channels(reading) for reading in readings]
        channel_names = set(parsed[0])
        if any(set(item) != channel_names for item in parsed[1:]):
            raise NktError("Dark baseline is missing a role/harmonic channel in some readings")
        channels = {}
        for name in sorted(channel_names):
            raw = [{**values[name], **_quality(reading, values[name])}
                   for reading, values in zip(readings, parsed)]
            x = _finite(statistics.mean(item["x_v"] for item in raw), "Mean dark X")
            y = _finite(statistics.mean(item["y_v"] for item in raw), "Mean dark Y")
            amplitude = _finite(math.hypot(x, y), "Mean dark complex amplitude")
            phase_defined = x != 0 or y != 0
            channels[name] = {"role": raw[0]["role"], "harmonic": raw[0]["harmonic"],
                "x_v": x, "y_v": y, "amplitude_v": amplitude,
                "phase_deg": math.degrees(math.atan2(y, x)) if phase_defined else None,
                "phase_defined": phase_defined, "count": len(raw), "samples": raw,
                "valid_for_analysis": all(item["valid_for_analysis"] for item in raw)}
        now = _finite(self.clock(), "Dark baseline monotonic time")
        result = {"schema_version": 1, "context": saved_context, "context_sha256": fingerprint,
            "captured_monotonic_s": now, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "samples": len(readings), "channels": channels, "raw_readings": deepcopy(list(readings)),
            "valid_for_analysis": all(item["valid_for_analysis"] for item in channels.values()),
            "method": "mean dark complex X+iY; magnitude/phase derived after averaging"}
        identity = json.dumps({"context": fingerprint, "time": now, "channels": channels},
                              sort_keys=True, separators=(",", ":"), allow_nan=False)
        result["baseline_id"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.baseline = deepcopy(result)
        self.invalidation_reason = None
        return deepcopy(result)

    def apply(self, light_reading, context):
        if not self.config.enabled:
            return deepcopy(light_reading)
        if self.baseline is None:
            raise NktError("No matched dark baseline: " + (self.invalidation_reason or "not captured"))
        _, fingerprint = _context(context)
        if fingerprint != self.baseline["context_sha256"]:
            raise NktError("Dark baseline context mismatch; recapture for this condition/reference/phase")
        age = _finite(self.clock(), "Light reading monotonic time") - self.baseline["captured_monotonic_s"]
        if age < 0 or age > self.config.max_age_s:
            raise NktError("Dark baseline expired or monotonic clock reversed")
        channels = _channels(light_reading)
        if set(channels) != set(self.baseline["channels"]):
            raise NktError("Dark/light role/harmonic channel mismatch; missing matched baseline channel")
        result = deepcopy(light_reading)
        assessment = {"baseline_id": self.baseline["baseline_id"],
            "context_sha256": fingerprint, "age_s": age, "max_age_s": self.config.max_age_s,
            "method": "(light X+iY) - mean(dark X+iY)", "channels": {},
            "valid_for_analysis": self.baseline["valid_for_analysis"],
            "original_quality": {name: deepcopy(light_reading.get(name))
                                 for name in ("clean", "valid_for_analysis", "problems")}}
        for name, channel in channels.items():
            dark = self.baseline["channels"][name]
            valid = dark["valid_for_analysis"] and _quality(light_reading, channel)["valid_for_analysis"]
            assessment["channels"][name] = {"valid_for_analysis": valid,
                                            "phase_defined": None, "difference_emitted": False}
            assessment["valid_for_analysis"] = assessment["valid_for_analysis"] and valid
        # No difference is promoted if any dark/light channel is invalid. Raw
        # light values and the original dark capture remain independently saved.
        if assessment["valid_for_analysis"]:
            for name, channel in channels.items():
                dark = self.baseline["channels"][name]
                x = _finite(channel["x_v"] - dark["x_v"], "Differential X")
                y = _finite(channel["y_v"] - dark["y_v"], "Differential Y")
                amplitude = _finite(math.hypot(x, y), "Differential amplitude")
                prefix = f"lockin_{channel['role']}_h{channel['harmonic']}_delta_"
                result["measurements"].update({prefix + "x_v": x, prefix + "y_v": y,
                                                prefix + "amplitude_v": amplitude})
                phase_defined = x != 0 or y != 0
                if phase_defined:
                    result["measurements"][prefix + "phase_deg"] = math.degrees(math.atan2(y, x))
                assessment["channels"][name].update(phase_defined=phase_defined, difference_emitted=True)
        else:
            result["clean"] = result["valid_for_analysis"] = False
            result.setdefault("problems", []).append("Invalid dark/light observations prevent optical background subtraction")
        result["background"] = assessment
        return result
