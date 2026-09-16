"""End-to-end test for T12's `nc ingest`, against real fixture feeds.

No network egress to news domains is available in this sandbox, so this
points a local outlets registry at the fixture feeds under
`tests/fixtures/feeds/` -- `feedparser` accepts a filesystem path, and
T10's `test_cli.py` already relies on the same trick. That gives a
genuine end-to-end run of fetch -> normalize (T11) -> store (T12) with
no network.

docs/PLAN.md T12's acceptance criterion: running `nc ingest` twice in a
row produces no git diff on the second run. This asserts that at the
level `nc ingest` actually writes at -- the JSONL bytes on disk -- not
merely that a second run reports zero new items (that could be true of
a command that silently does nothing at all, which is why there is also
a test that a genuinely new item does get appended).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nc.cli import main
from nc.feeds import Item
from nc.store import DataRoot, read_items

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def _write_config(
    tmp_path: Path, outlet_feeds: dict[str, Path], name: str = "outlets.yaml"
) -> Path:
    lines = ["outlets:"]
    for slug, feed_path in outlet_feeds.items():
        lines.append(f"  - slug: {slug}")
        lines.append(f"    display_name: {slug}")
        lines.append("    homepage: https://example.com/")
        lines.append(f"    feed_url: {feed_path}")
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return path


def _thresholds_path(tmp_path: Path) -> Path:
    path = tmp_path / "feeds.yaml"
    path.write_text(
        "user_agent: test-agent\n"
        "lede_min_median_words: 8\n"
        "lede_max_equal_title_share: 0.5\n"
        "stale_after_hours: 1000000\n"
        "lede_word_cap: 60\n"
    )
    return path


def _all_jsonl_bytes(root: DataRoot) -> dict[Path, bytes]:
    return {
        path: path.read_bytes() for path in sorted(root.items_dir().glob("*/*/*.jsonl"))
    }


def test_ingest_twice_produces_no_diff_on_the_second_run(tmp_path: Path) -> None:
    outlets_path = _write_config(
        tmp_path,
        {
            "theverge": FIXTURES / "theverge.xml",
            "arstechnica": FIXTURES / "arstechnica.xml",
            "theguardian": FIXTURES / "theguardian.xml",
        },
    )
    thresholds_path = _thresholds_path(tmp_path)
    data_root_path = tmp_path / "data-root"

    argv = [
        "ingest",
        "--outlets",
        str(outlets_path),
        "--thresholds",
        str(thresholds_path),
        "--data-root",
        str(data_root_path),
    ]

    exit_code_1 = main(argv)
    assert exit_code_1 == 0

    root = DataRoot(data_root_path)
    files_after_first_run = _all_jsonl_bytes(root)
    assert files_after_first_run  # something was actually written

    exit_code_2 = main(argv)
    assert exit_code_2 == 0

    files_after_second_run = _all_jsonl_bytes(root)
    assert files_after_second_run == files_after_first_run


def test_ingest_appends_a_genuinely_new_item_on_a_later_run(tmp_path: Path) -> None:
    thresholds_path = _thresholds_path(tmp_path)
    data_root_path = tmp_path / "data-root"

    outlets_first = _write_config(tmp_path, {"theverge": FIXTURES / "theverge.xml"})
    argv_first = [
        "ingest",
        "--outlets",
        str(outlets_first),
        "--thresholds",
        str(thresholds_path),
        "--data-root",
        str(data_root_path),
    ]
    main(argv_first)

    root = DataRoot(data_root_path)
    ids_after_first_run = {item.id for item in _read_all(root)}
    assert ids_after_first_run

    # A second outlet joins the registry, as a real config edit or a
    # feed publishing more items would look like -- a superset run.
    outlets_second = _write_config(
        tmp_path,
        {
            "theverge": FIXTURES / "theverge.xml",
            "arstechnica": FIXTURES / "arstechnica.xml",
        },
        name="outlets2.yaml",
    )
    argv_second = [
        "ingest",
        "--outlets",
        str(outlets_second),
        "--thresholds",
        str(thresholds_path),
        "--data-root",
        str(data_root_path),
    ]
    main(argv_second)

    ids_after_second_run = {item.id for item in _read_all(root)}
    assert ids_after_second_run > ids_after_first_run  # strictly grew
    assert ids_after_first_run <= ids_after_second_run  # old ids untouched


def _read_all(root: DataRoot) -> list[Item]:
    return list(read_items(root))


def test_ingest_uses_nc_data_root_env_var_when_no_flag_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outlets_path = _write_config(tmp_path, {"theverge": FIXTURES / "theverge.xml"})
    thresholds_path = _thresholds_path(tmp_path)
    data_root_path = tmp_path / "env-data-root"
    monkeypatch.setenv("NC_DATA_ROOT", str(data_root_path))

    exit_code = main(
        [
            "ingest",
            "--outlets",
            str(outlets_path),
            "--thresholds",
            str(thresholds_path),
        ]
    )

    assert exit_code == 0
    assert data_root_path.exists()
    assert list(DataRoot(data_root_path).items_dir().glob("*/*/*.jsonl"))
