"""Offline native proof checks for the versioned mixed-harmonic capture layout."""
from dataclasses import fields

from .models import LockinRole
from .sr865a import Sr865a, Sr865aError
from .sr865a_settings import sensitivity_code, time_constant_code


def _no_wait(seconds):
    raise ValueError("native settings proof cannot add waits")


class _RecordedSettings:
    """Consume one complete settings trace; never discover or contact hardware."""

    def __init__(self, raw):
        # Current read_settings has 22 replies, including three IVMD checks.
        if not isinstance(raw, (list, tuple)) or len(raw) != 22:
            raise ValueError("native settings proof has an incomplete query trace")
        self.raw = raw
        self.index = 0

    def query(self, command):
        if self.index >= len(self.raw):
            raise ValueError("native settings proof is missing a reply")
        record = self.raw[self.index]
        self.index += 1
        if (not isinstance(record, dict) or record.get("command") != command
                or record.get("error") is not None or not isinstance(record.get("response"), str)):
            raise ValueError("native settings proof has a contradictory/failed query")
        return record["response"]

    def write(self, command):
        raise ValueError("native settings proof cannot issue commands")

    def close(self):
        raise ValueError("native settings proof has no hardware connection")


def _same(actual, expected):
    if isinstance(expected, bool):
        return type(actual) is bool and actual == expected
    if isinstance(expected, (int, float)):
        return type(actual) in (int, float) and actual == expected
    return actual == expected


def validate_native_capture_proofs(before, after, config):
    """Bind both fresh SR865A snapshots to raw replies and archived targets.

    This is deliberately limited to the new mixed-pair contract. Legacy archive
    layouts retain their existing validation; no settings or status are queried.
    """
    targets = {key: config[key] for key in (
        "external_reference_edge", "input_mode", "shield_grounding", "input_coupling",
        "filter_slope_db_oct", "time_constant_s")}
    targets.update({
        "reference_source": "external", "advanced_filter": False, "synchronous_filter": False,
        "reference_input_impedance_ohm": config["sr865a"]["reference_input_impedance_ohm"],
        "input_range_v_peak": config["sr865a"]["input_range_v_peak"],
    })
    baseline_fields = {"sine_output_v", "source_offset_v", "source_dc_mode",
                       "sync_output_mode", "phase_shift_deg"}
    decoded = []
    for settings in (before, after):
        native = settings.get("receiver_invariants")
        if (not isinstance(native, dict) or settings.get("receiver_invariants_error") is not None
                or settings.get("model") != "SR865A" or settings.get("reserve_code") is not None
                or native.get("source_output_policy") != "preserve_disconnected_receiver"):
            raise ValueError("formal native receiver proof is missing or unsafe")
        trace = _RecordedSettings(native.get("raw"))
        try:
            value = Sr865a(trace, LockinRole.XY, sleep=_no_wait).read_settings()
        except (Sr865aError, TypeError, KeyError) as exc:
            raise ValueError("formal native settings raw trace is invalid") from exc
        if trace.index != len(trace.raw):
            raise ValueError("formal native settings proof has extra replies")
        physical = {field.name: getattr(value, field.name) for field in fields(value)
                    if field.name != "raw"}
        if any(not _same(native.get(key), expected) for key, expected in physical.items()):
            raise ValueError("formal native settings contradict their raw query replies")
        if any(not _same(native.get(key), expected) for key, expected in targets.items()):
            raise ValueError("formal native receiver differs from its archived configuration")
        for key in ("harmonic", "sensitivity_full_scale_v", "time_constant_s"):
            if not _same(settings.get(key), physical[key]):
                raise ValueError("formal native settings contradict compatibility readbacks")
        if (type(settings.get("harmonic")) is not int
                or type(settings.get("sensitivity_code")) is not int
                or type(settings.get("time_constant_code")) is not int
                or settings["sensitivity_code"] != sensitivity_code(value.sensitivity_full_scale_v)
                or settings["time_constant_code"] != time_constant_code(value.time_constant_s)):
            raise ValueError("formal native setting codes contradict physical readbacks")
        baseline = native.get("source_output_baseline")
        if (not isinstance(baseline, dict) or set(baseline) != baseline_fields
                or any(not _same(baseline[key], physical[key]) for key in baseline_fields)):
            raise ValueError("formal native disconnected-source baseline is inconsistent")
        decoded.append(physical)
    if decoded[0] != decoded[1]:
        raise ValueError("formal native receiver invariants changed during capture")
