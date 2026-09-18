"""Tests for T50's `nc nightly --dry-run`.

The dry run is a diagnostic for a Routine that will run unattended, so
the two things worth pinning are what it refuses to do (publish, analyse,
write a run record) and what it does when a step fails (report it and
keep going, rather than hide the six steps after the broken one).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nc import nightly, runlog
from nc.cli import main
from nc.store import DataRoot


def _data_root(tmp_path: Path) -> DataRoot:
    root = DataRoot(tmp_path / "data")
    root.path.mkdir(parents=True, exist_ok=True)
    return root


def _dry_run(tmp_path: Path, **overrides: object) -> nightly.DryRunReport:
    """Every path the dry run writes to, pointed at the test's own
    directory: the real defaults are under `.cache/` in the checkout,
    and a test suite has no business writing there."""
    kwargs: dict[str, object] = {
        "skip_sync": True,
        "db_path": tmp_path / "vectors.sqlite",
        "out_dir": tmp_path / "site",
        "validate_state": tmp_path / "validate-state.json",
    }
    kwargs.update(overrides)
    return nightly.dry_run(_data_root(tmp_path), **kwargs)  # type: ignore[arg-type]


def _names(report: nightly.DryRunReport) -> list[str]:
    return [step.name for step in report.steps]


def test_the_dry_run_covers_every_step_of_the_skill(tmp_path: Path) -> None:
    report = _dry_run(tmp_path)
    assert _names(report) == [
        "sync pull",
        "judge --validate",
        "cluster",
        "judge the pairs",
        "analyse the clusters",
        "validate",
        "build",
        "runlog",
        "sync push",
    ]


def test_the_agent_steps_are_named_as_skipped_not_left_out(tmp_path: Path) -> None:
    """A report with a gap where the analysis should be would read as a
    night that analysed nothing and was fine with it."""
    report = _dry_run(tmp_path)
    skipped = {
        step.name: step.detail for step in report.steps if "SKIPPED" in step.detail
    }
    assert "agent step" in skipped["judge the pairs"]
    assert "agent step" in skipped["analyse the clusters"]
    assert "never publishes" in skipped["sync push"]


def test_the_dry_run_writes_no_run_record(tmp_path: Path) -> None:
    """A `runs/` file left behind by a rehearsal would be pushed by the
    next real run as if a nightly had happened."""
    report = _dry_run(tmp_path)

    assert report.run is not None
    assert not runlog.runs_dir(_data_root(tmp_path)).exists()
    assert "not written" in next(
        step.detail for step in report.steps if step.name == "runlog"
    )


def test_a_failing_step_does_not_hide_the_ones_after_it(tmp_path: Path) -> None:
    report = _dry_run(tmp_path, cluster_config=tmp_path / "missing.yaml")

    failed = [step for step in report.steps if not step.ok]
    assert [step.name for step in failed] == ["cluster"]
    assert not report.ok
    # The steps after the failure still ran and still reported.
    assert report.run is not None
    assert "build" in _names(report)
    assert "nightly --dry-run: FAILED at cluster" in nightly.format_dry_run(report)


def test_skip_sync_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    report = _dry_run(tmp_path)
    step = next(s for s in report.steps if s.name == "sync pull")
    assert step.ok
    assert "skipped" in step.detail


def test_an_empty_data_root_is_a_clean_dry_run(tmp_path: Path) -> None:
    """Nothing to cluster, nothing to validate, nothing to publish is a
    correct night, not a broken one."""
    report = _dry_run(tmp_path)
    assert report.ok
    assert "all steps passed" in nightly.format_dry_run(report)


# --- the CLI --------------------------------------------------------------


def test_nc_nightly_without_dry_run_refuses_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no `nc nightly` that runs the whole night. A command
    that ran everything but the two agent steps and exited 0 would
    report a successful night on which nothing was analysed."""
    code = main(["nightly", "--data-root", str(tmp_path / "data")])
    out = capsys.readouterr().out

    assert code == 2
    assert "never calls an LLM" in out
    assert "SKILL.md" in out


@pytest.fixture
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the run journal somewhere this test owns.

    `NC_RUN_JOURNAL` exists so that every command can agree on where the
    journal is without any of them growing a flag for it -- the agent
    runs a bare `nc cluster`, and it still gets timed.
    """
    path = tmp_path / "journal.json"
    monkeypatch.setenv(runlog.ENV_VAR, str(path))
    return path


def test_nc_runlog_start_then_write(
    tmp_path: Path, journal: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_root = tmp_path / "data"
    argv = ["--data-root", str(data_root)]

    assert main(["runlog", "--start", *argv]) == 0
    assert journal.exists()

    assert main(["runlog", *argv]) == 0
    out = capsys.readouterr().out
    assert "runlog: wrote" in out
    # The journal is closed by the write: a stale one would make
    # tomorrow's record claim a duration measured from tonight.
    assert not journal.exists()
    assert len(runlog.load_runs(DataRoot(data_root))) == 1


def test_nc_runlog_dry_run_writes_nothing(
    tmp_path: Path, journal: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_root = tmp_path / "data"
    code = main(["runlog", "--dry-run", "--data-root", str(data_root)])
    assert code == 0
    assert "would write" in capsys.readouterr().out
    assert not runlog.runs_dir(DataRoot(data_root)).exists()


def test_nc_runlog_failed_exits_non_zero(tmp_path: Path, journal: Path) -> None:
    """So a Routine's own step reports red when the night stopped early,
    without anyone having to read the record."""
    code = main(
        [
            "runlog",
            "--failed",
            "the build broke for a reason that is not an analysis",
            "--data-root",
            str(tmp_path / "data"),
        ]
    )
    assert code == 1


def test_every_command_times_itself_into_an_open_journal(
    tmp_path: Path, journal: Path
) -> None:
    """The agent times nothing and reports nothing: it runs the commands
    it was going to run anyway, and the durations are measurements."""
    data_root = tmp_path / "data"
    argv = ["--data-root", str(data_root)]

    main(["runlog", "--start", *argv])
    main(["pending", *argv])
    main(["runlog", *argv])

    run = runlog.load_runs(DataRoot(data_root))[0]
    assert [step.name for step in run.steps] == ["pending"]
    assert run.steps[0].ok


def test_a_command_that_failed_is_marked_failed_in_the_journal(
    tmp_path: Path, journal: Path
) -> None:
    """A nightly that got slow says which step got slow; a nightly that
    went red says which step went red. Both come off the same journal."""
    data_root = tmp_path / "data"
    argv = ["--data-root", str(data_root)]
    main(["runlog", "--start", *argv])
    # `nc nightly` with no --dry-run exits 2, which is a command that
    # did not succeed.
    main(["nightly", *argv])
    main(["runlog", *argv])

    run = runlog.load_runs(DataRoot(data_root))[0]
    assert [(s.name, s.ok) for s in run.steps] == [("nightly", False)]
