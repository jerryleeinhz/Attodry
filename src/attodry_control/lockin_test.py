from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
import time
from typing import Callable, Sequence

from .config import RunMode, load_config
from .models import LockinRole
from .sr830 import (
    AuthorizationRequired,
    DualSr830Controller,
    PAIR_FREQUENCY_ABS_TOLERANCE_HZ,
    Sr830,
    Sr830AcquisitionError,
    Sr830Diagnostic,
    Sr830Error,
    Sr830HarmonicSample,
    configure_minimum_excitation_pair,
)


DEFAULT_FREQUENCY_SWEEP_HZ = (
    17.777,
    25.0,
    35.5,
    50.0,
    70.7,
    100.0,
    141.0,
    200.0,
    282.0,
    398.0,
    562.0,
    794.0,
    1000.0,
)
DEFAULT_EXCITATION_SWEEP_V = (
    0.004,
    0.006,
    0.010,
    0.016,
    0.026,
    0.040,
    0.064,
    0.100,
    0.160,
    0.252,
    0.400,
)
SR830_OUTPUT_RESISTANCE_OHM = 50.0
# The external SR830 frequency readback showed 54 ppm jitter at 50 Hz while
# remaining locked and error-free. This sweep-only tolerance leaves measured
# margin for that jitter; unlock and error status remain unconditional failures.
SWEEP_FREQUENCY_REL_TOLERANCE = 100e-6


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone dual-SR830 test tool. It never imports cryostat or gate drivers."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser(
        "discover", help="List VISA resources without opening instruments."
    )
    discover.set_defaults(handler=_run_discover)

    diagnose = subparsers.add_parser(
        "diagnose", help="Query two SR830 units without changing their settings."
    )
    _add_pair_arguments(diagnose)
    diagnose.add_argument("--samples", type=_positive_integer, default=1)
    diagnose.add_argument("--interval-s", type=_nonnegative_float, default=1.0)
    diagnose.add_argument(
        "--consume-status-latches",
        action="store_true",
        help=(
            "Query LIAS?/ERRS?. These queries clear latched status bits on the SR830."
        ),
    )
    diagnose.set_defaults(handler=_run_diagnose)

    configure = subparsers.add_parser(
        "configure-minimum",
        help=(
            "Set the confirmed xx/xy reference roles with both SINE OUT levels at "
            "the 4 mVrms minimum."
        ),
    )
    _add_pair_arguments(configure)
    configure.add_argument(
        "--frequency-hz",
        type=_positive_float,
        help="Override the config frequency. Default: config value or 17.777 Hz.",
    )
    configure.add_argument(
        "--authorize-writes",
        action="store_true",
        help="Explicitly authorize SR830 setting writes for this command.",
    )
    configure.add_argument(
        "--confirm-xy-sine-disconnected",
        action="store_true",
        help="Confirm lockin_xy SINE OUT is physically disconnected.",
    )
    configure.set_defaults(handler=_run_configure)

    harmonics = subparsers.add_parser(
        "measure-harmonics",
        help="Measure one ordered xx/xy 1/2/3-harmonic cycle and restore harmonic 1.",
    )
    _add_pair_arguments(harmonics)
    harmonics.add_argument(
        "--frequency-hz",
        type=_positive_float,
        help="Override the config frequency. Default: config value or 17.777 Hz.",
    )
    harmonics.add_argument(
        "--settle-s",
        type=_nonnegative_float,
        default=2.0,
        help="Wait after each paired harmonic change. Default: 2 seconds.",
    )
    harmonics.add_argument(
        "--authorize-writes",
        action="store_true",
        help="Explicitly authorize HARM 1/2/3 and minimum-output cleanup writes.",
    )
    harmonics.add_argument(
        "--confirm-xy-sine-disconnected",
        action="store_true",
        help="Confirm lockin_xy SINE OUT is physically disconnected.",
    )
    harmonics.set_defaults(handler=_run_harmonics)

    frequency_sweep = subparsers.add_parser(
        "sweep-frequency",
        help="Sweep lockin_xx internal frequency at 4 mVrms and restore baseline.",
    )
    _add_pair_arguments(frequency_sweep)
    _add_sweep_arguments(frequency_sweep)
    frequency_sweep.add_argument(
        "--points-hz",
        type=_positive_float_list,
        default=DEFAULT_FREQUENCY_SWEEP_HZ,
        help="Comma-separated increasing frequencies. Default: 17.777 Hz to 1 kHz.",
    )
    frequency_sweep.set_defaults(handler=_run_frequency_sweep)

    excitation_sweep = subparsers.add_parser(
        "sweep-excitation",
        help="Sweep lockin_xx SINE OUT and record nominal path current.",
    )
    _add_pair_arguments(excitation_sweep)
    _add_sweep_arguments(excitation_sweep)
    excitation_sweep.add_argument(
        "--points-v",
        type=_positive_float_list,
        default=DEFAULT_EXCITATION_SWEEP_V,
        help="Comma-separated increasing RMS source voltages. Default: 4-400 mV.",
    )
    excitation_sweep.add_argument(
        "--series-resistance-ohm", type=_positive_float, required=True
    )
    excitation_sweep.add_argument(
        "--device-resistance-ohm", type=_nonnegative_float, required=True
    )
    excitation_sweep.add_argument(
        "--max-device-current-a", type=_positive_float, required=True
    )
    excitation_sweep.add_argument(
        "--max-device-voltage-v", type=_positive_float, required=True
    )
    excitation_sweep.add_argument(
        "--confirm-no-50ohm-termination",
        action="store_true",
        help="Confirm no external 50 ohm termination is present.",
    )
    excitation_sweep.set_defaults(handler=_run_excitation_sweep)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except KeyboardInterrupt:
        print(
            "Interrupted. If a write-enabled test had started, its safe-state cleanup "
            "was attempted. Manually verify both HARM readbacks, lockin_xx at 4 mVrms, "
            "and lockin_xx at 17.777 Hz before disconnecting the device.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(exit_code)


def run(
    argv: Sequence[str] | None = None,
    *,
    resource_manager_factory: Callable[[], object] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    factory = resource_manager_factory or _load_resource_manager_factory()
    return args.handler(args, factory)


def _add_pair_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help="Hardware TOML supplying semantic SR830 addresses, timeout, and frequency.",
    )
    parser.add_argument("--xx-address", help="Override lockin_xx.address from --config.")
    parser.add_argument("--xy-address", help="Override lockin_xy.address from --config.")
    parser.add_argument(
        "--timeout-ms",
        type=_positive_integer,
        help="Override visa.timeout_ms from --config. Default without config: 5000.",
    )


def _add_sweep_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--settle-s", type=_nonnegative_float, default=1.5)
    parser.add_argument("--samples-per-point", type=_positive_integer, default=3)
    parser.add_argument("--sample-interval-s", type=_nonnegative_float, default=0.3)
    parser.add_argument(
        "--xx-sensitivity-code",
        type=_sensitivity_code,
        default=21,
        help="Temporary lockin_xx sensitivity code. Default 21 (20 mV).",
    )
    parser.add_argument(
        "--authorize-writes",
        action="store_true",
        help="Explicitly authorize only the setting writes documented for this sweep.",
    )
    parser.add_argument(
        "--confirm-xy-sine-disconnected",
        action="store_true",
        help="Confirm lockin_xy SINE OUT is physically disconnected.",
    )


def _run_discover(args: argparse.Namespace, factory: Callable[[], object]) -> int:
    manager = factory()
    try:
        resources = tuple(manager.list_resources())
    finally:
        manager.close()
    print(json.dumps({"resources": resources}, indent=2))
    return 0


def _run_diagnose(args: argparse.Namespace, factory: Callable[[], object]) -> int:
    settings = _resolve_pair_settings(args)
    _validate_distinct_addresses(settings["xx_address"], settings["xy_address"])
    had_problem = False
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        for index in range(args.samples):
            if index:
                time.sleep(args.interval_s)
            xx = lockin_xx.read_diagnostic(
                consume_status_latches=args.consume_status_latches
            )
            xy = lockin_xy.read_diagnostic(
                consume_status_latches=args.consume_status_latches
            )
            problems = _diagnostic_problems(xx, xy)
            had_problem = had_problem or bool(problems)
            print(
                json.dumps(
                    {
                        "sample_index": index,
                        "captured_unix_s": time.time(),
                        "safety_status_complete": args.consume_status_latches,
                        "limitations": (
                            []
                            if args.consume_status_latches
                            else [
                                "LIAS?/ERRS? status latches were not consumed; "
                                "unlock, overload, and instrument-error acceptance "
                                "cannot be determined from this sample"
                            ]
                        ),
                        "lockin_xx": asdict(xx),
                        "lockin_xy": asdict(xy),
                        "problems": problems,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return 1 if had_problem else 0


def _run_configure(args: argparse.Namespace, factory: Callable[[], object]) -> int:
    settings = _resolve_pair_settings(args)
    _validate_distinct_addresses(settings["xx_address"], settings["xy_address"])
    if not args.authorize_writes:
        raise AuthorizationRequired("SR830 setting writes were not explicitly authorized.")
    if not args.confirm_xy_sine_disconnected:
        raise AuthorizationRequired(
            "Physical disconnection of lockin_xy SINE OUT was not confirmed."
        )
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        result = configure_minimum_excitation_pair(
            lockin_xx,
            lockin_xy,
            frequency_hz=settings["frequency_hz"],
            authorize_writes=args.authorize_writes,
            confirm_xy_sine_disconnected=args.confirm_xy_sine_disconnected,
        )
        print(
            json.dumps(
                {
                    "configured": True,
                    "before": {
                        "lockin_xx": asdict(result.before_xx),
                        "lockin_xy": asdict(result.before_xy),
                    },
                    "after": {
                        "lockin_xx": asdict(result.after_xx),
                        "lockin_xy": asdict(result.after_xy),
                    },
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    return 0


def _run_harmonics(args: argparse.Namespace, factory: Callable[[], object]) -> int:
    settings = _resolve_pair_settings(args)
    _validate_distinct_addresses(settings["xx_address"], settings["xy_address"])
    if not args.authorize_writes:
        raise AuthorizationRequired("SR830 harmonic-setting writes were not authorized.")
    if not args.confirm_xy_sine_disconnected:
        raise AuthorizationRequired(
            "Physical disconnection of lockin_xy SINE OUT was not confirmed."
        )
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        controller = DualSr830Controller(lockin_xx, lockin_xy)
        preflight_xx, preflight_xy = controller.authorize_existing_configuration(
            frequency_hz=settings["frequency_hz"],
            authorize_writes=args.authorize_writes,
            confirm_xy_sine_disconnected=args.confirm_xy_sine_disconnected,
        )
        try:
            measurement = controller.measure_harmonics(
                settle_s=args.settle_s,
                sleeper=time.sleep,
            )
        except Sr830AcquisitionError as exc:
            print(
                json.dumps(
                    {
                        "completed": False,
                        "captured_unix_s": time.time(),
                        "error": str(exc),
                        "preflight": {
                            "lockin_xx": asdict(preflight_xx),
                            "lockin_xy": asdict(preflight_xy),
                        },
                        "partial_readings": [
                            _harmonic_sample_record(sample)
                            for sample in exc.partial_samples
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            raise
        print(
            json.dumps(
                {
                    "completed": True,
                    "captured_unix_s": time.time(),
                    "status_latches_consumed": True,
                    "preflight": {
                        "lockin_xx": asdict(preflight_xx),
                        "lockin_xy": asdict(preflight_xy),
                    },
                    "readings": [
                        _harmonic_sample_record(sample)
                        for sample in measurement.samples
                    ],
                    "pair_reads_are_sequential": measurement.pair_reads_are_sequential,
                    "restored_harmonic": 1,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    return 0


def _harmonic_sample_record(sample: Sr830HarmonicSample) -> dict[str, object]:
    record = asdict(sample.reading)
    record["captured_at_utc"] = sample.captured_at_utc.isoformat()
    return record


def _audited_harmonic_sample_record(
    sample: Sr830HarmonicSample,
) -> dict[str, object]:
    record = asdict(sample)
    record["captured_at_utc"] = sample.captured_at_utc.isoformat()
    return record


def _run_frequency_sweep(
    args: argparse.Namespace, factory: Callable[[], object]
) -> int:
    settings = _resolve_pair_settings(args)
    _validate_distinct_addresses(settings["xx_address"], settings["xy_address"])
    _require_sweep_authorization(args)
    points = _validate_increasing_points(args.points_hz, "frequency")
    if points[0] < 0.001 or points[-1] > 102_000.0:
        raise ValueError("Frequency sweep points must be within 0.001-102000 Hz.")
    baseline_hz = float(settings["frequency_hz"])
    records: list[dict[str, object]] = []
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        controller = DualSr830Controller(lockin_xx, lockin_xy)
        preflight_xx, preflight_xy = controller.authorize_existing_configuration(
            frequency_hz=baseline_hz,
            authorize_writes=True,
            confirm_xy_sine_disconnected=True,
        )
        writes_started = False
        failure: BaseException | None = None
        try:
            writes_started = True
            lockin_xx.set_sensitivity(args.xx_sensitivity_code)
            if lockin_xx.read_sensitivity() != args.xx_sensitivity_code:
                raise Sr830Error(
                    "lockin_xx sensitivity readback does not match the sweep setting."
                )
            for point_index, target_hz in enumerate(points):
                wrote_setting = not math.isclose(
                    target_hz, baseline_hz, rel_tol=0.0, abs_tol=1e-12
                )
                point_record: dict[str, object] = {
                    "point_index": point_index,
                    "target_frequency_hz": target_hz,
                    "source_v_rms": 0.004,
                    "nominal_current_a_rms": None,
                    "write_performed": wrote_setting,
                    "transition_status": None,
                    "frequency_readback_hz": None,
                    "samples": [],
                }
                records.append(point_record)
                if wrote_setting:
                    lockin_xx.set_internal_reference_frequency(target_hz)
                    time.sleep(args.settle_s)
                    transition, transition_problems = _consume_frequency_transition(
                        lockin_xx, lockin_xy
                    )
                    point_record["transition_status"] = transition
                    if transition_problems:
                        raise Sr830Error(
                            "Unsafe frequency transition: "
                            + "; ".join(transition_problems)
                        )
                time.sleep(args.settle_s)
                xx_readback = lockin_xx.read_reference_frequency()
                xy_readback = lockin_xy.read_reference_frequency()
                point_record["frequency_readback_hz"] = {
                    "lockin_xx": xx_readback,
                    "lockin_xy": xy_readback,
                }
                _verify_frequency_readbacks(
                    target_hz,
                    xx_readback,
                    xy_readback,
                    rel_tolerance=SWEEP_FREQUENCY_REL_TOLERANCE,
                )
                _capture_sweep_point(
                    lockin_xx,
                    lockin_xy,
                    target_frequency_hz=target_hz,
                    samples=args.samples_per_point,
                    sample_interval_s=args.sample_interval_s,
                    record=point_record,
                    frequency_rel_tolerance=SWEEP_FREQUENCY_REL_TOLERANCE,
                )
        except BaseException as exc:
            failure = exc
        cleanup = _restore_scan_state(
            lockin_xx,
            lockin_xy,
            baseline_hz=baseline_hz,
            original_xx_sensitivity=preflight_xx.sensitivity,
            restore_sensitivity=True,
            restore_frequency=True,
            settle_s=args.settle_s,
            writes_started=writes_started,
        )
        result = {
            "scan": "frequency",
            "completed": failure is None and cleanup["verified"],
            "captured_unix_s": time.time(),
            "status_latches_consumed": True,
            "preflight": {
                "lockin_xx": asdict(preflight_xx),
                "lockin_xy": asdict(preflight_xy),
            },
            "requested_points_hz": points,
            "temporary_xx_sensitivity_code": args.xx_sensitivity_code,
            "settle_s": args.settle_s,
            "samples_per_point": args.samples_per_point,
            "sample_interval_s": args.sample_interval_s,
            "points": records,
            "cleanup": cleanup,
            "error": None if failure is None else str(failure),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
        if failure is not None:
            raise failure
        if not cleanup["verified"]:
            raise Sr830Error("Frequency sweep cleanup could not be verified.")
    return 0


def _run_excitation_sweep(
    args: argparse.Namespace, factory: Callable[[], object]
) -> int:
    settings = _resolve_pair_settings(args)
    _validate_distinct_addresses(settings["xx_address"], settings["xy_address"])
    _require_sweep_authorization(args)
    if not args.confirm_no_50ohm_termination:
        raise AuthorizationRequired("Absence of an external 50 ohm termination was not confirmed.")
    points = _validate_increasing_points(args.points_v, "excitation")
    safety = _validate_excitation_safety(args, points)
    baseline_hz = float(settings["frequency_hz"])
    records: list[dict[str, object]] = []
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        controller = DualSr830Controller(lockin_xx, lockin_xy)
        preflight_xx, preflight_xy = controller.authorize_existing_configuration(
            frequency_hz=baseline_hz,
            authorize_writes=True,
            confirm_xy_sine_disconnected=True,
        )
        writes_started = False
        failure: BaseException | None = None
        try:
            writes_started = True
            lockin_xx.set_sensitivity(args.xx_sensitivity_code)
            if lockin_xx.read_sensitivity() != args.xx_sensitivity_code:
                raise Sr830Error("lockin_xx sensitivity readback does not match the sweep setting.")
            for point_index, source_v in enumerate(points):
                wrote_setting = not math.isclose(
                    source_v, 0.004, rel_tol=0.0, abs_tol=1e-12
                )
                nominal_current_a = source_v / safety["nominal_total_resistance_ohm"]
                point_record = {
                    "point_index": point_index,
                    "target_frequency_hz": baseline_hz,
                    "source_v_rms": source_v,
                    "source_readback_v_rms": None,
                    "nominal_current_a_rms": nominal_current_a,
                    "write_performed": wrote_setting,
                    "samples": [],
                }
                records.append(point_record)
                if wrote_setting:
                    lockin_xx.set_sine_output(source_v)
                time.sleep(args.settle_s)
                output_readback = lockin_xx.read_sine_output()
                point_record["source_readback_v_rms"] = output_readback
                if not math.isclose(output_readback, source_v, rel_tol=1e-6, abs_tol=0.001):
                    raise Sr830Error(
                        f"lockin_xx SINE OUT readback {output_readback:g} V does not "
                        f"match requested {source_v:g} V."
                    )
                _capture_sweep_point(
                    lockin_xx,
                    lockin_xy,
                    target_frequency_hz=baseline_hz,
                    samples=args.samples_per_point,
                    sample_interval_s=args.sample_interval_s,
                    record=point_record,
                    frequency_rel_tolerance=1e-5,
                )
        except BaseException as exc:
            failure = exc
        cleanup = _restore_scan_state(
            lockin_xx,
            lockin_xy,
            baseline_hz=baseline_hz,
            original_xx_sensitivity=preflight_xx.sensitivity,
            restore_sensitivity=True,
            restore_frequency=False,
            settle_s=args.settle_s,
            writes_started=writes_started,
        )
        result = {
            "scan": "excitation",
            "completed": failure is None and cleanup["verified"],
            "captured_unix_s": time.time(),
            "status_latches_consumed": True,
            "preflight": {
                "lockin_xx": asdict(preflight_xx),
                "lockin_xy": asdict(preflight_xy),
            },
            "requested_points_v_rms": points,
            "temporary_xx_sensitivity_code": args.xx_sensitivity_code,
            "settle_s": args.settle_s,
            "samples_per_point": args.samples_per_point,
            "sample_interval_s": args.sample_interval_s,
            "safety": safety,
            "points": records,
            "cleanup": cleanup,
            "error": None if failure is None else str(failure),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
        if failure is not None:
            raise failure
        if not cleanup["verified"]:
            raise Sr830Error("Excitation sweep cleanup could not be verified.")
    return 0


def _require_sweep_authorization(args: argparse.Namespace) -> None:
    if not args.authorize_writes:
        raise AuthorizationRequired("SR830 sweep setting writes were not authorized.")
    if not args.confirm_xy_sine_disconnected:
        raise AuthorizationRequired(
            "Physical disconnection of lockin_xy SINE OUT was not confirmed."
        )


def _validate_increasing_points(
    values: Sequence[float], name: str
) -> tuple[float, ...]:
    points = tuple(float(value) for value in values)
    if not points:
        raise ValueError(f"At least one {name} point is required.")
    if any(not math.isfinite(value) or value <= 0 for value in points):
        raise ValueError(f"Every {name} point must be finite and positive.")
    if any(current <= previous for previous, current in zip(points, points[1:])):
        raise ValueError(f"{name.capitalize()} points must be strictly increasing.")
    return points


def _validate_excitation_safety(
    args: argparse.Namespace, points: tuple[float, ...]
) -> dict[str, float]:
    maximum_source_v = max(points)
    if min(points) < 0.004 or maximum_source_v > 5.0:
        raise ValueError("Source-voltage points must be within the SR830 0.004-5 V RMS range.")
    current_bound_a = maximum_source_v / (
        args.series_resistance_ohm + SR830_OUTPUT_RESISTANCE_OHM
    )
    voltage_bound_v = maximum_source_v
    if current_bound_a > args.max_device_current_a:
        raise ValueError(
            "Worst-case current bound exceeds the confirmed device RMS current limit."
        )
    if voltage_bound_v > args.max_device_voltage_v:
        raise ValueError(
            "Worst-case device voltage bound exceeds the confirmed device RMS voltage limit."
        )
    nominal_total = (
        args.series_resistance_ohm
        + SR830_OUTPUT_RESISTANCE_OHM
        + args.device_resistance_ohm
    )
    nominal_current_a = maximum_source_v / nominal_total
    return {
        "series_resistance_ohm": args.series_resistance_ohm,
        "sr830_output_resistance_ohm": SR830_OUTPUT_RESISTANCE_OHM,
        "approximate_device_resistance_ohm": args.device_resistance_ohm,
        "nominal_total_resistance_ohm": nominal_total,
        "confirmed_max_device_current_a_rms": args.max_device_current_a,
        "confirmed_max_device_voltage_v_rms": args.max_device_voltage_v,
        "maximum_source_v_rms": maximum_source_v,
        "worst_case_current_bound_a_rms": current_bound_a,
        "worst_case_device_voltage_bound_v_rms": voltage_bound_v,
        "nominal_maximum_current_a_rms": nominal_current_a,
        "nominal_maximum_device_voltage_v_rms": (
            nominal_current_a * args.device_resistance_ohm
        ),
    }


def _verify_frequency_readbacks(
    target_hz: float,
    xx_readback_hz: float,
    xy_readback_hz: float,
    *,
    rel_tolerance: float = 1e-5,
) -> None:
    for role, readback in (("xx", xx_readback_hz), ("xy", xy_readback_hz)):
        if not math.isclose(
            readback,
            target_hz,
            rel_tol=rel_tolerance,
            abs_tol=PAIR_FREQUENCY_ABS_TOLERANCE_HZ,
        ):
            raise Sr830Error(
                f"lockin_{role} frequency readback {readback:g} Hz does not match "
                f"requested {target_hz:g} Hz."
            )
    if not math.isclose(
        xx_readback_hz,
        xy_readback_hz,
        rel_tol=rel_tolerance,
        abs_tol=PAIR_FREQUENCY_ABS_TOLERANCE_HZ,
    ):
        raise Sr830Error("xx/xy frequency readbacks do not match.")


def _capture_sweep_point(
    lockin_xx: Sr830,
    lockin_xy: Sr830,
    *,
    target_frequency_hz: float,
    samples: int,
    sample_interval_s: float,
    record: dict[str, object],
    frequency_rel_tolerance: float,
) -> None:
    raw_samples = record["samples"]
    if not isinstance(raw_samples, list):
        raise TypeError("Sweep point samples must be a list.")
    for sample_index in range(samples):
        if sample_index:
            time.sleep(sample_interval_s)
        xx = lockin_xx.read_harmonic_sample(1)
        xy = lockin_xy.read_harmonic_sample(1)
        problems: list[str] = []
        for sample in (xx, xy):
            role = sample.reading.role.value
            if sample.lia_status.reference_unlocked:
                problems.append(f"lockin_{role} reference is unlocked")
            if sample.lia_status.any_overload:
                problems.append(f"lockin_{role} reports overload")
            if sample.error_status:
                problems.append(
                    f"lockin_{role} instrument error is {sample.error_status}"
                )
        try:
            _verify_frequency_readbacks(
                target_frequency_hz,
                xx.reading.frequency_hz,
                xy.reading.frequency_hz,
                rel_tolerance=frequency_rel_tolerance,
            )
        except Sr830Error as exc:
            problems.append(str(exc))
        sample_payload = {
            "sample_index": sample_index,
            "captured_unix_s": time.time(),
            "lockin_xx": _audited_harmonic_sample_record(xx),
            "lockin_xy": _audited_harmonic_sample_record(xy),
            "problems": problems,
        }
        raw_samples.append(sample_payload)
        if problems:
            raise Sr830Error("Sweep sample rejected: " + "; ".join(problems))


def _consume_frequency_transition(
    lockin_xx: Sr830, lockin_xy: Sr830
) -> tuple[dict[str, object], list[str]]:
    """Record and clear status from a deliberate FREQ transition.

    Transient unlock, frequency-range-change, or overload latches can occur while
    the TTL reference and internal filters move. They are discarded, not accepted
    as data. The formal window begins only after they are recorded, consumed, and
    followed by another settling interval; any repeated latch then fails normally.
    """

    xx = lockin_xx.read_harmonic_sample(1)
    xy = lockin_xy.read_harmonic_sample(1)
    problems: list[str] = []
    if xx.lia_status.reference_unlocked:
        problems.append("lockin_xx internal reference unlocked during transition")
    for sample in (xx, xy):
        role = sample.reading.role.value
        if sample.lia_status.time_constant_changed:
            problems.append(f"lockin_{role} time constant changed unexpectedly")
        if sample.error_status:
            problems.append(
                f"lockin_{role} instrument error during transition is "
                f"{sample.error_status}"
            )
    return (
        {
            "captured_unix_s": time.time(),
            "expected_transient_latches": [
                "lockin_xy.reference_unlocked",
                "frequency_range_changed",
                "input_or_reserve_overload",
                "filter_overload",
                "output_overload",
            ],
            "lockin_xx": _audited_harmonic_sample_record(xx),
            "lockin_xy": _audited_harmonic_sample_record(xy),
            "problems": problems,
        },
        problems,
    )


def _consume_sensitivity_transition(
    lockin_xx: Sr830,
    lockin_xy: Sr830,
) -> tuple[dict[str, object], list[str]]:
    """Record and clear overload latches after restoring the narrow XX range.

    A range change can leave a transient overload latch even after the 4 mVrms
    baseline is restored. The latch is discarded, not accepted as final status;
    a second settling interval precedes the strict cleanup readback.
    """

    xx = lockin_xx.read_harmonic_sample(1)
    xy = lockin_xy.read_harmonic_sample(1)
    problems: list[str] = []
    for sample in (xx, xy):
        role = sample.reading.role.value
        if role == "xy" and sample.lia_status.any_overload:
            problems.append("lockin_xy overloaded during XX sensitivity restoration")
        if sample.lia_status.reference_unlocked:
            problems.append(
                f"lockin_{role} reference unlocked during sensitivity restoration"
            )
        if sample.lia_status.frequency_range_changed:
            problems.append(f"lockin_{role} frequency range changed unexpectedly")
        if sample.lia_status.time_constant_changed:
            problems.append(f"lockin_{role} time constant changed unexpectedly")
        if sample.error_status:
            problems.append(
                f"lockin_{role} instrument error status {sample.error_status}"
            )
    return (
        {
            "captured_unix_s": time.time(),
            "expected_transient_latches": [
                "lockin_xx.input_or_reserve_overload",
                "lockin_xx.filter_overload",
                "lockin_xx.output_overload",
            ],
            "lockin_xx": _audited_harmonic_sample_record(xx),
            "lockin_xy": _audited_harmonic_sample_record(xy),
            "problems": problems,
        },
        problems,
    )


def _restore_scan_state(
    lockin_xx: Sr830,
    lockin_xy: Sr830,
    *,
    baseline_hz: float,
    original_xx_sensitivity: int,
    restore_sensitivity: bool,
    restore_frequency: bool,
    settle_s: float,
    writes_started: bool,
) -> dict[str, object]:
    if not writes_started:
        return {"attempted": False, "verified": True, "errors": []}
    errors: list[str] = []
    diagnostics: dict[str, object] = {}
    actions: list[tuple[str, Callable[[], None]]] = [
        ("restore lockin_xx to 4 mVrms", lockin_xx.set_minimum_sine_output)
    ]
    if restore_frequency:
        actions.append(
            (
                f"restore lockin_xx to {baseline_hz:g} Hz",
                lambda: lockin_xx.set_internal_reference_frequency(baseline_hz),
            )
        )
    for label, action in actions:
        try:
            action()
        except BaseException as exc:
            errors.append(f"{label}: {exc}")
    transition: dict[str, object] | None = None
    time.sleep(settle_s)
    if restore_frequency:
        try:
            transition, transition_problems = _consume_frequency_transition(
                lockin_xx, lockin_xy
            )
            errors.extend(transition_problems)
        except BaseException as exc:
            errors.append(f"frequency-restoration transition readback: {exc}")
        time.sleep(settle_s)
    sensitivity_transition: dict[str, object] | None = None
    if restore_sensitivity:
        try:
            lockin_xx.set_sensitivity(original_xx_sensitivity)
        except BaseException as exc:
            errors.append(f"restore lockin_xx sensitivity: {exc}")
        time.sleep(settle_s)
        try:
            sensitivity_transition, transition_problems = (
                _consume_sensitivity_transition(lockin_xx, lockin_xy)
            )
            errors.extend(transition_problems)
        except BaseException as exc:
            errors.append(f"sensitivity-restoration transition readback: {exc}")
        time.sleep(settle_s)
    try:
        xx = lockin_xx.read_diagnostic(consume_status_latches=True)
        xy = lockin_xy.read_diagnostic(consume_status_latches=True)
        diagnostics = {"lockin_xx": asdict(xx), "lockin_xy": asdict(xy)}
        errors.extend(_diagnostic_problems(xx, xy))
        if not math.isclose(xx.sine_output_v, 0.004, rel_tol=0.0, abs_tol=0.001):
            errors.append("lockin_xx did not read back 4 mVrms")
        try:
            _verify_frequency_readbacks(
                baseline_hz,
                xx.frequency_hz,
                xy.frequency_hz,
                rel_tolerance=(
                    SWEEP_FREQUENCY_REL_TOLERANCE if restore_frequency else 1e-5
                ),
            )
            _verify_frequency_readbacks(
                baseline_hz,
                xx.snapshot_frequency_hz,
                xy.snapshot_frequency_hz,
                rel_tolerance=(
                    SWEEP_FREQUENCY_REL_TOLERANCE if restore_frequency else 1e-5
                ),
            )
        except Sr830Error as exc:
            errors.append(str(exc))
        if restore_sensitivity and xx.sensitivity != original_xx_sensitivity:
            errors.append("lockin_xx sensitivity did not restore")
    except BaseException as exc:
        errors.append(f"final readback: {exc}")
    return {
        "attempted": True,
        "verified": not errors,
        "errors": errors,
        "transition_status": transition,
        "sensitivity_transition_status": sensitivity_transition,
        "final": diagnostics,
    }


def _open_pair(settings: dict[str, object], factory: Callable[[], object]):
    stack = ExitStack()
    manager = factory()
    stack.callback(manager.close)
    try:
        xx_resource = _open_resource(
            manager, str(settings["xx_address"]), int(settings["timeout_ms"])
        )
        stack.callback(xx_resource.close)
        xy_resource = _open_resource(
            manager, str(settings["xy_address"]), int(settings["timeout_ms"])
        )
        stack.callback(xy_resource.close)
    except BaseException:
        stack.close()
        raise
    pair = (
        Sr830(xx_resource, LockinRole.XX),
        Sr830(xy_resource, LockinRole.XY),
    )
    return _PairContext(stack, pair)


def _resolve_pair_settings(args: argparse.Namespace) -> dict[str, object]:
    config = None
    if args.config is not None:
        config = load_config(args.config)
        if config.project.mode is not RunMode.HARDWARE:
            raise ValueError("The standalone SR830 tool requires a hardware config.")

    xx_address = args.xx_address or (
        None if config is None else config.lockin_xx.address
    )
    xy_address = args.xy_address or (
        None if config is None else config.lockin_xy.address
    )
    for name, address in (("lockin_xx", xx_address), ("lockin_xy", xy_address)):
        if address is None or "CHANGE_ME" in address:
            raise ValueError(
                f"{name} VISA address is required via --config or --{name[-2:]}-address."
            )

    timeout_ms = args.timeout_ms
    if timeout_ms is None:
        timeout_ms = 5000 if config is None else config.visa.timeout_ms

    frequency_hz = getattr(args, "frequency_hz", None)
    if frequency_hz is None:
        frequency_hz = 17.777 if config is None else config.lockin_xx.frequency_hz

    return {
        "xx_address": xx_address,
        "xy_address": xy_address,
        "timeout_ms": timeout_ms,
        "frequency_hz": frequency_hz,
    }


class _PairContext:
    def __init__(self, stack: ExitStack, pair: tuple[Sr830, Sr830]):
        self._stack = stack
        self._pair = pair

    def __enter__(self) -> tuple[Sr830, Sr830]:
        return self._pair

    def __exit__(self, *exc_info: object) -> bool | None:
        return self._stack.__exit__(*exc_info)


def _open_resource(manager: object, address: str, timeout_ms: int):
    resource = manager.open_resource(address)
    resource.timeout = timeout_ms
    resource.write_termination = "\n"
    resource.read_termination = "\n"
    return resource


def _diagnostic_problems(
    xx: Sr830Diagnostic, xy: Sr830Diagnostic
) -> list[str]:
    problems: list[str] = []
    if xx.identity == xy.identity:
        problems.append("both VISA addresses returned the same SR830 identity")
    if xx.reference_mode != 1:
        problems.append("lockin_xx reference is not internal")
    if xy.reference_mode != 0:
        problems.append("lockin_xy reference is not external")
    if xy.reference_slope != 1:
        problems.append("lockin_xy external reference is not TTL rising")
    if xx.harmonic != 1 or xy.harmonic != 1:
        problems.append("both lock-ins must use first harmonic for this test")
    if xy.sine_output_v > 0.005:
        problems.append("lockin_xy SINE OUT is above its minimum setting")
    if not math.isclose(
        xx.snapshot_frequency_hz,
        xy.snapshot_frequency_hz,
        rel_tol=1e-5,
        abs_tol=PAIR_FREQUENCY_ABS_TOLERANCE_HZ,
    ):
        problems.append("lock-in reference frequencies differ")
    for diagnostic in (xx, xy):
        if diagnostic.lia_status is not None:
            if diagnostic.lia_status.reference_unlocked:
                problems.append(f"lockin_{diagnostic.role.value} reference is unlocked")
            if diagnostic.lia_status.any_overload:
                problems.append(f"lockin_{diagnostic.role.value} reports overload")
        if diagnostic.error_status:
            problems.append(
                f"lockin_{diagnostic.role.value} error status is "
                f"{diagnostic.error_status}"
            )
    return problems


def _validate_distinct_addresses(xx_address: str, xy_address: str) -> None:
    if xx_address.strip() == xy_address.strip():
        raise ValueError("lockin_xx and lockin_xy VISA addresses must be distinct.")


def _load_resource_manager_factory() -> Callable[[], object]:
    try:
        import pyvisa
    except ImportError as exc:
        raise Sr830Error(
            "PyVISA is not installed. Install this project with the 'hardware' extra."
        ) from exc
    return pyvisa.ResourceManager


def _positive_integer(value: str) -> int:
    converted = int(value)
    if converted <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return converted


def _positive_float(value: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return converted


def _nonnegative_float(value: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return converted


def _positive_float_list(value: str) -> tuple[float, ...]:
    try:
        converted = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a comma-separated list of numbers"
        ) from exc
    if not converted or any(
        not math.isfinite(item) or item <= 0 for item in converted
    ):
        raise argparse.ArgumentTypeError(
            "all comma-separated values must be finite and positive"
        )
    return converted


def _sensitivity_code(value: str) -> int:
    converted = int(value)
    if not 0 <= converted <= 26:
        raise argparse.ArgumentTypeError("must be an SR830 sensitivity code from 0 to 26")
    return converted


if __name__ == "__main__":
    main()
