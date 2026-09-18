"""Tests for T41's `nc.stats`.

docs/PLAN.md T41's acceptance criterion: "unit test with fixture
analyses reproduces hand-computed numbers". The fixture below is small
enough to count by hand, and each assertion says what the hand count
was.
"""

from __future__ import annotations

from typing import Any

from nc.cluster import STATUS_PENDING, Cluster, ClusterItem
from nc.contract import Analysis
from nc.stats import compute_outlet_stats

A, B, C = "a" * 40, "b" * 40, "c" * 40


def _cluster(cluster_id: str, members: list[tuple[str, str, str]]) -> Cluster:
    items = tuple(
        ClusterItem(
            item_id=item_id,
            outlet=outlet,
            title=f"{outlet} headline",
            lede=f"{outlet} lede.",
            published=published,
        )
        for item_id, outlet, published in members
    )
    return Cluster(
        id=cluster_id,
        version=1,
        anchor=items[0].item_id,
        status=STATUS_PENDING,
        items=items,
    )


def _analysis(cluster_id: str, discrepancy_outlets: list[tuple[str, str]]) -> Analysis:
    payload: dict[str, Any] = {
        "cluster_id": cluster_id,
        "cluster_version": 1,
        "backend": "claude_code",
        "model": "unknown",
        "generated_at": "2026-09-17T12:00:00Z",
        "headline": "A headline",
        "summary": "A summary.",
        "claims": [
            {
                "id": "c1",
                "statement": "Something.",
                "sources": [
                    {"outlet": "theverge", "item_id": A, "quote": "theverge headline"}
                ],
            }
        ],
        "discrepancies": (
            [
                {
                    "kind": "framing",
                    "severity": "low",
                    "explanation": "They differ.",
                    "quotes": [
                        {
                            "outlet": outlet,
                            "item_id": item_id,
                            "quote": f"{outlet} headline",
                        }
                        for outlet, item_id in discrepancy_outlets
                    ],
                }
            ]
            if discrepancy_outlets
            else []
        ),
        "agreement": "partial" if discrepancy_outlets else "full",
    }
    return Analysis.model_validate(payload)


def test_hand_computed_numbers() -> None:
    """Three stories, counted by hand:

    story 1  theverge (10:00), arstechnica (11:00)  discrepancy: both
    story 2  theverge (12:00), wired (09:00)        no discrepancy
    story 3  theverge (08:00), arstechnica (08:00)  discrepancy: both

    theverge     3 stories, 2 with a discrepancy, first in 2
                 (story 1 at 10:00, story 3 tied at 08:00)
    arstechnica  2 stories, 2 with a discrepancy, first in 1
                 (story 3, tied)
    wired        1 story,  0 with a discrepancy, first in 1
    """
    one = _cluster(
        "2026-09-17-000001",
        [
            (A, "theverge", "2026-09-17T10:00:00Z"),
            (B, "arstechnica", "2026-09-17T11:00:00Z"),
        ],
    )
    two = _cluster(
        "2026-09-17-000002",
        [
            (A, "theverge", "2026-09-17T12:00:00Z"),
            (C, "wired", "2026-09-17T09:00:00Z"),
        ],
    )
    three = _cluster(
        "2026-09-17-000003",
        [
            (A, "theverge", "2026-09-17T08:00:00Z"),
            (B, "arstechnica", "2026-09-17T08:00:00Z"),
        ],
    )
    stats = {
        entry.outlet: entry
        for entry in compute_outlet_stats(
            [
                (_analysis(one.id, [("theverge", A), ("arstechnica", B)]), one),
                (_analysis(two.id, []), two),
                (_analysis(three.id, [("theverge", A), ("arstechnica", B)]), three),
            ]
        )
    }

    assert (
        stats["theverge"].stories,
        stats["theverge"].discrepancy_stories,
        stats["theverge"].first_stories,
    ) == (3, 2, 2)
    assert (
        stats["arstechnica"].stories,
        stats["arstechnica"].discrepancy_stories,
        stats["arstechnica"].first_stories,
    ) == (2, 2, 1)
    assert (
        stats["wired"].stories,
        stats["wired"].discrepancy_stories,
        stats["wired"].first_stories,
    ) == (1, 0, 1)

    assert stats["theverge"].discrepancy_rate == 2 / 3
    assert stats["arstechnica"].first_rate == 1 / 2


def test_a_tie_makes_both_outlets_first() -> None:
    """Breaking the tie by name or item id would manufacture a
    difference out of a timestamp's resolution."""
    tied = _cluster(
        "2026-09-17-000004",
        [
            (A, "theverge", "2026-09-17T08:00:00Z"),
            (B, "arstechnica", "2026-09-17T08:00:00Z"),
        ],
    )
    stats = {
        e.outlet: e for e in compute_outlet_stats([(_analysis(tied.id, []), tied)])
    }
    assert stats["theverge"].first_stories == 1
    assert stats["arstechnica"].first_stories == 1


def test_an_outlet_is_counted_once_per_story_however_many_discrepancies() -> None:
    """The rate is stories-with-a-discrepancy over stories, not
    discrepancies over stories -- otherwise it could exceed 1."""
    cluster = _cluster(
        "2026-09-17-000005",
        [
            (A, "theverge", "2026-09-17T08:00:00Z"),
            (B, "arstechnica", "2026-09-17T09:00:00Z"),
        ],
    )
    analysis = _analysis(cluster.id, [("theverge", A), ("arstechnica", B)])
    doubled = Analysis.model_validate(
        {
            **analysis.model_dump(mode="json", exclude_none=True),
            "discrepancies": [
                analysis.discrepancies[0].model_dump(mode="json"),
                analysis.discrepancies[0].model_dump(mode="json"),
            ],
        }
    )
    stats = {e.outlet: e for e in compute_outlet_stats([(doubled, cluster)])}
    assert stats["theverge"].discrepancy_stories == 1
    assert stats["theverge"].discrepancy_rate == 1.0


def test_no_stories_is_not_a_division_by_zero() -> None:
    assert compute_outlet_stats([]) == []


def test_ordering_is_stable() -> None:
    """The front page renders this list, and the built site must not
    churn between runs."""
    one = _cluster(
        "2026-09-17-000001",
        [
            (A, "theverge", "2026-09-17T10:00:00Z"),
            (B, "arstechnica", "2026-09-17T11:00:00Z"),
        ],
    )
    stats = compute_outlet_stats([(_analysis(one.id, []), one)])
    assert [e.outlet for e in stats] == ["arstechnica", "theverge"]
