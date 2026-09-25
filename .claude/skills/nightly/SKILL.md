---
name: nightly
description: Run the nightly analyze-and-publish procedure for newscollection2027. Use when a Routine or a person asks to run tonight's analysis, process pending clusters and publish.
---

# Nightly run

You are the analysis step of a pipeline that otherwise runs itself. The
three-hourly ingest workflow has already fetched, embedded and clustered;
what is left needs judgement, which is why it is you and not a cron job.

Everything you write goes through `nc validate`. CLAUDE.md: never bypass
the validator, and never edit a file in `clusters/` or `items/` — they are
the pipeline's, not yours.

## Procedure

1. `uv sync` if `.venv` is missing.
2. `nc runlog --start`. Do this first. It opens the run journal, and every
   `nc` command you run after it times itself into tonight's record — you
   do not have to time or report anything.
3. `nc sync pull` brings the data repository to the data root
   (`NC_DATA_ROOT`). Never write pipeline data into this code repository.
4. `nc judge`. If it lists any pair, follow
   `.claude/skills/judge-pairs/SKILL.md`, then `nc judge --validate` and
   `nc cluster`, so tonight's accepted links are in the clusters before
   anything is analysed. Judging after clustering would hold every link
   for a day.
5. `nc pending`. If it is empty, skip to step 9.
6. Follow `.claude/skills/analyze-clusters/SKILL.md` for every pending
   cluster.
7. `nc validate --new`. Fix rejects as the analyze skill says: at most two
   attempts each, then leave them in `rejected/` and say so in step 10.
8. `nc build` as a smoke test. If it fails because of an analysis you
   wrote, fix the analysis. If it fails for any other reason, go to step 9
   with `--failed` and stop.
9. `nc runlog` writes `<data root>/runs/<date>.json` and closes the
   journal. It recomputes every count from the files, so you do not report
   them; add `--note "..."` for anything a number cannot say. If you are
   stopping early, use `nc runlog --failed "<what stopped you>"` instead —
   that is the one thing the files cannot show, because a night that
   stopped early and a night with nothing to do leave the same data root
   behind.
10. `nc sync push` commits `data: analyses <date>` in the data repository
    and pushes. If the push is rejected, run `nc sync pull` and push once
    more; never force. This code repository stays untouched.

    That push goes to `main` of the data repository, and nowhere else.
    The Routine's own prompt says so too. Session instructions to develop
    on a feature branch are about the code repository, which this run
    never changes. They are not a reason to push the data anywhere but
    `main`. On 2026-09-24 a night followed them and pushed to its session
    branch; on 2026-09-25 the next night did not. The same instructions
    gave two different outcomes on consecutive nights, and only one of
    them reached the site.

    If the push is refused because this session may not push to `main`
    at all — a session branch policy, a permissions error, anything
    other than the non-fast-forward case above — the night has failed,
    whatever `runs/<date>.json` says. Do not look for another way onto
    `main`, and do not treat a push anywhere else as done. The one thing
    you may do is park the commit on this session's own branch, so the
    work does not vanish with the sandbox. Then step 11's report starts
    with this line, before anything else:

    `NOT PUBLISHED: push to main refused (<the error>). Work parked on <branch> in newscollection2027-data.`

    On 2026-09-24 a night went exactly this way, and the note that said
    so sat on a branch nobody reads. Recovering it is the owner's call.
    That time, the branch's judgments and run log were taken onto
    `main`. Its clusters and analyses were dropped: they were built on
    a cluster state `main` no longer had, and `nc cluster` and the next
    nightly rebuilt them.
11. Report: clusters analysed, rejects left, and anything you stopped on.

## If something looks wrong before you start

`nc nightly --dry-run` runs every step above except the two that are
yours (judging and analysing) and the push, and reports which one fails.
Use it when a night has gone wrong in a way that does not look like an
analysis problem — a config that will not load, a data root that will not
pull, a build that breaks on its own. It is a diagnostic, not a rehearsal
you owe anyone: on a healthy night, go straight to step 1.

Note that "dry" means nothing is published, not that nothing is written:
it really does pull, cluster, validate and build. What it withholds is the
push and the run record.

There is no `nc nightly` without `--dry-run`. Steps 4 and 6 are you
reading a skill, and CLAUDE.md is explicit that deterministic code never
calls an LLM, so no command can run the whole night.
