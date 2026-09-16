"""T12: `nc sync pull|push` -- the only module in `nc` that knows git.

docs/DECISIONS.md: "the code sees only a data root directory; `nc sync`
is the only part that changes" when storage moves from a git checkout
(option B, today) to a synced mirror from object storage (option C,
later). Everywhere else, `nc` touches the data root only through
`nc.store.DataRoot`; the clone/fetch/commit/push plumbing lives here
and nowhere else, per CLAUDE.md's "no git knowledge inside `DataRoot`
itself" boundary.

Commit messages are prefixed `data:` per docs/WORKFLOW.md. `push` only
commits and pushes when `git status` actually shows a change, so a
no-op ingest run (see `nc.store`'s idempotent append) does not produce
an empty commit.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from nc.store import DataRoot

DEFAULT_SYNC_CONFIG_PATH = Path("config/sync.yaml")


@dataclass(frozen=True)
class SyncConfig:
    repo_url: str
    branch: str


def load_sync_config(path: Path = DEFAULT_SYNC_CONFIG_PATH) -> SyncConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return SyncConfig(
        repo_url=str(raw["repo_url"]),
        branch=str(raw.get("branch", "main")),
    )


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def _is_git_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def _set_local_identity(path: Path) -> None:
    # A fresh clone in CI or a sandbox has no global user.* configured,
    # and commits from here are automation's, not a person's, so a
    # fixed local identity is set rather than relying on the
    # environment having one.
    _run(
        [
            "git",
            "-C",
            str(path),
            "config",
            "user.email",
            "nc-bot@newscollection2027.invalid",
        ]
    )
    _run(["git", "-C", str(path), "config", "user.name", "nc bot"])


def pull(data_root: DataRoot, config: SyncConfig) -> None:
    """Clone the data repo into the data root, or fast-forward it.

    A pre-existing checkout is only ever fast-forwarded (`merge
    --ff-only`), never reset, so a local commit that has not been
    pushed yet is never silently discarded.
    """
    path = data_root.path
    if _is_git_checkout(path):
        _run(["git", "-C", str(path), "fetch", "origin", config.branch])
        _run(["git", "-C", str(path), "merge", "--ff-only", f"origin/{config.branch}"])
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", config.repo_url, str(path)])
    _set_local_identity(path)

    current = _run(
        ["git", "-C", str(path), "symbolic-ref", "--short", "-q", "HEAD"], check=False
    )
    if current.stdout.strip() == config.branch:
        return

    # The remote's default branch differs from the configured one, or
    # the clone landed on no branch at all (a genuinely empty repo) --
    # point the checkout at the branch `push` will use, tracking it
    # when it already exists upstream.
    remote_branch = _run(
        ["git", "-C", str(path), "rev-parse", "--verify", f"origin/{config.branch}"],
        check=False,
    )
    if remote_branch.returncode == 0:
        _run(
            [
                "git",
                "-C",
                str(path),
                "checkout",
                "-B",
                config.branch,
                f"origin/{config.branch}",
            ]
        )
    else:
        _run(["git", "-C", str(path), "checkout", "-B", config.branch])


def push(data_root: DataRoot, config: SyncConfig, message: str) -> bool:
    """Commit and push the data root, only if something changed.

    Returns whether a commit was made and pushed.
    """
    path = data_root.path
    if not message.startswith("data:"):
        message = f"data: {message}"

    _run(["git", "-C", str(path), "add", "-A"])
    status = _run(["git", "-C", str(path), "status", "--porcelain"])
    if not status.stdout.strip():
        return False

    _set_local_identity(path)
    _run(["git", "-C", str(path), "commit", "-m", message])
    _run(["git", "-C", str(path), "push", "origin", config.branch])
    return True
