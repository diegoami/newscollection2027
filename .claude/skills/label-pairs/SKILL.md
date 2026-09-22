---
name: label-pairs
description: Refresh the "Same Story?" web page with the current unlabelled borderline pairs so the owner can label them by hand, then take the answers back into labels/pairs.jsonl. Use when asked for the labelling UI, the judging page, or to top up the human label set that nc tune and nc bench-judge score against.
---

# The labelling page

`nc label` is the terminal version and needs nothing else. This skill
covers the web page, which exists because a labelling session is two
hundred yes/no answers in a row and a phone is a better place for that
than a terminal.

**https://claude.ai/artifact/8enyepuhfxqc5c6hW1iz7v** — "Same Story?"

## What this is not

Labels are **not** judgments, and the two have opposite effects:

| | writes | effect |
|---|---|---|
| `nc judge`, the judge-pairs skill | `judgments/` | creates links, makes stories |
| `nc label`, this page | `labels/pairs.jsonl` | ground truth, creates nothing |

Labelling does not shorten the judge queue and does not publish a story.
What it does is keep the only independent check on the judge step: `nc
bench-judge` scores an agent's judgments against these human answers,
and `nc tune` reads them to say whether `tau_low` is in the right place.
A label set the agent wrote would make both meaningless.

**Never write `labels/pairs.jsonl` yourself.** The judge-pairs skill
says so and this is why. The page emits the lines; the owner commits
them. If you are asked to transcribe them out of the page's database,
say the rule exists and let the owner decide — do not decide it for
them.

## Refreshing it

One command. Do not assemble the selection by hand:

```
export NC_DATA_ROOT=…
nc sync pull
nc label-page --page <current.html> --out <new.html>
```

Get `<current.html>` by reading the artifact with the Artifact tool —
it saves the full HTML to a file and tells you where. Then publish
`<new.html>` back to **the same url**, omitting `capabilities` so the
stored `db` declaration carries forward. The command prints a table of
labels-per-band before and after; read it, because it is the whole point
of the selection.

`nc label-page` without `--page` writes just the pair JSON, for
inspecting the selection without touching the page.

## Why there is a command rather than a recipe here

This skill used to carry the selection as a code block. That is how it
went wrong. On 2026-09-22 the page was built from the head of
`nc.labelling.order_for_labelling`, which round-robins across score
buckets **and takes the highest score first within each one** — right
for `nc label`, which walks the whole list, and wrong the moment you
truncate it, because you then get the top slice of every bucket.

The result: a page whose lowest pair scored 0.6079 while 38% of the pool
sat below 0.60, about 57% likely matches against the pool's 25%, and not
one new label in `[0.57, 0.60)` — the band with the fewest labels and
the one carrying the whole argument that `tau_low` is not set too low.
The owner caught it by asking whether the page was only sending
positives.

So the selection now lives in `src/nc/labelpage.py` with tests that fail
against both halves of that mistake (`tests/test_labelpage.py`), and the
thresholds live in `config/labelpage.yaml` where CLAUDE.md says
thresholds go. A snippet in a markdown file gets retyped; tested code
does not.

`nc label-page` also refuses to render a page that has lost its `db`
capability, or whose progress readout counts `labels` rather than the
current batch. That second one is the other 2026-09-22 bug: `labels` is
restored from the artifact's database and holds every answer the page
has ever taken, so counting its keys against the batch made the counter
read **222 / 150**. Both invariants only became breakable when the page
stopped being a one-off snapshot, which is exactly when nobody thinks to
re-check them.

**It is a stratified sample, not a representative one.** It is sized to
estimate a boundary, not the pool's overall match rate, and it
over-represents the sparse high bands on purpose. Anyone who wants to
know what share of the pool is a real match needs a random sample and
should say so — reading it off this page gives a confident wrong answer.

## Taking the answers back

The page keeps every answer in the artifact's database under
`labels/<pair_id>` and in the browser's local storage. When the owner
finishes, it shows the JSONL and a copy button, filtered to the current
batch. They append it to `<data root>/labels/pairs.jsonl` and run:

```
nc tune          # precision and recall per threshold, from the labels
nc bench-judge   # how the agent's judgments score against them
nc sync push
```

Filtered to the batch matters: `load_labels` does not deduplicate by
pair id, so a line appended twice is counted twice by `nc tune`.

Answers survive a refresh — the page skips any pair it already has an
answer for — so re-baking an overlapping pool loses nothing.

## What the labels are for

As of 2026-09-22, 198 labels give this, and it is why `tau_low` is 0.57
rather than a guess:

| band | labels | same story | yield |
|---|---|---|---|
| 0.80-0.85 | 5 | 5 | 100% |
| 0.75-0.80 | 8 | 7 | 88% |
| 0.70-0.75 | 11 | 8 | 73% |
| 0.65-0.70 | 30 | 16 | 53% |
| 0.60-0.65 | 33 | 11 | 33% |
| 0.57-0.60 | 10 | 2 | 20% |
| below 0.57 | 101 | **0** | 0% |

101 labelled pairs below `tau_low` and not one a real story, against 20%
yield immediately above it. That is a threshold argument made of
evidence. The thin rows — `[0.57, 0.60)` at ten labels, and 0.75 and up
where `nc tune` still prints its fewer-than-five-labels warning — are
where the next session's answers are worth most, and are what the
allocation aims at.
