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

import json
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from nc.cluster import (
    ClusterConfig,
    ClusterItem,
    PendingPair,
    load_pending_pairs,
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
    format_import_report,
    format_tune_report,
    import_labels,
    label_from_pair,
    labelling_pool,
    labels_path,
    load_labels,
    order_for_labelling,
    run_label_session,
    run_tune,
    unlabeled_pairs,
)
from nc.promo import PromoRules, load_promo_rules
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
    seed_a: str,
    outlet_a: str,
    seed_b: str,
    outlet_b: str,
    score: float,
    title_a: str | None = None,
    title_b: str | None = None,
) -> PendingPair:
    return PendingPair(
        a=ClusterItem.from_item(_item(seed_a, outlet_a, title_a)),
        b=ClusterItem.from_item(_item(seed_b, outlet_b, title_b)),
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


def test_labelling_pool_drops_same_outlet_pairs(tmp_path: Path) -> None:
    """A cluster needs two distinct outlets, so what a label has to
    settle is whether two *outlets* covered one story. They stay in
    `pending-pairs/` for T24's judge, which can still use one to bridge
    two components -- the right rule for a machine working a queue and
    the wrong one for a human working an hour.
    """
    data_root = DataRoot(tmp_path / "data")
    cross = _pair("a0", "theverge", "a1", "tomshardware", 0.72)
    same = _pair("b0", "techcrunch", "b1", "techcrunch", 0.95)
    write_pending_pairs(data_root, [cross, same])

    assert {pair.pair_id for pair in labelling_pool(data_root)} == {cross.pair_id}
    # Still in the judge's queue, which is the point of filtering here
    # rather than pruning the directory.
    assert len(load_pending_pairs(data_root)) == 2


def test_labelling_pool_drops_promotional_and_buying_advice(tmp_path: Path) -> None:
    """Read-time, so a pair file written before a rule existed is
    filtered too -- a cluster-time filter alone cannot reach those, and
    11 of the 362-pair pool on 2026-09-17 were exactly that."""
    data_root = DataRoot(tmp_path / "data")
    real = _pair("a0", "theverge", "a1", "wired", 0.72)
    coupon = _pair(
        "b0", "wired", "b1", "theverge", 0.81, title_a="Casetify Promo Codes: 15% Off"
    )
    guide = _pair(
        "c0",
        "theguardian",
        "c1",
        "wired",
        0.67,
        title_b="7 Best Android Phones of 2026, Tested and Reviewed",
    )
    write_pending_pairs(data_root, [real, coupon, guide])

    pool = labelling_pool(
        data_root, promo_rules=load_promo_rules(Path("config/promo.yaml"))
    )
    assert {pair.pair_id for pair in pool} == {real.pair_id}


def test_labelling_pool_keeps_everything_when_rules_are_empty(tmp_path: Path) -> None:
    """Injected rules, like `run_clustering`'s: empty ones filter
    nothing, so a data root without config/promo.yaml still labels."""
    data_root = DataRoot(tmp_path / "data")
    coupon = _pair(
        "b0", "wired", "b1", "theverge", 0.81, title_a="Casetify Promo Codes: 15% Off"
    )
    write_pending_pairs(data_root, [coupon])

    pool = labelling_pool(data_root, promo_rules=PromoRules(frozenset(), ()))
    assert [pair.pair_id for pair in pool] == [coupon.pair_id]


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


# --- nc label --import (the guard on ground truth) ---------------------
#
# `labels/pairs.jsonl` decides `tau_high` through `nc tune` and scores
# the judge model through `nc bench-judge`. The answers in it come off
# a published page as taps, and something has to carry them back into
# the file. Every test below is one way that transcription can be
# wrong while looking right, and the import is allowed to write only
# when none of them applies.


def _import_root(tmp_path: Path) -> tuple[DataRoot, PendingPair]:
    data_root = DataRoot(tmp_path / "data")
    pair = _pair("i0", "theverge", "i1", "arstechnica", 0.6789)
    write_pending_pairs(data_root, [pair])
    return data_root, pair


def _line(pair: PendingPair, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "item_id_a": pair.a.item_id,
        "item_id_b": pair.b.item_id,
        "outlet_a": pair.a.outlet,
        "outlet_b": pair.b.outlet,
        "score": pair.score,
        "same_story": True,
        "labeled_at": "2026-09-22T09:00:00Z",
    }
    row.update(overrides)
    return row


def _write_import(tmp_path: Path, *rows: object) -> Path:
    path = tmp_path / "import.jsonl"
    path.write_text(
        "\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_import_records_an_answer_for_a_real_pair(tmp_path: Path) -> None:
    data_root, pair = _import_root(tmp_path)

    report = import_labels(data_root, _write_import(tmp_path, _line(pair)))

    assert report.ok and report.written
    recorded = load_labels(data_root)
    assert [label.pair_id for label in recorded] == [pair.pair_id]
    assert recorded[0].same_story is True
    assert recorded[0].labeled_at == "2026-09-22T09:00:00Z"


def test_import_refuses_a_pair_that_was_never_scored(tmp_path: Path) -> None:
    """The one that matters most: an id nothing on disk knows about is
    an answer to a question no page ever asked."""
    data_root, pair = _import_root(tmp_path)
    invented = _line(pair, item_id_a="item-nowhere")

    report = import_labels(data_root, _write_import(tmp_path, invented))

    assert not report.ok
    assert "no such pair" in report.rejected[0].reason
    assert load_labels(data_root) == []


def test_import_refuses_a_score_the_clusterer_did_not_compute(tmp_path: Path) -> None:
    """A label at the wrong score reads as ordinary and moves a
    `nc tune` threshold."""
    data_root, pair = _import_root(tmp_path)

    report = import_labels(
        data_root, _write_import(tmp_path, _line(pair, score=0.9123))
    )

    assert not report.ok
    assert "nc cluster computed 0.6789" in report.rejected[0].reason


def test_import_takes_the_score_from_disk_not_the_file(tmp_path: Path) -> None:
    """Within the four decimals the page carries the import agrees, and
    what gets written is still the clusterer's number."""
    data_root, pair = _import_root(tmp_path)

    import_labels(data_root, _write_import(tmp_path, _line(pair, score=0.67890001)))

    assert load_labels(data_root)[0].score == pair.score


def test_import_refuses_outlets_that_are_not_that_pairs(tmp_path: Path) -> None:
    data_root, pair = _import_root(tmp_path)

    report = import_labels(
        data_root, _write_import(tmp_path, _line(pair, outlet_a="wired"))
    )

    assert not report.ok
    assert "outlets say wired" in report.rejected[0].reason


def test_import_refuses_an_answer_that_is_not_a_yes_or_a_no(tmp_path: Path) -> None:
    data_root, pair = _import_root(tmp_path)

    report = import_labels(
        data_root, _write_import(tmp_path, _line(pair, same_story="yes"))
    )

    assert not report.ok
    assert "same_story is not true or false" in report.rejected[0].reason


def test_import_refuses_a_bad_timestamp(tmp_path: Path) -> None:
    data_root, pair = _import_root(tmp_path)

    report = import_labels(
        data_root, _write_import(tmp_path, _line(pair, labeled_at="yesterday"))
    )

    assert not report.ok
    assert "not a UTC timestamp" in report.rejected[0].reason


def test_import_refuses_a_line_that_is_not_json(tmp_path: Path) -> None:
    data_root, pair = _import_root(tmp_path)

    report = import_labels(data_root, _write_import(tmp_path, _line(pair), "{oops"))

    assert not report.ok
    assert report.rejected[0].line == 2
    assert load_labels(data_root) == []


def test_one_bad_line_stops_the_whole_batch(tmp_path: Path) -> None:
    """A partial import is both incomplete and indistinguishable from a
    complete one, which is why the good lines go unwritten too."""
    data_root = DataRoot(tmp_path / "data")
    good = _pair("g0", "theverge", "g1", "arstechnica", 0.71)
    other = _pair("h0", "wired", "h1", "zdnet", 0.62)
    write_pending_pairs(data_root, [good, other])
    path = _write_import(
        tmp_path,
        _line(good),
        _line(other, score=0.5),
        _line(other, same_story=False),
    )

    report = import_labels(data_root, path)

    assert not report.ok
    assert len(report.accepted) == 2, "the two good lines did pass validation"
    assert not report.written
    assert load_labels(data_root) == []


def test_importing_the_same_answer_twice_records_it_once(tmp_path: Path) -> None:
    """Re-importing a page after answering more of it is the normal
    way this gets used."""
    data_root, pair = _import_root(tmp_path)
    path = _write_import(tmp_path, _line(pair))
    import_labels(data_root, path)

    report = import_labels(data_root, path)

    assert report.ok
    assert report.accepted == ()
    assert [label.pair_id for label in report.already] == [pair.pair_id]
    assert len(load_labels(data_root)) == 1


def test_import_refuses_to_overturn_a_recorded_answer(tmp_path: Path) -> None:
    """`labels/pairs.jsonl` is append-only, and a human changing their
    mind is a thing to look at rather than overwrite."""
    data_root, pair = _import_root(tmp_path)
    import_labels(data_root, _write_import(tmp_path, _line(pair, same_story=True)))

    report = import_labels(
        data_root, _write_import(tmp_path, _line(pair, same_story=False))
    )

    assert not report.ok
    assert "already labelled same story" in report.rejected[0].reason
    assert [label.same_story for label in load_labels(data_root)] == [True]


def test_import_refuses_a_file_that_answers_one_pair_both_ways(tmp_path: Path) -> None:
    data_root, pair = _import_root(tmp_path)
    path = _write_import(
        tmp_path, _line(pair, same_story=True), _line(pair, same_story=False)
    )

    report = import_labels(data_root, path)

    assert not report.ok
    assert "both ways" in report.rejected[0].reason


def test_a_dry_run_reports_without_writing(tmp_path: Path) -> None:
    data_root, pair = _import_root(tmp_path)

    report = import_labels(
        data_root, _write_import(tmp_path, _line(pair)), dry_run=True
    )

    assert report.ok and not report.written
    assert len(report.accepted) == 1
    assert load_labels(data_root) == []
    assert "nothing written (dry run)" in format_import_report(report)


def test_the_report_names_a_rejection_by_line(tmp_path: Path) -> None:
    data_root, pair = _import_root(tmp_path)
    path = _write_import(tmp_path, _line(pair), _line(pair, score=0.1))

    text = format_import_report(import_labels(data_root, path))

    assert "1 line(s) rejected, nothing written:" in text
    assert "line 2:" in text
