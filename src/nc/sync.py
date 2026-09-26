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
        _onto_branch(path, config.branch)
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


def _onto_branch(path: Path, branch: str) -> None:
    """Put an existing checkout on `branch` before fast-forwarding it.

    A cloud session checks the data repo out on a branch of its own
    (`claude/...`), not on `main`. Fast-forwarding that branch and then
    pushing `main` published nothing: the commit sat on the session
    branch, `git push origin main` sent the untouched local `main`, git
    said "Everything up-to-date", and the night's work stayed in the
    sandbox. So the checkout moves to `branch` first -- but only when
    that loses nothing, i.e. when everything on the current branch is
    already on the remote one. Anything else is a checkout with work of
    its own, and that is a person's call, not this function's.
    """
    current = _run(
        ["git", "-C", str(path), "symbolic-ref", "--short", "-q", "HEAD"], check=False
    ).stdout.strip()
    if current == branch:
        return
    upstream = f"origin/{branch}"
    contained = _run(
        ["git", "-C", str(path), "merge-base", "--is-ancestor", "HEAD", upstream],
        check=False,
    )
    if contained.returncode != 0:
        raise RuntimeError(
            f"{path} is on {current or 'a detached HEAD'} with commits that are "
            f"not on {upstream}; refusing to switch to {branch} and leave them "
            "behind"
        )
    _run(["git", "-C", str(path), "checkout", "-B", branch, upstream])


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
    # HEAD, not the local branch of that name: whatever was just
    # committed is what goes to the remote branch, whichever local
    # branch it sits on. A remote that has moved on rejects this as a
    # non-fast-forward, which is loud, and the caller pulls and retries.
    _run(["git", "-C", str(path), "push", "origin", f"HEAD:refs/heads/{config.branch}"])
    return True
