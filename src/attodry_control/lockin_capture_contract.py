"""Versioned raw-pair layout shared by combination capture and archive review.

A mixed pair is one sequential XX/XY read, with no common harmonic. Its role
map names the actual detector settings; old scalar-harmonic pairs stay readable.
"""

SINGLE_ROLE_PAIR_POLICY = "fixed_role_single_pair_v1"


def selected_sample_harmonic(sample, role):
    layout = sample.get("capture_layout")
    if layout is None:
        return sample["harmonic"]
    if layout != SINGLE_ROLE_PAIR_POLICY or sample.get("harmonic") is not None:
        raise ValueError("Unknown or contradictory Lock-in capture layout")
    key = role if role.startswith("lockin_") else "lockin_" + role
    harmonic = sample["harmonics_by_role"][key]
    if type(harmonic) is not int or harmonic < 1:
        raise ValueError("Mixed Lock-in pair lacks a valid role harmonic")
    return harmonic
