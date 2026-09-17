"""Tests for T33's `nc.evals` (`nc eval`, the golden set).

No network and no spend: `run_eval` is pure, and every backend question
is answered by handing it analyses rather than producing them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nc.cluster import STATUS_PENDING, Cluster, ClusterItem, pending_path
from nc.contract import Analysis, analysis_path
from nc.evals import (
    ExpectedDiscrepancy,
    GoldenCase,
    clusters_from_positive_labels,
    format_eval_report,
    golden_from_dict,
    golden_to_dict,
    load_analyses_from_files,
    load_golden,
    materialize,
    run_eval,
    score_case,
    write_golden,
)
from nc.store import DataRoot

VERGE = "a" * 40
ARS = "b" * 40
WIRED = "c" * 40


def _item(
    item_id: str, outlet: str, title: str, lede: str, published: str
) -> ClusterItem:
    return ClusterItem(
        item_id=item_id, outlet=outlet, title=title, lede=lede, published=published
    )


ITEMS = (
    _item(
        VERGE,
        "theverge",
        "Acme ships the Widget 4",
        "It ships in Q1.",
        "2026-09-17T10:00:00Z",
    ),
    _item(
        ARS,
        "arstechnica",
        "Acme announces Widget 4",
        "It will cost $499.",
        "2026-09-17T11:00:00Z",
    ),
)


def _cluster(**overrides: Any) -> Cluster:
    base: dict[str, Any] = dict(
        id="2026-09-17-abc123",
        version=1,
        anchor=VERGE,
        status=STATUS_PENDING,
        items=ITEMS,
    )
    base.update(overrides)
    return Cluster(**base)


def _case(
    expected: tuple[ExpectedDiscrepancy, ...] = (), checked: bool = False
) -> GoldenCase:
    return GoldenCase(
        cluster=_cluster(), expected=expected, checked=checked, source="x"
    )


def _analysis(**overrides: Any) -> Analysis:
    base: dict[str, Any] = {
        "cluster_id": "2026-09-17-abc123",
        "cluster_version": 1,
        "backend": "claude_code",
        "model": "unknown",
        "generated_at": "2026-09-17T12:00:00Z",
        "headline": "Acme ships the Widget 4",
        "summary": "Two outlets report the Widget 4.",
        "claims": [
            {
                "id": "c1",
                "statement": "It ships in Q1.",
                "sources": [
                    {"outlet": "theverge", "item_id": VERGE, "quote": "It ships in Q1."}
                ],
            }
        ],
        "discrepancies": [],
        "agreement": "full",
    }
    base.update(overrides)
    return Analysis.model_validate(base)


_DISC = {
    "kind": "number",
    "severity": "high",
    "explanation": "The prices differ.",
    "quotes": [
        {"outlet": "theverge", "item_id": VERGE, "quote": "It ships in Q1."},
        {"outlet": "arstechnica", "item_id": ARS, "quote": "It will cost $499."},
    ],
}


# --- building the set -----------------------------------------------------


def test_positive_pairs_sharing_an_item_become_one_cluster() -> None:
    """Two pairs that share an item are one story with three outlets,
    not two stories with two -- so the components are the clusters."""
    third = _item(
        WIRED, "wired", "Widget 4 arrives", "Out now.", "2026-09-17T12:00:00Z"
    )
    clusters = clusters_from_positive_labels([(ITEMS[0], ITEMS[1]), (ITEMS[1], third)])
    assert len(clusters) == 1
    assert len(clusters[0].items) == 3


def test_a_same_outlet_component_is_not_a_cluster() -> None:
    """A cluster needs two distinct outlets, golden or not."""
    twin = _item(
        WIRED, "theverge", "Same outlet again", "More.", "2026-09-17T12:00:00Z"
    )
    assert clusters_from_positive_labels([(ITEMS[0], twin)]) == []


def test_golden_ids_match_the_contract_pattern() -> None:
    """A `golden-` prefix would have failed `cluster_id`'s pattern and
    the contract would have rejected every analysis of the set."""
    import re

    clusters = clusters_from_positive_labels([(ITEMS[0], ITEMS[1])])
    assert re.match(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{6}$", clusters[0].id)


def test_a_golden_case_round_trips(tmp_path: Path) -> None:
    case = _case((ExpectedDiscrepancy("number", ("theverge", "arstechnica"), "n"),))
    write_golden(case, tmp_path)
    assert load_golden(tmp_path) == [case]
    assert golden_from_dict(golden_to_dict(case)) == case


# --- scoring --------------------------------------------------------------


def test_a_matching_discrepancy_scores_on_kind_and_outlets() -> None:
    """Matched on `(kind, outlets)`, never the explanation: two correct
    descriptions of one disagreement never match as strings."""
    case = _case((ExpectedDiscrepancy("number", ("arstechnica", "theverge"), "n"),))
    score = score_case(case, _analysis(discrepancies=[_DISC], agreement="conflicting"))
    assert (score.matched, score.expected, score.produced) == (1, 1, 1)
    assert score.schema_ok


def test_a_wrong_kind_does_not_match() -> None:
    case = _case((ExpectedDiscrepancy("framing", ("arstechnica", "theverge"), "n"),))
    score = score_case(case, _analysis(discrepancies=[_DISC], agreement="conflicting"))
    assert (score.matched, score.produced) == (0, 1)


def test_an_invented_discrepancy_costs_precision() -> None:
    """The control the golden set needs: a case with nothing to disagree
    about, scored against a backend that invented one anyway."""
    score = score_case(
        _case(()), _analysis(discrepancies=[_DISC], agreement="conflicting")
    )
    assert (score.matched, score.expected, score.produced) == (0, 0, 1)
    report = run_eval(
        [_case(())],
        {
            "2026-09-17-abc123": _analysis(
                discrepancies=[_DISC], agreement="conflicting"
            )
        },
        "files",
        "m",
    )
    assert report.discrepancy_precision == 0.0


def test_a_missing_analysis_scores_zero_but_is_counted_separately() -> None:
    """Scoring it zero is right -- a backend that declines half the set
    is not perfect on the set -- but the report has to distinguish
    missing from failed or thin coverage reads as catastrophe."""
    report = run_eval([_case(()), _case(())], {}, "files", "m")
    assert report.schema_pass_rate == 0.0
    assert report.found == 0
    assert report.schema_pass_rate_of_found == 0.0


def test_an_absent_note_is_not_a_schema_failure() -> None:
    """Regression: a second serializer kept `notes: null`, which the
    schema refuses, so an analysis that is fine on disk scored as a
    schema failure."""
    score = score_case(_case(()), _analysis())
    assert score.schema_ok
    assert score.problems == []


def test_a_fabricated_quote_costs_quote_validity() -> None:
    bad = _analysis(
        claims=[
            {
                "id": "c1",
                "statement": "Made up.",
                "sources": [
                    {"outlet": "theverge", "item_id": VERGE, "quote": "never published"}
                ],
            }
        ]
    )
    report = run_eval([_case(())], {"2026-09-17-abc123": bad}, "files", "m")
    assert report.quote_validity == 0.0
    assert report.schema_pass_rate == 0.0


def test_the_report_names_the_unchecked_count() -> None:
    """Until T34 the discrepancy scores are measured against a proposal,
    and a reader who misses that will over-trust them."""
    report = run_eval([_case(()), _case((), checked=True)], {}, "files", "m")
    assert report.unchecked == 1
    assert "unchecked by the owner: 1" in format_eval_report(report)


# --- the loop -------------------------------------------------------------


def test_materialize_makes_the_golden_set_a_working_queue(tmp_path: Path) -> None:
    """Without this `--backend files` has nothing to score: the golden
    clusters are rebuilt from labels, so they are not the clusters the
    live pipeline holds."""
    data_root = DataRoot(tmp_path / "data")
    assert materialize([_case(())], data_root) == 1
    assert pending_path(data_root, "2026-09-17-abc123").exists()

    from nc.cluster import pending_clusters

    assert [c.id for c in pending_clusters(data_root)] == ["2026-09-17-abc123"]


def test_files_backend_reads_what_is_on_disk(tmp_path: Path) -> None:
    from nc.contract import render_analysis

    data_root = DataRoot(tmp_path / "data")
    case = _case(())
    path = analysis_path(data_root, case.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_analysis(_analysis()), encoding="utf-8")

    found = load_analyses_from_files(data_root, [case, _case()])
    assert found[case.id] is not None


def test_a_corrupt_analysis_reads_as_missing_not_as_a_crash(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    case = _case(())
    path = analysis_path(data_root, case.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_analyses_from_files(data_root, [case])[case.id] is None


# --- the shipped set ------------------------------------------------------


def test_the_shipped_golden_set_has_twenty_cases() -> None:
    cases = load_golden()
    assert len(cases) == 20
    assert all(len({i.outlet for i in c.cluster.items}) >= 2 for c in cases)


def test_the_shipped_set_has_a_case_with_no_expected_discrepancy() -> None:
    """The control that catches a backend inventing disagreements, which
    is the worst thing this site can publish."""
    assert any(not case.expected for case in load_golden())


def test_the_shipped_set_is_honest_about_not_being_checked() -> None:
    assert all(not case.checked for case in load_golden())
