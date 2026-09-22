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

**This is a stratified sample, not a representative one.** It is sized
to estimate a boundary, not the pool's overall match rate. A page built
this way over-represents the sparse high bands on purpose. Anyone who
wants to know what share of the pool is a real match needs a random
sample and must say so -- reading it off this page gives the wrong
answer, confidently.

`render_page` handles the other half: swapping the pair data into the
published HTML, with the invariants that page has to keep. It refuses
rather than publishes when one breaks, because the page's failures are
quiet -- a miscounted progress readout or a stale data block looks
exactly like a working page.
"""

from __future__ import annotations

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


def select_for_page(
    labels: Iterable[Label],
    pool: Iterable[PendingPair],
    config: LabelPageConfig,
) -> list[PendingPair]:
    """The pairs one page should carry, given what is already labelled."""
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
    return [pair for band in bands for pair in stride(by_band[band], taken[band])]


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
