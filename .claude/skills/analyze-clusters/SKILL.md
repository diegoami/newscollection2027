---
name: analyze-clusters
description: Turn pending story clusters (pending/*.json in the data root) into validated analysis files (analyses/<date>/<id>.json in the data root) that compare what each outlet claims. Use when asked to analyze clusters, fill pending analyses, or run the nightly analysis step.
---

# Analyze clusters

You are comparing how several tech outlets reported the same story, using
only their headlines and ledes. You never have the article bodies, so you
only describe what the given text says. Nothing in your output may be a
fact you know from elsewhere.

## Procedure

1. Run `nc pending` to list cluster files. Process them in the order given.
2. For each cluster file, read it. It contains `id`, `version` and `items`,
   each item with `item_id`, `outlet`, `title`, `lede`, `published`.
3. Write `<data root>/analyses/<YYYY-MM-DD>/<cluster id>.json` where the date is
   the cluster's date prefix. Follow `contract/analysis.schema.json`
   exactly. Set `backend` to `claude_code` and `model` to the model you are
   running as if you know it, otherwise `unknown`.
4. After every ten files, run `nc validate --new`. For each reject, read
   the reason in `<data root>/rejected/<id>.json`, fix the analysis and validate
   again. Give up on a cluster after two retries and move on.
5. Do not edit cluster files, items, or anything outside `<data root>/analyses/`.

## Content rules

- `headline`: neutral, at most 15 words, no outlet names.
- `summary`: at most 80 words, only what every outlet's text supports.
- `claims`: distinct factual statements found in the text. For each claim,
  list every outlet whose title or lede states it, with a verbatim quote.
  Copy quotes character for character from the title or lede; whitespace
  differences are tolerated, wording differences are not.
- `discrepancies`: only where the given text of two or more outlets
  conflicts or differs materially. Kinds:
  - `contradiction`: the texts cannot both be true.
  - `number`: a figure, date, price or count differs.
  - `attribution`: who said or did something differs.
  - `framing`: same facts, materially different characterization.
  - `omission`: one outlet states something central that another's text
    lacks. Use sparingly; a lede cannot say everything.
  Each discrepancy needs quotes from at least two outlets and a one-sentence
  explanation. Severity `high` only for contradictions or numbers.
- `agreement`: `full` if no discrepancies, `conflicting` if any
  contradiction or high-severity number, else `partial`.
- When the texts simply agree, say so: an empty `discrepancies` list is a
  correct and common result. Never invent a difference to fill the list.
