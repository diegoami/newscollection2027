"""T22: `nc label` and `nc tune`.

docs/PLAN.md T22: "`nc label` shows near-threshold pairs and records yes
or no to `data/labels/pairs.jsonl`; `nc tune` prints precision and
recall per threshold from the labels." Both are here because they share
one file format (`Label`) and one purpose: turning what `nc.cluster`
persisted -- the exhaustive borderline band (`pending-pairs/*.json`,
`nc.cluster.write_pending_pairs`) *and*, since the gap found on #39, a
bounded, stratified sample across the whole usable score range
(`label-sample/*.json`, `nc.cluster.select_label_sample`) -- into
evidence for T23's threshold choice. `labelling_pool` in this module is
what a labelling session actually draws from: the union of both
directories, deduplicated by pair id. See that function's docstring for
why neither directory alone is enough on its own.

**Why this module never touches the embedding model.** The design
question T22's brief poses is where the borderline pairs a human judges
come from. `nc cluster` already computes every pair's score
(`nc.cluster.scan_pairs`); this module's whole job is reading what that
run persisted, so a labelling session opens no model, needs no vectors,
and works offline on a laptop, on the fixture data in
`tests/fixtures/`, or in this sandbox, which has no route to Hugging
Face at all (see `nc.embed`'s module docstring).

**Ordering (`order_for_labelling`).** A human doing 200 of these in one
sitting is T23; sorting by score would spend the first fifty answers on
whichever score happens to be most common, wasting a session that gets
interrupted before it reaches the rest of the band. This module buckets
the labeled score range `[tau_low, tau_high)` into `_NUM_BUCKETS` equal
slices and round-robins across them, each slice highest-score-first --
so the first `_NUM_BUCKETS` answers already span the whole band, and the
band stays covered however early the session stops. Buckets are cut
against `config.tau_low`/`tau_high`, not the min/max score actually
present, so the bucket a pair falls into does not shift as new pairs
accumulate across sessions.

**Labels are not pipeline output.** CLAUDE.md's determinism rule is
about `ingest`/`cluster`/`validate`/`build`; `nc label` is the one place
a human, not code, decides something, and `labels/pairs.jsonl` records
that decision once, append-only, with a timestamp -- unlike a cluster
file, it is never recomputed or rewritten, so it carries no idempotence
obligation of its own.

**`nc tune`'s thresholds are the labeled scores themselves**
(`build_tune_report`), not an arbitrary step size: every threshold that
can change which labeled pairs would auto-link is exactly one of the
observed scores (nothing changes between two consecutive ones), so the
table is exhaustive and every row is backed by real, nameable evidence
rather than an interpolated guess. What the table still cannot show,
even with the labelling corpus: recall against a true match that scored
below `config.sample_floor` -- that pair was never persisted, never
shown to a human, and is invisible to this arithmetic. Before #39 that
blind spot was the historic `tau_low`, and precision *above* `tau_high`
was invisible too (no clear positives were ever persisted at all); the
labelling corpus narrows the blind spot to below `sample_floor` and
makes precision above `tau_high` measurable for the first time.
`docs/CLUSTERING.md` says this again, for the owner sitting down with
the report.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from nc.cluster import ClusterConfig, PendingPair, load_label_sample, load_pending_pairs
from nc.promo import DEFAULT_PROMO_CONFIG_PATH, PromoRules, load_promo_rules
from nc.store import DataRoot, append_line

# nc.cluster._TIME_FORMAT is private to that module; labels are a
# different file with their own timestamp field, so this is its own
# constant rather than a reach into another module's internals.
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# How many equal-width slices of [tau_low, tau_high) `nc label` spreads
# its ordering across -- see the module docstring, "Ordering". Not a
# threshold in the CLAUDE.md sense (it does not decide what links); a
# fixed constant for a display/ordering heuristic, like _BLOCK in
# nc.cluster.
_NUM_BUCKETS = 8

# Below this many labels behind it, a precision or recall number in
# `nc tune`'s table is flagged, not hidden -- T22's brief: "a precision
# computed from 3 labels is noise."
_MIN_RELIABLE = 5


# --- labels -----------------------------------------------------------


@dataclass(frozen=True)
class Label:
    """One human judgment on one pair, with everything `nc tune` needs
    (`score`, `same_story`) and enough to audit later (`outlet_a`,
    `outlet_b`, `labeled_at`) without re-reading the pending-pair file."""

    item_id_a: str
    item_id_b: str
    outlet_a: str
    outlet_b: str
    score: float
    same_story: bool
    labeled_at: str

    @property
    def pair_id(self) -> str:
        """Matches `PendingPair.pair_id`: both are `<lower item id>-<higher
        item id>`, so a label can be matched back to its pair by string
        equality alone."""
        return f"{self.item_id_a}-{self.item_id_b}"

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id_a": self.item_id_a,
            "item_id_b": self.item_id_b,
            "outlet_a": self.outlet_a,
            "outlet_b": self.outlet_b,
            "score": self.score,
            "same_story": self.same_story,
            "labeled_at": self.labeled_at,
        }


def _label_from_dict(raw: dict[str, object]) -> Label:
    return Label(
        item_id_a=str(raw["item_id_a"]),
        item_id_b=str(raw["item_id_b"]),
        outlet_a=str(raw["outlet_a"]),
        outlet_b=str(raw["outlet_b"]),
        score=float(str(raw["score"])),
        same_story=bool(raw["same_story"]),
        labeled_at=str(raw["labeled_at"]),
    )


def label_from_pair(pair: PendingPair, same_story: bool, labeled_at: str) -> Label:
    return Label(
        item_id_a=pair.a.item_id,
        item_id_b=pair.b.item_id,
        outlet_a=pair.a.outlet,
        outlet_b=pair.b.outlet,
        score=pair.score,
        same_story=same_story,
        labeled_at=labeled_at,
    )


def labels_path(data_root: DataRoot) -> Path:
    """docs/PLAN.md T22, verbatim: `data/labels/pairs.jsonl`."""
    return data_root.resolve("labels", "pairs.jsonl")


def load_labels(data_root: DataRoot) -> list[Label]:
    """Every recorded judgment, in file order (append order, i.e. the
    order they were made in)."""
    path = labels_path(data_root)
    if not path.exists():
        return []
    labels: list[Label] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                labels.append(_label_from_dict(json.loads(line)))
    return labels


def append_label(data_root: DataRoot, label: Label) -> None:
    """Append one judgment. Opened in append mode only, one line, so an
    interrupted session (killed mid-write, `nc label` quit) loses at
    most the judgment in progress, never one already recorded -- the
    same append-only shape as `nc.store.append_items`."""
    path = labels_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    append_line(path, json.dumps(label.to_dict(), sort_keys=True, ensure_ascii=True))


# --- importing off the labelling page -----------------------------------


@dataclass(frozen=True)
class ImportRejection:
    """One line of an import file that will not be recorded, and why."""

    line: int
    pair_id: str
    reason: str


@dataclass(frozen=True)
class ImportReport:
    accepted: tuple[Label, ...]
    already: tuple[Label, ...]
    rejected: tuple[ImportRejection, ...]
    written: bool

    @property
    def ok(self) -> bool:
        return not self.rejected


def read_import_file(path: Path) -> list[tuple[int, dict[str, object] | None]]:
    """Each non-blank line as `(line number, object or None)`; `None`
    for a line that is not a JSON object, so the caller can reject it
    by number rather than dying on the first bad byte."""
    rows: list[tuple[int, dict[str, object] | None]] = []
    with path.open("r", encoding="utf-8") as fh:
        for number, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                rows.append((number, None))
                continue
            rows.append((number, parsed if isinstance(parsed, dict) else None))
    return rows


def import_labels(
    data_root: DataRoot,
    path: Path,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Record answers collected off the labelling page, checking every
    one against the pair files `nc cluster` wrote.

    Why this is not just `cat >> labels/pairs.jsonl`. `labels/pairs.jsonl`
    is ground truth: `nc tune` picks `tau_high` from it and
    `nc bench-judge` scores the judge model against it. It is the one
    file in the system an agent must not author, and until now that was
    a rule in a skill -- honoured, unenforced, and worth exactly as
    much as the care of whoever last transcribed a page full of taps.

    So each line is checked against `labelling_pool`, which is
    `pending-pairs/` and `label-sample/` on disk:

    * the pair must exist there -- an id for a pair that was never
      scored cannot have been answered on any page;
    * the claimed score must be the score `nc cluster` computed, to the
      four decimals the page carries -- a label at the wrong score
      moves a `nc tune` threshold while looking entirely normal;
    * the outlets must be that pair's outlets;
    * a pair already in `labels/pairs.jsonl` with the other answer is a
      conflict, not an update: this file is append-only and a human
      changing their mind is a thing to look at, not to overwrite.

    Nothing is written unless every line passes. A partial import of a
    page's answers is the worst outcome available -- it is both
    incomplete and indistinguishable from a complete one -- so a single
    bad line stops the batch and the report names it by line number.

    What survives validation is built with `label_from_pair` from the
    pair on disk, so the recorded score and outlets are the ones
    `nc cluster` computed even where the import agreed with them. The
    only field taken from the import is the answer itself, plus its
    timestamp; those are the only two things a human actually produced.
    """
    pool = {pair.pair_id: pair for pair in labelling_pool(data_root)}
    existing = {label.pair_id: label for label in load_labels(data_root)}

    accepted: list[Label] = []
    already: list[Label] = []
    rejected: list[ImportRejection] = []
    seen: dict[str, bool] = {}

    for number, row in read_import_file(path):
        if row is None:
            rejected.append(ImportRejection(number, "?", "not a JSON object"))
            continue
        pair_id = f"{row.get('item_id_a')}-{row.get('item_id_b')}"
        reject = partial(ImportRejection, number, pair_id)

        answer = row.get("same_story")
        if not isinstance(answer, bool):
            rejected.append(reject("same_story is not true or false"))
            continue
        pair = pool.get(pair_id)
        if pair is None:
            rejected.append(reject("no such pair in pending-pairs/ or label-sample/"))
            continue
        try:
            claimed = float(str(row.get("score")))
        except (TypeError, ValueError):
            rejected.append(reject(f"score {row.get('score')!r} is not a number"))
            continue
        if round(claimed, 4) != round(pair.score, 4):
            rejected.append(
                reject(
                    f"score says {claimed:.4f}, nc cluster computed {pair.score:.4f}"
                )
            )
            continue
        if (row.get("outlet_a"), row.get("outlet_b")) != (pair.a.outlet, pair.b.outlet):
            rejected.append(
                reject(
                    f"outlets say {row.get('outlet_a')}/{row.get('outlet_b')}, "
                    f"the pair is {pair.a.outlet}/{pair.b.outlet}"
                )
            )
            continue
        stamp = row.get("labeled_at")
        if not isinstance(stamp, str) or not _is_timestamp(stamp):
            rejected.append(reject(f"labeled_at {stamp!r} is not a UTC timestamp"))
            continue

        if pair_id in seen:
            if seen[pair_id] != answer:
                rejected.append(reject("the import answers this pair both ways"))
            continue
        seen[pair_id] = answer

        if pair_id in existing:
            if existing[pair_id].same_story != answer:
                rejected.append(
                    reject(
                        f"already labelled {_yesno(existing[pair_id].same_story)}, "
                        f"the import says {_yesno(answer)}"
                    )
                )
            else:
                already.append(existing[pair_id])
            continue

        accepted.append(label_from_pair(pair, answer, stamp))

    write = bool(accepted) and not rejected and not dry_run
    if write:
        for label in accepted:
            append_label(data_root, label)
    return ImportReport(
        accepted=tuple(accepted),
        already=tuple(already),
        rejected=tuple(rejected),
        written=write,
    )


def _yesno(same_story: bool) -> str:
    return "same story" if same_story else "not the same story"


def _is_timestamp(value: str) -> bool:
    try:
        datetime.strptime(value, _ISO_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return False
    return True


def format_import_report(report: ImportReport) -> str:
    """What the owner reads before deciding, and after."""
    lines: list[str] = []
    for rejection in report.rejected:
        lines.append(
            f"  line {rejection.line}: {rejection.reason}  "
            f"[{rejection.pair_id[:16]}...]"
        )
    if lines:
        lines.insert(0, f"{len(report.rejected)} line(s) rejected, nothing written:")
        lines.append("")
    yes = sum(1 for label in report.accepted if label.same_story)
    lines.append(
        f"{len(report.accepted)} new label(s): {yes} same story, "
        f"{len(report.accepted) - yes} not"
    )
    if report.already:
        lines.append(f"{len(report.already)} already recorded, unchanged")
    if report.accepted and not report.rejected:
        lines.append(
            "written to labels/pairs.jsonl"
            if report.written
            else "nothing written (dry run)"
        )
    return "\n".join(lines)


# --- ordering -----------------------------------------------------------


def labelling_pool(
    data_root: DataRoot, promo_rules: PromoRules | None = None
) -> list[PendingPair]:
    """Every pair `nc label` can show: `pending-pairs/` (T24's judge
    queue, exhaustive over `[tau_low, tau_high)`) union `label-sample/`
    (T22's bounded, stratified sample over the wider
    `[sample_floor, 1.0]`), deduplicated by `pair_id`.

    Not just `label-sample/` alone: `sample_bucket_cap` means the
    labelling corpus is *not* guaranteed to hold every pair in the band
    -- a 0.05-wide slice with more borderline pairs than the cap allows
    still has the rest sitting only in `pending-pairs/`. Reading only
    the sample would silently shrink the exhaustive band coverage `nc
    label` has always given, which T23's "label at least 200
    near-threshold pairs" leans on. Not just `pending-pairs/` alone
    either: that directory only ever held the band, which is the whole
    gap this function closes. Reading the union keeps both guarantees:
    every borderline pair remains reachable, and the labelling session
    also sees clear positives and clear negatives it never saw before.

    A pair present in both directories (its score put it in the band,
    and the stratified sample also picked it) is shown once: `pair_id`
    is the same key in both, `nc.cluster.PendingPair`'s definition,
    so a plain dict keyed on it dedupes for free.

    **Two kinds of pair are filtered out here, and only here.** The
    union above is what makes them reachable: `label-sample/` already
    excludes both (`select_label_sample` skips same-outlet pairs, and
    `nc cluster` drops promotional items from the window before any
    pair exists), but `pending-pairs/` is T24's judge queue and keeps
    same-outlet pairs on purpose -- they cannot form a cluster alone
    but they can bridge two components. That is the right rule for a
    machine working a queue and the wrong one for a human working an
    hour, which is exactly why this function, not that directory, is
    where the line goes.

    - *Same-outlet pairs.* A cluster needs two distinct outlets, so
      what a label has to settle is whether two *outlets* covered one
      story. Measured on the pool of 2026-09-17: 92 of 362.
    - *Promotional items and buying advice.* Filtered at read time
      rather than pruned off disk, for the reason `nc.promo` gives
      about ingest: the files are the record and these rules will be
      retuned, so editing `config/promo.yaml` changes the next session
      with no migration. It also catches pair files written before a
      rule existed, which a cluster-time filter alone cannot: 11 of
      that same 362 were still reachable after the rules shipped.
      Only the title rules apply here (`classify_title`) -- a pending
      pair does not carry the items' tags.

    `rules` is injected so a test passes its own rather than depending
    on the shipped config, the same contract `run_clustering` uses.
    """
    rules = (
        load_promo_rules(DEFAULT_PROMO_CONFIG_PATH)
        if promo_rules is None
        else promo_rules
    )
    merged: dict[str, PendingPair] = {}
    for pair in load_pending_pairs(data_root):
        merged[pair.pair_id] = pair
    for pair in load_label_sample(data_root):
        merged.setdefault(pair.pair_id, pair)
    return [
        pair
        for pair_id in sorted(merged)
        if _is_labellable(pair := merged[pair_id], rules)
    ]


def _is_labellable(pair: PendingPair, rules: PromoRules) -> bool:
    if pair.a.outlet == pair.b.outlet:
        return False
    return not (
        rules.classify_title(pair.a.title) or rules.classify_title(pair.b.title)
    )


def unlabeled_pairs(
    pairs: Sequence[PendingPair], labels: Sequence[Label]
) -> list[PendingPair]:
    """Pending pairs with no recorded judgment yet -- what makes
    quitting and resuming `nc label` lose nothing: a pair already in
    `labels/pairs.jsonl` is never shown again."""
    judged = {label.pair_id for label in labels}
    return [pair for pair in pairs if pair.pair_id not in judged]


def order_for_labelling(
    pairs: Sequence[PendingPair],
    tau_low: float,
    tau_high: float,
    num_buckets: int = _NUM_BUCKETS,
) -> list[PendingPair]:
    """Round-robin across `num_buckets` equal slices of
    `[tau_low, tau_high)`, each slice ordered highest-score-first. See
    the module docstring, "Ordering", for why: the first `num_buckets`
    pairs shown already span the whole band, so a session that stops
    early still leaves an evenly-sampled label set behind, not 200
    answers clustered at one score.

    A pair scoring outside `[tau_low, tau_high)` (config retuned since
    it was written, or a floating point edge) clamps into the nearest
    end bucket rather than being dropped -- it is still a pair a human
    can judge, and `nc tune` can still use the label.
    """
    span = tau_high - tau_low

    def bucket_of(score: float) -> int:
        if span <= 0:
            return 0
        index = int((score - tau_low) / span * num_buckets)
        return min(max(index, 0), num_buckets - 1)

    buckets: list[deque[PendingPair]] = [deque() for _ in range(num_buckets)]
    for pair in sorted(pairs, key=lambda pair: (-pair.score, pair.pair_id)):
        buckets[bucket_of(pair.score)].append(pair)

    ordered: list[PendingPair] = []
    while any(buckets):
        for bucket in buckets:
            if bucket:
                ordered.append(bucket.popleft())
    return ordered


# --- nc label -------------------------------------------------------------


@dataclass(frozen=True)
class LabelSessionResult:
    shown: int
    labeled: int
    skipped: int
    quit_early: bool
    remaining: int


_YES = {"y", "yes"}
_NO = {"n", "no"}
_SKIP = {"s", "skip"}
_QUIT = {"q", "quit"}


def _format_pair(pair: PendingPair, index: int, total: int) -> str:
    return "\n".join(
        (
            f"[{index}/{total}] score {pair.score:.4f}",
            f"  A  {pair.a.outlet}",
            f'     "{pair.a.title}"',
            f"     {pair.a.lede}",
            f"  B  {pair.b.outlet}",
            f'     "{pair.b.title}"',
            f"     {pair.b.lede}",
        )
    )


def run_label_session(
    data_root: DataRoot,
    config: ClusterConfig,
    *,
    limit: int | None = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    now_fn: Callable[[], str] = lambda: datetime.now(UTC).strftime(_ISO_FORMAT),
) -> LabelSessionResult:
    """Show unlabeled pairs from `labelling_pool` one at a time; y/n appends to
    `labels/pairs.jsonl` immediately (not buffered -- see
    `append_label`), s skips without recording, q stops. `input_fn` and
    `print_fn` are the whole interactive surface, injected so a scripted
    session can drive this without a real terminal (see
    tests/test_labelling.py).
    """
    pairs = labelling_pool(data_root)
    labels = load_labels(data_root)
    pool = order_for_labelling(
        unlabeled_pairs(pairs, labels), config.tau_low, config.tau_high
    )
    if limit is not None:
        pool = pool[:limit]
    total = len(pool)

    if total == 0:
        print_fn(
            f"label: nothing to label ({len(labels)} pair(s) already "
            "judged, or `nc cluster` has not written any pending-pair or "
            "label-sample files yet)"
        )
        return LabelSessionResult(0, 0, 0, False, 0)

    print_fn(f"label: {total} unlabeled pair(s) ({len(labels)} already judged)")

    shown = 0
    labeled = 0
    skipped = 0
    quit_early = False
    for index, pair in enumerate(pool, start=1):
        shown = index
        print_fn("")
        print_fn(_format_pair(pair, index, total))
        while True:
            try:
                answer = input_fn("same story? [y/n/s=skip/q=quit] ").strip().lower()
            except EOFError:
                answer = "q"
            if answer in _YES or answer in _NO:
                append_label(
                    data_root,
                    label_from_pair(pair, answer in _YES, now_fn()),
                )
                labeled += 1
                break
            if answer in _SKIP:
                skipped += 1
                break
            if answer in _QUIT:
                quit_early = True
                break
            print_fn("  please answer y, n, s or q")
        if quit_early:
            break

    # The pair on screen when the session quits was neither labeled nor
    # skipped, so it (and everything after it in `pool`) is still owed
    # a judgment.
    remaining = total - (shown - 1) if quit_early else total - shown
    print_fn("")
    print_fn(
        f"label: {labeled} labeled, {skipped} skipped, {remaining} left for next time"
    )
    return LabelSessionResult(shown, labeled, skipped, quit_early, remaining)


# --- nc tune ----------------------------------------------------------


@dataclass(frozen=True)
class TuneRow:
    """Precision and recall if `tau_high` were set to `threshold`
    (>= links): every labeled pair splits into true/false positive
    (scored at or above `threshold`) or false negative (scored below
    it, but judged the same story)."""

    threshold: float
    true_positive: int
    false_positive: int
    false_negative: int

    @property
    def n_at_or_above(self) -> int:
        return self.true_positive + self.false_positive

    @property
    def precision(self) -> float | None:
        denom = self.n_at_or_above
        return self.true_positive / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else None


@dataclass(frozen=True)
class TuneReport:
    total_labels: int
    total_positive: int
    total_negative: int
    rows: tuple[TuneRow, ...]


def build_tune_report(labels: Sequence[Label]) -> TuneReport:
    """One row per distinct labeled score, highest first -- see the
    module docstring, "`nc tune`'s thresholds are the labeled scores
    themselves"."""
    total_positive = sum(1 for label in labels if label.same_story)
    total_negative = len(labels) - total_positive

    thresholds = sorted({label.score for label in labels}, reverse=True)
    rows = []
    for threshold in thresholds:
        true_positive = sum(
            1 for label in labels if label.score >= threshold and label.same_story
        )
        false_positive = sum(
            1 for label in labels if label.score >= threshold and not label.same_story
        )
        false_negative = total_positive - true_positive
        rows.append(TuneRow(threshold, true_positive, false_positive, false_negative))

    return TuneReport(len(labels), total_positive, total_negative, tuple(rows))


def run_tune(data_root: DataRoot) -> TuneReport:
    return build_tune_report(load_labels(data_root))


def _format_precision(row: TuneRow) -> str:
    if row.precision is None:
        return "n/a"
    text = f"{row.precision:.3f} ({row.true_positive}/{row.n_at_or_above})"
    return text + "*" if row.n_at_or_above < _MIN_RELIABLE else text


def _format_recall(row: TuneRow, total_positive: int) -> str:
    if row.recall is None:
        return "n/a"
    text = f"{row.recall:.3f} ({row.true_positive}/{total_positive})"
    return text + "*" if total_positive < _MIN_RELIABLE else text


def format_tune_report(report: TuneReport, config: ClusterConfig | None = None) -> str:
    """The `nc tune` output. Decidable means: every number carries the
    count behind it (`_format_precision`/`_format_recall`), a `*` flags
    a count under `_MIN_RELIABLE`, and the false-links column is next to
    precision rather than implied by it, because a false link is the
    error this project cannot afford (see `config/cluster.yaml` and
    `nc.cluster`'s module docstring, "Cost")."""
    lines = [
        f"tune: {report.total_labels} labeled pair(s) "
        f"({report.total_positive} same-story, "
        f"{report.total_negative} different-story)",
    ]
    if config is not None:
        lines.append(
            f"tune: config/cluster.yaml today: tau_low={config.tau_low}, "
            f"tau_high={config.tau_high}"
        )
    if not report.rows:
        lines.append("tune: no labels yet -- run `nc label` first")
        return "\n".join(lines)

    lines.append(
        f"tune: precision/recall marked * are backed by fewer than "
        f"{_MIN_RELIABLE} labels and are noise, not evidence"
    )
    lines.append(
        "tune: recall is out of the labeled same-story pairs only -- a "
        "true match scoring below the band these labels were drawn from "
        "was never shown to a human and is invisible to this table"
    )
    lines.append("")
    lines.append(
        f"{'threshold':>9}  {'n>=t':>5}  {'precision':>18}  {'recall':>18}  "
        f"{'false links':>12}  {'missed links':>13}"
    )
    for row in report.rows:
        lines.append(
            f"{row.threshold:>9.4f}  {row.n_at_or_above:>5}  "
            f"{_format_precision(row):>18}  "
            f"{_format_recall(row, report.total_positive):>18}  "
            f"{row.false_positive:>12}  {row.false_negative:>13}"
        )
    lines.append("")
    lines.append(
        "tune: a false link merges unrelated stories into one page and "
        'reports quoted-looking "discrepancies" between outlets that '
        "never covered the same event -- not recoverable downstream. A "
        "missed link only costs a story an outlet, or drops it as a "
        "single-outlet component nobody sees, and T24's judge exists to "
        "recover some of that. Prefer the highest threshold with zero "
        "false links over one with marginally better recall."
    )
    return "\n".join(lines)
