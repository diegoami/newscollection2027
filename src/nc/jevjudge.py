"""`nc judge --backend jev`: Jev answers the confident ends of the queue.

**Why.** The judge could not keep up with the borderline band (about 90
new cross-outlet pairs a day against a nightly budget of 150, and a
backlog of about a thousand in September 2026), and the Jev trial
(nc/jevtrial.py) found that Jev's confident answers are the judge's
answers:

- on the labels, pairs Jev put at 0.9 or above were 97-100% matches,
  and pairs below 0.1 were 2-3%;
- on the live queue, 2026-09-26, the Claude judge agreed with Jev's
  confident answers on 70 of 71 pairs, and the owner agreed on 31 of
  31 of the ones the judge had not reached.

So Jev answers those two ends and the judge answers the middle. That is
a three-way split, not a replacement: the middle is where the hard
calls are, and it stays with the model that can say why.

**Where it sits in the contract.** Jev is a third backend of the same
file contract (nc/judge.py's module docstring): it writes
`judgments/<pair_id>.json` with `backend: jev`, the validator decides
what links, and a human label still outranks it. It runs in the
nightly's judge step, before the Claude judge, so the judge's budget
goes to pairs Jev could not settle. It is not part of `nc cluster`:
CLAUDE.md keeps deterministic code free of model calls.

**Off unless it has a key.** Without the key named in
`config/judge.yaml` (`jev.api_key_env`) this does nothing and says so,
so the nightly behaves exactly as before until someone gives the
Routine a key. A network failure mid-run keeps what was answered and
stops: a broken provider must cost a night's Jev answers, never the
night.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nc.jevtrial import (
    DEFAULT_CACHE_DIR,
    DEFAULT_JEV_PROMPT_PATH,
    VARIANT_DATES,
    Answer,
    JevConfig,
    Post,
    ask_all,
    http_post,
    load_jev_prompt,
)
from nc.judge import BACKEND_JEV, Judgment, unjudged_pairs, write_judgments
from nc.store import DataRoot

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class JevJudgeReport:
    queue: int
    asked: int
    yes: int
    no: int
    middle: int
    written: int
    cost: float
    skipped: str | None = None  # why nothing ran
    error: str | None = None  # why the run stopped early


def verdict(answer: Answer, config: JevConfig) -> bool | None:
    """True or False at the confident ends, None for the judge."""
    if answer.p >= config.auto_yes_at:
        return True
    if answer.p < config.prefilter_below:
        return False
    return None


def _reason(answer: Answer, same_story: bool, config: JevConfig) -> str:
    if same_story:
        return (
            f"Jev put p={answer.p:.2f} on both reporting the same event, at or "
            f"above {config.auto_yes_at}, where it links without the judge"
        )
    return (
        f"Jev put p={answer.p:.2f} on both reporting the same event, below "
        f"{config.prefilter_below}, where it is settled without the judge"
    )


def judge_with_jev(
    data_root: DataRoot,
    config: JevConfig,
    post: Post | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    prompt_path: Path = DEFAULT_JEV_PROMPT_PATH,
    now: datetime | None = None,
) -> JevJudgeReport:
    queue = unjudged_pairs(data_root)
    if post is None:
        key = os.environ.get(config.api_key_env, "")
        if not key:
            return JevJudgeReport(
                queue=len(queue),
                asked=0,
                yes=0,
                no=0,
                middle=0,
                written=0,
                cost=0.0,
                skipped=f"{config.api_key_env} is not set",
            )
        post = http_post(config, key)

    prompt = load_jev_prompt(prompt_path)
    answers: list[Answer] = []
    error = None
    for pair in queue:
        try:
            answers += ask_all([pair], prompt, config, post, cache_dir, [VARIANT_DATES])
        except Exception as exc:  # a broken provider must not cost the night
            error = f"{type(exc).__name__}: {exc}"
            break

    judged_at = (now or datetime.now(UTC)).strftime(_TIME_FORMAT)
    members = {pair.pair_id: pair for pair in queue}
    judgments = []
    for answer in answers:
        same_story = verdict(answer, config)
        if same_story is None:
            continue
        pair = members[answer.pair_id]
        judgments.append(
            Judgment(
                pair_id=pair.pair_id,
                item_id_a=pair.a.item_id,
                item_id_b=pair.b.item_id,
                same_story=same_story,
                reason=_reason(answer, same_story, config),
                backend=BACKEND_JEV,
                model=answer.model or config.model,
                judged_at=judged_at,
            )
        )
    yes = sum(j.same_story for j in judgments)
    return JevJudgeReport(
        queue=len(queue),
        asked=len(answers),
        yes=yes,
        no=len(judgments) - yes,
        middle=len(answers) - len(judgments),
        written=write_judgments(data_root, judgments),
        cost=sum(a.cost for a in answers if not a.cached),
        error=error,
    )


def format_jev_judge_report(report: JevJudgeReport) -> str:
    if report.skipped:
        return f"judge --backend jev: skipped, {report.skipped}"
    lines = [
        f"judge --backend jev: asked {report.asked} of {report.queue} unjudged "
        f"pair(s): {report.yes} yes, {report.no} no, {report.middle} left "
        f"for the judge; wrote {report.written} judgment file(s), "
        f"${report.cost:.4f}"
    ]
    if report.error:
        lines.append(
            f"judge --backend jev: stopped early on {report.error}; "
            "the answers before it were kept"
        )
    return "\n".join(lines)
