"""T21: the four-day clustering window, union-find over linked pairs.

docs/ARCHITECTURE.md's Components table: "Cluster | nc.cluster |
vectors, window of recent items | `data/clusters/<date>/<id>.json`,
`data/pending/<id>.json`". The Clustering section fixes the algorithm:
a four-day window, cosine over the T20 vectors of `title + " " + lede`,
`tau_high` links, `[tau_low, tau_high)` is a borderline band judged by
the LLM backend (T24), below `tau_low` never links, connected
components are clusters, and a cluster is emitted only with two or more
distinct outlets.

===========================================================================
1. The cluster id: resolving a contradiction in docs/ARCHITECTURE.md
===========================================================================

docs/ARCHITECTURE.md asserts two things that cannot both hold:

  (a) Data model, Cluster: `id  <YYYY-MM-DD>-<6 hex of sorted item ids>`
  (b) Clustering: "Cluster ids are stable across runs; membership
      changes bump `version` and re-queue the cluster."

If the id is a hash of the whole membership, then the moment a story
picks up a fourth outlet the hash changes and the result is a *new*
cluster with a *new* id. `version` could then never leave 1, because
nothing persists from run to run to increment. That also breaks the
analysis contract (contract/analysis.schema.json), where an analysis is
keyed to `cluster_id` *and* `cluster_version` precisely so that a
membership change can invalidate it and re-queue the story.

**Resolution: the id is a function of the cluster's anchor, not of its
membership, and the emitted cluster files are the registry that keeps it
stable.**

  id = "<anchor's published date>-<first 6 hex of sha1(anchor item id)>"

The *anchor* is the earliest-published item in the component at the
moment the cluster is first emitted (ties broken by the lower item id).
It is chosen once, written into the cluster file as `anchor`, and never
recomputed: a later run reuses the stored anchor, so the id survives
every membership change. `version` is then meaningful, and increments
exactly when the member set changes.

Why an anchor rather than the two obvious alternatives:

- *Hash of the membership* (the literal reading of (a)) is what the
  contradiction is about; it makes `version` dead and orphans every
  analysis the first time an outlet is late to a story -- which is the
  normal case, not the exception.
- *A separate registry file* (say `clusters/index.json` mapping ids to
  members) would work, but it is a second source of truth that has to be
  kept consistent with the cluster files, and it rewrites on every run --
  a `data:` commit every three hours forever, which is exactly the churn
  T12's acceptance criterion exists to prevent. The cluster files
  already are the registry; this module reads them back.

What is given up, deliberately and visibly: the id's date prefix is the
date of the *anchor*, which is the earliest item known when the cluster
was born, not necessarily the earliest item it will ever contain. A feed
that back-dates an item can therefore add a member published before the
date in the id. The true earliest item is always in the file's `items`
list, and the site (T40) should order by that, not by the id prefix.
Stability was preferred over a cosmetically exact prefix because the
analysis contract depends on the first and not at all on the second.

`anchor` is one field more than the four in docs/ARCHITECTURE.md's
Cluster model (`id`, `version`, `items`, `status`). It is the price of
the resolution above and it is additive: nothing that reads the four
documented fields is affected. The owner has to ratify the Cluster model
either way, because the contradiction is in the document, not here.

Collisions: contract/analysis.schema.json pins `cluster_id` to
`^\\d{4}-\\d{2}-\\d{2}-[0-9a-f]{6}$`, so six hex digits are not
negotiable. Six hex is 16.7M values and ids are namespaced by date, so
at roughly fifty clusters a day the chance of two anchors colliding
inside one date is about 7e-5 per day -- small but not zero over years.
`_allocate_id` therefore probes deterministically (`sha1(anchor \\x00
n)`) until it finds an id unused *in that date*, which it can do because
every existing cluster file is loaded anyway.

===========================================================================
2. Membership is monotonic. That is what makes merge, split and
   ageing-out tractable
===========================================================================

The window decides which items are *candidates for new links*. It does
not decide what a cluster contains. Once an item is a member of an
emitted cluster it stays a member forever: every run seeds its union-find
with the memberships already on disk, then adds the new links found in
the window.

Three consequences, each of which is one of the ways a clustering scheme
usually breaks:

**Ageing out.** A cluster whose items have all fallen out of the
four-day window simply stops growing. Its membership does not shrink, so
its `version` does not change, so its file is not rewritten and the
analysis written against it stays valid indefinitely. Without
monotonicity, the window sliding forward by three hours would silently
drop the oldest member of a live cluster, bump its version, re-queue it
and bill a re-analysis -- eight times a day, forever, for no new
information. `test_ageing_out_of_the_window_changes_nothing` pins this.

**Merge.** A new item can link two previously separate clusters. Both
may already have analyses. The rule: the surviving cluster is the one
with the earlier anchor (ties broken by the lower id) -- a property of
the stored files, so the outcome does not depend on the order components
are visited or on which run notices the merge. The survivor takes the
whole component as its membership, bumps `version` and is re-queued. The
loser is *not* deleted: its file is kept exactly as it was and gains
`superseded_by: <survivor id>`, and its pending file is removed. Its
analysis stays in `analyses/` and becomes history.

  Consumers must therefore treat an analysis as current only when its
  `cluster_id` names a cluster whose status is not `superseded`
  *and* its
  `cluster_version` equals that cluster's current `version`. T30's
  validator and T40's site builder both need this rule; it is stated
  here because this module is what creates the situation.

  `superseded_by` names the survivor. The owner ratified a fourth
  `status` value on #38, so supersession is now both: the status
  marks it, the field says which cluster absorbed it. That changed
  the documented
  `pending | analyzed | rejected` enum, which is the owner's to ratify,
  so this module stays inside the documented enum and adds a field
  beside it.

**Split.** Monotonic membership means a cluster cannot split on its own:
not when the window moves, not when a borderline pair is judged, not
when T23 retunes the thresholds (existing memberships are seeded from
disk before any similarity is looked at, so a stricter `tau_high` only
affects links not yet made). The only way to split a cluster is to
recompute from scratch, which `cluster_items` supports by being handed
`existing=()` -- every id, version and analysis association is then
reassigned. That is a destructive operation on data an analysis may
point at, so it is deliberately not a flag on `nc cluster`: it is the
owner's decision to make once, after T23's tuning, by clearing
`clusters/` and `pending/` in the data root.

===========================================================================
3. Idempotence
===========================================================================

`nc cluster` runs every three hours (.github/workflows/ingest.yml) and
its output is committed to the data repository, so a run that finds
nothing new must write nothing at all -- not a reordered key, not a
re-stamped timestamp. This module has no timestamps in its output, sorts
every collection it serializes, and compares the rendered bytes against
what is on disk before writing. Hence also: no `generated_at` in a
cluster file, ever.

The pending file mirrors `status`: `pending/<id>.json` exists exactly
when the cluster's `status` is `pending` and it has not been superseded,
and it holds the same bytes as the cluster file (the analyze-clusters
skill reads `id`, `version` and `items` straight out of it). To take a
cluster off the queue, a later step (T30's `nc validate`) sets `status`
in the cluster file; the next `nc cluster` run removes the pending file.

===========================================================================
4. Cost
===========================================================================

Pair finding is a single chunked numpy matmul over L2-normalized
vectors, not a Python double loop: 5,000 items in the window is 12.5M
pairs, which BLAS does in a fraction of a second and a Python loop does
not do in a minute. No blocking, no approximation, no ANN index --
every pair in the window really is compared, so there is nothing for a
blocking key to miss. Peak memory is bounded by the chunk (`_BLOCK`
rows x n columns), not by the full n x n matrix.

Measured for T21's acceptance criterion, 5,000 items of 256 dimensions
in 500 stories of 10 outlets, on this sandbox: 0.35s for
`cluster_items`, 0.85s for a whole `nc cluster` run including reading
the items from JSONL, the vectors from SQLite and writing 500 cluster
and 500 pending files. See
`tests/test_cluster.py::test_five_thousand_items_in_the_window`
(`NC_BENCH=1`), which prints those numbers and asserts nothing about
them -- a wall-clock assertion in `make check` is a flaky test.

The one thing that is not constant is turning matched cells into `Pair`
objects, which is per-hit Python. At the configured `tau_low` that is
tens of thousands of hits and invisible (0.30s of the 0.35s above is
the matmul). Measured with a deliberately absurd `tau_low` of 0.10 on
the same 5,000 items -- 2.8M borderline pairs -- it is 9.9s, still
inside the minute. `tau_low` is what bounds this, which is one more
reason T23 should not set it near zero.

Existing clusters are read in full on every run (`load_clusters` globs
`clusters/*/*.json`). At the project's volume -- roughly fifty clusters
a night, a couple of kilobytes each -- that is tens of megabytes a year
and a few seconds; the fix when it stops being cheap is an item-id ->
cluster-id index in `.cache/` rebuilt from those files, like
`.cache/nc.sqlite`, not a committed index file.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from pathlib import Path

import numpy as np
import numpy.typing as npt
import yaml

from nc.embed import DEFAULT_VECTORS_DB_PATH, load_vectors
from nc.feeds import Item
from nc.store import DataRoot, read_items_since

DEFAULT_CLUSTER_CONFIG_PATH = Path("config/cluster.yaml")

# nc.feeds._iso_utc's format, the one every `published` is written in.
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Rows per similarity chunk. 512 x 5,000 float32 is ~10MB, so peak
# memory stays flat while BLAS still gets matrices big enough to be
# worth the call.
_BLOCK = 512

# Deterministic probes when two anchors hash to the same six hex digits
# inside one date. See the module docstring, "Collisions".
_MAX_ID_ATTEMPTS = 1000

STATUS_PENDING = "pending"
# Ratified by the owner on #38: supersession is a status value, not
# only a field. `superseded_by` stays alongside it, because the status
# says a cluster was absorbed and the field says which cluster took
# it; a reader needs both to follow the chain.
STATUS_SUPERSEDED = "superseded"


# --- config ---------------------------------------------------------------


@dataclass(frozen=True)
class ClusterConfig:
    tau_low: float
    tau_high: float
    window_days: int
    min_outlets: int


def load_cluster_config(path: Path = DEFAULT_CLUSTER_CONFIG_PATH) -> ClusterConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    config = ClusterConfig(
        tau_low=float(raw["tau_low"]),
        tau_high=float(raw["tau_high"]),
        window_days=int(raw["window_days"]),
        min_outlets=int(raw["min_outlets"]),
    )
    if not 0.0 <= config.tau_low <= config.tau_high <= 1.0:
        raise ValueError(
            f"{path}: expected 0 <= tau_low <= tau_high <= 1, got "
            f"tau_low={config.tau_low}, tau_high={config.tau_high}"
        )
    if config.window_days < 1:
        raise ValueError(f"{path}: window_days must be >= 1, got {config.window_days}")
    if config.min_outlets < 1:
        raise ValueError(f"{path}: min_outlets must be >= 1, got {config.min_outlets}")
    return config


# --- the cluster record ---------------------------------------------------


@dataclass(frozen=True)
class ClusterItem:
    """One member of a cluster, as docs/ARCHITECTURE.md's Cluster model
    describes `items`: "item ids with outlet, title, lede, published"."""

    item_id: str
    outlet: str
    title: str
    lede: str
    published: str

    @classmethod
    def from_item(cls, item: Item) -> ClusterItem:
        return cls(
            item_id=item.id,
            outlet=item.outlet,
            title=item.title,
            lede=item.lede,
            published=item.published,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "outlet": self.outlet,
            "title": self.title,
            "lede": self.lede,
            "published": self.published,
        }


@dataclass(frozen=True)
class Cluster:
    """One story. docs/ARCHITECTURE.md's Cluster model, plus `anchor`
    and `superseded_by` -- see this module's docstring for why both
    exist and why neither could be left out."""

    id: str
    version: int
    anchor: str
    status: str
    items: tuple[ClusterItem, ...]
    superseded_by: str | None = None

    @property
    def date(self) -> str:
        """The id's date prefix, which is also the directory it lives in."""
        return self.id[:10]

    @property
    def item_ids(self) -> frozenset[str]:
        return frozenset(item.item_id for item in self.items)

    @property
    def outlets(self) -> frozenset[str]:
        return frozenset(item.outlet for item in self.items)

    @property
    def anchor_published(self) -> str:
        """The anchor member's `published`, used to order merge
        candidates. Falls back to the id's date prefix if the anchor is
        somehow not in `items`, which keeps merge ordering total even
        for a hand-edited file."""
        for item in self.items:
            if item.item_id == self.anchor:
                return item.published
        return self.date

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "version": self.version,
            "anchor": self.anchor,
            "status": self.status,
            "items": [item.to_dict() for item in self.items],
        }
        if self.superseded_by is not None:
            payload["superseded_by"] = self.superseded_by
        return payload


def cluster_from_dict(raw: Mapping[str, object]) -> Cluster:
    items_raw = raw["items"]
    assert isinstance(items_raw, list)
    items = tuple(
        ClusterItem(
            item_id=str(entry["item_id"]),
            outlet=str(entry["outlet"]),
            title=str(entry["title"]),
            lede=str(entry["lede"]),
            published=str(entry["published"]),
        )
        for entry in items_raw
    )
    superseded_by = raw.get("superseded_by")
    return Cluster(
        id=str(raw["id"]),
        version=int(str(raw["version"])),
        anchor=str(raw["anchor"]),
        status=str(raw["status"]),
        items=items,
        superseded_by=None if superseded_by is None else str(superseded_by),
    )


def render(cluster: Cluster) -> str:
    """The exact bytes of a cluster or pending file.

    `indent=2` because these files are committed to the data repository
    and read by an agent; `sort_keys=True` and the callers' sorted
    collections because the same cluster must render to the same bytes
    on every run (see the module docstring, "Idempotence")."""
    return (
        json.dumps(cluster.to_dict(), indent=2, sort_keys=True, ensure_ascii=True)
        + "\n"
    )


# --- union-find -----------------------------------------------------------


class UnionFind:
    """Union by size with path compression, keyed by item id."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._size: dict[str, int] = {}

    def add(self, key: str) -> None:
        if key not in self._parent:
            self._parent[key] = key
            self._size[key] = 1

    def find(self, key: str) -> str:
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self._size[left_root] < self._size[right_root]:
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root
        self._size[left_root] += self._size[right_root]

    def components(self) -> list[list[str]]:
        """Every component, each member list sorted, components ordered
        by their first member -- so the caller's iteration order never
        depends on dict insertion order."""
        groups: dict[str, list[str]] = {}
        for key in self._parent:
            groups.setdefault(self.find(key), []).append(key)
        return sorted(sorted(members) for members in groups.values())


# --- similarity -----------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    """Two item ids and their cosine similarity, `a` < `b`."""

    a: str
    b: str
    score: float


@dataclass(frozen=True)
class PairScan:
    linked: tuple[Pair, ...]
    borderline: tuple[Pair, ...]
    compared: int
    skipped_dim_mismatch: int


def scan_pairs(
    item_ids: Sequence[str],
    vectors: Mapping[str, Sequence[float]],
    tau_low: float,
    tau_high: float,
) -> PairScan:
    """Every pair at or above `tau_low`, split at `tau_high`.

    Exhaustive: no blocking key, no approximate index, so nothing is
    missed by construction. The cost is one chunked matmul -- see the
    module docstring, "Cost".

    Items without a stored vector are skipped (T20's `nc embed` has not
    seen them yet); so are vectors of a dimensionality other than the
    majority one, which is what a half-finished model change looks like
    in `.cache/vectors.sqlite`. Both are counted, never silently
    dropped.
    """
    present = [item_id for item_id in item_ids if item_id in vectors]
    dims: dict[int, int] = {}
    for item_id in present:
        dim = len(vectors[item_id])
        dims[dim] = dims.get(dim, 0) + 1
    if not dims:
        return PairScan((), (), 0, 0)
    # Ties broken by the larger dimensionality so the choice does not
    # depend on dict order.
    majority_dim = max(dims, key=lambda dim: (dims[dim], dim))
    usable = [item_id for item_id in present if len(vectors[item_id]) == majority_dim]
    skipped = len(present) - len(usable)

    count = len(usable)
    if count < 2:
        return PairScan((), (), count, skipped)

    matrix: npt.NDArray[np.float32] = np.asarray(
        [vectors[item_id] for item_id in usable], dtype=np.float32
    )
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # A zero vector has no direction; giving it norm 1 leaves it at
    # cosine 0 against everything rather than producing a NaN that
    # would compare false against both thresholds in a way nobody
    # could explain later.
    norms[norms == 0] = 1.0
    matrix = matrix / norms

    columns = np.arange(count)
    linked: list[Pair] = []
    borderline: list[Pair] = []
    for start in range(0, count, _BLOCK):
        stop = min(start + _BLOCK, count)
        sims = matrix[start:stop] @ matrix.T
        # Upper triangle only: each pair once, and never an item with
        # itself.
        upper = columns[None, :] > np.arange(start, stop)[:, None]
        for target, low, high in (
            (linked, tau_high, None),
            (borderline, tau_low, tau_high),
        ):
            mask = upper & (sims >= low)
            if high is not None:
                mask &= sims < high
            rows, cols = np.nonzero(mask)
            for row, col in zip(rows.tolist(), cols.tolist(), strict=True):
                left, right = usable[start + row], usable[col]
                if left > right:
                    left, right = right, left
                target.append(Pair(left, right, float(sims[row, col])))

    linked.sort(key=lambda pair: (-pair.score, pair.a, pair.b))
    borderline.sort(key=lambda pair: (-pair.score, pair.a, pair.b))
    return PairScan(tuple(linked), tuple(borderline), count, skipped)


# --- ids ------------------------------------------------------------------


def _allocate_id(anchor_item_id: str, anchor_published: str, taken: set[str]) -> str:
    """`<anchor date>-<6 hex>`, unique among `taken`.

    Six hex digits are fixed by contract/analysis.schema.json's
    `cluster_id` pattern, so uniqueness is reached by probing rather
    than by widening the id."""
    date = anchor_published[:10]
    for attempt in range(_MAX_ID_ATTEMPTS):
        seed = anchor_item_id if attempt == 0 else f"{anchor_item_id}\x00{attempt}"
        candidate = f"{date}-{sha1(seed.encode('utf-8')).hexdigest()[:6]}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(
        f"could not allocate a cluster id for anchor {anchor_item_id} on {date} "
        f"after {_MAX_ID_ATTEMPTS} attempts"
    )


# --- the run --------------------------------------------------------------


@dataclass(frozen=True)
class ClusterStats:
    window_items: int
    items_without_vectors: int
    items_skipped_dim_mismatch: int
    linked_pairs: int
    borderline_pairs: int
    components: int
    clusters_total: int
    clusters_new: int
    clusters_requeued: int
    clusters_superseded: int
    dropped_single_item: int
    dropped_single_outlet: int

    @property
    def dropped_total(self) -> int:
        """What T61's weekly quality page calls "singletons dropped":
        components that never became a cluster because only one outlet
        ran the story. A jump here means the thresholds or the outlet
        set drifted."""
        return self.dropped_single_item + self.dropped_single_outlet


@dataclass(frozen=True)
class ClusterRun:
    clusters: tuple[Cluster, ...]
    superseded: tuple[Cluster, ...]
    borderline: tuple[Pair, ...]
    stats: ClusterStats


def cluster_items(
    items: Sequence[Item],
    vectors: Mapping[str, Sequence[float]],
    existing: Sequence[Cluster],
    config: ClusterConfig,
    extra_links: Iterable[tuple[str, str]] = (),
) -> ClusterRun:
    """Cluster one window of items against the clusters already on disk.

    Pure: no filesystem, no clock, no network. `existing` is what makes
    ids stable and `version` meaningful; passing `()` is the
    recompute-from-scratch path described in the module docstring.

    `extra_links` is the seam T24 plugs into: pairs its judge accepted
    from the borderline band, linked here exactly as a `tau_high` pair
    is. Nothing else about this function has to know they exist. The
    matching output seam is `ClusterRun.borderline`, the pairs T24 has
    to judge.
    """
    live = [cluster for cluster in existing if cluster.status != STATUS_SUPERSEDED]

    known: dict[str, ClusterItem] = {}
    for cluster in existing:
        for member in cluster.items:
            known.setdefault(member.item_id, member)
    for item in items:
        known[item.id] = ClusterItem.from_item(item)

    union = UnionFind()
    window_ids = sorted(item.id for item in items)
    for item_id in window_ids:
        union.add(item_id)
    # Seed with what is already on disk *before* any similarity is
    # looked at: this is what makes membership monotonic (module
    # docstring, section 2).
    for cluster in live:
        members = sorted(cluster.item_ids)
        for member_id in members[1:]:
            union.union(members[0], member_id)

    scan = scan_pairs(window_ids, vectors, config.tau_low, config.tau_high)
    for pair in scan.linked:
        union.union(pair.a, pair.b)
    extra = 0
    for left, right in extra_links:
        union.union(left, right)
        extra += 1

    by_item: dict[str, Cluster] = {}
    for cluster in live:
        for member in cluster.items:
            by_item[member.item_id] = cluster
    taken = {cluster.id for cluster in existing}

    emitted: list[Cluster] = []
    superseded: list[Cluster] = []
    new_count = 0
    requeued = 0
    dropped_single_item = 0
    dropped_single_outlet = 0
    components = union.components()

    for members in components:
        matched = sorted(
            {
                cluster.id: cluster for cluster in map(by_item.get, members) if cluster
            }.values(),
            key=lambda cluster: (cluster.anchor_published, cluster.id),
        )
        member_items = tuple(
            sorted(
                (known[member] for member in members if member in known),
                key=lambda item: (item.published, item.item_id),
            )
        )
        if not member_items:
            # Only reachable through `extra_links` naming an item that
            # is neither in the window nor in any cluster file. There is
            # nothing to cluster and nothing to report about it.
            continue
        if not matched:
            outlets = {item.outlet for item in member_items}
            if len(outlets) < config.min_outlets:
                if len(member_items) < 2:
                    dropped_single_item += 1
                else:
                    dropped_single_outlet += 1
                continue
            anchor = member_items[0]
            cluster_id = _allocate_id(anchor.item_id, anchor.published, taken)
            taken.add(cluster_id)
            emitted.append(
                Cluster(
                    id=cluster_id,
                    version=1,
                    anchor=anchor.item_id,
                    status=STATUS_PENDING,
                    items=member_items,
                )
            )
            new_count += 1
            continue

        winner, losers = matched[0], matched[1:]
        changed = winner.item_ids != frozenset(item.item_id for item in member_items)
        if changed or losers:
            emitted.append(
                replace(
                    winner,
                    version=winner.version + 1,
                    status=STATUS_PENDING,
                    items=member_items,
                )
            )
            requeued += 1
        else:
            emitted.append(winner)
        for loser in losers:
            superseded.append(
                replace(loser, status=STATUS_SUPERSEDED, superseded_by=winner.id)
            )

    emitted.sort(key=lambda cluster: cluster.id)
    superseded.sort(key=lambda cluster: cluster.id)
    stats = ClusterStats(
        window_items=len(items),
        items_without_vectors=len(items) - scan.compared - scan.skipped_dim_mismatch,
        items_skipped_dim_mismatch=scan.skipped_dim_mismatch,
        linked_pairs=len(scan.linked) + extra,
        borderline_pairs=len(scan.borderline),
        components=len(components),
        clusters_total=len(emitted),
        clusters_new=new_count,
        clusters_requeued=requeued,
        clusters_superseded=len(superseded),
        dropped_single_item=dropped_single_item,
        dropped_single_outlet=dropped_single_outlet,
    )
    return ClusterRun(tuple(emitted), tuple(superseded), scan.borderline, stats)


# --- the data root --------------------------------------------------------


def load_clusters(data_root: DataRoot) -> list[Cluster]:
    """Every cluster file in the data root, in id order."""
    clusters_dir = data_root.resolve("clusters")
    if not clusters_dir.exists():
        return []
    clusters = [
        cluster_from_dict(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(clusters_dir.glob("*/*.json"))
    ]
    clusters.sort(key=lambda cluster: cluster.id)
    return clusters


def cluster_path(data_root: DataRoot, cluster: Cluster) -> Path:
    return data_root.resolve("clusters", cluster.date, f"{cluster.id}.json")


def pending_path(data_root: DataRoot, cluster_id: str) -> Path:
    return data_root.resolve("pending", f"{cluster_id}.json")


def _write_if_changed(path: Path, text: str) -> bool:
    """Write only when the bytes would differ. The whole idempotence
    story (module docstring, section 3) comes down to this."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


@dataclass(frozen=True)
class WriteResult:
    clusters_written: int
    pending_written: int
    pending_removed: int


def write_run(data_root: DataRoot, run: ClusterRun) -> WriteResult:
    """Write the run's clusters and mirror the pending queue onto them."""
    clusters_written = 0
    pending_written = 0
    pending_removed = 0

    for cluster in (*run.clusters, *run.superseded):
        text = render(cluster)
        if _write_if_changed(cluster_path(data_root, cluster), text):
            clusters_written += 1
        path = pending_path(data_root, cluster.id)
        wants_pending = cluster.status == STATUS_PENDING
        if wants_pending:
            if _write_if_changed(path, text):
                pending_written += 1
        elif path.exists():
            path.unlink()
            pending_removed += 1

    return WriteResult(clusters_written, pending_written, pending_removed)


@dataclass(frozen=True)
class RunReport:
    cutoff: str
    run: ClusterRun
    written: WriteResult


def run_clustering(
    data_root: DataRoot,
    config: ClusterConfig,
    db_path: Path = DEFAULT_VECTORS_DB_PATH,
    now: datetime | None = None,
    extra_links: Iterable[tuple[str, str]] = (),
) -> RunReport:
    """`nc cluster`: window, link, emit, write.

    `now` is injected rather than read from the clock inside the
    algorithm so the window is reproducible in a test and in a backfill.
    """
    moment = datetime.now(UTC) if now is None else now
    cutoff = (moment - timedelta(days=config.window_days)).strftime(_TIME_FORMAT)

    items = [
        item
        for item in read_items_since(data_root, cutoff[:10])
        if item.published >= cutoff
    ]
    existing = load_clusters(data_root)
    vectors = load_vectors([item.id for item in items], db_path)
    run = cluster_items(items, vectors, existing, config, extra_links)
    written = write_run(data_root, run)
    return RunReport(cutoff=cutoff, run=run, written=written)


def format_report(report: RunReport, config: ClusterConfig) -> str:
    """The `nc cluster` summary. The dropped counts are what T61's
    weekly cluster-quality page reads."""
    stats = report.run.stats
    return "\n".join(
        (
            f"cluster: window since {report.cutoff} ({config.window_days} days), "
            f"{stats.window_items} item(s), "
            f"{stats.items_without_vectors} without a vector, "
            f"{stats.items_skipped_dim_mismatch} with a stale vector size",
            f"cluster: {stats.linked_pairs} linked pair(s), "
            f"{stats.borderline_pairs} borderline pair(s) in "
            f"[{config.tau_low}, {config.tau_high}) awaiting T24",
            f"cluster: {stats.components} component(s) -> "
            f"{stats.clusters_total} cluster(s): {stats.clusters_new} new, "
            f"{stats.clusters_requeued} re-queued, "
            f"{stats.clusters_superseded} superseded by a merge",
            f"cluster: dropped {stats.dropped_total} singleton component(s) "
            f"({stats.dropped_single_item} single-item, "
            f"{stats.dropped_single_outlet} single-outlet)",
            f"cluster: wrote {report.written.clusters_written} cluster file(s), "
            f"{report.written.pending_written} pending file(s), removed "
            f"{report.written.pending_removed} pending file(s)",
        )
    )
