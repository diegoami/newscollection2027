"""Tests for T22's `nc.labelling` (`nc label`, `nc tune`).

No embedding model anywhere, by design (nc/labelling.py's module
docstring): pending pairs here are built directly with `ClusterItem` and
`PendingPair` and written straight to a `DataRoot` with
`nc.cluster.write_pending_pairs`, exactly what `nc cluster` would have
already done. `nc label`'s interactive loop is driven with a scripted
`input_fn`/`print_fn` pair instead of a real terminal, per T22's
constraint ("nc label is interactive; test it with scripted input").
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from nc.cluster import (
    ClusterConfig,
    ClusterItem,
    PendingPair,
    write_label_sample,
    write_pending_pairs,
)
from nc.feeds import Item
from nc.labelling import (
    Label,
    LabelSessionResult,
    TuneReport,
    append_label,
    build_tune_report,
    format_tune_report,
    label_from_pair,
    labelling_pool,
    labels_path,
    load_labels,
    order_for_labelling,
    run_label_session,
    run_tune,
    unlabeled_pairs,
)
from nc.store import DataRoot

CONFIG = ClusterConfig(tau_low=0.65, tau_high=0.80, window_days=4, min_outlets=2)


def _item(seed: str, outlet: str, title: str | None = None) -> Item:
    return Item(
        id=f"item-{seed}",
        outlet=outlet,
        url=f"https://{outlet}.example/{seed}",
        title=title or f"{seed} headline",
        lede=f"{seed} lede sentence.",
        author=None,
        published="2026-09-15T10:00:00Z",
        fetched="2026-09-16T00:00:00Z",
        tags=(),
    )


def _pair(
    seed_a: str, outlet_a: str, seed_b: str, outlet_b: str, score: float
) -> PendingPair:
    return PendingPair(
        a=ClusterItem.from_item(_item(seed_a, outlet_a)),
        b=ClusterItem.from_item(_item(seed_b, outlet_b)),
        score=score,
    )


def _scripted(*answers: str) -> Callable[[str], str]:
    """Feeds `answers` to `run_label_session` one prompt at a time,
    raising `EOFError` once exhausted -- the same signal a real
    terminal gives on Ctrl-D, which `run_label_session` treats as a
    quiet quit."""
    remaining = list(answers)

    def _input(prompt: str) -> str:
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    return _input


def _silent(text: str) -> None:
    pass


# --- Label ------------------------------------------------------------


def test_label_pair_id_matches_pending_pair_id() -> None:
    pair = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    label = label_from_pair(pair, True, "2026-09-16T12:00:00Z")
    assert label.pair_id == pair.pair_id
    assert label.item_id_a == pair.a.item_id
    assert label.item_id_b == pair.b.item_id
    assert label.same_story is True
    assert label.score == pair.score


def test_labels_path_is_the_plan_path(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    assert labels_path(data_root) == data_root.path / "labels" / "pairs.jsonl"


def test_load_labels_is_empty_when_nothing_recorded(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    assert load_labels(data_root) == []


def test_append_label_is_append_only_and_one_line_each(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    pair_a = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    pair_b = _pair("f0", "engadget", "f1", "zdnet", 0.68)

    append_label(data_root, label_from_pair(pair_a, True, "2026-09-16T12:00:00Z"))
    append_label(data_root, label_from_pair(pair_b, False, "2026-09-16T12:01:00Z"))

    text = labels_path(data_root).read_text()
    assert len(text.splitlines()) == 2

    labels = load_labels(data_root)
    assert [label.pair_id for label in labels] == [pair_a.pair_id, pair_b.pair_id]
    assert labels[0].same_story is True
    assert labels[1].same_story is False


def test_append_label_never_rewrites_an_earlier_line(tmp_path: Path) -> None:
    """The append-only guarantee `nc label` leans on: an interrupted
    session loses at most the judgment in progress."""
    data_root = DataRoot(tmp_path / "data")
    pair = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    append_label(data_root, label_from_pair(pair, True, "2026-09-16T12:00:00Z"))
    before = labels_path(data_root).read_text()

    other = _pair("f0", "engadget", "f1", "zdnet", 0.68)
    append_label(data_root, label_from_pair(other, False, "2026-09-16T12:01:00Z"))

    after = labels_path(data_root).read_text()
    assert after.startswith(before)


# --- labelling_pool (T22, gap closed after #39) ------------------------


def test_labelling_pool_is_the_union_of_both_directories(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    band_pair = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    negative = _pair("f0", "engadget", "f1", "zdnet", 0.20)
    write_pending_pairs(data_root, [band_pair])
    write_label_sample(data_root, [negative])

    pool = labelling_pool(data_root)

    assert {pair.pair_id for pair in pool} == {band_pair.pair_id, negative.pair_id}


def test_labelling_pool_dedupes_a_pair_present_in_both_directories(
    tmp_path: Path,
) -> None:
    """A pair in the band can also be picked by the stratified sample
    (the ranges overlap by design); `nc label` must show it once."""
    data_root = DataRoot(tmp_path / "data")
    overlap = _pair("g0", "bbc", "g1", "wired", 0.75)
    write_pending_pairs(data_root, [overlap])
    write_label_sample(data_root, [overlap])

    pool = labelling_pool(data_root)

    assert len(pool) == 1
    assert pool[0].pair_id == overlap.pair_id


def test_labelling_pool_is_empty_when_nothing_written(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    assert labelling_pool(data_root) == []


# --- unlabeled_pairs / order_for_labelling -----------------------------


def test_unlabeled_pairs_excludes_judged_pair_ids() -> None:
    pair_a = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    pair_b = _pair("f0", "engadget", "f1", "zdnet", 0.68)
    label = label_from_pair(pair_a, True, "2026-09-16T12:00:00Z")

    remaining = unlabeled_pairs([pair_a, pair_b], [label])
    assert remaining == [pair_b]


def test_order_for_labelling_spreads_the_first_answers_across_the_band() -> None:
    """T22's brief: "pairs spread across the score range beat 200 pairs
    clustered at 0.66." Eight pairs bunched at one end and one pair each
    at seven other scores -- the first handful shown must still include
    the lonely high-score pair, not the eight low ones first."""
    low_cluster = [
        _pair(f"low{i}", "a", f"low{i}b", "b", 0.66 + i * 0.0001) for i in range(8)
    ]
    spread = [
        _pair(f"hi{i}", "a", f"hi{i}b", "b", score)
        for i, score in enumerate([0.67, 0.69, 0.71, 0.73, 0.75, 0.77, 0.79])
    ]
    pairs = low_cluster + spread

    ordered = order_for_labelling(pairs, tau_low=0.65, tau_high=0.80, num_buckets=8)

    assert len(ordered) == len(pairs)
    assert set(ordered) == set(pairs)
    # The first 8 answers (one per bucket) must include the highest
    # score in the set, not just more of the 0.66 cluster.
    first_scores = {pair.score for pair in ordered[:8]}
    assert max(pair.score for pair in pairs) in first_scores


def test_order_for_labelling_is_deterministic() -> None:
    pairs = [_pair(f"s{i}", "a", f"s{i}b", "b", 0.65 + i * 0.01) for i in range(15)]
    first = order_for_labelling(pairs, 0.65, 0.80)
    second = order_for_labelling(list(reversed(pairs)), 0.65, 0.80)
    assert [pair.pair_id for pair in first] == [pair.pair_id for pair in second]


def test_order_for_labelling_clamps_out_of_band_scores() -> None:
    """A pair scored before a retune, now outside the current
    `[tau_low, tau_high)` -- still labelable, clamped to an end bucket
    rather than dropped."""
    below = _pair("below", "a", "belowb", "b", 0.10)
    above = _pair("above", "a", "aboveb", "b", 0.99)
    ordered = order_for_labelling([below, above], 0.65, 0.80)
    assert set(ordered) == {below, above}


# --- run_label_session --------------------------------------------------


def _write_pairs(data_root: DataRoot, pairs: Iterable[PendingPair]) -> None:
    write_pending_pairs(data_root, pairs)


def test_run_label_session_also_shows_label_sample_only_pairs(
    tmp_path: Path,
) -> None:
    """T22's gap, closed: a pair that never entered the borderline band
    (here, a clear negative at 0.20 -- `pending-pairs/` never held
    scores like this) is still shown, because `run_label_session` draws
    from `labelling_pool`, not from `load_pending_pairs` alone."""
    data_root = DataRoot(tmp_path / "data")
    negative = _pair("z0", "engadget", "z1", "zdnet", 0.20)
    write_label_sample(data_root, [negative])

    result = run_label_session(
        data_root, CONFIG, input_fn=_scripted("n"), print_fn=_silent
    )

    assert result.labeled == 1
    labels = load_labels(data_root)
    assert labels[0].pair_id == negative.pair_id
    assert labels[0].same_story is False


def test_run_label_session_records_yes_and_no(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    pair_a = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    pair_b = _pair("f0", "engadget", "f1", "zdnet", 0.68)
    _write_pairs(data_root, [pair_a, pair_b])

    result = run_label_session(
        data_root,
        CONFIG,
        input_fn=_scripted("y", "n"),
        print_fn=_silent,
        now_fn=lambda: "2026-09-16T12:00:00Z",
    )

    assert result.labeled == 2
    assert result.skipped == 0
    assert result.quit_early is False
    labels = load_labels(data_root)
    assert {label.pair_id for label in labels} == {pair_a.pair_id, pair_b.pair_id}
    assert {label.same_story for label in labels} == {True, False}


def test_run_label_session_accepts_yes_and_no_spellings(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    pair_a = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    pair_b = _pair("f0", "engadget", "f1", "zdnet", 0.68)
    _write_pairs(data_root, [pair_a, pair_b])

    result = run_label_session(
        data_root,
        CONFIG,
        input_fn=_scripted("YES", "No"),
        print_fn=_silent,
    )
    assert result.labeled == 2


def test_run_label_session_reprompts_on_garbage_input(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    pair = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    _write_pairs(data_root, [pair])

    result = run_label_session(
        data_root,
        CONFIG,
        input_fn=_scripted("maybe", "", "y"),
        print_fn=_silent,
    )
    assert result.labeled == 1
    assert load_labels(data_root)[0].same_story is True


def test_run_label_session_skip_does_not_record_a_label(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    pair = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    _write_pairs(data_root, [pair])

    result = run_label_session(
        data_root, CONFIG, input_fn=_scripted("s"), print_fn=_silent
    )
    assert result.skipped == 1
    assert result.labeled == 0
    assert load_labels(data_root) == []


def test_run_label_session_quit_stops_and_saves_nothing_new(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    pairs = [
        _pair("e0", "theverge", "e1", "tomshardware", 0.72),
        _pair("f0", "engadget", "f1", "zdnet", 0.68),
        _pair("g0", "bbc", "g1", "wired", 0.75),
    ]
    _write_pairs(data_root, pairs)

    result = run_label_session(
        data_root, CONFIG, input_fn=_scripted("y", "q"), print_fn=_silent
    )
    assert result.labeled == 1
    assert result.quit_early is True
    assert result.remaining == 2
    assert len(load_labels(data_root)) == 1


def test_run_label_session_eof_is_a_quiet_quit(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    pair = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    _write_pairs(data_root, [pair])

    result = run_label_session(
        data_root, CONFIG, input_fn=_scripted(), print_fn=_silent
    )
    assert result.quit_early is True
    assert result.labeled == 0
    assert load_labels(data_root) == []


def test_run_label_session_never_reshows_a_judged_pair(tmp_path: Path) -> None:
    """Quitting and resuming loses no work and does not re-show judged
    pairs -- T22's brief, verbatim."""
    data_root = DataRoot(tmp_path / "data")
    pair_a = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    pair_b = _pair("f0", "engadget", "f1", "zdnet", 0.68)
    _write_pairs(data_root, [pair_a, pair_b])

    first = run_label_session(
        data_root, CONFIG, input_fn=_scripted("y", "q"), print_fn=_silent
    )
    assert first.labeled == 1

    second = run_label_session(
        data_root, CONFIG, input_fn=_scripted("n"), print_fn=_silent
    )
    assert second.shown == 1  # only the one pair left, not both again
    assert second.labeled == 1
    labels = load_labels(data_root)
    assert len(labels) == 2
    assert {label.pair_id for label in labels} == {pair_a.pair_id, pair_b.pair_id}


def test_run_label_session_with_nothing_to_label(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    result = run_label_session(
        data_root, CONFIG, input_fn=_scripted(), print_fn=_silent
    )
    assert result == LabelSessionResult(0, 0, 0, False, 0)


def test_run_label_session_limit_caps_the_session(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    pairs = [_pair(f"s{i}", "a", f"s{i}b", "b", 0.65 + i * 0.01) for i in range(10)]
    _write_pairs(data_root, pairs)

    result = run_label_session(
        data_root,
        CONFIG,
        limit=3,
        input_fn=_scripted("y", "y", "y"),
        print_fn=_silent,
    )
    assert result.labeled == 3
    assert len(load_labels(data_root)) == 3


# --- nc tune ------------------------------------------------------------


def _label(score: float, same_story: bool) -> Label:
    return Label(
        item_id_a="a",
        item_id_b="b",
        outlet_a="theverge",
        outlet_b="tomshardware",
        score=score,
        same_story=same_story,
        labeled_at="2026-09-16T12:00:00Z",
    )


def test_build_tune_report_is_empty_with_no_labels() -> None:
    report = build_tune_report([])
    assert report.total_labels == 0
    assert report.rows == ()


def test_build_tune_report_precision_and_recall_at_each_observed_score() -> None:
    labels = [
        _label(0.90, True),
        _label(0.80, True),
        _label(0.75, False),
        _label(0.70, True),
        _label(0.68, False),
    ]
    report = build_tune_report(labels)
    assert report.total_labels == 5
    assert report.total_positive == 3
    assert report.total_negative == 2

    by_threshold = {row.threshold: row for row in report.rows}
    assert set(by_threshold) == {0.90, 0.80, 0.75, 0.70, 0.68}

    # At 0.90: only the top label qualifies, and it is a true positive.
    top = by_threshold[0.90]
    assert (top.true_positive, top.false_positive, top.false_negative) == (1, 0, 2)
    assert top.precision == pytest.approx(1.0)
    assert top.recall == pytest.approx(1 / 3)

    # At 0.68 every label qualifies: precision is the base rate,
    # recall is complete (every true positive scores >= the lowest
    # threshold by construction).
    bottom = by_threshold[0.68]
    assert (bottom.true_positive, bottom.false_positive) == (3, 2)
    assert bottom.recall == pytest.approx(1.0)
    assert bottom.precision == pytest.approx(3 / 5)


def test_tune_row_precision_and_recall_are_none_with_no_denominator() -> None:
    report = build_tune_report([_label(0.70, False)])
    row = report.rows[0]
    assert row.true_positive == 0
    assert row.false_positive == 1
    assert row.precision == pytest.approx(0.0)
    assert row.recall is None  # no positive labels at all


def test_run_tune_reads_from_the_data_root(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    pair = _pair("e0", "theverge", "e1", "tomshardware", 0.72)
    append_label(data_root, label_from_pair(pair, True, "2026-09-16T12:00:00Z"))
    report = run_tune(data_root)
    assert report.total_labels == 1
    assert report.total_positive == 1


def test_format_tune_report_with_no_labels_says_so() -> None:
    text = format_tune_report(TuneReport(0, 0, 0, ()))
    assert "no labels yet" in text
    assert "nc label" in text


def test_format_tune_report_shows_counts_behind_every_number() -> None:
    labels = [_label(0.90, True)] + [_label(0.70 - i * 0.001, False) for i in range(6)]
    report = build_tune_report(labels)
    text = format_tune_report(report)
    # The precision at 0.90 is backed by exactly one label: flagged.
    assert "1.000 (1/1)*" in text
    # The denominator is printed next to every precision and recall.
    assert "false links" in text
    assert "missed links" in text


def test_format_tune_report_states_the_false_link_asymmetry() -> None:
    text = format_tune_report(build_tune_report([_label(0.7, True)]))
    assert "false link" in text
    assert "recover" in text or "not recoverable" in text


def test_format_tune_report_includes_current_config_when_given() -> None:
    text = format_tune_report(build_tune_report([_label(0.7, True)]), config=CONFIG)
    assert "tau_low=0.65" in text
    assert "tau_high=0.8" in text
