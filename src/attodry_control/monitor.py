from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import Sequence

from .storage import RunMonitor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only WAL-safe acquisition run monitor."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with RunMonitor(args.database) as monitor:
        print(json.dumps(asdict(monitor.summary(args.run_id)), indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
