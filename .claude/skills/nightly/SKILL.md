---
name: nightly
description: Run the nightly analyze-and-publish procedure for newscollection2027. Use when a Routine or a person asks to run tonight's analysis, process pending clusters and publish.
---

# Nightly run

1. `uv sync` if `.venv` is missing.
2. `nc sync pull` brings the data repository to the data root
   (`NC_DATA_ROOT`). Never write pipeline data into this code repository.
3. `nc pending`. If empty, run `nc runlog --empty`, commit if anything
   changed, and stop.
4. Follow `.claude/skills/analyze-clusters/SKILL.md` for every pending
   cluster.
5. `nc validate --new`. Fix rejects as the analyze skill says.
6. `nc build` as a smoke test. If it fails because of an analysis you wrote,
   fix the analysis; if it fails for any other reason, stop and report.
7. `nc runlog` writes `<data root>/runs/<date>.json`.
8. `nc sync push` commits `data: analyses <date>` in the data repository
   and pushes. If the push is rejected, run `nc sync pull` and push once
   more; never force. This code repository stays untouched.
9. Report: clusters analyzed, rejects left, and anything you stopped on.
