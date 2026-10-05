"""Synthetic configuration and stateful resources; never real addresses or hardware."""
from __future__ import annotations

import tomllib


PHOTONICS_LOCKIN_TOML = '''
[visa]
backend = "default"
timeout_ms = 5000

[photonics_lockin]
schema_version = 1
safety_file = "photonics_lockin_safety.toml"
reference_min_hz = 49000.0
reference_max_hz = 51000.0
reference_expected_hz = 50027.0
pair_tolerance_hz = 1.0
settle_time_constants = 10.0
sample_interval_s = 0.01

[photonics_lockin.source]
wiring = "single_ended"
load = "high_impedance"
amplitude_definition = "differential_rms_into_50_ohm_loads"
dc_mode = "common"
dc_offset_v = 0.0

[lockin_xx]
model = "SR865A"
address = "FAKE::XX"
reference_source = "external_ttl"
external_reference_edge = "rising"
sine_output_connected = true
input_mode = "a_minus_b"
shield_grounding = "float"
input_coupling = "ac"
time_constant_s = 0.01
filter_slope_db_oct = 24
sensitivity_mode = "fixed"
sensitivity_full_scale_v = 0.001
phase_shift_deg = 0.0
harmonics = [1, 3]

[lockin_xx.sr865a]
input_range_v_peak = 0.1
reference_input_impedance_ohm = 1000000.0
sync_output_mode = "unipolar_sync"
current_status_supported = true

[lockin_xy]
model = "SR830"
address = "FAKE::XY"
reference_source = "external_ttl"
external_reference_edge = "rising"
sine_output_connected = false
input_mode = "a_minus_b"
shield_grounding = "float"
input_coupling = "ac"
time_constant_s = 0.01
filter_slope_db_oct = 24
sensitivity_mode = "fixed"
sensitivity_full_scale_v = 0.001
phase_shift_deg = 0.0
harmonics = [2]

[lockin_xy.sr830]
reserve_mode = "normal"

[lockin_sweep]
excitation_points_v_rms = [0.004, 0.006]
'''

PHOTONICS_LOCKIN_SAFETY_TOML = '''
schema_version = 1
minimum_source_voltage_v = 0.000001
maximum_source_voltage_v = 0.010
cleanup_source_voltage_v = 0.004

[lockin_xx]
allowed_fixed_full_scales_v = [0.001, 0.01]

[lockin_xy]
allowed_fixed_full_scales_v = [0.001, 0.01]
'''


def fixture_document():
    return tomllib.loads(PHOTONICS_LOCKIN_TOML)


def safety_document():
    return tomllib.loads(PHOTONICS_LOCKIN_SAFETY_TOML)


def reversed_sine_document():
    """PEM -> XY SR865A SINE OUT -> XX SR830 REF IN; XX excites sample."""
    doc = fixture_document()
    doc["combination_scan"] = {"reference_topology": "pem_xy_xx_sine"}
    xx, xy = doc["lockin_xx"], doc["lockin_xy"]
    xx["model"], xy["model"] = "SR830", "SR865A"
    xy["sr865a"], xx["sr830"] = xx.pop("sr865a"), xy.pop("sr830")
    xx.update(reference_source="external_sine", external_reference_edge="sine_zero_crossing", harmonics=[1])
    xy["sine_output_connected"] = True
    xy["sr865a"]["sync_output_mode"] = "preserve"
    doc["photonics_lockin"]["source"].update(amplitude_definition="sr830_instrument_rms_setting", dc_mode="not_supported")
    doc["photonics_lockin"]["reference_output"] = {
        "wiring": "single_ended", "load": "high_impedance",
        "amplitude_definition": "differential_rms_into_50_ohm_loads",
        "amplitude_v_rms": 0.2, "dc_mode": "common", "dc_offset_v": 0.0,
        "destination": "lockin_xx_ref_in", "sample_connected": False,
    }
    return doc


def reversed_sine_safety_document():
    policy = safety_document()
    policy["minimum_source_voltage_v"] = 0.004
    return policy


class FakeResource:
    def __init__(self, model, role):
        self.model = model
        self.role = role
        self.queries = []
        self.writes = []
        self.closed = False
        self.on_query = None
        self.on_write = None
        self.responses = {
            "*IDN?": f"Stanford_Research_Systems,{model},{role},v1.00",
            "FMOD?": "0", "RSLP?": "1", "FREQ?": "50027", "HARM?": "1",
            "SLVL?": "0.004", "ISRC?": "1", "IGND?": "0", "ICPL?": "0",
            "ILIN?": "0", "SENS?": "17", "RMOD?": "1", "OFLT?": "8" if model == "SR865A" else "6",
            "OFSL?": "3", "PHAS?": "0", "LIAS?": "0", "ERRS?": "0", "*ESR?": "0",
            "RSRC?": "1", "RTRG?": "1", "REFZ?": "1", "IVMD?": "0", "SCAL?": "9",
            "IRNG?": "2", "ADVFILT?": "0", "SYNC?": "0", "SOFF?": "0", "REFM?": "0",
            "BLAZEX?": "2", "FREQEXT?": "50027", "FREQINT?": "50027", "FREQDET?": "50027",
            "SNAP? X,Y": "0.0001,0.0002", "CUROVLDSTAT?": "0",
        }

    def query(self, command):
        self.queries.append(command)
        if self.on_query:
            self.on_query(self, command)
        if command == "SNAP? 1,2,3,4,9":
            return "0.0001,0.0002,0.00022360679775,63.4349488," + self.responses["FREQ?"]
        value = self.responses[command]
        if isinstance(value, BaseException):
            raise value
        if command in ("LIAS?", "ERRS?", "*ESR?"):
            self.responses[command] = "0"
        return value

    def write(self, command):
        self.writes.append(command)
        if self.on_write:
            self.on_write(self, command)
        name, value = command.split(" ", 1)
        self.responses[name + "?"] = value
        if name == "HARM":
            self.responses["FREQDET?"] = str(int(value) * float(self.responses["FREQEXT?"]))
        if self.model == "SR865A":
            self.responses["*ESR?"] = "64"
        elif name in ("HARM", "FMOD"):
            self.responses["LIAS?"] = "16"
        elif name == "OFLT":
            self.responses["LIAS?"] = "32"

    def close(self):
        self.closed = True


class FakeManager:
    def __init__(self, document=None):
        document = fixture_document() if document is None else document
        self.resources = {document[role]["address"]: FakeResource(document[role]["model"], role)
                          for role in ("lockin_xx", "lockin_xy")}
        self.opened = []
        self.closed = False

    def open_resource(self, address):
        self.opened.append(address)
        return self.resources[address]

    def close(self):
        self.closed = True
