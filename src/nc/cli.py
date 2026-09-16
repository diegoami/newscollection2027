"""Command-line interface for nc.

Subcommands (`ingest`, `cluster`, `pending`, `validate`, `analyze`,
`build`, `sync`, `nightly`) are added by the tasks in `docs/PLAN.md` that
implement them. This skeleton only wires up `--version` and `--help`.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from nc import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nc",
        description=(
            "newscollection2027: cluster tech-news feed items across "
            "outlets and check their claims against each other."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
