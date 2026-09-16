"""Tests for T21's `nc.cluster`.

No real embedding model anywhere: this sandbox has no egress to Hugging
Face (see nc/embed.py's module docstring), and clustering must be
testable without it in any case. Vectors here are built by
`_vector` below, which places every item at an exactly known cosine
similarity to every other -- so a test that says "these two must not
link" is making a statement about the clustering rule, not about how a
model happens to feel about two headlines.

The synthetic set is T21's acceptance criterion: 30 items with known
groups, built to discriminate rather than to pass -- obvious same-story
groups across outlets, a near-miss that must not link, a single-outlet
group that must be dropped, and a pair parked inside the borderline
band.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path

import pytest

from nc.cluster import (
    STATUS_SUPERSEDED,
    Cluster,
    ClusterConfig,
    ClusterItem,
    PendingPair,
    UnionFind,
    WriteResult,
    _allocate_id,
    cluster_items,
    format_report,
    load_cluster_config,
    load_clusters,
    load_pending_pairs,
    pending_pair_from_dict,
    pending_pair_path,
    render,
    render_pending_pair,
    run_clustering,
    scan_pairs,
    write_pending_pairs,
)
from nc.embed import HashBackend, embed_items, load_vectors
from nc.feeds import Item
from nc.store import DataRoot, append_items

CONFIG = ClusterConfig(tau_low=0.65, tau_high=0.80, window_days=4, min_outlets=2)

# Injected instead of the clock so the four-day window is the same in
# every run of every test. The synthetic items are published on
# 2026-09-15 and 2026-09-16, comfortably inside it.
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

# The number of "topic" axes the synthetic vectors below use. Each group
# of same-story items shares one; every item also gets a private axis, so
# items in different groups are exactly orthogonal.
_TOPICS = 12
_ITEM_AXES = 64
_DIM = _TOPICS + _ITEM_AXES


def _vector(topic: int, slot: int, similarity: float) -> list[float]:
    """A unit vector whose cosine with any other vector on the same
    `topic` and the same `similarity` is exactly `similarity`, and whose
    cosine with any vector on another topic is exactly 0.

    v = sqrt(s) * e_topic + sqrt(1 - s) * e_slot, with e_topic and every
    e_slot mutually orthogonal unit axes. Then
    <v_i, v_j> = s for i != j on the same topic, and 0 across topics.
    """
    values = [0.0] * _DIM
    values[topic] = math.sqrt(similarity)
    values[_TOPICS + slot] = math.sqrt(1.0 - similarity)
    return values


def _axis(*components: tuple[int, float]) -> list[float]:
    """A unit vector over the topic axes, e.g. `_axis((0, 0.4), (1, 0.9165))`.

    Two *stories* are not orthogonal in a real embedding space, and the
    merge fixtures below need that: no vector can sit at cosine >= 0.80
    from two vectors that are themselves orthogonal (the angles would
    have to sum to more than 90 degrees). A bridging item is only
    geometrically possible between stories that are already somewhat
    alike, which is also the only kind of merge that happens in
    practice."""
    values = [0.0] * _DIM
    for index, weight in components:
        values[index] = weight
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def _near(axis: Sequence[float], slot: int, similarity: float) -> list[float]:
    """sqrt(similarity) * `axis` + sqrt(1 - similarity) * a private axis."""
    values = [value * math.sqrt(similarity) for value in axis]
    values[_TOPICS + slot] = math.sqrt(1.0 - similarity)
    return values


def _item_id(seed: str) -> str:
    """A 40-hex id, the shape contract/analysis.schema.json requires of
    every `item_id` an analysis may quote."""
    return sha1(seed.encode("utf-8")).hexdigest()


def _item(
    seed: str,
    outlet: str,
    published: str = "2026-09-15T10:00:00Z",
    title: str | None = None,
) -> Item:
    return Item(
        id=_item_id(seed),
        outlet=outlet,
        url=f"https://{outlet}.example/{seed}",
        title=title or f"{seed} headline",
        lede=f"{seed} lede sentence.",
        author=None,
        published=published,
        fetched="2026-09-16T00:00:00Z",
        tags=(),
    )


# --- the synthetic 30 -----------------------------------------------------
#
# group           items  outlets  what it proves
# ---------------------------------------------------------------------
# A (0.95)          4      4      obvious same story, links, is emitted
# B (0.88)          3      3      same story, less similar, still links
# C (0.85)          2      2      the minimum emittable cluster
# D (0.85)          3      1      single outlet -> dropped, the product rule
# E (0.72)          2      2      borderline band -> not linked, reported
# F (0.50)          2      2      near miss -> never linked, never reported
# singles          14     14      one-item components -> dropped
#
# Groups A, B and D also sit on *different* topic axes from each other,
# so nothing links across groups: every assertion below is about the
# thresholds, not about accidental geometry.

_GROUPS: tuple[tuple[str, int, float, tuple[str, ...]], ...] = (
    ("a", 0, 0.95, ("theverge", "arstechnica", "techcrunch", "wired")),
    ("b", 1, 0.88, ("theverge", "engadget", "zdnet")),
    ("c", 2, 0.85, ("bbc", "theguardian")),
    ("d", 3, 0.85, ("theregister", "theregister", "theregister")),
    ("e", 4, 0.72, ("theverge", "tomshardware")),
    ("f", 5, 0.50, ("engadget", "bleepingcomputer")),
)


def _synthetic_set() -> tuple[list[Item], dict[str, list[float]]]:
    items: list[Item] = []
    vectors: dict[str, list[float]] = {}
    slot = 0
    for name, topic, similarity, outlets in _GROUPS:
        for index, outlet in enumerate(outlets):
            item = _item(f"{name}{index}", outlet)
            items.append(item)
            vectors[item.id] = _vector(topic, slot, similarity)
            slot += 1
    for index in range(30 - len(items)):
        item = _item(f"single{index}", f"outlet{index}")
        items.append(item)
        # Its own topic axis, shared with nothing: orthogonal to every
        # other item in the set.
        vectors[item.id] = _vector(_TOPICS - 1, slot, 0.0)
        slot += 1
    assert len(items) == 30
    return items, vectors


def _ids(*seeds: str) -> frozenset[str]:
    return frozenset(_item_id(seed) for seed in seeds)


def _memberships(clusters: Sequence[Cluster]) -> set[frozenset[str]]:
    return {cluster.item_ids for cluster in clusters}


# --- config ---------------------------------------------------------------


def test_load_cluster_config_reads_the_shipped_config() -> None:
    config = load_cluster_config()
    assert config.window_days == 4  # docs/ARCHITECTURE.md, Clustering
    assert config.min_outlets == 2
    assert 0.0 <= config.tau_low < config.tau_high <= 1.0


def test_load_cluster_config_rejects_inverted_thresholds(tmp_path: Path) -> None:
    path = tmp_path / "cluster.yaml"
    path.write_text("tau_low: 0.9\ntau_high: 0.5\nwindow_days: 4\nmin_outlets: 2\n")
    with pytest.raises(ValueError, match="tau_low <= tau_high"):
        load_cluster_config(path)


def test_load_cluster_config_rejects_a_zero_window(tmp_path: Path) -> None:
    path = tmp_path / "cluster.yaml"
    path.write_text("tau_low: 0.6\ntau_high: 0.8\nwindow_days: 0\nmin_outlets: 2\n")
    with pytest.raises(ValueError, match="window_days"):
        load_cluster_config(path)


# --- union-find -----------------------------------------------------------


def test_union_find_components_are_sorted_and_complete() -> None:
    union = UnionFind()
    for key in ("d", "c", "b", "a", "e"):
        union.add(key)
    union.union("c", "a")
    union.union("d", "c")
    assert union.components() == [["a", "c", "d"], ["b"], ["e"]]


def test_union_find_is_transitive_through_a_chain() -> None:
    union = UnionFind()
    union.union("a", "b")
    union.union("b", "c")
    union.union("c", "d")
    assert union.components() == [["a", "b", "c", "d"]]


# --- similarity -----------------------------------------------------------


def test_scan_pairs_splits_at_the_thresholds() -> None:
    items, vectors = _synthetic_set()
    scan = scan_pairs([item.id for item in items], vectors, 0.65, 0.80)

    linked = {frozenset((pair.a, pair.b)) for pair in scan.linked}
    borderline = {frozenset((pair.a, pair.b)) for pair in scan.borderline}

    # Group A is 4 items: all 6 of its pairs link. B is 3 items: 3
    # pairs. C and D are 2 and 3 items: 1 and 3 pairs.
    assert len(scan.linked) == 6 + 3 + 1 + 3
    assert _ids("a0", "a1") in linked
    # The borderline pair is reported, not linked.
    assert _ids("e0", "e1") in borderline
    assert _ids("e0", "e1") not in linked
    # The near miss is below tau_low: it appears nowhere at all.
    assert _ids("f0", "f1") not in linked
    assert _ids("f0", "f1") not in borderline
    assert len(scan.borderline) == 1
    assert scan.compared == 30
    assert scan.skipped_dim_mismatch == 0


def test_scan_pairs_ignores_items_without_a_vector() -> None:
    items, vectors = _synthetic_set()
    del vectors[_item_id("a0")]
    scan = scan_pairs([item.id for item in items], vectors, 0.65, 0.80)
    assert scan.compared == 29
    assert all(_item_id("a0") not in (pair.a, pair.b) for pair in scan.linked)


def test_scan_pairs_skips_vectors_of_a_stale_size() -> None:
    """What a half-finished model change looks like in the vector cache:
    a few items embedded with a different model's dimensionality."""
    items, vectors = _synthetic_set()
    vectors[_item_id("a0")] = [0.1, 0.2, 0.3]
    scan = scan_pairs([item.id for item in items], vectors, 0.65, 0.80)
    assert scan.skipped_dim_mismatch == 1
    assert scan.compared == 29


def test_scan_pairs_survives_a_zero_vector() -> None:
    items, vectors = _synthetic_set()
    vectors[_item_id("a0")] = [0.0] * _DIM
    scan = scan_pairs([item.id for item in items], vectors, 0.65, 0.80)
    assert all(math.isfinite(pair.score) for pair in scan.linked)
    assert all(_item_id("a0") not in (pair.a, pair.b) for pair in scan.linked)


def test_scan_pairs_handles_a_window_too_small_to_have_pairs() -> None:
    assert scan_pairs([], {}, 0.65, 0.80).linked == ()
    assert scan_pairs(["x"], {"x": [1.0, 0.0]}, 0.65, 0.80).linked == ()


# --- the acceptance criterion: 30 items, known groups ---------------------


def test_known_groups_cluster_as_expected() -> None:
    items, vectors = _synthetic_set()
    run = cluster_items(items, vectors, (), CONFIG)

    assert _memberships(run.clusters) == {
        _ids("a0", "a1", "a2", "a3"),
        _ids("b0", "b1", "b2"),
        _ids("c0", "c1"),
    }
    stats = run.stats
    assert stats.window_items == 30
    assert stats.clusters_new == 3
    assert stats.clusters_total == 3
    assert stats.items_without_vectors == 0

    # The single-outlet group (D) is dropped although its three items
    # linked: one outlet has nothing to compare.
    assert stats.dropped_single_outlet == 1
    # E and F never linked, so their 4 items are 4 one-item components,
    # as are the 14 singles.
    assert stats.dropped_single_item == 4 + 14
    assert stats.dropped_total == 19

    # The borderline pair is surfaced for T24 and nothing else.
    assert len(run.borderline) == 1
    assert {run.borderline[0].a, run.borderline[0].b} == set(_ids("e0", "e1"))


def test_cluster_ids_match_the_contract_pattern() -> None:
    import re

    items, vectors = _synthetic_set()
    run = cluster_items(items, vectors, (), CONFIG)
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{6}$")
    for cluster in run.clusters:
        assert pattern.match(cluster.id), cluster.id
        assert cluster.version == 1
        assert cluster.status == "pending"
        assert cluster.anchor in cluster.item_ids


def test_a_judged_borderline_pair_links(tmp_path: Path) -> None:
    """The T24 seam: a pair its judge accepts is linked exactly as a
    tau_high pair is, and nothing else in the algorithm changes."""
    items, vectors = _synthetic_set()
    judged = [(_item_id("e0"), _item_id("e1"))]
    run = cluster_items(items, vectors, (), CONFIG, extra_links=judged)
    assert _ids("e0", "e1") in _memberships(run.clusters)
    assert run.stats.clusters_new == 4
    # Still reported as borderline: judging it is T24's business, not
    # this module's, and the pair did not stop being borderline.
    assert len(run.borderline) == 1


# --- ids, versions, merges, ageing out ------------------------------------


def _first_run() -> tuple[list[Item], dict[str, list[float]], Cluster]:
    items, vectors = _synthetic_set()
    run = cluster_items(items, vectors, (), CONFIG)
    group_c = next(c for c in run.clusters if c.item_ids == _ids("c0", "c1"))
    return items, vectors, group_c


def test_a_new_member_keeps_the_id_and_bumps_the_version() -> None:
    items, vectors, group_c = _first_run()

    latecomer = _item("c2", "engadget", published="2026-09-16T09:00:00Z")
    items.append(latecomer)
    vectors[latecomer.id] = _vector(2, 40, 0.85)

    run = cluster_items(items, vectors, [group_c], CONFIG)
    grown = next(c for c in run.clusters if group_c.id == c.id)
    assert grown.id == group_c.id  # stable across the membership change
    assert grown.version == 2
    assert grown.anchor == group_c.anchor
    assert grown.item_ids == _ids("c0", "c1", "c2")
    assert grown.status == "pending"
    assert run.stats.clusters_requeued == 1
    assert run.stats.clusters_new == 2  # A and B, unchanged by any of this


def test_an_analyzed_cluster_is_left_alone_until_its_membership_changes() -> None:
    items, vectors, group_c = _first_run()
    analyzed = Cluster(
        id=group_c.id,
        version=group_c.version,
        anchor=group_c.anchor,
        status="analyzed",
        items=group_c.items,
    )
    run = cluster_items(items, vectors, [analyzed], CONFIG)
    unchanged = next(c for c in run.clusters if c.id == analyzed.id)
    assert unchanged == analyzed
    assert run.stats.clusters_requeued == 0


# --- merge fixture --------------------------------------------------------
#
# Two stories, four items, plus one item that bridges them. The two
# stories sit at cosine 0.4 from each other: close enough that a single
# item can be >= tau_high from both (a bridge between two *orthogonal*
# stories is geometrically impossible, see `_axis`), far enough that
# their items never link directly.

_STORY_ONE = _axis((0, 1.0))
_STORY_TWO = _axis((0, 0.4), (1, 0.9165))
_BRIDGE = _axis((0, 1.4), (1, 0.9165))  # the bisector of the two


def _merge_fixture() -> tuple[list[Item], dict[str, list[float]], Item]:
    older = _item("m0", "theverge", published="2026-09-14T08:00:00Z")
    older_peer = _item("m1", "wired", published="2026-09-14T09:00:00Z")
    newer = _item("n0", "zdnet", published="2026-09-15T08:00:00Z")
    newer_peer = _item("n1", "engadget", published="2026-09-15T09:00:00Z")
    bridge = _item("bridge", "bbc", published="2026-09-16T08:00:00Z")
    vectors = {
        older.id: _near(_STORY_ONE, 0, 0.98),
        older_peer.id: _near(_STORY_ONE, 1, 0.98),
        newer.id: _near(_STORY_TWO, 2, 0.98),
        newer_peer.id: _near(_STORY_TWO, 3, 0.98),
        bridge.id: _near(_BRIDGE, 4, 1.0),
    }
    return [older, older_peer, newer, newer_peer], vectors, bridge


def test_the_merge_fixture_really_is_two_separate_stories() -> None:
    """Guards the fixture itself: without this, a geometry mistake would
    make the merge tests below pass for the wrong reason."""
    items, vectors, bridge = _merge_fixture()
    scan = scan_pairs(
        [item.id for item in items], vectors, CONFIG.tau_low, CONFIG.tau_high
    )
    assert len(scan.linked) == 2  # one pair inside each story, none across
    assert scan.borderline == ()
    with_bridge = scan_pairs(
        [item.id for item in (*items, bridge)], vectors, CONFIG.tau_low, CONFIG.tau_high
    )
    assert len(with_bridge.linked) == 2 + 4  # the bridge links to all four


def test_a_bridging_item_merges_two_clusters_earliest_anchor_surviving() -> None:
    """Merge: one new item links two clusters that may both already have
    an analysis. The earlier anchor survives; the other id is retired
    with a pointer, not deleted."""
    items, vectors, bridge = _merge_fixture()
    first = cluster_items(items, vectors, (), CONFIG)
    assert len(first.clusters) == 2
    old_cluster = next(c for c in first.clusters if items[0].id in c.item_ids)
    new_cluster = next(c for c in first.clusters if items[2].id in c.item_ids)

    second = cluster_items([*items, bridge], vectors, first.clusters, CONFIG)

    assert len(second.clusters) == 1
    survivor = second.clusters[0]
    assert survivor.id == old_cluster.id  # earliest anchor wins
    assert survivor.anchor == old_cluster.anchor
    assert survivor.version == old_cluster.version + 1
    assert survivor.status == "pending"  # re-queued: the story changed
    assert survivor.item_ids == {item.id for item in (*items, bridge)}
    assert len(second.superseded) == 1
    retired = second.superseded[0]
    assert retired.id == new_cluster.id
    assert retired.superseded_by == survivor.id
    # Ratified on #38: supersession is a status value, not only a field.
    assert retired.status == STATUS_SUPERSEDED
    # The loser's own record is frozen, not rewritten: an analysis
    # written against it still describes exactly what it says.
    assert retired.items == new_cluster.items
    assert retired.version == new_cluster.version
    assert second.stats.clusters_superseded == 1


def test_a_superseded_cluster_is_not_resurrected_on_the_next_run() -> None:
    items, vectors, bridge = _merge_fixture()
    first = cluster_items(items, vectors, (), CONFIG)
    second = cluster_items([*items, bridge], vectors, first.clusters, CONFIG)
    third = cluster_items(
        [*items, bridge], vectors, [*second.clusters, *second.superseded], CONFIG
    )
    assert len(third.clusters) == 1
    assert third.clusters[0] == second.clusters[0]  # no further version bump
    assert third.stats.clusters_superseded == 0


def test_ageing_out_of_the_window_changes_nothing() -> None:
    """A cluster whose items have all left the window keeps every
    member, its version and its analysis. The window bounds new links,
    not membership."""
    _, vectors, group_c = _first_run()
    run = cluster_items([], vectors, [group_c], CONFIG)
    assert run.clusters == (group_c,)
    assert run.stats.clusters_requeued == 0
    assert run.stats.window_items == 0


def test_allocate_id_probes_deterministically_on_a_collision() -> None:
    anchor = _item_id("collide")
    first = _allocate_id(anchor, "2026-09-15T10:00:00Z", set())
    second = _allocate_id(anchor, "2026-09-15T10:00:00Z", {first})
    third = _allocate_id(anchor, "2026-09-15T10:00:00Z", {first, second})
    assert first != second != third
    assert {first, second, third} == {
        _allocate_id(anchor, "2026-09-15T10:00:00Z", set()),
        _allocate_id(anchor, "2026-09-15T10:00:00Z", {first}),
        _allocate_id(anchor, "2026-09-15T10:00:00Z", {first, second}),
    }
    assert all(entry.startswith("2026-09-15-") for entry in (first, second, third))


# --- the data root: files, idempotence ------------------------------------


class _FixedBackend:
    """An `EmbeddingBackend` that serves the synthetic vectors by text,
    so `nc embed` -> `.cache/vectors.sqlite` -> `nc cluster` can be
    exercised end to end without a model."""

    def __init__(self, by_text: dict[str, list[float]]) -> None:
        self._by_text = by_text

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._by_text[text] for text in texts]


def _prepare(
    tmp_path: Path, items: Sequence[Item], vectors: dict[str, list[float]]
) -> tuple[DataRoot, Path]:
    data_root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "vectors.sqlite"
    append_items(data_root, items)
    _embed(data_root, db_path, items, vectors)
    return data_root, db_path


def _embed(
    data_root: DataRoot,
    db_path: Path,
    items: Sequence[Item],
    vectors: dict[str, list[float]],
) -> None:
    by_text = {f"{item.title} {item.lede}": vectors[item.id] for item in items}
    embed_items(data_root, _FixedBackend(by_text), "synthetic", db_path)


def _snapshot(root: Path) -> dict[str, tuple[str, float]]:
    return {
        str(path.relative_to(root)): (path.read_text(), path.stat().st_mtime)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_run_clustering_writes_cluster_and_pending_files(tmp_path: Path) -> None:
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)

    report = run_clustering(data_root, CONFIG, db_path, now=NOW)

    assert report.run.stats.clusters_new == 3
    assert report.written.clusters_written == 3
    assert report.written.pending_written == 3
    for cluster in report.run.clusters:
        path = data_root.resolve("clusters", cluster.date, f"{cluster.id}.json")
        pending = data_root.resolve("pending", f"{cluster.id}.json")
        assert path.exists()
        assert pending.exists()
        # The analyze-clusters skill reads id, version and items
        # straight out of the pending file.
        payload = json.loads(pending.read_text())
        assert payload["id"] == cluster.id
        assert payload["version"] == 1
        assert {entry["outlet"] for entry in payload["items"]} == set(cluster.outlets)
        assert payload == json.loads(path.read_text())


def test_running_twice_with_no_new_items_writes_nothing(tmp_path: Path) -> None:
    """T21's idempotence requirement, the same one T12's acceptance
    criterion states for ingest: the workflow runs every three hours and
    commits its output, so a no-op run must produce no diff at all."""
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)

    run_clustering(data_root, CONFIG, db_path, now=NOW)
    before = _snapshot(data_root.path)
    time.sleep(0.01)
    second = run_clustering(data_root, CONFIG, db_path, now=NOW)
    after = _snapshot(data_root.path)

    assert after == before  # bytes *and* mtimes: nothing was rewritten
    assert second.written == WriteResult(0, 0, 0)
    assert second.run.stats.clusters_new == 0
    assert second.run.stats.clusters_requeued == 0
    assert [c.version for c in second.run.clusters] == [1, 1, 1]


def test_the_window_moving_forward_does_not_rewrite_anything(tmp_path: Path) -> None:
    """The same no-op, three hours later: every item has aged closer to
    the edge of the window and some have fallen out of it."""
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)
    run_clustering(data_root, CONFIG, db_path, now=NOW)
    before = _snapshot(data_root.path)

    later = run_clustering(
        data_root, CONFIG, db_path, now=datetime(2026, 9, 30, tzinfo=UTC)
    )
    assert _snapshot(data_root.path) == before
    assert later.run.stats.window_items == 0
    assert later.run.stats.clusters_total == 3


def test_a_new_item_requeues_its_cluster_and_leaves_the_others(tmp_path: Path) -> None:
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)
    first = run_clustering(data_root, CONFIG, db_path, now=NOW)
    grown_id = next(c.id for c in first.run.clusters if c.item_ids == _ids("c0", "c1"))

    latecomer = _item("c2", "engadget", published="2026-09-16T09:00:00Z")
    vectors[latecomer.id] = _vector(2, 40, 0.85)
    append_items(data_root, [latecomer])
    _embed(data_root, db_path, [latecomer], vectors)

    second = run_clustering(data_root, CONFIG, db_path, now=NOW)
    assert second.written.clusters_written == 1  # only the one that changed
    assert second.written.pending_written == 1
    payload = json.loads(
        data_root.resolve("clusters", grown_id[:10], f"{grown_id}.json").read_text()
    )
    assert payload["version"] == 2
    assert len(payload["items"]) == 3


def test_the_pending_file_mirrors_the_status(tmp_path: Path) -> None:
    """How a cluster leaves the queue: T30's `nc validate` sets `status`
    in the cluster file, and the next `nc cluster` run removes the
    pending file. No other step has to know where the queue lives."""
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)
    first = run_clustering(data_root, CONFIG, db_path, now=NOW)
    target = first.run.clusters[0]

    path = data_root.resolve("clusters", target.date, f"{target.id}.json")
    payload = json.loads(path.read_text())
    payload["status"] = "analyzed"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    second = run_clustering(data_root, CONFIG, db_path, now=NOW)
    assert second.written.pending_removed == 1
    assert not data_root.resolve("pending", f"{target.id}.json").exists()

    third = run_clustering(data_root, CONFIG, db_path, now=NOW)
    assert third.written.pending_removed == 0  # and it stays gone


def test_a_merge_retires_the_pending_file_of_the_loser(tmp_path: Path) -> None:
    items, vectors, bridge = _merge_fixture()
    data_root, db_path = _prepare(tmp_path, items, vectors)
    first = run_clustering(data_root, CONFIG, db_path, now=NOW)
    loser_id = next(c.id for c in first.run.clusters if items[2].id in c.item_ids)
    assert data_root.resolve("pending", f"{loser_id}.json").exists()

    append_items(data_root, [bridge])
    _embed(data_root, db_path, [bridge], vectors)

    second = run_clustering(data_root, CONFIG, db_path, now=NOW)
    assert second.run.stats.clusters_superseded == 1
    assert not data_root.resolve("pending", f"{loser_id}.json").exists()
    assert second.written.pending_removed == 1
    retired = json.loads(
        data_root.resolve("clusters", loser_id[:10], f"{loser_id}.json").read_text()
    )
    assert retired["superseded_by"] == second.run.clusters[0].id
    assert retired["status"] == STATUS_SUPERSEDED
    # Reloading is stable: the retired cluster is not reconsidered, and
    # a third run is a byte-level no-op.
    reloaded = load_clusters(data_root)
    assert len(reloaded) == 2
    assert sum(1 for c in reloaded if c.superseded_by) == 1
    third = run_clustering(data_root, CONFIG, db_path, now=NOW)
    assert third.written == WriteResult(0, 0, 0)


def test_load_clusters_round_trips_what_render_writes(tmp_path: Path) -> None:
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)
    first = run_clustering(data_root, CONFIG, db_path, now=NOW)
    reloaded = load_clusters(data_root)
    assert reloaded == list(first.run.clusters)
    assert all(
        render(c) == render(r)
        for c, r in zip(first.run.clusters, reloaded, strict=True)
    )


def test_items_without_vectors_are_reported_not_clustered(tmp_path: Path) -> None:
    """The ordinary case in the ingest workflow: items arrived after the
    last `nc embed`. They cannot link, and the count is visible."""
    items, vectors = _synthetic_set()
    data_root = DataRoot(tmp_path / "data")
    append_items(data_root, items)

    # Only the four group-A items have a vector. `embed_items` embeds
    # every item in the data root it is given, so the partial state is
    # built by embedding from a second data root that holds just those
    # four, into the vector cache the clustering run then reads.
    db_path = tmp_path / "vectors.sqlite"
    partial_root = DataRoot(tmp_path / "partial")
    append_items(partial_root, items[:4])
    _embed(partial_root, db_path, items[:4], vectors)

    report = run_clustering(data_root, CONFIG, db_path, now=NOW)
    assert report.run.stats.window_items == 30
    assert report.run.stats.items_without_vectors == 26
    assert report.run.stats.clusters_new == 1


def test_load_vectors_is_the_bulk_form_of_get_vector(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    db_path = tmp_path / "vectors.sqlite"
    items = [_item(f"v{index}", "theverge") for index in range(5)]
    append_items(data_root, items)
    embed_items(data_root, HashBackend(dim=4), "hash", db_path)

    loaded = load_vectors([item.id for item in items] + ["missing"], db_path)
    assert set(loaded) == {item.id for item in items}
    assert all(len(vector) == 4 for vector in loaded.values())


def test_format_report_mentions_the_dropped_singletons(tmp_path: Path) -> None:
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)
    report = run_clustering(data_root, CONFIG, db_path, now=NOW)
    text = format_report(report, CONFIG)
    assert "dropped 19 singleton component(s)" in text
    assert "1 single-outlet" in text
    assert "borderline pair(s)" in text


# --- pending pairs (T22) ----------------------------------------------


def test_pending_pair_renders_both_members_and_score() -> None:
    a = ClusterItem.from_item(_item("e0", "theverge"))
    b = ClusterItem.from_item(_item("e1", "tomshardware"))
    pair = PendingPair(a=a, b=b, score=0.7231234567)
    rendered = render_pending_pair(pair)
    payload = json.loads(rendered)
    assert payload["a"] == a.to_dict()
    assert payload["b"] == b.to_dict()
    # Rounded, not truncated to whatever repr a float happens to have --
    # see PendingPair.to_dict.
    assert payload["score"] == 0.723123


def test_pending_pair_round_trips_through_json() -> None:
    a = ClusterItem.from_item(_item("e0", "theverge"))
    b = ClusterItem.from_item(_item("e1", "tomshardware"))
    pair = PendingPair(a=a, b=b, score=0.723123)
    assert pending_pair_from_dict(json.loads(render_pending_pair(pair))) == pair


def test_pending_pair_id_is_a_and_b_joined() -> None:
    a = ClusterItem.from_item(_item("e0", "theverge"))
    b = ClusterItem.from_item(_item("e1", "tomshardware"))
    pair = PendingPair(a=a, b=b, score=0.72)
    assert pair.pair_id == f"{a.item_id}-{b.item_id}"


def test_run_clustering_orders_pending_pair_members_by_id(tmp_path: Path) -> None:
    """`scan_pairs` always orders `a < b`; `run_clustering` must not
    scramble that when it denormalizes the pair for persistence."""
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)
    run_clustering(data_root, CONFIG, db_path, now=NOW)
    pair = load_pending_pairs(data_root)[0]
    assert pair.a.item_id < pair.b.item_id


def test_run_clustering_writes_the_borderline_pair(tmp_path: Path) -> None:
    """T22's seam: the borderline pair `nc cluster` reports is also
    persisted, denormalized, for `nc label` to read with no model."""
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)

    report = run_clustering(data_root, CONFIG, db_path, now=NOW)

    assert report.pending_pairs_written == 1
    pairs = load_pending_pairs(data_root)
    assert len(pairs) == 1
    pair = pairs[0]
    assert {pair.a.item_id, pair.b.item_id} == _ids("e0", "e1")
    assert pair.a.item_id < pair.b.item_id
    assert pytest.approx(pair.score, abs=1e-6) == 0.72
    # Denormalized: nc label needs nothing else to show a human this pair.
    outlets = {pair.a.outlet, pair.b.outlet}
    assert outlets == {"theverge", "tomshardware"}
    titles = {pair.a.title, pair.b.title}
    assert titles == {"e0 headline", "e1 headline"}
    path = pending_pair_path(data_root, pair)
    assert path.parent == data_root.resolve("pending-pairs")
    assert path.exists()


def test_pending_pairs_are_stable_across_a_no_op_rerun(tmp_path: Path) -> None:
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)
    run_clustering(data_root, CONFIG, db_path, now=NOW)
    before = _snapshot(data_root.path)

    second = run_clustering(data_root, CONFIG, db_path, now=NOW)

    assert second.pending_pairs_written == 0
    assert _snapshot(data_root.path) == before


def test_pending_pairs_are_not_deleted_when_the_window_moves_on(
    tmp_path: Path,
) -> None:
    """Ageing out drops a pair from `ClusterRun.borderline` (its items
    are no longer in the window), but the persisted file stays -- see
    `write_pending_pairs`'s docstring."""
    items, vectors = _synthetic_set()
    data_root, db_path = _prepare(tmp_path, items, vectors)
    run_clustering(data_root, CONFIG, db_path, now=NOW)
    before = load_pending_pairs(data_root)
    assert len(before) == 1

    later = run_clustering(
        data_root, CONFIG, db_path, now=datetime(2026, 9, 30, tzinfo=UTC)
    )

    assert later.run.stats.window_items == 0
    assert later.pending_pairs_written == 0
    assert load_pending_pairs(data_root) == before


def test_write_pending_pairs_only_rewrites_changed_files(tmp_path: Path) -> None:
    data_root = DataRoot(tmp_path / "data")
    a = ClusterItem.from_item(_item("e0", "theverge"))
    b = ClusterItem.from_item(_item("e1", "tomshardware"))
    pair = PendingPair(a=a, b=b, score=0.72)

    assert write_pending_pairs(data_root, [pair]) == 1
    assert write_pending_pairs(data_root, [pair]) == 0  # unchanged, not rewritten


# --- timing ---------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NC_BENCH") != "1",
    reason="benchmark; set NC_BENCH=1 to run it",
)
def test_five_thousand_items_in_the_window(tmp_path: Path) -> None:
    """T21's second acceptance criterion: under a minute for 5,000 items
    in the window. Measured and printed, never asserted -- a wall-clock
    assertion inside `make check` is a flaky test on a shared CI runner,
    and the number that matters is the one this prints.

    5,000 items is 12,497,500 pairs and every one of them is really
    compared: `scan_pairs` uses no blocking key and no approximate
    index, so there is nothing here for a shortcut to miss. Two numbers
    are reported: the algorithm alone, and the whole `nc cluster` run
    including reading the items from JSONL, the vectors from SQLite and
    writing the cluster and pending files.
    """
    count = 5000
    items: list[Item] = []
    vectors: dict[str, list[float]] = {}
    for index in range(count):
        item = _item(f"bench{index}", f"outlet{index % 10}")
        items.append(item)
        vectors[item.id] = _bench_vector(index)

    started = time.perf_counter()
    run = cluster_items(items, vectors, (), CONFIG)
    core = time.perf_counter() - started

    data_root, db_path = _prepare(tmp_path, items, vectors)
    started = time.perf_counter()
    report = run_clustering(data_root, CONFIG, db_path, now=NOW)
    end_to_end = time.perf_counter() - started

    print(
        f"\n5,000 items in the window ({count * (count - 1) // 2:,} pairs)"
        f"\n  cluster_items:  {core:.2f}s"
        f"\n  nc cluster:     {end_to_end:.2f}s (read, cluster, write)"
        f"\n  {run.stats.linked_pairs} linked, {run.stats.borderline_pairs} "
        f"borderline, {run.stats.clusters_new} clusters, "
        f"{report.written.clusters_written} files written"
    )


_BENCH_DIM = 256


def _bench_vector(index: int) -> list[float]:
    """A 256-dimensional vector (the order of a small static embedding
    model) placing every tenth item on the same story.

    Deterministically pseudo-random directions, not tidy one-hot axes:
    500 distinct story directions do not fit in 256 orthogonal axes, and
    the point of the benchmark is a realistic number of pairs clearing
    the threshold -- roughly 22,500 real links plus whatever two random
    directions happen to produce -- rather than a matmul over vectors so
    sparse that nothing matches.
    """
    story = index // 10
    center = _pseudo_random_unit(f"story{story}")
    private = _pseudo_random_unit(f"item{index}")
    raw = [
        math.sqrt(0.9) * a + math.sqrt(0.1) * b
        for a, b in zip(center, private, strict=True)
    ]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


def _pseudo_random_unit(seed: str) -> list[float]:
    digest = sha1(seed.encode("utf-8")).digest()
    values = [
        ((digest * (_BENCH_DIM // len(digest) + 2))[index] - 127.5) / 127.5
        + math.sin(index * 1.7 + digest[index % len(digest)])
        for index in range(_BENCH_DIM)
    ]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def test_cluster_item_from_item_keeps_the_fields_the_agent_quotes() -> None:
    item = _item("q0", "theverge")
    member = ClusterItem.from_item(item)
    assert member.to_dict() == {
        "item_id": item.id,
        "outlet": "theverge",
        "title": item.title,
        "lede": item.lede,
        "published": item.published,
    }
