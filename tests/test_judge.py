"""Tests for T24's `nc.judge` (`nc judge`, `nc bench-judge`).

No LLM anywhere, by design: this module is a *file* contract, and every
test here writes the files a backend would have written and then checks
what the validator, the clustering seam and the eval make of them. That
is the whole point of judgments being files (nc/judge.py's module
docstring) -- the question "would a wrong judgment link two unrelated
stories?" is answerable without a model in the loop.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nc.cluster import (
    ClusterConfig,
    ClusterItem,
    PendingPair,
    run_clustering,
    write_pending_pairs,
)
from nc.embed import embed_items
from nc.feeds import Item
from nc.judge import (
    BACKEND_API,
    BACKEND_CLAUDE_CODE,
    Judgment,
    JudgmentRejected,
    accepted_links,
    format_judge_eval,
    format_judge_report,
    judgment_from_dict,
    judgment_path,
    judgments_dir,
    load_judge_config,
    load_judge_prompt,
    load_judgments,
    render_judgment,
    render_pair_question,
    run_bench_judge,
    score_judgments,
    unjudged_pairs,
    validate_all,
    validate_judgment,
    write_judgments,
)
from nc.labelling import Label, append_label
from nc.promo import PromoRules
from nc.store import DataRoot, append_items

CONFIG = ClusterConfig(tau_low=0.65, tau_high=0.80, window_days=4, min_outlets=2)

_MODEL_ID = "synthetic"

# Injected instead of the clock so the four-day window is the same in
# every run: the items below are published on 2026-09-15.
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

# Explicit rather than the shipped config/promo.yaml: nothing here is
# testing the promotional filter, and a test that reads a live config
# file fails the day somebody edits it.
NO_PROMO_RULES = PromoRules(tags=frozenset(), title_patterns=())


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


def _pair(a: Item, b: Item, score: float = 0.72) -> PendingPair:
    return PendingPair(
        a=ClusterItem.from_item(a), b=ClusterItem.from_item(b), score=score
    )


def _judgment(
    pair: PendingPair,
    same_story: bool = True,
    reason: str = "both report the same launch event",
    backend: str = BACKEND_CLAUDE_CODE,
    model: str = "claude-sonnet-5",
) -> Judgment:
    return Judgment(
        pair_id=pair.pair_id,
        item_id_a=pair.a.item_id,
        item_id_b=pair.b.item_id,
        same_story=same_story,
        reason=reason,
        backend=backend,
        model=model,
        judged_at="2026-09-17T04:00:00Z",
    )


def _root(tmp_path: Path, pairs: list[PendingPair]) -> DataRoot:
    data_root = DataRoot(tmp_path / "data")
    write_pending_pairs(data_root, pairs)
    return data_root


# --- the file itself ------------------------------------------------------


def test_a_judgment_round_trips_through_its_file(tmp_path: Path) -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [pair])
    judgment = _judgment(pair)

    assert write_judgments(data_root, [judgment]) == 1
    assert load_judgments(data_root) == [judgment]


def test_writing_is_idempotent_and_never_rewrites(tmp_path: Path) -> None:
    """A run that re-judges nothing writes nothing: the data repo has a
    cron firing at it, and a rewritten file is a commit with no new
    information in it."""
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [pair])
    judgment = _judgment(pair)

    write_judgments(data_root, [judgment])
    path = judgment_path(data_root, pair.pair_id)
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns

    assert write_judgments(data_root, [_judgment(pair, reason="a later answer")]) == 0
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_the_rendered_file_is_byte_stable(tmp_path: Path) -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    text = render_judgment(_judgment(pair))
    assert text.endswith("}\n")
    assert ", " not in text
    assert list(json.loads(text)) == sorted(json.loads(text))


def test_a_string_same_story_is_refused() -> None:
    """`"false"` is truthy in Python. Reading it as a bool would link a
    pair whose backend said not to."""
    payload = {
        "backend": BACKEND_CLAUDE_CODE,
        "item_id_a": "item-a",
        "item_id_b": "item-b",
        "judged_at": "2026-09-17T04:00:00Z",
        "model": "claude-sonnet-5",
        "pair_id": "item-a-item-b",
        "reason": "both report the same launch event",
        "same_story": "false",
    }
    with pytest.raises(ValueError, match="same_story"):
        judgment_from_dict(payload)


def test_a_missing_field_names_itself() -> None:
    with pytest.raises(ValueError, match="reason"):
        judgment_from_dict({"pair_id": "x", "same_story": True})


def test_a_corrupt_file_raises_rather_than_being_skipped(tmp_path: Path) -> None:
    """`load_judgments` is the loud reader, and stays loud: `nc
    bench-judge` scoring a corpus it could not fully read would report a
    number about the wrong set of judgments.

    The clustering path is the quiet one (`accepted_links`), because it
    runs unattended every three hours and one bad file there stops
    every outlet. "Nobody would ever see why" is answered by `nc judge
    --validate`, which lists the file it could not read, and by the pair
    coming back onto the queue to be judged again.
    """
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [pair])
    write_judgments(data_root, [_judgment(pair)])
    judgment_path(data_root, pair.pair_id).write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match=pair.pair_id):
        load_judgments(data_root)


# --- the validator --------------------------------------------------------


def test_a_well_formed_judgment_passes(tmp_path: Path) -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    validate_judgment(_judgment(pair), {pair.pair_id: pair})


@pytest.mark.parametrize("backend", [BACKEND_CLAUDE_CODE, BACKEND_API])
def test_both_backends_are_accepted(backend: str) -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    validate_judgment(_judgment(pair, backend=backend), {pair.pair_id: pair})


def test_a_judgment_for_an_unknown_pair_is_refused() -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    with pytest.raises(JudgmentRejected, match="no pending pair"):
        validate_judgment(_judgment(pair), {})


def test_item_ids_that_do_not_match_the_pair_are_refused() -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    forged = Judgment(
        pair_id=pair.pair_id,
        item_id_a=pair.a.item_id,
        item_id_b="item-somebody-else",
        same_story=True,
        reason="both report the same launch event",
        backend=BACKEND_CLAUDE_CODE,
        model="claude-sonnet-5",
        judged_at="2026-09-17T04:00:00Z",
    )
    with pytest.raises(JudgmentRejected, match="item ids"):
        validate_judgment(forged, {pair.pair_id: pair})


def test_an_unknown_backend_is_refused() -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    with pytest.raises(JudgmentRejected, match="unknown backend"):
        validate_judgment(_judgment(pair, backend="vibes"), {pair.pair_id: pair})


def test_an_empty_model_is_refused() -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    with pytest.raises(JudgmentRejected, match="model"):
        validate_judgment(_judgment(pair, model="  "), {pair.pair_id: pair})


@pytest.mark.parametrize("reason", ["yes", "", "  same  ", "x" * 301])
def test_a_reason_outside_the_bounds_is_refused(reason: str) -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    with pytest.raises(JudgmentRejected, match="reason"):
        validate_judgment(_judgment(pair, reason=reason), {pair.pair_id: pair})


def test_validate_all_reports_the_rejects_without_stopping(tmp_path: Path) -> None:
    good = _pair(_item("a", "alpha"), _item("b", "beta"))
    orphan = _pair(_item("c", "gamma"), _item("d", "delta"))
    data_root = _root(tmp_path, [good])
    write_judgments(data_root, [_judgment(good), _judgment(orphan)])

    report = validate_all(data_root)
    assert report.judged == 2
    assert report.accepted == 1
    assert len(report.rejected) == 1
    assert orphan.pair_id in report.rejected[0]
    assert "rejected" in format_judge_report(report)


# --- the queue ------------------------------------------------------------


def test_the_queue_is_what_has_no_judgment_yet(tmp_path: Path) -> None:
    first = _pair(_item("a", "alpha"), _item("b", "beta"), score=0.70)
    second = _pair(_item("c", "gamma"), _item("d", "delta"), score=0.78)
    data_root = _root(tmp_path, [first, second])

    assert [pair.pair_id for pair in unjudged_pairs(data_root)] == [
        second.pair_id,
        first.pair_id,
    ]

    write_judgments(data_root, [_judgment(second)])
    assert [pair.pair_id for pair in unjudged_pairs(data_root)] == [first.pair_id]


def test_cross_outlet_pairs_are_judged_before_same_outlet_ones(
    tmp_path: Path,
) -> None:
    """A cluster needs two distinct outlets, so a same-outlet pair
    cannot form one on its own. It can still bridge two components, so
    it stays in the queue -- but on the live queue 1 of 78 would bridge,
    while they took 17 of the next 60 slots. Last, not gone.
    """
    high_same = _pair(_item("a", "alpha"), _item("b", "alpha"), score=0.96)
    low_cross = _pair(_item("c", "beta"), _item("d", "gamma"), score=0.60)
    data_root = _root(tmp_path, [high_same, low_cross])

    assert [pair.pair_id for pair in unjudged_pairs(data_root)] == [
        low_cross.pair_id,
        high_same.pair_id,
    ]


def test_a_self_pair_is_dropped_from_the_queue(tmp_path: Path) -> None:
    """One item id on both sides. Linking an item to itself is a no-op
    in union-find, so the answer can never change the output; these only
    exist because an outlet re-published under one id into two day files
    before `nc.cluster` was fixed."""
    item = _item("a", "alpha")
    self_pair = _pair(item, item, score=1.0)
    real = _pair(_item("b", "beta"), _item("c", "gamma"), score=0.61)
    data_root = _root(tmp_path, [self_pair, real])

    assert [pair.pair_id for pair in unjudged_pairs(data_root)] == [real.pair_id]


def test_score_still_orders_within_each_group(tmp_path: Path) -> None:
    low_cross = _pair(_item("a", "alpha"), _item("b", "beta"), score=0.61)
    high_cross = _pair(_item("c", "gamma"), _item("d", "delta"), score=0.88)
    low_same = _pair(_item("e", "alpha"), _item("f", "alpha"), score=0.62)
    high_same = _pair(_item("g", "beta"), _item("h", "beta"), score=0.91)
    data_root = _root(tmp_path, [low_cross, high_cross, low_same, high_same])

    assert [pair.pair_id for pair in unjudged_pairs(data_root)] == [
        high_cross.pair_id,
        low_cross.pair_id,
        high_same.pair_id,
        low_same.pair_id,
    ]


def test_the_question_shows_the_text_and_hides_the_score() -> None:
    """The score is the signal that could not decide this pair; showing
    it would anchor the answer on exactly what T23 falsified."""
    pair = _pair(
        _item("a", "alpha", title="Acme ships the Widget 4"),
        _item("b", "beta", title="Widget 4 is here"),
        score=0.7234,
    )
    question = render_pair_question(pair)
    assert "Acme ships the Widget 4" in question
    assert "Widget 4 is here" in question
    assert "alpha" in question and "beta" in question
    assert pair.a.item_id in question and pair.b.item_id in question
    assert "0.72" not in question


# --- the seam into clustering ---------------------------------------------


def test_only_an_accepted_yes_becomes_a_link(tmp_path: Path) -> None:
    yes = _pair(_item("a", "alpha"), _item("b", "beta"))
    no = _pair(_item("c", "gamma"), _item("d", "delta"))
    orphan = _pair(_item("e", "epsilon"), _item("f", "zeta"))
    data_root = _root(tmp_path, [yes, no])
    write_judgments(
        data_root,
        [_judgment(yes), _judgment(no, same_story=False), _judgment(orphan)],
    )

    assert accepted_links(data_root) == [(yes.a.item_id, yes.b.item_id)]


def test_a_judgment_for_an_unknown_pair_cannot_stop_the_nights_clustering(
    tmp_path: Path,
) -> None:
    """A well-formed judgment naming a pair that is not on the queue.
    It fails `validate_judgment` and is skipped."""
    good = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [good])
    write_judgments(
        data_root,
        [_judgment(good), _judgment(_pair(_item("x", "xi"), _item("y", "y")))],
    )

    assert accepted_links(data_root) == [(good.a.item_id, good.b.item_id)]


def test_an_unparseable_judgment_file_cannot_stop_the_nights_clustering(
    tmp_path: Path,
) -> None:
    """The case the test above was named for but did not cover: a file
    that is not valid JSON at all.

    `nc cluster` calls `accepted_links` on every three-hourly ingest, so
    one truncated file in the data repo used to fail the whole run for
    every outlet until somebody edited it by hand.
    """
    good = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [good])
    write_judgments(data_root, [_judgment(good)])
    truncated = judgments_dir(data_root) / "truncated.json"
    truncated.write_text('{"pair_id": "abc", "same_st', encoding="utf-8")

    assert accepted_links(data_root) == [(good.a.item_id, good.b.item_id)]


def test_a_judgment_file_missing_a_field_cannot_stop_clustering_either(
    tmp_path: Path,
) -> None:
    good = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [good])
    write_judgments(data_root, [_judgment(good)])
    (judgments_dir(data_root) / "partial.json").write_text(
        json.dumps({"pair_id": "abc"}), encoding="utf-8"
    )

    assert accepted_links(data_root) == [(good.a.item_id, good.b.item_id)]


def test_a_human_yes_links_a_pair_the_judge_never_saw(tmp_path: Path) -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [pair])
    append_label(data_root, _label(pair, True))

    assert accepted_links(data_root) == [(pair.a.item_id, pair.b.item_id)]


def test_a_human_answer_outranks_the_judges_either_way(tmp_path: Path) -> None:
    """The judge said yes and the human no: no link. The judge said no
    and the human yes: a link. A labelled pair is the human's call."""
    judge_yes = _pair(_item("a", "alpha"), _item("b", "beta"))
    judge_no = _pair(_item("c", "gamma"), _item("d", "delta"))
    data_root = _root(tmp_path, [judge_yes, judge_no])
    write_judgments(
        data_root, [_judgment(judge_yes), _judgment(judge_no, same_story=False)]
    )
    append_label(data_root, _label(judge_yes, False))
    append_label(data_root, _label(judge_no, True))

    assert accepted_links(data_root) == [(judge_no.a.item_id, judge_no.b.item_id)]


def test_the_last_human_answer_is_the_one_that_links(tmp_path: Path) -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [pair])
    append_label(data_root, _label(pair, True))
    append_label(data_root, _label(pair, False))

    assert accepted_links(data_root) == []


def test_a_labelled_pair_leaves_the_judges_queue(tmp_path: Path) -> None:
    labelled = _pair(_item("a", "alpha"), _item("b", "beta"))
    open_pair = _pair(_item("c", "gamma"), _item("d", "delta"))
    data_root = _root(tmp_path, [labelled, open_pair])
    append_label(data_root, _label(labelled, False))

    assert [pair.pair_id for pair in unjudged_pairs(data_root)] == [open_pair.pair_id]


def test_validate_reports_the_file_it_could_not_read(tmp_path: Path) -> None:
    """Skipped, not swallowed. `nc judge --validate` is where a person
    asks what is on disk, and an unreadable file has to show up there or
    it is invisible until someone wonders why a pair keeps coming back."""
    good = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [good])
    write_judgments(data_root, [_judgment(good)])
    (judgments_dir(data_root) / "truncated.json").write_text("{", encoding="utf-8")

    report = validate_all(data_root)

    assert report.accepted == 1
    assert any("truncated.json" in reason for reason in report.rejected)


def test_load_judgments_still_raises_for_a_caller_that_wants_to_know(
    tmp_path: Path,
) -> None:
    good = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [good])
    write_judgments(data_root, [_judgment(good)])
    (judgments_dir(data_root) / "truncated.json").write_text("{", encoding="utf-8")

    with pytest.raises(ValueError):
        load_judgments(data_root)


class _FixedBackend:
    def __init__(self, by_text: dict[str, list[float]]) -> None:
        self._by_text = by_text

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._by_text[text] for text in texts]


def test_a_judged_pair_changes_what_nc_cluster_writes(tmp_path: Path) -> None:
    """T24's acceptance criterion, end to end through the data root:
    two borderline items that do not link on cosine alone are one
    cluster once a judgment says they are the same story.
    """
    a = _item("a", "alpha")
    b = _item("b", "beta")
    data_root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "vectors.sqlite"
    append_items(data_root, [a, b])
    # cosine 0.72: inside [tau_low, tau_high), so borderline and unlinked.
    backend = _FixedBackend(
        {
            f"{a.title} {a.lede}": [1.0, 0.0],
            f"{b.title} {b.lede}": [0.72, 0.6939740629158988],
        }
    )
    embed_items(data_root, backend, _MODEL_ID, db_path)

    before = run_clustering(
        data_root,
        CONFIG,
        db_path,
        now=NOW,
        promo_rules=NO_PROMO_RULES,
        model_id=_MODEL_ID,
    )
    assert before.run.stats.clusters_new == 0
    pairs = unjudged_pairs(data_root)
    assert len(pairs) == 1

    write_judgments(data_root, [_judgment(pairs[0])])
    after = run_clustering(
        data_root,
        CONFIG,
        db_path,
        now=NOW,
        extra_links=accepted_links(data_root),
        promo_rules=NO_PROMO_RULES,
        model_id=_MODEL_ID,
    )
    assert after.run.stats.clusters_new == 1
    assert [cluster.item_ids for cluster in after.run.clusters] == [
        frozenset({a.id, b.id})
    ]


# --- the eval -------------------------------------------------------------


def _label(pair: PendingPair, same_story: bool) -> Label:
    return Label(
        item_id_a=pair.a.item_id,
        item_id_b=pair.b.item_id,
        outlet_a=pair.a.outlet,
        outlet_b=pair.b.outlet,
        score=pair.score,
        same_story=same_story,
        labeled_at="2026-09-16T12:00:00Z",
    )


def test_the_eval_counts_only_pairs_that_are_both_labelled_and_judged() -> None:
    hit = _pair(_item("a", "alpha"), _item("b", "beta"))
    miss = _pair(_item("c", "gamma"), _item("d", "delta"))
    unjudged = _pair(_item("e", "epsilon"), _item("f", "zeta"))

    result = score_judgments(
        [_label(hit, True), _label(miss, True), _label(unjudged, True)],
        [_judgment(hit), _judgment(miss, same_story=False)],
    )
    assert result.labelled == 3
    assert result.judged == 2
    assert result.true_positive == 1
    assert result.false_negative == 1
    assert result.precision == 1.0
    assert result.recall == 0.5


def test_a_wrong_link_shows_up_as_a_precision_miss() -> None:
    wrong = _pair(_item("a", "alpha"), _item("b", "beta"))
    result = score_judgments([_label(wrong, False)], [_judgment(wrong)])
    assert result.false_positive == 1
    assert result.precision == 0.0
    assert [d.pair_id for d in result.disagreements] == [wrong.pair_id]
    assert "disagreements" in format_judge_eval(result)


def test_the_last_label_for_a_pair_wins() -> None:
    """`labels/pairs.jsonl` is append-only, so a corrected human answer
    is a later line rather than an edit."""
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    result = score_judgments(
        [_label(pair, False), _label(pair, True)], [_judgment(pair)]
    )
    assert result.true_positive == 1
    assert result.false_positive == 0


def test_bench_judge_reads_both_files_off_the_data_root(tmp_path: Path) -> None:
    pair = _pair(_item("a", "alpha"), _item("b", "beta"))
    data_root = _root(tmp_path, [pair])
    write_judgments(data_root, [_judgment(pair)])
    append_label(data_root, _label(pair, True))

    result = run_bench_judge(data_root)
    assert result.judged == 1
    assert result.accuracy == 1.0


def test_an_eval_with_nothing_judged_says_so(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    assert "none of them judged yet" in format_judge_eval(run_bench_judge(data_root))


# --- the shipped config and prompt ----------------------------------------


def test_the_shipped_config_loads_and_names_a_model_for_each_backend() -> None:
    config = load_judge_config()
    assert config.model_for(BACKEND_CLAUDE_CODE)
    assert config.model_for(BACKEND_API)
    assert config.max_pairs_per_run > 0


def test_the_shipped_prompt_draws_the_distinction_the_score_cannot() -> None:
    """Also a contamination guard: T23's 174 labels are `nc
    bench-judge`'s ground truth, so none of them may be in the prompt as
    a worked example."""
    prompt = load_judge_prompt()
    assert "same TOPIC" in prompt and "same EVENT" in prompt
    assert "item-" not in prompt
