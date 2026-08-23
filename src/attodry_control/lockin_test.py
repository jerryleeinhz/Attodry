from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import tempfile
import time
from typing import Callable, Mapping, Sequence

from .config import ControlConfig, LockinConfig, RunMode, load_config
from .lockin_autorange import (
    AutorangeAction,
    AutorangePolicy,
    AutorangeState,
    decide_autorange,
)
from .lockin_live import LiveLockinPairSnapshot, format_live_lockin_snapshot
from .models import LockinRole
from .sr830 import (
    AuthorizationRequired,
    DualSr830Controller,
    MAXIMUM_SINE_OUTPUT_V,
    MAXIMUM_REFERENCE_FREQUENCY_HZ,
    MINIMUM_SINE_OUTPUT_V,
    PAIR_FREQUENCY_ABS_TOLERANCE_HZ,
    Sr830,
    Sr830AcquisitionError,
    Sr830Diagnostic,
    Sr830Error,
    Sr830HarmonicSample,
    configure_minimum_excitation_pair,
    verify_fixed_settings_readback,
    verify_pair_readback,
)
from .sr830_settings import (
    SensitivityMode,
    Sr830SettingCodes,
    map_sr830_settings,
    sensitivity_code,
    sensitivity_full_scale_v,
)


SR830_OUTPUT_RESISTANCE_OHM = 50.0
MINIMUM_SWEEP_SETTLE_S = 1.5
EXCITATION_SOURCE_STEP_SETTLE_INTERVALS = 2
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

    monitor_live = subparsers.add_parser(
        "monitor-live",
        help=(
            "Continuously display paired SR830 voltage, phase, frequency, and "
            "optional latch status without changing settings."
        ),
    )
    _add_pair_arguments(monitor_live)
    monitor_live.add_argument(
        "--samples",
        type=_nonnegative_integer,
        default=0,
        help="Number of refreshes; 0 (the default) refreshes until Ctrl+C.",
    )
    monitor_live.add_argument(
        "--interval-s",
        type=_nonnegative_float,
        default=1.0,
        help="Seconds between refreshes. Default: 1 second.",
    )
    monitor_live.add_argument(
        "--consume-status-latches",
        action="store_true",
        help=(
            "Query LIAS?/ERRS? for live lock/overload/error status. These "
            "queries clear latched status bits on the SR830."
        ),
    )
    monitor_live.set_defaults(
        config=Path("config/hardware.local.toml"), handler=_run_monitor_live
    )

    set_xx_sensitivity = subparsers.add_parser(
        "set-xx-sensitivity",
        help=(
            "Set only lockin_xx SENS to the strict TOML start range and verify it."
        ),
    )
    _add_pair_arguments(set_xx_sensitivity)
    set_xx_sensitivity.add_argument(
        "--settle-s",
        type=_positive_float,
        default=1.5,
        help="Wait after the SENS write and before formal verification. Default: 1.5 s.",
    )
    set_xx_sensitivity.add_argument(
        "--authorize-writes",
        action="store_true",
        help="Explicitly authorize only the documented lockin_xx SENS write and recovery writes.",
    )
    set_xx_sensitivity.add_argument(
        "--authorize-status-latch-consumption",
        action="store_true",
        help="Explicitly authorize LIAS?/ERRS? queries, which clear latched status bits.",
    )
    set_xx_sensitivity.add_argument(
        "--confirm-xy-sine-disconnected",
        action="store_true",
        help="Confirm lockin_xy SINE OUT is physically disconnected.",
    )
    set_xx_sensitivity.set_defaults(handler=_run_set_xx_sensitivity)

    commission_autorange_narrow = subparsers.add_parser(
        "commission-xx-autorange-narrow",
        help=(
            "Stage XX at the configured 20 mV maximum, then verify the real "
            "two-sample bounded-auto narrowing back to 10 mV."
        ),
    )
    _add_pair_arguments(commission_autorange_narrow)
    commission_autorange_narrow.add_argument(
        "--settle-s",
        type=_positive_float,
        default=1.5,
        help="Wait after each sensitivity transition and between fit samples. Default: 1.5 s.",
    )
    commission_autorange_narrow.add_argument(
        "--authorize-writes",
        action="store_true",
        help="Explicitly authorize only XX SENS 21 staging, SENS 20 narrowing, and recovery writes.",
    )
    commission_autorange_narrow.add_argument(
        "--authorize-status-latch-consumption",
        action="store_true",
        help="Explicitly authorize LIAS?/ERRS? queries, which clear latched status bits.",
    )
    commission_autorange_narrow.add_argument(
        "--confirm-xy-sine-disconnected",
        action="store_true",
        help="Confirm lockin_xy SINE OUT is physically disconnected.",
    )
    commission_autorange_narrow.set_defaults(handler=_run_commission_xx_autorange_narrow)

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
    _add_pair_arguments(frequency_sweep, hide_overrides=True)
    _add_sweep_arguments(frequency_sweep, hide_overrides=True)
    frequency_sweep.add_argument(
        "--points-hz",
        type=_positive_float_list,
        help=argparse.SUPPRESS,
    )
    unsupported_policy = frequency_sweep.add_mutually_exclusive_group()
    unsupported_policy.add_argument(
        "--skip-unsupported-harmonics",
        action="store_true",
        dest="skip_unsupported_harmonics",
        default=None,
        help=argparse.SUPPRESS,
    )
    unsupported_policy.add_argument(
        "--fail-on-unsupported-harmonics",
        action="store_false",
        dest="skip_unsupported_harmonics",
        help=argparse.SUPPRESS,
    )
    frequency_sweep.set_defaults(
        config=Path("config/hardware.local.toml"),
        handler=_run_frequency_sweep,
    )

    excitation_sweep = subparsers.add_parser(
        "sweep-excitation",
        help="Sweep lockin_xx SINE OUT and record nominal path current.",
    )
    _add_pair_arguments(excitation_sweep, hide_overrides=True)
    _add_sweep_arguments(excitation_sweep, hide_overrides=True)
    excitation_sweep.add_argument(
        "--points-v",
        type=_positive_float_list,
        help=argparse.SUPPRESS,
    )
    excitation_sweep.add_argument(
        "--series-resistance-ohm", type=_positive_float, help=argparse.SUPPRESS
    )
    excitation_sweep.add_argument(
        "--device-resistance-ohm", type=_nonnegative_float, help=argparse.SUPPRESS
    )
    excitation_sweep.add_argument(
        "--max-device-current-a", type=_positive_float, help=argparse.SUPPRESS
    )
    excitation_sweep.add_argument(
        "--max-device-voltage-v", type=_positive_float, help=argparse.SUPPRESS
    )
    excitation_sweep.set_defaults(
        config=Path("config/hardware.local.toml"),
        handler=_run_excitation_sweep,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except KeyboardInterrupt:
        if _requested_command(argv) == "monitor-live":
            print(
                "Live monitor stopped. It does not issue SR830 setting writes.",
                file=sys.stderr,
            )
            raise SystemExit(130) from None
        print(
            "Interrupted. If a sweep had started, its safe-state cleanup "
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


def _add_pair_arguments(
    parser: argparse.ArgumentParser, *, hide_overrides: bool = False
) -> None:
    override_help = argparse.SUPPRESS if hide_overrides else None
    parser.add_argument(
        "--config",
        type=Path,
        help="Hardware TOML supplying semantic SR830 addresses, timeout, and frequency.",
    )
    parser.add_argument(
        "--xx-address",
        help=(
            override_help
            or "Override lockin_xx.address from --config."
        ),
    )
    parser.add_argument(
        "--xy-address",
        help=(
            override_help
            or "Override lockin_xy.address from --config."
        ),
    )
    parser.add_argument(
        "--timeout-ms",
        type=_positive_integer,
        help=(
            override_help
            or "Override visa.timeout_ms from --config. Default without config: 5000."
        ),
    )


def _add_sweep_arguments(
    parser: argparse.ArgumentParser, *, hide_overrides: bool = False
) -> None:
    override_help = argparse.SUPPRESS if hide_overrides else None
    parser.add_argument(
        "--settle-s",
        type=_nonnegative_float,
        help=(
            override_help
            or "Transition-settle interval in seconds. Excitation source steps use "
            "two intervals. Overrides lockin_sweep.settle_s."
        ),
    )
    parser.add_argument(
        "--samples-per-point", type=_positive_integer, help=override_help
    )
    parser.add_argument(
        "--sample-interval-s", type=_nonnegative_float, help=override_help
    )
    harmonic_selection = parser.add_mutually_exclusive_group()
    harmonic_selection.add_argument(
        "--all-harmonics",
        action="store_true",
        dest="all_harmonics",
        default=None,
        help=(
            override_help
            or "At every point, acquire harmonics 1, 2, and 3 in order. "
            "Overrides the configured scan-specific harmonic list."
        ),
    )
    harmonic_selection.add_argument(
        "--first-harmonic-only",
        action="store_false",
        dest="all_harmonics",
        help=override_help or "Override the TOML and acquire only harmonic 1.",
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


def _run_monitor_live(
    args: argparse.Namespace, factory: Callable[[], object]
) -> int:
    """Render a read-only terminal panel; no SR830 setting writes are possible here."""

    if args.samples == 0 and args.interval_s <= 0:
        raise ValueError("Continuous live monitoring requires --interval-s greater than zero.")
    settings = _resolve_pair_settings(args)
    _validate_distinct_addresses(settings["xx_address"], settings["xy_address"])
    had_problem = False
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        index = 0
        while args.samples == 0 or index < args.samples:
            if index:
                time.sleep(args.interval_s)
            xx = lockin_xx.read_diagnostic(
                consume_status_latches=args.consume_status_latches
            )
            xy = lockin_xy.read_diagnostic(
                consume_status_latches=args.consume_status_latches
            )
            problems = tuple(_diagnostic_problems(xx, xy))
            had_problem = had_problem or bool(problems)
            snapshot = LiveLockinPairSnapshot(
                sample_index=index,
                captured_at_utc=datetime.now(timezone.utc),
                status_latches_consumed=args.consume_status_latches,
                lockin_xx=xx,
                lockin_xy=xy,
                problems=problems,
            )
            if index:
                print()
            print(format_live_lockin_snapshot(snapshot), flush=True)
            index += 1
    return 1 if had_problem else 0


def _run_set_xx_sensitivity(
    args: argparse.Namespace, factory: Callable[[], object]
) -> int:
    settings = _resolve_pair_settings(args)
    _validate_distinct_addresses(settings["xx_address"], settings["xy_address"])
    config = settings["config"]
    if config is None:
        raise ValueError("set-xx-sensitivity requires --config with the strict TOML policy.")
    if not args.authorize_writes:
        raise AuthorizationRequired("SR830 sensitivity writes were not explicitly authorized.")
    if not args.authorize_status_latch_consumption:
        raise AuthorizationRequired(
            "SR830 LIAS?/ERRS? latch consumption was not explicitly authorized."
        )
    if not args.confirm_xy_sine_disconnected:
        raise AuthorizationRequired(
            "Physical disconnection of lockin_xy SINE OUT was not confirmed."
        )
    if args.settle_s < 1.5:
        raise Sr830Error("XX sensitivity verification requires at least 1.5 s settling.")

    xx_settings = _setting_codes(config.lockin_xx)
    xy_settings = _setting_codes(config.lockin_xy)
    target_code = sensitivity_code(config.lockin_xx.sensitivity_full_scale_v)
    record: dict[str, object] = {
        "operation": "set_xx_sensitivity",
        "target_sensitivity_code": target_code,
        "target_full_scale_v": config.lockin_xx.sensitivity_full_scale_v,
        "settle_s": args.settle_s,
        "safety_status_complete": True,
    }
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        previous_code: int | None = None
        write_started = False
        try:
            before_xx = lockin_xx.read_diagnostic(consume_status_latches=True)
            before_xy = lockin_xy.read_diagnostic(consume_status_latches=True)
            record["before"] = {
                "lockin_xx": asdict(before_xx),
                "lockin_xy": asdict(before_xy),
            }
            problems = _diagnostic_problems(before_xx, before_xy)
            if problems:
                raise Sr830Error("XX sensitivity preflight failed: " + "; ".join(problems))
            verify_pair_readback(
                before_xx, before_xy, float(settings["frequency_hz"])
            )
            verify_fixed_settings_readback(
                before_xx,
                replace(xx_settings, sensitivity=before_xx.sensitivity),
                before_xx.phase_shift_deg,
            )
            verify_fixed_settings_readback(
                before_xy, xy_settings, before_xy.phase_shift_deg
            )
            previous_code = before_xx.sensitivity
            if previous_code not in (17, target_code):
                raise Sr830Error(
                    "lockin_xx sensitivity must be the known 1 mV baseline or the "
                    "configured 10 mV target before this commissioning command."
                )

            if previous_code == target_code:
                record["write_performed"] = False
                record["completed"] = True
                print(json.dumps(record, indent=2, ensure_ascii=False), flush=True)
                return 0

            write_started = True
            lockin_xx.set_sensitivity(target_code)
            time.sleep(args.settle_s)
            transition_xx = lockin_xx.read_diagnostic(consume_status_latches=True)
            transition_xy = lockin_xy.read_diagnostic(consume_status_latches=True)
            record["transition"] = {
                "lockin_xx": asdict(transition_xx),
                "lockin_xy": asdict(transition_xy),
            }
            problems = _diagnostic_problems(transition_xx, transition_xy)
            if problems:
                raise Sr830Error(
                    "XX sensitivity transition failed: " + "; ".join(problems)
                )
            verify_pair_readback(
                transition_xx, transition_xy, float(settings["frequency_hz"])
            )
            verify_fixed_settings_readback(
                transition_xx, xx_settings, before_xx.phase_shift_deg
            )
            verify_fixed_settings_readback(
                transition_xy, xy_settings, before_xy.phase_shift_deg
            )

            time.sleep(args.settle_s)
            after_xx = lockin_xx.read_diagnostic(consume_status_latches=True)
            after_xy = lockin_xy.read_diagnostic(consume_status_latches=True)
            record["after"] = {
                "lockin_xx": asdict(after_xx),
                "lockin_xy": asdict(after_xy),
            }
            problems = _diagnostic_problems(after_xx, after_xy)
            if problems:
                raise Sr830Error(
                    "XX sensitivity formal verification failed: " + "; ".join(problems)
                )
            verify_pair_readback(after_xx, after_xy, float(settings["frequency_hz"]))
            verify_fixed_settings_readback(after_xx, xx_settings, before_xx.phase_shift_deg)
            verify_fixed_settings_readback(after_xy, xy_settings, before_xy.phase_shift_deg)
        except BaseException as exc:
            record["completed"] = False
            record["error"] = str(exc)
            record["write_performed"] = write_started
            if write_started and previous_code is not None:
                record["cleanup"] = _restore_scan_state(
                    lockin_xx,
                    lockin_xy,
                    baseline_hz=float(settings["frequency_hz"]),
                    original_xx_sensitivity=previous_code,
                    restore_sensitivity=True,
                    restore_frequency=False,
                    settle_s=args.settle_s,
                    writes_started=True,
                )
            print(json.dumps(record, indent=2, ensure_ascii=False), flush=True)
            raise

    record["completed"] = True
    record["write_performed"] = True
    print(json.dumps(record, indent=2, ensure_ascii=False), flush=True)
    return 0


def _run_commission_xx_autorange_narrow(
    args: argparse.Namespace, factory: Callable[[], object]
) -> int:
    """Verify the real two-safe-sample XX narrowing branch without changing excitation."""

    settings = _resolve_pair_settings(args)
    _validate_distinct_addresses(settings["xx_address"], settings["xy_address"])
    config = settings["config"]
    if config is None:
        raise ValueError(
            "commission-xx-autorange-narrow requires --config with the strict TOML policy."
        )
    if not args.authorize_writes:
        raise AuthorizationRequired("SR830 autorange commissioning writes were not authorized.")
    if not args.authorize_status_latch_consumption:
        raise AuthorizationRequired(
            "SR830 LIAS?/ERRS? latch consumption was not explicitly authorized."
        )
    if not args.confirm_xy_sine_disconnected:
        raise AuthorizationRequired(
            "Physical disconnection of lockin_xy SINE OUT was not confirmed."
        )
    if args.settle_s < 1.5:
        raise Sr830Error("Autorange commissioning requires at least 1.5 s settling.")
    if config.lockin_xx.sensitivity_mode is not SensitivityMode.BOUNDED_AUTO:
        raise ValueError(
            "commission-xx-autorange-narrow requires "
            "lockin_xx.sensitivity_mode = 'bounded_auto'."
        )

    xx_settings = _setting_codes(config.lockin_xx)
    xy_settings = _setting_codes(config.lockin_xy)
    policy = _autorange_policy_for_lockin(config.lockin_xx)
    if policy is None:
        raise AssertionError("XX bounded_auto policy was not constructed.")
    minimum_code = sensitivity_code(policy.minimum_full_scale_v)
    maximum_code = sensitivity_code(policy.maximum_full_scale_v)
    if xx_settings.sensitivity != minimum_code:
        raise Sr830Error("XX fixed start range must equal the autorange minimum.")
    record: dict[str, object] = {
        "operation": "commission_xx_autorange_narrow",
        "minimum_sensitivity_code": minimum_code,
        "maximum_sensitivity_code": maximum_code,
        "maximum_range_staged_manually": True,
        "settle_s": args.settle_s,
        "safety_status_complete": True,
    }
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        write_started = False
        before_xx: Sr830Diagnostic | None = None
        try:
            before_xx, before_xy = _read_verified_autorange_pair(
                lockin_xx,
                lockin_xy,
                xx_settings=xx_settings,
                xy_settings=xy_settings,
                expected_xx_sensitivity=minimum_code,
                expected_xx_phase=None,
                expected_xy_phase=None,
                expected_frequency_hz=float(settings["frequency_hz"]),
                stage="autorange preflight",
            )
            record["before"] = {
                "lockin_xx": asdict(before_xx),
                "lockin_xy": asdict(before_xy),
            }

            write_started = True
            lockin_xx.set_sensitivity(maximum_code)
            time.sleep(args.settle_s)
            maximum_transition_xx, maximum_transition_xy = _read_verified_autorange_pair(
                lockin_xx,
                lockin_xy,
                xx_settings=xx_settings,
                xy_settings=xy_settings,
                expected_xx_sensitivity=maximum_code,
                expected_xx_phase=before_xx.phase_shift_deg,
                expected_xy_phase=before_xy.phase_shift_deg,
                expected_frequency_hz=float(settings["frequency_hz"]),
                stage="maximum-range transition",
            )
            record["maximum_range_transition"] = {
                "lockin_xx": asdict(maximum_transition_xx),
                "lockin_xy": asdict(maximum_transition_xy),
            }

            state = AutorangeState(policy.maximum_full_scale_v)
            decisions: list[dict[str, object]] = []
            for sample_index in range(2):
                time.sleep(args.settle_s)
                xx, xy = _read_verified_autorange_pair(
                    lockin_xx,
                    lockin_xy,
                    xx_settings=xx_settings,
                    xy_settings=xy_settings,
                    expected_xx_sensitivity=maximum_code,
                    expected_xx_phase=before_xx.phase_shift_deg,
                    expected_xy_phase=before_xy.phase_shift_deg,
                    expected_frequency_hz=float(settings["frequency_hz"]),
                    stage=f"maximum-range fit sample {sample_index + 1}",
                )
                decision = decide_autorange(
                    policy,
                    state,
                    amplitude_v=xx.amplitude_v,
                    overload=bool(xx.lia_status and xx.lia_status.any_overload),
                )
                decisions.append(
                    {
                        "sample_index": sample_index,
                        "lockin_xx": asdict(xx),
                        "lockin_xy": asdict(xy),
                        "decision": asdict(decision),
                    }
                )
                state = decision.state
                expected_action = (
                    AutorangeAction.KEEP if sample_index == 0 else AutorangeAction.NARROW
                )
                if decision.action is not expected_action:
                    raise Sr830Error(
                        "Autorange narrowing commissioning requires two consecutive "
                        "safe maximum-range samples."
                    )
            record["fit_samples"] = decisions

            lockin_xx.set_sensitivity(minimum_code)
            time.sleep(args.settle_s)
            narrowing_transition_xx, narrowing_transition_xy = _read_verified_autorange_pair(
                lockin_xx,
                lockin_xy,
                xx_settings=xx_settings,
                xy_settings=xy_settings,
                expected_xx_sensitivity=minimum_code,
                expected_xx_phase=before_xx.phase_shift_deg,
                expected_xy_phase=before_xy.phase_shift_deg,
                expected_frequency_hz=float(settings["frequency_hz"]),
                stage="automatic-narrowing transition",
                allow_xx_output_overload=True,
            )
            record["narrowing_transition"] = {
                "lockin_xx": asdict(narrowing_transition_xx),
                "lockin_xy": asdict(narrowing_transition_xy),
            }

            time.sleep(args.settle_s)
            after_xx, after_xy = _read_verified_autorange_pair(
                lockin_xx,
                lockin_xy,
                xx_settings=xx_settings,
                xy_settings=xy_settings,
                expected_xx_sensitivity=minimum_code,
                expected_xx_phase=before_xx.phase_shift_deg,
                expected_xy_phase=before_xy.phase_shift_deg,
                expected_frequency_hz=float(settings["frequency_hz"]),
                stage="automatic-narrowing formal verification",
            )
            record["after"] = {
                "lockin_xx": asdict(after_xx),
                "lockin_xy": asdict(after_xy),
            }
        except BaseException as exc:
            record["completed"] = False
            record["error"] = str(exc)
            record["write_performed"] = write_started
            if write_started and before_xx is not None:
                record["cleanup"] = _restore_scan_state(
                    lockin_xx,
                    lockin_xy,
                    baseline_hz=float(settings["frequency_hz"]),
                    original_xx_sensitivity=minimum_code,
                    restore_sensitivity=True,
                    restore_frequency=False,
                    settle_s=args.settle_s,
                    writes_started=True,
                )
            print(json.dumps(record, indent=2, ensure_ascii=False), flush=True)
            raise

    record["completed"] = True
    record["write_performed"] = True
    print(json.dumps(record, indent=2, ensure_ascii=False), flush=True)
    return 0


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
    _resolve_sweep_settings(args, settings, scan="frequency")
    config = settings["config"]
    if not isinstance(config, ControlConfig):
        raise ValueError("A validated hardware config is required for sweeps.")
    if args.settle_s < MINIMUM_SWEEP_SETTLE_S:
        raise Sr830Error(
            "Frequency sweep requires at least 1.5 s per settle interval for "
            "sensitivity and reference transitions."
        )
    record_directory = _prepare_sweep_record_directory(args, settings)
    points = _validate_increasing_points(args.points_hz, "frequency")
    if points[0] < 0.001 or points[-1] > MAXIMUM_REFERENCE_FREQUENCY_HZ:
        raise ValueError(
            "Frequency sweep points must be within "
            f"0.001-{MAXIMUM_REFERENCE_FREQUENCY_HZ:g} Hz."
        )
    harmonics_by_role = _requested_sweep_harmonics_by_role(args)
    harmonics = _requested_sweep_harmonics(args)
    if not args.skip_unsupported_harmonics:
        _validate_harmonic_detection_frequencies(points, harmonics)
    baseline_hz = float(settings["frequency_hz"])
    _sweep_baseline_source_voltage(settings)
    frequency_source_v = config.lockin_sweep.frequency_source_voltage_v_rms
    frequency_safety = _validate_frequency_source_safety(config, frequency_source_v)
    excitation_path = _resolved_excitation_path(settings)
    nominal_current_a_rms = frequency_source_v / float(
        excitation_path["nominal_total_resistance_ohm"]
    )
    source_step_settle_s = EXCITATION_SOURCE_STEP_SETTLE_INTERVALS * args.settle_s
    records: list[dict[str, object]] = []
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        controller = DualSr830Controller(lockin_xx, lockin_xy)
        preflight_xx = None
        preflight_xy = None
        sensitivity_setup: dict[str, object] | None = None
        writes_started = False
        failure: BaseException | None = None
        try:
            preflight_xx, preflight_xy = controller.verify_existing_configuration(
                frequency_hz=baseline_hz,
            )
            writes_started = True
            sensitivity_setup = _new_sweep_sensitivity_setup(
                lockin_xx_config=config.lockin_xx,
                lockin_xy_config=config.lockin_xy,
                original_xx_sensitivity=preflight_xx.sensitivity,
                original_xy_sensitivity=preflight_xy.sensitivity,
            )
            _configure_sweep_sensitivities(
                lockin_xx,
                lockin_xy,
                sensitivity_setup=sensitivity_setup,
                settle_s=args.settle_s,
            )
            source_write_performed = not math.isclose(
                preflight_xx.sine_output_v,
                frequency_source_v,
                rel_tol=1e-6,
                abs_tol=0.001,
            )
            if source_write_performed:
                lockin_xx.set_sine_output(frequency_source_v)
                time.sleep(source_step_settle_s)
            else:
                time.sleep(args.settle_s)
            source_readback_v = lockin_xx.read_sine_output()
            if not math.isclose(
                source_readback_v,
                frequency_source_v,
                rel_tol=1e-6,
                abs_tol=0.001,
            ):
                raise Sr830Error(
                    f"lockin_xx SINE OUT readback {source_readback_v:g} V does not "
                    f"match configured frequency sweep amplitude {frequency_source_v:g} V."
                )
            autorange_policies, autorange_states = _new_sweep_autorange_controls(
                config.lockin_xx,
                config.lockin_xy,
                sensitivity_setup=sensitivity_setup,
            )
            for point_index, target_hz in enumerate(points):
                wrote_setting = not math.isclose(
                    target_hz, baseline_hz, rel_tol=0.0, abs_tol=1e-12
                )
                point_record: dict[str, object] = {
                    "point_index": point_index,
                    "target_frequency_hz": target_hz,
                    "source_v_rms": frequency_source_v,
                    "source_readback_v_rms": source_readback_v,
                    "nominal_current_a_rms": nominal_current_a_rms,
                    "write_performed": wrote_setting,
                    "transition_status": None,
                    "frequency_readback_hz": None,
                    "skipped_harmonics": [],
                    "harmonic_transition_status": [],
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
                point_harmonics, skipped_harmonics = _harmonics_for_frequency(
                    target_hz,
                    harmonics,
                    skip_unsupported=args.skip_unsupported_harmonics,
                )
                point_record["skipped_harmonics"] = skipped_harmonics
                _apply_sweep_autorange(
                    lockin_xx,
                    lockin_xy,
                    sensitivity_setup=sensitivity_setup,
                    policies=autorange_policies,
                    states=autorange_states,
                    target_frequency_hz=target_hz,
                    frequency_rel_tolerance=SWEEP_FREQUENCY_REL_TOLERANCE,
                    settle_s=args.settle_s,
                    record=point_record,
                )
                _capture_sweep_point(
                    lockin_xx,
                    lockin_xy,
                    target_frequency_hz=target_hz,
                    harmonics=point_harmonics,
                    selected_roles_by_harmonic=_roles_by_harmonic(
                        point_harmonics, harmonics_by_role
                    ),
                    harmonic_settle_s=args.settle_s,
                    samples=args.samples_per_point,
                    sample_interval_s=args.sample_interval_s,
                    record=point_record,
                    frequency_rel_tolerance=SWEEP_FREQUENCY_REL_TOLERANCE,
                )
        except BaseException as exc:
            failure = exc
        cleanup = (
            _restore_scan_state(
                lockin_xx,
                lockin_xy,
                baseline_hz=baseline_hz,
                original_xx_sensitivity=preflight_xx.sensitivity,
                original_xy_sensitivity=preflight_xy.sensitivity,
                restore_sensitivity=_range_write_attempted(
                    sensitivity_setup, "lockin_xx"
                ),
                restore_xy_sensitivity=_range_write_attempted(
                    sensitivity_setup, "lockin_xy"
                ),
                restore_frequency=True,
                settle_s=args.settle_s,
                writes_started=writes_started,
            )
            if preflight_xx is not None
            else {"attempted": False, "verified": True, "errors": []}
        )
        result = {
            "scan": "frequency",
            "completed": failure is None and cleanup["verified"],
            "outcome": _sweep_outcome(failure, cleanup),
            "captured_unix_s": time.time(),
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "status_latches_consumed": True,
            "run_metadata": _sweep_run_metadata(settings),
            "measurement_config": _measurement_config_snapshot(
                settings,
                args,
                scan="frequency",
                excitation_path=excitation_path,
            ),
            "preflight": {
                "lockin_xx": None if preflight_xx is None else asdict(preflight_xx),
                "lockin_xy": None if preflight_xy is None else asdict(preflight_xy),
            },
            "requested_points_hz": points,
            "frequency_source_voltage_v_rms": frequency_source_v,
            "source_readback_v_rms": (
                source_readback_v if "source_readback_v" in locals() else None
            ),
            "source_step_settle_s": (
                source_step_settle_s
                if "source_write_performed" in locals() and source_write_performed
                else 0.0
            ),
            "requested_harmonics": harmonics,
            "requested_harmonics_by_role": harmonics_by_role,
            "skip_unsupported_harmonics": args.skip_unsupported_harmonics,
            "sensitivity_modes": {
                "lockin_xx": config.lockin_xx.sensitivity_mode.value,
                "lockin_xy": config.lockin_xy.sensitivity_mode.value,
            },
            "sensitivity_setup": sensitivity_setup,
            "settle_s": args.settle_s,
            "time_constant_settle_floor_s": args.time_constant_settle_floor_s,
            "samples_per_point": args.samples_per_point,
            "sample_interval_s": args.sample_interval_s,
            "safety": frequency_safety,
            "points": records,
            "cleanup": cleanup,
            "error": None if failure is None else str(failure),
        }
        _emit_sweep_result(record_directory, result)
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
    _resolve_sweep_settings(args, settings, scan="excitation")
    record_directory = _prepare_sweep_record_directory(args, settings)
    if args.settle_s < MINIMUM_SWEEP_SETTLE_S:
        raise Sr830Error(
            "Excitation sweep requires at least 1.5 s per settle interval; "
            "each SINE OUT step waits two intervals."
        )
    points = _validate_increasing_points(args.points_v, "excitation")
    safety = _validate_excitation_safety(args, points)
    harmonics_by_role = _requested_sweep_harmonics_by_role(args)
    harmonics = _requested_sweep_harmonics(args)
    baseline_hz = float(settings["frequency_hz"])
    baseline_source_v = _sweep_baseline_source_voltage(settings)
    source_step_settle_s = EXCITATION_SOURCE_STEP_SETTLE_INTERVALS * args.settle_s
    config = settings["config"]
    if not isinstance(config, ControlConfig):
        raise ValueError("A validated hardware config is required for sweeps.")
    records: list[dict[str, object]] = []
    with _open_pair(settings, factory) as (lockin_xx, lockin_xy):
        controller = DualSr830Controller(lockin_xx, lockin_xy)
        preflight_xx = None
        preflight_xy = None
        sensitivity_setup: dict[str, object] | None = None
        writes_started = False
        failure: BaseException | None = None
        try:
            preflight_xx, preflight_xy = controller.verify_existing_configuration(
                frequency_hz=baseline_hz,
            )
            writes_started = True
            sensitivity_setup = _new_sweep_sensitivity_setup(
                lockin_xx_config=config.lockin_xx,
                lockin_xy_config=config.lockin_xy,
                original_xx_sensitivity=preflight_xx.sensitivity,
                original_xy_sensitivity=preflight_xy.sensitivity,
            )
            _configure_sweep_sensitivities(
                lockin_xx,
                lockin_xy,
                sensitivity_setup=sensitivity_setup,
                settle_s=args.settle_s,
            )
            autorange_policies, autorange_states = _new_sweep_autorange_controls(
                config.lockin_xx,
                config.lockin_xy,
                sensitivity_setup=sensitivity_setup,
            )
            for point_index, source_v in enumerate(points):
                wrote_setting = not math.isclose(
                    source_v, baseline_source_v, rel_tol=0.0, abs_tol=1e-12
                )
                nominal_current_a = source_v / safety["nominal_total_resistance_ohm"]
                point_record = {
                    "point_index": point_index,
                    "target_frequency_hz": baseline_hz,
                    "source_v_rms": source_v,
                    "source_readback_v_rms": None,
                    "source_step_settle_s": (
                        source_step_settle_s if wrote_setting else 0.0
                    ),
                    "nominal_current_a_rms": nominal_current_a,
                    "write_performed": wrote_setting,
                    "harmonic_transition_status": [],
                    "samples": [],
                }
                records.append(point_record)
                if wrote_setting:
                    lockin_xx.set_sine_output(source_v)
                    time.sleep(source_step_settle_s)
                else:
                    time.sleep(args.settle_s)
                output_readback = lockin_xx.read_sine_output()
                point_record["source_readback_v_rms"] = output_readback
                if not math.isclose(output_readback, source_v, rel_tol=1e-6, abs_tol=0.001):
                    raise Sr830Error(
                        f"lockin_xx SINE OUT readback {output_readback:g} V does not "
                        f"match requested {source_v:g} V."
                    )
                _apply_sweep_autorange(
                    lockin_xx,
                    lockin_xy,
                    sensitivity_setup=sensitivity_setup,
                    policies=autorange_policies,
                    states=autorange_states,
                    target_frequency_hz=baseline_hz,
                    frequency_rel_tolerance=1e-5,
                    settle_s=args.settle_s,
                    record=point_record,
                )
                _capture_sweep_point(
                    lockin_xx,
                    lockin_xy,
                    target_frequency_hz=baseline_hz,
                    harmonics=harmonics,
                    selected_roles_by_harmonic=_roles_by_harmonic(
                        harmonics, harmonics_by_role
                    ),
                    harmonic_settle_s=args.settle_s,
                    samples=args.samples_per_point,
                    sample_interval_s=args.sample_interval_s,
                    record=point_record,
                    frequency_rel_tolerance=1e-5,
                )
        except BaseException as exc:
            failure = exc
        cleanup = (
            _restore_scan_state(
                lockin_xx,
                lockin_xy,
                baseline_hz=baseline_hz,
                original_xx_sensitivity=preflight_xx.sensitivity,
                original_xy_sensitivity=preflight_xy.sensitivity,
                restore_sensitivity=_range_write_attempted(
                    sensitivity_setup, "lockin_xx"
                ),
                restore_xy_sensitivity=_range_write_attempted(
                    sensitivity_setup, "lockin_xy"
                ),
                restore_frequency=False,
                settle_s=args.settle_s,
                writes_started=writes_started,
            )
            if preflight_xx is not None
            else {"attempted": False, "verified": True, "errors": []}
        )
        result = {
            "scan": "excitation",
            "completed": failure is None and cleanup["verified"],
            "outcome": _sweep_outcome(failure, cleanup),
            "captured_unix_s": time.time(),
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "status_latches_consumed": True,
            "run_metadata": _sweep_run_metadata(settings),
            "measurement_config": _measurement_config_snapshot(
                settings,
                args,
                scan="excitation",
                excitation_path={
                    **safety,
                    "external_50_ohm_termination": False,
                },
            ),
            "preflight": {
                "lockin_xx": None if preflight_xx is None else asdict(preflight_xx),
                "lockin_xy": None if preflight_xy is None else asdict(preflight_xy),
            },
            "requested_points_v_rms": points,
            "requested_harmonics": harmonics,
            "requested_harmonics_by_role": harmonics_by_role,
            "sensitivity_modes": {
                "lockin_xx": config.lockin_xx.sensitivity_mode.value,
                "lockin_xy": config.lockin_xy.sensitivity_mode.value,
            },
            "sensitivity_setup": sensitivity_setup,
            "settle_s": args.settle_s,
            "time_constant_settle_floor_s": args.time_constant_settle_floor_s,
            "source_step_settle_s": source_step_settle_s,
            "samples_per_point": args.samples_per_point,
            "sample_interval_s": args.sample_interval_s,
            "safety": safety,
            "points": records,
            "cleanup": cleanup,
            "error": None if failure is None else str(failure),
        }
        _emit_sweep_result(record_directory, result)
        if failure is not None:
            raise failure
        if not cleanup["verified"]:
            raise Sr830Error("Excitation sweep cleanup could not be verified.")
    return 0


def _resolved_excitation_path(settings: dict[str, object]) -> dict[str, float | bool]:
    config = settings["config"]
    if not isinstance(config, ControlConfig):
        raise ValueError("A validated hardware config is required for sweep records.")
    sweep = config.lockin_sweep
    nominal_total = (
        sweep.external_series_resistance_ohm
        + SR830_OUTPUT_RESISTANCE_OHM
        + sweep.approximate_device_resistance_ohm
    )
    return {
        "series_resistance_ohm": sweep.external_series_resistance_ohm,
        "sr830_output_resistance_ohm": SR830_OUTPUT_RESISTANCE_OHM,
        "approximate_device_resistance_ohm": (
            sweep.approximate_device_resistance_ohm
        ),
        "maximum_device_resistance_ohm": sweep.maximum_device_resistance_ohm,
        "nominal_total_resistance_ohm": nominal_total,
        "confirmed_max_device_current_a_rms": sweep.max_device_current_a_rms,
        "confirmed_max_device_voltage_v_rms": sweep.max_device_voltage_v_rms,
        "external_50_ohm_termination": sweep.external_50_ohm_termination,
    }


def _measurement_config_snapshot(
    settings: dict[str, object],
    args: argparse.Namespace,
    *,
    scan: str,
    excitation_path: dict[str, object],
) -> dict[str, object]:
    config = settings["config"]
    if not isinstance(config, ControlConfig):
        raise ValueError("A validated hardware config is required for sweep records.")

    def lockin_snapshot(lockin: LockinConfig) -> dict[str, object]:
        snapshot = asdict(lockin)
        snapshot.pop("address", None)
        return snapshot

    sweep_snapshot: dict[str, object] = {
        "points": (
            tuple(args.points_hz) if scan == "frequency" else tuple(args.points_v)
        ),
        "harmonics": _requested_sweep_harmonics(args),
        "harmonics_by_role": _requested_sweep_harmonics_by_role(args),
        "sensitivity_modes": {
            "lockin_xx": config.lockin_xx.sensitivity_mode.value,
            "lockin_xy": config.lockin_xy.sensitivity_mode.value,
        },
        "run_name": config.lockin_sweep.run_name,
        "note": config.lockin_sweep.note,
        "settle_s": args.settle_s,
        "time_constant_settle_floor_s": args.time_constant_settle_floor_s,
        "samples_per_point": args.samples_per_point,
        "sample_interval_s": args.sample_interval_s,
        "output_directory": config.lockin_sweep.output_directory.as_posix(),
    }
    if scan == "frequency":
        sweep_snapshot["frequency_source_voltage_v_rms"] = (
            config.lockin_sweep.frequency_source_voltage_v_rms
        )
        sweep_snapshot["source_step_settle_s"] = (
            EXCITATION_SOURCE_STEP_SETTLE_INTERVALS * args.settle_s
        )
        sweep_snapshot["skip_unsupported_harmonics"] = (
            args.skip_unsupported_harmonics
        )
    else:
        sweep_snapshot["source_step_settle_s"] = (
            EXCITATION_SOURCE_STEP_SETTLE_INTERVALS * args.settle_s
        )
    return {
        "schema_version": 4,
        "scan": scan,
        "source": "resolved_hardware_toml",
        "readback_location": "preflight and per-point records",
        "setting_writes_enabled_by_command": True,
        "lockin_xx": lockin_snapshot(config.lockin_xx),
        "lockin_xy": lockin_snapshot(config.lockin_xy),
        "sweep": sweep_snapshot,
        "excitation_path": excitation_path,
    }


def _sweep_run_metadata(settings: dict[str, object]) -> dict[str, str]:
    config = settings["config"]
    if not isinstance(config, ControlConfig):
        raise ValueError("A validated hardware config is required for sweep records.")
    return {"name": config.lockin_sweep.run_name, "note": config.lockin_sweep.note}


def _sweep_outcome(
    failure: BaseException | None, cleanup: dict[str, object]
) -> str:
    if isinstance(failure, KeyboardInterrupt):
        return "interrupted"
    if failure is None and cleanup["verified"]:
        return "completed"
    return "rejected"


def _emit_sweep_result(record_directory: Path, result: dict[str, object]) -> None:
    try:
        _save_sweep_result(record_directory, result)
    except OSError as exc:
        result["completed"] = False
        result["outcome"] = "rejected"
        result["recording_error"] = str(exc)
        if result.get("error") is None:
            result["error"] = f"audit record write failed: {exc}"
    print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    if "recording_error" in result:
        raise Sr830Error("Sweep audit record could not be saved.")


def _save_sweep_result(
    output_directory: Path, result: dict[str, object]
) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_metadata = result.get("run_metadata")
    if not isinstance(run_metadata, dict):
        raise ValueError("Sweep results must include run_metadata.")
    run_name = _sweep_filename_label(run_metadata.get("name"))
    destination = output_directory / (
        f"{timestamp}_{run_name}_{result['scan']}_{result['outcome']}.json"
    )
    serialized = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=output_directory,
            prefix=".lockin_sweep_",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary_path = Path(temporary.name)
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _sweep_filename_label(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Sweep run_metadata.name must be a non-empty trimmed string.")
    if len(value) > 80 or any(
        ord(character) < 32 or character in "\\/:*?\"<>|" for character in value
    ):
        raise ValueError(
            "Sweep run_metadata.name must be a safe filename label without path "
            "separators or Windows-reserved characters."
        )
    return value


def _prepare_sweep_record_directory(
    args: argparse.Namespace, settings: dict[str, object]
) -> Path:
    config = settings["config"]
    if not isinstance(config, ControlConfig) or args.config is None:
        raise ValueError("A hardware TOML is required to save sweep results.")
    config_path = Path(args.config).resolve()
    config_directory = config_path.parent
    allowed_root = (
        config_directory.parent
        if config_directory.name.casefold() == "config"
        else config_directory
    )
    output_directory = (
        config_directory / config.lockin_sweep.output_directory
    ).resolve()
    try:
        output_directory.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError(
            "lockin_sweep.output_directory must resolve within the station "
            "configuration directory (or its project root when it is config/)."
        ) from exc
    output_directory.mkdir(parents=True, exist_ok=True)
    return output_directory


def _sweep_baseline_source_voltage(settings: dict[str, object]) -> float:
    config = settings["config"]
    if not isinstance(config, ControlConfig):
        raise ValueError("A validated hardware config is required for sweeps.")
    for role, lockin in (("xx", config.lockin_xx), ("xy", config.lockin_xy)):
        if not math.isclose(
            lockin.source_voltage_v,
            MINIMUM_SINE_OUTPUT_V,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(
                "Sweep commands require lockin_"
                f"{role}.source_voltage_v = {MINIMUM_SINE_OUTPUT_V:g} V RMS."
            )
    return config.lockin_xx.source_voltage_v


def _new_sweep_sensitivity_setup(
    *,
    lockin_xx_config: LockinConfig,
    lockin_xy_config: LockinConfig,
    original_xx_sensitivity: int,
    original_xy_sensitivity: int,
) -> dict[str, object]:
    return {
        "ranges": {
            "lockin_xx": _new_sweep_range_record(
                lockin_xx_config, original_xx_sensitivity
            ),
            "lockin_xy": _new_sweep_range_record(
                lockin_xy_config, original_xy_sensitivity
            ),
        },
        "transition_status": None,
    }


def _new_sweep_range_record(
    lockin: LockinConfig, original_sensitivity: int
) -> dict[str, object]:
    policy = _autorange_policy_for_lockin(lockin)
    configured_code = sensitivity_code(lockin.sensitivity_full_scale_v)
    if policy is None:
        initial_target = configured_code
    else:
        try:
            original_full_scale_v = sensitivity_full_scale_v(original_sensitivity)
        except ValueError as exc:
            raise Sr830Error(
                f"lockin_{lockin.role.value} preflight sensitivity "
                f"{original_sensitivity} is outside the project-confirmed ranges; "
                "autorange will not change it."
            ) from exc
        if original_full_scale_v > policy.maximum_full_scale_v:
            raise Sr830Error(
                f"lockin_{lockin.role.value} preflight sensitivity "
                f"{original_full_scale_v:g} V exceeds its autorange maximum "
                f"{policy.maximum_full_scale_v:g} V; autorange will not narrow it "
                "before a safe probe."
            )
        initial_target = (
            original_sensitivity
            if original_full_scale_v >= policy.minimum_full_scale_v
            else sensitivity_code(policy.minimum_full_scale_v)
        )
    policy_record = None
    if policy is not None:
        policy_record = asdict(policy)
        policy_record["full_scales_v"] = policy.full_scales_v
    return {
        "mode": lockin.sensitivity_mode.value,
        "configured_fixed_sensitivity_code": configured_code,
        "configured_fixed_full_scale_v": lockin.sensitivity_full_scale_v,
        "autorange_policy": policy_record,
        "original_sensitivity_code": original_sensitivity,
        "initial_target_sensitivity_code": initial_target,
        "initial_target_full_scale_v": sensitivity_full_scale_v(initial_target),
        "current_sensitivity_code": initial_target,
        "write_attempted": False,
        "initial_setup_write_attempted": False,
        "autorange_write_attempted": False,
        "readback_sensitivity_code": None,
        "verification_sensitivity_code": None,
        "autorange_transitions": [],
    }


def _autorange_policy_for_lockin(lockin: LockinConfig) -> AutorangePolicy | None:
    if lockin.sensitivity_mode is SensitivityMode.FIXED:
        return None
    values = (
        lockin.autorange_min_full_scale_v,
        lockin.autorange_max_full_scale_v,
        lockin.autorange_target_occupancy,
        lockin.autorange_stable_samples,
        lockin.autorange_max_steps,
    )
    if any(value is None for value in values):
        raise ValueError(
            f"lockin_{lockin.role.value} bounded_auto configuration is incomplete."
        )
    minimum, maximum, occupancy, stable_samples, maximum_steps = values
    return AutorangePolicy(
        float(minimum),
        float(maximum),
        float(occupancy),
        int(stable_samples),
        int(maximum_steps),
    )


def _configure_sweep_sensitivities(
    lockin_xx: Sr830,
    lockin_xy: Sr830,
    *,
    sensitivity_setup: dict[str, object],
    settle_s: float,
) -> None:
    """Ensure both sweep ranges, retaining every range-write and status transition."""

    ranges = sensitivity_setup.get("ranges")
    if not isinstance(ranges, dict):
        raise ValueError("Sweep sensitivity setup must contain per-role range records.")
    instruments = (("lockin_xx", lockin_xx), ("lockin_xy", lockin_xy))
    for role, instrument in instruments:
        range_record = ranges.get(role)
        if not isinstance(range_record, dict):
            raise ValueError(f"Sweep sensitivity setup is missing {role}.")
        original = _sensitivity_record_int(
            range_record, "original_sensitivity_code", role
        )
        target = _sensitivity_record_int(
            range_record, "initial_target_sensitivity_code", role
        )
        if original != target:
            range_record["write_attempted"] = True
            range_record["initial_setup_write_attempted"] = True
            instrument.set_sensitivity(target)
    wrote_any_range = any(
        isinstance(range_record, dict) and range_record.get("write_attempted") is True
        for range_record in ranges.values()
    )
    if wrote_any_range:
        time.sleep(settle_s)
    for role, instrument in instruments:
        range_record = ranges.get(role)
        if not isinstance(range_record, dict):
            raise ValueError(f"Sweep sensitivity setup is missing {role}.")
        target = _sensitivity_record_int(
            range_record, "initial_target_sensitivity_code", role
        )
        readback = instrument.read_sensitivity()
        range_record["readback_sensitivity_code"] = readback
        if readback != target:
            raise Sr830Error(
                f"{role} sensitivity readback {readback} does not match the "
                f"configured sweep range {target}."
            )
        range_record["current_sensitivity_code"] = target
    if wrote_any_range:
        transition, problems = _consume_sensitivity_transition(
            lockin_xx,
            lockin_xy,
            allow_xx_output_overload=False,
        )
        sensitivity_setup["transition_status"] = transition
        if problems:
            raise Sr830Error(
                "Unsafe sweep sensitivity transition: " + "; ".join(problems)
            )
        time.sleep(settle_s)
        for role, instrument in instruments:
            range_record = ranges.get(role)
            if not isinstance(range_record, dict):
                raise ValueError(f"Sweep sensitivity setup is missing {role}.")
            target = _sensitivity_record_int(
                range_record, "initial_target_sensitivity_code", role
            )
            readback = instrument.read_sensitivity()
            range_record["verification_sensitivity_code"] = readback
            if readback != target:
                raise Sr830Error(
                    f"{role} sensitivity changed after the transition from {target} "
                    f"to {readback}."
                )


def _sensitivity_record_int(
    range_record: dict[str, object], key: str, role: str
) -> int:
    value = range_record.get(key)
    if not isinstance(value, int):
        raise ValueError(f"Sweep sensitivity setup has no integer {key} for {role}.")
    return value


def _new_sweep_autorange_controls(
    lockin_xx_config: LockinConfig,
    lockin_xy_config: LockinConfig,
    *,
    sensitivity_setup: dict[str, object],
) -> tuple[dict[str, AutorangePolicy], dict[str, AutorangeState]]:
    ranges = sensitivity_setup.get("ranges")
    if not isinstance(ranges, dict):
        raise ValueError("Sweep sensitivity setup must contain per-role range records.")
    policies: dict[str, AutorangePolicy] = {}
    states: dict[str, AutorangeState] = {}
    for role, lockin in (
        ("lockin_xx", lockin_xx_config),
        ("lockin_xy", lockin_xy_config),
    ):
        policy = _autorange_policy_for_lockin(lockin)
        if policy is None:
            continue
        range_record = ranges.get(role)
        if not isinstance(range_record, dict):
            raise ValueError(f"Sweep sensitivity setup is missing {role}.")
        current_code = _sensitivity_record_int(
            range_record, "current_sensitivity_code", role
        )
        current_full_scale_v = sensitivity_full_scale_v(current_code)
        if current_full_scale_v not in policy.full_scales_v:
            raise ValueError(
                f"{role} initial autorange sensitivity is outside its configured bounds."
            )
        policies[role] = policy
        states[role] = AutorangeState(current_full_scale_v)
    return policies, states


def _apply_sweep_autorange(
    lockin_xx: Sr830,
    lockin_xy: Sr830,
    *,
    sensitivity_setup: dict[str, object],
    policies: dict[str, AutorangePolicy],
    states: dict[str, AutorangeState],
    target_frequency_hz: float,
    frequency_rel_tolerance: float,
    settle_s: float,
    record: dict[str, object],
) -> None:
    """Run one h1 preprobe for explicitly enabled roles before formal samples."""

    if not policies:
        return
    automatic_roles = frozenset(policies)
    xx = lockin_xx.read_harmonic_sample(1)
    xy = lockin_xy.read_harmonic_sample(1)
    probe_problems = _autorange_probe_problems(
        xx,
        xy,
        target_frequency_hz=target_frequency_hz,
        frequency_rel_tolerance=frequency_rel_tolerance,
        allowed_output_overload_roles=automatic_roles,
    )
    autorange_record: dict[str, object] = {
        "enabled_roles": sorted(automatic_roles),
        "probe": {
            "captured_unix_s": time.time(),
            "lockin_xx": _audited_harmonic_sample_record(xx),
            "lockin_xy": _audited_harmonic_sample_record(xy),
            "problems": probe_problems,
            "decisions": {},
        },
        "transition_status": None,
        "verification": None,
    }
    record["autorange"] = autorange_record
    probe = autorange_record["probe"]
    if not isinstance(probe, dict):
        raise TypeError("Autorange probe record must be a mapping.")
    decisions = probe["decisions"]
    if not isinstance(decisions, dict):
        raise TypeError("Autorange decisions must be a mapping.")
    if probe_problems:
        raise Sr830Error("Autorange probe rejected: " + "; ".join(probe_problems))

    samples_by_role = {"lockin_xx": xx, "lockin_xy": xy}
    changes: dict[str, AutorangeDecision] = {}
    failures: list[str] = []
    for role, policy in policies.items():
        state = states[role]
        sample = samples_by_role[role]
        decision = decide_autorange(
            policy,
            state,
            amplitude_v=sample.reading.amplitude_v,
            overload=sample.lia_status.output_overload,
        )
        states[role] = decision.state
        decisions[role] = {
            "prior_state": asdict(state),
            "action": decision.action.value,
            "occupancy": decision.occupancy,
            "reason": decision.reason,
            "next_state": asdict(decision.state),
        }
        if decision.action is AutorangeAction.FAIL:
            failures.append(f"{role}: {decision.reason}")
        elif decision.action is not AutorangeAction.KEEP:
            changes[role] = decision
    if failures:
        raise Sr830Error("Autorange cannot remain within bounds: " + "; ".join(failures))
    if not changes:
        return

    ranges = sensitivity_setup.get("ranges")
    if not isinstance(ranges, dict):
        raise ValueError("Sweep sensitivity setup must contain per-role range records.")
    instruments = {"lockin_xx": lockin_xx, "lockin_xy": lockin_xy}
    for role, decision in changes.items():
        range_record = ranges.get(role)
        if not isinstance(range_record, dict):
            raise ValueError(f"Sweep sensitivity setup is missing {role}.")
        target_code = sensitivity_code(decision.state.current_full_scale_v)
        range_record["write_attempted"] = True
        range_record["autorange_write_attempted"] = True
        instruments[role].set_sensitivity(target_code)
        transitions = range_record.get("autorange_transitions")
        if not isinstance(transitions, list):
            raise ValueError(f"Sweep sensitivity setup has no transition list for {role}.")
        transitions.append(
            {
                "action": decision.action.value,
                "target_sensitivity_code": target_code,
                "target_full_scale_v": decision.state.current_full_scale_v,
                "occupancy": decision.occupancy,
                "reason": decision.reason,
            }
        )
    time.sleep(settle_s)
    for role, instrument in instruments.items():
        range_record = ranges.get(role)
        if not isinstance(range_record, dict):
            raise ValueError(f"Sweep sensitivity setup is missing {role}.")
        expected_code = _sensitivity_record_int(
            range_record, "current_sensitivity_code", role
        )
        if role in changes:
            expected_code = sensitivity_code(changes[role].state.current_full_scale_v)
        readback = instrument.read_sensitivity()
        if readback != expected_code:
            raise Sr830Error(
                f"{role} autorange readback {readback} does not match requested "
                f"range {expected_code}."
            )
        range_record["current_sensitivity_code"] = expected_code

    narrowing_roles = frozenset(
        role.removeprefix("lockin_")
        for role, decision in changes.items()
        if decision.action is AutorangeAction.NARROW
    )
    transition, transition_problems = _consume_sensitivity_transition(
        lockin_xx,
        lockin_xy,
        allow_xx_output_overload=False,
        allow_output_overload_roles=narrowing_roles,
    )
    transition["autorange_actions"] = {
        role: decision.action.value for role, decision in changes.items()
    }
    autorange_record["transition_status"] = transition
    if transition_problems:
        raise Sr830Error(
            "Unsafe autorange transition: " + "; ".join(transition_problems)
        )
    time.sleep(settle_s)
    verification_xx = lockin_xx.read_harmonic_sample(1)
    verification_xy = lockin_xy.read_harmonic_sample(1)
    verification_problems = _autorange_probe_problems(
        verification_xx,
        verification_xy,
        target_frequency_hz=target_frequency_hz,
        frequency_rel_tolerance=frequency_rel_tolerance,
        allowed_output_overload_roles=frozenset(),
    )
    autorange_record["verification"] = {
        "captured_unix_s": time.time(),
        "lockin_xx": _audited_harmonic_sample_record(verification_xx),
        "lockin_xy": _audited_harmonic_sample_record(verification_xy),
        "problems": verification_problems,
    }
    if verification_problems:
        raise Sr830Error(
            "Autorange transition verification rejected: "
            + "; ".join(verification_problems)
        )


def _autorange_probe_problems(
    xx: Sr830HarmonicSample,
    xy: Sr830HarmonicSample,
    *,
    target_frequency_hz: float,
    frequency_rel_tolerance: float,
    allowed_output_overload_roles: frozenset[str],
) -> list[str]:
    problems: list[str] = []
    for sample in (xx, xy):
        role = f"lockin_{sample.reading.role.value}"
        if sample.lia_status.reference_unlocked:
            problems.append(f"{role} reference is unlocked")
        if sample.lia_status.input_or_reserve_overload:
            problems.append(f"{role} input/reserve overload")
        if sample.lia_status.filter_overload:
            problems.append(f"{role} filter overload")
        if sample.lia_status.output_overload and not (
            role in allowed_output_overload_roles and sample.lia_status.raw == 4
        ):
            problems.append(f"{role} output overload")
        if sample.lia_status.frequency_range_changed:
            problems.append(f"{role} frequency range changed unexpectedly")
        if sample.lia_status.time_constant_changed:
            problems.append(f"{role} time constant changed unexpectedly")
        if sample.error_status:
            problems.append(f"{role} instrument error is {sample.error_status}")
    try:
        _verify_frequency_readbacks(
            target_frequency_hz,
            xx.reading.frequency_hz,
            xy.reading.frequency_hz,
            rel_tolerance=frequency_rel_tolerance,
        )
    except Sr830Error as exc:
        problems.append(str(exc))
    return problems


def _range_write_attempted(
    sensitivity_setup: dict[str, object] | None, role: str
) -> bool:
    if sensitivity_setup is None:
        return False
    ranges = sensitivity_setup.get("ranges")
    if not isinstance(ranges, dict):
        return False
    range_record = ranges.get(role)
    return isinstance(range_record, dict) and range_record.get("write_attempted") is True


def _resolve_sweep_settings(
    args: argparse.Namespace, settings: dict[str, object], *, scan: str
) -> None:
    config = settings["config"]
    if not isinstance(config, ControlConfig):
        raise ValueError(
            "Sweep commands require --config so wiring and safety limits come from "
            "the validated hardware TOML."
        )
    sweep = config.lockin_sweep
    if scan == "frequency":
        args.configured_harmonics_by_role = {
            "xx": sweep.frequency_xx_harmonics,
            "xy": sweep.frequency_xy_harmonics,
        }
    elif scan == "excitation":
        args.configured_harmonics_by_role = {
            "xx": sweep.excitation_xx_harmonics,
            "xy": sweep.excitation_xy_harmonics,
        }
    else:
        raise ValueError(f"Unsupported sweep type: {scan}.")
    args.configured_harmonics = tuple(
        sorted(
            set(args.configured_harmonics_by_role["xx"])
            | set(args.configured_harmonics_by_role["xy"])
        )
    )
    requested_settle_s = sweep.settle_s if args.settle_s is None else args.settle_s
    time_constant_settle_floor_s = _time_constant_settle_floor_s(config)
    if requested_settle_s < time_constant_settle_floor_s:
        raise Sr830Error(
            "Sweep settle_s must be at least "
            f"{time_constant_settle_floor_s:g} s for the configured SR830 "
            "time constants and settle_time_constants."
        )
    args.settle_s = requested_settle_s
    args.time_constant_settle_floor_s = time_constant_settle_floor_s
    args.samples_per_point = (
        sweep.samples_per_point
        if args.samples_per_point is None
        else args.samples_per_point
    )
    args.sample_interval_s = (
        sweep.sample_interval_s
        if args.sample_interval_s is None
        else args.sample_interval_s
    )
    if scan == "frequency":
        args.points_hz = (
            sweep.frequency_points_hz if args.points_hz is None else args.points_hz
        )
        if args.skip_unsupported_harmonics is None:
            args.skip_unsupported_harmonics = sweep.skip_unsupported_harmonics
        return
    args.points_v = (
        sweep.excitation_points_v_rms if args.points_v is None else args.points_v
    )
    for argument_name, configured_value in (
        ("series_resistance_ohm", sweep.external_series_resistance_ohm),
        ("device_resistance_ohm", sweep.approximate_device_resistance_ohm),
        ("max_device_current_a", sweep.max_device_current_a_rms),
        ("max_device_voltage_v", sweep.max_device_voltage_v_rms),
    ):
        if getattr(args, argument_name) is None:
            setattr(args, argument_name, configured_value)
    args.maximum_device_resistance_ohm = sweep.maximum_device_resistance_ohm


def _time_constant_settle_floor_s(config: ControlConfig) -> float:
    """Return the minimum post-setting wait implied by both SR830 filters."""

    return max(
        MINIMUM_SWEEP_SETTLE_S,
        config.lockin_xx.time_constant_s * config.lockin_xx.settle_time_constants,
        config.lockin_xy.time_constant_s * config.lockin_xy.settle_time_constants,
    )


def _requested_sweep_harmonics(args: argparse.Namespace) -> tuple[int, ...]:
    selections = _requested_sweep_harmonics_by_role(args)
    return tuple(sorted(set(selections["xx"]) | set(selections["xy"])))


def _requested_sweep_harmonics_by_role(
    args: argparse.Namespace,
) -> dict[str, tuple[int, ...]]:
    configured = args.configured_harmonics_by_role
    if not isinstance(configured, dict):
        raise TypeError("Configured sweep harmonics must be keyed by role.")
    if args.all_harmonics is True:
        return {
            role: (1, 2, 3) if configured[role] else ()
            for role in ("xx", "xy")
        }
    if args.all_harmonics is False:
        return {
            role: (1,) if configured[role] else ()
            for role in ("xx", "xy")
        }
    return {role: tuple(configured[role]) for role in ("xx", "xy")}


def _roles_by_harmonic(
    harmonics: Sequence[int],
    harmonics_by_role: Mapping[str, Sequence[int]],
) -> dict[int, tuple[str, ...]]:
    return {
        harmonic: tuple(
            role for role in ("xx", "xy") if harmonic in harmonics_by_role[role]
        )
        for harmonic in harmonics
    }


def _validate_harmonic_detection_frequencies(
    points_hz: Sequence[float], harmonics: Sequence[int]
) -> None:
    """Reject harmonic/reference products the SR830 cannot represent before VISA I/O."""

    unsupported = [
        (frequency_hz, harmonic, frequency_hz * harmonic)
        for frequency_hz in points_hz
        for harmonic in harmonics
        if frequency_hz * harmonic > MAXIMUM_REFERENCE_FREQUENCY_HZ
    ]
    if not unsupported:
        return
    details = "; ".join(
        f"h{harmonic} at {frequency_hz:g} Hz requires {detection_hz:g} Hz"
        for frequency_hz, harmonic, detection_hz in unsupported
    )
    raise ValueError(
        "Requested harmonic detection frequency exceeds the SR830 "
        f"{MAXIMUM_REFERENCE_FREQUENCY_HZ:g} Hz limit: {details}."
    )


def _harmonics_for_frequency(
    frequency_hz: float,
    requested_harmonics: Sequence[int],
    *,
    skip_unsupported: bool,
) -> tuple[tuple[int, ...], list[dict[str, float | int | str]]]:
    supported: list[int] = []
    skipped: list[dict[str, float | int | str]] = []
    for harmonic in requested_harmonics:
        detection_hz = frequency_hz * harmonic
        if detection_hz <= MAXIMUM_REFERENCE_FREQUENCY_HZ:
            supported.append(harmonic)
            continue
        if not skip_unsupported:
            _validate_harmonic_detection_frequencies((frequency_hz,), (harmonic,))
        skipped.append(
            {
                "harmonic": harmonic,
                "required_detection_frequency_hz": detection_hz,
                "limit_hz": MAXIMUM_REFERENCE_FREQUENCY_HZ,
                "reason": "exceeds_sr830_reference_limit",
            }
        )
    if not supported:
        raise ValueError(
            f"No requested harmonic is supported at {frequency_hz:g} Hz."
        )
    return tuple(supported), skipped


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
    if min(points) < MINIMUM_SINE_OUTPUT_V or maximum_source_v > MAXIMUM_SINE_OUTPUT_V:
        raise ValueError("Source-voltage points must be within the SR830 0.004-5 V RMS range.")
    return _calculate_source_voltage_safety(
        maximum_source_v,
        series_resistance_ohm=args.series_resistance_ohm,
        device_resistance_ohm=args.device_resistance_ohm,
        maximum_device_resistance_ohm=args.maximum_device_resistance_ohm,
        max_device_current_a=args.max_device_current_a,
        max_device_voltage_v=args.max_device_voltage_v,
    )


def _validate_frequency_source_safety(
    config: ControlConfig, source_voltage_v: float
) -> dict[str, float]:
    sweep = config.lockin_sweep
    return _calculate_source_voltage_safety(
        source_voltage_v,
        series_resistance_ohm=sweep.external_series_resistance_ohm,
        device_resistance_ohm=sweep.approximate_device_resistance_ohm,
        maximum_device_resistance_ohm=sweep.maximum_device_resistance_ohm,
        max_device_current_a=sweep.max_device_current_a_rms,
        max_device_voltage_v=sweep.max_device_voltage_v_rms,
    )


def _calculate_source_voltage_safety(
    maximum_source_v: float,
    *,
    series_resistance_ohm: float,
    device_resistance_ohm: float,
    maximum_device_resistance_ohm: float,
    max_device_current_a: float,
    max_device_voltage_v: float,
) -> dict[str, float]:
    if (
        not math.isfinite(maximum_source_v)
        or maximum_source_v < MINIMUM_SINE_OUTPUT_V
        or maximum_source_v > MAXIMUM_SINE_OUTPUT_V
    ):
        raise ValueError("Source voltage must be within the SR830 0.004-5 V RMS range.")
    current_bound_a = maximum_source_v / (
        series_resistance_ohm + SR830_OUTPUT_RESISTANCE_OHM
    )
    voltage_bound_v = maximum_source_v * maximum_device_resistance_ohm / (
        series_resistance_ohm
        + SR830_OUTPUT_RESISTANCE_OHM
        + maximum_device_resistance_ohm
    )
    if current_bound_a > max_device_current_a:
        raise ValueError(
            "Worst-case current bound exceeds the confirmed device RMS current limit."
        )
    if voltage_bound_v > max_device_voltage_v:
        raise ValueError(
            "Worst-case device voltage bound exceeds the confirmed device RMS voltage limit."
        )
    nominal_total = (
        series_resistance_ohm
        + SR830_OUTPUT_RESISTANCE_OHM
        + device_resistance_ohm
    )
    nominal_current_a = maximum_source_v / nominal_total
    return {
        "series_resistance_ohm": series_resistance_ohm,
        "sr830_output_resistance_ohm": SR830_OUTPUT_RESISTANCE_OHM,
        "approximate_device_resistance_ohm": device_resistance_ohm,
        "maximum_device_resistance_ohm": maximum_device_resistance_ohm,
        "nominal_total_resistance_ohm": nominal_total,
        "confirmed_max_device_current_a_rms": max_device_current_a,
        "confirmed_max_device_voltage_v_rms": max_device_voltage_v,
        "maximum_source_v_rms": maximum_source_v,
        "worst_case_current_bound_a_rms": current_bound_a,
        "worst_case_device_voltage_bound_v_rms": voltage_bound_v,
        "nominal_maximum_current_a_rms": nominal_current_a,
        "nominal_maximum_device_voltage_v_rms": (
            nominal_current_a * device_resistance_ohm
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
    harmonics: tuple[int, ...],
    selected_roles_by_harmonic: Mapping[int, tuple[str, ...]],
    harmonic_settle_s: float,
    samples: int,
    sample_interval_s: float,
    record: dict[str, object],
    frequency_rel_tolerance: float,
) -> None:
    raw_samples = record["samples"]
    if not isinstance(raw_samples, list):
        raise TypeError("Sweep point samples must be a list.")
    raw_transitions = record["harmonic_transition_status"]
    if not isinstance(raw_transitions, list):
        raise TypeError("Sweep point harmonic transitions must be a list.")
    for harmonic in harmonics:
        selected_roles = selected_roles_by_harmonic.get(harmonic, ())
        if not selected_roles:
            raise ValueError(f"Harmonic {harmonic} has no selected formal role.")
        if harmonic != 1:
            lockin_xx.set_harmonic(harmonic)
            lockin_xy.set_harmonic(harmonic)
            time.sleep(harmonic_settle_s)
            transition, transition_problems = _consume_harmonic_transition(
                lockin_xx, lockin_xy, harmonic=harmonic
            )
            raw_transitions.append(transition)
            if transition_problems:
                raise Sr830Error(
                    "Unsafe harmonic transition: " + "; ".join(transition_problems)
                )
            time.sleep(harmonic_settle_s)
        for sample_index in range(samples):
            if sample_index:
                time.sleep(sample_interval_s)
            xx = lockin_xx.read_harmonic_sample(harmonic)
            xy = lockin_xy.read_harmonic_sample(harmonic)
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
                "selected_roles": list(selected_roles),
                "lockin_xx": _audited_harmonic_sample_record(xx),
                "lockin_xy": _audited_harmonic_sample_record(xy),
                "problems": problems,
            }
            raw_samples.append(sample_payload)
            if problems:
                raise Sr830Error("Sweep sample rejected: " + "; ".join(problems))
    if harmonics[-1] != 1:
        lockin_xx.set_harmonic(1)
        lockin_xy.set_harmonic(1)
        time.sleep(harmonic_settle_s)
        transition, transition_problems = _consume_harmonic_transition(
            lockin_xx, lockin_xy, harmonic=1
        )
        raw_transitions.append(transition)
        if transition_problems:
            raise Sr830Error(
                "Unsafe harmonic restoration transition: "
                + "; ".join(transition_problems)
            )
        time.sleep(harmonic_settle_s)


def _restore_first_harmonic(instrument: Sr830) -> bool:
    if instrument.read_harmonic() != 1:
        instrument.set_harmonic(1)
        return True
    return False


def _consume_harmonic_transition(
    lockin_xx: Sr830,
    lockin_xy: Sr830,
    *,
    harmonic: int,
) -> tuple[dict[str, object], list[str]]:
    """Record expected filter/reference-range latches after a paired HARM write."""

    xx = lockin_xx.read_harmonic_sample(harmonic)
    xy = lockin_xy.read_harmonic_sample(harmonic)
    problems: list[str] = []
    for sample in (xx, xy):
        role = sample.reading.role.value
        if sample.lia_status.reference_unlocked:
            problems.append(f"lockin_{role} reference unlocked during harmonic transition")
        if sample.lia_status.input_or_reserve_overload:
            problems.append(f"lockin_{role} input/reserve overload during harmonic transition")
        if sample.lia_status.output_overload:
            problems.append(f"lockin_{role} output overload during harmonic transition")
        if sample.lia_status.time_constant_changed:
            problems.append(f"lockin_{role} time constant changed during harmonic transition")
        if sample.error_status:
            problems.append(
                f"lockin_{role} instrument error during harmonic transition is "
                f"{sample.error_status}"
            )
    return (
        {
            "harmonic": harmonic,
            "captured_unix_s": time.time(),
            "expected_transient_latches": [
                "filter_overload",
                "frequency_range_changed",
            ],
            "lockin_xx": _audited_harmonic_sample_record(xx),
            "lockin_xy": _audited_harmonic_sample_record(xy),
            "problems": problems,
        },
        problems,
    )


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
    *,
    allow_xx_output_overload: bool = True,
    allow_output_overload_roles: frozenset[str] = frozenset(),
) -> tuple[dict[str, object], list[str]]:
    """Record and clear sensitivity-transition latches before strict verification.

    Only an explicitly listed role's output-overload-only latch (`LIAS=4`) may
    be discarded. This is used for a deliberate range narrowing; all other
    overloads and transition latches remain failures.
    """

    allowed_roles = set(allow_output_overload_roles)
    if allow_xx_output_overload:
        allowed_roles.add("xx")
    xx = lockin_xx.read_harmonic_sample(1)
    xy = lockin_xy.read_harmonic_sample(1)
    problems: list[str] = []
    for sample in (xx, xy):
        role = sample.reading.role.value
        allowed_output_overload = (
            role in allowed_roles and sample.lia_status.raw == 4
        )
        if sample.lia_status.any_overload and not allowed_output_overload:
            problems.append(f"lockin_{role} overloaded during sensitivity transition")
        if sample.lia_status.reference_unlocked:
            problems.append(
                f"lockin_{role} reference unlocked during sensitivity transition"
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
                f"lockin_{role}.output_overload" for role in sorted(allowed_roles)
            ],
            "allow_xx_output_overload": allow_xx_output_overload,
            "allow_output_overload_roles": sorted(allowed_roles),
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
    original_xy_sensitivity: int | None = None,
    restore_xy_sensitivity: bool = False,
) -> dict[str, object]:
    if not writes_started:
        return {"attempted": False, "verified": True, "errors": []}
    errors: list[str] = []
    diagnostics: dict[str, object] = {}
    actions: list[tuple[str, Callable[[], None]]] = [
        ("restore lockin_xx to 4 mVrms", lockin_xx.set_minimum_sine_output)
    ]
    harmonic_restored = False
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
    for label, instrument in (
        ("restore lockin_xx to first harmonic", lockin_xx),
        ("restore lockin_xy to first harmonic", lockin_xy),
    ):
        try:
            harmonic_restored = _restore_first_harmonic(instrument) or harmonic_restored
        except BaseException as exc:
            errors.append(f"{label}: {exc}")
    transition: dict[str, object] | None = None
    harmonic_transition: dict[str, object] | None = None
    time.sleep(settle_s)
    if harmonic_restored:
        try:
            harmonic_transition, transition_problems = _consume_harmonic_transition(
                lockin_xx, lockin_xy, harmonic=1
            )
            errors.extend(transition_problems)
        except BaseException as exc:
            errors.append(f"harmonic-restoration transition readback: {exc}")
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
    if restore_sensitivity or restore_xy_sensitivity:
        sensitivity_actions: list[tuple[str, Callable[[], None]]] = []
        if restore_sensitivity:
            sensitivity_actions.append(
                (
                    "restore lockin_xx sensitivity",
                    lambda: lockin_xx.set_sensitivity(original_xx_sensitivity),
                )
            )
        if restore_xy_sensitivity:
            if original_xy_sensitivity is None:
                errors.append("original lockin_xy sensitivity is unavailable")
            else:
                sensitivity_actions.append(
                    (
                        "restore lockin_xy sensitivity",
                        lambda: lockin_xy.set_sensitivity(original_xy_sensitivity),
                    )
                )
        for label, action in sensitivity_actions:
            try:
                action()
            except BaseException as exc:
                errors.append(f"{label}: {exc}")
        time.sleep(settle_s)
        try:
            sensitivity_transition, transition_problems = (
                _consume_sensitivity_transition(
                    lockin_xx,
                    lockin_xy,
                    allow_xx_output_overload=restore_sensitivity,
                )
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
        if (
            restore_xy_sensitivity
            and original_xy_sensitivity is not None
            and xy.sensitivity != original_xy_sensitivity
        ):
            errors.append("lockin_xy sensitivity did not restore")
    except BaseException as exc:
        errors.append(f"final readback: {exc}")
    return {
        "attempted": True,
        "verified": not errors,
        "errors": errors,
        "harmonic_transition_status": harmonic_transition,
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
        "config": config,
        "xx_address": xx_address,
        "xy_address": xy_address,
        "timeout_ms": timeout_ms,
        "frequency_hz": frequency_hz,
    }


def _setting_codes(config: LockinConfig) -> Sr830SettingCodes:
    return map_sr830_settings(
        reference_source=config.reference_source,
        external_reference_edge=config.external_reference_edge,
        input_mode=config.input_mode,
        shield_grounding=config.shield_grounding,
        input_coupling=config.input_coupling,
        time_constant_s=config.time_constant_s,
        filter_slope_db_oct=config.filter_slope_db_oct,
        sensitivity_full_scale_v=config.sensitivity_full_scale_v,
    )


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


def _read_verified_autorange_pair(
    lockin_xx: Sr830,
    lockin_xy: Sr830,
    *,
    xx_settings: Sr830SettingCodes,
    xy_settings: Sr830SettingCodes,
    expected_xx_sensitivity: int,
    expected_xx_phase: float | None,
    expected_xy_phase: float | None,
    expected_frequency_hz: float,
    stage: str,
    allow_xx_output_overload: bool = False,
) -> tuple[Sr830Diagnostic, Sr830Diagnostic]:
    """Read a complete pair status window and validate the frozen settings."""

    xx = lockin_xx.read_diagnostic(consume_status_latches=True)
    xy = lockin_xy.read_diagnostic(consume_status_latches=True)
    problems = _diagnostic_problems(xx, xy)
    allowed_xx_output_overload = (
        allow_xx_output_overload
        and xx.lia_status is not None
        and xx.lia_status.raw == 4
    )
    if allowed_xx_output_overload:
        problems.remove("lockin_xx reports overload")
    for diagnostic in (xx, xy):
        if (
            diagnostic.lia_status is not None
            and diagnostic.lia_status.raw != 0
            and not (diagnostic.role is LockinRole.XX and allowed_xx_output_overload)
        ):
            problems.append(
                f"lockin_{diagnostic.role.value} has a nonzero status latch"
            )
    if problems:
        raise Sr830Error(f"{stage} failed: " + "; ".join(problems))
    verify_pair_readback(xx, xy, expected_frequency_hz)
    verify_fixed_settings_readback(
        xx,
        replace(xx_settings, sensitivity=expected_xx_sensitivity),
        xx.phase_shift_deg if expected_xx_phase is None else expected_xx_phase,
    )
    verify_fixed_settings_readback(
        xy,
        xy_settings,
        xy.phase_shift_deg if expected_xy_phase is None else expected_xy_phase,
    )
    return xx, xy


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


def _nonnegative_integer(value: str) -> int:
    converted = int(value)
    if converted < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
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


def _requested_command(argv: Sequence[str] | None) -> str | None:
    arguments = sys.argv[1:] if argv is None else argv
    return arguments[0] if arguments else None


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


if __name__ == "__main__":
    main()
