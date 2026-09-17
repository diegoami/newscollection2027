"""Compare embedding models against the labelled pairs.

**Why this exists.** `nc.embed`'s module docstring accepted a quality
tradeoff on the record: "a lower but consistent embedding quality is
absorbed by threshold tuning, not by reaching for a heavier model on a
job that runs 8 times a day for free." T23's first labelling session
measured that claim and it does not hold. Threshold tuning can only
absorb weak embeddings if the same-story and different-story pairs are
*separable* by some cutoff, and on the 174 labels gathered by T23 they
are not: a different-story pair scored 0.7992 while true matches ran
down to 0.5708, so every threshold either links that false pair or
misses most of the true ones.

That makes the embedding model a variable worth measuring rather than
a settled decision -- and the labels are what make it measurable. A
human judgment ("are these two the same story") is a fact about the
two articles, not about the model that scored them, so the same
judgments can score any candidate, whatever the label set has grown
to.

**The metric.** Precision and recall at a threshold answer "how good is
this cutoff", which is the wrong question when choosing a model. The
question here is how much of the range the model gets *right enough to
act on without asking*, so the headline number is:

    recall at precision 1.0
      = (true pairs scoring above the highest-scoring false pair)
        / (all true pairs)

i.e. how many genuine matches you could auto-link before the first
wrong one. For the model in `config/embed.yaml` that is 4/35 = 0.114,
which is why auto-linking barely earns its place today. A model that
lifts it to, say, 0.7 changes the architecture: auto-linking becomes
safe and T24's judge queue shrinks to genuinely hard pairs.

`roc_auc` is reported beside it as a threshold-free summary (the
probability that a random true pair outranks a random false one),
because recall-at-precision-1.0 is decided by a single pair -- the
highest-scoring false one -- and a model can lose on that one pair
while ranking better everywhere else. Read them together.

**Stale scores.** `Label.score` is a copy of what the model that ran at
labelling time produced (`nc.labelling.Label`), so it is meaningless
for any other model. This module never reads it: it re-embeds both
items of every labelled pair with the candidate backend and recomputes
the cosine. The judgments carry over, the numbers do not.

**Where it runs.** Not in the development sandbox, which has no egress
to huggingface.co -- the same wall that shaped T20. It runs on a
GitHub Actions runner, like `.github/workflows/embed-check.yml`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from nc.embed import EmbeddingBackend, embed_text
from nc.feeds import Item
from nc.labelling import Label, load_labels
from nc.store import DataRoot, read_items


@dataclass(frozen=True)
class BenchResult:
    """One candidate model, measured against one label set."""

    model_id: str
    labels_scored: int
    labels_skipped: int
    true_pairs: int
    false_pairs: int
    recall_at_precision_1: float
    tau_for_precision_1: float | None
    roc_auc: float
    highest_false_score: float | None
    lowest_true_score: float | None

    @property
    def separable(self) -> bool:
        """True when some cutoff gets every judgment right -- no
        overlap between the two populations at all."""
        if self.highest_false_score is None or self.lowest_true_score is None:
            return True
        return self.highest_false_score < self.lowest_true_score

    @property
    def unreachable_true_pairs(self) -> int:
        """True pairs no threshold can auto-link without also linking a
        false one. These are what a judge has to decide."""
        return self.true_pairs - round(self.recall_at_precision_1 * self.true_pairs)


def cosine(left: list[float], right: list[float]) -> float:
    """Cosine of two vectors, matching `nc.cluster.scan_pairs`: both
    normalized, then a dot product, with a zero vector treated as
    orthogonal rather than as a NaN."""
    left_norm = math.sqrt(sum(value * value for value in left)) or 1.0
    right_norm = math.sqrt(sum(value * value for value in right)) or 1.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (left_norm * right_norm)


def roc_auc(scores: list[tuple[float, bool]]) -> float:
    """Probability that a random true pair outranks a random false one,
    by the rank-sum identity (ties share the average rank, so a model
    that scores everything identically lands at 0.5 rather than at
    whatever the sort order happened to be). 0.5 is chance; 1.0 is a
    perfect ranking. Returns 0.5 when either class is empty -- there is
    nothing to rank."""
    trues = sum(1 for _, is_true in scores if is_true)
    falses = len(scores) - trues
    if trues == 0 or falses == 0:
        return 0.5

    ordered = sorted(scores, key=lambda pair: pair[0])
    ranks: list[float] = [0.0] * len(ordered)
    index = 0
    while index < len(ordered):
        stop = index
        while stop + 1 < len(ordered) and ordered[stop + 1][0] == ordered[index][0]:
            stop += 1
        average = (index + stop) / 2 + 1  # ranks are 1-based
        for position in range(index, stop + 1):
            ranks[position] = average
        index = stop + 1

    true_rank_sum = sum(
        rank for rank, (_, is_true) in zip(ranks, ordered, strict=True) if is_true
    )
    return (true_rank_sum - trues * (trues + 1) / 2) / (trues * falses)


def score_labels(
    labels: list[Label],
    items: dict[str, Item],
    backend: EmbeddingBackend,
) -> tuple[list[tuple[float, bool]], int]:
    """`([(score, same_story)], skipped)` for every labelled pair whose
    two items are both still in the store.

    A label whose items have been pruned from the window is skipped and
    counted rather than dropped silently: a benchmark that quietly
    measured half the label set would flatter whichever model ran last.
    """
    needed = sorted(
        {label.item_id_a for label in labels} | {label.item_id_b for label in labels}
    )
    present = [item_id for item_id in needed if item_id in items]
    vectors: dict[str, list[float]] = {}
    if present:
        texts = [embed_text(items[item_id]) for item_id in present]
        for item_id, vector in zip(present, backend.embed(texts), strict=True):
            vectors[item_id] = list(vector)

    scored: list[tuple[float, bool]] = []
    skipped = 0
    for label in labels:
        left = vectors.get(label.item_id_a)
        right = vectors.get(label.item_id_b)
        if left is None or right is None:
            skipped += 1
            continue
        scored.append((cosine(left, right), label.same_story))
    return scored, skipped


def measure(
    model_id: str, scored: list[tuple[float, bool]], skipped: int
) -> BenchResult:
    trues = [score for score, is_true in scored if is_true]
    falses = [score for score, is_true in scored if not is_true]

    highest_false = max(falses) if falses else None
    lowest_true = min(trues) if trues else None

    # Precision 1.0 means linking nothing at or below the best false
    # pair. Strictly above: a tie with a false pair is not a clean link.
    above = sorted(
        (score for score in trues if highest_false is None or score > highest_false),
        reverse=True,
    )
    recall = len(above) / len(trues) if trues else 0.0

    return BenchResult(
        model_id=model_id,
        labels_scored=len(scored),
        labels_skipped=skipped,
        true_pairs=len(trues),
        false_pairs=len(falses),
        recall_at_precision_1=recall,
        tau_for_precision_1=above[-1] if above else None,
        roc_auc=roc_auc(scored),
        highest_false_score=highest_false,
        lowest_true_score=lowest_true,
    )


def bench_model(
    data_root: DataRoot, model_id: str, backend: EmbeddingBackend
) -> BenchResult:
    """Score every labelled pair with `backend` and measure it."""
    items = {item.id: item for item in read_items(data_root)}
    scored, skipped = score_labels(load_labels(data_root), items, backend)
    return measure(model_id, scored, skipped)


def format_bench_report(results: list[BenchResult], current_model: str) -> str:
    """One row per candidate, best recall-at-precision-1.0 first."""
    if not results:
        return "bench: no candidate models in config/embed.yaml (bench_candidates)"

    head = results[0]
    lines = [
        f"bench: {head.labels_scored} labelled pair(s) "
        f"({head.true_pairs} same-story, {head.false_pairs} different-story)"
        + (
            f", {head.labels_skipped} skipped (items no longer stored)"
            if head.labels_skipped
            else ""
        ),
        "bench: recall@p1.0 is how many true pairs score above the highest false "
        "one -- what a threshold could auto-link with no wrong link at all",
        "bench: auc is threshold-free (chance is 0.500); read it beside recall@p1.0, "
        "which one unlucky false pair can sink on its own",
        "",
        f"{'model':<34} {'recall@p1.0':>12} {'auc':>7} {'tau':>8} "
        f"{'top false':>10} {'judge':>6}",
    ]
    for result in sorted(
        results, key=lambda r: (-r.recall_at_precision_1, -r.roc_auc, r.model_id)
    ):
        tau = (
            f"{result.tau_for_precision_1:.4f}" if result.tau_for_precision_1 else "--"
        )
        top_false = (
            f"{result.highest_false_score:.4f}"
            if result.highest_false_score is not None
            else "--"
        )
        marker = " *" if result.model_id == current_model else ""
        lines.append(
            f"{result.model_id[:34]:<34} "
            f"{result.recall_at_precision_1:>11.3f} "
            f"{result.roc_auc:>7.3f} {tau:>8} {top_false:>10} "
            f"{result.unreachable_true_pairs:>6}{marker}"
        )

    lines += [
        "",
        "bench: * is the model config/embed.yaml runs today",
        "bench: 'judge' is the true pairs no threshold can reach -- the work "
        "that has to go to T24 whatever tau_high is set to",
        "bench: a model only earns a switch if it moves recall@p1.0 enough to "
        "pay for re-embedding every stored item; changing model_id invalidates "
        "every vector and both thresholds together (nc/embed.py's _SCHEMA, "
        "docs/CLUSTERING.md)",
    ]
    return "\n".join(lines)


def load_bench_candidates(path: Path) -> list[str]:
    """`bench_candidates` from config/embed.yaml, or [] when absent."""
    import yaml

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    candidates = raw.get("bench_candidates") or []
    return [str(candidate) for candidate in candidates]
