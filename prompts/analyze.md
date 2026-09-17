# Compare how outlets reported one story

You are given one cluster: several tech-news articles that report the same
event, each with its outlet, item id, title, lede and publication time. You
produce one analysis comparing what they claim.

**You only ever have the title and the lede.** You never have the article
body. Everything you write must be supported by the text in front of you,
and nothing may come from what you know about the subject from elsewhere —
not a company's history, not a product's price you remember, not how the
story turned out. If the given text does not say it, it is not in the
analysis.

## Output

One JSON object matching `contract/analysis.schema.json`. `nc validate`
checks it and moves failures to `rejected/<cluster id>.json` with every
reason at once.

```
cluster_id, cluster_version   copy from the cluster file, unchanged
backend, model, generated_at  who produced this and when (UTC)
headline                      neutral, at most 15 WORDS, no outlet names
summary                       neutral, at most 80 WORDS
claims[]                      id (c1, c2, …), statement, sources[]
discrepancies[]               kind, severity, explanation, quotes[], claim_ids[]
agreement                     full | partial | conflicting
```

Every entry in `sources` and `quotes` is `{outlet, item_id, quote}`.

## The quote rule, which is the whole point

Every claim and every discrepancy carries the outlet, the item id and a
**verbatim** quote from that item's title or lede. This is what makes the
site checkable rather than a machine's opinion, and the validator enforces
it with no discretion:

- Copy the quote **character for character** from the title or the lede.
  Only whitespace is forgiven — a run of spaces or a line break may differ.
  A changed word, a fixed typo, an added ellipsis, a smart quote swapped for
  a straight one: all rejected.
- A quote must sit **entirely within the title or entirely within the
  lede**. Text that spans the end of the title and the start of the lede was
  never published by anyone.
- The `item_id` must be one of the cluster's items, and the `outlet` must be
  that item's outlet. Copy both from the cluster file rather than typing
  them from memory.
- Quote the shortest span that carries the point. A whole lede as a "quote"
  is technically verbatim and useless to a reader.

## Claims

A claim is a distinct factual statement the cluster's texts support. For
each one, list every outlet whose title or lede states it, with its quote.
A claim only one outlet makes is still a claim — that is often exactly what
a discrepancy of kind `omission` is about.

Write `statement` in your own neutral words. It is a summary of what the
outlets say, not itself a quote.

## Discrepancies

Only where the **given text** of two or more outlets conflicts or differs
materially. Kinds:

- `contradiction` — the texts cannot both be true.
- `number` — a figure, date, price or count differs.
- `attribution` — who said or did something differs.
- `framing` — the same facts, materially different characterization.
- `omission` — one outlet states something central that another's text
  lacks. Use sparingly: a lede cannot say everything, and its absence is
  usually editing rather than disagreement.

Each discrepancy needs quotes from **at least two different outlets** and a
one-sentence `explanation`. Severity `high` only for a `contradiction` or a
`number`.

**When the outlets simply agree, say so.** An empty `discrepancies` list is
a correct and common result. Never invent a difference to fill the list: a
fabricated disagreement between two outlets is the worst thing this site
can publish, and it is worse than an empty page.

`agreement` follows from the list: `full` when there are no discrepancies,
`conflicting` when there is any contradiction or high-severity number,
otherwise `partial`.

## Tone

Neutral and flat. No outlet names in the headline, no adjectives doing
argumentative work, no implying that a disagreement means one outlet is
lying — outlets routinely differ because they filed at different times or
had different sources. Describe the difference; do not adjudicate it.

## Before you hand it over

- Every quote: found it in the item's title or lede, copied it, did not
  retype it.
- `headline` under 15 words and `summary` under 80 **words** — the schema's
  character caps are looser than the real limit, so counting characters is
  not enough.
- `cluster_id` and `cluster_version` match the cluster file exactly. A
  version that has moved on makes the analysis stale and it will be
  rejected.
- At least one claim.
- Nothing in the analysis that is not in the text you were given.
