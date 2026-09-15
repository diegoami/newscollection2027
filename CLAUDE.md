# newscollection2027

Static tech-news site that clusters feed items across outlets into stories
and checks the outlets' claims against each other. Python 3.12, `uv`,
`src/nc/` package, CLI entry point `nc`.

Read first: `docs/ARCHITECTURE.md` (what and why), `docs/WORKFLOW.md`
(how we build and run), `docs/PLAN.md` (tasks and acceptance criteria).

## Rules

- Deterministic code (ingest, cluster, validate, build) never calls an LLM.
- The LLM step is a file contract: `data/pending/*.json` in,
  `data/analyses/<date>/*.json` out, `nc validate` decides. Never bypass
  the validator.
- Every claim and discrepancy on the site carries outlet, item id and a
  verbatim quote from that item's title or lede. No exceptions.
- `data/**` is text only (JSONL and JSON). SQLite lives in `.cache/` and is
  never committed.
- `main` is PR-only except `data:` commits from automation touching
  `data/**`. Agents never merge.
- Run `make check` before reporting any task as done.
- Model ids and thresholds live in `config/`, never inline in code.

## Commands

```
make check          ruff + mypy + pytest
nc ingest           fetch feeds into data/items/
nc cluster          embed, link, emit clusters and pending files
nc pending          list clusters awaiting analysis
nc validate --new   validate analyses written since the last run
nc analyze --backend api    fill pending clusters through the SDK backend
nc build            write the static site to site/
nc nightly --dry-run        every nightly step except the agent step and the push
```

## Skills

- `.claude/skills/analyze-clusters/SKILL.md`: how an agent turns one
  pending cluster into a valid analysis file.
- `.claude/skills/nightly/SKILL.md`: the nightly Routine procedure.
