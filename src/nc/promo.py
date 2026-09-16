"""Promotional items: coupon pages, deals posts, conference marketing.

**The problem this solves.** The eleven outlets do not publish only
news. Wired runs a coupon desk (measured on the first real corpus: 27
of its 55 items in a four-day window were `Shopping`, `Coupons` or
`Gear / Deals`), Tom's Hardware posts build-a-PC discounts, The Verge
has a deals tag, TechCrunch markets its own conference. None of it is a
story two outlets can be compared on.

Left in the window they are actively harmful, not merely useless,
because they are *near duplicates of each other*. "Casetify Promo Codes
| 15% Off September 2026" and "Visible Promo Codes and Coupons for
September 2026" score 0.7323 -- higher than The Register and
BleepingComputer on the same Iranian malware campaign (0.7330 was the
closest real pair measured, and most genuine cross-outlet matches sit
below that). Three consequences, worst first:

1. A coupon page can reach a story cluster. Two outlets' shopping
   desks linking, plus one link out to a real article, is a component
   with two distinct outlets -- which is all `min_outlets` asks for.
   The site would then be checking one outlet's affiliate copy against
   another's, with quotes, as though it were reporting.
2. They crowd out the labelling corpus. `select_label_sample` caps
   each score bucket, and coupon pairs fill the top buckets first: 7
   of the 10 pairs above 0.80 in the first corpus were same-outlet,
   most of them shopping copy.
3. They corrupt the labels themselves. In the first labelling session
   3 of the 10 "same story" answers were two unrelated coupon pages --
   a positive label on a pair that shares nothing but its genre. Since
   positives are the scarce class, that is a third of the evidence
   `tau_high` is set from, pointing the wrong way.

**Why it is a cluster-time filter and not an ingest-time drop.** The
item store is append-only and the data root is the record (CLAUDE.md);
an item dropped at ingest is gone, and these rules are a judgment call
that will be retuned. So `nc ingest` still writes every item and
`nc cluster` skips them on the way in. Widening or narrowing
`config/promo.yaml` therefore changes the next run's window with no
migration, no rewrite of a JSONL file, and no idempotence obligation --
the same reason `nc.labelling` reads what `nc cluster` persisted
rather than recomputing it.

**Why the rules are two kinds.** The outlets' own feed categories are
the reliable signal, because a human at the outlet chose them; matching
those alone catches Wired and The Verge completely. Tom's Hardware
files its discount posts under `Gaming PCs` like any other hardware
piece, so titles have to be matched too -- and only on shopping-copy
constructions. Matching a bare "deal" was measured against the corpus
and dropped three real stories ("May Mobility is going public in a
$1.4B SPAC deal", "signed reciprocal severance deals", "biggest-ever
US community benefits deal"), which is why `config/promo.yaml` lists
"promo code" and "$N off" but not "deal".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from nc.feeds import Item

DEFAULT_PROMO_CONFIG_PATH = Path("config/promo.yaml")


def _normalize(text: str) -> str:
    """Lowercased, whitespace collapsed -- so `Gear / Deals`,
    `gear / deals` and `Gear  /  Deals` are one tag."""
    return " ".join(text.lower().split())


@dataclass(frozen=True)
class PromoRules:
    """What `config/promo.yaml` says, compiled once.

    Empty rules match nothing, so a missing or empty config file
    disables the filter rather than dropping the whole window.
    """

    tags: frozenset[str]
    title_patterns: tuple[re.Pattern[str], ...]

    def matches(self, item: Item) -> bool:
        """True when this item is promotional under these rules."""
        for tag in item.tags:
            if _normalize(tag) in self.tags:
                return True
        title = item.title
        return any(pattern.search(title) for pattern in self.title_patterns)


def load_promo_rules(path: Path = DEFAULT_PROMO_CONFIG_PATH) -> PromoRules:
    """Read `config/promo.yaml`. A missing file is not an error: it
    means no filtering, which is what a fresh checkout of the data
    repository or a test fixture without the file should get."""
    if not path.exists():
        return PromoRules(frozenset(), ())
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return PromoRules(frozenset(), ())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")

    tags = frozenset(_normalize(str(tag)) for tag in raw.get("tags") or ())
    patterns: list[re.Pattern[str]] = []
    for source in raw.get("title_patterns") or ():
        try:
            patterns.append(re.compile(str(source), re.IGNORECASE))
        except re.error as exc:  # a typo in the config, named where it is
            raise ValueError(f"{path}: bad title pattern {source!r}: {exc}") from exc
    return PromoRules(tags, tuple(patterns))


def partition(items: list[Item], rules: PromoRules) -> tuple[list[Item], list[Item]]:
    """`(news, promotional)`, each keeping the input order.

    Both halves are returned rather than just the keepers so the caller
    can report how many were dropped: a filter whose effect is invisible
    in the run log is one nobody notices misfiring.
    """
    news: list[Item] = []
    promotional: list[Item] = []
    for item in items:
        (promotional if rules.matches(item) else news).append(item)
    return news, promotional
