"""T31: `nc analyze --backend api`, the SDK path into the same contract.

**What this is for, and what it is not.** docs/ARCHITECTURE.md's cost
model puts the nightly analysis on the Claude Code subscription: T32's
skill is the production backend and this one is not. This exists for
local development, backfills, and T33's eval -- the places where a
scripted, repeatable run matters more than the bill. The two write the
same files and `nc validate` cannot tell them apart, which is the whole
point of the file contract (CLAUDE.md).

**The model is asked for less than the file contains.** An analysis
carries five fields the model has no business choosing -- `cluster_id`,
`cluster_version`, `backend`, `model`, `generated_at` -- and every one
of them is knowable here for certain. So `response_schema` strips them
from the published schema before the request and this module fills them
in afterwards. That is not tidiness: a model that guesses
`cluster_version` invents stale analyses, and a model that writes its
own `model` field fabricates the provenance `nc bench-*` reads. Asking
only for the judgement removes a class of rejection the model could
cause but should never have been able to.

The subset is *derived* from `contract/analysis.schema.json` rather than
written out again, and `$defs` are inlined rather than passed as
`$ref`s, so the request carries exactly the shape the validator will
later enforce with nothing to keep in step by hand.

**Why the SDK call sits behind a protocol.** `AnalysisBackend` is one
method, mirroring `nc.embed`'s `EmbeddingBackend` for the same reason:
every test in this module runs against a stub, so the contract
plumbing, the retry path and the reporting are all exercised without a
network call or a cent of spend. `AnthropicBackend` is the only part
that needs an API key, and it is deliberately the thinnest thing in the
file.

**Rejections are retried once, with the reasons.** `nc validate`
already collects every problem in one pass, so a retry can hand the
model the whole list and ask for a corrected analysis rather than
discovering faults one round trip at a time -- the same argument the
skill makes to a human agent. One retry, not a loop: a backend that
cannot satisfy the contract in two attempts has a prompt problem, and
burning tokens on a third is how a nightly run turns into a bill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from nc.cluster import Cluster, pending_clusters
from nc.contract import (
    BACKEND_API,
    DEFAULT_SCHEMA_PATH,
    Analysis,
    analysis_path,
    load_schema,
    render_analysis,
    validate_analysis,
    write_rejection,
)
from nc.store import DataRoot

DEFAULT_ANALYZE_CONFIG_PATH = Path("config/analyze.yaml")
DEFAULT_PROMPT_PATH = Path("prompts/analyze.md")

# The five fields this module knows for certain and the model is never
# asked for. See the module docstring.
_MODEL_FILLED_BY_CODE = frozenset(
    {"cluster_id", "cluster_version", "backend", "model", "generated_at"}
)

# The API's own set. Kept as a Literal rather than a bare `str` so a typo
# in config/analyze.yaml fails at load, with the file named, instead of
# coming back as a 400 from the first request of a batch run.
Effort = Literal["low", "medium", "high", "xhigh", "max"]
_EFFORTS: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class AnalyzeConfig:
    model: str
    max_tokens: int
    effort: Effort
    max_clusters_per_run: int


def load_analyze_config(path: Path = DEFAULT_ANALYZE_CONFIG_PATH) -> AnalyzeConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    effort = str(raw["effort"])
    if effort not in _EFFORTS:
        raise ValueError(
            f"{path}: effort must be one of {', '.join(_EFFORTS)}, not {effort!r}"
        )
    return AnalyzeConfig(
        model=str(raw["model"]),
        max_tokens=int(raw["max_tokens"]),
        effort=effort,
        max_clusters_per_run=int(raw["max_clusters_per_run"]),
    )


def load_prompt(path: Path = DEFAULT_PROMPT_PATH) -> str:
    """The brief, shared verbatim with T32's skill. If the two ever read
    different text, `nc bench-*` is comparing two different questions."""
    return path.read_text(encoding="utf-8")


# --- the schema the model is asked for ------------------------------------


def _inline_refs(node: object, defs: dict[str, Any]) -> object:
    """Replace every `$ref: #/$defs/x` with the definition itself.

    Structured outputs accept a JSON Schema subset, and rather than
    depend on `$ref` being part of it, the request carries a fully
    inlined schema. The definitions are small (one `quote` object) and
    inlining is a few lines, so this buys robustness for nothing.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return _inline_refs(defs[ref.removeprefix("#/$defs/")], defs)
        return {key: _inline_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_refs(entry, defs) for entry in node]
    return node


def response_schema(schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    """The published contract minus what this module fills in itself.

    Derived, never restated: `contract/analysis.schema.json` stays the
    one description of an analysis, and this is a view of it. A field
    added to the contract appears here automatically; a field renamed
    there cannot silently keep its old name in the request.
    """
    schema = load_schema(schema_path)
    defs = schema.get("$defs", {})
    properties = {
        name: _inline_refs(value, defs)
        for name, value in schema["properties"].items()
        if name not in _MODEL_FILLED_BY_CODE
    }
    required = [
        name for name in schema["required"] if name not in _MODEL_FILLED_BY_CODE
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def render_cluster(cluster: Cluster) -> str:
    """One cluster as the model sees it: every item's outlet, id, time,
    title and lede, verbatim. Verbatim matters more here than anywhere
    else in this project -- every quote the model returns is checked
    against these exact strings, so a rendering that tidied them would
    make the contract unsatisfiable."""
    lines = [f"cluster_id: {cluster.id}", f"items: {len(cluster.items)}", ""]
    for index, item in enumerate(cluster.items, start=1):
        lines.extend(
            [
                f"--- item {index}",
                f"outlet: {item.outlet}",
                f"item_id: {item.item_id}",
                f"published: {item.published}",
                f"title: {item.title}",
                f"lede: {item.lede}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def retry_prompt(problems: list[str]) -> str:
    """What a rejected analysis is sent back with.

    Every problem at once, because `nc validate` found them all at once
    -- the same reason the skill tells a human agent to fix the whole
    list in one pass.
    """
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        "The analysis you produced was rejected by the validator for the "
        "following reasons:\n\n"
        f"{listed}\n\n"
        "Produce a corrected analysis of the same cluster. Fix every reason "
        "above. Quotes must be copied character for character from the item's "
        "title or lede as given."
    )


# --- backends -------------------------------------------------------------


class AnalysisBackend(Protocol):
    """Turns (system, user, schema) into whatever JSON object the model
    produced. One method, so every test in this module runs against a
    stub and the SDK is the only part that needs a key."""

    def complete(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...


class AnthropicBackend:
    """The Anthropic SDK, with structured outputs against `schema`.

    Imported lazily so `nc validate`, `nc pending` and the whole
    deterministic pipeline keep working in an environment with no
    `anthropic` package and no key -- which is every environment except
    a backfill or T33's eval.
    """

    def __init__(self, config: AnalyzeConfig) -> None:
        self._config = config
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def complete(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._get_client().messages.create(
            model=self._config.model,
            max_tokens=self._config.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": self._config.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        # Structured outputs guarantee one text block of valid JSON, but
        # a refusal stops before any of that -- check before indexing.
        if response.stop_reason == "refusal":
            raise RuntimeError(
                f"the model declined this cluster: {response.stop_details}"
            )
        text = next(block.text for block in response.content if block.type == "text")
        parsed: dict[str, Any] = json.loads(text)
        return parsed


# --- the run --------------------------------------------------------------


@dataclass(frozen=True)
class AnalyzeReport:
    attempted: int
    written: int
    retried: int
    failed: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def first_try_rate(self) -> float:
        """T31's acceptance criterion: the share that passed without a
        retry. Counted on first attempts only, because a backend that
        needs a second pass every time is a backend with a prompt
        problem, however green the final number looks."""
        if not self.attempted:
            return 0.0
        return (self.written - self.retried) / self.attempted


def analyze_cluster(
    cluster: Cluster,
    backend: AnalysisBackend,
    config: AnalyzeConfig,
    prompt: str,
    schema: dict[str, Any],
    now: str | None = None,
) -> tuple[Analysis | None, list[str], bool]:
    """One cluster, one retry. `(analysis, problems, retried)`.

    The five code-filled fields are stamped here rather than trusted
    from the model -- see the module docstring.
    """
    moment = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if now is None else now

    def stamp(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload,
            "cluster_id": cluster.id,
            "cluster_version": cluster.version,
            "backend": BACKEND_API,
            "model": config.model,
            "generated_at": moment,
        }

    user = render_cluster(cluster)
    analysis, problems = validate_analysis(
        stamp(backend.complete(prompt, user, schema)), cluster
    )
    if not problems:
        return analysis, [], False

    retry_user = f"{user}\n{retry_prompt(problems)}\n"
    analysis, problems = validate_analysis(
        stamp(backend.complete(prompt, retry_user, schema)), cluster
    )
    return (analysis if not problems else None), problems, True


def run_analyze(
    data_root: DataRoot,
    backend: AnalysisBackend,
    config: AnalyzeConfig,
    limit: int | None = None,
    prompt_path: Path = DEFAULT_PROMPT_PATH,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    now: str | None = None,
) -> AnalyzeReport:
    """Work the pending queue through the API backend.

    Writes analyses and nothing else: the cluster's status is
    `nc validate`'s to change, so this command is never the thing that
    decides a story has been analysed (CLAUDE.md).
    """
    prompt = load_prompt(prompt_path)
    schema = response_schema(schema_path)
    queue = pending_clusters(data_root)[: limit or config.max_clusters_per_run]

    attempted = written = retried = 0
    failed: list[tuple[str, list[str]]] = []
    for cluster in queue:
        attempted += 1
        analysis, problems, did_retry = analyze_cluster(
            cluster, backend, config, prompt, schema, now
        )
        retried += did_retry
        if analysis is None:
            failed.append((cluster.id, problems))
            write_rejection(data_root, cluster.id, problems, None)
            continue
        path = analysis_path(data_root, cluster.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_analysis(analysis), encoding="utf-8")
        written += 1
    return AnalyzeReport(
        attempted=attempted, written=written, retried=retried, failed=failed
    )


def format_analyze_report(report: AnalyzeReport) -> str:
    lines = [
        f"analyze: {report.attempted} cluster(s) attempted, "
        f"{report.written} written, {len(report.failed)} failed",
        f"analyze: first-try rate {report.first_try_rate:.3f} "
        f"({report.retried} needed a retry)",
    ]
    for cluster_id, problems in report.failed:
        lines.append(f"  {cluster_id} still invalid after one retry:")
        lines.extend(f"    {problem}" for problem in problems)
    return "\n".join(lines)


# --- `--batch`: the Message Batches API -----------------------------------
#
# Half price, asynchronous, and the right shape for the one job this
# backend actually has: a backfill or T33's eval over many clusters at
# once, where nothing is waiting on the answer. It is deliberately not
# wired into any nightly path -- a batch can take up to 24 hours, which
# is longer than the window a story is worth publishing in.


@dataclass(frozen=True)
class BatchSubmission:
    batch_id: str
    cluster_ids: list[str]


def submit_batch(
    data_root: DataRoot,
    config: AnalyzeConfig,
    client: Any,
    limit: int | None = None,
    prompt_path: Path = DEFAULT_PROMPT_PATH,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> BatchSubmission:
    """Queue the pending clusters as one batch; return its id.

    `custom_id` is the cluster id, which is what makes the results
    matchable: batch results come back in any order, so keying on
    position rather than `custom_id` would silently file analyses
    against the wrong stories.
    """
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    prompt = load_prompt(prompt_path)
    schema = response_schema(schema_path)
    queue = pending_clusters(data_root)[: limit or config.max_clusters_per_run]

    requests = [
        Request(
            custom_id=cluster.id,
            params=MessageCreateParamsNonStreaming(
                model=config.model,
                max_tokens=config.max_tokens,
                system=prompt,
                messages=[{"role": "user", "content": render_cluster(cluster)}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": config.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            ),
        )
        for cluster in queue
    ]
    batch = client.messages.batches.create(requests=requests)
    return BatchSubmission(
        batch_id=batch.id, cluster_ids=[cluster.id for cluster in queue]
    )


def collect_batch(
    data_root: DataRoot,
    config: AnalyzeConfig,
    client: Any,
    batch_id: str,
    now: str | None = None,
) -> AnalyzeReport:
    """Validate and write a finished batch's results.

    No retry here, unlike the synchronous path: a second batch would be
    another wait of up to 24 hours, so a rejected analysis is recorded
    in `rejected/` and left for a later run. The cluster stays on the
    queue by virtue of never having been marked analyzed.
    """
    moment = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if now is None else now
    attempted = written = 0
    failed: list[tuple[str, list[str]]] = []
    clusters = {cluster.id: cluster for cluster in pending_clusters(data_root)}

    for result in client.messages.batches.results(batch_id):
        attempted += 1
        cluster_id = result.custom_id
        cluster = clusters.get(cluster_id)
        if result.result.type != "succeeded":
            problems = [f"batch: {result.result.type}"]
            failed.append((cluster_id, problems))
            write_rejection(data_root, cluster_id, problems, None)
            continue
        text = next(
            block.text
            for block in result.result.message.content
            if block.type == "text"
        )
        payload = {
            **json.loads(text),
            "cluster_id": cluster_id,
            "cluster_version": cluster.version if cluster else 0,
            "backend": BACKEND_API,
            "model": config.model,
            "generated_at": moment,
        }
        analysis, problems = validate_analysis(payload, cluster)
        if problems or analysis is None:
            failed.append((cluster_id, problems))
            write_rejection(data_root, cluster_id, problems, payload)
            continue
        path = analysis_path(data_root, cluster_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_analysis(analysis), encoding="utf-8")
        written += 1
    return AnalyzeReport(attempted=attempted, written=written, retried=0, failed=failed)
