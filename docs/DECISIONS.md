# Decisions

Answered questions, newest first. Open questions live in the plan thread
until answered here.

Open: feed terms of use, see T09. The recommended outlets were scored
without reading a single robots.txt or terms page, so whether these feeds
may be aggregated with attribution is unanswered. It also decides whether
NYT Technology, which measured well, joins the set.

- 2026-09-17 T23 closed by the owner at 198 clean labels, not 200.
  The count was always a proxy for "enough evidence to set two
  thresholds", and the evidence settled before the count did.
  `tau_low: 0.57` has held across three independent rounds: the lowest
  labelled true pair is 0.5708 and has not moved, and the last session
  added 16 positives, none below it. `tau_high: 1.00` stands on the
  six-model benchmark rather than on any single pair -- which matters,
  because the pair it was originally argued from (a false match at
  0.7992) turned out to be buying advice and was retired. On the clean
  corpus the ceiling is 0.7710 and has now stopped climbing, but at 8
  of 49 true pairs it still buys a fraction of the work in exchange for
  the one unrecoverable failure mode, and the judge answers the band at
  0.881 agreement.
  The number T23 explicitly left open was measured before closing:
  widening `tau_low` from 0.65 to 0.57 is 66% of the judge's queue (168
  of 253 pairs) and buys back 27% of the labelled true pairs (13 of
  49). A good trade only because the judge runs on the Claude Code
  subscription; the first thing to revisit if it ever runs metered.
  Still unmeasured and now blocked on T13 rather than on labelling:
  what a single night costs. The 253-pair queue is a catch-up count
  over every borderline pair since clustering began -- `pending-pairs/`
  has never been pruned -- not a rate.
  Also settled in this round: the labelling pool excludes same-outlet
  pairs, coupon posts and buyer's guides, which raised the positive
  rate of a session from 1 in 5 to better than 1 in 2. Same-outlet
  pairs stay in the judge's queue, where measurement showed the
  bridging case they were kept for occurs 3 times in 92 and would be
  wrong all three times -- recorded as an open question rather than
  acted on.
- 2026-09-17 The judge is a file contract, and a `no` is inert. T24
  writes `judgments/<pair_id>.json` and validates it before anything
  links, exactly as the analysis step does -- the alternative, a backend
  that calls `cluster_items` directly, would put an LLM inside the
  deterministic path CLAUDE.md forbids and leave no record of why a
  link exists. A judgment can only *add* a link: `same_story: false` is
  recorded but changes nothing, because nothing links without a yes in
  the first place. It is kept only as `nc bench-judge`'s evidence and
  so a declined pair is not re-asked every night forever. Judgments are
  written once and never rewritten, for the same idempotence reason
  `nc cluster` carries: a cron fires at the data repository eight times
  a day and a rewritten file is a commit with no new information.
  The production backend is `claude_code`, in the nightly Routine, on
  the subscription -- docs/ARCHITECTURE.md's cost model puts the
  nightly LLM step there, so the judge adds no metered spend. The `api`
  backend is named in `config/judge.yaml` but not built; it waits on
  T31, which brings the SDK in for the analysis step.
  `nc bench-judge` spends T23's 174 labels as the judge's test set
  rather than as tuning data. It reports precision first: a missed pair
  costs one link and tomorrow's run may catch it, while a wrong link
  merges two unrelated stories and makes every claim and discrepancy
  built on top of it wrong -- so both the prompt and the skill say
  answer no when unsure. Those 174 pairs may never become few-shot
  examples in `prompts/judge.md`; a judge shown its own answer key
  measures nothing, and a test pins that.
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
- 2026-09-17 A problem and its fix are two stories (judge rule). **Repealed 2026-09-18**: they are one. Telling a patch story from a patch-fix story costs more than it is worth, and the outlets differing over whether the problem is solved is the disagreement the site exists to show. See docs/CLUSTERING.md and prompts/judge.md.
- 2026-09-18 The golden set fixes membership only. Naming the differing take each cluster must yield is out of scope: articles about one news item are supposed to differ. `nc eval` therefore scores only what the contract can decide without an opinion.
