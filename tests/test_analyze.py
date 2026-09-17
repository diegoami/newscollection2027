"""Tests for T31's `nc.analyze` (`nc analyze --backend api`).

**Nothing here touches the network or spends a cent.** `AnalysisBackend`
is a one-method protocol precisely so the parts worth testing -- the
schema the model is asked for, the fields the code fills in rather than
trusting, the retry, the reporting -- run against a stub. The SDK call
itself is four lines in `AnthropicBackend` and is the only thing a real
key would exercise.

The fixture cluster and its valid analysis are the same shape as
tests/test_contract.py's, deliberately: this module is about getting a
model's output *into* that contract, and reusing the shape keeps the two
files describing one thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nc.analyze import (
    AnalyzeConfig,
    AnalyzeReport,
    analyze_cluster,
    format_analyze_report,
    load_analyze_config,
    load_prompt,
    render_cluster,
    response_schema,
    retry_prompt,
    run_analyze,
)
from nc.cluster import STATUS_PENDING, Cluster, ClusterItem, render
from nc.contract import analysis_path, rejected_path
from nc.store import DataRoot

VERGE = "a" * 40
ARS = "b" * 40
CLUSTER_ID = "2026-09-17-abc123"

CONFIG = AnalyzeConfig(
    model="claude-sonnet-5", max_tokens=16000, effort="medium", max_clusters_per_run=25
)


def _cluster() -> Cluster:
    return Cluster(
        id=CLUSTER_ID,
        version=2,
        anchor=VERGE,
        status=STATUS_PENDING,
        items=(
            ClusterItem(
                item_id=VERGE,
                outlet="theverge",
                title="Acme ships the Widget 4",
                lede="The company said the part ships in the first quarter.",
                published="2026-09-17T10:00:00Z",
            ),
            ClusterItem(
                item_id=ARS,
                outlet="arstechnica",
                title="Acme announces Widget 4 for Q1",
                lede="Acme said on Tuesday that the Widget 4 will cost $499.",
                published="2026-09-17T11:00:00Z",
            ),
        ),
    )


def _model_output(**overrides: Any) -> dict[str, Any]:
    """What the model returns: the judgement only, with none of the five
    fields `nc.analyze` fills in itself."""
    base: dict[str, Any] = {
        "headline": "Acme ships the Widget 4",
        "summary": "Two outlets report the Widget 4 ships in the first quarter.",
        "claims": [
            {
                "id": "c1",
                "statement": "The Widget 4 ships in the first quarter.",
                "sources": [
                    {
                        "outlet": "theverge",
                        "item_id": VERGE,
                        "quote": "ships in the first quarter",
                    },
                    {
                        "outlet": "arstechnica",
                        "item_id": ARS,
                        "quote": "Acme announces Widget 4 for Q1",
                    },
                ],
            }
        ],
        "discrepancies": [],
        "agreement": "full",
    }
    base.update(overrides)
    return base


class _Stub:
    """An `AnalysisBackend` that replays scripted answers and records
    what it was asked. No network, no key, no spend."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def complete(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((system, user, schema))
        return self._answers.pop(0) if self._answers else _model_output()


def _write_cluster(data_root: DataRoot, cluster: Cluster) -> None:
    path = data_root.resolve("clusters", cluster.date, f"{cluster.id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(cluster), encoding="utf-8")


# --- the schema the model is asked for ------------------------------------


def test_the_request_schema_drops_what_the_code_fills_in() -> None:
    """A model that guesses `cluster_version` invents stale analyses,
    and one that writes its own `model` field fabricates the provenance
    `nc bench-*` reads. Neither is asked for."""
    schema = response_schema()
    for field in (
        "cluster_id",
        "cluster_version",
        "backend",
        "model",
        "generated_at",
    ):
        assert field not in schema["properties"], field
        assert field not in schema["required"], field


def test_the_request_schema_keeps_everything_the_model_decides() -> None:
    schema = response_schema()
    for field in ("headline", "summary", "claims", "discrepancies", "agreement"):
        assert field in schema["properties"], field
    assert schema["additionalProperties"] is False


def test_the_request_schema_has_no_refs_left() -> None:
    """`$defs` are inlined rather than passed as `$ref`s: structured
    outputs take a JSON Schema subset, and this costs a few lines to
    stop depending on `$ref` being part of it."""
    assert "$ref" not in json.dumps(response_schema())
    assert "$defs" not in response_schema()


def test_the_request_schema_is_derived_not_restated(tmp_path: Path) -> None:
    """A field added to the published contract appears in the request
    automatically -- there is no second copy to update."""
    custom = tmp_path / "schema.json"
    custom.write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["cluster_id", "headline", "invented_field"],
                "properties": {
                    "cluster_id": {"type": "string"},
                    "headline": {"type": "string"},
                    "invented_field": {"type": "string"},
                },
            }
        )
    )
    schema = response_schema(custom)
    assert "invented_field" in schema["properties"]
    assert "cluster_id" not in schema["properties"]


# --- what the model is shown ----------------------------------------------


def test_the_cluster_is_rendered_verbatim() -> None:
    """Every quote the model returns is checked against these exact
    strings, so a rendering that tidied them would make the contract
    unsatisfiable."""
    rendered = render_cluster(_cluster())
    for item in _cluster().items:
        assert item.title in rendered
        assert item.lede in rendered
        assert item.item_id in rendered
        assert item.outlet in rendered


def test_the_prompt_is_the_one_the_skill_reads() -> None:
    assert load_prompt() == Path("prompts/analyze.md").read_text(encoding="utf-8")


# --- the fields the code fills in -----------------------------------------


def test_the_code_stamps_the_five_fields_itself() -> None:
    analysis, problems, retried = analyze_cluster(
        _cluster(),
        _Stub(_model_output()),
        CONFIG,
        "prompt",
        response_schema(),
        now="2026-09-17T12:00:00Z",
    )
    assert problems == []
    assert analysis is not None
    assert analysis.cluster_id == CLUSTER_ID
    assert analysis.cluster_version == 2
    assert analysis.backend == "api"
    assert analysis.model == "claude-sonnet-5"
    assert analysis.generated_at == "2026-09-17T12:00:00Z"
    assert retried is False


def test_a_model_that_returns_the_wrong_cluster_id_cannot_win() -> None:
    """Even if the model invents one, the code's value is what is
    written -- the stamp is applied after, not merged before."""
    analysis, problems, _ = analyze_cluster(
        _cluster(),
        _Stub(_model_output(cluster_id="2026-01-01-ffffff")),
        CONFIG,
        "prompt",
        response_schema(),
        now="2026-09-17T12:00:00Z",
    )
    assert problems == []
    assert analysis is not None
    assert analysis.cluster_id == CLUSTER_ID


# --- the retry ------------------------------------------------------------


def test_a_rejected_analysis_is_retried_once_with_every_reason() -> None:
    bad = _model_output(
        claims=[
            {
                "id": "c1",
                "statement": "Made up.",
                "sources": [
                    {
                        "outlet": "theverge",
                        "item_id": VERGE,
                        "quote": "a sentence nobody published",
                    }
                ],
            }
        ],
        headline=" ".join(["word"] * 16),
    )
    stub = _Stub(bad, _model_output())

    analysis, problems, retried = analyze_cluster(
        _cluster(), stub, CONFIG, "prompt", response_schema()
    )

    assert retried is True
    assert problems == []
    assert analysis is not None
    assert len(stub.calls) == 2
    # The second call carries the whole list, not the first fault only.
    second_user = stub.calls[1][1]
    assert "not verbatim" in second_user
    assert "16 words" in second_user


def test_the_retry_is_not_a_loop() -> None:
    """Two attempts, then stop. A backend that cannot satisfy the
    contract twice has a prompt problem, and a third attempt is how a
    run turns into a bill."""
    bad = _model_output(headline=" ".join(["word"] * 16))
    stub = _Stub(bad, bad, _model_output())

    analysis, problems, retried = analyze_cluster(
        _cluster(), stub, CONFIG, "prompt", response_schema()
    )

    assert len(stub.calls) == 2
    assert analysis is None
    assert problems
    assert retried is True


def test_retry_prompt_lists_every_problem() -> None:
    text = retry_prompt(["schema: a", "quote: b"])
    assert "- schema: a" in text
    assert "- quote: b" in text


# --- the run --------------------------------------------------------------


def test_run_analyze_writes_valid_analyses_and_leaves_status_alone(
    tmp_path: Path,
) -> None:
    """`nc analyze` never marks a cluster analyzed: that is
    `nc validate`'s decision and CLAUDE.md gives it one home."""
    data_root = DataRoot(tmp_path / "data")
    _write_cluster(data_root, _cluster())

    report = run_analyze(data_root, _Stub(_model_output()), CONFIG)

    assert (report.attempted, report.written, report.failed) == (1, 1, [])
    written = json.loads(analysis_path(data_root, CLUSTER_ID).read_text())
    assert written["cluster_id"] == CLUSTER_ID
    stored = json.loads(
        data_root.resolve("clusters", "2026-09-17", f"{CLUSTER_ID}.json").read_text()
    )
    assert stored["status"] == STATUS_PENDING


def test_run_analyze_records_a_cluster_it_could_not_satisfy(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    _write_cluster(data_root, _cluster())
    bad = _model_output(headline=" ".join(["word"] * 16))

    report = run_analyze(data_root, _Stub(bad, bad), CONFIG)

    assert report.written == 0
    assert [cluster_id for cluster_id, _ in report.failed] == [CLUSTER_ID]
    assert rejected_path(data_root, CLUSTER_ID).exists()
    assert not analysis_path(data_root, CLUSTER_ID).exists()


def test_the_first_try_rate_counts_first_attempts() -> None:
    """T31's acceptance criterion. A backend that needs a retry every
    time still writes every file, so counting finished files would
    report 1.000 for a prompt that is failing every first attempt."""
    assert AnalyzeReport(attempted=4, written=4, retried=0).first_try_rate == 1.0
    assert AnalyzeReport(attempted=4, written=4, retried=4).first_try_rate == 0.0
    assert AnalyzeReport(attempted=4, written=3, retried=1).first_try_rate == 0.5
    assert AnalyzeReport(attempted=0, written=0, retried=0).first_try_rate == 0.0
    assert "first-try rate" in format_analyze_report(
        AnalyzeReport(attempted=1, written=1, retried=0)
    )


# --- the shipped config ---------------------------------------------------


def test_the_shipped_config_loads() -> None:
    config = load_analyze_config()
    assert config.model
    assert config.max_tokens > 0
    assert config.max_clusters_per_run > 0


def test_a_bad_effort_fails_at_load_not_at_the_api(tmp_path: Path) -> None:
    """Otherwise a typo comes back as a 400 from the first request of a
    batch run, after the money is committed."""
    path = tmp_path / "analyze.yaml"
    path.write_text(
        "model: claude-sonnet-5\nmax_tokens: 100\neffort: meduim\n"
        "max_clusters_per_run: 5\n"
    )
    with pytest.raises(ValueError, match="effort must be one of"):
        load_analyze_config(path)
