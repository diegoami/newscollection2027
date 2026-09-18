# newscollection2027

Static tech-news site that clusters feed items across outlets into stories
and checks the outlets' claims against each other. Python 3.12, `uv`,
`src/nc/` package, CLI entry point `nc`.

Read first: `docs/ARCHITECTURE.md` (what and why), `docs/WORKFLOW.md`
(how we build and run), `docs/PLAN.md` (tasks and acceptance criteria).

## Rules

- Deterministic code (ingest, cluster, validate, build) never calls an LLM.
- The LLM step is a file contract: `pending/*.json` in the data root in,
  `analyses/<date>/*.json` out, `nc validate` decides. Never bypass
  the validator.
- Every claim and discrepancy on the site carries outlet, item id and a
  verbatim quote from that item's title or lede. No exceptions.
- Pipeline data is text only (JSONL and JSON) and lives in the data root,
  a checkout of `newscollection2027-data` at `NC_DATA_ROOT`. It is never
  committed to this repository. SQLite lives in `.cache/`.
- `main` is PR-only, no exceptions. Agents never merge. Automation pushes
  only to the data repository.
- Run `make check` before reporting any task as done.
- Model ids and thresholds live in `config/`, never inline in code.

## Commands

```
make check          ruff + mypy + pytest
nc sync pull|push   fast-forward or commit and push the data repo
nc ingest           fetch feeds into the data root
nc cluster          embed, link, emit clusters and pending files
nc pending          list clusters awaiting analysis
nc judge            list borderline pairs awaiting a yes/no
nc judge --validate check the judgments on disk before they can link
nc bench-judge      score those judgments against the human labels
nc validate --new   validate analyses written since the last run
nc analyze --backend api    fill pending clusters through the SDK backend
nc build            write the static site to site/
nc runlog --start   open the run journal; every nc command after it is timed
nc runlog           write runs/<date>.json from the files, close the journal
nc nightly --dry-run        every nightly step except the agent step and the push
```

## Skills

- `.claude/skills/analyze-clusters/SKILL.md`: how an agent turns one
  pending cluster into a valid analysis file.
- `.claude/skills/judge-pairs/SKILL.md`: how an agent answers one
  borderline pair, same story or not. Prompt in `prompts/judge.md`.
- `.claude/skills/nightly/SKILL.md`: the nightly Routine procedure.
