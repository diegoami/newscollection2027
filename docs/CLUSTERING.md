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

## `nc label`: labelling a session

```
nc label                  # every unlabeled pending pair
nc label --limit 50       # stop after 50 (still saved, still resumable)
```

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
nothing about 0.75.

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
- **Recall's blind spot.** These labels only ever came from pairs `nc
  cluster` judged borderline at the time, i.e. pairs that scored in
  `[tau_low, tau_high)` under whatever thresholds were live when they
  were written. A true match that scored below the old `tau_low` was
  never persisted, never labeled, and never counted here. `nc tune`
  cannot tell you how much recall you are losing below the band; only a
  wider band (a lower `tau_low`, labeled for a while) can answer that.

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
2. Run `nc tune` and pick `tau_high` from the highest-precision row you
   trust, and `tau_low` from where recall stops improving or the labels
   run out (see "recall's blind spot" above -- widening the band and
   labelling again is the only way to check below it).
3. Edit `tau_low` and `tau_high` in `config/cluster.yaml` directly
   (both are plain numbers with comments explaining what they do; no
   other file needs to change).
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
