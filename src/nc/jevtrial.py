"""The Jev trial (`nc bench-jev`): could TypeSafe's Jev do the judge's job?

**Why try it.** The judge (nc/judge.py) answers one question per pair --
same event or not -- and it cannot keep up: about 90 new cross-outlet
pairs a day against a nightly budget of 150, with a backlog of over a
thousand (config/judge.yaml). Jev is a "System One" model: it does not
generate text, it returns a calibrated probability for a question asked
of some state. That is the judge's question exactly, at roughly a
hundredth of a cent per pair and half a second per call.

**Why it is only a trial.** Everything known about Jev on 2026-09-26 was
the vendor's own claims and developer anecdotes, and none of it covers
this task: telling one event from one topic, the distinction cosine
could not draw (docs/CLUSTERING.md). The human labels can settle it, so
this module asks Jev every labelled pair and scores the answers with
`nc.judge.score_judgments` -- the same function `nc bench-judge` uses,
so the two numbers mean the same thing.

**What it must not do.** Nothing here writes to the data repository or
reaches clustering. Answers are cached under `.cache/jev-trial/`, which
is derived state per CLAUDE.md, so a rerun is free and an interrupted
run resumes. Judgment files stay the judge's alone.

**Two questions, not one.**

- *Replace the judge?* Only at a cutoff whose precision matches the
  judge's, because a wrong yes merges two unrelated stories and every
  claim built on that merge is wrong too. The cutoff is picked on half
  the labels and reported on the other half; picking and reporting on
  the same pairs would flatter it.
- *Pre-filter the queue?* The likelier win. If almost no labelled match
  scores below `prefilter_below`, pairs Jev puts there can leave the
  judge's queue unasked, and the judge's budget goes to pairs that can
  make a story.

**Dates are a variant, not a given.** The judge sees publication times,
and its prompt uses them ("a gap of more than a day or two ... is
usually two events"). Jev's documented weak spots include dates, so the
trial asks every pair twice, with and without them, and reports both.

The score is never sent. Like the judge (`render_pair_question`), Jev
must not be anchored on the one signal already known not to work.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from nc.cluster import (
    ClusterItem,
    PendingPair,
    load_cluster_config,
    load_label_sample,
    load_pending_pairs,
)
from nc.judge import (
    BACKEND_JEV,
    DEFAULT_JUDGE_CONFIG_PATH,
    JudgeEval,
    Judgment,
    load_judgments,
    score_judgments,
)
from nc.labelling import Label, load_labels
from nc.store import DataRoot, write_text

DEFAULT_JEV_PROMPT_PATH = Path("prompts/jev.md")
DEFAULT_CACHE_DIR = Path(".cache/jev-trial")

# The SDK's own path (typesafe_sdk/_core/constants.py, SYSTEM_ONE_PATH).
SYSTEM_ONE_PATH = "/v1/systemone"
QUESTION = "same_event"

VARIANT_DATES = "with-dates"
VARIANT_NO_DATES = "no-dates"
VARIANTS = (VARIANT_NO_DATES, VARIANT_DATES)

# Calibration bins, lower edges. Fixed rather than configured: they
# only shape a table, they decide nothing.
_BINS = (0.0, 0.1, 0.3, 0.5, 0.7, 0.9)


@dataclass(frozen=True)
class JevConfig:
    model: str
    base_url: str
    api_key_env: str
    timeout_seconds: float
    cutoffs: tuple[float, ...]
    target_precision: float
    prefilter_below: float
    auto_yes_at: float


def load_jev_config(path: Path = DEFAULT_JUDGE_CONFIG_PATH) -> JevConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("jev"), dict):
        raise ValueError(f"{path}: no `jev` section")
    jev = raw["jev"]
    cutoffs = tuple(sorted(float(c) for c in jev["cutoffs"]))
    if not cutoffs or not all(0.0 < c < 1.0 for c in cutoffs):
        raise ValueError(f"{path}: jev.cutoffs must be in (0, 1), got {cutoffs}")
    return JevConfig(
        model=str(jev["model"]),
        base_url=str(jev["base_url"]).rstrip("/"),
        api_key_env=str(jev["api_key_env"]),
        timeout_seconds=float(jev["timeout_seconds"]),
        cutoffs=cutoffs,
        target_precision=float(jev["target_precision"]),
        prefilter_below=float(jev["prefilter_below"]),
        auto_yes_at=float(jev["auto_yes_at"]),
    )


def load_jev_prompt(path: Path = DEFAULT_JEV_PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8").strip()


# --- the pairs --------------------------------------------------------------


@dataclass(frozen=True)
class TrialPair:
    pair: PendingPair
    human: bool


def trial_pairs(data_root: DataRoot) -> tuple[list[TrialPair], int]:
    """Every labelled pair whose two articles are on disk, with the
    human's last answer, and how many labels had no pair to rebuild.

    The text comes from the pair files `nc cluster` wrote, the same
    source `nc label` and the page show a human -- so Jev reads what the
    labeller read."""
    human: dict[str, Label] = {label.pair_id: label for label in load_labels(data_root)}
    on_disk = {p.pair_id: p for p in load_label_sample(data_root)}
    on_disk.update({p.pair_id: p for p in load_pending_pairs(data_root)})
    pairs = [
        TrialPair(pair=on_disk[pid], human=label.same_story)
        for pid, label in sorted(human.items())
        if pid in on_disk
    ]
    return pairs, len(human) - len(pairs)


def _article(item: ClusterItem, with_dates: bool) -> dict[str, str]:
    article = {"outlet": item.outlet, "title": item.title, "lede": item.lede}
    if with_dates:
        article["published"] = item.published
    return article


def state_for(pair: PendingPair, variant: str) -> dict[str, Any]:
    """Named fields, which Jev's docs recommend "whenever the context has
    several parts". No score: see the module docstring."""
    with_dates = variant == VARIANT_DATES
    return {
        "article_a": _article(pair.a, with_dates),
        "article_b": _article(pair.b, with_dates),
    }


def request_body(
    pair: PendingPair, variant: str, prompt: str, model: str
) -> dict[str, Any]:
    return {
        "model": model,
        "state": state_for(pair, variant),
        "questions": {QUESTION: {"type": "noul", "instructions": prompt}},
    }


def in_tuning_half(pair_id: str) -> bool:
    """A fixed split: the same pair is always on the same side, so a
    rerun or a new label never reshuffles which half picked the cutoff."""
    return hashlib.blake2b(pair_id.encode("utf-8"), digest_size=1).digest()[0] % 2 == 0


# --- asking -----------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    pair_id: str
    variant: str
    p: float
    model: str
    input_tokens: int
    cost: float
    seconds: float
    cached: bool = False


def parse_answer(
    raw: Mapping[str, Any], pair_id: str, variant: str, seconds: float
) -> Answer:
    """One API response, or a raise. A probability outside [0, 1] or a
    missing answer is refused rather than read as a no: a trial that
    quietly counted failures as negatives would report a recall that
    was never measured."""
    answers = raw.get("answers")
    if not isinstance(answers, dict) or QUESTION not in answers:
        raise ValueError(f"{pair_id}: response has no {QUESTION!r} answer: {raw!r}")
    p = answers[QUESTION].get("noul")
    if isinstance(p, bool) or not isinstance(p, int | float) or not 0.0 <= p <= 1.0:
        raise ValueError(f"{pair_id}: noul {p!r} is not a probability")
    usage = raw.get("usage") or {}
    return Answer(
        pair_id=pair_id,
        variant=variant,
        p=float(p),
        model=str(raw.get("model", "")),
        input_tokens=int(usage.get("input_tokens", 0)),
        cost=float(usage.get("cost", 0.0)),
        seconds=round(seconds, 3),
    )


Post = Callable[[dict[str, Any]], dict[str, Any]]


def http_post(config: JevConfig, api_key: str) -> Post:
    """The one function here that touches the network. Retries 429, 5xx
    and timeouts with backoff, the SDK's default policy in miniature;
    any other HTTP error is the caller's to see.

    Timeouts are retried because the first full run on 2026-09-26 died
    on one: a single slow response out of 560 ended the trial at 111.
    The cache kept those, but a run that needs babysitting is not one
    anybody will rerun."""

    def post(body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            config.base_url + SYSTEM_ONE_PATH,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(
                    request, timeout=config.timeout_seconds
                ) as response:
                    parsed: dict[str, Any] = json.loads(response.read())
                    return parsed
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500 or attempt == 3:
                    raise
            except (TimeoutError, urllib.error.URLError):
                if attempt == 3:
                    raise
            time.sleep(0.5 * 2**attempt)
        raise AssertionError("unreachable")

    return post


def _cache_path(cache_dir: Path, model: str, variant: str, pair_id: str) -> Path:
    return cache_dir / model / variant / f"{pair_id}.json"


def ask_all(
    pairs: Sequence[PendingPair],
    prompt: str,
    config: JevConfig,
    post: Post,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    variants: Iterable[str] = VARIANTS,
) -> list[Answer]:
    """Every pair, every variant, asked once. A cached answer is reused
    only for the same model and the same prompt -- a changed prompt is a
    different question and is asked again."""
    prompt_id = hashlib.blake2b(prompt.encode("utf-8"), digest_size=6).hexdigest()
    answers: list[Answer] = []
    for variant in variants:
        for pair in pairs:
            pid = pair.pair_id
            path = _cache_path(cache_dir, config.model, variant, pid)
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("prompt_id") == prompt_id:
                    answer = parse_answer(cached["raw"], pid, variant, 0.0)
                    answers.append(replace(answer, cached=True))
                    continue
            started = time.monotonic()
            raw = post(request_body(pair, variant, prompt, config.model))
            answer = parse_answer(raw, pid, variant, time.monotonic() - started)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text(path, json.dumps({"prompt_id": prompt_id, "raw": raw}) + "\n")
            answers.append(answer)
    return answers


# --- scoring ----------------------------------------------------------------


def as_judgments(answers: Iterable[Answer], cutoff: float) -> list[Judgment]:
    """Jev's answers in the judge's shape, so `score_judgments` scores
    them exactly as it scores the judge. In memory only; never written."""
    judgments = []
    for a in answers:
        # Item ids are hex digests, so the first hyphen is the separator
        # (`PendingPair.pair_id`).
        item_a, item_b = a.pair_id.split("-", 1)
        judgments.append(
            Judgment(
                pair_id=a.pair_id,
                item_id_a=item_a,
                item_id_b=item_b,
                same_story=a.p >= cutoff,
                reason=f"jev p={a.p:.3f} at cutoff {cutoff}",
                backend=BACKEND_JEV,
                model=a.model,
                judged_at="",
            )
        )
    return judgments


def _labels_for(pairs: Iterable[TrialPair]) -> list[Label]:
    return [
        Label(
            item_id_a=t.pair.a.item_id,
            item_id_b=t.pair.b.item_id,
            outlet_a=t.pair.a.outlet,
            outlet_b=t.pair.b.outlet,
            score=t.pair.score,
            same_story=t.human,
            labeled_at="",
        )
        for t in pairs
    ]


def pick_cutoff(
    tuning: Sequence[TrialPair], answers: Sequence[Answer], config: JevConfig
) -> float | None:
    """The lowest configured cutoff -- the most recall -- whose precision
    on the tuning half reaches the target. None if no cutoff does, which
    is itself the answer to "can Jev replace the judge"."""
    labels = _labels_for(tuning)
    for cutoff in config.cutoffs:
        result = score_judgments(labels, as_judgments(answers, cutoff))
        if result.true_positive + result.false_positive and (
            result.precision >= config.target_precision
        ):
            return cutoff
    return None


@dataclass(frozen=True)
class CalibrationBin:
    low: float
    high: float
    count: int
    mean_p: float
    yes_rate: float


def calibration(
    pairs: Sequence[TrialPair], answers: Sequence[Answer]
) -> list[CalibrationBin]:
    """Of the pairs Jev put near p, how many were really matches. If the
    vendor's calibration claim holds, `yes_rate` tracks `mean_p`."""
    truth = {t.pair.pair_id: t.human for t in pairs}
    bins: list[CalibrationBin] = []
    edges = (*_BINS, 1.0001)
    for low, high in zip(edges, edges[1:], strict=False):
        inside = [a for a in answers if low <= a.p < high and a.pair_id in truth]
        if not inside:
            continue
        bins.append(
            CalibrationBin(
                low=low,
                high=min(high, 1.0),
                count=len(inside),
                mean_p=sum(a.p for a in inside) / len(inside),
                yes_rate=sum(truth[a.pair_id] for a in inside) / len(inside),
            )
        )
    return bins


@dataclass(frozen=True)
class Prefilter:
    below: float
    dropped: int
    total: int
    matches_lost: int
    matches: int


def prefilter(
    pairs: Sequence[TrialPair], answers: Sequence[Answer], below: float
) -> Prefilter:
    truth = {t.pair.pair_id: t.human for t in pairs}
    scored = [a for a in answers if a.pair_id in truth]
    dropped = [a for a in scored if a.p < below]
    return Prefilter(
        below=below,
        dropped=len(dropped),
        total=len(scored),
        matches_lost=sum(truth[a.pair_id] for a in dropped),
        matches=sum(truth[a.pair_id] for a in scored),
    )


@dataclass(frozen=True)
class Triage:
    """The three-way split: Jev says yes at or above `high`, no below
    `low`, and the judge decides the middle. Against labels, each lane
    also carries its mistakes; against the live queue there are none to
    count, only how the queue would divide."""

    low: float
    high: float
    no: int
    middle: int
    yes: int
    no_lost: int | None = None  # labelled matches Jev would have said no to
    yes_wrong: int | None = None  # labelled non-matches Jev would have linked


def triage(
    answers: Sequence[Answer],
    low: float,
    high: float,
    truth: Mapping[str, bool] | None = None,
) -> Triage:
    no = [a for a in answers if a.p < low]
    yes = [a for a in answers if a.p >= high]
    lost = wrong = None
    if truth is not None:
        lost = sum(truth[a.pair_id] for a in no if a.pair_id in truth)
        wrong = sum(not truth[a.pair_id] for a in yes if a.pair_id in truth)
    return Triage(
        low=low,
        high=high,
        no=len(no),
        middle=len(answers) - len(no) - len(yes),
        yes=len(yes),
        no_lost=lost,
        yes_wrong=wrong,
    )


def format_triage(t: Triage) -> str:
    line = (
        f"no below {t.low}: {t.no} | judge decides: {t.middle} | "
        f"yes at {t.high}+: {t.yes}"
    )
    if t.no_lost is not None and t.yes_wrong is not None:
        line += f"  (matches lost {t.no_lost}, wrong links {t.yes_wrong})"
    return line


@dataclass(frozen=True)
class VariantReport:
    variant: str
    per_cutoff: list[tuple[float, JudgeEval, JudgeEval]]  # cutoff, tuning, held out
    picked: float | None
    held_out_at_pick: JudgeEval | None
    head_to_head: tuple[JudgeEval, JudgeEval] | None  # judge, jev on shared pairs
    calibration: list[CalibrationBin]
    prefilter: Prefilter
    triage: Triage
    cost: float
    mean_seconds: float | None


@dataclass(frozen=True)
class TrialReport:
    model: str
    pairs: int
    matches: int
    unrebuildable: int
    variants: list[VariantReport]


def build_report(
    pairs: Sequence[TrialPair],
    answers: Sequence[Answer],
    judge_judgments: Sequence[Judgment],
    config: JevConfig,
    unrebuildable: int = 0,
    queue_floor: float = 0.0,
) -> TrialReport:
    """`queue_floor` is `tau_low`: the pre-filter is measured only on
    pairs that can reach the judge's queue. The labels include pairs
    sampled below it (all no, most of them easy), and counting those made
    the first run's pre-filter look twice as effective as it is: 47% of
    pairs dropped over all labels, 23% over the queue's band."""
    in_queue = [t for t in pairs if t.pair.score >= queue_floor]
    queue_ids = {t.pair.pair_id for t in in_queue}
    tuning = [t for t in pairs if in_tuning_half(t.pair.pair_id)]
    held_out = [t for t in pairs if not in_tuning_half(t.pair.pair_id)]
    reports: list[VariantReport] = []
    for variant in sorted({a.variant for a in answers}):
        mine = [a for a in answers if a.variant == variant]
        per_cutoff = [
            (
                cutoff,
                score_judgments(_labels_for(tuning), as_judgments(mine, cutoff)),
                score_judgments(_labels_for(held_out), as_judgments(mine, cutoff)),
            )
            for cutoff in config.cutoffs
        ]
        picked = pick_cutoff(tuning, mine, config)
        held_out_at_pick = (
            None
            if picked is None
            else score_judgments(_labels_for(held_out), as_judgments(mine, picked))
        )
        # Head to head: only pairs both the judge and a human answered,
        # scored at the cutoff picked on the tuning half.
        judged = {j.pair_id for j in judge_judgments}
        shared = [t for t in pairs if t.pair.pair_id in judged]
        head_to_head = None
        if shared and picked is not None:
            ids = {t.pair.pair_id for t in shared}
            head_to_head = (
                score_judgments(_labels_for(shared), list(judge_judgments)),
                score_judgments(
                    _labels_for(shared),
                    as_judgments([a for a in mine if a.pair_id in ids], picked),
                ),
            )
        timed = [a.seconds for a in mine if not a.cached]
        reports.append(
            VariantReport(
                variant=variant,
                per_cutoff=per_cutoff,
                picked=picked,
                held_out_at_pick=held_out_at_pick,
                head_to_head=head_to_head,
                calibration=calibration(pairs, mine),
                prefilter=prefilter(in_queue, mine, config.prefilter_below),
                triage=triage(
                    [a for a in mine if a.pair_id in queue_ids],
                    config.prefilter_below,
                    config.auto_yes_at,
                    {t.pair.pair_id: t.human for t in in_queue},
                ),
                cost=_spent(mine),
                mean_seconds=sum(timed) / len(timed) if timed else None,
            )
        )
    return TrialReport(
        model=config.model,
        pairs=len(pairs),
        matches=sum(t.human for t in pairs),
        unrebuildable=unrebuildable,
        variants=reports,
    )


def _spent(answers: Iterable[Answer]) -> float:
    """What this run paid: answers read from the cache carry the cost of
    the call that first fetched them, and cost nothing now."""
    return sum(a.cost for a in answers if not a.cached)


def _pr(result: JudgeEval) -> str:
    return (
        f"precision {result.precision:.3f} recall {result.recall:.3f} "
        f"({result.true_positive} right, {result.false_positive} wrong, "
        f"{result.false_negative} missed)"
    )


def format_report(report: TrialReport, config: JevConfig) -> str:
    lines = [
        f"bench-jev: {report.model}, {report.pairs} labelled pair(s), "
        f"{report.matches} of them matches",
    ]
    if report.unrebuildable:
        lines.append(f"  {report.unrebuildable} label(s) skipped: no pair file on disk")
    for v in report.variants:
        lines += [
            "",
            f"== {v.variant}",
            "  cutoff   tuning half                      held-out half",
        ]
        for cutoff, tune, held in v.per_cutoff:
            lines.append(
                f"  {cutoff:<6}   P {tune.precision:.3f} R {tune.recall:.3f}"
                f"                  P {held.precision:.3f} R {held.recall:.3f}"
            )
        if v.picked is None or v.held_out_at_pick is None:
            lines.append(
                f"  no cutoff reaches precision {config.target_precision} on the "
                "tuning half: Jev cannot replace the judge at this bar"
            )
        else:
            lines.append(
                f"  picked {v.picked} on the tuning half; held out: "
                + _pr(v.held_out_at_pick)
            )
        if v.head_to_head is not None:
            judge_eval, jev_eval = v.head_to_head
            lines.append(
                f"  head to head on {judge_eval.judged} pair(s) both answered:"
            )
            lines.append("    judge  " + _pr(judge_eval))
            lines.append("    jev    " + _pr(jev_eval))
        lines.append("  calibration (p bin: n, mean p, share that were matches)")
        for b in v.calibration:
            lines.append(
                f"    [{b.low:.1f}, {b.high:.1f}): {b.count:>3}, "
                f"{b.mean_p:.2f}, {b.yes_rate:.2f}"
            )
        f = v.prefilter
        lines.append(
            f"  pre-filter below {f.below}, queue band only: "
            f"drops {f.dropped}/{f.total} pairs, "
            f"losing {f.matches_lost}/{f.matches} matches"
        )
        lines.append("  three-way, queue band only: " + format_triage(v.triage))
        timing = "" if v.mean_seconds is None else f", {v.mean_seconds:.2f} s/call"
        lines.append(f"  cost ${v.cost:.4f}{timing}")
    return "\n".join(lines)


def _api_key(config: JevConfig) -> str:
    key = os.environ.get(config.api_key_env, "")
    if not key:
        raise SystemExit(f"bench-jev: {config.api_key_env} is not set")
    return key


def run_trial(
    data_root: DataRoot,
    config: JevConfig,
    post: Post | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    limit: int | None = None,
    prompt_path: Path = DEFAULT_JEV_PROMPT_PATH,
) -> TrialReport:
    """`nc bench-jev`. `post` is injected so tests never reach the
    network; left None, it is the real API with the key from
    `config.api_key_env`."""
    pairs, unrebuildable = trial_pairs(data_root)
    if limit is not None:
        pairs = pairs[:limit]
    if post is None:
        post = http_post(config, _api_key(config))
    answers = ask_all(
        [t.pair for t in pairs], load_jev_prompt(prompt_path), config, post, cache_dir
    )
    return build_report(
        pairs,
        answers,
        load_judgments(data_root),
        config,
        unrebuildable,
        queue_floor=load_cluster_config().tau_low,
    )


# --- the live queue -------------------------------------------------------------


@dataclass(frozen=True)
class QueueSurvey:
    model: str
    variant: str
    cross: Triage
    same: Triage
    cost: float
    answers: tuple[Answer, ...] = ()
    cross_ids: frozenset[str] = frozenset()


LANES = ("no", "middle", "yes")


def lane_ids(survey: QueueSurvey, lane: str, low: float, high: float) -> list[str]:
    """The cross-outlet pair ids in one lane of the three-way split, for
    `nc label-page --ids`: the way a human checks a lane on the real
    queue rather than trusting the labelled estimate. Cross-outlet only,
    because those are the pairs the judge takes first and the only ones
    that make a story on their own."""
    if lane not in LANES:
        raise ValueError(f"lane must be one of {LANES}, got {lane!r}")

    def lane_of(p: float) -> str:
        return "no" if p < low else "yes" if p >= high else "middle"

    return sorted(
        a.pair_id
        for a in survey.answers
        if a.pair_id in survey.cross_ids and lane_of(a.p) == lane
    )


def survey_queue(
    data_root: DataRoot,
    config: JevConfig,
    post: Post | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    prompt_path: Path = DEFAULT_JEV_PROMPT_PATH,
    variant: str = VARIANT_DATES,
) -> QueueSurvey:
    """`nc bench-jev --queue`: ask Jev every pair in the judge's queue and
    report how the three-way split would divide it. Read-only like the
    rest: answers go to the cache, the queue and the judgments are
    untouched. With dates, because the labelled run found they help.

    Cross-outlet and same-outlet pairs are counted apart: the judge takes
    cross-outlet pairs first (`nc.judge.unjudged_pairs`), so they are the
    ones whose share of the middle lane decides the judge's workload."""
    from nc.judge import unjudged_pairs

    queue = unjudged_pairs(data_root)
    if post is None:
        post = http_post(config, _api_key(config))
    answers = ask_all(
        queue, load_jev_prompt(prompt_path), config, post, cache_dir, [variant]
    )
    cross_ids = {p.pair_id for p in queue if p.a.outlet != p.b.outlet}
    low, high = config.prefilter_below, config.auto_yes_at
    return QueueSurvey(
        model=config.model,
        variant=variant,
        cross=triage([a for a in answers if a.pair_id in cross_ids], low, high),
        same=triage([a for a in answers if a.pair_id not in cross_ids], low, high),
        cost=_spent(answers),
        answers=tuple(answers),
        cross_ids=frozenset(cross_ids),
    )


def format_survey(survey: QueueSurvey) -> str:
    total = survey.cross.no + survey.cross.middle + survey.cross.yes
    same = survey.same.no + survey.same.middle + survey.same.yes
    return "\n".join(
        [
            f"bench-jev --queue: {survey.model}, {survey.variant}, "
            f"{total + same} unjudged pair(s)",
            f"  cross-outlet ({total}): " + format_triage(survey.cross),
            f"  same-outlet ({same}): " + format_triage(survey.same),
            f"  cost ${survey.cost:.4f} (cached answers cost nothing)",
        ]
    )
