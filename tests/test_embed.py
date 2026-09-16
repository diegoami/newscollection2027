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

from pathlib import Path

from nc.embed import (
    DEFAULT_VECTORS_DB_PATH,
    EmbedConfig,
    HashBackend,
    embed_items,
    embed_text,
    get_vector,
    load_embed_config,
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
    assert stored_item_ids(db_path) == {"a" * 40, "b" * 40}

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
    assert stored_item_ids(db_path) == {"a" * 40, "b" * 40}


def test_embed_items_batches_without_dropping_or_duplicating_items(
    tmp_path: Path,
) -> None:
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "cache" / "vectors.sqlite"
    items = [_item(f"{i:040d}") for i in range(5)]
    append_items(root, items)

    result = embed_items(root, HashBackend(), MODEL_ID, db_path, batch_size=2)

    assert result.embedded == 5
    assert stored_item_ids(db_path) == {item.id for item in items}


def test_store_round_trips_a_vector_exactly(tmp_path: Path) -> None:
    root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "cache" / "vectors.sqlite"
    item = _item("a" * 40, title="A title", lede="A lede.")
    append_items(root, [item])

    backend = HashBackend()
    expected_vector = backend.embed([embed_text(item)])[0]
    embed_items(root, backend, MODEL_ID, db_path, batch_size=10)

    stored = get_vector(item.id, db_path)
    assert stored == expected_vector  # exact, not approx: see nc.embed._pack


def test_get_vector_returns_none_for_an_unknown_item(tmp_path: Path) -> None:
    db_path = tmp_path / "cache" / "vectors.sqlite"
    assert get_vector("unknown", db_path) is None


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
    vector_a_before = get_vector(items[0].id, vectors_db)

    # A completely different file: nc.sqlite, the rebuildable cache.
    nc_db = tmp_path / "cache" / "nc.sqlite"
    assert rebuild_db(root, nc_db) == 2

    # The vectors file was never opened by rebuild_db, so it is
    # byte-for-byte what it was before -- not just "still has the same
    # ids", the actual vector is unchanged.
    assert stored_item_ids(vectors_db) == {item.id for item in items}
    assert get_vector(items[0].id, vectors_db) == vector_a_before

    # And embed_items still treats both as already embedded: rebuilding
    # nc.sqlite did not quietly cause re-embedding work either.
    result_after_rebuild = embed_items(
        root, backend, MODEL_ID, vectors_db, batch_size=10
    )
    assert result_after_rebuild.embedded == 0
    assert result_after_rebuild.already_stored == 2
