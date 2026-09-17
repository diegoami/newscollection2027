---
name: analyze-clusters
description: Turn pending story clusters (pending/*.json in the data root) into validated analysis files (analyses/<date>/<id>.json in the data root) that compare what each outlet claims. Use when asked to analyze clusters, fill pending analyses, or run the nightly analysis step.
---

# Analyze clusters

Read `prompts/analyze.md` first and follow it exactly. It is the whole
brief — what an analysis contains, the verbatim quote rule, how to decide a
discrepancy, and what to check before handing one over. It is also the
prompt the API backend uses, so the two backends answer the same question
and the evals mean something. This file covers only the loop around it: which
files to read, where to write, and how to get a rejection fixed.

## The loop

1. `nc pending` lists the clusters awaiting analysis, oldest first. Work
   them in that order; a story is worth least once it is a day old.
2. For each one, read `<data root>/pending/<cluster id>.json`. It holds
   `id`, `version`, `status` and `items`, each item with `item_id`,
   `outlet`, `title`, `lede` and `published`.
3. Write `<data root>/analyses/<YYYY-MM-DD>/<cluster id>.json`, where the
   date is the cluster id's own prefix. Follow `prompts/analyze.md` and
   `contract/analysis.schema.json`. Set `backend` to `claude_code`,
   `model` to the model you are running as if you know it and otherwise
   `unknown`, and `generated_at` to now in UTC (`YYYY-MM-DDTHH:MM:SSZ`).
4. After every ten files, run `nc validate --new`. For each rejection, read
   `<data root>/rejected/<cluster id>.json` — it holds every problem found,
   not just the first, plus the analysis you wrote — fix them all in one
   pass and write the file again.
5. Give up on a cluster after two retries. Leave the rejection in place and
   move on; it stays on the queue and tomorrow's run sees it again.
6. Stop when `nc pending` is empty.

## What the validator will reject, in order of how often

Read the rejection file rather than guessing, but these are the ones worth
knowing before you write:

- **A quote that is not verbatim.** Copy from the item's title or lede;
  never retype from memory or tidy the punctuation. Only whitespace is
  forgiven.
- **A quote that straddles the title and the lede.** It has to sit entirely
  inside one or the other.
- **A word limit.** 15 words for `headline`, 80 for `summary`. The schema's
  character caps are looser than the contract, so passing the schema is not
  enough.
- **A stale `cluster_version`.** If `nc cluster` has run since you read the
  file, the cluster may have gained an item and bumped its version. Re-read
  the pending file and redo the analysis against the new membership rather
  than editing the old one.
- **An `item_id` or `outlet` that is not the cluster's.** Copy both out of
  the pending file.

## Rules

- Never edit anything outside `<data root>/analyses/`. Not cluster files,
  not pending files, not items, and never `labels/pairs.jsonl` or
  `judgments/` — those are other steps' evidence.
- Do not set a cluster's `status` yourself. `nc validate` marks a cluster
  analyzed once an analysis of that version passes, and the next
  `nc cluster` run removes its pending file. Editing the status by hand
  retires a story that nothing has actually analysed.
- Do not go and read the articles. You analyse the text in the pending
  file, which is what every quote is checked against.
- An empty `discrepancies` list is a correct result. Never invent a
  difference to fill it.
