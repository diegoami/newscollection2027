"""Tests for `nc.labelpage`: what the labelling page is allowed to show.

Every test here is a regression for something that went wrong on
2026-09-22, when the page was refreshed by hand for the first time. The
page's failures are quiet -- a skewed sample and a miscounted readout
both look like a working page -- so the checks are the only thing that
would catch a repeat.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nc.cluster import ClusterItem, PendingPair
from nc.labelling import Label
from nc.labelpage import (
    LabelPageConfig,
    allocate,
    band_of,
    interleave,
    load_label_page_config,
    page_payload,
    render_page,
    select_for_page,
    select_from_queue,
    stride,
)

CONFIG = LabelPageConfig(
    budget=150, band_edges=(0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.57)
)


def _pair(score: float, n: int) -> PendingPair:
    """`pair_id` is derived from the two item ids, so they must differ
    per pair or two fixtures collide into one."""

    def side(tag: str) -> ClusterItem:
        return ClusterItem(
            item_id=f"{tag}{n:039x}"[:40],
            outlet="theverge" if tag == "a" else "arstechnica",
            title=f"Title {tag}{n}",
            lede=f"Lede {tag}{n}.",
            published="2026-09-20T10:00:00Z",
            url=f"https://example.com/{tag}{n}",
        )

    return PendingPair(a=side("a"), b=side("b"), score=score)


def _label(score: float, same: bool = False) -> Label:
    return Label(
        item_id_a="a" * 40,
        item_id_b="b" * 40,
        outlet_a="theverge",
        outlet_b="arstechnica",
        score=score,
        same_story=same,
        labeled_at="2026-09-20T10:00:00Z",
    )


def _real_shaped_pool() -> list[PendingPair]:
    """A pool shaped like the live one on 2026-09-22: lopsided, with
    most of its mass just above `tau_low` and a handful at the top.

    The shape is the point. `order_for_labelling` buckets the band into
    eight slices, so a fixture whose scores all land in one slice cannot
    reproduce what went wrong.
    """
    spec = [
        (0.570, 0.600, 151),
        (0.600, 0.650, 141),
        (0.650, 0.700, 58),
        (0.700, 0.750, 32),
        (0.750, 0.800, 16),
        (0.800, 0.885, 6),
    ]
    pool, n = [], 0
    for lo, hi, count in spec:
        for i in range(count):
            pool.append(_pair(lo + (hi - lo) * i / count, n))
            n += 1
    return pool


# --- the shipped config ----------------------------------------------------


def test_the_shipped_config_bands_reach_tau_low_and_below() -> None:
    """The lowest edge is `tau_low`, and there is a band under it. The
    zero-yield evidence below the threshold is half the argument that it
    is in the right place."""
    from nc.cluster import load_cluster_config

    config = load_label_page_config()
    assert config.band_edges[-1] == pytest.approx(load_cluster_config().tau_low)
    assert config.bands[-1][0] == 0.0
    assert config.bands[0][1] > 1.0  # a pair scoring exactly 1.0 has a home


def test_band_edges_must_descend() -> None:
    path = Path("config/labelpage.yaml")
    text = path.read_text(encoding="utf-8").replace(
        "band_edges: [0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.57]",
        "band_edges: [0.57, 0.85]",
    )
    scratch = Path("/tmp/bad-labelpage.yaml")
    scratch.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="descend"):
        load_label_page_config(scratch)


# --- the skew --------------------------------------------------------------


def test_the_page_covers_the_band_down_to_tau_low(tmp_path: Path) -> None:
    """**The 2026-09-22 bug.** A page built from the head of
    `order_for_labelling` had a lowest pair of 0.6079 while 38% of the
    pool sat below 0.60, and added nothing to [0.57, 0.60) -- the band
    with the fewest labels and the whole argument for `tau_low`.

    The pool here has the same lopsided shape as the real one: many
    pairs just above `tau_low`, a handful at the top.
    """
    pool = _real_shaped_pool()
    labels = [_label(0.62) for _ in range(33)] + [_label(0.58) for _ in range(10)]

    selected = select_for_page(labels, pool, CONFIG)
    scores = [p.score for p in selected]

    assert len(selected) == CONFIG.budget
    assert min(scores) < 0.60, "the band above tau_low is missing again"
    bottom = [s for s in scores if 0.57 <= s < 0.60]
    assert len(bottom) >= 20, f"only {len(bottom)} pairs in the band needing them most"
    # Spread across the band, not piled at its top: the pool's bottom
    # band runs 0.5800 to 0.5950, so a top-slice selection would have
    # nothing in its lower half.
    assert min(bottom) < 0.5850, "the bottom band was sampled from its top"


def test_the_page_beats_the_recipe_that_failed(tmp_path: Path) -> None:
    """The literal 2026-09-22 mistake, side by side.

    `order_for_labelling` is right for `nc label`, which walks the whole
    list. Its *head* is what skews, because each bucket is
    highest-score-first. This asserts the difference rather than
    describing it.
    """
    from nc.labelling import order_for_labelling

    pool = _real_shaped_pool()
    labels = [_label(0.62) for _ in range(33)] + [_label(0.58) for _ in range(10)]

    broken = order_for_labelling(pool, 0.57, 1.0)[: CONFIG.budget]
    fixed = select_for_page(labels, pool, CONFIG)

    assert min(p.score for p in broken) >= 0.60, "fixture no longer reproduces it"
    assert min(p.score for p in fixed) < 0.60


def test_the_thinnest_band_is_filled_first() -> None:
    bands = CONFIG.bands
    thin, fat = bands[-2], bands[-3]  # [0.57,0.60) and [0.60,0.65)
    taken = allocate(have={thin: 2, fat: 40}, available={thin: 50, fat: 50}, budget=20)
    assert taken[thin] > taken[fat]
    assert taken[thin] + taken[fat] == 20


def test_allocation_never_exceeds_what_a_band_holds() -> None:
    bands = CONFIG.bands
    taken = allocate(have={}, available={bands[0]: 2, bands[1]: 100}, budget=50)
    assert taken[bands[0]] == 2
    assert sum(taken.values()) == 50


def test_allocation_stops_when_the_pool_runs_out() -> None:
    bands = CONFIG.bands
    taken = allocate(have={}, available={bands[0]: 1, bands[1]: 2}, budget=99)
    assert sum(taken.values()) == 3


# --- the stride ------------------------------------------------------------


def test_stride_spreads_rather_than_taking_the_top() -> None:
    """The second half of the same bug: even with the right number of
    pairs per band, taking each band's top pairs re-creates the skew
    inside the band."""
    items = list(range(100))

    picked = stride(items, 5)

    assert picked == [0, 20, 40, 60, 80]
    assert picked != items[:5]


def test_the_page_does_not_front_load_the_matches(tmp_path: Path) -> None:
    """The third half of the bug: the right pairs, in the wrong order.

    The page used to emit band by band, highest first, so the first
    fifty pairs a human answered were nearly all matches and every
    negative waited at the end -- and a session abandoned halfway then
    contributed only the top bands, which is the original skew again.
    Every quarter of the page has to look like the whole page.
    """
    pool = _real_shaped_pool()
    bands = CONFIG.bands

    selected = select_for_page([], pool, CONFIG)

    quarter = len(selected) // 4
    quarters = [selected[i : i + quarter] for i in range(0, quarter * 4, quarter)]
    means = [sum(p.score for p in q) / len(q) for q in quarters]
    assert max(means) - min(means) < 0.02, f"front-loaded by score: {means}"
    for index, part in enumerate(quarters):
        seen = {band_of(p.score, bands) for p in part}
        assert len(seen) >= 5, f"quarter {index} draws from only {len(seen)} band(s)"


def test_interleave_spreads_a_thin_band_across_a_thick_one() -> None:
    """Two of one band and forty of another: the two land near a third
    and two thirds of the way in, not adjacent at either end."""
    thin, thick = (0.85, 1.01), (0.57, 0.60)

    order = interleave({thin: ["T1", "T2"], thick: [f"k{i}" for i in range(40)]})

    positions = [i for i, item in enumerate(order) if item.startswith("T")]
    assert positions == [10, 31]


def test_interleave_is_deterministic() -> None:
    """A page has to be reproducible from its inputs to be debuggable,
    which is why this is an interleave and not a shuffle."""
    by_band = {(0.7, 0.75): list("abcde"), (0.57, 0.6): list("vwxyz")}

    assert interleave(by_band) == interleave(dict(reversed(by_band.items())))


def test_interleave_keeps_every_item() -> None:
    by_band = {(0.8, 0.85): [1, 2, 3], (0.6, 0.65): [4], (0.57, 0.6): [5, 6]}

    assert sorted(interleave(by_band)) == [1, 2, 3, 4, 5, 6]


def test_stride_degenerate_cases() -> None:
    assert stride([1, 2, 3], 0) == []
    assert stride([1, 2, 3], 9) == [1, 2, 3]
    assert stride([], 4) == []


def test_band_of_puts_a_low_score_in_the_bottom_band() -> None:
    bands = CONFIG.bands
    assert band_of(0.30, bands) == bands[-1]
    assert band_of(0.58, bands) == (0.57, 0.60)
    assert band_of(1.0, bands) == bands[0]


# --- what the page is handed ------------------------------------------------


def test_page_payload_carries_both_sides_and_the_score() -> None:
    (row,) = page_payload([_pair(0.6123, 1)])
    assert row["s"] == 0.6123
    assert set(row["a"]) == {"o", "t", "l", "i", "p"}
    assert row["a"]["o"] == "theverge"
    assert row["b"]["o"] == "arstechnica"


# --- publishing guards ------------------------------------------------------

_PAGE = (
    '<html><body><script id="pairs-data" type="application/json">[]</script>'
    '<script>claude.use("db"); '
    "var done = PAIRS.filter(function (p) { return labels[p.id]; }).length;"
    "</script></body></html>"
)


def test_the_queue_page_takes_the_head_of_the_queue() -> None:
    """Not a stride: the judge would reach these first, and they are the
    ones most likely to make a story."""
    queue = [_pair(0.95 - i * 0.001, i) for i in range(300)]
    picked = select_from_queue(queue, 150)
    assert {p.pair_id for p in picked} == {p.pair_id for p in queue[:150]}


def test_the_queue_page_does_not_front_load_the_high_scores() -> None:
    queue = [_pair(0.95 - i * 0.001, i) for i in range(150)]
    picked = select_from_queue(queue, 150)
    first, last = picked[:50], picked[-50:]
    assert picked != queue
    assert (
        abs(sum(p.score for p in first) / 50 - sum(p.score for p in last) / 50) < 0.03
    )
    assert select_from_queue(queue, 150) == picked


def test_the_queue_page_copes_with_a_short_queue() -> None:
    queue = [_pair(0.7, i) for i in range(3)]
    assert len(select_from_queue(queue, 150)) == 3
    assert select_from_queue(queue, 0) == []


def test_render_page_swaps_the_data_block() -> None:
    out = render_page(_PAGE, page_payload([_pair(0.61, 1)]))
    assert '"s": 0.61' in out
    assert 'claude.use("db")' in out


def test_render_page_refuses_an_unscoped_progress_readout() -> None:
    """**The other 2026-09-22 bug.** `labels` is restored from the
    artifact's database, so it holds every answer the page has ever
    taken. Counting its keys against this batch made the counter read
    '222 / 150'. It only became wrong when the page stopped being a
    snapshot, which is exactly when nobody re-checks it."""
    broken = _PAGE.replace(
        "PAIRS.filter(function (p) { return labels[p.id]; }).length",
        "Object.keys(labels).length",
    )
    with pytest.raises(ValueError, match="222 / 150"):
        render_page(broken, page_payload([_pair(0.61, 1)]))


def test_render_page_refuses_a_page_that_lost_the_database() -> None:
    with pytest.raises(ValueError, match="answers would not be saved"):
        render_page(_PAGE.replace('claude.use("db")', "void 0"), [])


def test_render_page_refuses_something_that_is_not_the_page() -> None:
    with pytest.raises(ValueError, match="not the labelling page"):
        render_page("<html><body>hello</body></html>", [])
