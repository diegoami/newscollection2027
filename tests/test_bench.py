"""Tests for `nc.bench`'s reading of the store.

`bench_model` is otherwise exercised through `nc bench-embed` against
real candidate models, which this sandbox has no egress for. What is
worth pinning here without a model is which *row* it benchmarks when the
store holds an id twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from nc.bench import bench_model
from nc.embed import HashBackend, embed_text
from nc.feeds import Item
from nc.labelling import Label, append_label
from nc.store import DataRoot, append_items

MODEL_ID = "test/hash-8"


def _item(id_: str, title: str, published: str, fetched: str) -> Item:
    return Item(
        id=id_,
        outlet="bbc-technology",
        url=f"https://example.com/{id_}",
        title=title,
        lede="A lede.",
        author=None,
        published=published,
        fetched=fetched,
        tags=(),
    )


class _RecordingBackend(HashBackend):
    """A `HashBackend` that keeps the texts it was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return super().embed(texts)


def test_bench_model_benchmarks_the_freshest_row_for_a_duplicated_id(
    tmp_path: Path,
) -> None:
    """A dict comprehension over the rows picks whichever comes last in
    (file, id) order; `latest_by_id` picks the freshest *fetched*. Those
    usually agree, which is why the two rules coexisted unnoticed.

    They disagree when a re-fetch moves an item's `published` date
    *backwards* -- an outlet correcting a timestamp, which files the
    newer row under an earlier date. File order then puts the stale row
    last, and the dict comprehension benchmarks text the outlet has
    already replaced. Constructed that way here deliberately: a test
    where both rules give the same answer proves nothing about which one
    is running.

    The headlines differ because that is what makes it matter -- on the
    live data BBC re-published an item under a new date with a new
    headline, so the two rows embed to different vectors.
    """
    root = DataRoot(tmp_path / "data")
    # Fetched first, filed under the later date: last in file order.
    stale = _item(
        "a" * 40,
        title="Microsoft says AI rival could have disastrous consequences",
        published="2026-09-17T14:16:14Z",
        fetched="2026-09-16T17:04:33Z",
    )
    # Fetched second, but the outlet corrected `published` backwards, so
    # it is filed under the earlier date and comes first in file order.
    rewritten = replace(
        stale,
        title="Uncontrolled AI could lead to 'silicon species' rivalling humans",
        published="2026-09-16T08:22:12Z",
        fetched="2026-09-17T09:06:52Z",
    )
    other = _item(
        "b" * 40,
        title="A different story entirely",
        published="2026-09-17T09:00:00Z",
        fetched="2026-09-17T09:06:52Z",
    )
    append_items(root, [stale])
    append_items(root, [rewritten, other])

    append_label(
        root,
        Label(
            item_id_a=stale.id,
            item_id_b=other.id,
            outlet_a=stale.outlet,
            outlet_b=other.outlet,
            score=0.5,
            same_story=False,
            labeled_at="2026-09-17T10:00:00Z",
        ),
    )

    backend = _RecordingBackend()
    result = bench_model(root, MODEL_ID, backend)

    assert result.labels_scored == 1
    assert embed_text(rewritten) in backend.texts
    assert embed_text(stale) not in backend.texts
