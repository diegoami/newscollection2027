"""Fetch every candidate feed and report what it actually contains.

T09 needs measurements, not guesses, and the development sandbox cannot
reach news domains. This runs on a GitHub Actions runner, which can, and
writes the numbers back as JSON for `docs/OUTLETS.md` and
`config/outlets.candidates.yaml`.

Throwaway scaffolding: T10 turns this into `nc feeds check`.
"""

from __future__ import annotations

import json
import re
import sys
from calendar import timegm
from html import unescape
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import feedparser
import yaml

TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|source$|at_)", re.I)
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def plain(html: str) -> str:
    return WS.sub(" ", unescape(TAG.sub(" ", html or ""))).strip()


def lede_of(entry: Any) -> str:
    for key in ("summary", "description"):
        if value := getattr(entry, key, ""):
            return plain(value)
    content = getattr(entry, "content", None)
    if content:
        return plain(content[0].get("value", ""))
    return ""


def tracking_params(url: str) -> list[str]:
    return [k for k, _ in parse_qsl(urlsplit(url).query) if TRACKING.match(k)]


def probe(slug: str, url: str) -> dict[str, Any]:
    result: dict[str, Any] = {"slug": slug, "feed_url": url}
    try:
        agent = "newscollection2027/0.1 (+feed evaluation)"
        parsed = feedparser.parse(url, agent=agent)
    except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["http_status"] = parsed.get("status")
    result["bozo"] = bool(parsed.get("bozo"))
    if parsed.get("bozo") and parsed.get("bozo_exception"):
        result["bozo_reason"] = str(parsed["bozo_exception"])[:200]

    entries = parsed.entries
    result["entries_per_fetch"] = len(entries)
    if not entries:
        return result

    ledes, equal_to_title, tracked = [], 0, set()
    dates = []
    for entry in entries:
        title = plain(getattr(entry, "title", ""))
        lede = lede_of(entry)
        ledes.append(len(lede.split()))
        if lede and title and lede.rstrip(".") == title.rstrip("."):
            equal_to_title += 1
        tracked.update(tracking_params(getattr(entry, "link", "")))
        if parsed_date := getattr(entry, "published_parsed", None) or getattr(
            entry, "updated_parsed", None
        ):
            dates.append(parsed_date)

    ledes.sort()
    median = ledes[len(ledes) // 2]
    result["lede_words_median"] = median
    result["lede_words_max"] = ledes[-1]
    result["entries_without_lede"] = sum(1 for n in ledes if n == 0)
    result["lede_equals_title"] = equal_to_title
    # A feed is title-only if its entries carry no text beyond the headline.
    result["has_lede"] = median >= 8 and equal_to_title < len(entries) / 2
    result["tracking_params_seen"] = sorted(tracked)
    result["canonical_links_clean"] = not tracked

    if dates:
        stamps = sorted(timegm(d) for d in dates)
        newest = max(dates)
        result["newest_entry"] = (
            f"{newest.tm_year:04d}-{newest.tm_mon:02d}-{newest.tm_mday:02d}"
        )
        result["age_of_newest_hours"] = round((time() - stamps[-1]) / 3600, 1)
        # Entries in the last week, and the typical gap between consecutive
        # entries. Dividing the count by the newest-to-oldest span looks
        # simpler but a single stale entry drags it to near zero, and a feed
        # whose entries all land within a day divides by ~0.
        week_ago = time() - 7 * 86400
        result["items_last_7d"] = sum(1 for s in stamps if s >= week_ago)
        if len(stamps) > 2:
            gaps = sorted(b - a for a, b in zip(stamps, stamps[1:], strict=True))
            result["median_gap_hours"] = round(gaps[len(gaps) // 2] / 3600, 1)
    return result


def main() -> int:
    candidates = yaml.safe_load(Path("config/outlets.candidates.yaml").read_text())
    results = []
    for candidate in candidates["candidates"]:
        for feed in candidate.get("feed_urls", []):
            outcome = probe(candidate["slug"], feed["url"])
            outcome["recommended"] = bool(candidate.get("recommended"))
            results.append(outcome)
            status = outcome.get("http_status", outcome.get("error", "?"))
            print(
                f"{candidate['slug']:<20} {str(status):<24} "
                f"entries={outcome.get('entries_per_fetch', 0):<4} "
                f"lede_words={outcome.get('lede_words_median', 0):<4} "
                f"has_lede={outcome.get('has_lede')}",
                flush=True,
            )

    Path("feed-probe.json").write_text(json.dumps(results, indent=2, sort_keys=True))

    reachable = [r for r in results if r.get("entries_per_fetch")]
    with_lede = [r for r in reachable if r.get("has_lede")]
    print(f"\nprobed {len(results)} feeds, {len(reachable)} returned entries, ")
    print(f"{len(with_lede)} carry a real lede")
    bad = [r["slug"] for r in results if r.get("recommended") and not r.get("has_lede")]
    if bad:
        names = ", ".join(sorted(set(bad)))
        print(f"RECOMMENDED BUT TITLE-ONLY OR UNREACHABLE: {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
