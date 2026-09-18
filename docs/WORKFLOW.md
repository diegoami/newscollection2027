# Workflow and agent structure

This file defines how the product is built (development workflow) and how
it runs (runtime workflow). Both are deliberately small. The question
"do we need a planner or orchestrator" is answered per phase below.

## 1. Development workflow

### Roles

| Role | Who | Model | Responsibilities |
|---|---|---|---|
| Owner | Diego | | Decides scope, merges PRs, labels clustering pairs, checks golden analyses |
| Orchestrator | One Claude Code session per milestone | Claude Opus 5 (or the session default) | Keeps `docs/PLAN.md` current, turns tasks into GitHub issues, spawns implementers and reviewers, opens PRs, addresses review, reports to the owner |
| Implementer | Subagent spawned by the orchestrator, one per task, in a git worktree | Claude Sonnet 5 for tasks tagged `S`, Claude Opus 5 for tasks tagged `O` | Implements one task on its own branch, runs `make check`, reports the diff and test output |
| Reviewer | Subagent with a fresh context, spawned after the implementer | Claude Opus 5, effort high | Reads the diff against the task's acceptance criteria, runs the tests, reports findings ranked by severity. Never edits code |
| CI | GitHub Actions | | ruff, mypy, pytest on every PR; build and deploy on push to main |

Is a persistent planner or orchestrator service needed? No. State lives
in `docs/PLAN.md` and in GitHub issues, so any new Claude Code session can
pick up as orchestrator by reading those two things. Nothing long-running
has to stay alive between milestones.

Tasks tagged `O` are the ones where design judgment matters: the
analysis contract, the clustering algorithm, the eval design. Everything
else is well specified enough for Sonnet 5.

Parallel fan-out (several implementers at once) is only worth it when
tasks touch disjoint files. `docs/PLAN.md` marks which tasks are
parallel-safe. If the owner says "use a workflow", the orchestrator may run
the fan-out as a Workflow script; otherwise it spawns plain subagents.

### Branching and pull requests

- `main` is protected by a ruleset: PRs only, CI must pass, no force
  pushes, no deletions, required approvals set to zero. Zero, because
  agent sessions open PRs under the owner's GitHub identity and GitHub
  does not let an author approve their own PR. The owner merges after
  reading the PR and the reviewer agent's findings. No agent merges.
- Feature branches: `feat/T<nn>-<slug>` (one task per branch), fixes:
  `fix/<slug>`. Branches are short-lived and deleted after merge.
- One PR per task. The PR body states the task id, what changed, how it
  was verified, and anything left out. The PR template enforces this.
- Review flow for a PR:
  1. Implementer pushes and opens the PR.
  2. Orchestrator runs the reviewer agent and posts its findings as one PR
     comment. Findings are fixed on the same branch or explicitly declined
     with a reason in the thread.
  3. Owner reads the PR, asks for changes or merges (squash).
- Where data lives was decided in T00 (`docs/DECISIONS.md`): a separate
  repository, `newscollection2027-data`. Nothing writes data into this
  repository, so there is no exception to the PR rule for it -- the
  nightly Routine and the ingest workflow push `data:` commits to the
  data repo, and the deploy workflow writes only the generated
  `gh-pages` branch. No automation pushes to `main`.

### Definition of done for a task

- Acceptance criteria in `docs/PLAN.md` met and demonstrated in the PR.
- `make check` green locally and in CI.
- Tests added for new behavior; fixture data committed under `tests/fixtures/`.
- `docs/` updated when a decision changed.

## 2. Runtime workflow

### Every three hours: ingest (GitHub Actions)

```
nc sync pull      clone or fast-forward the data repo into the data root
nc ingest         fetch feeds, append new items to items/
nc cluster        embed new items, link, emit clusters and pending files
nc sync push      commit "data: ingest <timestamp>" and push, only if changed
                  then repository_dispatch data-updated -> deploy workflow
```

Concurrency group `data` so runs never overlap. The embedding model is
cached between runs with `actions/cache`. The workflow authenticates to
the data repo with a fine-grained personal access token scoped to that
one repository, stored as the secret `DATA_REPO_TOKEN`.

### 04:00 Europe/Berlin: analyze and publish (Claude Code Routine)

Routine settings: fresh session per firing, model Claude Sonnet 5,
environment Default, repos `diegoami/newscollection2027` (read) and
`diegoami/newscollection2027-data` (push) attached, push and email
notifications on. Cron is evaluated in UTC: `0 2 * * *`
is 04:00 in summer and 03:00 in winter; the schedule is changed twice a
year or left to drift, owner's choice.

Routine prompt (kept to one line, the detail lives in the repo skill):

> Follow `.claude/skills/nightly/SKILL.md` to run tonight's analysis and publish.

The skill procedure:

1. `nc runlog --start` opens the run journal. Every `nc` command after it
   times itself into tonight's record, so the agent reports no durations
   and cannot get them wrong.
2. `nc sync pull` brings the data repo to the data root.
3. `nc judge`, and the judge skill for any borderline pair, before
   clustering: judging afterwards would hold every link for a day.
4. `nc pending` lists cluster files awaiting analysis.
5. For each pending cluster, the agent reads the cluster file and writes
   `data/analyses/<date>/<cluster_id>.json` following
   `.claude/skills/analyze-clusters/SKILL.md`.
6. `nc validate` checks every new analysis. Rejects are listed with
   reasons; the agent fixes and revalidates each reject at most twice, then
   leaves it in `data/rejected/`.
7. `nc build` as a smoke test that the site still builds.
8. `nc runlog` writes `data/runs/<date>.json` and closes the journal. Its
   counts are recomputed from the files, never taken from the agent's
   account of its own night.
9. `nc sync push` commits `data: analyses <date>` to the data repo and
   pushes; the data repo's push triggers the deploy workflow in the code
   repo through `repository_dispatch`, which builds the site and publishes
   it to GitHub Pages (later Netlify).

Agent structure at runtime: one agent, no subagents in v1. Fifty clusters
at about a thousand tokens each fit comfortably in one context. If nightly
volume grows past a few hundred clusters, the skill switches to spawning
one subagent per batch of twenty clusters so each batch starts with a
clean context; the validator gate makes that change safe.

Is a reviewer agent needed at runtime? Not in v1. The validator is the
reviewer for structure and attribution. A second-pass critic for
high-severity discrepancies is a v2 option (see PLAN M6) and would run as
one extra subagent over the day's high-severity items only.

### On a data or code change: deploy (GitHub Actions)

```
nc sync pull      clone or fast-forward the data repo into the data root
nc build          render site/ from the current, still-valid analyses
                  then publish site/ to gh-pages, only if the bytes differ
```

Three triggers: a push to `main` (the builder or templates changed), the
`data-updated` repository_dispatch (the data changed -- fired by the
ingest workflow and, through it, by the nightly Routine's `nc sync
push`), and manually. Concurrency group `deploy`, separate from `data`,
so a deploy never queues behind an ingest; unlike the ingest it does
cancel in progress, because a superseded build has nothing worth
finishing and the push is a single step at the end.

`nc build` writes byte-stable output, so a rebuild that found nothing
new produces no commit on `gh-pages` and no deploy. The branch is
generated output only: it is rewritten from `site/` every time and can
be thrown away and rebuilt with one `nc build`.

### Failure handling

- Ingest workflow failure: GitHub notifies the owner; the next run catches
  up because feeds overlap.
- Routine failure: the Routine's notifications reach the owner; pending
  files stay pending and are picked up the next night.
- Persistent validator rejects: visible on `/status/` and in
  `data/rejected/`; they feed the golden set and prompt tuning.
- Deploy failure: the published site stays as it was -- the branch is
  only ever updated by a successful run -- so the site goes stale rather
  than broken, and the next data change retries the whole build.

## 3. Model choices, summarized

| Where | Model | Why |
|---|---|---|
| Orchestrator sessions | Claude Opus 5 | Planning and review judgment |
| Implementers, tasks tagged S | Claude Sonnet 5 | Well-specified work, cheaper and fast |
| Implementers, tasks tagged O | Claude Opus 5 | Design decisions |
| Reviewer agents | Claude Opus 5, effort high | Adversarial reading of diffs |
| Nightly Routine | Claude Sonnet 5 | Owner's choice to start; swappable per Routine |
| API backend (`nc analyze --backend api`) | `claude-sonnet-5` | Owner's choice to start; single config value |
| Embeddings | Local open-source sentence-embedding model | Free, deterministic, no API |
