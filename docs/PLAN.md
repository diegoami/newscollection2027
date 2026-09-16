# Plan

## Status

Bootstrap documents merged. Storage decided (separate data repo, see
`docs/DECISIONS.md`). Owner has applied the `main` ruleset and created
the empty `newscollection2027-data` repository. Next session starts with
T01 (repo skeleton with CI) and, in the same PR or the next, adds the
`check` status check name to the ruleset. Remaining owner steps: enable
GitHub Pages once `gh-pages` exists (T42) and create `DATA_REPO_TOKEN`
before T13. Nothing else is in progress.

Milestones are sequential. Tasks inside a milestone marked `[P]` can run
in parallel with the other `[P]` tasks of the same milestone. Sizes:
S (< 2 h agent time), M (half a day), L (a day or more). Model tag: `S`
Sonnet 5 implementer, `O` Opus 5 implementer. Owner tasks are marked
`[owner]` and cannot be delegated.

Each task becomes a GitHub issue with the same id. A task is done when its
acceptance criteria are shown in a merged PR.

## M0 Bootstrap

- **T01 (S, size S)** Repo skeleton: `pyproject.toml` with `uv`, `src/nc/`
  layout, `ruff`, `mypy --strict`, `pytest`, `Makefile` with `make check`
  (lint, type, test), `.gitignore` for `.cache/` and `site/`, CI workflow
  running `make check` on pull requests.
  AC: `make check` passes on an empty package; CI is green on a PR.
- **T02 (done in bootstrap commit)** Docs and agent config: this plan,
  `docs/ARCHITECTURE.md`, `docs/WORKFLOW.md`, `CLAUDE.md`, skill stubs,
  PR template.
- **T00 [owner, done]** Storage backend decided: separate data repository.
- **T03 [owner, in progress]** Ruleset on `main` (done): PR required,
  required approvals zero, block force pushes, restrict deletions. Data
  repository `newscollection2027-data` created (done). Still to do: add
  the required status check `check` once T01's CI workflow exists; enable
  GitHub Pages from the `gh-pages` branch after T42; create a fine-grained
  token with contents write on the data repository and store it as the
  secret `DATA_REPO_TOKEN` before T13.
- **T04 (S, size S)** Create one GitHub issue per task below with labels
  `milestone:Mx`, `size:x`, `model:S|O`.

## M1 Ingest

- **T09 (O, size M)** Outlet research and evaluation. The outlet list is
  open. Candidates: the 25 sites the old project scraped, plus any
  English-language tech outlet with a public feed. For each candidate,
  find the feed URLs and score: feed exists and is discoverable; entries
  carry a real summary or lede rather than the title alone; number of
  entries per fetch; posting frequency; canonical links without tracking
  parameters; one feed per outlet or clear section feeds; feed terms of
  use permit aggregation with attribution. Deliverable: `docs/OUTLETS.md`
  with a scoring table and a recommended starting set of 8 to 12 outlets,
  plus the same data as `config/outlets.candidates.yaml`.
  AC: every candidate has a score and a reason; the recommended set has
  no outlet whose feed is title-only.
- **T10 (S, size S) [P]** `config/outlets.yaml` generated from the
  recommended set in `docs/OUTLETS.md`, and `nc feeds check`, which
  fetches every feed and prints item counts, newest date and whether ledes
  are present.
  AC: report shows every configured feed returning items with ledes; dead
  feeds are removed or replaced.
- **T11 (S, size M) [P]** Fetch and normalize: `feedparser`, canonical URL
  (tracking parameters stripped, scheme and host lowercased), item id,
  lede extraction (HTML stripped, first 60 words of summary or content),
  published time in UTC, outlet, author, tags.
  AC: unit tests with one fixture feed per outlet under `tests/fixtures/feeds/`.
- **T12 (S, size S)** Store: `DataRoot` abstraction (a local directory,
  by default a checkout of `newscollection2027-data` at `NC_DATA_ROOT`),
  idempotent append to `items/YYYY/MM/DD.jsonl` keyed by item id;
  `nc db rebuild` builds `.cache/nc.sqlite` from the JSONL files;
  `nc sync pull|push` wraps clone or pull and commit or push of the data
  repo with `data:` commit messages.
  AC: running `nc ingest` twice in a row produces no git diff on the
  second run.
- **T13 (S, size S)** Ingest workflow: `.github/workflows/ingest.yml` in
  the code repo, cron every three hours, concurrency group `data`, checks
  out the data repo with a fine-grained token stored as a secret, runs
  `nc ingest` and `nc cluster`, pushes to the data repo only when files
  changed, then sends a `repository_dispatch` event `data-updated` to the
  code repo.
  AC: two consecutive scheduled runs; the second produces a commit only if
  new items exist.

## M2 Cluster

- **T20 (S, size S)** Embedding module: small local sentence-embedding
  model, cached under `.cache/`, vectors stored in SQLite keyed by item id,
  `nc embed` for items without vectors.
  AC: same input gives the same vector across runs; test with two items.
- **T21 (O, size M)** Clustering: four-day window, `tau_low` and
  `tau_high` thresholds, union-find over linked pairs, clusters emitted
  only with two or more outlets, stable ids, version bump on membership
  change, writes `data/clusters/<date>/<id>.json` and
  `data/pending/<id>.json`.
  AC: tests on a synthetic set of 30 items with known groups; under one
  minute for 5,000 items in the window.
- **T22 (S, size S)** Labelling tools: `nc label` shows near-threshold
  pairs and records yes or no to `data/labels/pairs.jsonl`; `nc tune`
  prints precision and recall per threshold from the labels.
  AC: both commands work on fixture data; docs in `docs/CLUSTERING.md`.
- **T23 [owner]** Label at least 200 near-threshold pairs and set
  `tau_low` and `tau_high` from the `nc tune` report.
- **T24 (S, size S)** Borderline pair judgments: pairs in
  `[tau_low, tau_high)` written to `data/pending-pairs/`, judged by the
  analysis backend with a yes-or-no schema, results merged into the next
  clustering run.
  AC: judged pairs change cluster output in a test; validator accepts the
  judge output schema.

## M3 Analysis contract

- **T30 (O, size M)** Contract: `contract/analysis.schema.json`, pydantic
  models in `nc.contract`, validator with the verbatim quote check,
  `nc validate` command, rejects moved to `data/rejected/<id>.json` with
  reasons.
  AC: tests with at least five valid and ten invalid samples covering
  every rule in `docs/ARCHITECTURE.md`.
- **T31 (O, size M)** API backend: `nc analyze --backend api` using the
  Anthropic Python SDK with structured outputs against the same schema,
  model from config (default `claude-sonnet-5`), prompt text in
  `prompts/analyze.md` shared with the skill, optional `--batch` using the
  Message Batches API.
  AC: runs over the golden clusters with at least 95 percent passing the
  validator on the first try.
- **T32 (O, size M)** Claude Code backend: finalize
  `.claude/skills/analyze-clusters/SKILL.md` so an agent can produce valid
  analyses from pending files using the same prompt; `nc pending` and
  `nc validate --new` support the loop.
  AC: a dry run in a Claude Code session over five pending clusters passes
  the validator.
- **T33 (S, size M)** Golden set and eval: 20 clusters under
  `evals/golden/` with owner-checked expected discrepancies; `nc eval
  --backend api|files` scores schema pass rate, quote validity, and
  discrepancy precision and recall against the golden labels; report
  written to `evals/reports/<date>.md`.
  AC: report committed for both backends.
- **T34 [owner]** Check the 20 golden analyses.

## M4 Site

- **T40 (S, size M) [P]** Site builder: Jinja2 templates, pages listed in
  `docs/ARCHITECTURE.md`, no JS framework, minimal CSS, readable on a
  phone, `nc build` writes `site/`.
  AC: builds from fixture data in under ten seconds; an internal link
  check test passes.
- **T41 (S, size S) [P]** Outlet statistics: discrepancy involvement rate,
  first-to-report rate, story count, computed by one function from the
  analyses and used by the outlet pages and the front page.
  AC: unit test with fixture analyses reproduces hand-computed numbers.
- **T42 (S, size S)** Deploy workflow: on push to `main`, on
  `repository_dispatch` `data-updated`, and manually; checks out both
  repos, `nc build`, publishes `site/` to the `gh-pages` branch.
  AC: site reachable at `https://diegoami.github.io/newscollection2027/`.
- **T43 (S, size S)** Status page from `data/runs/`.
  AC: last run's counts, rejects and durations visible on `/status/`.

## M5 Automation

- **T50 (O, size S)** Nightly skill: finalize
  `.claude/skills/nightly/SKILL.md`, `nc runlog`, and `nc nightly
  --dry-run`, which executes every step except the agent step and the push.
  AC: dry run succeeds in a Claude Code session on real pending data.
- **T51 [owner + orchestrator]** Create the Routine: cron `0 2 * * *`
  (UTC), fresh session, Claude Sonnet 5, notifications on, both
  repositories attached with push access to the data repo, prompt as in
  `docs/WORKFLOW.md`.
  AC: first unattended run commits analyses and the site updates.
- **T52 (S, size S)** Failure surfacing: the ingest workflow opens or
  updates a GitHub issue on failure; the status page shows the last
  successful run age.
  AC: a forced failure creates the issue.

## M6 Quality

- **T60 (O, size M)** Prompt tuning against the golden set using `nc eval`,
  documented in `docs/EVALS.md` with before and after scores.
- **T61 (S, size S)** Weekly cluster quality page: clusters per day,
  singletons dropped, judge decisions, validator reject rate.
- **T62 (S, size S)** Portfolio README: purpose, architecture diagram,
  method, limits, cost, links to the site and the old project.
- **T63 (O, size M, v2)** Second-pass critic for high-severity
  discrepancies: one subagent re-reads each high-severity item against the
  quotes and downgrades or confirms.

## M7 Later

- Netlify: `netlify.toml` with the build command, custom domain.
- Browser extension channel for full text, as a separate contract.
- Hindsight scoring and claim ledger once several months of data exist.

## Open questions

Tracked in `docs/DECISIONS.md` as they are answered.
