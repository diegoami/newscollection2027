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
   Where the data root lives (a data repository or object storage) is an
   open decision, see `docs/STORAGE.md`; the code only sees a directory.
4. **Nothing unattributed reaches the page.** Every claim and every
   discrepancy carries the outlet, the item id and the verbatim sentence it
   came from. The validator rejects anything else.

## Runtime topology

```
 every 3h                          04:00 Europe/Berlin
 GitHub Actions (free)             Claude Code Routine (subscription, Sonnet 5)
 ┌───────────────────────┐         ┌──────────────────────────────────────┐
 │ nc ingest             │         │ git pull                             │
 │ nc cluster            │ commit  │ nc pending      -> list cluster files │
 │ -> data/items/…       │──main──>│ agent writes data/analyses/…         │
 │ -> data/clusters/…    │         │ nc validate     -> accept / reject    │
 │ -> data/pending/…     │         │ nc build        -> smoke check        │
 └───────────────────────┘         │ commit + push main                    │
                                   └──────────────────────────────────────┘
                                                     │ push to main
                                                     ▼
                                   GitHub Actions deploy: nc build -> gh-pages
                                   (later: Netlify build from the same command)
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
id           <YYYY-MM-DD>-<6 hex of sorted item ids>   (date of earliest item)
version      increments when membership changes; analysis is per version
items        list of item ids with outlet, title, lede, published
status       pending | analyzed | rejected
```

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

## Clustering

Window: items from the last 4 days. Similarity: cosine over embeddings of
`title + " " + lede` from a small local sentence-embedding model. Pairs
above `tau_high` link automatically; pairs in `[tau_low, tau_high)` are
written as pending pair judgments for the LLM backend; pairs below
`tau_low` never link. Connected components form clusters. A cluster is
emitted only with two or more distinct outlets. Thresholds are tuned with
`nc label` (human labels a few hundred near-threshold pairs) and
`nc tune` (precision and recall per threshold). Cluster ids are stable
across runs; membership changes bump `version` and re-queue the cluster.

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
