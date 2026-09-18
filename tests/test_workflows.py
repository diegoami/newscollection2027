"""The contracts between the workflow files, pinned.

Two workflows talk to each other through strings that nothing else
checks. `ingest.yml` fires a `repository_dispatch` whose `event_type`
`deploy.yml` listens for; rename it on either side and nothing fails --
no job errors, no red check, no notification. The site simply stops
republishing when the data changes, and the first sign of it is a front
page that is quietly a week old.

tests/test_cli.py pins the other half of the same chain (the exact
stdout `ingest.yml` compares to decide whether to fire at all) for the
identical reason. These tests are cheap; a stale site is not.

They read the YAML rather than grepping it, so a value moved between
`types: [data-updated]` and a block list still matches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(".github/workflows")


def _load(name: str) -> dict[Any, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _triggers(name: str) -> dict[str, Any]:
    """The workflow's `on:` block, which is not filed under `"on"`.

    YAML 1.1, which PyYAML implements, resolves the bare word `on` to the
    boolean True -- the "Norway problem", the same rule that turns `no`
    into False. GitHub Actions reads YAML 1.2, where it stays a string,
    so the workflow file is right and it is the loader that differs.
    """
    block = _load(name)[True]
    assert isinstance(block, dict)
    return block


def test_deploy_listens_for_the_event_ingest_sends() -> None:
    ingest = (WORKFLOWS / "ingest.yml").read_text(encoding="utf-8")
    assert '{"event_type":"data-updated"}' in ingest

    triggers = _triggers("deploy.yml")
    assert triggers["repository_dispatch"]["types"] == ["data-updated"]


def test_deploy_also_runs_on_a_code_change_and_on_demand() -> None:
    """The data is only one of the two inputs: a template or builder
    change renders the same data differently, and the first run ever has
    to be started by hand because `gh-pages` does not exist yet (T03)."""
    triggers = _triggers("deploy.yml")
    assert triggers["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in triggers


def test_deploy_builds_with_nc_build_and_nothing_else() -> None:
    """The site published is the site `nc build` produces. A workflow
    that post-processed the output would make the local build a
    different thing from the deployed one."""
    steps = _load("deploy.yml")["jobs"]["deploy"]["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    assert any(run.strip() == "uv run nc build --out site" for run in runs)


def test_deploy_publishes_only_to_gh_pages() -> None:
    """CLAUDE.md: `main` is PR-only and automation never pushes source.
    The one `git push` in this workflow names `gh-pages` explicitly."""
    steps = _load("deploy.yml")["jobs"]["deploy"]["steps"]
    pushes = [
        line.strip()
        for step in steps
        for line in step.get("run", "").splitlines()
        if line.strip().startswith("git push")
    ]
    assert pushes == ["git push -q origin gh-pages"]


@pytest.mark.parametrize("name", ["deploy.yml", "ingest.yml", "feeds-check.yml"])
def test_no_other_job_is_named_check(name: str) -> None:
    """T03 registers the bare `check` as main's required status check and
    GitHub matches check runs by name, so a second job called `check`
    would let an unrelated failure block every pull request."""
    jobs = _load(name)["jobs"]
    assert "check" not in jobs
