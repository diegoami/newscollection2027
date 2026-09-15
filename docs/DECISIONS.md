# Decisions

Answered questions, newest first. Open questions live in the plan thread
until answered here.

Open: outlet list, see T09.

- 2026-09-15 Storage: option B from `docs/STORAGE.md`. Pipeline data lives
  in a separate public repository `newscollection2027-data`, holding the
  `data/**` tree, written by the ingest workflow and the nightly Routine.
  Move to object storage (option C, Cloudflare R2) when the site moves to
  Netlify. The code sees only a data root directory; `nc sync` is the
  only part that changes.
- 2026-09-15 Tooling: uv, ruff, mypy strict, pytest.
- 2026-09-15 Repository `newscollection2027`, public (confirmed), GitHub Pages while
  prototyping, Netlify afterwards.
- 2026-09-15 Timezone Europe/Berlin confirmed. Nightly Routine at 04:00
  local (cron `0 2 * * *` UTC in summer, left to drift to 03:00 in winter) on Claude Sonnet 5, fresh session per firing.
- 2026-09-15 Python, fresh codebase, feeds only, cross-outlet consistency
  as the fact-checking scope.
- 2026-09-15 Deterministic steps run in GitHub Actions every three hours;
  only the agentic analysis step uses the Claude Code Routine.
