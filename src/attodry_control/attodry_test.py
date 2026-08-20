from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
import time
from typing import Callable, Sequence

from .attodry import (
    AttoDryAuthorizationError,
    AttoDryDriver,
    load_attodry_dll,
)
from .config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only attoDRY state commissioning tool."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--samples", type=_positive_integer, default=1)
    parser.add_argument("--interval-s", type=_nonnegative_float, default=1.0)
    parser.add_argument(
        "--authorize-connection",
        action="store_true",
        help="Authorize begin/connect/read/disconnect only; setting writes remain disabled.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except KeyboardInterrupt:
        print(
            "Interrupted. Disconnect/end cleanup was attempted; manually verify the "
            "attoDRY connection and magnet state.",
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
    dll_loader: Callable[[str | Path], object] = load_attodry_dll,
) -> int:
    args = build_parser().parse_args(argv)
    if not args.authorize_connection:
        raise AttoDryAuthorizationError(
            "attoDRY read-only connection was not explicitly authorized."
        )
    config = load_config(args.config)
    if config.cryostat.dll_path is None:
        raise ValueError("Hardware cryostat DLL path is missing.")
    dll = dll_loader(config.cryostat.dll_path)
    driver = AttoDryDriver.from_config(
        config,
        dll=dll,
        connection_authorized=True,
        writes_authorized=False,
    )
    states = []
    driver.connect()
    try:
        for index in range(args.samples):
            if index:
                time.sleep(args.interval_s)
            states.append(
                {
                    "sample_index": index,
                    "captured_unix_s": time.time(),
                    "state": asdict(driver.read_state()),
                }
            )
    except BaseException as exc:
        try:
            driver.close()
        except BaseException as cleanup_error:
            exc.add_note(f"attoDRY close cleanup also failed: {cleanup_error}")
        print(
            json.dumps(
                {
                    "completed": False,
                    "error": str(exc),
                    "samples": states,
                    "last_confirmed_state": (
                        None
                        if driver.last_confirmed_state is None
                        else asdict(driver.last_confirmed_state)
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise
    driver.close()
    print(
        json.dumps(
            {
                "completed": True,
                "writes_authorized": False,
                "samples": states,
                "disconnected": True,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _positive_integer(value: str) -> int:
    converted = int(value)
    if converted <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return converted


def _nonnegative_float(value: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return converted


if __name__ == "__main__":
    main()
