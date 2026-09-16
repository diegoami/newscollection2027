"""Tests for nc.cli."""

from __future__ import annotations

import json
import subprocess
from importlib import metadata
from pathlib import Path

import pytest

from nc import __version__, embed
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


def test_sync_push_output_strings_are_exact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.github/workflows/ingest.yml` compares this output for equality.

    It fires the `data-updated` repository_dispatch only when stdout is
    exactly "sync push: pushed". Rewording the line would not fail
    anything at runtime -- the dispatch would simply stop firing and the
    site would quietly stop republishing -- so the contract is pinned
    here. Change these strings and the workflow must change with them.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    config_path = tmp_path / "sync.yaml"
    config_path.write_text(f"repo_url: {remote}\nbranch: main\n")
    data_root = tmp_path / "data-root"
    argv = ["--data-root", str(data_root), "--config", str(config_path)]

    assert main(["sync", "pull", *argv]) == 0
    capsys.readouterr()

    (data_root / "hello.txt").write_text("hi\n")
    assert main(["sync", "push", *argv, "--message", "data: test"]) == 0
    assert capsys.readouterr().out.strip() == "sync push: pushed"

    assert main(["sync", "push", *argv, "--message", "data: test"]) == 0
    assert capsys.readouterr().out.strip() == "sync push: nothing changed"


# --- T20: nc embed -----------------------------------------------------
#
# `Model2VecBackend` (the real model) needs Hugging Face egress this
# sandbox does not have, so these tests monkeypatch `nc.cli.embed.
# Model2VecBackend` -- the one place production code chooses a backend
# (see `nc.cli._embed`) -- with a fake that wraps the network-free
# `HashBackend`. That proves the CLI wiring (arg parsing, exit code,
# the printed summary, --data-root/--config/--db plumbing) without
# touching the real model; nc/test_embed.py covers the storage logic
# this delegates to, and .github/workflows/embed-check.yml covers the
# real model's determinism.


class _FakeModel2VecBackend:
    def __init__(self, config: embed.EmbedConfig) -> None:
        self.config = config
        self._backend = embed.HashBackend()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._backend.embed(texts)


def _write_embed_config(tmp_path: Path) -> Path:
    path = tmp_path / "embed.yaml"
    path.write_text(
        f"model_id: fake-model\nmodel_dir: {tmp_path / 'hf-cache'}\nbatch_size: 10\n"
    )
    return path


def test_embed_reports_embedded_and_already_stored_counts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nc.cli.embed.Model2VecBackend", _FakeModel2VecBackend)

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
    ingest_argv = [
        "ingest",
        "--outlets",
        str(outlets_path),
        "--thresholds",
        str(thresholds_path),
        "--data-root",
        str(data_root),
    ]
    assert main(ingest_argv) == 0
    capsys.readouterr()

    embed_config_path = _write_embed_config(tmp_path)
    db_path = tmp_path / "cache" / "vectors.sqlite"
    embed_argv = [
        "embed",
        "--data-root",
        str(data_root),
        "--config",
        str(embed_config_path),
        "--db",
        str(db_path),
    ]

    exit_code = main(embed_argv)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "5 item(s) embedded" in out  # healthy.xml has 5 entries
    assert "0 already had a vector" in out

    # Re-running embeds nothing new -- the items already have vectors.
    exit_code_2 = main(embed_argv)
    assert exit_code_2 == 0
    out_2 = capsys.readouterr().out
    assert "0 item(s) embedded" in out_2
    assert "5 already had a vector" in out_2


def test_cluster_emits_a_cluster_and_is_a_no_op_the_second_time(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T21 end to end through the CLI, with no model anywhere.

    `HashBackend` gives identical text an identical vector, so two
    outlets running the same headline and lede land at cosine 1.0 and
    link; the third item is unrelated text and stays a dropped
    singleton.
    """
    from nc.embed import HashBackend, embed_items
    from nc.feeds import Item, utc_now_iso
    from nc.store import DataRoot, append_items

    now = utc_now_iso()
    shared_title = "Chipmaker announces a new accelerator"
    shared_lede = "The company said the part ships in the first quarter."
    items = [
        Item(
            id=f"{index:040x}",
            outlet=outlet,
            url=f"https://{outlet}.example/{index}",
            title=title,
            lede=lede,
            author=None,
            published=now,
            fetched=now,
            tags=(),
        )
        for index, (outlet, title, lede) in enumerate(
            (
                ("theverge", shared_title, shared_lede),
                ("arstechnica", shared_title, shared_lede),
                ("wired", "Something else entirely", "An unrelated lede."),
            )
        )
    ]
    data_root = tmp_path / "data-root"
    append_items(DataRoot(data_root), items)
    db_path = tmp_path / "vectors.sqlite"
    embed_items(DataRoot(data_root), HashBackend(dim=16), "hash", db_path)

    config_path = tmp_path / "cluster.yaml"
    config_path.write_text(
        "tau_low: 0.65\ntau_high: 0.80\nwindow_days: 4\nmin_outlets: 2\n"
    )
    argv = [
        "cluster",
        "--data-root",
        str(data_root),
        "--config",
        str(config_path),
        "--db",
        str(db_path),
    ]

    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "1 new" in out
    assert "dropped 1 singleton component(s)" in out
    assert "wrote 1 cluster file(s), 1 pending file(s)" in out
    written = sorted((data_root / "clusters").rglob("*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    assert {entry["outlet"] for entry in payload["items"]} == {
        "theverge",
        "arstechnica",
    }

    before = {path: path.read_bytes() for path in sorted(data_root.rglob("*.json"))}
    assert main(argv) == 0
    out_2 = capsys.readouterr().out
    assert "0 new" in out_2
    assert "wrote 0 cluster file(s), 0 pending file(s)" in out_2
    assert {path: path.read_bytes() for path in sorted(data_root.rglob("*.json"))} == (
        before
    )
