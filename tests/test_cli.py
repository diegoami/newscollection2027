"""Tests for nc.cli."""

from __future__ import annotations

import json
import subprocess
from importlib import metadata
from pathlib import Path

import pytest

from nc import __version__
from nc.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def test_version_returns_zero_and_prints_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--version"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    assert "usage" in capsys.readouterr().out


def test_no_args_returns_zero_and_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main([])

    assert exit_code == 0
    assert "usage" in capsys.readouterr().out


def test_unknown_command_does_not_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])

    assert excinfo.value.code != 0
    assert excinfo.value.code == 2


def test_version_matches_package_metadata() -> None:
    """__version__ and the version in pyproject.toml must not drift apart."""
    assert metadata.version("newscollection2027") == __version__


def _write_local_config(tmp_path: Path) -> tuple[Path, Path]:
    outlets_path = tmp_path / "outlets.yaml"
    outlets_path.write_text(
        "outlets:\n"
        "  - slug: healthy\n"
        "    display_name: Healthy\n"
        "    homepage: https://example.com/\n"
        f"    feed_url: {FIXTURES / 'healthy.xml'}\n"
        "  - slug: empty\n"
        "    display_name: Empty\n"
        "    homepage: https://example.com/\n"
        f"    feed_url: {FIXTURES / 'empty.xml'}\n"
    )
    thresholds_path = tmp_path / "feeds.yaml"
    thresholds_path.write_text(
        "user_agent: test-agent\n"
        "lede_min_median_words: 8\n"
        "lede_max_equal_title_share: 0.5\n"
        "stale_after_hours: 1000000\n"
    )
    return outlets_path, thresholds_path


def test_feeds_check_reports_and_fails_on_a_bad_feed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outlets_path, thresholds_path = _write_local_config(tmp_path)

    exit_code = main(
        [
            "feeds",
            "check",
            "--outlets",
            str(outlets_path),
            "--thresholds",
            str(thresholds_path),
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 1  # the empty feed fails
    assert "healthy" in out
    assert "empty" in out
    assert "1/2 feeds ok" in out


def test_feeds_check_json_output_is_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outlets_path, thresholds_path = _write_local_config(tmp_path)

    main(
        [
            "feeds",
            "check",
            "--outlets",
            str(outlets_path),
            "--thresholds",
            str(thresholds_path),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert {row["slug"] for row in payload} == {"healthy", "empty"}
    healthy = next(row for row in payload if row["slug"] == "healthy")
    assert healthy["ok"] is True
    assert healthy["measurement"]["entries"] == 5


# --- T12: nc ingest / nc db rebuild / nc sync pull|push --------------------


def test_db_rebuild_reports_the_item_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outlets_path = tmp_path / "outlets.yaml"
    outlets_path.write_text(
        "outlets:\n"
        "  - slug: healthy\n"
        "    display_name: Healthy\n"
        "    homepage: https://example.com/\n"
        f"    feed_url: {FIXTURES / 'healthy.xml'}\n"
    )
    thresholds_path = tmp_path / "feeds.yaml"
    thresholds_path.write_text(
        "user_agent: test-agent\n"
        "lede_min_median_words: 8\n"
        "lede_max_equal_title_share: 0.5\n"
        "stale_after_hours: 1000000\n"
        "lede_word_cap: 60\n"
    )
    data_root = tmp_path / "data-root"
    db_path = tmp_path / "cache" / "nc.sqlite"

    exit_code = main(
        [
            "ingest",
            "--outlets",
            str(outlets_path),
            "--thresholds",
            str(thresholds_path),
            "--data-root",
            str(data_root),
        ]
    )
    assert exit_code == 0
    capsys.readouterr()

    exit_code = main(
        [
            "db",
            "rebuild",
            "--data-root",
            str(data_root),
            "--db",
            str(db_path),
        ]
    )

    assert exit_code == 0
    assert db_path.exists()
    out = capsys.readouterr().out
    assert "5 item(s)" in out  # healthy.xml has 5 entries


def test_sync_pull_then_push_against_a_local_bare_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    config_path = tmp_path / "sync.yaml"
    config_path.write_text(f"repo_url: {remote}\nbranch: main\n")
    data_root = tmp_path / "data-root"

    exit_code = main(
        [
            "sync",
            "pull",
            "--data-root",
            str(data_root),
            "--config",
            str(config_path),
        ]
    )
    assert exit_code == 0
    assert (data_root / ".git").exists()

    (data_root / "hello.txt").write_text("hi\n")

    exit_code = main(
        [
            "sync",
            "push",
            "--data-root",
            str(data_root),
            "--config",
            str(config_path),
            "--message",
            "test push",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "pushed" in out

    log = subprocess.run(
        ["git", "-C", str(data_root), "log", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "data: test push"
