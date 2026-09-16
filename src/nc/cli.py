"""Command-line interface for nc.

Subcommands (`ingest`, `cluster`, `pending`, `validate`, `analyze`,
`build`, `sync`, `nightly`) are added by the tasks in `docs/PLAN.md` that
implement them. `feeds check` is wired here by T10.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from nc import __version__, feeds


def _feeds_check(args: argparse.Namespace) -> int:
    outlets = feeds.load_outlets(args.outlets)
    thresholds = feeds.load_thresholds(args.thresholds)
    results = feeds.check_feeds(outlets, thresholds)

    if args.json:
        print(json.dumps([feeds.result_to_dict(r) for r in results], indent=2))
    else:
        print(feeds.format_report(results))

    return 0 if all(result.ok for result in results) else 1


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

    subparsers = parser.add_subparsers(dest="command")

    feeds_parser = subparsers.add_parser("feeds", help="outlet feed registry")
    feeds_subparsers = feeds_parser.add_subparsers(dest="feeds_command")

    check_parser = feeds_subparsers.add_parser(
        "check",
        help="fetch every configured feed and report item counts, newest "
        "date and whether ledes are present",
    )
    check_parser.add_argument(
        "--outlets",
        type=Path,
        default=feeds.DEFAULT_OUTLETS_PATH,
        help=f"outlet registry (default: {feeds.DEFAULT_OUTLETS_PATH})",
    )
    check_parser.add_argument(
        "--thresholds",
        type=Path,
        default=feeds.DEFAULT_THRESHOLDS_PATH,
        help=f"feed thresholds (default: {feeds.DEFAULT_THRESHOLDS_PATH})",
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of the text report",
    )
    check_parser.set_defaults(func=_feeds_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    func = getattr(args, "func", None)
    if func is not None:
        return_code: int = func(args)
        return return_code

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
