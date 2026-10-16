"""PEM configuration reused from integration-photonics-nonlinear-hall.

Only the PEM contract is extracted: PM/feedback remain owned by power_config.
Loading and validation never open resources.
"""
from dataclasses import dataclass, fields
from .nkt_config import NktError, keys, number, text

def peak_retardance_nm(wavelength_nm, waves):
    return number(wavelength_nm, "wavelength_nm", 0.001, 1e6) * number(waves, "waves", 0.000001, 10)


@dataclass(frozen=True)
class PemConfig:
    backend: str
    io_timeout_s: float
    settle_timeout_s: float
    poll_interval_s: float
    wavelength_min_nm: float
    wavelength_max_nm: float
    amplitude_min_nm: float
    amplitude_max_nm: float
    amplitude_tolerance_nm: float
    frequency_min_hz: float
    frequency_max_hz: float
    finish_action: str
    port: str = ""
    expected_idn: str = ""

    def validate(self):
        if self.backend not in ("simulation", "serial"):
            raise NktError("PEM backend must be simulation or serial")
        number(self.io_timeout_s, "io_timeout_s", 0.3, 60)
        number(self.settle_timeout_s, "settle_timeout_s", 0.3, 3600)
        number(self.poll_interval_s, "poll_interval_s", 0.001, self.settle_timeout_s)
        number(self.wavelength_min_nm, "wavelength_min_nm", 0.001, 1e6)
        number(self.wavelength_max_nm, "wavelength_max_nm", self.wavelength_min_nm, 1e6)
        number(self.amplitude_min_nm, "amplitude_min_nm", 0, 1e6)
        number(self.amplitude_max_nm, "amplitude_max_nm", self.amplitude_min_nm, 1e6)
        number(self.amplitude_tolerance_nm, "amplitude_tolerance_nm", 0, 1)
        number(self.frequency_min_hz, "frequency_min_hz", 1, 1e7)
        number(self.frequency_max_hz, "frequency_max_hz", self.frequency_min_hz, 1e7)
        if self.finish_action not in ("disable_ack", "leave_active"):
            raise NktError("PEM finish_action must be disable_ack or leave_active; physical-off query unavailable")
        text(self.port, "port", empty=True)
        text(self.expected_idn, "expected_idn", empty=True)

    def target(self, wavelength_nm, waves):
        self.validate()
        number(wavelength_nm, "PEM wavelength", self.wavelength_min_nm, self.wavelength_max_nm)
        return number(peak_retardance_nm(wavelength_nm, waves), "PEM amplitude",
                      self.amplitude_min_nm, self.amplitude_max_nm)

    def require_hardware(self, writes=False):
        self.validate()
        if self.backend != "serial" or not self.port or "CHANGE_ME" in self.port:
            raise NktError("Configure PEM serial port before connection")
        if writes and (not self.expected_idn or "CHANGE_ME" in self.expected_idn):
            raise NktError("Confirm exact PEM firmware IDN before writes")



def load_pem_config(doc):
    """Load the exact existing [pem] contract; no inferred head or local limits."""
    if "pem" not in doc:
        return None
    optional = {"port", "expected_idn"}
    keys(doc["pem"], "pem", {f.name for f in fields(PemConfig)} - optional, optional)
    result = PemConfig(**doc["pem"])
    result.validate()
    return result
