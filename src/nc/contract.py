"""T30: the analysis contract, and the validator that is the only way in.

CLAUDE.md: "The LLM step is a file contract: `pending/*.json` in the
data root in, `analyses/<date>/*.json` out, `nc validate` decides. Never
bypass the validator." This module is that decision. Nothing else in the
pipeline may conclude that an analysis is usable.

**Why three layers rather than one.** An analysis can be wrong in three
different ways, and no single tool catches all of them:

1. *Shape* -- `contract/analysis.schema.json`, checked with
   `jsonschema`. That file is the published contract: the analyze skill
   quotes it, docs/ARCHITECTURE.md reproduces it, and T31 will hand it
   to the SDK as a structured-output schema. It stays authoritative, and
   this module reads it from disk rather than restating it.
2. *Types* -- the pydantic models below, which give the rest of the code
   (and T31's SDK call) a typed object instead of a raw dict.
3. *Truth* -- `check_against_cluster`, which is the part that actually
   matters and the part neither of the first two can do. A schema can
   say `quote` is a string of 3 to 400 characters. Only this layer can
   say the string appears verbatim in the item it claims to come from.

Layers 1 and 2 describe the same shape twice, which is a drift risk, so
`tests/test_contract.py` runs every sample in the corpus through both
and asserts they agree on every accept and every reject. Two sources of
truth are tolerable only when something checks they still say the same
thing.

**Why the word limits are here and not in the schema.**
docs/ARCHITECTURE.md specifies "headline: neutral, <= 15 words" and
"summary: <= 80 words". JSON Schema cannot count words, so the schema
file approximates them as character caps (120 and 600). Those caps are a
guard rail, not the contract; the word counts are, and they are enforced
in layer 3. An analysis passing the schema is therefore not yet valid --
which is exactly why `nc validate` exists rather than a bare
`jsonschema` call.

**Why a reject is a file and not an exception.** docs/PLAN.md T30:
"rejects moved to `data/rejected/<id>.json` with reasons". A rejected
analysis is evidence: it is what T60's prompt tuning reads, what the
analyze skill re-reads to fix its own output, and what tells a human
whether a backend is failing on the schema or on the quotes. Raising
would lose the analysis and the reason together. Every reason is
collected, not just the first, so one round trip through the agent can
fix everything wrong with a file rather than one thing per attempt.

**Every quote carries outlet, item id and verbatim text** (CLAUDE.md,
"No exceptions"). `_normalize_whitespace` is the only latitude given: a
feed's lede may wrap differently than the model reproduced it, so runs
of whitespace collapse before comparison. Nothing else is forgiven --
not an ellipsis, not a changed dash, not a "[sic]". A quote that does
not appear in the item's title or lede is a fabrication, and the site's
whole claim to be checkable rests on this check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nc.cluster import (
    STATUS_ANALYZED,
    STATUS_PENDING,
    STATUS_SUPERSEDED,
    Cluster,
    ClusterItem,
    load_clusters,
    set_cluster_status,
)
from nc.store import DataRoot

DEFAULT_SCHEMA_PATH = Path("contract/analysis.schema.json")

BACKEND_CLAUDE_CODE = "claude_code"
BACKEND_API = "api"

# docs/ARCHITECTURE.md's Analysis model, in words rather than the
# schema's character approximation. See the module docstring.
MAX_HEADLINE_WORDS = 15
MAX_SUMMARY_WORDS = 80


# --- the typed model ------------------------------------------------------


class Quote(BaseModel):
    """One outlet's own words, with the item they came from.

    The three fields travel together everywhere in this project because
    a quote without its item id cannot be checked and a quote without
    its outlet cannot be attributed. CLAUDE.md makes that a rule with no
    exceptions; this type is how the code keeps it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outlet: str = Field(min_length=1)
    item_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    quote: str = Field(min_length=3, max_length=400)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^c\d{1,2}$")
    statement: str = Field(min_length=1, max_length=300)
    sources: tuple[Quote, ...] = Field(min_length=1)


class Discrepancy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["contradiction", "number", "attribution", "framing", "omission"]
    severity: Literal["low", "medium", "high"]
    explanation: str = Field(min_length=1, max_length=300)
    # At least two, because a discrepancy is a disagreement and one
    # outlet cannot disagree with itself. That they come from two
    # *different* outlets is layer 3's job: the count is structural, the
    # distinctness needs the cluster.
    quotes: tuple[Quote, ...] = Field(min_length=2)
    claim_ids: tuple[str, ...] = ()


class Analysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cluster_id: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{6}$")
    cluster_version: int = Field(ge=1)
    backend: Literal["api", "claude_code"]
    model: str = Field(min_length=1)
    generated_at: str
    headline: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=600)
    claims: tuple[Claim, ...] = Field(min_length=1, max_length=20)
    discrepancies: tuple[Discrepancy, ...] = Field(max_length=10)
    agreement: Literal["full", "partial", "conflicting"]
    notes: str | None = Field(default=None, max_length=500)

    @property
    def all_quotes(self) -> tuple[Quote, ...]:
        quotes = [quote for claim in self.claims for quote in claim.sources]
        for discrepancy in self.discrepancies:
            quotes.extend(discrepancy.quotes)
        return tuple(quotes)


# --- paths ----------------------------------------------------------------


def analyses_dir(data_root: DataRoot) -> Path:
    return data_root.resolve("analyses")


def analysis_path(data_root: DataRoot, cluster_id: str) -> Path:
    """`analyses/<date>/<cluster id>.json` -- the date is the cluster
    id's own prefix, so the path is a function of the id alone and a
    reader never has to guess which day a cluster was filed under."""
    return analyses_dir(data_root) / cluster_id[:10] / f"{cluster_id}.json"


def rejected_dir(data_root: DataRoot) -> Path:
    return data_root.resolve("rejected")


def rejected_path(data_root: DataRoot, cluster_id: str) -> Path:
    return rejected_dir(data_root) / f"{cluster_id}.json"


def render_analysis(analysis: Analysis) -> str:
    """Byte-stable, like every other file this project writes: sorted
    keys, no spaces, one trailing newline, so an unchanged analysis
    rewritten is byte-identical and git sees nothing to commit."""
    payload = analysis.model_dump(mode="json", exclude_none=True)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


# --- layer 1: the published schema ----------------------------------------


@lru_cache(maxsize=4)
def load_schema(path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    """`contract/analysis.schema.json`, cached.

    Read from disk rather than restated in Python: it is the artifact
    the skill, the docs and T31's structured-output call all point at,
    and a copy in code would be a second thing to keep in step.
    """
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a JSON object at the top level")
    return loaded


def schema_errors(payload: object, path: Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    """Every way `payload` fails the published schema, not just the
    first: an agent fixing one problem per round trip is an agent making
    ten round trips."""
    validator = jsonschema.Draft202012Validator(load_schema(path))
    return [
        f"schema: {error.message}"
        + (
            f" (at {'/'.join(str(p) for p in error.absolute_path)})"
            if error.absolute_path
            else ""
        )
        for error in sorted(validator.iter_errors(payload), key=str)
    ]


# --- layer 2: the typed model ---------------------------------------------


def model_errors(payload: object) -> tuple[Analysis | None, list[str]]:
    """Parse into `Analysis`, or report why not.

    Returns the parsed object so a caller does not parse twice; `None`
    with a non-empty list means layer 3 cannot run at all, which is why
    `validate_analysis` stops there rather than reporting cascading
    nonsense about a document it could not read.
    """
    try:
        return Analysis.model_validate(payload), []
    except ValidationError as exc:
        return None, [
            "model: "
            + (".".join(str(p) for p in error["loc"]) or "<root>")
            + f": {error['msg']}"
            for error in exc.errors()
        ]


# --- layer 3: against the cluster it claims to analyse --------------------


def _normalize_whitespace(text: str) -> str:
    """The only latitude a quote is given.

    A feed's lede arrives with whatever wrapping, non-breaking spaces
    and newlines the outlet's CMS produced, and a model reproducing it
    faithfully may still normalize those. Collapsing runs of whitespace
    on both sides compares the words, which is what "verbatim" is
    protecting. Nothing else is forgiven -- see the module docstring.
    """
    return " ".join(text.split())


def _word_count(text: str) -> int:
    return len(text.split())


def check_against_cluster(analysis: Analysis, cluster: Cluster) -> list[str]:
    """Every rule docs/ARCHITECTURE.md's validator paragraph names that
    the schema cannot express. The list is exhaustive and in one place
    on purpose: a rule enforced somewhere else is a rule that stops
    being enforced when that somewhere else is refactored.
    """
    problems: list[str] = []

    if analysis.cluster_id != cluster.id:
        problems.append(
            f"cluster: analysis is for {analysis.cluster_id}, checked against "
            f"{cluster.id}"
        )
        return problems

    if cluster.status == STATUS_SUPERSEDED:
        problems.append(
            f"cluster: {cluster.id} is superseded by {cluster.superseded_by}; "
            "an analysis written against it is history, not current"
        )
    if analysis.cluster_version != cluster.version:
        problems.append(
            f"cluster: analysis is for version {analysis.cluster_version}, "
            f"cluster is at version {cluster.version}"
        )

    members: dict[str, ClusterItem] = {item.item_id: item for item in cluster.items}

    for quote in analysis.all_quotes:
        item = members.get(quote.item_id)
        if item is None:
            problems.append(
                f"quote: item {quote.item_id} is not in cluster {cluster.id}"
            )
            continue
        if quote.outlet != item.outlet:
            problems.append(
                f"quote: item {quote.item_id} is {item.outlet}, "
                f"attributed to {quote.outlet}"
            )
        haystack = _normalize_whitespace(f"{item.title} {item.lede}")
        title_only = _normalize_whitespace(item.title)
        lede_only = _normalize_whitespace(item.lede)
        needle = _normalize_whitespace(quote.quote)
        # Checked against title and lede separately as well as joined:
        # joined alone would accept a "quote" that straddles the two,
        # which is text no outlet ever published.
        if needle not in title_only and needle not in lede_only:
            where = "title or lede"
            if needle in haystack:
                where = "title or lede (it straddles the two)"
            problems.append(
                f"quote: not verbatim in {item.outlet} {quote.item_id}'s "
                f"{where}: {quote.quote!r}"
            )

    for index, discrepancy in enumerate(analysis.discrepancies):
        outlets = {quote.outlet for quote in discrepancy.quotes}
        if len(outlets) < 2:
            problems.append(
                f"discrepancy {index}: quotes span {len(outlets)} outlet(s); "
                "a disagreement needs at least two"
            )

    claim_ids = {claim.id for claim in analysis.claims}
    duplicate = len(analysis.claims) - len(claim_ids)
    if duplicate:
        problems.append(f"claims: {duplicate} duplicate claim id(s)")
    for index, discrepancy in enumerate(analysis.discrepancies):
        for claim_id in discrepancy.claim_ids:
            if claim_id not in claim_ids:
                problems.append(
                    f"discrepancy {index}: claim_ids names {claim_id}, "
                    "which is not a claim in this analysis"
                )

    headline_words = _word_count(analysis.headline)
    if headline_words > MAX_HEADLINE_WORDS:
        problems.append(
            f"headline: {headline_words} words, limit is {MAX_HEADLINE_WORDS}"
        )
    summary_words = _word_count(analysis.summary)
    if summary_words > MAX_SUMMARY_WORDS:
        problems.append(f"summary: {summary_words} words, limit is {MAX_SUMMARY_WORDS}")

    return problems


# --- the decision ---------------------------------------------------------


def validate_analysis(
    payload: object,
    cluster: Cluster | None,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> tuple[Analysis | None, list[str]]:
    """The one entry point. `(analysis, problems)`; valid means the list
    is empty.

    Layers run in order and layer 3 is skipped when layer 2 could not
    parse, because semantic complaints about a document that is not an
    analysis are noise an agent then has to read past. A missing cluster
    is itself a rejection: an analysis of a cluster that does not exist
    cannot be checked, and unverifiable is the same as invalid here.
    """
    problems = schema_errors(payload, schema_path)
    analysis, model_problems = model_errors(payload)
    problems.extend(model_problems)
    if analysis is None:
        return None, problems
    if cluster is None:
        problems.append(
            f"cluster: no cluster file for {analysis.cluster_id}; an analysis "
            "that cannot be checked against its cluster is not valid"
        )
        return analysis, problems
    problems.extend(check_against_cluster(analysis, cluster))
    return analysis, problems


def write_rejection(
    data_root: DataRoot,
    cluster_id: str,
    problems: list[str],
    payload: object,
    now: str | None = None,
) -> Path:
    """`rejected/<cluster id>.json`: the reasons and the analysis that
    earned them, kept together so a retry has both."""
    path = rejected_path(data_root, cluster_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    moment = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if now is None else now
    path.write_text(
        json.dumps(
            {
                "cluster_id": cluster_id,
                "rejected_at": moment,
                "problems": problems,
                "analysis": payload,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


# --- `nc validate` --------------------------------------------------------

# Where `--new` remembers the last run. Under .cache/ with the SQLite
# file, per CLAUDE.md: it is derived state about a run, not pipeline
# data, so it must not reach the data repository.
DEFAULT_VALIDATE_STATE_PATH = Path(".cache/validate-state.json")


@dataclass(frozen=True)
class ValidateReport:
    checked: int
    valid: int
    rejected: list[tuple[str, list[str]]]
    skipped_unchanged: int = 0
    marked_analyzed: int = 0
    requeued: int = 0


def find_analyses(data_root: DataRoot) -> list[Path]:
    return sorted(analyses_dir(data_root).rglob("*.json"))


def _last_run(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = raw.get("last_run_epoch") if isinstance(raw, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _record_run(path: Path, moment: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_run_epoch": moment}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_validate(
    data_root: DataRoot,
    only_new: bool = False,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    state_path: Path = DEFAULT_VALIDATE_STATE_PATH,
    now: float | None = None,
) -> ValidateReport:
    """Check every analysis on disk; move the failures to `rejected/`.

    A rejected analysis is *moved*, not copied: leaving it in
    `analyses/` would let `nc build` read an analysis the validator
    refused, which is precisely the bypass CLAUDE.md forbids. The file
    survives inside the rejection record, so nothing is lost and a retry
    has the text it needs to fix.

    `--new` (`only_new`) filters on file mtime against the last run,
    recorded in `.cache/`. Two honest caveats: a fresh clone gives every
    file the clone's mtime, so in CI this validates everything -- which
    is the safe direction, more work rather than a missed reject -- and
    a `.cache/` that has been wiped does the same. `--new` is a speed
    optimisation for a long-running session, never a correctness claim.
    """
    moment = datetime.now(UTC).timestamp() if now is None else now
    cutoff = _last_run(state_path) if only_new else None
    clusters = {cluster.id: cluster for cluster in load_clusters(data_root)}

    checked = valid = skipped = analyzed = requeued = 0
    rejected: list[tuple[str, list[str]]] = []

    for path in find_analyses(data_root):
        # `<`, not `<=`: a file written during the previous run has an
        # mtime at or just after that run's recorded start, and a tie
        # must re-check rather than skip. Over-validating costs a second
        # of work; under-validating ships an unchecked analysis.
        if cutoff is not None and path.stat().st_mtime < cutoff:
            skipped += 1
            continue
        checked += 1
        cluster_id = path.stem
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems = [f"file: {exc}"]
            payload = None
        else:
            _, problems = validate_analysis(
                payload, clusters.get(cluster_id), schema_path
            )
        cluster = clusters.get(cluster_id)
        if problems:
            write_rejection(data_root, cluster_id, problems, payload)
            path.unlink()
            rejected.append((cluster_id, problems))
            # Back on the queue: the analysis that was supposed to
            # answer for this cluster is gone, so leaving it `analyzed`
            # would retire a story nothing has actually analysed.
            if cluster is not None and set_cluster_status(
                data_root, cluster, STATUS_PENDING
            ):
                requeued += 1
        else:
            valid += 1
            # The only thing that takes a cluster off the queue, and it
            # happens here because only the validator knows the analysis
            # both exists and holds up.
            if cluster is not None and set_cluster_status(
                data_root, cluster, STATUS_ANALYZED
            ):
                analyzed += 1

    _record_run(state_path, moment)
    return ValidateReport(
        checked=checked,
        valid=valid,
        rejected=rejected,
        skipped_unchanged=skipped,
        marked_analyzed=analyzed,
        requeued=requeued,
    )


def format_validate_report(report: ValidateReport) -> str:
    lines = [
        f"validate: {report.checked} analysis file(s) checked, "
        f"{report.valid} valid, {len(report.rejected)} rejected"
    ]
    if report.skipped_unchanged:
        lines.append(
            f"validate: {report.skipped_unchanged} unchanged since the last run"
        )
    if report.marked_analyzed or report.requeued:
        lines.append(
            f"validate: {report.marked_analyzed} cluster(s) marked analyzed, "
            f"{report.requeued} put back on the queue"
        )
    for cluster_id, problems in report.rejected:
        lines.append(f"  {cluster_id} -> rejected/{cluster_id}.json")
        lines.extend(f"    {problem}" for problem in problems)
    return "\n".join(lines)
