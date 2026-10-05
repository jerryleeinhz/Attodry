"""Standalone PEM CLI."""
from .optical_cli import standalone_main


def main(argv=None):
    return standalone_main("pem", argv)


if __name__ == "__main__":
    raise SystemExit(main())
