"""T41: what the site can honestly say about an outlet.

Three numbers per outlet, all computed here by one function so the front
page and the outlet pages can never disagree about them:

- *stories* -- how many published analyses this outlet appears in.
- *discrepancy involvement rate* -- of those stories, the share where
  this outlet is quoted in at least one discrepancy.
- *first-to-report rate* -- of those stories, the share where this
  outlet's item has the earliest `published` in the cluster.

**What these numbers are not.** None of them is a quality score, and the
site must never present them as one. A high discrepancy rate can mean an
outlet gets things wrong, or that it reports more detail than the others
and therefore has more surface to differ on, or that it files later and
can correct them. The pipeline cannot tell those apart from a title and
a lede, so `/outlet/<slug>/` describes the number and refuses to
interpret it.

**Involvement, not fault.** A discrepancy names two or more outlets and
this module counts *every* outlet quoted in it. It never assigns the
disagreement to one of them: deciding who is wrong needs the article
bodies, which this project never has.

**Ties are shared, not broken.** Two outlets that filed in the same
second are both first. Breaking the tie by outlet name or item id would
manufacture a difference out of a timestamp's resolution, and
"first-to-report" is exactly the number a reader would over-read.
"""

from __future__ import annotations

from dataclasses import dataclass

from nc.cluster import Cluster
from nc.contract import Analysis


@dataclass(frozen=True)
class OutletStats:
    outlet: str
    stories: int
    discrepancy_stories: int
    first_stories: int

    @property
    def discrepancy_rate(self) -> float:
        return self.discrepancy_stories / self.stories if self.stories else 0.0

    @property
    def first_rate(self) -> float:
        return self.first_stories / self.stories if self.stories else 0.0


def compute_outlet_stats(
    pairs: list[tuple[Analysis, Cluster]],
) -> list[OutletStats]:
    """The one place these numbers are computed.

    Takes `(analysis, cluster)` pairs rather than reading the data root,
    so it is pure and the caller decides what counts as published --
    `nc.site` passes only current analyses, and a test passes whatever
    it wants to assert about.

    Sorted by story count then name, so the front page's ordering is
    stable across runs and the built site does not churn.
    """
    stories: dict[str, int] = {}
    with_discrepancy: dict[str, int] = {}
    first: dict[str, int] = {}

    for analysis, cluster in pairs:
        outlets = {item.outlet for item in cluster.items}
        for outlet in outlets:
            stories[outlet] = stories.get(outlet, 0) + 1

        involved = {
            quote.outlet
            for discrepancy in analysis.discrepancies
            for quote in discrepancy.quotes
        }
        for outlet in involved & outlets:
            with_discrepancy[outlet] = with_discrepancy.get(outlet, 0) + 1

        # Shared, not broken: see the module docstring.
        earliest = min(item.published for item in cluster.items)
        for outlet in {
            item.outlet for item in cluster.items if item.published == earliest
        }:
            first[outlet] = first.get(outlet, 0) + 1

    return sorted(
        (
            OutletStats(
                outlet=outlet,
                stories=count,
                discrepancy_stories=with_discrepancy.get(outlet, 0),
                first_stories=first.get(outlet, 0),
            )
            for outlet, count in stories.items()
        ),
        key=lambda entry: (-entry.stories, entry.outlet),
    )
