from . import __version__


def main() -> None:
    print(f"attodry-transport-control {__version__}")
    print("Stages 1-7 offline implementations are complete.")
    print("Laboratory commissioning and exact SMU adapters remain pending.")
    print("Simulation, monitoring, and analysis paths do not connect to hardware.")
    print("Use the separately authorized lockin_test tool for SR830 bench testing.")


if __name__ == "__main__":
    main()
