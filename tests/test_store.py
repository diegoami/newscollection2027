"""Tests for T12's `nc.store`: `DataRoot`, idempotent append, db rebuild.

The acceptance criterion (docs/PLAN.md T12) is that running `nc ingest`
twice produces no git diff on the second run, and the trap named in the
task is `Item.fetched` drifting on a rewrite. So the core test here
asserts *file bytes*, not just item counts, are unchanged by a second
append of the same items -- see
`test_second_append_of_the_same_items_leaves_the_file_byte_identical`.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nc.feeds import Item
from nc.store import (
    DataRoot,
    append_items,
    append_line,
    latest_by_id,
    read_items,
    rebuild_db,
    write_text,
)

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


# --- the same id in two day files -----------------------------------------


def test_rebuild_db_survives_one_id_filed_under_two_dates(tmp_path: Path) -> None:
    """`append_items` only checks the target day file, so an outlet that
    re-publishes an article under a new `published` leaves the same id in
    two files. `items.id` is the primary key, so inserting both rows
    raised IntegrityError and `nc db rebuild` -- the one documented way
    to rebuild the cache -- failed on real data.
    """
    root = DataRoot(tmp_path / "data")
    item_id = "a" * 40
    first = _item(item_id, published="2026-09-14T10:00:00Z")
    republished = replace(
        first, published="2026-09-17T10:00:00Z", fetched="2026-09-17T11:00:00Z"
    )
    append_items(root, [first])
    append_items(root, [republished])

    # Two rows on disk, in two files: that is the store working as built.
    assert len(list(read_items(root))) == 2

    count = rebuild_db(root, tmp_path / "nc.sqlite")

    assert count == 1
    conn = sqlite3.connect(tmp_path / "nc.sqlite")
    try:
        rows = conn.execute("SELECT id, published FROM items").fetchall()
    finally:
        conn.close()
    # The freshest fetched row wins, the same one `nc cluster` uses.
    assert rows == [(item_id, "2026-09-17T10:00:00Z")]


def test_latest_by_id_keeps_the_freshest_fetched_row() -> None:
    older = _item("a" * 40, published="2026-09-14T10:00:00Z")
    newer = replace(
        older, published="2026-09-17T10:00:00Z", fetched="2026-09-17T11:00:00Z"
    )
    other = _item("b" * 40, published="2026-09-15T10:00:00Z")

    assert latest_by_id([newer, older, other]) == [other, newer]
    assert latest_by_id([older, newer, other]) == [other, newer]


# --- bytes on disk, on every platform -------------------------------------
#
# These pin a guarantee this CI cannot observe. `Path.write_text` opens
# in text mode, and text mode on Windows translates every "\n" into
# "\r\n"; on Linux the bug is invisible, so a test asserting the bytes
# come out with no "\r" passes either way. What can be checked anywhere
# is the mechanism -- that the writers disable the translation, and that
# nothing in the package goes around them.


def _newline_used(write: Callable[[Path], object], path: Path) -> object:
    """The `newline=` the writer opened `path` with.

    Checked rather than inferred from the bytes, because on Linux the
    bytes are identical either way -- the translation this guards
    against only happens on Windows, and a test that cannot fail on CI
    is not a guard.
    """
    seen: dict[str, object] = {}
    real = Path.open

    def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "open", spy)
        write(path)
    return seen.get("newline", "<not passed>")


def test_write_text_disables_newline_translation(tmp_path: Path) -> None:
    path = tmp_path / "x.json"

    assert _newline_used(lambda p: write_text(p, "one\ntwo\n"), path) == ""
    assert path.read_bytes() == b"one\ntwo\n"


def test_append_line_disables_newline_translation(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"

    assert _newline_used(lambda p: append_line(p, "one"), path) == ""
    append_line(path, "two")
    assert path.read_bytes() == b"one\ntwo\n"


def test_append_items_disables_newline_translation(tmp_path: Path) -> None:
    """The store's own writer, which opens the file itself rather than
    going through `append_line` (it holds one handle for a whole day's
    batch)."""
    root = DataRoot(tmp_path / "data")
    item = _item("a" * 40)
    path = root.item_file_for(item.published)
    path.parent.mkdir(parents=True, exist_ok=True)

    assert _newline_used(lambda _: append_items(root, [item]), path) == ""
    assert b"\r" not in path.read_bytes()


# `store.write_text(path, text)` is the helper itself, reached through
# its module; `path.write_text(text)` is the thing being banned. The
# first version of this guard matched both and flagged `nc.cli` for
# calling the helper correctly -- a guard that cries wolf gets widened
# by whoever trips over it, so it is narrowed here instead.
_PATH_WRITE_TEXT = re.compile(r"(?<!\bstore)\.write_text\(")


def test_no_module_writes_a_pipeline_file_with_path_write_text() -> None:
    """The regression guard. `Path.write_text` is the easy thing to
    reach for and the one that breaks byte-stability on Windows, and no
    Linux test can catch a new call site after the fact -- so the call
    site itself is what is checked.
    """
    package = Path(__file__).resolve().parents[1] / "src" / "nc"
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(package.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if _PATH_WRITE_TEXT.search(line)
    ]
    assert offenders == [], (
        "use nc.store.write_text (or append_line) instead of Path.write_text: "
        + ", ".join(offenders)
    )


# `path.open("w")`, `open(path, "a")`, `io.open(f, mode="wt")` -- any
# write- or append-mode text open, however it is spelled. Binary modes
# are excluded because binary never translates; `newline=` on the same
# line is what clears it.
#
# A heuristic over source text, not a parser: it sees one line at a
# time, so a call split across lines with the mode on its own line, or a
# mode held in a variable, slips past. That is a deliberate floor rather
# than a ceiling -- it catches the forms anyone actually writes, and the
# test below pins which those are, because a guard nobody has seen fail
# is a guard nobody knows works.
_WRITE_OPEN = re.compile(r"""\bopen\(\s*[^)]*?["'][wax]t?\+?["']""")


def _write_mode_opens_without_newline(source: str) -> list[int]:
    return [
        number
        for number, line in enumerate(source.split("\n"), 1)
        if _WRITE_OPEN.search(line) and "newline=" not in line
    ]


def test_the_write_mode_detector_catches_what_it_claims_to() -> None:
    for line in [
        'path.open("w", encoding="utf-8")',
        "path.open('a', encoding='utf-8')",
        'open(path, "w")',
        'with open(name, "at") as fh:',
        'io.open(path, mode="w", encoding="utf-8")',
    ]:
        assert _write_mode_opens_without_newline(line) == [1], line

    for line in [
        'path.open("r", encoding="utf-8")',
        'path.open("rb")',
        'open(path, "wb")',  # binary never translates
        'path.open("w", encoding="utf-8", newline="")',
        'open(path, "a", newline="")',
    ]:
        assert _write_mode_opens_without_newline(line) == [], line


def test_every_writer_opens_in_binary_safe_text_mode() -> None:
    """A write- or append-mode text open without `newline=""` translates
    on Windows. Covers the builtin `open(path, "w")` as well as
    `Path.open` -- both are things a new writer would reach for, and the
    first form used to slip past this guard entirely."""
    package = Path(__file__).resolve().parents[1] / "src" / "nc"
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(package.glob("*.py"))
        for number in _write_mode_opens_without_newline(
            path.read_text(encoding="utf-8")
        )
    ]
    assert offenders == [], 'every write-mode open needs newline="": ' + ", ".join(
        offenders
    )


def test_the_path_write_text_guard_still_catches_the_real_thing() -> None:
    """Narrowing it to exclude the helper must not blind it to the ban."""
    assert _PATH_WRITE_TEXT.search('path.write_text(text, encoding="utf-8")')
    assert _PATH_WRITE_TEXT.search("    self._path.write_text(body)")
    assert not _PATH_WRITE_TEXT.search("store.write_text(path, body)")
    assert not _PATH_WRITE_TEXT.search("    write_text(path, body)")
