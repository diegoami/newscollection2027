# Clustering: labelling and tuning

T22. Two commands that turn `nc cluster`'s borderline band into evidence
for T23's threshold choice, and how to read what they print. Read
`docs/ARCHITECTURE.md`'s Clustering section first, and `src/nc/cluster.py`'s
module docstring if the "why" behind any of this is unclear -- this page
only covers the two new commands and how to use them.

## Where the borderline pairs come from

Every `nc cluster` run scores every pair in the four-day window. Pairs
at or above `tau_high` link; pairs below `tau_low` are discarded; pairs
in between -- the borderline band -- are written to
`pending-pairs/<pair_id>.json` in the data root, one file per pair, with
both items fully denormalized (outlet, title, lede, published) and the
score. That is what makes `nc label` a pure file-reading operation: no
embedding model, no vectors, works offline.

These files accumulate. They are not deleted when a pair ages out of
the window or when the thresholds are retuned -- see
`nc.cluster.write_pending_pairs`'s docstring for why deleting them would
either reopen the churn problem T12's acceptance criterion exists to
prevent, or silently shrink the pool a labelling session draws from.
Pruning judged or resolved pairs out of `pending-pairs/` is T24's job,
once its judge starts consuming them.

## The labelling corpus: `label-sample/`

Found on PR #39, before it merged: `pending-pairs/` only ever holds
`[tau_low, tau_high)`, so a labelling session built only from it can
only ever contain contested middle pairs. That leaves `nc tune` with no
clear negatives to anchor the bottom of the curve, no clear positives to
measure precision *above* `tau_high` with, and a recall number that is
conditional on a pair having been in the band in the first place --
the "recall's blind spot" bullet below, which used to be about
`tau_low` alone.

**The fix is not a wider `tau_low`.** `tau_low` also gates T24's judge
queue (it is the same band that becomes `pending-pairs/`): lowering it
to get a better labelling sample would queue thousands of obviously
unrelated pairs for T24's paid LLM judge every three hours, for no
labelling benefit that couldn't be had more cheaply. Labelling and the
judge queue are two different purposes and must not share a threshold.

Instead, `nc cluster` also writes a **bounded, stratified sample**,
independent of the band, to `label-sample/<pair_id>.json` in the data
root -- same file shape as `pending-pairs/` (both items denormalized,
plus the score), different selection rule and a different directory,
deliberately:

- `pending-pairs/` is defined by the band (`[tau_low, tau_high)`),
  uncapped, and is T24's judge input.
- `label-sample/` is defined by a stratification of the *whole* usable
  score range into `config/cluster.yaml`'s `sample_floor` up to 1.0, in
  `sample_bucket_width`-wide slices, each capped at
  `sample_bucket_cap` pairs (defaults: 0.30, 0.05, 20). It exists only
  so a labelling session also sees clear negatives and clear positives,
  and it is never read by T24's judge.

Conflating the two -- for example, capping `pending-pairs/` itself, or
widening `tau_low` to feed both purposes from one band -- would re-couple
exactly what keeping them apart is for: the judge queue's size would
start depending on a labelling knob, or the labelling sample's coverage
would start depending on how many pairs happen to be paid to judge.

**Why the selection is stable.** `nc cluster` runs every three hours and
commits its output, so `label-sample/` must not churn the way a
"keep the best N seen so far" sample would. `nc.cluster.select_label_sample`
is deterministic (a pair's bucket is a fixed function of its own score,
rounded to the same precision the file format stores -- see that
function's docstring for why the rounding step itself matters, not just
the bucketing) and *additive only*: a pair already written to
`label-sample/` is never reconsidered, never evicted and never replaces
another pair, however a later run's new candidates would sort. New
pairs can only fill a bucket that still has room under its cap. Combined
with `_write_if_changed`, a `nc cluster` run with no new items writes
zero `label-sample/` bytes for the same reason it writes zero cluster
and zero pending-pair bytes --
`tests/test_cluster.py::test_labelling_sample_is_stable_across_a_no_op_rerun`
pins this, and
`tests/test_cluster.py::test_labelling_sample_selection_is_stable_when_a_bucket_is_full`
pins the sharper case: new items that add candidates to an *already
full* bucket leave the file already written for that bucket completely
untouched -- same bytes, same mtime.

**Expected size.** The number of buckets is fixed by the config
(`ceil((1.0 - sample_floor) / sample_bucket_width)`, 14 with the
defaults), not by how many items or pairs exist, so the corpus is
bounded at `14 * sample_bucket_cap` = 280 files with the shipped
defaults, regardless of window or corpus size -- the opposite of an
all-pairs dump. For the real corpus this project has seen so far (303
items in the four-day window, ~45,700 pairs, see "starting evidence"
below), that means at most 280 `label-sample/` files ever accumulate
from a single run's candidates, and in practice fewer: most 0.05-wide
slices near 1.0 are sparse for real news headlines, so they rarely fill
to the cap.

## `nc label`: labelling a session

```
nc label                  # every unlabeled pair, band and sample alike
nc label --limit 50       # stop after 50 (still saved, still resumable)
```

`nc label` draws from `nc.labelling.labelling_pool`: the union of
`pending-pairs/` and `label-sample/`, deduplicated by pair id. Not just
`label-sample/` alone -- `sample_bucket_cap` means the sample is not
guaranteed to hold every pair in the band, so reading only the sample
would silently shrink the exhaustive band coverage `nc label` has
always given, which T23's "label at least 200 near-threshold pairs"
leans on. Not just `pending-pairs/` alone either -- that directory only
ever held the band, which is the gap the labelling corpus closes.
Reading the union keeps both: every borderline pair stays reachable,
and a session also sees the clear positives and negatives it never saw
before. A pair picked by both (the stratified sample also lands some
pairs inside the band) is shown once, since `pair_id` is the same key
in both directories.

For each unlabeled pair it shows both outlets, both titles, both ledes
and the score, then asks:

```
same story? [y/n/s=skip/q=quit]
```

- `y` / `n` (also `yes` / `no`) records the judgment and appends one
  line to `labels/pairs.jsonl` in the data root immediately -- not
  buffered, so a killed session loses at most the pair on screen.
- `s` / `skip` moves on without recording anything. A skipped pair is
  shown again in a later session; use it for a pair you are genuinely
  unsure about rather than guessing.
- `q` / `quit` (or Ctrl-D) stops the session. Nothing already answered
  is lost.
- Anything else reprompts. There is no way to record a judgment by
  accident.

Resuming: every invocation reads `labels/pairs.jsonl` first and never
shows a pair whose id is already in it. Quitting and restarting costs
nothing.

**Ordering.** Pairs are not shown lowest-to-highest or in file order.
`order_for_labelling` splits `[tau_low, tau_high)` into 8 equal slices
and round-robins across them, highest score first within each slice. A
session that stops after 30 answers still has roughly 4 labels from
every part of the band, not 30 answers clustered around whatever score
happens to be most common. This matters because `nc tune`'s table is
only as good as its coverage: 200 labels bunched at 0.66 tell you
nothing about 0.75. A pair from `label-sample/` outside `[tau_low,
tau_high)` -- a clear negative or a clear positive -- clamps into the
nearest end bucket rather than being dropped, so it is still shown, just
not spread across its own sub-range the way the band is; ordering
between the labelling corpus's own buckets was not worth the added
complexity for T22's brief and is left as a possible follow-up if a
labelling session ends up front-loaded with clear cases.

**Record format**, one JSON object per line in `labels/pairs.jsonl`:

```json
{"item_id_a": "...", "item_id_b": "...", "outlet_a": "theverge",
 "outlet_b": "tomshardware", "score": 0.7231, "same_story": true,
 "labeled_at": "2026-09-16T12:00:00Z"}
```

This is human-produced data, not pipeline output: CLAUDE.md's
determinism rule covers `ingest`/`cluster`/`validate`/`build`, not a
person answering yes or no. It is append-only by construction and
carries no idempotence obligation the way a cluster file does.

## `nc tune`: reading the report

```
nc tune
```

Reads every label and prints one row per distinct labeled score
(highest first) -- not an arbitrary step size, because every threshold
that can change which labeled pairs would auto-link *is* one of the
observed scores; nothing changes between two consecutive ones. Each row
answers: "if `tau_high` were set here, what would precision and recall
look like, among the labels I have?"

```
threshold  n>=t  precision            recall               false links  missed links
   0.9012     3       1.000 (3/3)*        0.130 (3/23)             0             20
   0.8420    11       0.909 (10/11)       0.435 (10/23)            1             13
   0.7800    41       0.780 (32/41)       1.000 (23/23)*           9              0
```

(Illustrative numbers, not real ones -- see "starting evidence" below
for what the one real run actually showed.)

Read it like this:

- **`n>=t`** is how many labeled pairs score at or above the threshold
  -- the denominator behind precision. A row with `n>=t` under 5 has a
  `*` after its precision: three labels do not make a rate, they make
  noise. The same `*` appears on recall when the total number of
  same-story labels is under 5 -- printed once at the top of the table,
  since it does not change per row.
- **Precision** is "of the pairs this threshold would link, how many
  really were the same story." This is the number that matters most:
  a false link merges two unrelated stories into one page, and the
  analysis step then reports fabricated-looking "discrepancies" between
  outlets that were never covering the same event. That is not
  recoverable downstream -- nothing later in the pipeline checks it.
- **Recall** is "of the same-story pairs I labeled, how many did this
  threshold catch." A missed link only drops a story to single-outlet
  obscurity, or leaves it for T24's judge to catch in the borderline
  band. Cheaper, but still worth watching: if the report shows the
  same-story rate only nearing 1.0 at the very bottom of the labeled
  range, that is a sign `tau_low` may be sitting above where real
  matches start, not just `tau_high`.
- **Recall's blind spot, narrowed but not closed.** Before the
  labelling corpus, every label came from a pair `nc cluster` judged
  borderline at the time -- scored in `[tau_low, tau_high)` under
  whatever thresholds were live when it was written -- so a true match
  scoring below the old `tau_low`, or a false match scoring above the
  old `tau_high`, was never persisted or labeled at all. `label-sample/`
  now persists both, down to `sample_floor`, so this table can measure
  precision above `tau_high` for the first time and recall down to
  `sample_floor` rather than only down to `tau_low`. What is still
  invisible: a true match scoring below `sample_floor`. That pair was
  never persisted, never labeled, and never counted here -- `nc tune`
  cannot tell you how much recall you are losing below the floor; only
  lowering `sample_floor` itself (a labelling knob, not `tau_low`) and
  labelling for a while can answer that.

**Choosing a value.** Given the asymmetry above, prefer the highest
threshold with zero false links over one with marginally better recall
-- `nc tune` prints this as its closing line, not just as advice here.
Concretely: scan the false-links column from the top down and stop
picking `tau_high` past the first row where it turns positive, unless
that row's precision is well below 1.0 and backed by enough labels to
trust.

## Applying the result to `config/cluster.yaml`

1. Run a labelling session (T23: at least 200 pairs, ideally spread
   over several sittings since `nc label` never repeats a judged pair).
   With the labelling corpus in place, that session already draws clear
   positives and negatives alongside the band -- no separate step
   needed to get them.
2. Run `nc tune` and pick `tau_high` from the highest-precision row you
   trust, and `tau_low` from where recall stops improving or the labels
   run out (see "recall's blind spot" above -- lowering `sample_floor`
   and labelling again is the only way to check below it; `tau_low`
   itself is not the knob for that, see "The labelling corpus" above).
3. Edit `tau_low` and `tau_high` in `config/cluster.yaml` directly
   (both are plain numbers with comments explaining what they do; no
   other file needs to change). `sample_floor`, `sample_bucket_width`
   and `sample_bucket_cap` are separate knobs in the same file and are
   not part of this step -- they shape the labelling corpus, not
   clustering or the judge queue.
4. Retuning does **not** retroactively re-cluster: `nc cluster` seeds
   every run from the memberships already on disk (module docstring,
   "Membership is monotonic"), so a new `tau_high` only affects links
   not yet made. To re-cluster from scratch with the new thresholds --
   reassigning every cluster id and orphaning every existing analysis
   -- clear `clusters/` and `pending/` in the data root and run `nc
   cluster` again. That is deliberately not a flag on the command; do
   it once, knowingly, after tuning settles.

## Starting evidence: one real run, 2026-09-16

The first real `nc cluster` run against 303 items from 11 outlets over
a four-day window reported:

```
10 linked pair(s), 74 borderline pair(s) in [0.65, 0.8) awaiting T24
294 component(s) -> 3 cluster(s)
dropped 291 singleton component(s) (286 single-item, 5 single-outlet)
```

All 3 emitted clusters were checked by hand and are genuinely the same
story across two outlets -- precision at `tau_high = 0.80` looks right.
But seven times as many pairs sat just below it as above it, and 3
shared stories out of 303 items across 11 outlets over four days is
implausibly low for tech news -- recall looks poor. This is one run,
not a measurement of the thresholds: `tau_high: 0.80` was chosen from
transformer intuition, and `nc.embed` uses `model2vec`, a *static*
embedding model whose absolute cosine scale is not the same as a
transformer's. The likely fix is that `tau_high` needs to come down,
but "likely" is exactly what T23's labelling and this page's `nc tune`
report are for -- do not retune from this paragraph alone.

## What never reaches the labelling pool

Two filters run before a human ever sees a pair. Both come from the
first real labelling session, where the pool of 223 pairs turned out to
be 46% unusable.

**Promotional items are dropped from the window** (`config/promo.yaml`,
`nc.promo`). The outlets do not publish only news: 27 of Wired's 55
items in one four-day window were coupon pages, and Tom's Hardware
posts build-a-PC discounts. These are not stories, and they are near
duplicates *of each other*, so they outscore genuine cross-outlet
matches: "Casetify Promo Codes | 15% Off" against "Visible Promo Codes
and Coupons" scored 0.7323, above The Register and BleepingComputer on
the same Iranian malware campaign at 0.7330 — and above most real
pairs. Three things follow, worst first: a coupon page can reach a
story cluster (two shopping desks linking, plus one link out to a real
article, satisfies `min_outlets`), it crowds the top score buckets out
of the labelling sample, and it corrupts the labels — 3 of the first 10
"same story" answers were two unrelated coupon pages.

The rules are of two kinds because one is not enough. An outlet's own
feed categories are the reliable signal, since a human at the outlet
chose them; that catches The Verge completely. Tom's Hardware files
discounts under `Gaming PCs` like any other hardware piece, so titles
are matched too, on shopping-copy constructions only. Two rules were
tried and rejected against the real corpus: a bare `deal` (it is
ordinary business vocabulary — "a $1.4B SPAC deal", "reciprocal
severance deals") and Wired's `Shopping` tag (it also carries product
launches and reviews). Both are documented in `config/promo.yaml` so
they are not re-added.

The filter runs at cluster time, not ingest time. The item store is
append-only and is the record, so nothing is deleted; widening or
narrowing the rules changes the next run's window with no migration.

**Same-outlet pairs are dropped from the labelling sample**
(`select_label_sample`), but kept in `pending-pairs/`. A cluster is
emitted only with two or more distinct outlets, so the judgment the
thresholds govern is whether two *outlets* are on the same story. In
the first corpus, same-outlet pairs were 97 of 223 and 7 of the 10
above 0.80 — one masthead's own follow-ups crowding exactly the part of
the range the thresholds are set from. They stay in the band for T24's
judge because a same-outlet link still matters to clustering: it can
bridge two items of one outlet into a component that reaches a second
outlet, and that component is a legitimate cluster holding two items
from the same masthead. It is the human's hour that should not go on
them.

Net effect on the first corpus: 223 pairs to 121, and 318 items to 290.

## Choosing the embedding model

`nc bench-embed` scores every model in `config/embed.yaml`'s
`bench_candidates` against `labels/pairs.jsonl` and reports which one
gets most of the score range right enough to act on. It runs from
`.github/workflows/embed-bench.yml`, manually — the development sandbox
has no route to huggingface.co, the same wall that shaped T20.

**Why this became a question.** `nc.embed` accepted on the record that
"a lower but consistent embedding quality is absorbed by threshold
tuning". The first labelling session measured that and it does not
hold. Tuning can only absorb a weak embedding when the two populations
are separable by some cutoff; on 174 labels they overlap, with a
different-story pair at 0.7992 and true matches down to 0.5708. No
threshold gets both ends right, so the model became a variable rather
than a settled decision.

**The metric is recall at precision 1.0** — of all the genuine matches,
how many score above the highest-scoring false pair, and could
therefore be auto-linked with no wrong link at all. For
`potion-base-8M` that is 4/35 = 0.114, which is
why auto-linking earns so little today: it saves roughly 10 judge calls
a run while owning the one failure mode that is not recoverable
downstream. A model that lifts it to 0.7 changes the architecture.
`roc_auc` sits beside it as a threshold-free summary, because
recall-at-precision-1.0 turns on a single pair and a model can lose
there while ranking better everywhere else.

The labels carry over to any candidate: "are these the same story" is a
fact about the two articles, not about the model that scored them. The
*scores* stored in `labels/pairs.jsonl` do not — they are whatever
model ran at labelling time — so `nc.bench` re-embeds both items of
every pair and recomputes the cosine rather than reading `Label.score`.

**Switching the model is not a config edit.** `model_id` is part of the
vector cache's key, so a change re-embeds every stored item, and both
thresholds were tuned against the old model's scale and become
meaningless with it. The two move together, or not at all.

## What the model comparison found

`nc bench-embed` was run on 2026-09-17 against the 159 labelled pairs
available at the time, over six model2vec models spanning a 64x
parameter range. The numbers below are that measurement and have not
been recomputed since; the label set has grown to 174 (see "What more
labels changed" below), which moved the configured model's recall@p1.0
from 0.111 to 0.114 and left the shape of the table alone.

```
model                          recall@p1.0     auc      tau  top false  judge
potion-retrieval-32M                 0.222   0.923   0.7051     0.6974     21
potion-base-4M                       0.148   0.935   0.8100     0.8091     23
potion-base-32M                      0.148   0.925   0.7567     0.7530     23
potion-base-8M  (configured)         0.111   0.946   0.8175     0.7710     24
potion-base-2M                       0.111   0.920   0.8487     0.8333     24
potion-multilingual-128M             0.111   0.889   0.8351     0.8061     24
```

**The curve is flat.** 2M to 128M is sixty-four times the parameters and
recall at precision 1.0 goes 0.111, 0.148, 0.111, 0.148, 0.111. There is
no relationship between capacity and separability on this task. That is
the answer to "would a better model fix this": within static embeddings,
no — and the ladder was built to answer exactly that, so the flatness is
a result rather than a disappointment.

**Every model ranks well and classifies badly.** AUC runs 0.889 to 0.946
across the six, and the configured model is the best of them. High AUC
with low recall-at-precision-1.0 means the same thing every time: the
ordering is broadly right, but a few different-story pairs score at the
very top, above most true matches. Those are the topic-versus-event
confusions — two AI-safety pieces, two iOS 27 articles — and capacity
cannot fix them, because both articles genuinely are about the same
subject. Only something that reasons about *events* can tell them apart.

**Do not read the top row as a winner.** `potion-retrieval-32M` leads on
recall@p1.0, but 0.222 against 0.111 is three pairs out of 27, and its
AUC is lower than the configured model's. Switching costs a re-embed of
every stored item and re-tuning both thresholds from scratch. Three
pairs, inside the noise, does not pay for that.

**What it changed.** The thresholds, not the model. Cosine stays as a
recall filter — its real strength, and what keeps the judge from being
handed all ~49,000 pairs in a four-day window — and stops being asked to
classify. `tau_high: 1.00`, `tau_low: 0.57`; see `config/cluster.yaml`
for the reasoning on each and `docs/DECISIONS.md` for the decision.

The honest caveat, repeated because it matters: 27 positives is a thin
basis, and recall@p1.0 turns on a single pair — the highest-scoring
false one. What carries the conclusion is not any single row but six
independent measurements agreeing that size does not help.

## What more labels changed

The first pass produced 159 usable labels with 27 positives; a second
sitting took it to **174 labels, 35 positives**. The conclusions did not
move, and one of them got a good deal firmer.

**Recall at precision 1.0 went 0.111 to 0.114** for the configured
model -- four true pairs above the highest false one instead of three.
Statistically the same number.

**The highest-scoring false pair moved *up*, from 0.7710 to 0.7992:**

```
0.7992  zdnet / wired
   "I've used both iPhone 18 Pro models - here's how my buying advice is changing in 2026"
   "What's the Best iPhone to Buy or Avoid Right Now? (2026)"
```

Two iPhone buying-advice pieces: the same subject, no shared event.
The same failure as the AI-safety pair that topped the first pass,
from a different corner of the corpus.

That is the useful part. On 159 labels, the highest safe threshold was
0.78 -- above the 0.7710 false pair, below a true one at 0.8175 -- and
that is what a tuning exercise would have shipped. Fifteen labels later
it would be making a false link. **The ceiling on safe auto-linking rose
with more evidence rather than settling**, which is what a threshold
tracking the most recent unlucky pair does. It is the strongest single
argument for `tau_high: 1.00`: the number was not merely buying little,
it was not stable enough to be worth buying.

**`tau_low: 0.57` held.** The lowest true pair is still 0.5708, the same
TechCrunch/Tom's Hardware pair, and no new positive landed below it --
now across two independent rounds of labelling.
