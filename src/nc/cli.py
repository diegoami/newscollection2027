"""Command-line interface for nc.

Subcommands (`pending`, `validate`, `analyze`, `build`, `nightly`) are
added by the tasks in `docs/PLAN.md` that implement them. `feeds check`
is wired here by T10; `ingest`, `db rebuild` and `sync pull|push` by
T12; `embed` by T20; `cluster` by T21; `label` and `tune` by T22;
`judge` and `bench-judge` by T24;
`validate` by T30; `pending` by T32; `analyze` by T31; `eval` by T33.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import feedparser

from nc import (
    __version__,
    analyze,
    bench,
    cluster,
    contract,
    embed,
    evals,
    feeds,
    judge,
    labelling,
    store,
    sync,
)
from nc.store import DataRoot


def _feeds_check(args: argparse.Namespace) -> int:
    outlets = feeds.load_outlets(args.outlets)
    thresholds = feeds.load_thresholds(args.thresholds)
    results = feeds.check_feeds(outlets, thresholds)

    if args.json:
        print(json.dumps([feeds.result_to_dict(r) for r in results], indent=2))
    else:
        print(feeds.format_report(results))

    return 0 if all(result.ok for result in results) else 1


def _data_root(args: argparse.Namespace) -> DataRoot:
    return DataRoot(args.data_root) if args.data_root else DataRoot.from_env()


def _ingest(args: argparse.Namespace) -> int:
    """Fetch every configured feed, normalize entries, append new items.

    Reuses `nc.feeds` for both fetching (the same `feedparser.parse`
    call and user agent as `nc feeds check`) and normalization
    (`normalize_entries`, T11); this command only adds the storage step
    (`nc.store.append_items`, T12).
    """
    outlets = feeds.load_outlets(args.outlets)
    thresholds = feeds.load_thresholds(args.thresholds)
    normalize_config = feeds.load_normalize_config(args.thresholds)
    data_root = _data_root(args)

    fetched = feeds.utc_now_iso()
    items: list[feeds.Item] = []
    for outlet in outlets:
        parsed = feedparser.parse(outlet.feed_url, agent=thresholds.user_agent)
        items.extend(
            feeds.normalize_entries(
                outlet, parsed, fetched, normalize_config.lede_word_cap
            )
        )

    result = store.append_items(data_root, items)
    print(f"ingest: {result.added} new item(s), {result.skipped} already stored")
    return 0


def _embed(args: argparse.Namespace) -> int:
    """Embed every stored item without a vector yet.

    `Model2VecBackend` (the real model) is constructed here, at the CLI
    boundary, and nowhere else -- `nc.embed.embed_items` only knows the
    `EmbeddingBackend` protocol, so this is the one place production
    code chooses which backend runs (see nc/embed.py's module
    docstring).
    """
    data_root = _data_root(args)
    config = embed.load_embed_config(args.config)
    backend = embed.Model2VecBackend(config)
    result = embed.embed_items(
        data_root, backend, config.model_id, args.db, config.batch_size
    )
    print(
        f"embed: {result.embedded} item(s) embedded, "
        f"{result.already_stored} already had a vector"
    )
    return 0


def _cluster(args: argparse.Namespace) -> int:
    """Link the four-day window into clusters and queue the new ones.

    Deliberately does not embed anything: `nc embed` (T20) owns the
    model and is run before this in .github/workflows/ingest.yml, so
    clustering stays pure numpy and never needs the model to be
    loadable. Items ingested since the last `nc embed` therefore have no
    vector yet; they cannot link, and the summary says how many.

    T24's judgments enter here, and only here: `judge.accepted_links`
    returns the pairs an LLM backend said were the same story *and* the
    validator accepted, and they are linked exactly as a `tau_high`
    pair would be. With `tau_high` at 1.00 this is where essentially
    every cross-outlet link now comes from (docs/CLUSTERING.md) --
    `nc cluster` still calls no LLM, it reads files the LLM step wrote.
    """
    data_root = _data_root(args)
    config = cluster.load_cluster_config(args.config)
    links = judge.accepted_links(data_root)
    report = cluster.run_clustering(data_root, config, args.db, extra_links=links)
    print(cluster.format_report(report, config))
    print(f"cluster: {len(links)} link(s) from accepted judgments")
    return 0


def _label(args: argparse.Namespace) -> int:
    """T22: show unlabeled pairs, record yes/no to `labels/pairs.jsonl`.
    Reads `pending-pairs/` (T24's exhaustive judge queue) and
    `label-sample/` (T22's bounded, stratified sample) written by `nc
    cluster` -- no embedding model, see nc/labelling.py's module
    docstring and `labelling_pool`.
    """
    data_root = _data_root(args)
    config = cluster.load_cluster_config(args.config)
    labelling.run_label_session(data_root, config, limit=args.limit)
    return 0


def _bench_embed(args: argparse.Namespace) -> int:
    """Score `labels/pairs.jsonl` with each model in
    config/embed.yaml's `bench_candidates` and report which gets most
    of the range right enough to auto-link.

    Needs network: it downloads and runs each candidate. That is why it
    is a command run on a GitHub Actions runner rather than part of
    `make check` -- the development sandbox has no route to
    huggingface.co (nc/embed.py's module docstring).
    """
    data_root = _data_root(args)
    config = embed.load_embed_config(args.config)
    candidates = bench.load_bench_candidates(args.config)
    results = []
    for model_id in candidates:
        backend = embed.Model2VecBackend(replace(config, model_id=model_id))
        results.append(bench.bench_model(data_root, model_id, backend))
    print(bench.format_bench_report(results, config.model_id))
    return 0


def _judge(args: argparse.Namespace) -> int:
    """T24: the borderline-pair judge queue and its validator.

    Bare, this prints the pairs still awaiting a yes-or-no, highest
    score first, in the form a backend answers -- the same role `nc
    pending` plays for the analysis step. With `--validate` it reports
    what is on disk instead: how many judgments, how many would link,
    and every one the validator refuses and why. It never writes a
    judgment itself; that is the backend's job, and this command is the
    gate (CLAUDE.md: "Never bypass the validator").
    """
    data_root = _data_root(args)
    if args.validate:
        report = judge.validate_all(data_root)
        print(judge.format_judge_report(report))
        return 1 if report.rejected else 0

    config = judge.load_judge_config(args.config)
    pending = judge.unjudged_pairs(data_root)
    limit = config.max_pairs_per_run if args.limit is None else args.limit
    shown = pending[:limit]
    print(
        f"judge: {len(pending)} pair(s) unjudged, showing {len(shown)} (limit {limit})"
    )
    for pair in shown:
        print()
        print(judge.render_pair_question(pair), end="")
    return 0


def _analyze(args: argparse.Namespace) -> int:
    """T31: fill pending analyses through the Anthropic SDK.

    Not the production path -- docs/ARCHITECTURE.md's cost model puts
    the nightly analysis on the Claude Code subscription, where T32's
    skill runs it for nothing metered. This is for backfills, local
    development and T33's eval. **It spends money**, which is why there
    is no default backend: `--backend api` has to be asked for.

    Writes analyses and nothing else. Marking a cluster analyzed is
    `nc validate`'s job, so this command never decides a story has been
    analysed (CLAUDE.md).
    """
    data_root = _data_root(args)
    config = analyze.load_analyze_config(args.config)

    if args.batch:
        import anthropic

        client = anthropic.Anthropic()
        if args.collect:
            report = analyze.collect_batch(data_root, config, client, args.collect)
            print(analyze.format_analyze_report(report))
            return 1 if report.failed else 0
        submission = analyze.submit_batch(data_root, config, client, args.limit)
        print(
            f"analyze: submitted {len(submission.cluster_ids)} cluster(s) as "
            f"batch {submission.batch_id}"
        )
        print(
            "analyze: results are not immediate -- re-run with "
            f"--batch --collect {submission.batch_id} once it has ended"
        )
        return 0

    backend = analyze.AnthropicBackend(config)
    report = analyze.run_analyze(data_root, backend, config, args.limit)
    print(analyze.format_analyze_report(report))
    return 1 if report.failed else 0


def _eval(args: argparse.Namespace) -> int:
    """T33: score a backend against the golden set.

    `--backend files` reads analyses already on disk and costs nothing,
    which is how T32's skill output gets measured. `--backend api` runs
    T31's backend over the golden clusters first, and **spends money**.
    The scoring is identical either way: whichever backend produced the
    analyses is a label on the report, never a branch in the scoring.
    """
    data_root = _data_root(args)
    cases = evals.load_golden(args.golden)
    if not cases:
        print(f"eval: no golden cases in {args.golden}")
        return 1

    if args.materialize:
        target = DataRoot(args.materialize)
        count = evals.materialize(cases, target)
        print(f"eval: wrote {count} golden cluster(s) into {target.path} as pending")
        print("eval: run a backend over them, then score with --backend files")
        return 0

    config = analyze.load_analyze_config(args.config)
    if args.backend == "api":
        backend = analyze.AnthropicBackend(config)
        prompt = analyze.load_prompt()
        schema = analyze.response_schema()
        produced: dict[str, contract.Analysis | None] = {}
        for case in cases:
            analysis, _, _ = analyze.analyze_cluster(
                case.cluster, backend, config, prompt, schema
            )
            produced[case.id] = analysis
        model = config.model
    else:
        produced = evals.load_analyses_from_files(data_root, cases)
        model = "n/a (scored from files on disk)"

    report = evals.run_eval(cases, produced, args.backend, model)
    print(evals.format_eval_report(report))
    path = evals.write_report(report, args.reports)
    print(f"eval: written to {path}")
    return 0


def _pending(args: argparse.Namespace) -> int:
    """T32: the clusters awaiting an analysis, oldest first.

    The queue an agent works through, and the input half of the file
    contract: each line names the file in `pending/` that holds the
    cluster's id, version and items. `nc validate` is what removes one
    from this list, by marking the cluster analyzed.
    """
    data_root = _data_root(args)
    queue = cluster.pending_clusters(data_root)
    print(f"pending: {len(queue)} cluster(s) awaiting analysis")
    for entry in queue[: args.limit] if args.limit else queue:
        outlets = ", ".join(sorted(entry.outlets))
        print(f"  {entry.id}  v{entry.version}  {len(entry.items)} item(s)  {outlets}")
        if args.verbose:
            for member in entry.items:
                print(f"      [{member.outlet}] {member.title}")
    return 0


def _validate(args: argparse.Namespace) -> int:
    """T30: check every analysis against the contract, move the failures
    to `rejected/`.

    Exits non-zero when anything was rejected, so the nightly Routine
    and CI can tell a clean run from one that needs the agent to go
    back. A rejected analysis is moved out of `analyses/` rather than
    left there: leaving it would let `nc build` read something the
    validator refused, which is the bypass CLAUDE.md forbids.
    """
    data_root = _data_root(args)
    report = contract.run_validate(
        data_root, only_new=args.new, schema_path=args.schema, state_path=args.state
    )
    print(contract.format_validate_report(report))
    return 1 if report.rejected else 0


def _bench_judge(args: argparse.Namespace) -> int:
    """T24: score the judgments on disk against T23's human labels.

    No model and no network: it joins `judgments/` to
    `labels/pairs.jsonl` on pair id and counts agreement, so it runs in
    `make check` and in this sandbox, unlike `nc bench-embed`.
    """
    data_root = _data_root(args)
    print(judge.format_judge_eval(judge.run_bench_judge(data_root)))
    return 0


def _tune(args: argparse.Namespace) -> int:
    """T22: precision/recall per candidate threshold, from
    `labels/pairs.jsonl`."""
    data_root = _data_root(args)
    config = cluster.load_cluster_config(args.config)
    report = labelling.run_tune(data_root)
    print(labelling.format_tune_report(report, config))
    return 0


def _db_rebuild(args: argparse.Namespace) -> int:
    data_root = _data_root(args)
    count = store.rebuild_db(data_root, args.db)
    print(f"db rebuild: {count} item(s) loaded into {args.db}")
    return 0


def _sync_pull(args: argparse.Namespace) -> int:
    data_root = _data_root(args)
    config = sync.load_sync_config(args.config)
    sync.pull(data_root, config)
    print(f"sync pull: {data_root.path} up to date with origin/{config.branch}")
    return 0


def _sync_push(args: argparse.Namespace) -> int:
    data_root = _data_root(args)
    config = sync.load_sync_config(args.config)
    message = args.message or f"data: sync {feeds.utc_now_iso()}"
    pushed = sync.push(data_root, config, message)
    # .github/workflows/ingest.yml compares this line for equality to
    # decide whether to fire the data-updated dispatch. Reword it and
    # the workflow stops firing silently; tests/test_cli.py pins it.
    print("sync push: pushed" if pushed else "sync push: nothing changed")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nc",
        description=(
            "newscollection2027: cluster tech-news feed items across "
            "outlets and check their claims against each other."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and exit",
    )

    subparsers = parser.add_subparsers(dest="command")

    feeds_parser = subparsers.add_parser("feeds", help="outlet feed registry")
    feeds_subparsers = feeds_parser.add_subparsers(dest="feeds_command")

    check_parser = feeds_subparsers.add_parser(
        "check",
        help="fetch every configured feed and report item counts, newest "
        "date and whether ledes are present",
    )
    check_parser.add_argument(
        "--outlets",
        type=Path,
        default=feeds.DEFAULT_OUTLETS_PATH,
        help=f"outlet registry (default: {feeds.DEFAULT_OUTLETS_PATH})",
    )
    check_parser.add_argument(
        "--thresholds",
        type=Path,
        default=feeds.DEFAULT_THRESHOLDS_PATH,
        help=f"feed thresholds (default: {feeds.DEFAULT_THRESHOLDS_PATH})",
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of the text report",
    )
    check_parser.set_defaults(func=_feeds_check)

    data_root_help = (
        f"data root (default: ${store.ENV_VAR}, else {store.DEFAULT_DATA_ROOT})"
    )

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="fetch every configured feed and append new items to the data root",
    )
    ingest_parser.add_argument(
        "--outlets", type=Path, default=feeds.DEFAULT_OUTLETS_PATH
    )
    ingest_parser.add_argument(
        "--thresholds", type=Path, default=feeds.DEFAULT_THRESHOLDS_PATH
    )
    ingest_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    ingest_parser.set_defaults(func=_ingest)

    embed_parser = subparsers.add_parser(
        "embed",
        help="embed every stored item without a vector yet (T20)",
    )
    embed_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    embed_parser.add_argument(
        "--config",
        type=Path,
        default=embed.DEFAULT_EMBED_CONFIG_PATH,
        help=f"embedding config (default: {embed.DEFAULT_EMBED_CONFIG_PATH})",
    )
    embed_parser.add_argument(
        "--db",
        type=Path,
        default=embed.DEFAULT_VECTORS_DB_PATH,
        help=f"vectors SQLite file (default: {embed.DEFAULT_VECTORS_DB_PATH})",
    )
    embed_parser.set_defaults(func=_embed)

    cluster_parser = subparsers.add_parser(
        "cluster",
        help="link the four-day window into clusters and queue new ones (T21)",
    )
    cluster_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    cluster_parser.add_argument(
        "--config",
        type=Path,
        default=cluster.DEFAULT_CLUSTER_CONFIG_PATH,
        help=f"clustering config (default: {cluster.DEFAULT_CLUSTER_CONFIG_PATH})",
    )
    cluster_parser.add_argument(
        "--db",
        type=Path,
        default=embed.DEFAULT_VECTORS_DB_PATH,
        help=f"vectors SQLite file (default: {embed.DEFAULT_VECTORS_DB_PATH})",
    )
    cluster_parser.set_defaults(func=_cluster)

    label_parser = subparsers.add_parser(
        "label",
        help="show unlabeled near-threshold pairs, record yes/no (T22)",
    )
    label_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    label_parser.add_argument(
        "--config",
        type=Path,
        default=cluster.DEFAULT_CLUSTER_CONFIG_PATH,
        help=f"clustering config (default: {cluster.DEFAULT_CLUSTER_CONFIG_PATH})",
    )
    label_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="stop after this many pairs (default: the whole unlabeled pool)",
    )
    label_parser.set_defaults(func=_label)

    tune_parser = subparsers.add_parser(
        "tune",
        help="print precision and recall per threshold from the labels (T22)",
    )
    tune_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    tune_parser.add_argument(
        "--config",
        type=Path,
        default=cluster.DEFAULT_CLUSTER_CONFIG_PATH,
        help=f"clustering config (default: {cluster.DEFAULT_CLUSTER_CONFIG_PATH})",
    )
    tune_parser.set_defaults(func=_tune)

    judge_parser = subparsers.add_parser(
        "judge",
        help="show borderline pairs awaiting a yes/no, or validate answers (T24)",
    )
    judge_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    judge_parser.add_argument(
        "--config",
        type=Path,
        default=judge.DEFAULT_JUDGE_CONFIG_PATH,
        help=f"judge config (default: {judge.DEFAULT_JUDGE_CONFIG_PATH})",
    )
    judge_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="how many pairs to show (default: the config's max_pairs_per_run)",
    )
    judge_parser.add_argument(
        "--validate",
        action="store_true",
        help="report what is on disk and what the validator refuses, and exit "
        "non-zero if anything is refused",
    )
    judge_parser.set_defaults(func=_judge)

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="fill pending analyses through the Anthropic SDK -- spends money (T31)",
    )
    analyze_parser.add_argument(
        "--backend",
        choices=["api"],
        required=True,
        help="which backend to run. Required and with one choice on purpose: "
        "the other backend is a Claude Code skill, not a command, and this "
        "one bills, so it is never the default",
    )
    analyze_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    analyze_parser.add_argument(
        "--config",
        type=Path,
        default=analyze.DEFAULT_ANALYZE_CONFIG_PATH,
        help=f"analysis config (default: {analyze.DEFAULT_ANALYZE_CONFIG_PATH})",
    )
    analyze_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="how many clusters to attempt (default: the config's "
        "max_clusters_per_run)",
    )
    analyze_parser.add_argument(
        "--batch",
        action="store_true",
        help="use the Message Batches API: half price, asynchronous, up to 24 "
        "hours. For backfills and evals, never for a nightly run",
    )
    analyze_parser.add_argument(
        "--collect",
        metavar="BATCH_ID",
        default=None,
        help="with --batch: write the results of an ended batch",
    )
    analyze_parser.set_defaults(func=_analyze)

    eval_parser = subparsers.add_parser(
        "eval",
        help="score a backend against the golden set (T33)",
    )
    eval_parser.add_argument(
        "--backend",
        choices=["api", "files"],
        required=True,
        help="`files` scores analyses already on disk and costs nothing; "
        "`api` runs T31's backend over the golden clusters first and bills",
    )
    eval_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    eval_parser.add_argument(
        "--golden",
        type=Path,
        default=evals.DEFAULT_GOLDEN_DIR,
        help=f"golden cases (default: {evals.DEFAULT_GOLDEN_DIR})",
    )
    eval_parser.add_argument(
        "--reports",
        type=Path,
        default=evals.DEFAULT_REPORTS_DIR,
        help=f"where the report is written (default: {evals.DEFAULT_REPORTS_DIR})",
    )
    eval_parser.add_argument(
        "--config",
        type=Path,
        default=analyze.DEFAULT_ANALYZE_CONFIG_PATH,
        help=f"analysis config (default: {analyze.DEFAULT_ANALYZE_CONFIG_PATH})",
    )
    eval_parser.add_argument(
        "--materialize",
        type=Path,
        default=None,
        help="write the golden clusters into this data root as pending "
        "clusters and stop, so a backend can work them like a real queue. "
        "Use a scratch data root: this writes cluster and pending files",
    )
    eval_parser.set_defaults(func=_eval)

    pending_parser = subparsers.add_parser(
        "pending",
        help="list clusters awaiting analysis (T32)",
    )
    pending_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    pending_parser.add_argument(
        "--limit", type=int, default=None, help="show at most this many"
    )
    pending_parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print each cluster's outlets and headlines",
    )
    pending_parser.set_defaults(func=_pending)

    validate_parser = subparsers.add_parser(
        "validate",
        help="check analyses against contract/analysis.schema.json (T30)",
    )
    validate_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    validate_parser.add_argument(
        "--new",
        action="store_true",
        help="only analyses modified since the last run (a speed option for a "
        "long session, not a correctness claim -- see nc.contract)",
    )
    validate_parser.add_argument(
        "--schema",
        type=Path,
        default=contract.DEFAULT_SCHEMA_PATH,
        help=f"analysis schema (default: {contract.DEFAULT_SCHEMA_PATH})",
    )
    validate_parser.add_argument(
        "--state",
        type=Path,
        default=contract.DEFAULT_VALIDATE_STATE_PATH,
        help=f"where --new remembers the last run "
        f"(default: {contract.DEFAULT_VALIDATE_STATE_PATH})",
    )
    validate_parser.set_defaults(func=_validate)

    bench_judge_parser = subparsers.add_parser(
        "bench-judge",
        help="score the judgments on disk against the human labels (T24)",
    )
    bench_judge_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    bench_judge_parser.set_defaults(func=_bench_judge)

    bench_parser = subparsers.add_parser(
        "bench-embed",
        help="score the labelled pairs with each candidate embedding model",
    )
    bench_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    bench_parser.add_argument(
        "--config",
        type=Path,
        default=embed.DEFAULT_EMBED_CONFIG_PATH,
        help=f"embedding config (default: {embed.DEFAULT_EMBED_CONFIG_PATH})",
    )
    bench_parser.set_defaults(func=_bench_embed)

    db_parser = subparsers.add_parser(
        "db", help="the SQLite cache built from the data root"
    )
    db_subparsers = db_parser.add_subparsers(dest="db_command")

    db_rebuild_parser = db_subparsers.add_parser(
        "rebuild", help="rebuild .cache/nc.sqlite from the JSONL files"
    )
    db_rebuild_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    db_rebuild_parser.add_argument(
        "--db",
        type=Path,
        default=store.DEFAULT_DB_PATH,
        help=f"SQLite file to write (default: {store.DEFAULT_DB_PATH})",
    )
    db_rebuild_parser.set_defaults(func=_db_rebuild)

    sync_parser = subparsers.add_parser(
        "sync", help="clone/pull or commit/push the data repo"
    )
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command")

    sync_pull_parser = sync_subparsers.add_parser(
        "pull", help="clone the data repo into the data root, or fast-forward it"
    )
    sync_pull_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    sync_pull_parser.add_argument(
        "--config", type=Path, default=sync.DEFAULT_SYNC_CONFIG_PATH
    )
    sync_pull_parser.set_defaults(func=_sync_pull)

    sync_push_parser = sync_subparsers.add_parser(
        "push", help="commit and push the data root, only if something changed"
    )
    sync_push_parser.add_argument(
        "--data-root", type=Path, default=None, help=data_root_help
    )
    sync_push_parser.add_argument(
        "--config", type=Path, default=sync.DEFAULT_SYNC_CONFIG_PATH
    )
    sync_push_parser.add_argument(
        "--message",
        default=None,
        help="commit message (default: data: sync <timestamp>)",
    )
    sync_push_parser.set_defaults(func=_sync_push)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    func = getattr(args, "func", None)
    if func is not None:
        return_code: int = func(args)
        return return_code

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
