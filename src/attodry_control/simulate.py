from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .acquisition import SimulationRunEngine
from .config import load_config
from .models import VectorField
from .records import ExperimentCondition
from .simulation import SimulationStation
from .storage import RunStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a hardware-free audited end-to-end acquisition."
    )
    parser.add_argument("--config", type=Path, default=Path("config/simulation.toml"))
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--temperature-k", type=float, default=2.0)
    parser.add_argument("--bx-t", type=float, default=0.0)
    parser.add_argument("--bz-t", type=float, default=0.0)
    parser.add_argument("--gate-top-v", type=float, default=0.0)
    parser.add_argument("--gate-bottom-v", type=float, default=0.0)
    parser.add_argument(
        "--inject-first-unlock",
        action="store_true",
        help="Reject the first attempt to exercise raw retention and retry.",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    created_stations = 0

    def station_factory() -> SimulationStation:
        nonlocal created_stations
        station = SimulationStation.from_config(config)
        if args.inject_first_unlock and created_stations == 0:
            station.lockin_xy.locked = False
        created_stations += 1
        return station

    condition = ExperimentCondition(
        condition_id="condition-0000",
        sequence_index=0,
        temperature_k=args.temperature_k,
        field=VectorField(args.bx_t, args.bz_t),
        excitation_v=config.lockin_xx.source_voltage_v,
        frequency_hz=config.lockin_xx.frequency_hz,
        gate_top_v=args.gate_top_v,
        gate_bottom_v=args.gate_bottom_v,
        scan_id="single-condition",
    )
    args.database.parent.mkdir(parents=True, exist_ok=True)
    with RunStore(args.database) as store:
        summary = SimulationRunEngine(
            store=store,
            run_id=args.run_id,
            station_factory=station_factory,
            max_attempts_per_condition=2,
            normal_end_field_policy=config.cleanup.normal_end_field_policy,
        ).start_new(
            (condition,),
            config_snapshot={
                "mode": config.project.mode.value,
                "source": str(args.config),
                "created_at_utc": datetime.now(UTC).isoformat(),
            },
        )
    print(
        f"run_id={summary.run_id} accepted_conditions="
        f"{summary.accepted_conditions} rejected_attempts="
        f"{summary.rejected_attempts}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
