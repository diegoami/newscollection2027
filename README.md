# newscollection2027

Groups tech-news feed items from several outlets into stories and checks
what each outlet claims against the others. Successor to
[techcontroversy.com](https://github.com/diegoami/newscollection), rebuilt
for feeds only, near-zero cost, and an analysis step that runs as a
scheduled Claude Code Routine.

Start with `docs/ARCHITECTURE.md`, then `docs/WORKFLOW.md` and
`docs/PLAN.md`.

## Development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```
make install   # uv sync: creates the venv, installs dev deps
make check     # ruff + mypy + pytest
```
