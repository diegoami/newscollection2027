"""Outlet registry and feed health check.

Owns config/outlets.yaml (the outlet registry named in
docs/ARCHITECTURE.md) and `nc feeds check`, which absorbs the
measurement logic of the retired scripts/probe_feeds.py: entry count,
lede presence, tracking params on entry links, recency and posting
cadence. Thresholds come from config/feeds.yaml, never inline here, per
CLAUDE.md.
"""

from __future__ import annotations

import re
import time
from calendar import timegm
from dataclasses import dataclass
from html import unescape
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
