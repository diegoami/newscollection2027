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


# --- T52: the ingest workflow says when it is failing ----------------------


def _report_failure() -> dict[str, Any]:
    job = _load("ingest.yml")["jobs"]["report-failure"]
    assert isinstance(job, dict)
    return job


def test_the_failure_job_runs_only_when_the_ingest_failed() -> None:
    job = _report_failure()
    assert job["needs"] == "ingest"
    assert job["if"] == "failure()"


def test_the_failure_job_can_open_an_issue_and_nothing_else() -> None:
    """Job-level permissions replace the workflow's `contents: write`.
    This job reports; it must never be able to push."""
    assert _report_failure()["permissions"] == {"issues": "write"}


def test_one_issue_per_outage_not_one_per_run() -> None:
    """A cron that fails every three hours would otherwise open
    fifty-six issues in a week. The first failure opens one, the rest
    comment on it, so an outage is one thread with a week of evidence."""
    run = "\n".join(
        step.get("run", "") for step in _report_failure()["steps"] if "run" in step
    )
    assert "gh issue list" in run
    assert "gh issue comment" in run
    assert "gh issue create" in run
    # Found by exact title: a label would have to exist first, and a
    # missing label fails the create.
    assert "select(.title ==" in run


def test_the_forced_failure_is_opt_in_and_stops_before_any_work() -> None:
    """T52's acceptance criterion is "a forced failure creates the
    issue", which needs a way to force one. It is reachable only from a
    manual run, and it is the first step so that forcing a failure
    exercises the reporting path rather than half an ingest.
    """
    triggers = _triggers("ingest.yml")
    assert triggers["workflow_dispatch"]["inputs"]["force_failure"]["default"] is False

    steps = _load("ingest.yml")["jobs"]["ingest"]["steps"]
    assert steps[0]["if"] == "inputs.force_failure"
    assert "exit 1" in steps[0]["run"]
