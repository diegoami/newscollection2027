---
name: label-pairs
description: Refresh the "Same Story?" web page with the current unlabelled borderline pairs so the owner can label them by hand, then take the answers back into labels/pairs.jsonl. Use when asked for the labelling UI, the judging page, or to top up the human label set that nc tune and nc bench-judge score against.
---

# The labelling page

`nc label` is the terminal version of this and needs nothing else. This
skill covers the web page, which exists because a human labelling
session is two hundred yes/no answers in a row and a phone on a sofa is
a better place for that than a terminal.

**https://claude.ai/artifact/8enyepuhfxqc5c6hW1iz7v** — "Same Story?"

## What this is not

Labels are **not** judgments, and the two have opposite effects:

| | writes | effect |
|---|---|---|
| `nc judge`, the judge-pairs skill | `judgments/` | creates links, makes stories |
| `nc label`, this page | `labels/pairs.jsonl` | ground truth, creates nothing |

Labelling does not shorten the judge queue and does not publish a
story. What it does is keep the only independent check on the judge
step: `nc bench-judge` scores an agent's judgments against these human
answers, and `nc tune` reads them to say whether `tau_low` is in the
right place. A label set the agent wrote would make both meaningless.

**Never write `labels/pairs.jsonl` yourself.** The judge-pairs skill
says so and this is why. The page emits the lines; the owner commits
them. If you are ever asked to transcribe them from the page's
database, say that the rule exists and let the owner decide — do not
decide it for them.

## Refreshing the page

The pairs are baked into the page as JSON, so a stale page shows pairs
that have since been labelled. Regenerate from the pool:

```python
# NC_DATA_ROOT set, after `nc sync pull`
from nc.cluster import load_cluster_config
from nc.labelling import labelling_pool, load_labels, order_for_labelling
from nc.store import DataRoot

root = DataRoot.from_env()
done = {label.pair_id for label in load_labels(root)}
todo = [p for p in labelling_pool(root) if p.pair_id not in done]
config = load_cluster_config()
ordered = order_for_labelling(todo, config.tau_low, config.tau_high)[:150]
```

`order_for_labelling` round-robins across score buckets, so the first
answers span the whole band and a session that stops early still
improves every row of `nc tune` rather than one. 150 keeps the page
around 150KB; take fewer if the pool is small, never the whole pool.

**Anything that reads as progress must be scoped to `PAIRS`.** The
page's `labels` map outlives a batch: it is restored from the artifact's
database and from local storage, so it holds every answer ever given the
page, not this batch's. The counter counted its keys against
`PAIRS.length` and read `222 / 150` the first time the pairs were
refreshed -- a numerator and denominator measuring different sets. That
invariant held for free while the page was a snapshot and broke the
moment it became refreshable. The rail and the JSONL export were already
scoped with `PAIRS.filter(p => labels[p.id])`; the counter is now too.

Then swap the JSON inside `<script id="pairs-data" type="application/json">`
in the artifact's current HTML — read it with the Artifact tool, replace
that one block, publish back to **the same url**. Change nothing else:
the page's runtime code, design and `db` capability are already right.
Omit `capabilities` on the republish so the stored declaration carries
forward.

## Taking the answers back

The page keeps every answer twice: in the artifact's database under
`labels/<pair_id>`, and in the browser's local storage. When the owner
finishes, the page shows the JSONL and a copy button. They append it to
`<data root>/labels/pairs.jsonl` and run:

```
nc tune          # precision and recall per threshold, from the labels
nc bench-judge   # how the agent's judgments score against them
nc sync push
```

Answers survive a refresh: the page filters out any pair it already has
an answer for, so re-baking a pool that overlaps an unfinished session
loses nothing.

## What the labels are for

As of 2026-09-22, 198 labels give this, and it is the reason `tau_low`
is 0.57 rather than a guess:

| band | labels | same story | yield |
|---|---|---|---|
| 0.80-0.85 | 5 | 5 | 100% |
| 0.75-0.80 | 8 | 7 | 88% |
| 0.70-0.75 | 11 | 8 | 73% |
| 0.65-0.70 | 30 | 16 | 53% |
| 0.60-0.65 | 33 | 11 | 33% |
| 0.57-0.60 | 10 | 2 | 20% |
| below 0.57 | 101 | **0** | 0% |

101 labelled pairs below `tau_low` and not one is a real story; the
bands above it yield 20% upwards. That is what a threshold argument
looks like when it is made of evidence, and it is why the bands with
fewest labels -- 0.75 and up, where `nc tune` still prints the
fewer-than-five-labels warning -- are worth the next session's answers.
