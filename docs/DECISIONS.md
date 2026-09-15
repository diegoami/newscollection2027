# Decisions

Answered questions, newest first. Open questions live in the plan thread
until answered here.

Open: data storage backend, see `docs/STORAGE.md` (T00). Outlet list,
see T09.

- 2026-09-15 Tooling: uv, ruff, mypy strict, pytest.
- 2026-09-15 Repository `newscollection2027`, public (confirmed), GitHub Pages while
  prototyping, Netlify afterwards.
- 2026-09-15 Timezone Europe/Berlin confirmed. Nightly Routine at 04:00
  local (cron `0 2 * * *` UTC in summer, left to drift to 03:00 in winter) on Claude Sonnet 5, fresh session per firing.
- 2026-09-15 Python, fresh codebase, feeds only, cross-outlet consistency
  as the fact-checking scope.
- 2026-09-15 Deterministic steps run in GitHub Actions every three hours;
  only the agentic analysis step uses the Claude Code Routine.
