"""Pure 6221 amplitude programming policy, shared by planning and validation.

SPEC-6221 Rev C p1 gives the source-range programming grid. We deliberately
program that conservative grid; neither the seven-digit AMPL? string nor the
16-bit arbitrary-waveform DAC is asserted to be the WAVE:AMPL parameter LSB.
No instrument queries, retries, waits, or measured-current claims belong here.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math


AMPLITUDE_RULE = "6221-source-range-grid-v1"
READBACK_RULE = "seven-significant-digits-with-binary32-rounding-bound-v1"
# Nominal range, documented source programming increment (amperes).
SOURCE_GRIDS = tuple((Decimal(r), Decimal(s)) for r, s in (
    ("2e-9", "1e-13"), ("2e-8", "1e-12"), ("2e-7", "1e-11"),
    ("2e-6", "1e-10"), ("2e-5", "1e-9"), ("2e-4", "1e-8"),
    ("2e-3", "1e-7"), ("2e-2", "1e-6"), ("1e-1", "1e-5")))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def readback_tolerance_a(peak):
    """Bound text rounding separately from the programmed-current increment.

    The observed D04 21 nA response crosses a decimal rounding tie. A binary32
    half-ULP plus half a seven-significant-digit decimal unit covers that evidence
    without using current accuracy or the much larger source programming step as
    a settings tolerance. This is a verification bound, not a firmware claim.
    """
    if not _finite(peak) or peak <= 0:
        raise ValueError("6221 amplitude must be positive and finite")
    decimal_half = float(Decimal(10) ** (Decimal(str(peak)).adjusted() - 6)) / 2
    binary_half = math.ldexp(1.0, math.frexp(peak)[1] - 25)
    return decimal_half + binary_half + 2 * math.ulp(float(peak))


def quantize_amplitude(current_rms_a, offset_a, max_current_peak_a):
    if not all(_finite(x) for x in (current_rms_a, offset_a, max_current_peak_a)):
        raise ValueError("6221 amplitude policy requires finite numeric inputs")
    peak = current_rms_a * math.sqrt(2)
    if not 2e-12 <= peak <= .105 or not 2e-12 <= max_current_peak_a <= .105:
        raise ValueError("6221 amplitude is outside the supported peak envelope")
    desired, offset, ceiling = map(lambda x: Decimal(str(x)),
                                   (peak, abs(offset_a), max_current_peak_a))
    if desired + offset > ceiling:
        raise ValueError("6221 abs(offset) + requested peak exceeds the configured DUT limit")
    candidates = []
    previous_capacity = Decimal(0)
    for nominal, step in SOURCE_GRIDS:
        capacity = nominal * Decimal("1.05")
        low = max(Decimal("2e-12"), previous_capacity - offset)
        high = min(capacity, ceiling) - offset
        previous_capacity = capacity
        if high < low:
            continue
        first = (low / step).to_integral_value(rounding=ROUND_CEILING)
        last = (high / step).to_integral_value(rounding=ROUND_FLOOR)
        if last < first:
            continue
        nearest_below = (desired / step).to_integral_value(rounding=ROUND_FLOOR)
        for count in (nearest_below, nearest_below + 1):
            value = min(last, max(first, count)) * step
            # A boundary belongs to its lowest covering range.
            best = next(r for r, _ in SOURCE_GRIDS if value + offset <= r * Decimal("1.05"))
            if best == nominal:
                candidates.append((abs(value - desired), value, nominal, step))
    if not candidates:
        raise ValueError("No legal 6221 amplitude setting within the approved limit")
    distance = min(item[0] for item in candidates)
    # Ignore only host-float noise at an exact half-step; this is far smaller
    # than a source increment and cannot enlarge the admissible envelope.
    tie_noise = Decimal(str(2 * math.ulp(peak)))
    _, value, nominal, step = min(
        (item for item in candidates if item[0] <= distance + tie_noise), key=lambda item: item[1])
    return {"rule": AMPLITUDE_RULE, "readback_rule": READBACK_RULE,
            "requested_current_rms_a": float(current_rms_a), "requested_current_peak_a": peak,
            "programmed_current_peak_a": float(value),
            "programmed_current_peak_scpi": format(value, "f"),
            "programmed_current_rms_a": float(value) / math.sqrt(2),
            "planning_source_range_a": float(nominal), "programming_step_a": float(step),
            "offset_a": float(offset_a), "max_total_peak_a": float(max_current_peak_a),
            "rounding": "nearest_within_limit_ties_lower",
            "readback_abs_tolerance_a": readback_tolerance_a(float(value))}


def validate_quantized_amplitude(state, current_rms_a, offset_a, max_current_peak_a):
    expected = quantize_amplitude(current_rms_a, offset_a, max_current_peak_a)
    recorded = state.get("amplitude_quantization")
    # Exact structured provenance: reject missing/extra fields and bool-as-number.
    if (not isinstance(recorded, dict) or recorded != expected
            or any(type(recorded[k]) is not type(v) for k, v in expected.items())):
        raise ValueError("6221 amplitude quantization evidence differs from this condition")
    value = state.get("current_peak_a")
    offset = state.get("offset_a")
    if (not _finite(value) or not _finite(offset)
            or abs(value - expected["programmed_current_peak_a"]) > expected["readback_abs_tolerance_a"]):
        raise ValueError("6221 source setting differs from quantized amplitude")
    # Rounding tolerance never enlarges the approved amplitude+offset ceiling.
    if (value < 2e-12 or Decimal(str(value)) + abs(Decimal(str(offset)))
            > Decimal(str(max_current_peak_a))):
        raise ValueError("6221 readback peak plus offset exceeds the approved envelope")
    return expected
