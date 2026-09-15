---
name: nightly
description: Run the nightly analyze-and-publish procedure for newscollection2027. Use when a Routine or a person asks to run tonight's analysis, process pending clusters and publish.
---

# Nightly run

1. `git pull --ff-only origin main`
2. `uv sync` if `.venv` is missing.
3. `nc pending`. If empty, run `nc runlog --empty`, commit if anything
   changed, and stop.
4. Follow `.claude/skills/analyze-clusters/SKILL.md` for every pending
   cluster.
5. `nc validate --new`. Fix rejects as the analyze skill says.
6. `nc build` as a smoke test. If it fails because of an analysis you wrote,
   fix the analysis; if it fails for any other reason, stop and report.
7. `nc runlog` writes `data/runs/<date>.json`.
8. `git add data && git commit -m "data: analyses <date>" && git push origin main`
   Only `data/**` may be in this commit. If the push is rejected, pull with
   `--ff-only` and push once more; do not force.
9. Report: clusters analyzed, rejects left, and anything you stopped on.
