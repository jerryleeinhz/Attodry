from dataclasses import replace
from pathlib import Path

from attodry_control.nkt_config import load_nkt_config
from attodry_control.optical_config import load_pem_config, load_pm_config, load_scan_config
from attodry_control.nkt_control import SimulatedNkt
from attodry_control.pem import Pem, SimulatedPemTransport
from attodry_control.pm100d import Pm100d, SimulatedPmResource
from attodry_control.optical_scan import OpticalScan


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/optical_simulation.toml"


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        if seconds <= 0:
            raise AssertionError("Nonpositive sleep")
        self.value += seconds


def devices():
    clock = Clock()
    nkt = load_nkt_config(EXAMPLE)
    scan, pc, mc = load_scan_config(EXAMPLE, nkt)
    backend = SimulatedNkt(nkt)
    pem = Pem(pc, SimulatedPemTransport(), clock=clock, sleep=clock.sleep)
    meter = Pm100d(mc, SimulatedPmResource(), clock=clock)
    return clock, nkt, scan, backend, pem, meter


def scanner(*, mode="direct", use_pem=True, use_pm=True, provider=None):
    clock, nkt, scan, backend, pem, meter = devices()
    if mode == "power_stabilized":
        import tomllib
        from attodry_control.optical_config import FeedbackConfig, WindowConfig
        raw = tomllib.loads(EXAMPLE.read_text())["power_feedback"]
        feedback = FeedbackConfig(**{**raw, "window": WindowConfig(**raw["window"])})
        scan = replace(scan, mode=mode, feedback=feedback)
    scan = replace(scan, use_pem=use_pem, use_power_meter=use_pm,
                   peak_retardance_waves=0.25 if use_pem else None)
    if provider:
        meter.resource.power_provider = lambda: provider(backend)
    return OpticalScan(nkt, scan, backend, pem=pem if use_pem else None,
                       meter=meter if use_pm else None, clock=clock, sleep=clock.sleep)
