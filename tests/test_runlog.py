"""Tests for T50's `nc.runlog` and `nc.nightly`.

Two properties carry most of the weight here, because both are things a
run record could plausibly get wrong in a way nobody would notice for
weeks:

- **The counts are recomputed, never reported.** A record that believed
  the agent would be a record of what the agent believed, and the one
  night that matters is the night it was wrong. So the tests write files
  and check the record found them, and one test hands `build_run` a note
  claiming a different story and checks the numbers ignore it.
- **A missing duration stays missing.** With no journal open there is no
  honest start time, and `started_at: null` is the right answer where a
  guess from the file's own mtime would look like data.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from nc import runlog
from nc.cluster import STATUS_ANALYZED, STATUS_PENDING, Cluster, ClusterItem, render
from nc.store import DataRoot

NOW = datetime(2026, 9, 18, 4, 30, 0, tzinfo=UTC)
VERGE = "a" * 40
ARS = "b" * 40


def _cluster(cluster_id: str, status: str = STATUS_PENDING) -> Cluster:
    return Cluster(
        id=cluster_id,
        version=1,
        anchor=VERGE,
        status=status,
        items=(
            ClusterItem(
                item_id=VERGE,
                outlet="theverge",
                title="Acme ships the Widget 4",
                lede="The company said the part ships in the first quarter.",
                published="2026-09-18T10:00:00Z",
            ),
            ClusterItem(
                item_id=ARS,
                outlet="arstechnica",
                title="Acme announces Widget 4 for Q1",
                lede="Acme said the Widget 4 will cost $499.",
                published="2026-09-18T11:00:00Z",
            ),
        ),
    )


def _write_cluster(data_root: DataRoot, cluster: Cluster) -> None:
    path = data_root.resolve("clusters", cluster.date, f"{cluster.id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(cluster), encoding="utf-8")


def _touch(path: Path, text: str = "{}\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- the counts -----------------------------------------------------------


def test_the_counts_are_read_off_the_disk(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    _write_cluster(data_root, _cluster("2026-09-18-aaaaaa"))
    _write_cluster(data_root, _cluster("2026-09-18-bbbbbb", STATUS_ANALYZED))
    _touch(data_root.resolve("analyses", "2026-09-18", "2026-09-18-bbbbbb.json"))
    _touch(data_root.resolve("analyses", "2026-09-17", "2026-09-17-cccccc.json"))
    _touch(data_root.resolve("rejected", "2026-09-18-dddddd.json"))

    counts = runlog.count(data_root, "2026-09-18")

    # No `since`, so how many were written *this run* is unknown -- see
    # test_analyses_are_counted_by_when_they_were_written.
    assert counts.analyses_written is None
    assert counts.analyses_total == 2
    assert counts.rejected == 1
    assert counts.pending == 1
    assert counts.clusters == 2


def test_the_note_cannot_move_a_number(tmp_path: Path) -> None:
    """The agent supplies free text and nothing else. A record that took
    its counts from the same place would be a record of what the agent
    believed it did."""
    data_root = DataRoot(tmp_path / "data")
    _write_cluster(data_root, _cluster("2026-09-18-aaaaaa"))

    run = runlog.build_run(
        data_root,
        now=NOW,
        note="analysed all 40 clusters, nothing left pending",
        journal=tmp_path / "journal.json",
    )

    assert run.counts.analyses_written is None
    assert run.counts.analyses_total == 0
    assert run.counts.pending == 1
    assert run.note == "analysed all 40 clusters, nothing left pending"


def test_an_empty_night_is_not_a_failed_one(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    journal = tmp_path / "journal.json"
    runlog.start(journal, now=NOW)
    run = runlog.build_run(data_root, now=NOW, journal=journal)
    assert run.status == runlog.STATUS_EMPTY


def test_a_night_that_left_work_behind_is_not_empty(tmp_path: Path) -> None:
    """`empty` means there was nothing to do. A queue nobody worked is a
    different thing, and the record has to be able to tell them apart --
    it is the shape of a nightly that silently stopped analysing."""
    data_root = DataRoot(tmp_path / "data")
    journal = tmp_path / "journal.json"
    runlog.start(journal, now=NOW)
    _write_cluster(data_root, _cluster("2026-09-18-aaaaaa"))

    run = runlog.build_run(data_root, now=NOW, journal=journal)

    assert run.status == runlog.STATUS_OK
    assert run.counts.pending == 1


def test_failed_is_the_one_thing_the_files_cannot_show(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    run = runlog.build_run(
        data_root,
        now=NOW,
        failed="nc build failed for a reason that is not an analysis",
        journal=tmp_path / "journal.json",
    )
    assert run.status == runlog.STATUS_FAILED
    assert "nc build failed" in run.note


# --- the journal ----------------------------------------------------------


def test_durations_come_from_the_journal(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    journal = tmp_path / "journal.json"
    started = datetime(2026, 9, 18, 4, 0, 0, tzinfo=UTC)
    runlog.start(journal, now=started)
    runlog.record_step("sync pull", 2.0, True, journal)
    runlog.record_step("cluster", 8.0, True, journal)

    run = runlog.build_run(data_root, now=NOW, journal=journal)

    assert run.started_at == "2026-09-18T04:00:00Z"
    assert run.duration_seconds == 1800.0
    assert [step.name for step in run.steps] == ["sync pull", "cluster"]
    # Everything the commands did not account for is the model's time --
    # otherwise invisible, and the number that says whether a nightly is
    # cheap enough to keep running.
    assert run.agent_seconds == 1790.0


def test_no_journal_means_no_duration_rather_than_a_guess(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    run = runlog.build_run(data_root, now=NOW, journal=tmp_path / "nothing.json")
    assert run.started_at is None
    assert run.duration_seconds is None
    assert run.agent_seconds is None
    assert "no start time" in runlog.format_run(run)


def test_start_discards_a_journal_left_behind_by_a_dead_run(tmp_path: Path) -> None:
    """Otherwise tonight's record claims a duration measured from the
    night the runner died."""
    journal = tmp_path / "journal.json"
    runlog.start(journal, now=datetime(2026, 9, 17, 4, 0, 0, tzinfo=UTC))
    runlog.record_step("cluster", 5.0, True, journal)

    runlog.start(journal, now=datetime(2026, 9, 18, 4, 0, 0, tzinfo=UTC))

    payload = json.loads(journal.read_text())
    assert payload["steps"] == []
    assert payload["started_at"] == "2026-09-18T04:00:00Z"


def test_recording_a_step_without_a_journal_does_nothing(tmp_path: Path) -> None:
    """`record_step` runs on every `nc` command on the machine, almost
    always with no journal open. It must be a no-op and must never
    raise: a `nc validate` that crashed because it could not write a
    timing would be a far worse trade than a missing duration."""
    missing = tmp_path / "nope" / "journal.json"
    runlog.record_step("cluster", 1.0, True, missing)
    assert not missing.exists()


def test_a_corrupt_journal_does_not_take_the_command_down(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    journal.write_text("{not json", encoding="utf-8")
    runlog.record_step("cluster", 1.0, True, journal)
    run = runlog.build_run(DataRoot(tmp_path / "data"), now=NOW, journal=journal)
    assert run.duration_seconds is None


def test_a_clock_that_went_backwards_shows_no_agent_time(tmp_path: Path) -> None:
    """Clamped at zero rather than reported negative: a reason to show
    nothing, not to claim the model worked less than no time at all."""
    journal = tmp_path / "journal.json"
    runlog.start(journal, now=NOW)
    runlog.record_step("cluster", 9000.0, True, journal)

    run = runlog.build_run(DataRoot(tmp_path / "data"), now=NOW, journal=journal)

    assert run.agent_seconds == 0.0


# --- the file -------------------------------------------------------------


def test_the_record_round_trips(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    journal = tmp_path / "journal.json"
    runlog.start(journal, now=datetime(2026, 9, 18, 4, 0, 0, tzinfo=UTC))
    runlog.record_step("build", 1.5, True, journal)
    run = runlog.build_run(data_root, now=NOW, note="hello", journal=journal)

    path = runlog.write_run(data_root, run)
    loaded = runlog.load_runs(data_root)

    assert path == data_root.resolve("runs", "2026-09-18.json")
    assert loaded == [run]


def test_writing_is_byte_stable(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    run = runlog.build_run(data_root, now=NOW, journal=tmp_path / "journal.json")
    assert runlog.render_run(run) == runlog.render_run(run)
    assert runlog.render_run(run).endswith("\n")


def test_a_second_run_on_one_date_replaces_the_first(tmp_path: Path) -> None:
    """The record answers "what happened on the 18th", and two answers
    for one date would leave a status page choosing between them."""
    data_root = DataRoot(tmp_path / "data")
    runlog.write_run(
        data_root,
        runlog.build_run(data_root, now=NOW, journal=tmp_path / "j.json"),
    )
    _write_cluster(data_root, _cluster("2026-09-18-aaaaaa"))
    runlog.write_run(
        data_root,
        runlog.build_run(data_root, now=NOW, journal=tmp_path / "j.json"),
    )

    runs = runlog.load_runs(data_root)
    assert len(runs) == 1
    assert runs[0].counts.pending == 1


def test_runs_load_newest_first(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    for day in (16, 17, 18):
        moment = datetime(2026, 9, day, 4, 0, 0, tzinfo=UTC)
        runlog.write_run(
            data_root,
            runlog.build_run(data_root, now=moment, journal=tmp_path / "j.json"),
        )
    assert [run.date for run in runlog.load_runs(data_root)] == [
        "2026-09-18",
        "2026-09-17",
        "2026-09-16",
    ]


def test_one_corrupt_record_does_not_hide_the_others(tmp_path: Path) -> None:
    """The status page exists to report the state of the pipeline. One
    bad file must not be able to take the whole page down."""
    data_root = DataRoot(tmp_path / "data")
    runlog.write_run(
        data_root,
        runlog.build_run(data_root, now=NOW, journal=tmp_path / "j.json"),
    )
    _touch(data_root.resolve("runs", "2026-09-17.json"), "{not json")

    assert [run.date for run in runlog.load_runs(data_root)] == ["2026-09-18"]


def test_a_record_from_an_older_version_still_loads(tmp_path: Path) -> None:
    """A field this version does not know is ignored and a field it
    expects falls back to its default, so a status page never crashes on
    a file an older nightly wrote."""
    data_root = DataRoot(tmp_path / "data")
    _touch(
        data_root.resolve("runs", "2026-09-18.json"),
        json.dumps(
            {
                "date": "2026-09-18",
                "finished_at": "2026-09-18T04:30:00Z",
                "status": "ok",
                "counts": {"analyses_today": 3, "clusters": 7},
            }
        ),
    )

    runs = runlog.load_runs(data_root)

    assert len(runs) == 1
    # `analyses_today` was this field's name before the first real run
    # showed it counted the wrong directory. An old record still loads;
    # the field it no longer has falls back to "unknown".
    assert runs[0].counts.clusters == 7
    assert runs[0].counts.analyses_written is None


# --- how a duration reads -------------------------------------------------


def test_durations_read_as_a_person_would_say_them() -> None:
    assert runlog.format_duration(0.04) == "<1s"
    assert runlog.format_duration(9.0) == "9s"
    assert runlog.format_duration(612.0) == "10m 12s"
    assert runlog.format_duration(3725.0) == "1h 02m"


def test_an_unknown_duration_is_not_zero() -> None:
    """A run that kept no journal has an unknown duration. Zero is a
    claim, and the wrong one."""
    assert runlog.format_duration(None) == "—"
    assert runlog.format_duration(0.0) == "<1s"


def test_an_empty_night_is_still_a_successful_one() -> None:
    """Most nights look like this once the backlog is clear. Treating
    `empty` as failure would light the status page up every quiet night.
    """
    runs = [
        runlog.Run(date="2026-09-18", finished_at="x", status=runlog.STATUS_EMPTY),
        runlog.Run(date="2026-09-17", finished_at="y", status=runlog.STATUS_OK),
    ]
    found = runlog.last_successful(runs)
    assert found is not None and found.date == "2026-09-18"


def test_a_failed_run_is_skipped_for_the_last_successful_one() -> None:
    runs = [
        runlog.Run(date="2026-09-18", finished_at="x", status=runlog.STATUS_FAILED),
        runlog.Run(date="2026-09-17", finished_at="y", status=runlog.STATUS_OK),
    ]
    found = runlog.last_successful(runs)
    assert found is not None and found.date == "2026-09-17"
    assert runlog.last_successful([]) is None


# --- the bug the first real run found -------------------------------------


def test_analyses_are_counted_by_when_they_were_written(tmp_path: Path) -> None:
    """Not by the directory they are filed under.

    An analysis is filed under its *cluster's* date, which is the date of
    the story's earliest item, so a night that analyses a three-day-old
    cluster writes nothing under today's date. Counting by directory
    reported the first real nightly -- nineteen analyses -- as having
    produced none, and the record called that night `empty`.
    """
    data_root = DataRoot(tmp_path / "data")
    journal = tmp_path / "journal.json"
    # An analysis from an earlier night, filed under an older date.
    old = data_root.resolve("analyses", "2026-09-14", "2026-09-14-aaaaaa.json")
    _touch(old)
    os.utime(old, (1000.0, 1000.0))

    runlog.start(journal, now=NOW)
    # Tonight's work, also filed under an older date because the cluster
    # is older than the run.
    fresh = data_root.resolve("analyses", "2026-09-16", "2026-09-16-bbbbbb.json")
    _touch(fresh)
    os.utime(fresh, (NOW.timestamp() + 10, NOW.timestamp() + 10))

    run = runlog.build_run(data_root, now=NOW, journal=journal)

    assert run.counts.analyses_written == 1
    assert run.counts.analyses_total == 2
    assert run.status == runlog.STATUS_OK


def test_without_a_journal_the_count_is_unknown_not_zero(tmp_path: Path) -> None:
    """With no start time there is no way to tell this run's files from
    last week's, and zero is a claim. A night cannot be called empty on
    no evidence either."""
    data_root = DataRoot(tmp_path / "data")
    _touch(data_root.resolve("analyses", "2026-09-16", "2026-09-16-bbbbbb.json"))

    run = runlog.build_run(data_root, now=NOW, journal=tmp_path / "none.json")

    assert run.counts.analyses_written is None
    assert run.status == runlog.STATUS_OK
    assert "? analysis file(s)" in runlog.format_run(run)


def test_a_record_with_durations_is_not_replaced_by_one_without(tmp_path: Path) -> None:
    """A `nc runlog` typed by hand, outside a journalled session, knows
    no start time and no steps. Letting it overwrite a real nightly's
    record destroys the only measurements of that night, silently --
    which happened on 2026-09-18 and was recoverable only because the
    data repo had committed the file."""
    data_root = DataRoot(tmp_path / "data")
    journal = tmp_path / "journal.json"
    runlog.start(journal, now=NOW)
    runlog.record_step("cluster", 12.0, True, journal)
    real = runlog.build_run(data_root, now=NOW, journal=journal)
    assert runlog.write_run(data_root, real) is not None

    bare = runlog.build_run(data_root, now=NOW, journal=tmp_path / "none.json")
    assert bare.duration_seconds is None

    assert runlog.write_run(data_root, bare) is None
    kept = runlog.load_runs(data_root)[0]
    assert kept.duration_seconds == real.duration_seconds
    assert [s.name for s in kept.steps] == ["cluster"]


def test_a_second_journalled_run_still_replaces_the_first(tmp_path: Path) -> None:
    """The guard is about losing measurements, not about freezing the
    day: a real second run of the day is still the day's answer."""
    data_root = DataRoot(tmp_path / "data")
    first = tmp_path / "a.json"
    runlog.start(first, now=NOW)
    runlog.record_step("cluster", 1.0, True, first)
    runlog.write_run(data_root, runlog.build_run(data_root, now=NOW, journal=first))

    second = tmp_path / "b.json"
    runlog.start(second, now=NOW)
    runlog.record_step("build", 99.0, True, second)
    assert (
        runlog.write_run(
            data_root, runlog.build_run(data_root, now=NOW, journal=second)
        )
        is not None
    )

    assert [s.name for s in runlog.load_runs(data_root)[0].steps] == ["build"]
