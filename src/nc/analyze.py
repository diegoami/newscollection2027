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
from typing import Any, Literal, NamedTuple, Protocol

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
from nc.store import DataRoot, write_text

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


def _take(limit: int | None, default: int) -> int:
    """How many clusters this run may take.

    `limit or default` read well and was wrong for one value: `--limit
    0` is falsy, so asking for nothing silently got the config default
    instead -- a command that spends money doing the opposite of what it
    was told. `nc.judge` already spells this out with an explicit `is
    None`; this is the same check in the one place both analyze paths
    can share.
    """
    return default if limit is None else limit


@dataclass(frozen=True)
class AnalyzeReport:
    attempted: int
    written: int
    retried: int
    first_try: int = 0
    failed: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def first_try_rate(self) -> float:
        """T31's acceptance criterion: the share that passed without a
        retry. Counted on first attempts only, because a backend that
        needs a second pass every time is a backend with a prompt
        problem, however green the final number looks.

        **Counted, not derived.** This was `(written - retried) /
        attempted`, which quietly assumes every retried cluster
        eventually wrote. A cluster that failed *after* its retry is
        counted in `retried` and not in `written`, so each one moved the
        rate down by a whole cluster: four attempted with three clean
        passes and one retried failure reported 0.500 instead of 0.750,
        and a single cluster failing after a retry reported -1.000. A
        negative rate is not a number anyone can act on, and this is the
        one figure T31 is judged by.
        """
        if not self.attempted:
            return 0.0
        return self.first_try / self.attempted


class AttemptResult(NamedTuple):
    """What one cluster's trip through the backend produced.

    `payload` is the last thing the model returned, valid or not, so the
    caller can file a rejection with the output in it.
    """

    analysis: Analysis | None
    problems: list[str]
    retried: bool
    payload: dict[str, Any] | None


def analyze_cluster(
    cluster: Cluster,
    backend: AnalysisBackend,
    config: AnalyzeConfig,
    prompt: str,
    schema: dict[str, Any],
    now: str | None = None,
) -> AttemptResult:
    """One cluster, one retry.

    The five code-filled fields are stamped here rather than trusted
    from the model -- see the module docstring.

    The last payload is handed back alongside the verdict so a
    rejection can be filed with the analysis that earned it. `rejected/`
    exists to hold the reasons *and* the output together (see
    `nc.contract.write_rejection`); a rejection carrying only reasons
    tells a prompt-tuning pass that something was wrong but not what the
    model actually wrote, which is the evidence it needs.
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
    payload = stamp(backend.complete(prompt, user, schema))
    analysis, problems = validate_analysis(payload, cluster)
    if not problems:
        return AttemptResult(analysis, [], retried=False, payload=payload)

    retry_user = f"{user}\n{retry_prompt(problems)}\n"
    payload = stamp(backend.complete(prompt, retry_user, schema))
    analysis, problems = validate_analysis(payload, cluster)
    return AttemptResult(
        analysis if not problems else None, problems, retried=True, payload=payload
    )


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
    queue = pending_clusters(data_root)[: _take(limit, config.max_clusters_per_run)]

    attempted = written = retried = first_try = 0
    failed: list[tuple[str, list[str]]] = []
    for cluster in queue:
        attempted += 1
        result = analyze_cluster(cluster, backend, config, prompt, schema, now)
        retried += result.retried
        if result.analysis is None:
            failed.append((cluster.id, result.problems))
            write_rejection(data_root, cluster.id, result.problems, result.payload)
            continue
        if not result.retried:
            first_try += 1
        path = analysis_path(data_root, cluster.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, render_analysis(result.analysis))
        written += 1
    return AnalyzeReport(
        attempted=attempted,
        written=written,
        retried=retried,
        first_try=first_try,
        failed=failed,
    )


def format_analyze_report(report: AnalyzeReport) -> str:
    lines = [
        f"analyze: {report.attempted} cluster(s) attempted, "
        f"{report.written} written, {len(report.failed)} failed",
        f"analyze: first-try rate {report.first_try_rate:.3f} "
        f"({report.first_try} of {report.attempted} passed first time, "
        f"{report.retried} needed a retry)",
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
    cluster_versions: dict[str, int] = field(default_factory=dict)


def submission_path(data_root: DataRoot, batch_id: str) -> Path:
    return data_root.batches_dir() / f"{batch_id}.json"


def write_submission(data_root: DataRoot, submission: BatchSubmission) -> Path:
    """Record which cluster versions went into a batch.

    A batch can take 24 hours, and a cluster's membership can change in
    that time. Nothing else on disk remembers what was sent, so without
    this file the collect has no way to tell an analysis written for the
    old membership from one written for the new.
    """
    path = submission_path(data_root, submission.batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(
        path,
        json.dumps(
            {
                "batch_id": submission.batch_id,
                "cluster_versions": submission.cluster_versions,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
    )
    return path


def read_submission(data_root: DataRoot, batch_id: str) -> dict[str, int] | None:
    """The `cluster_id -> version` map a submission recorded, or None.

    None means the record is missing or unreadable -- a batch submitted
    before this file existed, or one submitted from another checkout.
    The caller decides what to do about it; it does not get to be
    mistaken for "every version still matches".
    """
    path = submission_path(data_root, batch_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    versions = raw.get("cluster_versions") if isinstance(raw, dict) else None
    if not isinstance(versions, dict):
        return None
    return {str(k): int(v) for k, v in versions.items()}


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
    queue = pending_clusters(data_root)[: _take(limit, config.max_clusters_per_run)]

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
    submission = BatchSubmission(
        batch_id=batch.id,
        cluster_ids=[cluster.id for cluster in queue],
        cluster_versions={cluster.id: cluster.version for cluster in queue},
    )
    write_submission(data_root, submission)
    return submission


def _batch_staleness(
    cluster_id: str,
    cluster: Cluster | None,
    sent_version: int | None,
    submitted: dict[str, int] | None,
) -> list[str]:
    """Whether this result still answers for the cluster that was sent.

    Two ways it does not, and both are refusals rather than guesses:
    the submission record is missing, so what was sent is unknowable;
    or the cluster has moved to a new version since, so the analysis
    describes a membership that is no longer the story. Re-submitting a
    batch costs half-price tokens and a wait. Publishing a story that
    omits an outlet that has since joined it costs the thing the site
    is for.
    """
    if submitted is None:
        return [
            f"batch: no submission record for this batch, so the cluster "
            f"version {cluster_id} was analysed at is unknown; re-submit it"
        ]
    if sent_version is None:
        return [f"batch: {cluster_id} is not in this batch's submission record"]
    if cluster is not None and cluster.version != sent_version:
        return [
            f"cluster: analysis is for version {sent_version}, cluster is at "
            f"version {cluster.version}; its membership changed while the "
            "batch was running"
        ]
    return []


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

    **The version comes from the submission record, not from the
    cluster as it is now.** Re-reading `cluster.version` at collect time
    stamped the *current* version onto an analysis written against the
    membership of up to a day ago: if an outlet joined the cluster in
    the meantime, that analysis passed the validator and was published
    as current while missing the outlet that arrived. `cluster_version`
    is the one thing that makes a stale analysis unpublishable, so the
    version that was sent is what it has to carry; a cluster that moved
    is rejected here and stays on the queue.
    """
    moment = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if now is None else now
    attempted = written = 0
    failed: list[tuple[str, list[str]]] = []
    clusters = {cluster.id: cluster for cluster in pending_clusters(data_root)}
    submitted = read_submission(data_root, batch_id)

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
        sent_version = None if submitted is None else submitted.get(cluster_id)
        payload = {
            **json.loads(text),
            "cluster_id": cluster_id,
            "cluster_version": (
                sent_version
                if sent_version is not None
                else (cluster.version if cluster else 0)
            ),
            "backend": BACKEND_API,
            "model": config.model,
            "generated_at": moment,
        }
        problems = _batch_staleness(cluster_id, cluster, sent_version, submitted)
        if problems:
            failed.append((cluster_id, problems))
            write_rejection(data_root, cluster_id, problems, payload)
            continue
        analysis, problems = validate_analysis(payload, cluster)
        if problems or analysis is None:
            failed.append((cluster_id, problems))
            write_rejection(data_root, cluster_id, problems, payload)
            continue
        path = analysis_path(data_root, cluster_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, render_analysis(analysis))
        written += 1
    return AnalyzeReport(
        attempted=attempted,
        written=written,
        retried=0,
        first_try=written,
        failed=failed,
    )
