"""T33: the golden set and `nc eval`.

**Where the golden clusters come from, and why not from the pipeline.**
A golden set has to be right independently of the thing it measures. The
pipeline's own `clusters/` are only as good as `tau_low` and the judge,
so scoring analyses of them would fold two questions into one: did the
analysis go wrong, or was the cluster wrong to begin with?

`labels/pairs.jsonl` has a better answer already. T23's corpus holds 49
pairs a human confirmed report the same event, cross-outlet by
construction (`nc.labelling.labelling_pool` excludes same-outlet pairs)
and drawn from real feeds. Union-find over those positives gives 34
clusters of two to four outlets whose *membership is human-confirmed* --
exactly the property a golden set needs, and one no threshold or judge
had a say in.

**What is golden, and what the owner ruled is not.** The membership is
the owner's, via the labels. The *expected discrepancies* were mine, a
proposal for the owner to check in T34 -- and on 2026-09-18 the owner
checked all twenty and removed every one of them. The ruling, in the
owner's terms: articles about one news item are *supposed* to differ,
and showing that difference is the reason the site exists; naming in
advance which difference each cluster must yield is out of scope. So
`expected_discrepancies` is empty in every case and stays that way.

**What the eval measures now.** Only what `nc.contract` can decide
without an opinion: whether an analysis exists for each golden cluster,
whether it passes the validator, and whether its quotes are verbatim in
the items they cite. A backend's discrepancies are *counted and shown*
per case, never scored -- there is no expected answer to score against,
and reporting precision and recall against nothing would print 0.000
for a backend doing exactly what was asked. That failure mode has bitten
this report once already, when a missing analysis was scored the same as
a failed one.

**Two backends, one scorer** (docs/PLAN.md T33: `--backend api|files`).
`files` scores analyses already on disk, which is how T32's skill output
gets measured and costs nothing. `api` runs T31's backend over the
golden clusters first, which spends money. The scoring code cannot tell
them apart, for the same reason `nc validate` cannot: they write the
same files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nc.cluster import STATUS_PENDING, Cluster, ClusterItem, cluster_from_dict, render
from nc.contract import Analysis, render_analysis, validate_analysis
from nc.store import DataRoot

DEFAULT_GOLDEN_DIR = Path("evals/golden")
DEFAULT_REPORTS_DIR = Path("evals/reports")

# The two positive labels an earlier ruling overturned are back.
#
# On 2026-09-17 the owner ruled that a Windows 11 bug report and the
# patch fixing it are two stories, and `OVERTURNED_POSITIVES` excluded
# the two labels that said otherwise. On 2026-09-18 the owner repealed
# that rule: a problem and its response are one developing story, and
# the outlets differing over whether the problem is solved is exactly
# what the site is for (prompts/judge.md, "A problem and its response
# are one story"). So the exclusion is gone and the corpus stands as the
# owner labelled it in the first place -- which is what a golden set
# built from human labels should have been all along.


@dataclass(frozen=True)
class ExpectedDiscrepancy:
    """One disagreement the golden case says is there.

    `outlets` rather than item ids: which two outlets disagree is the
    fact a reader cares about and is stable across re-clustering, while
    an item id ties the label to one particular ingest.
    """

    kind: str
    outlets: tuple[str, ...]
    note: str = ""

    @property
    def key(self) -> tuple[str, tuple[str, ...]]:
        return (self.kind, tuple(sorted(self.outlets)))


@dataclass(frozen=True)
class GoldenCase:
    cluster: Cluster
    expected: tuple[ExpectedDiscrepancy, ...]
    # False until the owner has read it (docs/PLAN.md T34). Every number
    # `nc eval` prints carries the count of unchecked cases beside it.
    checked: bool
    source: str

    @property
    def id(self) -> str:
        return self.cluster.id


def golden_to_dict(case: GoldenCase) -> dict[str, Any]:
    return {
        "checked": case.checked,
        "cluster": json.loads(render(case.cluster)),
        "expected_discrepancies": [
            {"kind": d.kind, "outlets": list(d.outlets), "note": d.note}
            for d in case.expected
        ],
        "source": case.source,
    }


def golden_from_dict(raw: Any) -> GoldenCase:
    return GoldenCase(
        cluster=cluster_from_dict(raw["cluster"]),
        expected=tuple(
            ExpectedDiscrepancy(
                kind=str(entry["kind"]),
                outlets=tuple(str(o) for o in entry["outlets"]),
                note=str(entry.get("note", "")),
            )
            for entry in raw["expected_discrepancies"]
        ),
        checked=bool(raw["checked"]),
        source=str(raw.get("source", "")),
    )


def load_golden(directory: Path = DEFAULT_GOLDEN_DIR) -> list[GoldenCase]:
    if not directory.exists():
        return []
    return [
        golden_from_dict(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.json"))
    ]


def write_golden(case: GoldenCase, directory: Path = DEFAULT_GOLDEN_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case.id}.json"
    path.write_text(
        json.dumps(golden_to_dict(case), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


# --- building the set from the labelled positives -------------------------


def clusters_from_positive_labels(
    positives: list[tuple[ClusterItem, ClusterItem]],
) -> list[Cluster]:
    """Union-find over human-confirmed same-story pairs.

    Two pairs that share an item are one story with three outlets, not
    two stories with two -- so the components, not the pairs, are the
    golden clusters. Ids follow `nc.cluster`'s own shape so a golden
    cluster is indistinguishable from a real one to everything
    downstream; the `golden-` prefix would have leaked into
    `cluster_id`'s pattern and the contract would have rejected it.
    """
    from nc.cluster import UnionFind, _allocate_id

    union = UnionFind()
    members: dict[str, ClusterItem] = {}
    for left, right in positives:
        for item in (left, right):
            union.add(item.item_id)
            members[item.item_id] = item
        union.union(left.item_id, right.item_id)

    clusters: list[Cluster] = []
    taken: set[str] = set()
    for component in union.components():
        items = tuple(
            sorted(
                (members[item_id] for item_id in component),
                key=lambda item: (item.published, item.item_id),
            )
        )
        if len({item.outlet for item in items}) < 2:
            continue
        anchor = items[0]
        cluster_id = _allocate_id(anchor.item_id, anchor.published, taken)
        taken.add(cluster_id)
        clusters.append(
            Cluster(
                id=cluster_id,
                version=1,
                anchor=anchor.item_id,
                status=STATUS_PENDING,
                items=items,
            )
        )
    return sorted(clusters, key=lambda cluster: (-len(cluster.items), cluster.id))


# --- scoring --------------------------------------------------------------


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    schema_ok: bool
    quotes_total: int
    quotes_valid: int
    matched: int
    expected: int
    produced: int
    problems: list[str]


@dataclass(frozen=True)
class EvalReport:
    backend: str
    model: str
    scores: list[CaseScore]
    unchecked: int
    generated_at: str

    @property
    def found(self) -> int:
        """Golden cases an analysis actually exists for.

        Reported next to every rate because a missing analysis and a
        failed one score the same -- zero -- and conflating them makes
        `--backend files` on a partly-analysed data root read as a
        catastrophic failure rather than as thin coverage. Scoring them
        the same is still right: a backend that declines half the set
        and is perfect on the rest is not a perfect backend.
        """
        return sum(1 for s in self.scores if "no analysis" not in s.problems)

    @property
    def schema_pass_rate(self) -> float:
        """Over every golden case, not over the ones that happen to have
        a file. Read it with `found` beside it."""
        return _rate(sum(s.schema_ok for s in self.scores), len(self.scores))

    @property
    def schema_pass_rate_of_found(self) -> float:
        return _rate(sum(s.schema_ok for s in self.scores), self.found)

    @property
    def quote_validity(self) -> float:
        """Every quote in every analysis that survived the contract's
        verbatim check. The one number that cannot be argued with, and
        the one CLAUDE.md's "no exceptions" rule rests on."""
        return _rate(
            sum(s.quotes_valid for s in self.scores),
            sum(s.quotes_total for s in self.scores),
        )

    @property
    def stale(self) -> int:
        """Cases whose only problem is that the analysis describes a
        different version of the cluster.

        The golden clusters are frozen at the membership the labels give
        them; the pipeline's clusters keep growing as the judge accepts
        links, so an analysis written for the live cluster cites items
        the golden one does not have. That is the fixture being a
        fixture, not a backend producing bad output, and counting it as
        a schema failure makes a clean backend look broken -- the same
        distinction `nc.site.publishable` draws between stale and
        invalid, for the same reason.

        Score the golden set properly by materializing it into its own
        data root (`nc eval --materialize`) and analysing *those*
        clusters.
        """
        return sum(1 for s in self.scores if _is_stale(s.problems))

    @property
    def scores_discrepancies(self) -> bool:
        """Whether any golden case expects a particular discrepancy.

        False since the owner's 2026-09-18 ruling, and the report drops
        the two rates rather than printing them. Precision over nothing
        expected is 0.000 and recall is 0/0, and both would read as a
        backend failing badly at a job nobody asked it to do.
        """
        return any(s.expected for s in self.scores)

    @property
    def discrepancies_produced(self) -> int:
        """How many discrepancies the backend claimed, across the set.
        Reported as a count, never as a score: there is nothing to score
        it against."""
        return sum(s.produced for s in self.scores)

    @property
    def discrepancy_precision(self) -> float:
        """Only meaningful while `scores_discrepancies` is true."""
        return _rate(
            sum(s.matched for s in self.scores), sum(s.produced for s in self.scores)
        )

    @property
    def discrepancy_recall(self) -> float:
        """Only meaningful while `scores_discrepancies` is true."""
        return _rate(
            sum(s.matched for s in self.scores), sum(s.expected for s in self.scores)
        )


def _is_stale(problems: list[str]) -> bool:
    """Every problem is explained by the cluster having moved on.

    Two shapes count, and only these two: the version mismatch itself,
    and a quote citing an item this version of the cluster does not
    have -- which is the same staleness seen from the quote checker.
    A `not verbatim` quote is never stale; it is a backend quoting text
    nobody published, which is the failure this eval exists to catch.
    """
    if not problems:
        return False
    return all(p.startswith("cluster:") or "is not in cluster" in p for p in problems)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def score_case(case: GoldenCase, analysis: Analysis | None) -> CaseScore:
    """One analysis against one golden case.

    A missing analysis scores zero rather than being skipped: a backend
    that declines half the set and is perfect on the rest is not a
    backend with a perfect score.
    """
    if analysis is None:
        return CaseScore(
            case.id, False, 0, 0, 0, len(case.expected), 0, ["no analysis"]
        )

    # Serialized with `render_analysis`, not a second `model_dump_json`:
    # that one keeps `notes: null` for an absent note, which the schema
    # refuses, so an analysis that is fine on disk scored as a schema
    # failure. One serializer, and it is the byte-stable one the writer
    # already uses.
    _, problems = validate_analysis(json.loads(render_analysis(analysis)), case.cluster)
    quote_problems = [p for p in problems if p.startswith("quote:")]
    quotes_total = len(analysis.all_quotes)

    expected = {d.key for d in case.expected}
    produced = {
        (d.kind, tuple(sorted({q.outlet for q in d.quotes})))
        for d in analysis.discrepancies
    }
    return CaseScore(
        case_id=case.id,
        schema_ok=not problems,
        quotes_total=quotes_total,
        quotes_valid=quotes_total - len(quote_problems),
        matched=len(expected & produced),
        expected=len(expected),
        produced=len(produced),
        problems=problems,
    )


def format_eval_report(report: EvalReport) -> str:
    """The markdown written to `evals/reports/<date>.md`.

    The unchecked count sits next to the headline numbers rather than
    in a footnote: until T34, the discrepancy scores are measured
    against a proposal, and a reader who misses that will over-trust
    them.
    """
    lines = [
        f"# Eval report {report.generated_at[:10]}",
        "",
        f"- backend: `{report.backend}`",
        f"- model: `{report.model}`",
        f"- golden cases: {len(report.scores)}",
        f"- analyses found: {report.found} "
        f"({len(report.scores) - report.found} golden cluster(s) have none)",
        f"- of those, stale: {report.stale} "
        "(the analysis is for a later version of the cluster; the golden "
        "membership is frozen, the pipeline's is not)",
        f"- unchecked by the owner: {report.unchecked}",
        "",
        "| metric | value | what it measures |",
        "| --- | --- | --- |",
        f"| schema pass rate | {report.schema_pass_rate:.3f} | "
        "of ALL golden cases, those with no validator problem |",
        f"| schema pass rate (of found) | {report.schema_pass_rate_of_found:.3f} | "
        "of the cases an analysis exists for |",
        f"| quote validity | {report.quote_validity:.3f} | "
        "quotes that are verbatim in the item they cite |",
    ]
    if report.scores_discrepancies:
        lines.extend(
            [
                f"| discrepancy precision | {report.discrepancy_precision:.3f} | "
                "of the discrepancies produced, how many the golden case expects |",
                f"| discrepancy recall | {report.discrepancy_recall:.3f} | "
                "of the discrepancies expected, how many were produced |",
                "",
                "Discrepancies are matched on `(kind, outlets)`, never on the",
                "explanation text: two correct descriptions of one disagreement never",
                "match as strings, and scoring them that way would measure phrasing",
                "rather than judgement.",
            ]
        )
    else:
        lines.extend(
            [
                f"| discrepancies produced | {report.discrepancies_produced} | "
                "a count, not a score -- see below |",
                "",
                "**Discrepancies are not scored.** The owner ruled on 2026-09-18 that",
                "naming the differing take each cluster must yield is out of scope for",
                "the golden set: articles about one news item are supposed to differ,",
                "and showing that difference is what the site is for. No case",
                "expects a particular discrepancy, so there is nothing to compute",
                "precision or recall against, and printing 0.000 for each would",
                "read as a backend failing at a job nobody asked it to do.",
            ]
        )
    lines.extend(
        [
            "",
            "## Per case",
            "",
            "| cluster | schema | quotes | discrepancies produced |",
            "| --- | --- | --- | --- |",
        ]
    )
    for score in report.scores:
        quotes = (
            f"{score.quotes_valid}/{score.quotes_total}" if score.quotes_total else "-"
        )
        lines.append(
            f"| `{score.case_id}` | {'pass' if score.schema_ok else 'FAIL'} | "
            f"{quotes} | "
            + (
                f"{score.matched}/{score.expected}/{score.produced} |"
                if report.scores_discrepancies
                else f"{score.produced} |"
            )
        )
    failures = [s for s in report.scores if s.problems]
    if failures:
        lines.extend(["", "## Problems", ""])
        for score in failures:
            lines.append(f"- `{score.case_id}`")
            lines.extend(f"  - {problem}" for problem in score.problems)
    return "\n".join(lines) + "\n"


def write_report(report: EvalReport, directory: Path = DEFAULT_REPORTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.generated_at[:10]}-{report.backend}.md"
    path.write_text(format_eval_report(report), encoding="utf-8")
    return path


def run_eval(
    cases: list[GoldenCase],
    analyses: dict[str, Analysis | None],
    backend: str,
    model: str,
    now: str | None = None,
) -> EvalReport:
    """Pure: the cases, the analyses, and nothing else. Whichever
    backend produced the analyses is a label on the report, never a
    branch in the scoring."""
    moment = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if now is None else now
    return EvalReport(
        backend=backend,
        model=model,
        scores=[score_case(case, analyses.get(case.id)) for case in cases],
        unchecked=sum(1 for case in cases if not case.checked),
        generated_at=moment,
    )


def load_analyses_from_files(
    data_root: DataRoot, cases: list[GoldenCase]
) -> dict[str, Analysis | None]:
    """`--backend files`: whatever is on disk for each golden cluster.

    Parsed but not judged here -- `score_case` runs the full validator,
    so a file that is on disk and wrong scores as wrong rather than as
    missing.
    """
    from nc.contract import analysis_path

    found: dict[str, Analysis | None] = {}
    for case in cases:
        path = analysis_path(data_root, case.id)
        if not path.exists():
            found[case.id] = None
            continue
        try:
            found[case.id] = Analysis.model_validate_json(path.read_text("utf-8"))
        except ValueError:
            found[case.id] = None
    return found


def materialize(cases: list[GoldenCase], data_root: DataRoot) -> int:
    """Write the golden clusters into a data root as pending clusters.

    Without this `--backend files` has nothing to score: the golden
    clusters are rebuilt from the label corpus, so they are not the
    clusters the live pipeline happens to be holding, and an analysis
    written against a different membership is a different analysis.
    Materializing turns the golden set into an ordinary queue that
    `nc pending` lists and T32's skill works exactly as it works the
    real one -- which is the only way to measure the subscription
    backend without spending anything.

    Intended for a scratch data root. It writes cluster and pending
    files, so pointing it at the live one would put reconstructed
    clusters in the data repository.
    """
    from nc.cluster import cluster_path, pending_path

    written = 0
    for case in cases:
        text = render(case.cluster)
        for path in (
            cluster_path(data_root, case.cluster),
            pending_path(data_root, case.cluster.id),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        written += 1
    return written
