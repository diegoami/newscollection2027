"""Tests for `nc.jevjudge` (`nc judge --backend jev`).

No network: `post` is injected. Every test checks what lands in
`judgments/` and what the validator and the queue make of it, because
that is the whole contract (nc/judge.py's module docstring).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nc.cluster import ClusterItem, PendingPair, write_pending_pairs
from nc.feeds import Item
from nc.jevjudge import format_jev_judge_report, judge_with_jev
from nc.jevtrial import QUESTION, JevConfig
from nc.judge import (
    BACKEND_JEV,
    accepted_links,
    load_judgments,
    run_bench_judge,
    unjudged_pairs,
    validate_all,
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
    auto_yes_at=0.9,
)
NOW = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)


def _item(seed: str, outlet: str) -> Item:
    return Item(
        id=seed * 40,
        outlet=outlet,
        url=f"https://{outlet}.example/{seed}",
        title=f"{seed} headline",
        lede=f"{seed} lede sentence.",
        author=None,
        published="2026-09-25T10:00:00Z",
        fetched="2026-09-25T11:00:00Z",
        tags=(),
    )


def _pair(a: str, b: str) -> PendingPair:
    return PendingPair(
        a=ClusterItem.from_item(_item(a, "alpha")),
        b=ClusterItem.from_item(_item(b, "beta")),
        score=0.7,
    )


def _post(p_by_title: dict[str, float]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def post(body: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(body)
        p = next(p for title, p in p_by_title.items() if title in text)
        return {
            "model": "typesafe/jev-1.13-20260917",
            "answers": {QUESTION: {"type": "noul", "noul": p}},
            "usage": {"input_tokens": 300, "cost": 0.00001},
        }

    return post


def _root(tmp_path: Path, pairs: list[PendingPair]) -> DataRoot:
    data_root = DataRoot(tmp_path / "data")
    write_pending_pairs(data_root, pairs)
    return data_root


def test_jev_settles_the_confident_ends_and_leaves_the_middle(tmp_path: Path) -> None:
    sure, unsure, unrelated = _pair("a", "b"), _pair("c", "d"), _pair("e", "f")
    data_root = _root(tmp_path, [sure, unsure, unrelated])
    post = _post({"a headline": 0.95, "c headline": 0.5, "e headline": 0.02})

    report = judge_with_jev(data_root, CONFIG, post, tmp_path / "cache", now=NOW)

    assert (report.yes, report.no, report.middle, report.written) == (1, 1, 1, 2)
    assert [p.pair_id for p in unjudged_pairs(data_root)] == [unsure.pair_id]
    assert accepted_links(data_root) == [(sure.a.item_id, sure.b.item_id)]


def test_jev_judgments_pass_the_validator_and_say_why(tmp_path: Path) -> None:
    data_root = _root(tmp_path, [_pair("a", "b")])
    judge_with_jev(
        data_root, CONFIG, _post({"a headline": 0.95}), tmp_path / "c", now=NOW
    )

    assert validate_all(data_root).rejected == []
    [judgment] = load_judgments(data_root)
    assert judgment.backend == BACKEND_JEV
    assert judgment.model == "typesafe/jev-1.13-20260917"
    assert judgment.judged_at == "2026-09-26T03:00:00Z"
    assert "p=0.95" in judgment.reason


def test_without_a_key_nothing_runs_and_the_night_goes_on(tmp_path: Path) -> None:
    data_root = _root(tmp_path, [_pair("a", "b")])
    report = judge_with_jev(data_root, CONFIG, cache_dir=tmp_path / "c")
    assert report.skipped == "NO_SUCH_KEY is not set"
    assert load_judgments(data_root) == []
    assert "skipped" in format_jev_judge_report(report)


def test_a_provider_failure_keeps_what_was_answered(tmp_path: Path) -> None:
    first, second = _pair("a", "b"), _pair("c", "d")
    data_root = _root(tmp_path, [first, second])

    def post(body: dict[str, Any]) -> dict[str, Any]:
        if "c headline" in json.dumps(body):
            raise TimeoutError("The read operation timed out")
        return _post({"a headline": 0.95})(body)

    report = judge_with_jev(data_root, CONFIG, post, tmp_path / "cache", now=NOW)
    assert report.error is not None and "TimeoutError" in report.error
    assert report.written == 1
    assert "stopped early" in format_jev_judge_report(report)


def test_a_labelled_pair_is_never_asked(tmp_path: Path) -> None:
    labelled = _pair("a", "b")
    data_root = _root(tmp_path, [labelled])
    append_label(
        data_root,
        Label(
            item_id_a=labelled.a.item_id,
            item_id_b=labelled.b.item_id,
            outlet_a="alpha",
            outlet_b="beta",
            score=0.7,
            same_story=False,
            labeled_at="2026-09-25T12:00:00Z",
        ),
    )

    def post(body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a labelled pair was sent to Jev")

    report = judge_with_jev(data_root, CONFIG, post, tmp_path / "cache", now=NOW)
    assert report.asked == 0


def test_bench_judge_can_score_one_backend_alone(tmp_path: Path) -> None:
    pair = _pair("a", "b")
    data_root = _root(tmp_path, [pair])
    judge_with_jev(
        data_root, CONFIG, _post({"a headline": 0.95}), tmp_path / "c", now=NOW
    )
    append_label(
        data_root,
        Label(
            item_id_a=pair.a.item_id,
            item_id_b=pair.b.item_id,
            outlet_a="alpha",
            outlet_b="beta",
            score=0.7,
            same_story=True,
            labeled_at="2026-09-26T12:00:00Z",
        ),
    )
    assert run_bench_judge(data_root, BACKEND_JEV).judged == 1
    assert run_bench_judge(data_root, "claude_code").judged == 0
