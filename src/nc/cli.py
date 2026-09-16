"""Command-line interface for nc.

Subcommands (`cluster`, `pending`, `validate`, `analyze`, `build`,
`nightly`) are added by the tasks in `docs/PLAN.md` that implement them.
`feeds check` is wired here by T10; `ingest`, `db rebuild` and
`sync pull|push` are wired here by T12.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

import feedparser

from nc import __version__, feeds, store, sync
from nc.store import DataRoot


def _feeds_check(args: argparse.Namespace) -> int:
    outlets = feeds.load_outlets(args.outlets)
    thresholds = feeds.load_thresholds(args.thresholds)
    results = feeds.check_feeds(outlets, thresholds)

    if args.json:
        print(json.dumps([feeds.result_to_dict(r) for r in results], indent=2))
    else:
        print(feeds.format_report(results))

    return 0 if all(result.ok for result in results) else 1


def _data_root(args: argparse.Namespace) -> DataRoot:
    return DataRoot(args.data_root) if args.data_root else DataRoot.from_env()


def _ingest(args: argparse.Namespace) -> int:
    """Fetch every configured feed, normalize entries, append new items.

    Reuses `nc.feeds` for both fetching (the same `feedparser.parse`
    call and user agent as `nc feeds check`) and normalization
    (`normalize_entries`, T11); this command only adds the storage step
    (`nc.store.append_items`, T12).
    """
    outlets = feeds.load_outlets(args.outlets)
    thresholds = feeds.load_thresholds(args.thresholds)
    normalize_config = feeds.load_normalize_config(args.thresholds)
    data_root = _data_root(args)

    fetched = feeds.utc_now_iso()
    items: list[feeds.Item] = []
    for outlet in outlets:
        parsed = feedparser.parse(outlet.feed_url, agent=thresholds.user_agent)
        items.extend(
            feeds.normalize_entries(
                outlet, parsed, fetched, normalize_config.lede_word_cap
            )
        )

    result = store.append_items(data_root, items)
    print(f"ingest: {result.added} new item(s), {result.skipped} already stored")
    return 0


def _db_rebuild(args: argparse.Namespace) -> int:
    data_root = _data_root(args)
    count = store.rebuild_db(data_root, args.db)
    print(f"db rebuild: {count} item(s) loaded into {args.db}")
    return 0


def _sync_pull(args: argparse.Namespace) -> int:
    data_root = _data_root(args)
    config = sync.load_sync_config(args.config)
    sync.pull(data_root, config)
    print(f"sync pull: {data_root.path} up to date with origin/{config.branch}")
    return 0


def _sync_push(args: argparse.Namespace) -> int:
    data_root = _data_root(args)
    config = sync.load_sync_config(args.config)
    message = args.message or f"data: sync {feeds.utc_now_iso()}"
    pushed = sync.push(data_root, config, message)
    print("sync push: pushed" if pushed else "sync push: nothing changed")
    return 0


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

    data_root_help = (
        f"data root (default: ${store.ENV_VAR}, else {store.DEFAULT_DATA_ROOT})"
    )

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="fetch every configured feed and append new items to the data root",
    )
    ingest_parser.add_argument(
        "--outlets", type=Path, default=feeds.DEFAULT_OUTLETS_PATH
    )
    ingest_parser.add_argument(
        "--thresholds", type=Path, default=feeds.DEFAULT_THRESHOLDS_PATH
    )
    ingest_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    ingest_parser.set_defaults(func=_ingest)

    db_parser = subparsers.add_parser(
        "db", help="the SQLite cache built from the data root"
    )
    db_subparsers = db_parser.add_subparsers(dest="db_command")

    db_rebuild_parser = db_subparsers.add_parser(
        "rebuild", help="rebuild .cache/nc.sqlite from the JSONL files"
    )
    db_rebuild_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    db_rebuild_parser.add_argument(
        "--db",
        type=Path,
        default=store.DEFAULT_DB_PATH,
        help=f"SQLite file to write (default: {store.DEFAULT_DB_PATH})",
    )
    db_rebuild_parser.set_defaults(func=_db_rebuild)

    sync_parser = subparsers.add_parser(
        "sync", help="clone/pull or commit/push the data repo"
    )
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command")

    sync_pull_parser = sync_subparsers.add_parser(
        "pull", help="clone the data repo into the data root, or fast-forward it"
    )
    sync_pull_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    sync_pull_parser.add_argument(
        "--config", type=Path, default=sync.DEFAULT_SYNC_CONFIG_PATH
    )
    sync_pull_parser.set_defaults(func=_sync_pull)

    sync_push_parser = sync_subparsers.add_parser(
        "push", help="commit and push the data root, only if something changed"
    )
    sync_push_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    sync_push_parser.add_argument(
        "--config", type=Path, default=sync.DEFAULT_SYNC_CONFIG_PATH
    )
    sync_push_parser.add_argument(
        "--message",
        default=None,
        help="commit message (default: data: sync <timestamp>)",
    )
    sync_push_parser.set_defaults(func=_sync_push)

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
