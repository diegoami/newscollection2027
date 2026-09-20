"""T50: `nc runlog`, the record of what a nightly run actually did.

One file per run, `runs/<date>.json` in the data root, written at the
end of the nightly by `nc runlog`. T43's status page reads them; T52
reads the newest one's timestamp to say how long it has been since a
run finished.

**Everything here is derived from files on disk, not reported by the
agent.** A run record whose numbers came from the agent's own account of
its night would be a record of what it believed, and the one night that
matters is the night it was wrong. So the counts are recomputed from the
data root -- how many analyses exist for today, how many rejects sit in
`rejected/`, how many clusters still wait -- and the only fields an
agent supplies are a free-text note and, if it stopped deliberately, why.

**The journal.** Durations cannot be derived after the fact, so
`nc runlog --start` opens `.cache/run-journal.json` and every `nc`
command that runs while it is open appends its own name and elapsed
seconds to it (see `nc.cli.main`). The agent times nothing and reports
nothing; it just runs the commands it was going to run anyway. Two
things fall out of that:

- Per-command durations are real measurements, and a nightly that got
  slow says which step got slow.
- Total wall clock minus the sum of the commands is the time the model
  itself spent, which is otherwise invisible. It is reported as
  `agent_seconds`, and it is the number that says whether a nightly is
  cheap enough to keep running every night.

When no journal is open -- someone ran `nc runlog` by hand, or `.cache/`
was wiped mid-run -- there is no start time, and the record says
`started_at: null` and `duration_seconds: null` rather than inventing
one from the file's own mtime.

The journal lives under `.cache/` with the SQLite files, per CLAUDE.md:
it is derived state about a run in progress, not pipeline data, and it
must never reach the data repository. The record itself is data and does
go there, which is why `nc nightly --dry-run` computes one and prints it
without writing: a dry run that left a `runs/` file behind would be
pushed by the next real run as if a nightly had happened.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from nc.cluster import load_clusters, pending_clusters
from nc.contract import analyses_dir, rejected_dir
from nc.judge import judgments_dir, unjudged_pairs
from nc.store import DataRoot, write_text

# Beside `.cache/nc.sqlite` and `.cache/validate-state.json`: derived
# state about a run, never committed.
DEFAULT_JOURNAL_PATH = Path(".cache/run-journal.json")

# Overridable the same way the data root is (`nc.store.ENV_VAR`), and
# for the same reason: every command has to agree on where the journal
# is without any of them growing a flag for it. The nightly never sets
# it; tests and a second concurrent run do.
ENV_VAR = "NC_RUN_JOURNAL"


def journal_path(env: Mapping[str, str] | None = None) -> Path:
    mapping = os.environ if env is None else env
    raw = mapping.get(ENV_VAR)
    return Path(raw) if raw else DEFAULT_JOURNAL_PATH


STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"


def runs_dir(data_root: DataRoot) -> Path:
    return data_root.resolve("runs")


def run_path(data_root: DataRoot, date: str) -> Path:
    return runs_dir(data_root) / f"{date}.json"


@dataclass(frozen=True)
class Step:
    """One `nc` command that ran during the night."""

    name: str
    seconds: float
    ok: bool


@dataclass(frozen=True)
class RunCounts:
    """What the data root holds when the run ends.

    `analyses_written` is the night's output; `analyses_total` is the
    standing state. `pending` is what is still waiting -- a run can
    finish `ok` and leave work behind, and a status page that showed
    only the first number would call that a clean night.

    `analyses_written` is `None` when the run kept no journal, because
    without a start time there is no way to tell this run's files from
    last week's, and zero is a claim.
    """

    analyses_written: int | None = None
    analyses_total: int = 0
    rejected: int = 0
    pending: int = 0
    clusters: int = 0
    judgments: int = 0
    unjudged_pairs: int = 0


@dataclass(frozen=True)
class Run:
    date: str
    finished_at: str
    status: str
    counts: RunCounts = field(default_factory=RunCounts)
    started_at: str | None = None
    duration_seconds: float | None = None
    agent_seconds: float | None = None
    steps: tuple[Step, ...] = ()
    note: str = ""


# --- the journal ----------------------------------------------------------


def start(path: Path | None = None, now: datetime | None = None) -> datetime:
    """Open a journal, discarding any older one.

    Discarding rather than appending is deliberate: a journal left
    behind by a run that died would otherwise make tonight's record
    claim a duration measured from last night.
    """
    path = journal_path() if path is None else path
    moment = datetime.now(UTC) if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(
        path,
        json.dumps(
            {
                "started_at": iso(moment),
                "started_epoch": moment.timestamp(),
                "steps": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return moment


def _read_journal(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def record_step(name: str, seconds: float, ok: bool, path: Path | None = None) -> None:
    """Append one command to an open journal; do nothing if none is open.

    Called from `nc.cli.main` around every command, so it runs on the
    ordinary path of every `nc` invocation on the machine. It therefore
    must never raise and must never fail a command: a broken journal is
    a missing duration on a status page, and a `nc validate` that
    crashed because it could not write a timing would be a far worse
    trade.
    """
    try:
        path = journal_path() if path is None else path
        journal = _read_journal(path)
        if journal is None:
            return
        steps = journal.get("steps")
        if not isinstance(steps, list):
            steps = []
        steps.append({"name": name, "seconds": round(seconds, 3), "ok": ok})
        journal["steps"] = steps
        write_text(path, json.dumps(journal, indent=2, sort_keys=True) + "\n")
    except OSError:
        return


def clear(path: Path | None = None) -> None:
    (journal_path() if path is None else path).unlink(missing_ok=True)


# --- the record -----------------------------------------------------------


def _steps_from(raw: object) -> tuple[Step, ...]:
    """Parse a journal's or a record's `steps` list, skipping anything
    malformed. Same reason as `load_runs`: a bad entry costs a line on a
    report, and must not cost the report."""
    if not isinstance(raw, list):
        return ()
    steps: list[Step] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if not {"name", "seconds", "ok"} <= set(entry):
            continue
        try:
            steps.append(
                Step(
                    name=str(entry["name"]),
                    seconds=float(entry["seconds"]),
                    ok=bool(entry["ok"]),
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(steps)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count_json(directory: Path) -> int:
    return sum(1 for _ in directory.rglob("*.json")) if directory.exists() else 0


def _written_since(data_root: DataRoot, since: float | None) -> int | None:
    """Analyses written during this run, by file mtime.

    Not "analyses filed under today's date": an analysis is filed under
    its *cluster's* date, which is the date of the story's earliest item
    (`.claude/skills/analyze-clusters/SKILL.md`), so a night that
    analysed a three-day-old cluster writes nothing under today. Counting
    by directory reported a night that produced nineteen analyses as
    having produced none, and the record called it `empty`. Found on the
    first real run; mtime is the same signal `nc validate --new` uses.
    """
    if since is None:
        return None
    directory = analyses_dir(data_root)
    if not directory.exists():
        return 0
    return sum(1 for path in directory.rglob("*.json") if path.stat().st_mtime >= since)


def count(data_root: DataRoot, since: float | None = None) -> RunCounts:
    """Recompute every number from the data root. See the module
    docstring: nothing here is taken on the agent's word."""
    return RunCounts(
        analyses_written=_written_since(data_root, since),
        analyses_total=_count_json(analyses_dir(data_root)),
        rejected=_count_json(rejected_dir(data_root)),
        pending=len(pending_clusters(data_root)),
        clusters=len(load_clusters(data_root)),
        judgments=_count_json(judgments_dir(data_root)),
        unjudged_pairs=len(unjudged_pairs(data_root)),
    )


def build_run(
    data_root: DataRoot,
    now: datetime | None = None,
    note: str = "",
    failed: str = "",
    journal: Path | None = None,
) -> Run:
    """The record for a run ending now. Writes nothing.

    Separate from `write_run` so `nc nightly --dry-run` can show exactly
    what a real run would file without filing it.

    The status is derived, with one exception. `failed` is the agent
    saying it stopped on purpose -- step 7 of the nightly skill, "stop
    and report" -- which is the one thing the files cannot show, because
    a run that stopped early and a run that had nothing to do leave the
    same data root behind.
    """
    moment = datetime.now(UTC) if now is None else now
    date = iso(moment)[:10]

    entries = _read_journal(journal_path() if journal is None else journal) or {}
    steps = _steps_from(entries.get("steps"))

    started_raw = entries.get("started_at")
    started_at = started_raw if isinstance(started_raw, str) else None
    epoch = entries.get("started_epoch")
    counts = count(data_root, epoch if isinstance(epoch, int | float) else None)
    duration = (
        round(moment.timestamp() - float(epoch), 3)
        if isinstance(epoch, int | float)
        else None
    )
    # Wall clock the commands do not account for. Clamped at zero rather
    # than reported negative: a clock that moved backwards mid-run is a
    # reason to show nothing, not to claim the model worked less than no
    # time at all.
    agent_seconds = (
        round(max(duration - sum(step.seconds for step in steps), 0.0), 3)
        if duration is not None
        else None
    )

    if failed:
        status = STATUS_FAILED
    elif counts.analyses_written == 0 and counts.pending == 0:
        status = STATUS_EMPTY
    else:
        # Includes the unknown case: with no journal, `analyses_written`
        # is None and a night cannot be called empty on no evidence.
        status = STATUS_OK

    return Run(
        date=date,
        finished_at=iso(moment),
        status=status,
        counts=counts,
        started_at=started_at,
        duration_seconds=duration,
        agent_seconds=agent_seconds,
        steps=steps,
        note=failed or note,
    )


def render_run(run: Run) -> str:
    """Byte-stable, like every other file this project writes: the run
    record is committed to the data repo, and a re-render that reordered
    keys would be a diff saying nothing."""
    # `asdict` already recurses into the `Step` dataclasses, so
    # re-converting them was a no-op that read as though it were doing
    # something.
    return json.dumps(asdict(run), indent=2, sort_keys=True) + "\n"


def write_run(data_root: DataRoot, run: Run) -> Path | None:
    """One file per date, except when that would lose the day's measurements.

    A second run on the same day overwrites the first, which is the right
    shape for a nightly: the record answers "what happened on the 18th",
    and two answers for one date would leave a status page choosing
    between them.

    But a `nc runlog` typed by hand, outside a journalled session, knows
    no start time and no steps -- and overwriting a real nightly's record
    with it destroys the only measurements of that night, silently. That
    happened on 2026-09-18 and the record was recoverable only because
    the data repo had committed it. So a record that knows its duration
    is never replaced by one that does not; the caller is told nothing
    was written by the `None` return, and the fuller record stands.
    """
    path = run_path(data_root, run.date)
    if run.duration_seconds is None and path.exists():
        try:
            existing = run_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            existing = None
        if existing is not None and existing.duration_seconds is not None:
            return None
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, render_run(run))
    return path


def run_from_dict(payload: dict[str, object]) -> Run:
    """The inverse of `render_run`, tolerant of a record written by an
    older version: an unknown key is ignored and a missing one falls
    back to the dataclass default, so a status page never crashes on a
    file it did not write."""
    raw_counts = payload.get("counts")
    fields: dict[str, int] = {}
    if isinstance(raw_counts, dict):
        for key, value in raw_counts.items():
            if key in RunCounts.__dataclass_fields__ and isinstance(value, int):
                fields[str(key)] = value
    counts = RunCounts(**fields)  # a null analyses_written falls back to None

    raw_steps = payload.get("steps")
    steps = _steps_from(raw_steps)
    duration = payload.get("duration_seconds")
    agent = payload.get("agent_seconds")
    started = payload.get("started_at")
    return Run(
        date=str(payload["date"]),
        finished_at=str(payload["finished_at"]),
        status=str(payload["status"]),
        counts=counts,
        started_at=started if isinstance(started, str) else None,
        duration_seconds=float(duration) if isinstance(duration, int | float) else None,
        agent_seconds=float(agent) if isinstance(agent, int | float) else None,
        steps=steps,
        note=str(payload.get("note", "")),
    )


def load_runs(data_root: DataRoot) -> list[Run]:
    """Every run on disk, newest first.

    A file that will not parse is skipped rather than raised on: the
    status page exists to report the state of the pipeline, and one
    corrupt record must not be able to take the whole page down.
    """
    runs: list[Run] = []
    for path in sorted(runs_dir(data_root).glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            try:
                runs.append(run_from_dict(payload))
            except (KeyError, TypeError, ValueError):
                continue
    return runs


def format_run(run: Run) -> str:
    lines = [
        f"runlog: {run.date} {run.status} -- "
        f"{'?' if run.counts.analyses_written is None else run.counts.analyses_written}"
        " analysis file(s) written this run, "
        f"{run.counts.pending} cluster(s) still pending, "
        f"{run.counts.rejected} reject(s) on disk"
    ]
    if run.duration_seconds is not None:
        agent = "" if run.agent_seconds is None else f", {run.agent_seconds:.0f}s agent"
        lines.append(f"runlog: {run.duration_seconds:.0f}s total{agent}")
    else:
        lines.append("runlog: no start time (run `nc runlog --start` first)")
    for step in run.steps:
        mark = " " if step.ok else "!"
        lines.append(f"  {mark} {step.name:<20} {step.seconds:>7.2f}s")
    if run.note:
        lines.append(f"runlog: note: {run.note}")
    return "\n".join(lines)


def last_successful(runs: list[Run]) -> Run | None:
    """The newest run that was not a deliberate stop.

    A night with nothing to do is a successful night -- `empty` means the
    pipeline ran and found no work, which is exactly what most nights
    look like once the backlog is clear. Only `failed`, which an agent
    sets by hand with `--failed`, is not success.
    """
    for run in runs:
        if run.status != STATUS_FAILED:
            return run
    return None


def format_duration(seconds: float | None) -> str:
    """`4m 12s`, for a page a person reads. `None` renders as an em dash
    rather than `0s`: a run with no journal has an unknown duration, and
    zero is a claim."""
    if seconds is None:
        return "\u2014"
    # Under a second, "0s" reads as "this step did not happen". It did;
    # it was just fast.
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
