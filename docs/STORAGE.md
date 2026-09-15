# Storage options for pipeline data

Open decision. The bootstrap plan assumed data files committed to the code
repository; the owner wants alternatives considered first. This page lays
them out so the choice can be made once.

## What has to be stored

| Kind | Volume | Written by | Read by |
|---|---|---|---|
| Items (feed entries) | ~600 a day, ~1 KB each, ~200 MB a year uncompressed | ingest, every 3 h | cluster, site build |
| Embedding vectors | ~1.5 KB each, rebuildable | cluster | cluster |
| Clusters and pending files | ~50 a day, ~3 KB each | cluster | agent, validate, site |
| Analyses | ~50 a day, ~4 KB each | agent (Routine or API) | validate, site, evals |
| Labels, golden set, run logs | tiny | owner, evals, nightly | tune, evals, status page |

Everything except vectors is text. The whole first year fits in a few
hundred megabytes. Three processes need read and write access: the ingest
job in GitHub Actions, the nightly Claude Code Routine, and the site build.

## Options

### A. Same repository (bootstrap assumption)

Data files committed to `data/**` in the code repo.

- For: zero infrastructure; history is the audit trail; the Routine already
  has push access; a push naturally triggers the deploy; anyone can browse
  the data on GitHub.
- Against: code and data history are mixed; every data push runs CI; the
  repo grows by a few hundred megabytes a year; bot commits dominate the
  log. Using git as a database feels wrong to many readers of a portfolio.

### B. Separate data repository

`newscollection2027-data`, public, holding exactly the `data/**` tree.
The code repo stays code only. The site build clones the data repo. A
data push sends a `repository_dispatch` to the code repo to trigger the
deploy, or the deploy runs on its own schedule after the nightly Routine.

- For: everything in A, without polluting the code repo; the data repo can
  be squashed or archived yearly; the file contract and `nc` commands stay
  as designed; still zero infrastructure and zero credentials beyond the
  GitHub access the Routine already has.
- Against: two repositories to attach to the Routine and to CI; one more
  workflow for the dispatch; still git as a database, just elsewhere.

### C. Object storage

Cloudflare R2 (10 GB free, no egress fees) or Backblaze B2 or S3, holding
the same `data/**` tree as objects. `nc sync pull` and `nc sync push`
mirror it to a local directory before and after each step.

- For: proper storage with no growth concern; the code repo is clean; the
  data can be served directly if ever needed.
- Against: credentials must live in GitHub Actions secrets and in the
  Claude Code environment's variables for the Routine; no history unless
  versioning is turned on (S3 has it, R2 does not); harder to browse; the
  deploy needs an explicit trigger since nothing pushes to git.

### D. Hosted database

Turso (libSQL, generous free tier) or Neon Postgres. The SQLite cache
becomes the store.

- For: queryable, one source of truth, fits a later dynamic site.
- Against: credentials in three places; free tiers change or pause;
  overkill for a static site; the text file contract for the agent would
  need an export and import step anyway.

## Recommendation

**B now, C when moving to Netlify.** B removes the actual objection
(a code repo full of bot commits) without adding infrastructure or
credentials, and it keeps the agent's file contract untouched. To make the
later move cheap, `nc.store` gets a `DataRoot` abstraction from the start:
a local directory that is either a git checkout (B) or a synced mirror (C).
Only `nc sync` changes between the two.

If the owner prefers to avoid git as a database from day one, C with
Cloudflare R2 is the choice, and the only extra work is the two sync
commands and the secrets.

## Consequences on the plan if B is chosen

- T03 also creates `newscollection2027-data` and attaches it to the Routine.
- T12 writes to the data root instead of `data/` in the code repo.
- T13 and T42 gain a dispatch step between the two repositories.
- `docs/WORKFLOW.md` loses the "data commits on main" exception for the
  code repo entirely, which simplifies branch protection.
