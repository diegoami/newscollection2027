# Decisions

Answered questions, newest first. Open questions live in the plan thread
until answered here.

Open: feed terms of use, see T09. The recommended outlets were scored
without reading a single robots.txt or terms page, so whether these feeds
may be aggregated with attribution is unanswered. It also decides whether
NYT Technology, which measured well, joins the set.

- 2026-09-17 Thresholds set by the owner from 174 labelled
  cross-outlet pairs. T23 stays open: its criterion is 200 pairs, the
  corpus is still growing, and the judge-queue size that prices
  `tau_low` is unmeasured. The values are: `tau_high: 1.00`, `tau_low: 0.57`. Auto-linking is
  off. The labels showed cosine cannot separate the two populations --
  a different-story pair at 0.7992 sits above true matches running down
  to 0.5708 -- so the highest threshold with no false link reached only
  4 of 35 true pairs. That bought about ten links a run in exchange for
  owning the pipeline's one unrecoverable failure mode, a story page
  quoting "discrepancies" between outlets that never covered the same
  event. `nc bench-embed` then measured six embedding models across a
  64x parameter range and separability did not move, while every model
  scored 0.89-0.95 AUC. The conclusion is that cosine is a good ranker
  and a bad classifier, so it stays as the filter that keeps T24's
  judge from seeing all ~49,000 pairs in a window, and the judge
  decides the band. `config/embed.yaml`'s `model_id` stays: it has the
  best AUC of the six and every alternative costs a full re-embed for a
  three-pair difference inside the noise.
  Unmeasured and deliberately left so: how many pairs a run lands in
  [0.57, 0.65), since `pending-pairs/` was only ever exhaustive above
  0.65. That is T24's bill and the next `nc cluster` run prints it.
- 2026-09-16 Promotional items and same-outlet pairs, raised by the
  owner during the first labelling session. Coupon pages, deals posts
  and conference marketing are dropped from the clustering window by
  `config/promo.yaml`; they are not stories, they are near duplicates
  of each other, and they had already corrupted 3 of the first 10
  positive labels. Same-outlet pairs stay out of the labelling sample
  but remain in `pending-pairs/`: a cluster needs two distinct outlets,
  so they are not the judgment a human should spend an hour on, but
  they can still bridge a component. Filtering happens at cluster time,
  never at ingest: the item store is the record and these rules will be
  retuned. Consequence for T23: the pool is 121 pairs, not 223, so the
  "at least 200" acceptance criterion needs the owner's call — relax it,
  or wait for the corpus to refill with cross-outlet pairs.
- 2026-09-16 Clustering, ratified on #38. Cluster ids derive from an
  anchor item fixed when the cluster is born, not from a hash of the
  membership: the documented model asked for both and they contradict
  each other, because a late-arriving outlet rewrites a membership hash
  and orphans the analysis written against the old id. `superseded` is a
  fourth `status` value, alongside a `superseded_by` field naming the
  survivor. An analysis is current when its cluster is not superseded
  and its version still matches; that rule now lives in
  `docs/ARCHITECTURE.md` because T30 and T40 both implement it.
  Retuning the thresholds at T23 does not retroactively re-cluster:
  recomputing from scratch reassigns every id and orphans every
  analysis, so it stays a deliberate act rather than a side effect.
  `load_clusters` reading every cluster file each run is accepted; old
  articles get deleted or archived before it matters.
- 2026-09-16 Outlets: the eleven in `config/outlets.yaml`, chosen in T09
  from a live measurement of 42 candidates. Thresholds `tau_low` and
  `tau_high` in `config/cluster.yaml` are provisional guesses until T23
  sets them from labelled pairs.
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
