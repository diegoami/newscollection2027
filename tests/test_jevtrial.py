"""Tests for `nc.jevtrial` (`nc bench-jev`).

No network anywhere: `post` is injected, so every test hands the trial
the responses Jev would have given and checks what it makes of them --
the same stance as tests/test_judge.py, which never runs a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nc.cluster import ClusterItem, PendingPair, write_label_sample, write_pending_pairs
from nc.feeds import Item
from nc.jevtrial import (
    QUESTION,
    VARIANT_DATES,
    VARIANT_NO_DATES,
    Answer,
    JevConfig,
    TrialPair,
    ask_all,
    build_report,
    calibration,
    format_report,
    in_tuning_half,
    load_jev_config,
    load_jev_prompt,
    parse_answer,
    pick_cutoff,
    prefilter,
    request_body,
    run_trial,
    trial_pairs,
)
from nc.labelling import Label, append_label
from nc.store import DataRoot

CONFIG = JevConfig(
    model="jev-1.13",
    base_url="https://example.invalid/api",
    api_key_env="NO_SUCH_KEY",
    timeout_seconds=5,
    cutoffs=(0.5, 0.7, 0.9),
    target_precision=0.9,
    prefilter_below=0.1,
)


def _item(seed: str, outlet: str) -> Item:
    return Item(
        id=seed * 40 if len(seed) == 1 else seed,
        outlet=outlet,
        url=f"https://{outlet}.example/{seed}",
        title=f"{seed} headline",
        lede=f"{seed} lede sentence.",
        author=None,
        published="2026-09-15T10:00:00Z",
        fetched="2026-09-16T00:00:00Z",
        tags=(),
    )


def _pair(a: str, b: str, score: float = 0.72) -> PendingPair:
    return PendingPair(
        a=ClusterItem.from_item(_item(a, "alpha")),
        b=ClusterItem.from_item(_item(b, "beta")),
        score=score,
    )


def _label(pair: PendingPair, same: bool) -> Label:
    return Label(
        item_id_a=pair.a.item_id,
        item_id_b=pair.b.item_id,
        outlet_a=pair.a.outlet,
        outlet_b=pair.b.outlet,
        score=pair.score,
        same_story=same,
        labeled_at="2026-09-20T10:00:00Z",
    )


def _answer(pair: PendingPair, p: float, variant: str = VARIANT_NO_DATES) -> Answer:
    return Answer(
        pair_id=pair.pair_id,
        variant=variant,
        p=p,
        model="jev-1.13-x",
        input_tokens=300,
        cost=0.00001,
        seconds=0.4,
    )


def _response(p: float) -> dict[str, Any]:
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {QUESTION: {"type": "noul", "noul": p}},
        "usage": {"input_tokens": 303, "output_tokens": 21, "cost": 0.0000127},
    }


# --- what Jev is shown ------------------------------------------------------


def test_the_request_never_carries_the_score() -> None:
    """The judge is never shown the cosine score (judge.render_pair_question)
    and neither is Jev: it is the signal already known not to decide this."""
    pair = _pair("a", "b", score=0.7234)
    body = json.dumps(request_body(pair, VARIANT_DATES, "Same event?", "jev-1.13"))
    assert "0.72" not in body
    assert "a headline" in body and "b headline" in body


def test_dates_are_sent_only_in_the_dates_variant() -> None:
    pair = _pair("a", "b")
    with_dates = request_body(pair, VARIANT_DATES, "q", "m")["state"]
    without = request_body(pair, VARIANT_NO_DATES, "q", "m")["state"]
    assert with_dates["article_a"]["published"] == "2026-09-15T10:00:00Z"
    assert "published" not in without["article_a"]


def test_the_question_is_one_noul_with_the_prompt() -> None:
    body = request_body(_pair("a", "b"), VARIANT_NO_DATES, "Same event?", "jev-1.13")
    assert body["model"] == "jev-1.13"
    assert body["questions"] == {
        QUESTION: {"type": "noul", "instructions": "Same event?"}
    }


def test_the_shipped_prompt_draws_the_event_topic_distinction() -> None:
    prompt = load_jev_prompt().lower()
    assert "same event" in prompt
    assert "subject" in prompt and "if you cannot tell, answer no" in prompt


def test_the_split_is_fixed_and_roughly_half() -> None:
    ids = [f"{i:040x}-{i + 1:040x}" for i in range(400)]
    first = [in_tuning_half(pid) for pid in ids]
    assert first == [in_tuning_half(pid) for pid in ids]
    assert 150 < sum(first) < 250


# --- reading answers ---------------------------------------------------------


def test_a_response_parses_into_an_answer() -> None:
    answer = parse_answer(_response(0.93), "x-y", VARIANT_NO_DATES, 0.41)
    assert answer.p == 0.93
    assert answer.model == "typesafe/jev-1.13-20260917"
    assert answer.cost == 0.0000127


@pytest.mark.parametrize(
    "raw",
    [
        {"answers": {}},
        {"answers": {QUESTION: {"type": "noul", "noul": 1.7}}},
        {"answers": {QUESTION: {"type": "noul", "noul": True}}},
        {"answers": {QUESTION: {"type": "noul"}}},
    ],
)
def test_a_bad_response_raises_rather_than_counting_as_no(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_answer(raw, "x-y", VARIANT_NO_DATES, 0.1)


def test_answers_are_cached_per_model_and_prompt(tmp_path: Path) -> None:
    pairs = [TrialPair(_pair("a", "b"), True)]
    calls: list[dict[str, Any]] = []

    def post(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        return _response(0.8)

    ask_all(pairs, "q1", CONFIG, post, tmp_path, [VARIANT_NO_DATES])
    ask_all(pairs, "q1", CONFIG, post, tmp_path, [VARIANT_NO_DATES])
    assert len(calls) == 1
    # A changed prompt is a different question.
    ask_all(pairs, "q2", CONFIG, post, tmp_path, [VARIANT_NO_DATES])
    assert len(calls) == 2


# --- scoring ------------------------------------------------------------------


def test_the_picked_cutoff_is_the_lowest_that_reaches_the_target() -> None:
    right = [TrialPair(_pair(c, c.upper()), True) for c in "abcd"]
    wrong = TrialPair(_pair("e", "f"), False)
    answers = [_answer(t.pair, 0.95) for t in right] + [_answer(wrong.pair, 0.6)]
    # At 0.5 the wrong pair links (precision 0.8); at 0.7 it does not.
    assert pick_cutoff([*right, wrong], answers, CONFIG) == 0.7


def test_no_cutoff_is_picked_when_none_reaches_the_target() -> None:
    wrong = TrialPair(_pair("e", "f"), False)
    assert pick_cutoff([wrong], [_answer(wrong.pair, 0.99)], CONFIG) is None


def test_calibration_compares_mean_p_with_the_share_of_matches() -> None:
    pairs = [TrialPair(_pair(c, c.upper()), c in "ab") for c in "abcd"]
    answers = [_answer(t.pair, 0.95) for t in pairs]
    [top] = calibration(pairs, answers)
    assert top.count == 4 and top.mean_p == pytest.approx(0.95)
    assert top.yes_rate == 0.5


def test_the_prefilter_counts_the_matches_it_would_lose() -> None:
    match = TrialPair(_pair("a", "b"), True)
    miss = TrialPair(_pair("c", "d"), False)
    result = prefilter(
        [match, miss], [_answer(match.pair, 0.05), _answer(miss.pair, 0.02)], 0.1
    )
    assert (result.dropped, result.total) == (2, 2)
    assert (result.matches_lost, result.matches) == (1, 1)


def test_the_report_scores_each_variant_and_formats() -> None:
    pairs = [TrialPair(_pair(c, c.upper()), c in "abc") for c in "abcdef"]
    answers = [
        _answer(t.pair, 0.95 if t.human else 0.05, variant)
        for t in pairs
        for variant in (VARIANT_NO_DATES, VARIANT_DATES)
    ]
    report = build_report(pairs, answers, [], CONFIG)
    assert [v.variant for v in report.variants] == [VARIANT_NO_DATES, VARIANT_DATES]
    text = format_report(report, CONFIG)
    assert "jev-1.13" in text and "calibration" in text and "pre-filter" in text


# --- end to end ------------------------------------------------------------


def test_the_trial_reads_labels_and_both_pair_directories(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    queued, sampled, gone = _pair("a", "b"), _pair("c", "d", 0.4), _pair("e", "f")
    write_pending_pairs(data_root, [queued])
    write_label_sample(data_root, [sampled])
    for pair, same in ((queued, True), (sampled, False), (gone, True)):
        append_label(data_root, _label(pair, same))

    pairs, skipped = trial_pairs(data_root)
    assert sorted(t.pair.pair_id for t in pairs) == sorted(
        [queued.pair_id, sampled.pair_id]
    )
    assert skipped == 1

    def post(body: dict[str, Any]) -> dict[str, Any]:
        return _response(0.9 if "a headline" in json.dumps(body) else 0.1)

    report = run_trial(data_root, CONFIG, post=post, cache_dir=tmp_path / "cache")
    assert report.pairs == 2 and report.matches == 1 and report.unrebuildable == 1


def test_without_a_key_the_trial_stops_before_any_call(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="NO_SUCH_KEY"):
        run_trial(DataRoot(tmp_path / "data"), CONFIG, cache_dir=tmp_path / "c")


def test_the_shipped_config_has_a_jev_section() -> None:
    config = load_jev_config()
    assert config.model.startswith("jev-")
    assert config.cutoffs == tuple(sorted(config.cutoffs))
    assert 0 < config.prefilter_below < min(config.cutoffs)


def test_a_timeout_is_retried_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first full run died on one slow response out of 560."""
    import io
    import urllib.request

    from nc.jevtrial import http_post

    calls = {"n": 0}

    def fake_urlopen(request: object, timeout: float) -> io.BytesIO:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("The read operation timed out")
        return io.BytesIO(json.dumps(_response(0.8)).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nc.jevtrial.time.sleep", lambda seconds: None)
    raw = http_post(CONFIG, "key")({"model": "m"})
    assert raw["answers"][QUESTION]["noul"] == 0.8
    assert calls["n"] == 2


def test_the_prefilter_counts_only_pairs_that_can_reach_the_queue() -> None:
    """Labels sampled below tau_low are all no and mostly easy; counting
    them doubled the first run's apparent pre-filter."""
    in_band = TrialPair(_pair("a", "b", score=0.65), True)
    below = TrialPair(_pair("c", "d", score=0.40), False)
    answers = [_answer(in_band.pair, 0.95), _answer(below.pair, 0.01)]
    report = build_report([in_band, below], answers, [], CONFIG, queue_floor=0.57)
    [variant] = report.variants
    assert (variant.prefilter.dropped, variant.prefilter.total) == (0, 1)
