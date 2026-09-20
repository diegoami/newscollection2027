"""T20: local sentence-embedding model, vectors cached under `.cache/`.

docs/ARCHITECTURE.md's Components table: "Embed | nc.embed | title +
lede | vectors in SQLite (not committed)". The Clustering section gives
the exact input: `title + " " + lede`.

**Library choice, and why (a cost decision, not just a quality one).**
docs/ARCHITECTURE.md's Cost model requires ingest and clustering to stay
free on a GitHub Actions runner, and that workflow (docs/PLAN.md T13)
fires every three hours -- 8 times a day. `sentence-transformers` was
rejected on that basis alone: it pulls in `torch`, which is hundreds of
MB to over a GB, downloaded and unpacked on every cold `actions/cache`
miss for a job that runs that often. `fastembed` (ONNX-runtime based)
was considered next and rejected too: measured from PyPI metadata for
this task, its dependency tree still requires `onnxruntime` (a compiled
binary on the same order of size as this module's entire dependency
tree) plus `huggingface-hub` and `pillow` for image models this project
never uses.

This module uses `model2vec` (MinishLab) instead: a *static* (non-
transformer) sentence-embedding library -- one of the two lighter
approaches T20's brief names ("ONNX-runtime based, or static-embedding
approaches"). Its base install (`pip install model2vec`, no extras) is
numpy + tokenizers + safetensors + huggingface_hub (pulled in
transitively by tokenizers) + joblib + jinja2 + tqdm: measured for this
task at roughly 90-100MB total in a fresh virtualenv, against
sentence-transformers+torch's several hundred MB to 1GB+. No torch, no
onnxruntime, and nothing in `model2vec`'s base install reaches the
network at import time (only `StaticModel.from_pretrained(...)`, called
lazily below, does). Its inference path is pure numpy: tokenize, look
up each token's static vector, mean-pool -- no attention, no dropout, no
GPU -- so encoding one input is a fixed sequence of floating-point
operations with nothing to make two runs diverge, which is exactly T20's
AC ("same input gives the same vector across runs").

Quality tradeoff, accepted deliberately: a static embedding model scores
below a full transformer encoder on general STS/retrieval benchmarks.
This project clusters near-duplicate short headlines across outlets,
not open-domain semantic search, and T22/T23 tune `tau_low`/`tau_high`
against whichever backend is actually running -- a lower but consistent
embedding quality is absorbed by threshold tuning, not by reaching for a
heavier model on a job that runs 8 times a day for free.

**That last sentence was measured at T23 and it is wrong.** Threshold
tuning can only absorb weak embeddings when the same-story and
different-story pairs are separable by *some* cutoff. On the 174
labelled pairs gathered so far they are not: a different-story pair
(ZDNet/Wired, two iPhone buying-advice pieces) scored 0.7992 while true
matches ran down to 0.5708, so no threshold links the true pairs
without also linking that false one. The model conflates topic
with event, which is what a mean-pooled static vector is expected to do
and what tuning cannot undo. The cost of being wrong here is bounded --
`tau_high` can be set where precision is perfect and everything
ambiguous goes to T24's judge -- but the claim should not be read as
still standing. `nc.bench` (`nc bench-embed`) now measures candidate
models against those labels instead of arguing about it; see
docs/CLUSTERING.md.

**Backend injection.** This sandbox has no egress to huggingface.co (or
hf.co, or the LFS CDN), so the real model cannot be downloaded or run
here -- only PyPI is reachable, which is enough to add `model2vec` as a
dependency and import it, but not to fetch model weights. `nc embed`'s
storage/caching/idempotence logic (this module) is therefore built
against the small `EmbeddingBackend` protocol below, with two
implementations: `Model2VecBackend` (the real one, used by `nc embed`)
and `HashBackend` (a deterministic hash-based fake used only in tests,
see tests/test_embed.py). Everything except the real model's own output
is proven locally against `HashBackend`, with no network. The real
model's determinism -- the actual, load-bearing AC -- is proven
separately by `.github/workflows/embed-check.yml`, which has real
network access and runs the real model in two separate process
invocations.

**Vectors vs. `nc db rebuild` (T12).** `nc.store.rebuild_db` drops and
recreates `.cache/nc.sqlite` from the JSONL files on every call -- by
design, since that cache holds no state of its own. Vectors are *not*
derivable from the JSONL (they come from a model, not from the text
alone reversibly) and are expensive to recompute, so this module
deliberately stores them in their own SQLite file
(`.cache/vectors.sqlite`, `DEFAULT_VECTORS_DB_PATH` below), never inside
`nc.sqlite`. `rebuild_db` therefore cannot touch them even by accident --
there is no shared table, no shared connection, no shared file --
instead of relying on `rebuild_db` being careful about a table it also
owns. tests/test_embed.py's
`test_db_rebuild_does_not_touch_the_vectors_db` proves this holds: embed
some items, rebuild `nc.sqlite`, assert the vectors are still there.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import yaml

from nc.feeds import Item
from nc.store import DataRoot, latest_by_id, read_items

if TYPE_CHECKING:
    from model2vec import StaticModel

DEFAULT_EMBED_CONFIG_PATH = Path("config/embed.yaml")

# Deliberately not in config/embed.yaml, mirroring nc.store's
# DEFAULT_DB_PATH: a cache file path is a code constant with a CLI
# override (`--db`), not a "model id or threshold" per CLAUDE.md.
DEFAULT_VECTORS_DB_PATH = Path(".cache/vectors.sqlite")


@dataclass(frozen=True)
class EmbedConfig:
    model_id: str
    model_dir: Path
    batch_size: int


def load_embed_config(path: Path = DEFAULT_EMBED_CONFIG_PATH) -> EmbedConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return EmbedConfig(
        model_id=str(raw["model_id"]),
        model_dir=Path(str(raw["model_dir"])),
        batch_size=int(raw["batch_size"]),
    )


def embed_text(item: Item) -> str:
    """The text embedded for one item. docs/ARCHITECTURE.md, Clustering:
    'cosine over embeddings of `title + " " + lede`'."""
    return f"{item.title} {item.lede}"


class EmbeddingBackend(Protocol):
    """Turns a batch of texts into fixed-length float vectors.

    The only contract this module relies on: same text in, same vector
    out, every call -- in this process and in a fresh one. That
    determinism is T20's actual acceptance criterion; `embed_items`'s
    storage/caching/idempotence logic (tested against `HashBackend`)
    does not care which implementation provides it.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashBackend:
    """Deterministic, network-free fake `EmbeddingBackend`, for tests.

    Not a real embedding -- similar texts do not get similar vectors,
    there is no learned structure at all -- but it is exactly
    reproducible from the input text via a cryptographic hash, which is
    all the storage/idempotence logic in this module needs to exercise.
    Per T20's brief: this sandbox has no egress to Hugging Face, so
    everything except the real model's own output is proven against
    this instead.
    """

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        needed = self.dim * 4
        raw = (digest * (needed // len(digest) + 1))[:needed]
        # Each float derives from 4 hash bytes scaled into [-1, 1) --
        # plain, deterministic arithmetic, no randomness anywhere.
        return [
            (int.from_bytes(raw[i : i + 4], "big") / 2**32) * 2 - 1
            for i in range(0, needed, 4)
        ]


class Model2VecBackend:
    """The real embedding model. See this module's docstring for why
    `model2vec` and not `sentence-transformers` or `fastembed`.

    `model2vec` is imported lazily, inside `_load()`, not at module
    import time, so every test and every other command that never
    constructs this class never pays for the import -- and so that
    `HF_HOME` (see `_load`) is set before `huggingface_hub` (imported by
    `model2vec`) is first touched in this process.
    """

    def __init__(self, config: EmbedConfig) -> None:
        self._config = config
        self._model: StaticModel | None = None

    def _load(self) -> StaticModel:
        if self._model is None:
            # huggingface_hub reads HF_HOME to compute its cache
            # directory the first time it is imported, so this must run
            # before the `from model2vec import ...` line below, not
            # after. Setting it here (rather than relying on the
            # ambient $HOME) keeps the cache under this project's own
            # config (config/embed.yaml's model_dir) so
            # .github/workflows/embed-check.yml can cache exactly that
            # directory with actions/cache, per docs/WORKFLOW.md's "The
            # embedding model is cached between runs with
            # actions/cache."
            self._config.model_dir.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("HF_HOME", str(self._config.model_dir))

            from model2vec import StaticModel

            # force_download=False: model2vec's own default is True
            # (see StaticModel.from_pretrained), which would hit
            # huggingface.co on every single call regardless of the
            # cache -- defeating the entire point of caching model_dir
            # between runs on a workflow that fires every three hours.
            self._model = StaticModel.from_pretrained(
                self._config.model_id, force_download=False
            )
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(list(texts), batch_size=self._config.batch_size)
        return [[float(value) for value in row] for row in vectors]


# --- storage: vectors keyed by item id, in their own SQLite file -----------
#
# A separate file from nc.sqlite (nc.store.DEFAULT_DB_PATH) on purpose --
# see the module docstring, "Vectors vs. nc db rebuild".

# The key is (item_id, model_id), not item_id alone. A vector is the
# output of a *particular* model, so two models' vectors for one item
# are two different rows, and every read names the model it wants.
#
# This is not hypothetical tidiness. With item_id alone as the key,
# changing `model_id` in config/embed.yaml and running `nc embed` left
# every existing item on the OLD model's vector (`embed_items` skipped
# it as already stored) while new items got the new model's, and
# `load_vectors` returned the mixture unfiltered. `nc.cluster` then
# computed cosine between vectors from two different models -- a number
# with no meaning -- and the only thing that could notice was its
# "stale vector size" count, which fires only when the two models
# happen to differ in dimensionality. Two same-dimension models blended
# silently and produced plausible-looking scores.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    item_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (item_id, model_id)
)
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the vector cache, recreating it if it predates the
    (item_id, model_id) key.

    `CREATE TABLE IF NOT EXISTS` cannot widen a primary key, so a
    database written by an older build would keep the single-column key
    and the bug above. This file is a rebuildable cache under `.cache/`,
    never committed and never the record of anything (module docstring,
    "Vectors vs. nc db rebuild"), so the migration is simply to drop it
    and let the next `nc embed` refill it. The cost is one re-embed of
    the window; the alternative is carrying a schema that makes two
    models' vectors indistinguishable.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'vectors'"
    ).fetchone()
    if row is not None and "PRIMARY KEY (item_id, model_id)" not in str(row[0]):
        conn.execute("DROP TABLE vectors")
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def _pack(vector: Sequence[float]) -> bytes:
    # float64 (8 bytes/value): a lossless round trip of whatever
    # precision the backend hands us, not just whatever the model's own
    # native dtype happens to be. Vector dimensionality for a small
    # local model is in the low hundreds at most, so the extra bytes
    # over float32 are immaterial next to what they buy: an exact
    # round trip that a "stores floats correctly" test can assert on
    # with `==`, not `pytest.approx`.
    return struct.pack(f"{len(vector)}d", *vector)


def _unpack(dim: int, blob: bytes) -> list[float]:
    return list(struct.unpack(f"{dim}d", blob))


def stored_item_ids(model_id: str, db_path: Path = DEFAULT_VECTORS_DB_PATH) -> set[str]:
    """Every item id that already has a stored vector *from this model*."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT item_id FROM vectors WHERE model_id = ?", (model_id,)
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def get_vector(
    item_id: str, model_id: str, db_path: Path = DEFAULT_VECTORS_DB_PATH
) -> list[float] | None:
    """The stored vector for `item_id` under `model_id`, or None."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT dim, vector FROM vectors WHERE item_id = ? AND model_id = ?",
            (item_id, model_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    dim, blob = row
    return _unpack(dim, blob)


def load_vectors(
    item_ids: Sequence[str],
    model_id: str,
    db_path: Path = DEFAULT_VECTORS_DB_PATH,
) -> dict[str, list[float]]:
    """The stored vectors for `item_ids` *under `model_id`*, keyed by
    item id.

    `model_id` is required rather than defaulted because a caller that
    does not know which model it is comparing is the bug this key
    exists to prevent: mixing two models' vectors in one cosine.

    The bulk form of `get_vector`, added for T21: clustering a four-day
    window asks for thousands of vectors at once, and `get_vector`
    opens, prepares and closes a connection per call. Ids with no stored
    vector are simply absent from the result -- the caller (nc.cluster)
    reports them rather than treating a missing vector as an error,
    because the ordinary cause is an item ingested after the last
    `nc embed`.
    """
    unique = sorted(set(item_ids))
    vectors: dict[str, list[float]] = {}
    conn = _connect(db_path)
    try:
        # Chunked to stay well under SQLite's variable limit (999 by
        # default in older builds) regardless of window size.
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT item_id, dim, vector FROM vectors "
                f"WHERE model_id = ? AND item_id IN ({placeholders})",
                [model_id, *chunk],
            ).fetchall()
            for item_id, dim, blob in rows:
                vectors[str(item_id)] = _unpack(int(dim), bytes(blob))
    finally:
        conn.close()
    return vectors


@dataclass(frozen=True)
class EmbedResult:
    embedded: int
    already_stored: int


def embed_items(
    data_root: DataRoot,
    backend: EmbeddingBackend,
    model_id: str,
    db_path: Path = DEFAULT_VECTORS_DB_PATH,
    batch_size: int = 64,
) -> EmbedResult:
    """Embed every stored item that has no vector yet.

    Reuses `nc.store.read_items` (T12) for reading items -- this module
    only adds the vector cache alongside it, it never re-implements
    item storage. An item id already present in `db_path` is left
    untouched and is never passed to `backend.embed` again, so running
    this twice in a row embeds nothing new (T20's `nc embed` brief:
    "for items without vectors").
    """
    conn = _connect(db_path)
    try:
        existing_ids = {
            row[0]
            for row in conn.execute(
                "SELECT item_id FROM vectors WHERE model_id = ?", (model_id,)
            ).fetchall()
        }
        # `latest_by_id` rather than every row: a duplicated id would
        # otherwise be embedded twice, and `INSERT OR REPLACE` means the
        # second call's only effect is to overwrite the first. The
        # output was already right; the model work was wasted.
        items = latest_by_id(read_items(data_root))
        pending = [item for item in items if item.id not in existing_ids]

        step = max(batch_size, 1)
        for start in range(0, len(pending), step):
            batch = pending[start : start + step]
            vectors = backend.embed([embed_text(item) for item in batch])
            for item, vector in zip(batch, vectors, strict=True):
                conn.execute(
                    "INSERT OR REPLACE INTO vectors "
                    "(item_id, model_id, dim, vector) VALUES (?, ?, ?, ?)",
                    (item.id, model_id, len(vector), _pack(vector)),
                )
        conn.commit()
    finally:
        conn.close()

    return EmbedResult(embedded=len(pending), already_stored=len(items) - len(pending))
