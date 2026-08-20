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
    Sr830,
    Sr830Diagnostic,
    Sr830Error,
    configure_minimum_excitation_pair,
)


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
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except KeyboardInterrupt:
        print(
            "Interrupted. No automatic output write was attempted; manually verify "
            "lockin_xx is at 4 mVrms before disconnecting the device.",
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
        abs_tol=0.0001,
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


if __name__ == "__main__":
    main()
