"""Tests for T20's `nc.embed`.

`Model2VecBackend` (the real model) is never constructed here: this
sandbox has no egress to Hugging Face (see nc/embed.py's module
docstring), so it cannot be exercised locally. Everything storage,
caching and idempotence related is tested against `HashBackend`
instead -- a deterministic, network-free fake -- which is exactly what
`embed_items` is built to accept. The real model's determinism is
proven separately by `.github/workflows/embed-check.yml`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from nc.embed import (
    DEFAULT_VECTORS_DB_PATH,
    EmbedConfig,
    HashBackend,
    embed_items,
    embed_text,
    get_vector,
    load_embed_config,
    load_vectors,
    stored_item_ids,
)
from nc.feeds import Item
from nc.store import DataRoot, append_items, rebuild_db

MODEL_ID = "hash-backend-test"


def _item(id_: str, title: str = "Title", lede: str = "Lede.") -> Item:
    return Item(
        id=id_,
        outlet="theverge",
        url=f"https://example.com/{id_}",
        title=title,
        lede=lede,
        author=None,
        published="2026-09-16T10:00:00Z",
        fetched="2026-09-16T10:00:00Z",
        tags=(),
    )


# --- config -----------------------------------------------------------


def test_load_embed_config_reads_the_shipped_config() -> None:
    config = load_embed_config()
    assert config.model_id  # non-empty
    assert config.model_dir == Path(".cache/hf-models")
    assert config.batch_size > 0


def test_load_embed_config_reads_a_local_file(tmp_path: Path) -> None:
    path = tmp_path / "embed.yaml"
    path.write_text("model_id: some/model\nmodel_dir: .cache/custom\nbatch_size: 16\n")
    config = load_embed_config(path)
    assert config == EmbedConfig(
        model_id="some/model", model_dir=Path(".cache/custom"), batch_size=16
    )


# --- embed_text ---------------------------------------------------------


def test_embed_text_is_title_space_lede() -> None:
    item = _item("a" * 40, title="SpaceX launches Starship", lede="It landed safely.")
    assert embed_text(item) == "SpaceX launches Starship It landed safely."


# --- HashBackend: deterministic, no network -----------------------------


def test_hash_backend_is_deterministic_across_separate_instances() -> None:
    text = "Apple unveils new iPhone with a faster chip"
    vector_1 = HashBackend().embed([text])[0]
    vector_2 = HashBackend().embed([text])[0]  # a fresh instance
    assert vector_1 == vector_2


def test_hash_backend_gives_different_texts_different_vectors() -> None:
    backend = HashBackend()
    vector_a = backend.embed(["SpaceX launches new rocket"])[0]
    vector_b = backend.embed(["Apple unveils new iPhone"])[0]
    assert vector_a != vector_b


# --- embed_items: storage, idempotence -----------------------------------


def test_embed_items_embeds_only_items_without_a_vector(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    append_items(root, [_item("a" * 40), _item("b" * 40)])
    db_path = tmp_path / "cache" / "vectors.sqlite"

    result = embed_items(root, HashBackend(), MODEL_ID, db_path, batch_size=10)
    assert result.embedded == 2
    assert result.already_stored == 0
    assert stored_item_ids(MODEL_ID, db_path) == {"a" * 40, "b" * 40}

    # Re-running against the same data root and db embeds nothing new.
    result_2 = embed_items(root, HashBackend(), MODEL_ID, db_path, batch_size=10)
    assert result_2.embedded == 0
    assert result_2.already_stored == 2


def test_embed_items_only_embeds_the_genuinely_new_item(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "cache" / "vectors.sqlite"
    append_items(root, [_item("a" * 40)])
    embed_items(root, HashBackend(), MODEL_ID, db_path, batch_size=10)

    append_items(root, [_item("a" * 40), _item("b" * 40)])  # "a" already stored
    result = embed_items(root, HashBackend(), MODEL_ID, db_path, batch_size=10)

    assert result.embedded == 1
    assert result.already_stored == 1
    assert stored_item_ids(MODEL_ID, db_path) == {"a" * 40, "b" * 40}


def test_embed_items_batches_without_dropping_or_duplicating_items(
    tmp_path: Path,
) -> None:
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "cache" / "vectors.sqlite"
    items = [_item(f"{i:040d}") for i in range(5)]
    append_items(root, items)

    result = embed_items(root, HashBackend(), MODEL_ID, db_path, batch_size=2)

    assert result.embedded == 5
    assert stored_item_ids(MODEL_ID, db_path) == {item.id for item in items}


def test_store_round_trips_a_vector_exactly(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "cache" / "vectors.sqlite"
    item = _item("a" * 40, title="A title", lede="A lede.")
    append_items(root, [item])

    backend = HashBackend()
    expected_vector = backend.embed([embed_text(item)])[0]
    embed_items(root, backend, MODEL_ID, db_path, batch_size=10)

    stored = get_vector(item.id, MODEL_ID, db_path)
    assert stored == expected_vector  # exact, not approx: see nc.embed._pack


def test_get_vector_returns_none_for_an_unknown_item(tmp_path: Path) -> None:
    db_path = tmp_path / "cache" / "vectors.sqlite"
    assert get_vector("unknown", MODEL_ID, db_path) is None


def test_default_vectors_db_path_is_under_dot_cache() -> None:
    assert DEFAULT_VECTORS_DB_PATH == Path(".cache/vectors.sqlite")


# --- decision 2: nc db rebuild must not wipe the vectors ------------------


def test_db_rebuild_does_not_touch_the_vectors_db(tmp_path: Path) -> None:
    """T20 brief, decision 2: `nc.store.rebuild_db` drops and recreates
    `.cache/nc.sqlite` from scratch on every call. Vectors are not
    derivable from the JSONL and are expensive to recompute, so they
    live in a completely separate SQLite file
    (`DEFAULT_VECTORS_DB_PATH`) that `rebuild_db` never opens, never
    mind drops. This asserts that choice holds end to end: embed, then
    rebuild the *other* database, then confirm the vectors are still
    exactly what they were, and that `embed_items` still recognizes
    them as already stored.
    """
    root = DataRoot(tmp_path / "data")
    items = [_item("a" * 40), _item("b" * 40)]
    append_items(root, items)

    vectors_db = tmp_path / "cache" / "vectors.sqlite"
    backend = HashBackend()
    result = embed_items(root, backend, MODEL_ID, vectors_db, batch_size=10)
    assert result.embedded == 2
    vector_a_before = get_vector(items[0].id, MODEL_ID, vectors_db)

    # A completely different file: nc.sqlite, the rebuildable cache.
    nc_db = tmp_path / "cache" / "nc.sqlite"
    assert rebuild_db(root, nc_db) == 2

    # The vectors file was never opened by rebuild_db, so it is
    # byte-for-byte what it was before -- not just "still has the same
    # ids", the actual vector is unchanged.
    assert stored_item_ids(MODEL_ID, vectors_db) == {item.id for item in items}
    assert get_vector(items[0].id, MODEL_ID, vectors_db) == vector_a_before

    # And embed_items still treats both as already embedded: rebuilding
    # nc.sqlite did not quietly cause re-embedding work either.
    result_after_rebuild = embed_items(
        root, backend, MODEL_ID, vectors_db, batch_size=10
    )
    assert result_after_rebuild.embedded == 0
    assert result_after_rebuild.already_stored == 2


# --- one item, two models -------------------------------------------------
#
# The vector cache is keyed on (item_id, model_id). Before that, changing
# config/embed.yaml's model left every existing item on the old model's
# vector -- embed_items skipped it as "already stored" -- while new items
# got the new one, and nc.cluster then compared vectors from two models
# in a single cosine. See nc/embed.py's _SCHEMA.


def test_two_models_store_separate_vectors_for_one_item(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "vectors.sqlite"
    append_items(root, [_item("a" * 40)])

    embed_items(root, HashBackend(dim=8), "model-one", db_path)
    embed_items(root, HashBackend(dim=4), "model-two", db_path)

    one = get_vector("a" * 40, "model-one", db_path)
    two = get_vector("a" * 40, "model-two", db_path)
    assert one is not None and two is not None
    assert len(one) == 8
    assert len(two) == 4
    assert one != two


def test_changing_the_model_re_embeds_instead_of_reusing_old_vectors(
    tmp_path: Path,
) -> None:
    """The bug this key exists to prevent: the second model must not
    report the items as already embedded and silently keep the first
    model's vectors."""
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "vectors.sqlite"
    append_items(root, [_item("a" * 40), _item("b" * 40)])

    first = embed_items(root, HashBackend(dim=8), "model-one", db_path)
    assert (first.embedded, first.already_stored) == (2, 0)

    second = embed_items(root, HashBackend(dim=4), "model-two", db_path)
    assert (second.embedded, second.already_stored) == (2, 0)

    # ... and re-running the second model is still a no-op.
    third = embed_items(root, HashBackend(dim=4), "model-two", db_path)
    assert (third.embedded, third.already_stored) == (0, 2)


def test_load_vectors_never_returns_another_models_vectors(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "vectors.sqlite"
    items = [_item("a" * 40), _item("b" * 40)]
    append_items(root, items)
    embed_items(root, HashBackend(dim=8), "model-one", db_path)

    assert load_vectors([item.id for item in items], "model-one", db_path).keys() == {
        item.id for item in items
    }
    # A model that embedded nothing sees nothing, rather than inheriting
    # the other model's vectors.
    assert load_vectors([item.id for item in items], "model-two", db_path) == {}
    assert stored_item_ids("model-two", db_path) == set()


def test_a_database_with_the_old_single_column_key_is_rebuilt(
    tmp_path: Path,
) -> None:
    """`CREATE TABLE IF NOT EXISTS` cannot widen a primary key, so a
    cache written by an older build has to be dropped. It is a
    rebuildable cache under .cache/, never the record of anything."""
    import sqlite3

    db_path = tmp_path / "vectors.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    stale = sqlite3.connect(db_path)
    stale.execute(
        "CREATE TABLE vectors (item_id TEXT PRIMARY KEY, model_id TEXT NOT NULL, "
        "dim INTEGER NOT NULL, vector BLOB NOT NULL)"
    )
    stale.execute(
        "INSERT INTO vectors VALUES (?, ?, ?, ?)",
        ("a" * 40, "old-model", 1, b"\x00" * 8),
    )
    stale.commit()
    stale.close()

    # The first read through nc.embed migrates it: the stale row is gone
    # and the new key is in force.
    assert stored_item_ids("old-model", db_path) == set()

    root = DataRoot(tmp_path / "data")
    append_items(root, [_item("a" * 40)])
    embed_items(root, HashBackend(dim=8), "model-one", db_path)
    embed_items(root, HashBackend(dim=4), "model-two", db_path)
    assert stored_item_ids("model-one", db_path) == {"a" * 40}
    assert stored_item_ids("model-two", db_path) == {"a" * 40}


# --- nc bench-embed: comparing models against the labels ------------------


def test_cosine_matches_the_clustering_scan(tmp_path: Path) -> None:
    """`nc.bench` has to compute the same number `nc.cluster.scan_pairs`
    does, or a benchmark measures something the pipeline never uses."""
    import numpy as np

    from nc.bench import cosine

    left = [0.3, -0.7, 0.1, 0.9]
    right = [0.2, 0.5, -0.4, 0.6]
    matrix = np.asarray([left, right], dtype=np.float32)
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    expected = float((matrix @ matrix.T)[0, 1])
    assert cosine(left, right) == pytest.approx(expected, abs=1e-6)


def test_cosine_treats_a_zero_vector_as_orthogonal() -> None:
    from nc.bench import cosine

    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_roc_auc_is_one_when_every_true_outranks_every_false() -> None:
    from nc.bench import roc_auc

    assert roc_auc([(0.9, True), (0.8, True), (0.4, False), (0.1, False)]) == 1.0
    assert roc_auc([(0.9, False), (0.8, False), (0.4, True), (0.1, True)]) == 0.0


def test_roc_auc_is_chance_when_every_score_ties() -> None:
    """Ties share the average rank, so a model that scores everything
    identically lands at chance rather than at whatever the sort order
    happened to be."""
    from nc.bench import roc_auc

    assert roc_auc([(0.5, True), (0.5, False), (0.5, True), (0.5, False)]) == 0.5


def test_roc_auc_is_chance_when_a_class_is_empty() -> None:
    from nc.bench import roc_auc

    assert roc_auc([(0.9, True), (0.8, True)]) == 0.5


def test_recall_at_precision_1_counts_true_pairs_above_the_best_false() -> None:
    """The headline metric: how many genuine matches a threshold could
    auto-link before it would link a wrong one."""
    from nc.bench import measure

    scored = [
        (0.90, True),
        (0.85, True),
        (0.80, False),  # the highest false pair caps everything below it
        (0.75, True),
        (0.60, True),
    ]
    result = measure("m", scored, skipped=0)
    assert result.true_pairs == 4
    assert result.recall_at_precision_1 == pytest.approx(0.5)
    assert result.tau_for_precision_1 == pytest.approx(0.85)
    assert result.unreachable_true_pairs == 2
    assert not result.separable


def test_a_true_pair_tied_with_a_false_one_is_not_reachable() -> None:
    """A tie is not a clean link: precision 1.0 means linking strictly
    above the best false pair."""
    from nc.bench import measure

    result = measure("m", [(0.8, True), (0.8, False)], skipped=0)
    assert result.recall_at_precision_1 == 0.0
    assert result.tau_for_precision_1 is None


def test_a_separable_model_reaches_full_recall_at_precision_1() -> None:
    from nc.bench import measure

    result = measure("m", [(0.9, True), (0.8, True), (0.4, False)], skipped=0)
    assert result.recall_at_precision_1 == 1.0
    assert result.separable
    assert result.unreachable_true_pairs == 0


def test_score_labels_skips_and_counts_pairs_whose_items_are_gone(
    tmp_path: Path,
) -> None:
    """A benchmark that quietly measured half the label set would
    flatter whichever model ran last."""
    from nc.bench import score_labels
    from nc.labelling import Label

    items = {item.id: item for item in [_item("a" * 40), _item("b" * 40)]}
    labels = [
        Label(
            "a" * 40, "b" * 40, "theverge", "wired", 0.7, True, "2026-09-17T00:00:00Z"
        ),
        Label(
            "a" * 40, "c" * 40, "theverge", "wired", 0.5, False, "2026-09-17T00:00:00Z"
        ),
    ]
    scored, skipped = score_labels(labels, items, HashBackend(dim=8))
    assert len(scored) == 1
    assert skipped == 1


def test_bench_never_reads_the_stored_label_score(tmp_path: Path) -> None:
    """`Label.score` is whatever model ran at labelling time. A
    benchmark that trusted it would report that every candidate scores
    exactly like the one already configured."""
    from nc.bench import score_labels
    from nc.labelling import Label

    items = {item.id: item for item in [_item("a" * 40), _item("b" * 40)]}
    nonsense = Label(
        "a" * 40, "b" * 40, "theverge", "wired", -99.0, True, "2026-09-17T00:00:00Z"
    )
    scored, _ = score_labels([nonsense], items, HashBackend(dim=8))
    assert scored[0][0] != -99.0
    assert -1.0 <= scored[0][0] <= 1.0


# --- the same id in two day files -----------------------------------------


def test_embed_items_does_not_embed_a_duplicated_id_twice(tmp_path: Path) -> None:
    """The store can hold one id in two day files (an outlet
    re-publishing under a new date). `INSERT OR REPLACE` meant the
    second embedding's only effect was to overwrite the first: the
    output was right, the model work was wasted."""
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "cache" / "vectors.sqlite"
    first = _item("a" * 40)
    republished = replace(
        first, published="2026-09-19T10:00:00Z", fetched="2026-09-19T11:00:00Z"
    )
    append_items(root, [first])
    append_items(root, [republished])

    backend = _CountingBackend()
    result = embed_items(root, backend, MODEL_ID, db_path, batch_size=10)

    assert result.embedded == 1
    assert backend.texts_seen == 1
    assert stored_item_ids(MODEL_ID, db_path) == {"a" * 40}


class _CountingBackend(HashBackend):
    """A `HashBackend` that records how many texts it was asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.texts_seen = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts_seen += len(texts)
        return super().embed(texts)
