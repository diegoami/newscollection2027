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
says so and this is why. The page emits the lines; `nc label --import`
records them. Never append to the file with an editor, a heredoc or a
shell redirect — not even lines you believe you read out of the page's
database, because "I read them correctly" is exactly the claim the
import exists to stop anyone having to take on trust.

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

The same day, twice more. The counter (below), and the **order**: the
page emitted band by band, highest first, so the first fifty pairs the
owner answered were 77% matches and every negative waited in the last
fifty. The sample was fine — 48% matches against the pool's 36% — and
the experience of it was not, which is the form the owner's third
report took: "almost all of them still seem to be positive". It is also
a real defect and not just an unpleasant hour, because a session that
stops early is the normal way one ends, and a session that stopped
early contributed only the top bands. `interleave` now spreads the
bands so every prefix of the page carries the mix of the whole, and
within a band the order is a digest of the pair id so a pair's position
says nothing about its likely answer.

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
`labels/<pair_id>` and in the browser's local storage, and shows the
JSONL with a copy button. Either route — the owner's copy button, or
an agent reading the rows out of the database with `ArtifactData` —
ends at the same command, which is the only thing permitted to write
the file:

```
nc label --import answers.jsonl --dry-run   # show the owner first
nc label --import answers.jsonl
nc tune          # precision and recall per threshold, from the labels
nc bench-judge   # how the agent's judgments score against them
nc sync push
```

**Always the `--dry-run` first, and show the owner its output** before
recording anything. The import is a guard against a wrong
transcription, not a licence to skip the owner.

The import checks every line against the pair files `nc cluster` wrote:
the pair must exist in `pending-pairs/` or `label-sample/`, its score
must be the score the clusterer computed, and its outlets must be that
pair's outlets. A pair already labelled the other way is a conflict and
stops the batch rather than overwriting — the file is append-only and a
changed mind is a thing to look at. **Nothing is written unless every
line passes**, because a partial import is both incomplete and
indistinguishable from a complete one. What gets recorded is built from
the pair on disk; the only fields taken from the import are the answer
and its timestamp, the only two things a human actually produced.

So re-importing a page after more of it is answered is safe and normal:
already-recorded answers are reported and skipped, not duplicated. That
also removes the old footgun here — `load_labels` does not deduplicate
by pair id, so a line appended twice by hand was counted twice by `nc
tune`.

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
