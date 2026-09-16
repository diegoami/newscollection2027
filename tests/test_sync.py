"""Tests for T12's `nc.sync`.

The real data repo (`diegoami/newscollection2027-data`) is not
reachable from this sandbox, so every test here exercises `pull` and
`push` against a local bare git repository instead -- `git init --bare`
stands in for GitHub, and cloning/pushing/fetching against a
`file://`-less local path exercises the same git plumbing with no
network. See docs/PLAN.md T12's "what you cannot do here" note.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nc.store import DataRoot
from nc.sync import SyncConfig, pull, push


def _git(
    *args: str, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    # --initial-branch=main matches this project's default sync branch;
    # a bare repo created this way still has zero commits ("empty main"
    # per docs/PLAN.md T03), which is the case `pull`'s first clone has
    # to handle.
    _git("init", "--bare", "--initial-branch=main", str(remote))
    return remote


def _log(path: Path) -> list[str]:
    # An unborn branch (a freshly cloned, still-empty repo) makes
    # `git log` exit non-zero rather than print nothing.
    result = _git("log", "--format=%s", cwd=path, check=False)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def test_pull_clones_a_still_empty_repo(tmp_path: Path, bare_repo: Path) -> None:
    root = DataRoot(tmp_path / "data")
    config = SyncConfig(repo_url=str(bare_repo), branch="main")

    pull(root, config)

    assert (root.path / ".git").exists()
    assert _log(root.path) == []


def test_push_commits_and_pushes_new_files_with_a_data_prefixed_message(
    tmp_path: Path, bare_repo: Path
) -> None:
    root = DataRoot(tmp_path / "data")
    config = SyncConfig(repo_url=str(bare_repo), branch="main")
    pull(root, config)

    (root.path / "items").mkdir()
    (root.path / "items" / "2026-09-16.jsonl").write_text('{"id": "a"}\n')

    pushed = push(root, config, "ingest 2026-09-16T10:00:00Z")

    assert pushed is True
    messages = _log(root.path)
    assert messages == ["data: ingest 2026-09-16T10:00:00Z"]


def test_push_with_no_changes_does_nothing(tmp_path: Path, bare_repo: Path) -> None:
    root = DataRoot(tmp_path / "data")
    config = SyncConfig(repo_url=str(bare_repo), branch="main")
    pull(root, config)

    (root.path / "a.txt").write_text("hello\n")
    assert push(root, config, "first content") is True

    # Nothing changed since the last push.
    assert push(root, config, "nothing new") is False
    assert _log(root.path) == ["data: first content"]


def test_a_second_checkout_pulls_what_the_first_pushed(
    tmp_path: Path, bare_repo: Path
) -> None:
    config = SyncConfig(repo_url=str(bare_repo), branch="main")

    root_a = DataRoot(tmp_path / "checkout-a")
    pull(root_a, config)
    (root_a.path / "a.txt").write_text("from checkout a\n")
    push(root_a, config, "from a")

    root_b = DataRoot(tmp_path / "checkout-b")
    pull(root_b, config)
    assert (root_b.path / "a.txt").read_text() == "from checkout a\n"


def test_pull_fast_forwards_an_existing_checkout(
    tmp_path: Path, bare_repo: Path
) -> None:
    config = SyncConfig(repo_url=str(bare_repo), branch="main")

    root_a = DataRoot(tmp_path / "checkout-a")
    pull(root_a, config)
    (root_a.path / "a.txt").write_text("first\n")
    push(root_a, config, "first")

    root_b = DataRoot(tmp_path / "checkout-b")
    pull(root_b, config)  # sees the first push

    (root_a.path / "a.txt").write_text("second\n")
    push(root_a, config, "second")

    pull(root_b, config)  # fast-forwards onto the second push
    assert (root_b.path / "a.txt").read_text() == "second\n"
    assert _log(root_b.path)[0] == "data: second"


def test_message_already_prefixed_data_is_not_prefixed_twice(
    tmp_path: Path, bare_repo: Path
) -> None:
    root = DataRoot(tmp_path / "data")
    config = SyncConfig(repo_url=str(bare_repo), branch="main")
    pull(root, config)

    (root.path / "a.txt").write_text("hello\n")
    push(root, config, "data: already prefixed")

    assert _log(root.path) == ["data: already prefixed"]
