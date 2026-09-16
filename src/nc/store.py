"""T12: `DataRoot`, idempotent item storage, and the SQLite cache.

docs/ARCHITECTURE.md Principle 3 and docs/STORAGE.md: the code only ever
sees a directory. `DataRoot` is that directory -- by default a checkout
of `newscollection2027-data` at `NC_DATA_ROOT` -- and knows nothing about
git; `nc.sync` is the only module that clones, pulls, commits or pushes
it (docs/DECISIONS.md: "the code sees only a data root directory; `nc
sync` is the only part that changes").

Items live at `items/YYYY/MM/DD.jsonl`, one JSON object per line, keyed
by `Item.id`. The date is the item's `published` date in UTC, not the
date it was fetched: `published` is stable across runs (it comes from
the feed, or from `fetched` as a one-time fallback per `nc.feeds`), so
filing by it means a given item id always lands in the same file no
matter which run first saw it. Filing by "today" would let a late-
arriving item -- fetched today, published yesterday, or fetched just
after local midnight while a peer runner is still on the previous UTC
day -- land in a different file across runs and duplicate itself.

Append is the whole point of the acceptance criterion (docs/PLAN.md
T12: "running `nc ingest` twice in a row produces no git diff on the
second run"), so this module is deliberately paranoid about it:

- An id already present in a file is never rewritten. `append_items`
  only ever opens a file in append ("a") mode and only when there is at
  least one genuinely new line to add, so bytes already on disk -- an
  existing item's `fetched`, in particular -- are never touched.
- New items for the same file are sorted by id before writing, so their
  order does not depend on feed order (which can jitter across fetches)
  or dict/set iteration order.
- Serialization is byte-stable: `_dumps` fixes key order with
  `sort_keys=True` (robust to `Item`'s field order ever changing),
  `ensure_ascii=True` decided once, compact separators, and exactly one
  trailing newline per line.

The SQLite cache (`.cache/nc.sqlite`, never committed per CLAUDE.md) is
rebuilt from scratch on every `rebuild_db` call: it holds no state of
its own, only a queryable copy of the JSONL files.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from nc.feeds import Item

ENV_VAR = "NC_DATA_ROOT"

# Fallback when NC_DATA_ROOT is unset: a `data/` directory under the
# current working directory, mirroring the `.cache/` local-cache
# convention and already covered by this repo's .gitignore. It lets
# `nc ingest` etc. run out of the box in a throwaway checkout without
# NC_DATA_ROOT set, e.g. for a quick local smoke test; the real data
# root in every configured environment is a checkout of
# newscollection2027-data (docs/DECISIONS.md).
DEFAULT_DATA_ROOT = Path("data")

DEFAULT_DB_PATH = Path(".cache/nc.sqlite")


@dataclass(frozen=True)
class DataRoot:
    """A plain directory holding the pipeline's text files.

    No git knowledge here on purpose -- see the module docstring. Other
    tasks (T13, T21, T43, ...) should add their own named subdirectory
    accessors here rather than building paths inline, so the directory
    layout stays in one place.
    """

    path: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DataRoot:
        mapping = os.environ if env is None else env
        raw = mapping.get(ENV_VAR)
        return cls(Path(raw) if raw else DEFAULT_DATA_ROOT)

    def resolve(self, *parts: str) -> Path:
        """A path under the data root. The generic escape hatch for
        subdirectories this module does not (yet) name explicitly."""
        return self.path.joinpath(*parts)

    def items_dir(self) -> Path:
        return self.path / "items"

    def item_file_for(self, published: str) -> Path:
        """`items/YYYY/MM/DD.jsonl` for an item whose `published` is
        `published`. `published` is ISO 8601 UTC (`nc.feeds.Item`), so
        its first 10 characters are always `YYYY-MM-DD`."""
        year, month, day = published[:10].split("-")
        return self.items_dir() / year / month / f"{day}.jsonl"


@dataclass(frozen=True)
class AppendResult:
    added: int
    skipped: int


def _dumps(item: Item) -> str:
    # sort_keys=True fixes key order independently of Item's field
    # order or dict insertion order -- the actual "fixed key order"
    # guarantee, not just a currently-true accident of dict literals.
    # ensure_ascii=True and fixed separators are likewise decided once
    # here and never varied, so the same Item always serializes to the
    # same bytes.
    payload = {
        "author": item.author,
        "fetched": item.fetched,
        "id": item.id,
        "lede": item.lede,
        "outlet": item.outlet,
        "published": item.published,
        "tags": list(item.tags),
        "title": item.title,
        "url": item.url,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _item_from_dict(raw: dict[str, object]) -> Item:
    tags = raw.get("tags") or []
    assert isinstance(tags, list)
    author = raw.get("author")
    assert author is None or isinstance(author, str)
    return Item(
        id=str(raw["id"]),
        outlet=str(raw["outlet"]),
        url=str(raw["url"]),
        title=str(raw["title"]),
        lede=str(raw["lede"]),
        author=author,
        published=str(raw["published"]),
        fetched=str(raw["fetched"]),
        tags=tuple(str(tag) for tag in tags),
    )


def _existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                ids.add(json.loads(line)["id"])
    return ids


def append_items(data_root: DataRoot, items: Iterable[Item]) -> AppendResult:
    """Idempotent append, keyed by `Item.id`, filed by `published` date.

    An id already present in its file is left byte-for-byte untouched:
    the file is opened for append, never for rewrite, and only when
    there is a genuinely new line to add. Duplicate ids within `items`
    itself (e.g. the same item reappearing at the edge of two fetches
    in one ingest run) collapse to one line, first occurrence wins.
    """
    by_file: dict[Path, list[Item]] = {}
    seen_in_batch: set[str] = set()
    for item in items:
        if item.id in seen_in_batch:
            continue
        seen_in_batch.add(item.id)
        by_file.setdefault(data_root.item_file_for(item.published), []).append(item)

    added = 0
    skipped = 0
    for path, group in by_file.items():
        existing_ids = _existing_ids(path)
        new_items = sorted(
            (item for item in group if item.id not in existing_ids),
            key=lambda item: item.id,
        )
        added += len(new_items)
        skipped += len(group) - len(new_items)
        if not new_items:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for item in new_items:
                fh.write(_dumps(item))
                fh.write("\n")

    return AppendResult(added=added, skipped=skipped)


def read_items(data_root: DataRoot) -> Iterator[Item]:
    """Every stored item, in a deterministic (file path, then id) order."""
    items_dir = data_root.items_dir()
    if not items_dir.exists():
        return
    for path in sorted(items_dir.glob("*/*/*.jsonl")):
        rows = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(_item_from_dict(json.loads(line)))
        rows.sort(key=lambda item: item.id)
        yield from rows


_SCHEMA = """
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    outlet TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    lede TEXT NOT NULL,
    author TEXT,
    published TEXT NOT NULL,
    fetched TEXT NOT NULL,
    tags TEXT NOT NULL
)
"""


def rebuild_db(data_root: DataRoot, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Rebuild `.cache/nc.sqlite` from the JSONL files. Returns the item count.

    Always starts from an empty database: the cache holds no state of
    its own (CLAUDE.md), so "rebuild" means exactly that, not "upsert".
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_SCHEMA)
        count = 0
        for item in read_items(data_root):
            conn.execute(
                "INSERT INTO items "
                "(id, outlet, url, title, lede, author, published, fetched, tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.outlet,
                    item.url,
                    item.title,
                    item.lede,
                    item.author,
                    item.published,
                    item.fetched,
                    json.dumps(list(item.tags)),
                ),
            )
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count
