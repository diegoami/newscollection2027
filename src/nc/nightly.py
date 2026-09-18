"""T50: `nc nightly --dry-run`, every step of the night the CLI can do.

The nightly (docs/WORKFLOW.md, "04:00 Europe/Berlin") is a Claude Code
session working through `.claude/skills/nightly/SKILL.md`. Two of its
steps are not commands: judging borderline pairs and analysing pending
clusters are the agent's, and CLAUDE.md is explicit that deterministic
code never calls an LLM. So there is no `nc nightly` that runs the whole
night, and there never will be -- a bare `nc nightly` says so and exits
rather than doing three quarters of the job and calling it a night.

What `--dry-run` is for is the other question: before a Routine runs
unattended at 02:00 UTC with nobody watching, does the deterministic
chain work on this machine, against this data root, with these configs?
It runs every step the CLI owns, in the order the skill runs them, times
each one, and keeps going after a failure so one run reports every
broken step instead of the first.

**"Dry" means nothing is published, not that nothing is written.** The
steps it runs are the real ones: `nc sync pull` fast-forwards the data
root, `nc cluster` writes clusters and pending files, `nc validate`
moves rejects out of `analyses/`. All of that is what the three-hourly
ingest workflow does anyway, it is deterministic and idempotent, and
none of it leaves the machine. What the dry run withholds is the two
things that would be wrong to do unattended: the push, and the run
record -- it computes the record and prints it, because a `runs/` file
left behind by a rehearsal would be pushed by the next real run as if a
nightly had happened.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from nc import cluster, contract, embed, judge, runlog, site, sync
from nc.store import DataRoot

# The two steps a CLI cannot run, named here so the report can say they
# were skipped rather than leave a reader to notice the gap.
AGENT_STEPS = ("judge the borderline pairs", "analyse the pending clusters")


@dataclass(frozen=True)
class StepResult:
    name: str
    seconds: float
    ok: bool
    detail: str


@dataclass(frozen=True)
class DryRunReport:
    steps: tuple[StepResult, ...]
    run: runlog.Run | None

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)


def _time(name: str, work: Callable[[], str]) -> StepResult:
    """Run one step, keeping its failure as data.

    Every exception is caught, deliberately and broadly: this is a
    diagnostic, and a dry run that crashed on step two would hide
    whatever step six was going to say. The exception's text is the
    detail, so nothing is swallowed -- it is reported instead of raised.
    """
    started = time.monotonic()
    try:
        detail = work()
        ok = True
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        detail = f"{type(exc).__name__}: {exc}"
        ok = False
    return StepResult(name, round(time.monotonic() - started, 3), ok, detail)


def dry_run(
    data_root: DataRoot,
    sync_config: Path = sync.DEFAULT_SYNC_CONFIG_PATH,
    cluster_config: Path = cluster.DEFAULT_CLUSTER_CONFIG_PATH,
    db_path: Path = embed.DEFAULT_VECTORS_DB_PATH,
    out_dir: Path = site.DEFAULT_SITE_DIR,
    template_dir: Path = site.DEFAULT_TEMPLATE_DIR,
    validate_state: Path = contract.DEFAULT_VALIDATE_STATE_PATH,
    skip_sync: bool = False,
) -> DryRunReport:
    """The skill's deterministic steps, in the skill's order."""
    steps: list[StepResult] = []

    def _sync_pull() -> str:
        config = sync.load_sync_config(sync_config)
        sync.pull(data_root, config)
        return f"up to date with origin/{config.branch}"

    if skip_sync:
        steps.append(
            StepResult("sync pull", 0.0, True, "skipped (--skip-sync)"),
        )
    else:
        steps.append(_time("sync pull", _sync_pull))

    def _judge_validate() -> str:
        report = judge.validate_all(data_root)
        if report.rejected:
            raise ValueError(
                f"{len(report.rejected)} judgment(s) the validator refuses"
            )
        return f"{report.judged} judgment(s), {report.accepted} would link"

    steps.append(_time("judge --validate", _judge_validate))

    def _cluster() -> str:
        config = cluster.load_cluster_config(cluster_config)
        links = judge.accepted_links(data_root)
        report = cluster.run_clustering(data_root, config, db_path, extra_links=links)
        return (
            f"{report.written.clusters_written} cluster file(s) written, "
            f"{report.pending_pairs_written} pair(s) queued for the judge"
        )

    steps.append(_time("cluster", _cluster))

    # The agent's two steps. Reported, never attempted.
    queue = cluster.pending_clusters(data_root)
    unjudged = judge.unjudged_pairs(data_root)
    steps.append(
        StepResult(
            "judge the pairs",
            0.0,
            True,
            f"SKIPPED (agent step) -- {len(unjudged)} pair(s) would be judged",
        )
    )
    steps.append(
        StepResult(
            "analyse the clusters",
            0.0,
            True,
            f"SKIPPED (agent step) -- {len(queue)} cluster(s) would be analysed",
        )
    )

    def _validate() -> str:
        report = contract.run_validate(data_root, state_path=validate_state)
        if report.rejected:
            raise ValueError(f"{len(report.rejected)} analysis file(s) rejected")
        return f"{report.checked} checked, {report.valid} valid"

    steps.append(_time("validate", _validate))

    def _build() -> str:
        report = site.build_site(data_root, out_dir, template_dir)
        if report.skipped_invalid:
            raise ValueError(
                f"{len(report.skipped_invalid)} analysis file(s) invalid at build time"
            )
        return f"{report.stories} story page(s), {report.pages} file(s) written"

    steps.append(_time("build", _build))

    run: runlog.Run | None = None

    def _runlog() -> str:
        nonlocal run
        run = runlog.build_run(data_root, note="nc nightly --dry-run")
        return f"would write runs/{run.date}.json (not written: this is a dry run)"

    steps.append(_time("runlog", _runlog))

    steps.append(
        StepResult("sync push", 0.0, True, "SKIPPED (a dry run never publishes)")
    )

    return DryRunReport(steps=tuple(steps), run=run)


def format_dry_run(report: DryRunReport) -> str:
    lines = ["nightly --dry-run: every step except the agent's and the push", ""]
    for step in report.steps:
        mark = "ok  " if step.ok else "FAIL"
        lines.append(f"  {mark} {step.name:<22} {step.seconds:>7.2f}s  {step.detail}")
    lines.append("")
    if report.run is not None:
        lines.append(runlog.format_run(report.run))
    failed = [step.name for step in report.steps if not step.ok]
    lines.append("")
    lines.append(
        "nightly --dry-run: all steps passed"
        if not failed
        else f"nightly --dry-run: FAILED at {', '.join(failed)}"
    )
    return "\n".join(lines)
