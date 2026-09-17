# Architecture

newscollection2027 is a static tech-news site that groups feed items from
several outlets into stories and checks the outlets' claims against each
other. It is the successor to techcontroversy.com, rebuilt around three
constraints: feeds only (no article bodies), portfolio-grade cost (near
zero), and the LLM step running inside a Claude Code Routine on a
subscription rather than through metered API calls.

## Principles

1. **Deterministic first.** Ingest, clustering, validation and site
   generation are plain Python with no LLM. They run anywhere, including a
   free GitHub Actions runner, and their output is reproducible from the
   committed data.
2. **The LLM step is a file contract.** Python writes pending cluster
   files; an agent (Claude Code in a Routine, or the SDK backend) writes
   analysis files; Python validates them against a schema and a verbatim
   quote check. The rest of the system never knows which backend ran.
3. **Data is text files under one data root.** Daily JSONL and
   per-cluster JSON files. SQLite is a rebuildable cache, never stored.
   The data root is a checkout of the separate repository
   `newscollection2027-data`; object storage replaces it later (see
   `docs/STORAGE.md`). The code only sees a directory.
4. **Nothing unattributed reaches the page.** Every claim and every
   discrepancy carries the outlet, the item id and the verbatim sentence it
   came from. The validator rejects anything else.

## Runtime topology

```
 every 3h                          04:00 Europe/Berlin
 GitHub Actions (free)             Claude Code Routine (subscription, Sonnet 5)
 ┌───────────────────────┐         ┌──────────────────────────────────────┐
 │ nc sync pull          │         │ nc sync pull                         │
 │ nc ingest             │  push   │ nc pending      -> list cluster files │
 │ nc cluster            │──data──>│ agent writes analyses/…              │
 │ -> items/…            │  repo   │ nc validate     -> accept / reject    │
 │ -> clusters/…         │         │ nc build        -> smoke check        │
 │ -> pending/…          │         │ nc sync push    -> data repo          │
 └───────────────────────┘         └──────────────────────────────────────┘
              │                                      │
              └────────── repository_dispatch ───────┘
                                   ▼
                   GitHub Actions deploy (code repo): checkout both repos,
                   nc build -> gh-pages   (later: Netlify, same command)
```

Why split ingest from analysis: feeds only expose the last 10 to 30 items,
so polling every few hours is needed to avoid gaps, and that polling should
not spend a Claude Code session. The nightly Routine is short and does only
the agentic step plus publish.

## Components

| Component | Module | Input | Output |
|---|---|---|---|
| Outlet registry | `config/outlets.yaml` | hand-maintained | feed URLs, slug, display name, homepage |
| Ingest | `nc.feeds` | RSS/Atom | normalized items appended to `data/items/YYYY/MM/DD.jsonl` |
| Store | `nc.store` | JSONL | SQLite cache (`.cache/nc.sqlite`), rebuilt on demand |
| Embed | `nc.embed` | title + lede | vectors in SQLite (not committed) |
| Cluster | `nc.cluster` | vectors, window of recent items | `data/clusters/<date>/<id>.json`, `data/pending/<id>.json` |
| Contract | `nc.contract` | analysis JSON | pydantic models, JSON schema, validator |
| Analyze (api backend) | `nc.analyze.api` | pending cluster | analysis JSON via Anthropic SDK, model `claude-sonnet-5` |
| Analyze (claude_code backend) | `.claude/skills/analyze-clusters` | pending cluster | analysis JSON written by the agent |
| Site | `nc.site` | data dir | static HTML in `site/` |
| CLI | `nc.cli` | | `nc ingest`, `cluster`, `pending`, `validate`, `analyze --backend api`, `build`, `label`, `tune`, `eval`, `nightly` |

## Data model

**Item** (one feed entry)

```
id           sha1 of canonical URL (utm and tracking params stripped)
outlet       slug from outlets.yaml
url, title, lede (plain text, first ~60 words of summary/content), author
published    ISO 8601 UTC
fetched      ISO 8601 UTC
tags         list of strings from the feed
```

**Cluster** (one story, two or more outlets)

```
id           <YYYY-MM-DD>-<6 hex of sha1 of the anchor item id>
             (date of the anchor item)
anchor       the earliest-published item when the cluster was first
             emitted; chosen once and never recomputed
version      increments when membership changes; analysis is per version
items        list of item ids with outlet, title, lede, published
status       pending | analyzed | rejected | superseded
superseded_by  the cluster that absorbed this one, when superseded
```

The id comes from the anchor rather than from the membership, because an
id that is a function of its members cannot also be stable when those
members change: a fourth outlet arriving late to a story would rewrite
the hash, produce a different cluster, and orphan the analysis written
against the old id. Late arrivals are the normal case, so the anchor is
fixed when the cluster is born and the id survives every later change.
The cost is that the date prefix is the earliest item known at that
moment, not necessarily the earliest the cluster will ever hold; the
true earliest is always in `items`, and the site orders by that.

Membership is monotonic. The four-day window decides which items can
form new links, never what a cluster already contains, so a cluster
whose oldest member has left the window does not shrink, bump its
version, or re-queue itself for analysis. When a new item bridges two
clusters, the one with the earlier anchor survives and takes the whole
component; the other keeps its own membership, becomes `superseded` and
names its survivor, so an analysis already written against it stays
readable as history rather than disappearing.

**An analysis is current** when its `cluster_id` names a cluster whose
status is not `superseded` and its `cluster_version` equals that
cluster's current `version`. Anything else is history. The validator,
the site and the nightly run all decide staleness this way.

**The queue drains and refills by itself.** A cluster's `status` is the
fact and `pending/` is a mirror of it, so the two can never disagree
about what is waiting:

```
nc cluster   new cluster            -> pending
             membership changed     -> version + 1, back to pending
             membership unchanged   -> status kept as it was
nc validate  analysis passes        -> analyzed, pending file removed
             analysis rejected      -> back to pending
```

Only `nc validate` ever writes `analyzed`, because only the validator
knows an analysis both exists and holds up -- CLAUDE.md gives that
decision one home and this is it. Everything else follows from
clustering's existing rule that an unchanged cluster is re-emitted
untouched: `analyzed` sticks until the membership actually changes, and
a story that gains a fourth outlet is re-queued without anything having
to track which analyses are stale.

**Analysis** (see `contract/analysis.schema.json`)

```
cluster_id, cluster_version, backend, model, generated_at
headline     neutral, <= 15 words
summary      neutral, <= 80 words, only what all outlets agree on
claims[]     statement + list of {outlet, item_id, quote}
discrepancies[]
             kind: contradiction | number | attribution | framing | omission
             severity: low | medium | high
             quotes: >= 2 entries from >= 2 outlets, {outlet, item_id, quote}
             explanation: one sentence
agreement    full | partial | conflicting
```

The validator enforces: schema, every `item_id` belongs to the cluster,
every `outlet` matches that item, every `quote` is a verbatim substring of
that item's title or lede after whitespace normalization, at least one
claim, discrepancy quotes span at least two outlets, and length limits.
A rejected analysis is moved to `data/rejected/` with the reason so the
agent or the eval can retry.

**The API backend asks the model for less than the file contains.** Five
of an analysis's fields -- `cluster_id`, `cluster_version`, `backend`,
`model`, `generated_at` -- are knowable for certain by the code making
the request, so `nc.analyze` strips them from the schema it sends and
stamps them afterwards. A model that guesses `cluster_version` invents
stale analyses; one that writes its own `model` field fabricates the
provenance the evals read. The subset is derived from the published
schema rather than restated, so there is nothing to keep in step.

`nc.contract` does that in three layers, because an analysis can be
wrong in three ways no single tool catches. *Shape* is
`contract/analysis.schema.json` checked with `jsonschema` -- that file
stays the published contract, read from disk rather than restated in
code, and it is what T31 hands the SDK as a structured-output schema.
*Types* are pydantic models, so the rest of the code gets an object
rather than a dict. *Truth* is the check against the cluster, which is
the part that matters: a schema can say a quote is a string of 3 to 400
characters, but only this layer can say the string appears verbatim in
the item it claims to come from.

Two details that look like details and are not. The word limits live in
the third layer, not the schema: JSON Schema cannot count words, so the
schema's 120 and 600 character caps are a guard rail and the 15- and
80-word limits above are the contract. And a rejected analysis is
*moved* out of `analyses/`, not copied -- leaving it would let `nc build`
read something the validator refused, which is the bypass CLAUDE.md
forbids. Every problem is reported at once rather than the first one
found, because an agent that learns one fault per round trip makes one
round trip per fault.

## Clustering

Window: items from the last 4 days. Similarity: cosine over embeddings of
`title + " " + lede` from a small local sentence-embedding model. Pairs
above `tau_high` link automatically; pairs in `[tau_low, tau_high)` are
written as pending pair judgments for the LLM backend; pairs below
`tau_low` never link. Connected components form clusters. A cluster is
emitted only with two or more distinct outlets. Cluster ids are stable
across runs; membership changes bump `version` and re-queue the cluster.

**The embedding is a filter, not a classifier, and the LLM does the
deciding.** That was not the original plan -- the thresholds were meant
to be tuned into a decision rule -- but 174 labelled pairs and a
six-model comparison showed cosine cannot separate "same story" from
"same topic" at any threshold or any model size (docs/CLUSTERING.md).
So `tau_high` is 1.00: nothing links on a judgement about similarity,
only on identical text. `tau_low` is not a tuned threshold either. It
is the minimum score worth spending an LLM call on -- a budget dial
between recall and the judge's bill, not a precision setting. Every
real yes-or-no is made by the judge (T24), which is the only part of
the pipeline that can tell an event from a subject.

`nc label` and `nc tune` remain, with their purpose changed: the
labelled pairs are now most useful as an evaluation set for the judge
rather than as tuning data for the thresholds. `nc bench-judge` is what
spends them that way.

The judge is a file contract, like the analysis step:
`pending-pairs/<pair_id>.json` in, `judgments/<pair_id>.json` out,
`nc judge --validate` decides what may link, and
`nc.judge.accepted_links` is the only path from a judgment into
`cluster_items`. `nc cluster` still calls no LLM -- it reads files the
LLM step already wrote. Both backends (`claude_code` in the nightly
Routine, `api` for backfills and evals) write the same files and the
rest of the system never knows which ran. See docs/CLUSTERING.md, "The
judge".

## Site

Static pages, no JavaScript framework, minimal CSS, works on a phone.

- `/` today's stories, ordered by number of outlets then severity
- `/story/<id>/` claims table (rows: claims, columns: outlets) and the
  discrepancy list with quotes
- `/outlet/<slug>/` how often this outlet is involved in discrepancies,
  how often it reports first, recent stories
- `/archive/<date>/`
- `/method/` how the pipeline works, what it cannot do, and the cost
- `/status/` last run log: counts, rejects, durations

## Cost model

- Ingest, clustering, site build: free (GitHub Actions minutes on a public
  repo).
- Nightly analysis: Claude Code Routine on the subscription. Roughly fifty
  clusters a night at around a thousand input tokens each.
- API backend: used for local development, evals and backfills only.
  Claude Sonnet 5 list price is $2 per million input tokens and $10 per
  million output tokens, half that through the Batch API. Fifty clusters a
  day would be in the region of $8 to $15 a month if it were the only
  backend.
- Hosting: GitHub Pages during prototyping, Netlify afterwards. Both free
  at this size.

## Non-goals for v1

- No article bodies. Discrepancies are about headlines and ledes.
- No user accounts, no search, no database server.
- No hindsight scoring or prediction ledger. The data layout allows adding
  them later without migration.
