"""Outlet registry, feed health check, and feed-item normalization.

Owns config/outlets.yaml (the outlet registry named in
docs/ARCHITECTURE.md), `nc feeds check` (T10, absorbing the measurement
logic of the retired scripts/probe_feeds.py: entry count, lede presence,
tracking params on entry links, recency and posting cadence), and item
normalization (T11: feed entry -> the Item shape in
docs/ARCHITECTURE.md). Both live here because both are "ingest" per the
architecture's component table and both need the same tracking-param
regex and HTML-to-plain-text helpers -- see docs/OUTLETS.md, "Findings
for other tasks". Storage (`DataRoot`, JSONL append, `nc ingest`) is
T12, not this module: normalization stops at producing `Item` values
from a parsed feed. Thresholds and tunables come from config/feeds.yaml,
never inline here, per CLAUDE.md.
"""

from __future__ import annotations

import re
import time
from calendar import timegm
from dataclasses import dataclass
from hashlib import sha1
from html import unescape
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import yaml

# Tracking parameters seen in the wild are not only utm_*: the BBC uses
# at_campaign/at_medium, other outlets use fbclid, gclid, and bare ref or
# source. docs/OUTLETS.md, "T11, canonicalization".
TRACKING_PARAM = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|source$|at_)", re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

DEFAULT_OUTLETS_PATH = Path("config/outlets.yaml")
DEFAULT_THRESHOLDS_PATH = Path("config/feeds.yaml")


@dataclass(frozen=True)
class Outlet:
    slug: str
    display_name: str
    homepage: str
    feed_url: str


@dataclass(frozen=True)
class FeedThresholds:
    user_agent: str
    lede_min_median_words: int
    lede_max_equal_title_share: float
    stale_after_hours: float


@dataclass(frozen=True)
class FeedMeasurement:
    """Numbers absorbed from scripts/probe_feeds.py, over one fetch."""

    entries: int
    lede_words_median: int
    lede_words_max: int
    lede_equals_title: int
    tracking_params_seen: tuple[str, ...]
    items_last_7d: int
    median_gap_hours: float | None
    newest_entry_date: str | None
    age_of_newest_hours: float | None


@dataclass(frozen=True)
class FeedCheckResult:
    outlet: Outlet
    http_status: int | None
    error: str | None
    measurement: FeedMeasurement | None
    has_lede: bool
    stale: bool
    ok: bool
    failure_reasons: tuple[str, ...]


def _plain_text(html: str) -> str:
    return _WS.sub(" ", unescape(_TAG.sub(" ", html or ""))).strip()


def _lede_of(entry: Any) -> str:
    for key in ("summary", "description"):
        value = getattr(entry, key, "")
        if value:
            return _plain_text(value)
    content = getattr(entry, "content", None)
    if content:
        return _plain_text(content[0].get("value", ""))
    return ""


def _tracking_params(url: str) -> list[str]:
    return [k for k, _ in parse_qsl(urlsplit(url).query) if TRACKING_PARAM.match(k)]


def _measure(entries: list[Any], now: float) -> FeedMeasurement:
    ledes: list[int] = []
    equal_to_title = 0
    tracked: set[str] = set()
    dates: list[time.struct_time] = []

    for entry in entries:
        title = _plain_text(getattr(entry, "title", ""))
        lede = _lede_of(entry)
        ledes.append(len(lede.split()))
        if lede and title and lede.rstrip(".") == title.rstrip("."):
            equal_to_title += 1
        tracked.update(_tracking_params(getattr(entry, "link", "")))
        parsed_date = getattr(entry, "published_parsed", None) or getattr(
            entry, "updated_parsed", None
        )
        if parsed_date is not None:
            dates.append(parsed_date)

    ledes.sort()
    median = ledes[len(ledes) // 2]

    items_last_7d = 0
    median_gap_hours: float | None = None
    newest_entry_date: str | None = None
    age_of_newest_hours: float | None = None
    if dates:
        stamps = sorted(timegm(d) for d in dates)
        newest = max(dates)
        newest_entry_date = (
            f"{newest.tm_year:04d}-{newest.tm_mon:02d}-{newest.tm_mday:02d}"
        )
        age_of_newest_hours = round((now - stamps[-1]) / 3600, 1)
        week_ago = now - 7 * 86400
        items_last_7d = sum(1 for s in stamps if s >= week_ago)
        if len(stamps) > 2:
            gaps = sorted(b - a for a, b in pairwise(stamps))
            median_gap_hours = round(gaps[len(gaps) // 2] / 3600, 1)

    return FeedMeasurement(
        entries=len(entries),
        lede_words_median=median,
        lede_words_max=ledes[-1],
        lede_equals_title=equal_to_title,
        tracking_params_seen=tuple(sorted(tracked)),
        items_last_7d=items_last_7d,
        median_gap_hours=median_gap_hours,
        newest_entry_date=newest_entry_date,
        age_of_newest_hours=age_of_newest_hours,
    )


def check_feed(
    outlet: Outlet, thresholds: FeedThresholds, now: float | None = None
) -> FeedCheckResult:
    """Fetch one feed and score it against `thresholds`.

    A feed fails when it is unreachable, returns zero entries, has no
    real lede, or is stale -- reachable is not usable (docs/OUTLETS.md,
    "T10, nc feeds check").

    `now` overrides the clock staleness is measured against, so a test
    can exercise the configured threshold against a fixture instead of
    setting the threshold wide enough to neutralise it.
    """
    if now is None:
        now = time.time()
    try:
        parsed = feedparser.parse(outlet.feed_url, agent=thresholds.user_agent)
    except Exception as exc:  # noqa: BLE001 - report every feed, never abort the run
        return FeedCheckResult(
            outlet=outlet,
            http_status=None,
            error=f"{type(exc).__name__}: {exc}",
            measurement=None,
            has_lede=False,
            stale=True,
            ok=False,
            failure_reasons=("unreachable",),
        )

    http_status = parsed.get("status")
    entries = parsed.entries
    if not entries:
        return FeedCheckResult(
            outlet=outlet,
            http_status=http_status,
            error=None,
            measurement=None,
            has_lede=False,
            stale=True,
            ok=False,
            failure_reasons=("zero entries",),
        )

    measurement = _measure(entries, now)
    has_lede = (
        measurement.lede_words_median >= thresholds.lede_min_median_words
        and measurement.lede_equals_title
        < len(entries) * thresholds.lede_max_equal_title_share
    )
    stale = (
        measurement.age_of_newest_hours is None
        or measurement.age_of_newest_hours > thresholds.stale_after_hours
    )

    reasons: list[str] = []
    if not has_lede:
        reasons.append("no real lede")
    if stale:
        reasons.append("stale")

    return FeedCheckResult(
        outlet=outlet,
        http_status=http_status,
        error=None,
        measurement=measurement,
        has_lede=has_lede,
        stale=stale,
        ok=not reasons,
        failure_reasons=tuple(reasons),
    )


def check_feeds(
    outlets: list[Outlet], thresholds: FeedThresholds, now: float | None = None
) -> list[FeedCheckResult]:
    return [check_feed(outlet, thresholds, now) for outlet in outlets]


# --- T11: feed entry -> Item -------------------------------------------
#
# Canonicalization rules (docs/ARCHITECTURE.md: "id sha1 of canonical URL
# (utm and tracking params stripped)"), decided here because inconsistency
# silently splits one article into two items across fetches:
#
# - scheme and host lowercased; the path is left exactly as the feed
#   sent it, including case, because it is significant (e.g. WordPress
#   slugs) and there is no safe way to normalize it.
# - tracking parameters stripped with the shared TRACKING_PARAM regex --
#   the same one `nc feeds check` uses, not a second copy.
# - the fragment is dropped. It addresses a spot on the page, never a
#   different article, and some outlets vary it across copies of the
#   same link.
# - a default port (80 for http, 443 for https) is dropped since it
#   changes nothing about the resource; any other port is kept.
# - the trailing slash is left exactly as the feed sent it. Normalizing
#   it either way risks colliding two different resources or splitting
#   one that legitimately needs it; a feed is internally consistent
#   about it per outlet, so leaving it alone is the safer default.
_DEFAULT_PORTS = {"http": 80, "https": 443}

# RSS's <author> element is "email (Display Name)" per the spec, and
# outlets that follow it literally (e.g. engadget: "staff@engadget.com
# (Ian Carlos Campbell)") leave feedparser's `.author` holding that whole
# string. Extract the display name; feeds that just give a name pass
# through unchanged.
_AUTHOR_EMAIL_NAME = re.compile(r"^\S+@\S+\s*\((?P<name>.+)\)$")


@dataclass(frozen=True)
class Item:
    """One feed entry, normalized. docs/ARCHITECTURE.md, "Item"."""

    id: str
    outlet: str
    url: str
    title: str
    lede: str
    author: str | None
    published: str  # ISO 8601 UTC
    fetched: str  # ISO 8601 UTC
    tags: tuple[str, ...]


@dataclass(frozen=True)
class NormalizeConfig:
    lede_word_cap: int


def _port(parts: SplitResult) -> int | None:
    """The link's port, or None when it does not have a usable one.

    `SplitResult.port` raises `ValueError` on a non-numeric port --
    `http://host:abc/` is enough. Feeds are untrusted input and this is
    called once per entry, so that exception reached all the way out of
    an unattended `nc ingest` and stopped the run for every outlet over
    one bad link in one feed. A port we cannot parse is a port we drop;
    the rest of the url still canonicalizes, and the item is still
    identified.
    """
    try:
        return parts.port
    except ValueError:
        return None


def canonical_url(url: str) -> str:
    """Canonical form of an item link -- see the rules above this section."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = _port(parts)
    if port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = hostname if port is None else f"{hostname}:{port}"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not TRACKING_PARAM.match(key)
    ]
    return urlunsplit((scheme, netloc, parts.path, urlencode(query), ""))


def item_id(url: str) -> str:
    """sha1 of `canonical_url(url)`. Canonicalizing first is idempotent,
    so this accepts a raw feed link or an already-canonical one."""
    return sha1(canonical_url(url).encode("utf-8")).hexdigest()


def _iso_utc(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


def utc_now_iso() -> str:
    """Current instant as ISO 8601 UTC, for callers stamping `fetched`."""
    return _iso_utc(time.time())


def _first_words(text: str, limit: int) -> str:
    return " ".join(text.split()[:limit])


def _capped_lede(entry: Any, word_cap: int) -> str:
    return _first_words(_lede_of(entry), word_cap)


def _normalize_author(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _plain_text(raw)
    if not text:
        return None
    match = _AUTHOR_EMAIL_NAME.match(text)
    if match:
        text = match.group("name").strip()
    return text or None


def _normalize_tags(entry: Any) -> tuple[str, ...]:
    raw_tags = getattr(entry, "tags", None) or []
    names = (_plain_text(tag.get("term") or "") for tag in raw_tags)
    return tuple(name for name in names if name)


def _entry_published_utc(entry: Any, fetched: str) -> str:
    """The entry's UTC published time, or `fetched` when it has none.

    feedparser already normalizes every date format it recognizes --
    RFC 822 with a numeric or named offset, and ISO 8601 with an offset
    or a bare Z -- into a UTC time.struct_time, so this only has to pick
    published over updated and convert. A missing or unparseable date
    falls back to `fetched` rather than dropping the item: the item is
    real and worth keeping, an approximate timestamp still places it
    inside clustering's four-day window, and a dropped item is invisible
    forever while a fallback timestamp is only ever slightly wrong.
    """
    parsed_date = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if parsed_date is None:
        return fetched
    return _iso_utc(timegm(parsed_date))


def normalize_entry(
    outlet: Outlet, entry: Any, fetched: str, lede_word_cap: int
) -> Item | None:
    """One feed entry -> one `Item`, or None when it cannot be identified.

    An entry with no link cannot be canonicalized or given a stable id,
    so it is dropped rather than given one made up.
    """
    url = getattr(entry, "link", "") or ""
    if not url:
        return None
    canon = canonical_url(url)
    return Item(
        id=sha1(canon.encode("utf-8")).hexdigest(),
        outlet=outlet.slug,
        url=canon,
        title=_plain_text(getattr(entry, "title", "")),
        lede=_capped_lede(entry, lede_word_cap),
        author=_normalize_author(getattr(entry, "author", None)),
        published=_entry_published_utc(entry, fetched),
        fetched=fetched,
        tags=_normalize_tags(entry),
    )


@dataclass(frozen=True)
class NormalizeResult:
    """What one feed's entries yielded, and how many were unusable."""

    items: list[Item]
    skipped: int = 0


def normalize_feed(
    outlet: Outlet, parsed: Any, fetched: str, lede_word_cap: int
) -> NormalizeResult:
    """Every entry of one parsed feed -> `Item`s, plus a count of the
    ones that could not be turned into any.

    **One bad entry never stops the feed, and one bad feed never stops
    the run.** Feeds are untrusted input arriving on an unattended
    three-hourly schedule, and everything in `normalize_entry` -- url
    parsing, date parsing, whatever feedparser hands back for a field --
    is working on whatever an outlet's CMS emitted. A single entry that
    raises used to abort `nc ingest` for all eleven outlets; the run
    that skips it loses one article, and the next run of a
    ten-to-thirty-entry feed picks it up again anyway.

    Skips are counted and reported rather than swallowed: a feed that
    quietly yields nothing looks exactly like a feed with no news, and
    that is the failure no alert on a crash would ever catch.
    """
    items: list[Item] = []
    skipped = 0
    for entry in parsed.entries:
        try:
            item = normalize_entry(outlet, entry, fetched, lede_word_cap)
        except Exception:  # noqa: BLE001 -- untrusted input, see the docstring
            skipped += 1
            continue
        if item is None:
            skipped += 1
            continue
        items.append(item)
    return NormalizeResult(items=items, skipped=skipped)


def normalize_entries(
    outlet: Outlet, parsed: Any, fetched: str, lede_word_cap: int
) -> list[Item]:
    """`normalize_feed`'s items alone, for callers that do not need the
    skip count."""
    return normalize_feed(outlet, parsed, fetched, lede_word_cap).items


def load_outlets(path: Path = DEFAULT_OUTLETS_PATH) -> list[Outlet]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("outlets"), list):
        raise ValueError(f"{path}: expected a top-level 'outlets' list")

    outlets: list[Outlet] = []
    for item in raw["outlets"]:
        outlets.append(
            Outlet(
                slug=str(item["slug"]),
                display_name=str(item["display_name"]),
                homepage=str(item["homepage"]),
                feed_url=str(item["feed_url"]),
            )
        )
    return outlets


def load_thresholds(path: Path = DEFAULT_THRESHOLDS_PATH) -> FeedThresholds:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")

    return FeedThresholds(
        user_agent=str(raw["user_agent"]),
        lede_min_median_words=int(raw["lede_min_median_words"]),
        lede_max_equal_title_share=float(raw["lede_max_equal_title_share"]),
        stale_after_hours=float(raw["stale_after_hours"]),
    )


def load_normalize_config(path: Path = DEFAULT_THRESHOLDS_PATH) -> NormalizeConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")

    return NormalizeConfig(lede_word_cap=int(raw["lede_word_cap"]))


def result_to_dict(result: FeedCheckResult) -> dict[str, Any]:
    measurement = result.measurement
    return {
        "slug": result.outlet.slug,
        "display_name": result.outlet.display_name,
        "feed_url": result.outlet.feed_url,
        "http_status": result.http_status,
        "error": result.error,
        "ok": result.ok,
        "has_lede": result.has_lede,
        "stale": result.stale,
        "failure_reasons": list(result.failure_reasons),
        "measurement": None
        if measurement is None
        else {
            "entries": measurement.entries,
            "lede_words_median": measurement.lede_words_median,
            "lede_words_max": measurement.lede_words_max,
            "lede_equals_title": measurement.lede_equals_title,
            "tracking_params_seen": list(measurement.tracking_params_seen),
            "items_last_7d": measurement.items_last_7d,
            "median_gap_hours": measurement.median_gap_hours,
            "newest_entry_date": measurement.newest_entry_date,
            "age_of_newest_hours": measurement.age_of_newest_hours,
        },
    }


def format_report(results: list[FeedCheckResult]) -> str:
    """Human-readable report: item counts, newest date, ledes present."""
    lines = []
    for result in results:
        measurement = result.measurement
        entries = measurement.entries if measurement else 0
        newest = measurement.newest_entry_date if measurement else "-"
        lede = "yes" if result.has_lede else "no"
        status = "ok" if result.ok else f"FAIL ({', '.join(result.failure_reasons)})"
        lines.append(
            f"{result.outlet.slug:<20} entries={entries:<4} "
            f"newest={newest or '-':<12} lede={lede:<4} {status}"
        )

    ok_count = sum(1 for r in results if r.ok)
    lines.append(f"\n{ok_count}/{len(results)} feeds ok")
    return "\n".join(lines)
