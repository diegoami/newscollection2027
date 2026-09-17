"""T24: the borderline-pair judge, as a file contract.

**Why this is the workhorse and not a fallback.** T23's labelling
measured what cosine can decide, and the answer was: not this. On 174
labelled cross-outlet pairs the two populations overlap -- a
different-story pair scored 0.7992 while true matches ran down to
0.5708 -- and `nc bench-embed` found no model across a 64x parameter
range that separates them (docs/CLUSTERING.md). So `tau_high` is 1.00,
nothing links on a judgement about similarity, and every real yes-or-no
about whether two articles cover the same event is made here.

The distinction the embedding cannot draw, and this module exists for:

    same TOPIC   two outlets writing about AI safety, or about iOS 27
    same EVENT   two outlets reporting the same announcement, filing,
                 outage, launch or court ruling

Cosine sees shared vocabulary and calls both a match. Only something
that reasons about events can tell them apart, which is why the band
goes to an LLM.

**The contract.** CLAUDE.md: "The LLM step is a file contract ... Never
bypass the validator." This module is that contract for pair judgments,
the same shape the analysis step uses for clusters:

    in    data/pending-pairs/<pair_id>.json   (written by `nc cluster`)
    out   data/judgments/<pair_id>.json       (written by a backend)
    gate  `validate_judgment` decides what is allowed to affect
          clustering; `accepted_links` is the only way a judgment
          reaches `cluster_items`

Two backends write the same files and this module cannot tell them
apart, exactly as docs/ARCHITECTURE.md intends ("The rest of the system
never knows which backend ran"):

- `claude_code` -- the nightly Routine, on the subscription. This is the
  production path, and it is why the judge costs nothing metered:
  docs/ARCHITECTURE.md's cost model puts the nightly LLM step on the
  Claude Code subscription and reserves the API backend for "local
  development, evals and backfills only".
- `api` -- the Anthropic SDK, for `nc bench-judge` and backfills.

**Why judgments are files and not a database.** They are evidence, not
state. A judgment is written once and never rewritten, so a run that
re-judges nothing writes nothing and the data repo stays quiet under a
cron that fires eight times a day -- the same idempotence obligation
`nc.cluster` carries, for the same reason (docs/PLAN.md T12). It also
means a judgment survives the pair file being pruned, and that a human
can read why a link exists without running anything.

**What a judgment may not do.** It can only *add* a link. There is no
"these are definitely not the same story" instruction that suppresses
anything, because nothing links without a judgment in the first place:
below `tau_low` a pair is never seen, and at or above it a pair links
only if a judgment says so. `same_story: false` is therefore recorded
but inert -- kept because it is the evidence for `nc bench-judge`, and
because re-judging a pair the model already declined would be a waste
of a call every night forever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from nc.cluster import PendingPair, load_pending_pairs
from nc.labelling import Label, load_labels
from nc.store import DataRoot

DEFAULT_JUDGE_CONFIG_PATH = Path("config/judge.yaml")

# The prompt text, shared by every backend. A file, not a string in this
# module, for the same reason docs/PLAN.md T31 puts `prompts/analyze.md`
# on disk: the skill an agent reads and the code a backend runs have to
# be asking the same question, or `nc bench-judge`'s number describes
# neither of them.
DEFAULT_JUDGE_PROMPT_PATH = Path("prompts/judge.md")

BACKEND_CLAUDE_CODE = "claude_code"
BACKEND_API = "api"
_BACKENDS = frozenset({BACKEND_CLAUDE_CODE, BACKEND_API})

# A reason is for a human reading the data repo later, and for spotting
# a backend that has started returning boilerplate. Long enough for one
# real sentence, short enough that nobody is tempted to put an analysis
# in it -- that is T30's file, with its own quote rules.
_MIN_REASON = 10
_MAX_REASON = 300


@dataclass(frozen=True)
class Judgment:
    """One backend's answer about one borderline pair.

    `item_id_a`/`item_id_b` are redundant with `pair_id` and stored
    anyway: a judgment file has to be readable on its own, and the
    validator checks the three against each other rather than trusting
    a filename.
    """

    pair_id: str
    item_id_a: str
    item_id_b: str
    same_story: bool
    reason: str
    backend: str
    model: str
    judged_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "item_id_a": self.item_id_a,
            "item_id_b": self.item_id_b,
            "judged_at": self.judged_at,
            "model": self.model,
            "pair_id": self.pair_id,
            "reason": self.reason,
            "same_story": self.same_story,
        }


def judgments_dir(data_root: DataRoot) -> Path:
    return data_root.resolve("judgments")


def judgment_path(data_root: DataRoot, pair_id: str) -> Path:
    return judgments_dir(data_root) / f"{pair_id}.json"


def render_judgment(judgment: Judgment) -> str:
    """Byte-stable: sorted keys, no spaces, one trailing newline --
    `nc.cluster.render` and `nc.labelling` serialize the same way, so a
    rewritten file is byte-identical and git sees nothing to commit."""
    return json.dumps(judgment.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"


def judgment_from_dict(payload: object) -> Judgment:
    if not isinstance(payload, dict):
        raise ValueError("a judgment must be a JSON object")
    missing = sorted(
        {
            "backend",
            "item_id_a",
            "item_id_b",
            "judged_at",
            "model",
            "pair_id",
            "reason",
            "same_story",
        }
        - set(payload)
    )
    if missing:
        raise ValueError(f"missing field(s): {', '.join(missing)}")
    same_story = payload["same_story"]
    if not isinstance(same_story, bool):
        # json.loads turns `true` into a bool and "true" into a str; a
        # backend that emitted the string would otherwise be read as
        # truthy and silently link the pair.
        raise ValueError("same_story must be true or false, not a string or number")
    return Judgment(
        pair_id=str(payload["pair_id"]),
        item_id_a=str(payload["item_id_a"]),
        item_id_b=str(payload["item_id_b"]),
        same_story=same_story,
        reason=str(payload["reason"]),
        backend=str(payload["backend"]),
        model=str(payload["model"]),
        judged_at=str(payload["judged_at"]),
    )


class JudgmentRejected(ValueError):
    """A judgment that must not be allowed to affect clustering."""


def validate_judgment(judgment: Judgment, pairs: dict[str, PendingPair]) -> None:
    """Raise `JudgmentRejected` unless this judgment may link its pair.

    Every check exists because the alternative is a wrong link on the
    site, which docs/CLUSTERING.md records as the one error nothing
    downstream can recover.
    """
    pair = pairs.get(judgment.pair_id)
    if pair is None:
        raise JudgmentRejected(
            f"{judgment.pair_id}: no pending pair with this id -- a judgment "
            "may only answer a question the pipeline asked"
        )
    expected = {pair.a.item_id, pair.b.item_id}
    if {judgment.item_id_a, judgment.item_id_b} != expected:
        raise JudgmentRejected(
            f"{judgment.pair_id}: item ids do not match the pair's members"
        )
    if judgment.backend not in _BACKENDS:
        raise JudgmentRejected(
            f"{judgment.pair_id}: unknown backend {judgment.backend!r}"
        )
    if not judgment.model.strip():
        raise JudgmentRejected(f"{judgment.pair_id}: model must not be empty")
    reason = judgment.reason.strip()
    if len(reason) < _MIN_REASON:
        raise JudgmentRejected(
            f"{judgment.pair_id}: reason is too short to be one -- a backend "
            "that links without saying why cannot be reviewed later"
        )
    if len(reason) > _MAX_REASON:
        raise JudgmentRejected(
            f"{judgment.pair_id}: reason is longer than {_MAX_REASON} characters; "
            "a judgment records one sentence, not an analysis"
        )


def load_judgments(data_root: DataRoot) -> list[Judgment]:
    """Every judgment on disk, ordered by pair id. A file that does not
    parse raises: a judge queue that silently skips its own corrupt
    output would keep re-judging the same pair every night."""
    directory = judgments_dir(data_root)
    if not directory.exists():
        return []
    judgments: list[Judgment] = []
    for path in sorted(directory.glob("*.json")):
        try:
            judgments.append(judgment_from_dict(json.loads(path.read_text("utf-8"))))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: {exc}") from exc
    return judgments


def write_judgments(data_root: DataRoot, judgments: list[Judgment]) -> int:
    """Write the ones not already on disk; return how many were new.

    Never rewrites: a judgment is evidence of what a backend said at a
    moment, and rewriting it on every run would put a `data:` commit in
    the repository eight times a day for no new information.
    """
    directory = judgments_dir(data_root)
    directory.mkdir(parents=True, exist_ok=True)
    written = 0
    for judgment in judgments:
        path = judgment_path(data_root, judgment.pair_id)
        if path.exists():
            continue
        path.write_text(render_judgment(judgment), encoding="utf-8")
        written += 1
    return written


def unjudged_pairs(data_root: DataRoot) -> list[PendingPair]:
    """Borderline pairs with no judgment yet -- the queue a backend
    works through. Ordered highest score first: if a run is interrupted
    or a budget runs out, the pairs most likely to be real matches have
    been judged."""
    judged = {judgment.pair_id for judgment in load_judgments(data_root)}
    pairs = [
        pair for pair in load_pending_pairs(data_root) if pair.pair_id not in judged
    ]
    return sorted(pairs, key=lambda pair: (-pair.score, pair.pair_id))


def accepted_links(data_root: DataRoot) -> list[tuple[str, str]]:
    """The `extra_links` for `nc.cluster.cluster_items`: one pair per
    judgment that says same story *and* passes the validator.

    This is the only path from a judgment into clustering. A judgment
    that fails validation is skipped here rather than raising, so one
    malformed file cannot stop the night's clustering -- but it is
    counted and reported by `nc judge --validate`, never swallowed in
    silence.
    """
    links, _ = _accepted_links_with_rejects(data_root)
    return links


def _accepted_links_with_rejects(
    data_root: DataRoot,
) -> tuple[list[tuple[str, str]], list[str]]:
    pairs = {pair.pair_id: pair for pair in load_pending_pairs(data_root)}
    links: list[tuple[str, str]] = []
    rejects: list[str] = []
    for judgment in load_judgments(data_root):
        try:
            validate_judgment(judgment, pairs)
        except JudgmentRejected as exc:
            rejects.append(str(exc))
            continue
        if judgment.same_story:
            links.append((judgment.item_id_a, judgment.item_id_b))
    return sorted(links), rejects


@dataclass(frozen=True)
class JudgeReport:
    pending: int
    judged: int
    written: int
    accepted: int
    rejected: list[str]


def validate_all(data_root: DataRoot) -> JudgeReport:
    """`nc judge --validate`: what is on disk, what links, what is
    rejected and why."""
    judgments = load_judgments(data_root)
    links, rejects = _accepted_links_with_rejects(data_root)
    return JudgeReport(
        pending=len(unjudged_pairs(data_root)),
        judged=len(judgments),
        written=0,
        accepted=len(links),
        rejected=rejects,
    )


def format_judge_report(report: JudgeReport) -> str:
    lines = [
        f"judge: {report.judged} judgment(s) on disk, "
        f"{report.pending} pair(s) still unjudged",
        f"judge: {report.accepted} accepted link(s) for the next nc cluster run",
    ]
    if report.written:
        lines.insert(1, f"judge: wrote {report.written} new judgment file(s)")
    if report.rejected:
        lines.append(f"judge: {len(report.rejected)} rejected:")
        lines.extend(f"  {reason}" for reason in report.rejected)
    return "\n".join(lines)


# --- configuration ------------------------------------------------------


@dataclass(frozen=True)
class JudgeConfig:
    claude_code_model: str
    api_model: str
    max_pairs_per_run: int

    def model_for(self, backend: str) -> str:
        if backend == BACKEND_CLAUDE_CODE:
            return self.claude_code_model
        if backend == BACKEND_API:
            return self.api_model
        raise ValueError(f"unknown backend {backend!r}")


def load_judge_config(path: Path = DEFAULT_JUDGE_CONFIG_PATH) -> JudgeConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return JudgeConfig(
        claude_code_model=str(raw["claude_code_model"]),
        api_model=str(raw["api_model"]),
        max_pairs_per_run=int(raw["max_pairs_per_run"]),
    )


def load_judge_prompt(path: Path = DEFAULT_JUDGE_PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8")


def render_pair_question(pair: PendingPair) -> str:
    """One pair, as a backend sees it.

    Both members verbatim -- outlet, item id, title, lede -- because the
    judge's answer has to be checkable against the data repo afterwards
    and because a paraphrase is exactly the kind of drift that makes an
    eval number meaningless. The score is deliberately absent: it is the
    thing that could not decide this (module docstring), and showing it
    would anchor the answer on the signal we already know does not work.
    """
    lines = [f"pair_id: {pair.pair_id}", ""]
    for side, member in (("A", pair.a), ("B", pair.b)):
        lines.extend(
            [
                f"{side} outlet: {member.outlet}",
                f"{side} item_id: {member.item_id}",
                f"{side} published: {member.published}",
                f"{side} title: {member.title}",
                f"{side} lede: {member.lede}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


# --- eval against the human labels --------------------------------------


@dataclass(frozen=True)
class Disagreement:
    pair_id: str
    human: bool
    judge: bool
    reason: str


@dataclass(frozen=True)
class JudgeEval:
    """How one set of judgments compares to T23's human labels.

    `labelled` is the overlap, not the corpus: a label without a
    judgment says nothing about the judge, and a judgment on an
    unlabelled pair has nothing to be checked against. Reporting the
    overlap size next to every rate is what stops a number computed
    from four pairs being read as a result.
    """

    labelled: int
    judged: int
    agreed: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    disagreements: list[Disagreement]

    @property
    def accuracy(self) -> float:
        return self.agreed / self.judged if self.judged else 0.0

    @property
    def precision(self) -> float:
        """Of the links the judge would have made, how many a human
        agrees with. The number that matters most: a false positive is
        two unrelated stories merged on the site, which
        docs/CLUSTERING.md records as the one error nothing downstream
        can recover."""
        linked = self.true_positive + self.false_positive
        return self.true_positive / linked if linked else 0.0

    @property
    def recall(self) -> float:
        real = self.true_positive + self.false_negative
        return self.true_positive / real if real else 0.0


def score_judgments(labels: list[Label], judgments: list[Judgment]) -> JudgeEval:
    """Compare a backend's answers to the human ones, pair by pair.

    Pure: no filesystem. Matched on `pair_id`, which `Label.pair_id` and
    `PendingPair.pair_id` both build the same way (`<lower id>-<higher
    id>`), so the join is string equality and nothing has to be
    re-derived. The last label for a pair wins -- `labels/pairs.jsonl`
    is append-only, so a corrected answer is a later line, not an edit.
    """
    human = {label.pair_id: label.same_story for label in labels}
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    disagreements: list[Disagreement] = []
    judged = 0
    for judgment in sorted(judgments, key=lambda j: j.pair_id):
        if judgment.pair_id not in human:
            continue
        judged += 1
        truth = human[judgment.pair_id]
        if truth and judgment.same_story:
            counts["tp"] += 1
        elif truth:
            counts["fn"] += 1
        elif judgment.same_story:
            counts["fp"] += 1
        else:
            counts["tn"] += 1
        if truth != judgment.same_story:
            disagreements.append(
                Disagreement(
                    pair_id=judgment.pair_id,
                    human=truth,
                    judge=judgment.same_story,
                    reason=judgment.reason,
                )
            )
    return JudgeEval(
        labelled=len(human),
        judged=judged,
        agreed=counts["tp"] + counts["tn"],
        true_positive=counts["tp"],
        false_positive=counts["fp"],
        true_negative=counts["tn"],
        false_negative=counts["fn"],
        disagreements=disagreements,
    )


def run_bench_judge(data_root: DataRoot) -> JudgeEval:
    """`nc bench-judge`: score whatever judgments are on disk against
    `labels/pairs.jsonl`.

    Backend-agnostic on purpose. The judgment files are the contract
    (module docstring), so this command measures the `claude_code`
    backend that exists today and will measure T31's `api` backend
    unchanged -- there is nothing here that knows how a judgment was
    produced, only what it says.
    """
    return score_judgments(load_labels(data_root), load_judgments(data_root))


def format_judge_eval(result: JudgeEval) -> str:
    if not result.judged:
        return (
            f"bench-judge: {result.labelled} labelled pair(s), none of them "
            "judged yet -- run nc judge first"
        )
    lines = [
        f"bench-judge: {result.judged} pair(s) both labelled and judged "
        f"(of {result.labelled} labelled)",
        f"  agreement {result.agreed}/{result.judged} ({result.accuracy:.3f})",
        f"  precision {result.precision:.3f}  "
        f"({result.true_positive} right, {result.false_positive} wrong link(s))",
        f"  recall    {result.recall:.3f}  "
        f"({result.true_positive} found, {result.false_negative} missed)",
    ]
    if result.disagreements:
        lines.append("  disagreements (human / judge):")
        for item in result.disagreements:
            human = "yes" if item.human else "no"
            judge = "yes" if item.judge else "no"
            lines.append(f"    {item.pair_id}  {human} / {judge}  {item.reason}")
    return "\n".join(lines)
