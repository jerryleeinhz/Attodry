"""Standalone PM100D CLI."""
from .optical_cli import standalone_main


def main(argv=None):
    return standalone_main("pm100d", argv)


if __name__ == "__main__":
    raise SystemExit(main())
