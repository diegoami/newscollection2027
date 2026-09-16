"""Tests for T12's `nc.store`: `DataRoot`, idempotent append, db rebuild.

The acceptance criterion (docs/PLAN.md T12) is that running `nc ingest`
twice produces no git diff on the second run, and the trap named in the
task is `Item.fetched` drifting on a rewrite. So the core test here
asserts *file bytes*, not just item counts, are unchanged by a second
append of the same items -- see
`test_second_append_of_the_same_items_leaves_the_file_byte_identical`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from nc.feeds import Item
from nc.store import DataRoot, append_items, read_items, rebuild_db

FETCHED_RUN_1 = "2026-09-16T15:00:00Z"
FETCHED_RUN_2 = "2026-09-16T18:00:00Z"


def _item(
    id_: str,
    published: str = "2026-09-16T10:00:00Z",
    fetched: str = FETCHED_RUN_1,
    outlet: str = "theverge",
) -> Item:
    return Item(
        id=id_,
        outlet=outlet,
        url=f"https://example.com/{id_}",
        title=f"Title {id_}",
        lede=f"Lede for {id_}.",
        author=None,
        published=published,
        fetched=fetched,
        tags=(),
    )


# --- DataRoot -----------------------------------------------------------


def test_from_env_uses_nc_data_root_when_set(tmp_path: Path) -> None:
    root = DataRoot.from_env({"NC_DATA_ROOT": str(tmp_path / "custom")})
    assert root.path == tmp_path / "custom"


def test_from_env_falls_back_to_data_dir_when_unset() -> None:
    root = DataRoot.from_env({})
    assert root.path == Path("data")


def test_item_file_for_uses_published_date_not_fetched_date() -> None:
    root = DataRoot(Path("/tmp/does-not-matter"))
    item = _item(
        "abc", published="2026-01-02T00:00:00Z", fetched="2026-09-16T00:00:00Z"
    )
    path = root.item_file_for(item.published)
    assert path == root.path / "items" / "2026" / "01" / "02.jsonl"


# --- append_items: the byte-stability guarantee --------------------------


def test_second_append_of_the_same_items_leaves_the_file_byte_identical(
    tmp_path: Path,
) -> None:
    root = DataRoot(tmp_path / "data")
    items = [_item("a" * 40, published="2026-09-16T10:00:00Z", fetched=FETCHED_RUN_1)]

    result_1 = append_items(root, items)
    assert result_1.added == 1
    assert result_1.skipped == 0

    path = root.item_file_for(items[0].published)
    bytes_after_first_run = path.read_bytes()

    # A second "ingest" sees the very same item, but stamped with a
    # later `fetched` -- as a re-fetch of an unchanged feed entry would
    # be. The stored copy must not pick that up.
    same_items_later_fetch = [
        _item("a" * 40, published="2026-09-16T10:00:00Z", fetched=FETCHED_RUN_2)
    ]
    result_2 = append_items(root, same_items_later_fetch)
    assert result_2.added == 0
    assert result_2.skipped == 1

    bytes_after_second_run = path.read_bytes()
    assert bytes_after_second_run == bytes_after_first_run

    # And the original fetched survives, not the second run's.
    stored = list(read_items(root))
    assert len(stored) == 1
    assert stored[0].fetched == FETCHED_RUN_1


def test_a_genuinely_new_item_is_appended(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    first = [_item("a" * 40, published="2026-09-16T10:00:00Z")]
    append_items(root, first)

    path = root.item_file_for(first[0].published)
    bytes_before = path.read_bytes()

    second_run_items = [
        _item("a" * 40, published="2026-09-16T10:00:00Z"),  # already stored
        _item("b" * 40, published="2026-09-16T11:00:00Z"),  # genuinely new
    ]
    result = append_items(root, second_run_items)
    assert result.added == 1
    assert result.skipped == 1

    bytes_after = path.read_bytes()
    # The old bytes are an unmodified prefix: the existing line was
    # never rewritten, only a new line was appended after it.
    assert bytes_after.startswith(bytes_before)
    assert len(bytes_after) > len(bytes_before)

    stored_ids = {item.id for item in read_items(root)}
    assert stored_ids == {"a" * 40, "b" * 40}


def test_new_items_in_one_batch_are_ordered_by_id_regardless_of_input_order(
    tmp_path: Path,
) -> None:
    root = DataRoot(tmp_path / "data")
    same_date = "2026-09-16T10:00:00Z"
    items_order_1 = [
        _item("c" * 40, published=same_date),
        _item("a" * 40, published=same_date),
        _item("b" * 40, published=same_date),
    ]
    append_items(root, items_order_1)
    path = root.item_file_for(same_date)
    bytes_1 = path.read_bytes()

    root_2 = DataRoot(tmp_path / "data2")
    items_order_2 = [
        _item("b" * 40, published=same_date),
        _item("c" * 40, published=same_date),
        _item("a" * 40, published=same_date),
    ]
    append_items(root_2, items_order_2)
    path_2 = root_2.item_file_for(same_date)
    bytes_2 = path_2.read_bytes()

    assert bytes_1 == bytes_2
    ids_in_file = [
        line.split('"id":"')[1][:40] for line in bytes_1.decode().splitlines()
    ]
    assert ids_in_file == sorted(ids_in_file)


def test_serialized_line_has_no_trailing_whitespace_and_one_newline(
    tmp_path: Path,
) -> None:
    root = DataRoot(tmp_path / "data")
    items = [_item("a" * 40)]
    append_items(root, items)
    path = root.item_file_for(items[0].published)
    raw = path.read_bytes()

    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    line = raw.decode().rstrip("\n")
    assert line == line.rstrip()  # no trailing whitespace before the newline
    assert "\n" not in line  # exactly one line


def test_duplicate_id_within_one_batch_collapses_to_one_line(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    same_date = "2026-09-16T10:00:00Z"
    items = [
        _item("a" * 40, published=same_date, fetched=FETCHED_RUN_1),
        _item("a" * 40, published=same_date, fetched=FETCHED_RUN_2),
    ]
    result = append_items(root, items)
    assert result.added == 1
    assert result.skipped == 0

    stored = list(read_items(root))
    assert len(stored) == 1
    assert stored[0].fetched == FETCHED_RUN_1  # first occurrence wins


def test_items_for_different_dates_go_to_different_files(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    items = [
        _item("a" * 40, published="2026-01-05T00:00:00Z"),
        _item("b" * 40, published="2026-02-20T00:00:00Z"),
    ]
    append_items(root, items)

    assert (root.items_dir() / "2026" / "01" / "05.jsonl").exists()
    assert (root.items_dir() / "2026" / "02" / "20.jsonl").exists()


# --- read_items -----------------------------------------------------------


def test_read_items_round_trips_every_field(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    original = Item(
        id="f" * 40,
        outlet="wired",
        url="https://example.com/story",
        title="A title",
        lede="A lede.",
        author="Jane Doe",
        published="2026-09-16T10:00:00Z",
        fetched="2026-09-16T15:00:00Z",
        tags=("ai", "policy"),
    )
    append_items(root, [original])

    (stored,) = list(read_items(root))
    assert stored == original


def test_read_items_on_an_empty_data_root_yields_nothing(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    assert list(read_items(root)) == []


# --- rebuild_db ------------------------------------------------------------


def test_rebuild_db_loads_every_item(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    items = [
        _item("a" * 40, published="2026-09-16T10:00:00Z"),
        _item("b" * 40, published="2026-09-17T10:00:00Z"),
    ]
    append_items(root, items)

    db_path = tmp_path / "cache" / "nc.sqlite"
    count = rebuild_db(root, db_path)
    assert count == 2

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id FROM items ORDER BY id").fetchall()
    finally:
        conn.close()
    assert [row[0] for row in rows] == sorted(item.id for item in items)


def test_rebuild_db_is_a_full_rebuild_not_an_upsert(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "cache" / "nc.sqlite"

    append_items(root, [_item("a" * 40)])
    assert rebuild_db(root, db_path) == 1

    # Simulate an item's file being edited out from under the cache
    # (e.g. moved outlet, or -- more realistically -- pointed at a
    # smaller fixture set between two calls). A second rebuild must
    # reflect the current JSONL exactly, not accumulate leftovers.
    for jsonl in root.items_dir().glob("*/*/*.jsonl"):
        jsonl.unlink()
    append_items(root, [_item("b" * 40)])

    assert rebuild_db(root, db_path) == 1
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id FROM items").fetchall()
    finally:
        conn.close()
    assert [row[0] for row in rows] == ["b" * 40]
