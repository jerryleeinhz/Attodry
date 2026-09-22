"""Hardware-free configuration for the standalone NKT module."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
import tomllib


class NktError(ValueError):
    pass


MODES = ("broadband", "varia_bandpass", "lltf_swir")
NKT_TABLES = {"nkt_source", "nkt_varia", "nkt_lltf", "nkt_run"}
OTHER_TABLES = {
    "project", "cryostat", "magnet", "temperature_stability", "temperature_run",
    "temperature_scan", "cleanup", "visa", "lockin_xx", "lockin_xy",
    "lockin_sweep", "gate_top", "gate_bottom", "smu_bias", "three_smu_run",
}


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NktError(f"{name} must be a number, not {value!r}")
    if not math.isfinite(value) or not low <= value <= high:
        raise NktError(f"{name} must be finite and within [{low}, {high}]")
    return float(value)


def tenths(value):
    """Reject unrepresentable requests instead of silently shifting filter edges."""
    result = round(value * 10)
    if not math.isclose(value * 10, result, abs_tol=1e-8, rel_tol=0):
        raise NktError(f"{value} is not representable in 0.1 units")
    return result


def integer(value, name, low, high):
    number(value, name, low, high)
    if not isinstance(value, int):
        raise NktError(f"{name} must be an integer")
    return value


def text(value, name, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise NktError(f"{name} must be a {'possibly empty ' if empty else ''}string")
    return value


def keys(table, name, required, optional=()):
    if not isinstance(table, dict):
        raise NktError(f"{name} must be a table")
    missing = set(required) - table.keys()
    unknown = table.keys() - set(required) - set(optional)
    if missing or unknown:
        raise NktError(f"{name}: missing {sorted(missing)}, unknown {sorted(unknown)}")


@dataclass(frozen=True)
class NktPoint:
    source_level_pct: float
    wavelength_nm: float | None = None
    bandwidth_nm: float | None = None
    nd_pct: float | None = None
    pulse_picker_ratio: int | None = None

    def validate(self, mode, max_level_pct, allowed_ratios):
        tenths(number(self.source_level_pct, "source_level_pct", 0, max_level_pct))
        if self.pulse_picker_ratio is not None:
            integer(self.pulse_picker_ratio, "pulse_picker_ratio", 1, 65535)
            if self.pulse_picker_ratio not in allowed_ratios:
                raise NktError("pulse_picker_ratio is not in the operator-confirmed list")
        if mode == "varia_bandpass":
            number(self.wavelength_nm, "wavelength_nm", 400, 840)
            number(self.bandwidth_nm, "bandwidth_nm", 10, 100)
            tenths(number(self.nd_pct, "nd_pct", 0, 100))
            for edge in self.edges_nm:
                tenths(number(edge, "VARIA edge_nm", 400, 840))
        elif mode == "lltf_swir":
            number(self.wavelength_nm, "wavelength_nm", 1000, 2300)
            if self.bandwidth_nm is not None or self.nd_pct is not None:
                raise NktError("LLTF has no programmable bandwidth or VARIA ND")
        elif mode == "broadband":
            if any(v is not None for v in (self.wavelength_nm, self.bandwidth_nm, self.nd_pct)):
                raise NktError("broadband does not accept wavelength, bandwidth or ND")
        else:
            raise NktError(f"Unknown mode: {mode}")

    @property
    def edges_nm(self):
        return (self.wavelength_nm - self.bandwidth_nm / 2,
                self.wavelength_nm + self.bandwidth_nm / 2)


@dataclass(frozen=True)
class NktConfig:
    backend: str
    mode: str
    source_control: str
    max_level_pct: float
    allowed_pp_ratios: tuple[int, ...]
    points: tuple[NktPoint, ...]
    emit: bool
    manual_route: str
    timeout_s: float
    poll_interval_s: float
    dwell_s: float
    output_directory: Path
    run_name: str
    note: str
    dll_path: str = ""
    port: str = ""
    source_address: int = 1
    source_serial: str = ""
    varia_address: int = 2
    varia_serial: str = ""
    varia_monitor_present: bool = False

    def validate(self):
        if self.backend not in ("simulation", "nkt_sdk") or self.mode not in MODES:
            raise NktError("Unsupported backend or optical mode")
        if self.source_control not in ("current", "power"):
            raise NktError("source_control must be current or power")
        number(self.max_level_pct, "max_level_pct", 0, 100)
        number(self.timeout_s, "timeout_s", 0.01, 3600)
        number(self.poll_interval_s, "poll_interval_s", 0.001, self.timeout_s)
        number(self.dwell_s, "dwell_s", 0, 86400)
        text(self.manual_route, "manual_route")
        text(self.run_name, "run_name")
        text(self.note, "note", empty=True)
        text(self.source_serial, "source_serial", empty=True)
        text(self.varia_serial, "varia_serial", empty=True)
        if not isinstance(self.emit, bool) or not isinstance(self.varia_monitor_present, bool):
            raise NktError("emit and monitor_present must be booleans")
        if not self.points or len(self.points) > 10000:
            raise NktError("Provide 1 to 10000 explicit scan points")
        for ratio in self.allowed_pp_ratios:
            integer(ratio, "allowed_pp_ratios", 1, 65535)
        for point in self.points:
            point.validate(self.mode, self.max_level_pct, self.allowed_pp_ratios)
        for value in (self.source_address, self.varia_address):
            integer(value, "module address", 1, 255)
        if self.mode == "varia_bandpass" and self.source_address == self.varia_address:
            raise NktError("Source and VARIA must have distinct module addresses")

    def require_hardware(self, writes=False):
        self.validate()
        if self.backend != "nkt_sdk":
            raise NktError("Hardware commands require backend=nkt_sdk")
        if self.mode == "lltf_swir":
            raise NktError("LLTF vendor SDK/API is not yet verified; use simulation")
        for name, value in (("dll_path", self.dll_path), ("port", self.port)):
            if not value or "CHANGE_ME" in value:
                raise NktError(f"Configure {name} before connecting")
        if "," in self.port or "\x00" in self.port or not self.port.isascii():
            raise NktError("Exactly one explicit ASCII port is required")
        if writes:
            serials = [("source_serial", self.source_serial)]
            if self.mode == "varia_bandpass":
                serials.append(("varia_serial", self.varia_serial))
            for name, value in serials:
                if not value or "CHANGE_ME" in value:
                    raise NktError(f"Confirm {name} before writes")

    def snapshot(self):
        result = asdict(self)
        result["output_directory"] = str(self.output_directory)
        return result


def load_nkt_config(path: str | Path) -> NktConfig:
    path = Path(path).resolve()
    with path.open("rb") as stream:
        doc = tomllib.load(stream)
    keys(doc, "top level", {"nkt_source", "nkt_run"}, OTHER_TABLES | NKT_TABLES)
    run, source = doc["nkt_run"], doc["nkt_source"]
    keys(run, "nkt_run", {"backend", "mode", "emit", "manual_route", "timeout_s",
         "poll_interval_s", "dwell_s", "output_directory", "run_name", "note", "points"})
    keys(source, "nkt_source", {"control", "max_level_pct", "allowed_pp_ratios"},
         {"dll_path", "port", "address", "expected_serial"})
    if not isinstance(source["allowed_pp_ratios"], list):
        raise NktError("allowed_pp_ratios must be an array")
    if not isinstance(run["points"], list):
        raise NktError("points must be an array of tables")
    points = []
    for i, item in enumerate(run["points"]):
        keys(item, f"points[{i}]", {"source_level_pct"},
             {"wavelength_nm", "bandwidth_nm", "nd_pct", "pulse_picker_ratio"})
        points.append(NktPoint(**item))
    varia = {}
    if run["mode"] == "varia_bandpass":
        varia = doc.get("nkt_varia", {})
        keys(varia, "nkt_varia", {"monitor_present"}, {"address", "expected_serial"})
    if run["mode"] == "lltf_swir":
        keys(doc.get("nkt_lltf", {}), "nkt_lltf", set())
    directory = Path(text(run["output_directory"], "output_directory"))
    config = NktConfig(
        backend=run["backend"], mode=run["mode"], source_control=source["control"],
        max_level_pct=source["max_level_pct"], allowed_pp_ratios=tuple(source["allowed_pp_ratios"]),
        points=tuple(points), emit=run["emit"], manual_route=run["manual_route"],
        timeout_s=run["timeout_s"], poll_interval_s=run["poll_interval_s"], dwell_s=run["dwell_s"],
        output_directory=directory if directory.is_absolute() else path.parent / directory,
        run_name=run["run_name"], note=run["note"],
        dll_path=text(source.get("dll_path", ""), "dll_path", empty=True),
        port=text(source.get("port", ""), "port", empty=True),
        source_address=source.get("address", 1), source_serial=source.get("expected_serial", ""),
        varia_address=varia.get("address", 2), varia_serial=varia.get("expected_serial", ""),
        varia_monitor_present=varia.get("monitor_present", False),
    )
    if config.backend == "nkt_sdk":
        if "address" not in source or (config.mode == "varia_bandpass" and "address" not in varia):
            raise NktError("Hardware requires explicitly configured module addresses")
    config.validate()
    return config
