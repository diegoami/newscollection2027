"""Tests for T30's `nc.contract` (`nc validate`).

docs/PLAN.md T30's acceptance criterion: "tests with at least five valid
and ten invalid samples covering every rule in docs/ARCHITECTURE.md".
The samples are built from one fixture cluster by `_analysis(**overrides)`
rather than written out as literals, so a rule's test shows only the
thing that breaks it -- the valid baseline is in one place and cannot
drift away from the invalid cases.

The rules, and where each is covered:

    schema shape .................. test_schema_rejects_*
    item_id belongs to cluster .... test_a_quote_from_outside_the_cluster
    outlet matches the item ....... test_a_quote_attributed_to_the_wrong_outlet
    quote is verbatim ............. test_a_quote_that_is_not_verbatim
                                    test_a_quote_that_straddles_title_and_lede
                                    test_whitespace_is_the_only_latitude
    at least one claim ............ test_schema_rejects_an_analysis_with_no_claims
    discrepancy spans 2 outlets ... test_a_discrepancy_from_one_outlet
    length limits ................. test_a_headline_over_the_word_limit
                                    test_a_summary_over_the_word_limit
    currency ...................... test_a_stale_cluster_version
                                    test_a_superseded_cluster
                                    test_an_analysis_with_no_cluster
    internal consistency .......... test_duplicate_claim_ids
                                    test_a_discrepancy_naming_an_unknown_claim
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nc.cluster import (
    STATUS_ANALYZED,
    STATUS_PENDING,
    STATUS_SUPERSEDED,
    Cluster,
    ClusterItem,
    pending_clusters,
    pending_path,
    render,
    set_cluster_status,
)
from nc.contract import (
    DEFAULT_SCHEMA_PATH,
    Analysis,
    analysis_path,
    check_against_cluster,
    format_validate_report,
    model_errors,
    rejected_path,
    render_analysis,
    run_validate,
    schema_errors,
    validate_analysis,
)
from nc.store import DataRoot

VERGE = "a" * 40
ARS = "b" * 40
WIRED = "c" * 40
OUTSIDER = "d" * 40
CLUSTER_ID = "2026-09-17-abc123"


def _member(
    item_id: str,
    outlet: str,
    title: str,
    lede: str,
    published: str = "2026-09-17T10:00:00Z",
) -> ClusterItem:
    return ClusterItem(
        item_id=item_id,
        outlet=outlet,
        title=title,
        lede=lede,
        published=published,
    )


def _cluster(**overrides: Any) -> Cluster:
    base: dict[str, Any] = dict(
        id=CLUSTER_ID,
        version=2,
        anchor=VERGE,
        status=STATUS_PENDING,
        items=(
            _member(
                VERGE,
                "theverge",
                "Acme ships the Widget 4",
                "The company said the part ships in the first quarter.",
            ),
            _member(
                ARS,
                "arstechnica",
                "Acme announces Widget 4 for Q1",
                "Acme said on Tuesday that the Widget 4 will cost $499.",
            ),
            _member(
                WIRED,
                "wired",
                "Widget 4 arrives with a higher price",
                "Acme's new part will sell for $599 when it ships.",
            ),
        ),
    )
    base.update(overrides)
    return Cluster(**base)


def _quote(item_id: str, outlet: str, quote: str) -> dict[str, Any]:
    return {"outlet": outlet, "item_id": item_id, "quote": quote}


def _analysis(**overrides: Any) -> dict[str, Any]:
    """A valid analysis of `_cluster()`. Every invalid sample below is
    this with one thing changed, so each test names its own rule."""
    base: dict[str, Any] = {
        "cluster_id": CLUSTER_ID,
        "cluster_version": 2,
        "backend": "claude_code",
        "model": "claude-sonnet-5",
        "generated_at": "2026-09-17T12:00:00Z",
        "headline": "Acme ships the Widget 4",
        "summary": "Three outlets report that Acme's Widget 4 ships in the "
        "first quarter. They differ on the price.",
        "claims": [
            {
                "id": "c1",
                "statement": "The Widget 4 ships in the first quarter.",
                "sources": [
                    _quote(
                        VERGE,
                        "theverge",
                        "The company said the part ships in the first quarter.",
                    ),
                    _quote(ARS, "arstechnica", "Acme announces Widget 4 for Q1"),
                ],
            },
            {
                "id": "c2",
                "statement": "Accounts of the price differ.",
                "sources": [
                    _quote(
                        ARS,
                        "arstechnica",
                        "the Widget 4 will cost $499",
                    ),
                ],
            },
        ],
        "discrepancies": [
            {
                "kind": "number",
                "severity": "high",
                "claim_ids": ["c2"],
                "explanation": "The two outlets give different prices.",
                "quotes": [
                    _quote(ARS, "arstechnica", "the Widget 4 will cost $499"),
                    _quote(WIRED, "wired", "will sell for $599 when it ships"),
                ],
            }
        ],
        "agreement": "partial",
    }
    base.update(overrides)
    return base


def _problems(payload: dict[str, Any], cluster: Cluster | None = None) -> list[str]:
    _, problems = validate_analysis(payload, _cluster() if cluster is None else cluster)
    return problems


# --- valid samples (five, per the AC) -------------------------------------


def test_the_baseline_analysis_is_valid() -> None:
    assert _problems(_analysis()) == []


def test_a_quote_may_come_from_the_title_alone() -> None:
    payload = _analysis(
        claims=[
            {
                "id": "c1",
                "statement": "Acme shipped it.",
                "sources": [_quote(VERGE, "theverge", "Acme ships the Widget 4")],
            }
        ],
        discrepancies=[],
    )
    assert _problems(payload) == []


def test_a_quote_may_come_from_the_lede_alone() -> None:
    payload = _analysis(
        claims=[
            {
                "id": "c1",
                "statement": "It ships in Q1.",
                "sources": [
                    _quote(
                        VERGE,
                        "theverge",
                        "ships in the first quarter",
                    )
                ],
            }
        ],
        discrepancies=[],
    )
    assert _problems(payload) == []


def test_an_analysis_with_no_discrepancies_is_valid() -> None:
    """`discrepancies` has no minItems: outlets agreeing is a result,
    not a failure, and forcing one would invite a fabricated
    disagreement -- the worst thing this site could publish."""
    payload = _analysis(discrepancies=[], agreement="full")
    assert _problems(payload) == []


def test_notes_and_claim_ids_are_optional_and_accepted() -> None:
    payload = _analysis(notes="Both outlets cite the same press release.")
    assert _problems(payload) == []
    payload = _analysis(
        discrepancies=[
            {
                "kind": "framing",
                "severity": "low",
                "explanation": "One frames it as a price rise.",
                "quotes": [
                    _quote(ARS, "arstechnica", "the Widget 4 will cost $499"),
                    _quote(WIRED, "wired", "Widget 4 arrives with a higher price"),
                ],
            }
        ]
    )
    assert _problems(payload) == []


def test_whitespace_is_the_only_latitude() -> None:
    """A feed's lede wraps however the outlet's CMS wrapped it. Runs of
    whitespace collapse on both sides before comparison; nothing else
    is forgiven."""
    cluster = _cluster(
        items=(
            _member(
                VERGE,
                "theverge",
                "Acme ships the Widget 4",
                "The company said\n   the part ships\tin the first quarter.",
            ),
        )
    )
    payload = _analysis(
        claims=[
            {
                "id": "c1",
                "statement": "It ships in Q1.",
                "sources": [
                    _quote(
                        VERGE,
                        "theverge",
                        "The company said the part ships in the first quarter.",
                    )
                ],
            }
        ],
        discrepancies=[],
    )
    assert _problems(payload, cluster) == []


# --- invalid samples ------------------------------------------------------


def test_schema_rejects_a_missing_required_field() -> None:
    payload = _analysis()
    del payload["agreement"]
    problems = _problems(payload)
    assert any("agreement" in problem for problem in problems)


def test_schema_rejects_an_unknown_field() -> None:
    """`additionalProperties: false`, so a backend inventing a field is
    a reject rather than a silently ignored surprise."""
    problems = _problems(_analysis(confidence=0.9))
    assert any("confidence" in problem for problem in problems)


def test_schema_rejects_an_analysis_with_no_claims() -> None:
    problems = _problems(_analysis(claims=[]))
    assert problems


def test_schema_rejects_a_bad_cluster_id() -> None:
    problems = _problems(_analysis(cluster_id="not-a-cluster-id"))
    assert problems


def test_schema_rejects_an_unknown_discrepancy_kind() -> None:
    payload = _analysis()
    payload["discrepancies"][0]["kind"] = "vibes"
    assert _problems(payload)


def test_a_quote_from_outside_the_cluster() -> None:
    payload = _analysis(
        claims=[
            {
                "id": "c1",
                "statement": "Something else happened.",
                "sources": [_quote(OUTSIDER, "engadget", "Acme ships the Widget 4")],
            }
        ],
        discrepancies=[],
    )
    problems = _problems(payload)
    assert any("not in cluster" in problem for problem in problems)


def test_a_quote_attributed_to_the_wrong_outlet() -> None:
    payload = _analysis(
        claims=[
            {
                "id": "c1",
                "statement": "Acme shipped it.",
                "sources": [_quote(VERGE, "arstechnica", "Acme ships the Widget 4")],
            }
        ],
        discrepancies=[],
    )
    problems = _problems(payload)
    assert any("attributed to arstechnica" in problem for problem in problems)


def test_a_quote_that_is_not_verbatim() -> None:
    """The check the site's whole claim to be checkable rests on."""
    payload = _analysis(
        claims=[
            {
                "id": "c1",
                "statement": "Acme shipped it.",
                "sources": [_quote(VERGE, "theverge", "Acme shipped the Widget Four")],
            }
        ],
        discrepancies=[],
    )
    problems = _problems(payload)
    assert any("not verbatim" in problem for problem in problems)


def test_a_quote_that_straddles_title_and_lede() -> None:
    """Text that exists only because the title and lede were joined is
    not something the outlet published, so the joined string is not a
    haystack a quote may match against."""
    payload = _analysis(
        claims=[
            {
                "id": "c1",
                "statement": "Acme shipped it.",
                "sources": [_quote(VERGE, "theverge", "Widget 4 The company said")],
            }
        ],
        discrepancies=[],
    )
    problems = _problems(payload)
    assert any("straddles" in problem for problem in problems)


def test_a_discrepancy_from_one_outlet() -> None:
    payload = _analysis()
    payload["discrepancies"][0]["quotes"] = [
        _quote(ARS, "arstechnica", "the Widget 4 will cost $499"),
        _quote(ARS, "arstechnica", "Acme announces Widget 4 for Q1"),
    ]
    problems = _problems(payload)
    assert any("at least two" in problem for problem in problems)


def test_a_headline_over_the_word_limit() -> None:
    """15 words, which JSON Schema cannot count -- the schema's 120
    characters is a guard rail, this is the contract."""
    payload = _analysis(headline=" ".join(["word"] * 16))
    problems = _problems(payload)
    assert any("headline: 16 words" in problem for problem in problems)


def test_a_summary_over_the_word_limit() -> None:
    payload = _analysis(summary=" ".join(["word"] * 81))
    problems = _problems(payload)
    assert any("summary: 81 words" in problem for problem in problems)


def test_a_stale_cluster_version() -> None:
    """docs/ARCHITECTURE.md: an analysis is current only when its
    `cluster_version` equals the cluster's. Anything else is history."""
    problems = _problems(_analysis(cluster_version=1))
    assert any("version 1" in problem for problem in problems)


def test_a_superseded_cluster() -> None:
    cluster = _cluster(status=STATUS_SUPERSEDED, superseded_by="2026-09-16-ffffff")
    problems = _problems(_analysis(), cluster)
    assert any("superseded" in problem for problem in problems)


def test_an_analysis_with_no_cluster() -> None:
    """Unverifiable is the same as invalid: there is nothing to check
    the quotes against."""
    _, problems = validate_analysis(_analysis(), None)
    assert any("no cluster file" in problem for problem in problems)


def test_duplicate_claim_ids() -> None:
    payload = _analysis()
    payload["claims"][1]["id"] = "c1"
    problems = _problems(payload)
    assert any("duplicate claim id" in problem for problem in problems)


def test_a_discrepancy_naming_an_unknown_claim() -> None:
    payload = _analysis()
    payload["discrepancies"][0]["claim_ids"] = ["c9"]
    problems = _problems(payload)
    assert any("c9" in problem for problem in problems)


def test_every_problem_is_reported_not_just_the_first() -> None:
    """An agent that learns one fault per round trip makes one round
    trip per fault."""
    payload = _analysis(
        headline=" ".join(["word"] * 16),
        summary=" ".join(["word"] * 81),
        cluster_version=1,
    )
    assert len(_problems(payload)) >= 3


# --- the two structural layers must agree ---------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        _analysis(),
        _analysis(discrepancies=[]),
        _analysis(notes="a note"),
        {"cluster_id": CLUSTER_ID},
        _analysis(claims=[]),
        _analysis(cluster_id="nope"),
        _analysis(agreement="maybe"),
        _analysis(cluster_version=0),
        _analysis(confidence=0.9),
        _analysis(model=""),
    ],
)
def test_schema_and_model_agree_on_every_sample(payload: dict[str, Any]) -> None:
    """`contract/analysis.schema.json` and the pydantic models describe
    the same shape twice. Two sources of truth are only tolerable when
    something checks they still say the same thing."""
    by_schema = not schema_errors(payload)
    _, model_problems = model_errors(payload)
    by_model = not model_problems
    assert by_schema == by_model, (
        f"schema says {by_schema}, model says {by_model}: {payload}"
    )


def test_the_shipped_schema_is_the_one_being_checked() -> None:
    assert DEFAULT_SCHEMA_PATH.exists()
    schema = json.loads(DEFAULT_SCHEMA_PATH.read_text())
    assert schema["additionalProperties"] is False


# --- `nc validate` --------------------------------------------------------


def _write_cluster(data_root: DataRoot, cluster: Cluster) -> None:
    """Written with `nc.cluster.render`, not a hand-rolled dump, so
    these tests read exactly what `nc cluster` writes."""
    path = data_root.resolve("clusters", cluster.date, f"{cluster.id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(cluster), encoding="utf-8")


def _write_analysis(data_root: DataRoot, payload: dict[str, Any]) -> Path:
    path = analysis_path(data_root, str(payload["cluster_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_validate_keeps_a_valid_analysis_where_it_is(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    _write_cluster(data_root, _cluster())
    path = _write_analysis(data_root, _analysis())

    report = run_validate(data_root, state_path=tmp_path / "state.json")

    assert (report.checked, report.valid, report.rejected) == (1, 1, [])
    assert path.exists()


def test_validate_moves_a_rejected_analysis_out_of_analyses(tmp_path: Path) -> None:
    """Moved, not copied: leaving it would let `nc build` read an
    analysis the validator refused, which is the bypass CLAUDE.md
    forbids."""
    data_root = DataRoot(tmp_path / "data")
    _write_cluster(data_root, _cluster())
    path = _write_analysis(data_root, _analysis(cluster_version=1))

    report = run_validate(data_root, state_path=tmp_path / "state.json")

    assert report.valid == 0
    assert [cluster_id for cluster_id, _ in report.rejected] == [CLUSTER_ID]
    assert not path.exists()
    rejection = json.loads(rejected_path(data_root, CLUSTER_ID).read_text())
    assert rejection["problems"]
    # The analysis travels with its reasons, so a retry has the text.
    assert rejection["analysis"]["cluster_version"] == 1
    assert CLUSTER_ID in format_validate_report(report)


def test_validate_rejects_a_file_that_is_not_json(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    _write_cluster(data_root, _cluster())
    path = analysis_path(data_root, CLUSTER_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    report = run_validate(data_root, state_path=tmp_path / "state.json")

    assert len(report.rejected) == 1
    assert not path.exists()


def test_validate_new_skips_what_has_not_changed(tmp_path: Path) -> None:
    import os

    data_root = DataRoot(tmp_path / "data")
    state = tmp_path / "state.json"
    _write_cluster(data_root, _cluster())
    path = _write_analysis(data_root, _analysis())

    first = run_validate(data_root, state_path=state, now=1000.0)
    assert first.checked == 1

    os.utime(path, (900.0, 900.0))
    second = run_validate(data_root, only_new=True, state_path=state, now=2000.0)
    assert (second.checked, second.skipped_unchanged) == (0, 1)

    # Touched after the second run's recorded moment, so it is checked
    # again. A tie re-checks too, by design: see run_validate.
    os.utime(path, (2500.0, 2500.0))
    third = run_validate(data_root, only_new=True, state_path=state, now=3000.0)
    assert third.checked == 1

    os.utime(path, (3000.0, 3000.0))
    fourth = run_validate(data_root, only_new=True, state_path=state, now=4000.0)
    assert fourth.checked == 1


def test_render_is_byte_stable() -> None:
    analysis = Analysis.model_validate(_analysis())
    text = render_analysis(analysis)
    assert text.endswith("}\n")
    assert ", " not in text
    assert render_analysis(Analysis.model_validate(json.loads(text))) == text


def test_check_against_cluster_refuses_a_mismatched_cluster() -> None:
    other = _cluster(id="2026-09-16-ffffff")
    problems = check_against_cluster(Analysis.model_validate(_analysis()), other)
    assert any("checked against" in problem for problem in problems)


# --- the loop: pending -> analysis -> validate -> off the queue -----------


def test_a_valid_analysis_takes_the_cluster_off_the_queue(tmp_path: Path) -> None:
    """T32's loop, end to end: `nc pending` lists it, an analysis
    validates, and it is gone from both the status and the mirror."""
    data_root = DataRoot(tmp_path / "data")
    _write_cluster(data_root, _cluster())
    pending_path(data_root, CLUSTER_ID).parent.mkdir(parents=True, exist_ok=True)
    pending_path(data_root, CLUSTER_ID).write_text(render(_cluster()), "utf-8")
    _write_analysis(data_root, _analysis())

    assert [c.id for c in pending_clusters(data_root)] == [CLUSTER_ID]

    report = run_validate(data_root, state_path=tmp_path / "state.json")

    assert (report.valid, report.marked_analyzed) == (1, 1)
    assert pending_clusters(data_root) == []
    assert not pending_path(data_root, CLUSTER_ID).exists()
    stored = json.loads(
        (data_root.resolve("clusters", "2026-09-17", f"{CLUSTER_ID}.json")).read_text()
    )
    assert stored["status"] == STATUS_ANALYZED


def test_a_rejected_analysis_puts_the_cluster_back_on_the_queue(
    tmp_path: Path,
) -> None:
    """Otherwise a cluster whose analysis was thrown out would sit
    marked `analyzed` with nothing analysing it."""
    data_root = DataRoot(tmp_path / "data")
    analyzed = _cluster(status=STATUS_ANALYZED)
    _write_cluster(data_root, analyzed)
    _write_analysis(data_root, _analysis(cluster_version=1))

    assert pending_clusters(data_root) == []

    report = run_validate(data_root, state_path=tmp_path / "state.json")

    assert report.requeued == 1
    assert [c.id for c in pending_clusters(data_root)] == [CLUSTER_ID]
    assert pending_path(data_root, CLUSTER_ID).exists()


def test_validating_twice_writes_nothing_the_second_time(tmp_path: Path) -> None:
    """The idempotence obligation every writer in this project carries:
    a cron fires at the data repo and a re-run with nothing new to say
    must produce no diff."""
    data_root = DataRoot(tmp_path / "data")
    _write_cluster(data_root, _cluster())
    _write_analysis(data_root, _analysis())
    state = tmp_path / "state.json"

    run_validate(data_root, state_path=state)
    path = data_root.resolve("clusters", "2026-09-17", f"{CLUSTER_ID}.json")
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns

    second = run_validate(data_root, state_path=state)

    assert second.marked_analyzed == 0
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_a_superseded_cluster_is_never_marked_analyzed(tmp_path: Path) -> None:
    """Its analysis is history by definition, so claiming otherwise
    would make a superseded cluster look current."""
    data_root = DataRoot(tmp_path / "data")
    cluster = _cluster(status=STATUS_SUPERSEDED, superseded_by="2026-09-16-ffffff")
    assert set_cluster_status(data_root, cluster, STATUS_ANALYZED) is None


def test_pending_is_oldest_first(tmp_path: Path) -> None:
    """A story is worth least once it is a day old, so an agent that
    runs out of budget should have spent it on today's news."""
    data_root = DataRoot(tmp_path / "data")
    older = _cluster(
        id="2026-09-16-aaaaaa",
        items=(
            _member(
                VERGE,
                "theverge",
                "Older story",
                "Filed yesterday.",
                published="2026-09-16T08:00:00Z",
            ),
            _member(
                ARS,
                "arstechnica",
                "Older story too",
                "Also yesterday.",
                published="2026-09-16T09:00:00Z",
            ),
        ),
    )
    _write_cluster(data_root, _cluster())
    _write_cluster(data_root, older)

    assert [c.id for c in pending_clusters(data_root)] == [older.id, CLUSTER_ID]


def test_the_shipped_prompt_states_the_rules_the_validator_enforces() -> None:
    """The skill and the API backend share this file. If it stops
    naming a rule, both backends start failing on it at once."""
    prompt = Path("prompts/analyze.md").read_text(encoding="utf-8")
    for needle in ("verbatim", "15 WORDS", "80 WORDS", "at least two", "item_id"):
        assert needle in prompt, needle


# --- a misfiled analysis --------------------------------------------------


def test_a_misfiled_analysis_cannot_retire_the_cluster_it_names(
    tmp_path: Path,
) -> None:
    """`run_validate` keys the status update on the *filename*, and that
    is deliberate.

    An analysis whose embedded `cluster_id` disagrees with its filename
    is rejected outright -- `check_against_cluster` compares the two and
    returns on the mismatch before anything else -- so no analysis can
    ever be marked analyzed against a cluster it does not name. What the
    filename decides is only which cluster goes *back on the queue*, and
    the file-named one is the right answer there: its analysis slot was
    occupied and has just been emptied.

    Keying the status on `analysis.cluster_id` instead would be the
    actual bug: it would requeue the cluster the file claims while
    leaving the file-named one marked `analyzed` with no analysis on
    disk at all.
    """
    data_root = DataRoot(tmp_path / "data")
    # `occupant` starts *analyzed*, so the requeue is a real transition
    # rather than a no-op: asserting `pending` against a cluster that
    # was already pending would pass with the requeue deleted.
    occupant = _cluster(status=STATUS_ANALYZED)
    claimed = _cluster(id="2026-09-17-def456", status=STATUS_ANALYZED)
    _write_cluster(data_root, occupant)
    _write_cluster(data_root, claimed)

    # An analysis for `claimed`, written into `occupant`'s filename.
    path = analysis_path(data_root, occupant.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_analysis(cluster_id=claimed.id)), encoding="utf-8")

    report = run_validate(data_root, state_path=tmp_path / "state.json")

    assert report.valid == 0
    assert [cluster_id for cluster_id, _ in report.rejected] == [occupant.id]
    assert not path.exists()

    # The cluster whose slot was emptied is back on the queue.
    assert report.requeued == 1
    assert _status(data_root, occupant) == STATUS_PENDING
    # The cluster the file merely *claimed* is untouched: nothing has
    # validly analysed it, and nothing here pretended otherwise.
    assert _status(data_root, claimed) == STATUS_ANALYZED


def _status(data_root: DataRoot, cluster: Cluster) -> str:
    stored = json.loads(
        data_root.resolve("clusters", cluster.date, f"{cluster.id}.json").read_text(
            encoding="utf-8"
        )
    )
    return str(stored["status"])
