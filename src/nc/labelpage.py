"""The data behind the labelling page, and the guards on publishing it.

`nc label` is the terminal way to label borderline pairs. The page --
"Same Story?", an Artifact -- is the other way, and it exists because a
labelling session is two hundred yes/no answers in a row and a phone is
a better place for that than a terminal.

This module is the part that must not be improvised. The page itself is
published HTML; what goes *into* it is a sampling decision, and a
sampling decision made by hand each time is one that can be made wrong
each time. It was, on 2026-09-22:

    A page built from the first 150 of `nc.labelling.order_for_labelling`
    had a lowest pair of 0.6079, while 38% of the pool sat below 0.60.
    That ordering round-robins across buckets *and takes the highest
    score first within each one* -- correct for `nc label`, where the
    session works down the whole list, and wrong the moment you truncate
    it, because you then get the top slice of every bucket. The page
    would have collected 150 answers and added none to [0.57, 0.60),
    which is the band with the fewest labels and the one carrying the
    whole argument that `tau_low` is not set too low.

So: allocate per band, thinnest band first, and take a stride *across*
each band rather than its top. `select_for_page` is that, and the tests
pin both halves against exactly this regression.

The order the page presents them in is the third half of this, and
the third bug, same day:

    The page emitted band by band, highest first. The sample was
    48% matches against the pool's 36% -- defensible for a stratified
    sample -- but the first fifty pairs a human actually saw were 77%
    matches, and every negative sat in the last fifty. "Almost all of
    them still seem to be positive" was an accurate report of the page.

That is not only an unpleasant hour. A labelling session that stops
early -- the normal way one ends -- then contributes only the top
bands, which is the 0.6079 bug again by a different route. So
`interleave` spreads the bands evenly, and every prefix of the page
carries the band mix of the whole page.

**This is a stratified sample, not a representative one.** It is sized
to estimate a boundary, not the pool's overall match rate. A page built
this way over-represents the sparse high bands on purpose. Anyone who
wants to know what share of the pool is a real match needs a random
sample and must say so -- reading it off this page gives the wrong
answer, confidently.

**Or the judge's queue, with `--queue`.** Since a label decides its
pair (nc/judge.py's module docstring), the page is also a way to work
the judge's backlog by hand. That wants the opposite selection: not a
spread across bands, but the head of `nc.judge.unjudged_pairs` --
cross-outlet first, highest score first -- because those are the pairs
most likely to make a story. `select_from_queue` takes that head and
nothing else. Only the order changes: it is scattered like a band is,
so the page still does not front-load its likely matches.

`render_page` handles the other half: swapping the pair data into the
published HTML, with the invariants that page has to keep. It refuses
rather than publishes when one breaks, because the page's failures are
quiet -- a miscounted progress readout or a stale data block looks
exactly like a working page.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nc.cluster import PendingPair
from nc.labelling import Label

DEFAULT_CONFIG_PATH = Path("config/labelpage.yaml")

Band = tuple[float, float]

# The page's data block, and the readout that must never come back.
_DATA_BLOCK = re.compile(
    r'(<script id="pairs-data" type="application/json">)(.*?)(</script>)', re.S
)
# `labels` in the page is restored from the artifact's database and from
# local storage, so it holds every answer ever given the page -- not
# this batch's. Counting its keys against the batch is what made the
# counter read "222 / 150". Anything reporting progress has to filter
# by the batch, as the rail and the export already did.
_UNSCOPED_PROGRESS = re.compile(r"Object\.keys\(labels\)\.length")


@dataclass(frozen=True)
class LabelPageConfig:
    budget: int
    band_edges: tuple[float, ...]

    @property
    def bands(self) -> list[Band]:
        """Descending bands: one per edge, plus everything below the
        lowest edge.

        `1.01` rather than `1.0` as the top so a pair scoring exactly
        1.0 lands in a band instead of nowhere -- scores are cosine
        similarities and 1.0 is reachable. The final band, below the
        lowest edge, is where the evidence that `tau_low` is not too low
        lives, so it is a band rather than a discard.
        """
        edges = list(self.band_edges)
        bands = [(edge, edges[i - 1] if i else 1.01) for i, edge in enumerate(edges)]
        bands.append((0.0, edges[-1]))
        return bands


def load_label_page_config(path: Path = DEFAULT_CONFIG_PATH) -> LabelPageConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")
    edges = [float(e) for e in raw["band_edges"]]
    if sorted(edges, reverse=True) != edges:
        raise ValueError(f"{path}: band_edges must descend, got {edges}")
    return LabelPageConfig(budget=int(raw["budget"]), band_edges=tuple(edges))


def band_of(score: float, bands: Sequence[Band]) -> Band:
    for band in bands:
        if band[0] <= score < band[1]:
            return band
    return bands[-1]


def allocate(
    have: Mapping[Band, int], available: Mapping[Band, int], budget: int
) -> dict[Band, int]:
    """How many pairs each band contributes: one at a time, to whichever
    band would be thinnest afterwards.

    Not proportional to the pool. The pool is lopsided -- on 2026-09-22,
    151 pairs in [0.57, 0.60) against 2 above 0.85 -- and a proportional
    sample would spend the page on the band that already has the most
    labels. Ties break toward the higher band, where pairs are scarce
    and a label is therefore worth more.
    """
    taken: dict[Band, int] = {band: 0 for band in available}
    for _ in range(budget):
        open_bands = [b for b in taken if taken[b] < available[b]]
        if not open_bands:
            break
        taken[min(open_bands, key=lambda b: (have.get(b, 0) + taken[b], -b[0]))] += 1
    return taken


def stride(items: Sequence[Any], count: int) -> list[Any]:
    """`count` items spread evenly across `items` -- never its first
    `count`, which is the mistake this exists to prevent."""
    if count <= 0:
        return []
    if count >= len(items):
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def _scatter(pair: PendingPair) -> str:
    """A pair's position within its band on the page: stable, so a page
    is reproducible from its inputs, and independent of the score, so a
    pair's position tells a labeller nothing about its answer.

    A digest rather than `pair_id` itself. Real item ids are content
    digests and would scatter on their own, but that is a property of
    `nc.store`'s id scheme rather than a promise to this module, and an
    id scheme that ever became sequential would quietly restore the
    drift this exists to remove.
    """
    return hashlib.blake2b(pair.pair_id.encode("utf-8"), digest_size=8).hexdigest()


def interleave(by_band: Mapping[Band, Sequence[Any]]) -> list[Any]:
    """Every band's pairs spread evenly across one page, so that any
    prefix holds roughly the band mix of the whole.

    Item `i` of a band of `n` gets position `(i + 0.5) / n` on a unit
    line and everything is sorted by that; a band of 2 lands at 0.25
    and 0.75, a band of 46 every 0.022. Deterministic -- no shuffle,
    because a page has to be reproducible from its inputs to be
    debuggable -- and it beats a shuffle at the thing that matters
    here, since a shuffle only gets the mix right on average and this
    gets it right on every prefix.

    Ties go to the higher band, matching `allocate`.
    """
    placed: list[tuple[float, float, Any]] = []
    for band, items in by_band.items():
        for index, item in enumerate(items):
            placed.append(((index + 0.5) / len(items), -band[0], item))
    placed.sort(key=lambda row: (row[0], row[1]))
    return [item for _, _, item in placed]


def select_for_page(
    labels: Iterable[Label],
    pool: Iterable[PendingPair],
    config: LabelPageConfig,
) -> list[PendingPair]:
    """The pairs one page should carry, given what is already labelled,
    in the order it should ask them -- see `allocate` for which pairs,
    `stride` for which of a band, and `interleave` for the order."""
    bands = config.bands
    recorded = list(labels)
    have = {
        band: sum(1 for label in recorded if band_of(label.score, bands) == band)
        for band in bands
    }
    by_band: dict[Band, list[PendingPair]] = {band: [] for band in bands}
    for pair in pool:
        by_band[band_of(pair.score, bands)].append(pair)
    for band in bands:
        by_band[band].sort(key=lambda p: p.score)

    taken = allocate(have, {b: len(by_band[b]) for b in bands}, config.budget)
    # Picked in score order, because `stride` spreads across a sorted
    # band; presented scattered, because otherwise `interleave` walks
    # every band low-to-high at once and the page still drifts upward
    # -- 0.65 mean in its first quarter against 0.69 in its last.
    chosen = {
        band: sorted(stride(by_band[band], taken[band]), key=_scatter) for band in bands
    }
    return interleave({b: picks for b, picks in chosen.items() if picks})


def select_from_queue(queue: Sequence[PendingPair], budget: int) -> list[PendingPair]:
    """The first `budget` pairs of the judge's queue, in scattered order.

    `queue` is `nc.judge.unjudged_pairs`, already in priority order, so
    the selection is its head and not a stride: the point of this mode
    is to answer the pairs the judge would reach first. The order is
    `_scatter`'s, because the queue's own order is highest score first,
    and presenting it that way is the front-loading the band sample was
    fixed for -- the likely matches all first, and a session that stops
    early having seen only them.
    """
    return sorted(queue[: max(budget, 0)], key=_scatter)


def page_payload(pairs: Sequence[PendingPair]) -> list[dict[str, Any]]:
    """The JSON the page reads, in its own short key names."""
    return [
        {
            "id": p.pair_id,
            "s": round(p.score, 4),
            "a": {
                "o": p.a.outlet,
                "t": p.a.title,
                "l": p.a.lede,
                "i": p.a.item_id,
                "p": p.a.published,
            },
            "b": {
                "o": p.b.outlet,
                "t": p.b.title,
                "l": p.b.lede,
                "i": p.b.item_id,
                "p": p.b.published,
            },
        }
        for p in pairs
    ]


def render_page(html: str, payload: Sequence[Mapping[str, Any]]) -> str:
    """The published page with `payload` as its pairs, or a raise.

    Every check here is a failure that would otherwise ship looking
    fine: a page whose data did not swap, whose database capability was
    lost, or whose progress readout counts a different set from the one
    it is rendering.
    """
    match = _DATA_BLOCK.search(html)
    if match is None:
        raise ValueError("no pairs-data block: this is not the labelling page")
    if 'claude.use("db")' not in html:
        raise ValueError("the db capability is gone: answers would not be saved")

    swapped = _DATA_BLOCK.sub(
        lambda _: (
            match.group(1)
            + json.dumps(list(payload), ensure_ascii=False, separators=(", ", ": "))
            + match.group(3)
        ),
        html,
        count=1,
    )

    found = _UNSCOPED_PROGRESS.search(swapped)
    if found is not None:
        raise ValueError(
            "progress readout counts `labels` rather than this batch "
            "(Object.keys(labels).length): it outlives the batch and the "
            "counter read '222 / 150'. Filter by PAIRS."
        )
    after = _DATA_BLOCK.search(swapped)
    assert after is not None
    if len(json.loads(after.group(2))) != len(payload):
        raise ValueError("the data block did not take")
    return swapped
