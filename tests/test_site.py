"""Tests for T40's `nc.site` (`nc build`).

docs/PLAN.md T40's acceptance criteria: "builds from fixture data in
under ten seconds; an internal link check test passes". Both are here,
along with the rules that decide what may reach the site at all.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from nc.cluster import (
    STATUS_ANALYZED,
    STATUS_SUPERSEDED,
    Cluster,
    ClusterItem,
    render,
)
from nc.contract import Analysis, analysis_path, render_analysis
from nc.site import (
    build_site,
    internal_links,
    publishable,
    resolve_link,
    slugify,
)
from nc.store import DataRoot

VERGE, ARS = "a" * 40, "b" * 40
CLUSTER_ID = "2026-09-17-abc123"


def _cluster(**overrides: Any) -> Cluster:
    base: dict[str, Any] = dict(
        id=CLUSTER_ID,
        version=1,
        anchor=VERGE,
        status=STATUS_ANALYZED,
        items=(
            ClusterItem(
                item_id=VERGE,
                outlet="theverge",
                title="Acme ships the Widget 4",
                lede="The company said it ships in the first quarter.",
                published="2026-09-17T10:00:00Z",
            ),
            ClusterItem(
                item_id=ARS,
                outlet="arstechnica",
                title="Acme announces Widget 4 for Q1",
                lede="Acme said the Widget 4 will cost $499.",
                published="2026-09-17T11:00:00Z",
            ),
        ),
    )
    base.update(overrides)
    return Cluster(**base)


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "cluster_id": CLUSTER_ID,
        "cluster_version": 1,
        "backend": "claude_code",
        "model": "unknown",
        "generated_at": "2026-09-17T12:00:00Z",
        "headline": "Acme ships the Widget 4",
        "summary": "Two outlets report the Widget 4 ships in the first quarter.",
        "claims": [
            {
                "id": "c1",
                "statement": "It ships in the first quarter.",
                "sources": [
                    {
                        "outlet": "theverge",
                        "item_id": VERGE,
                        "quote": "it ships in the first quarter",
                    },
                    {
                        "outlet": "arstechnica",
                        "item_id": ARS,
                        "quote": "Acme announces Widget 4 for Q1",
                    },
                ],
            }
        ],
        "discrepancies": [
            {
                "kind": "omission",
                "severity": "medium",
                "explanation": "Only one gives a price.",
                "quotes": [
                    {
                        "outlet": "arstechnica",
                        "item_id": ARS,
                        "quote": "the Widget 4 will cost $499",
                    },
                    {
                        "outlet": "theverge",
                        "item_id": VERGE,
                        "quote": "Acme ships the Widget 4",
                    },
                ],
            }
        ],
        "agreement": "partial",
    }
    base.update(overrides)
    return base


def _prepare(
    tmp_path: Path,
    cluster: Cluster | None = None,
    payload: dict[str, Any] | None = None,
) -> DataRoot:
    data_root = DataRoot(tmp_path / "data")
    cluster = cluster or _cluster()
    path = data_root.resolve("clusters", cluster.date, f"{cluster.id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(cluster), encoding="utf-8")
    body = payload or _payload()
    out = analysis_path(data_root, str(body["cluster_id"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_analysis(Analysis.model_validate(body)), encoding="utf-8")
    return data_root


# --- what may reach the site ----------------------------------------------


def test_a_current_analysis_is_published(tmp_path: Path) -> None:
    stories, stale, invalid = publishable(_prepare(tmp_path))
    assert [s.id for s in stories] == [CLUSTER_ID]
    assert (stale, invalid) == (0, [])


def test_a_stale_version_is_skipped_not_published(tmp_path: Path) -> None:
    """History on the front page is worse than an empty front page: it
    quotes outlets against a membership that has since changed."""
    data_root = _prepare(tmp_path, cluster=_cluster(version=2))
    stories, stale, invalid = publishable(data_root)
    assert stories == []
    assert (stale, invalid) == (1, [])


def test_a_superseded_cluster_is_skipped(tmp_path: Path) -> None:
    data_root = _prepare(
        tmp_path,
        cluster=_cluster(status=STATUS_SUPERSEDED, superseded_by="2026-09-16-ffffff"),
    )
    stories, stale, _ = publishable(data_root)
    assert (stories, stale) == ([], 1)


def test_the_validator_runs_again_at_build_time(tmp_path: Path) -> None:
    """`nc validate` decided when the analysis was written; this decides
    against the cluster as it is now. An analysis edited by hand
    afterwards must not reach the site."""
    data_root = _prepare(tmp_path)
    path = analysis_path(data_root, CLUSTER_ID)
    tampered = json.loads(path.read_text())
    tampered["claims"][0]["sources"][0]["quote"] = "a sentence nobody published"
    path.write_text(json.dumps(tampered), encoding="utf-8")

    stories, _, invalid = publishable(data_root)
    assert stories == []
    assert invalid and "not verbatim" in invalid[0][1][0]


def test_a_story_is_dated_by_its_earliest_item_not_its_id(tmp_path: Path) -> None:
    """The id's date prefix is the earliest item known when the cluster
    was born; a later run can add an earlier one."""
    cluster = _cluster(
        items=(
            ClusterItem(
                item_id=VERGE,
                outlet="theverge",
                title="Acme ships the Widget 4",
                lede="The company said it ships in the first quarter.",
                published="2026-09-15T10:00:00Z",
            ),
            ClusterItem(
                item_id=ARS,
                outlet="arstechnica",
                title="Acme announces Widget 4 for Q1",
                lede="Acme said the Widget 4 will cost $499.",
                published="2026-09-17T11:00:00Z",
            ),
        )
    )
    stories, _, _ = publishable(_prepare(tmp_path, cluster=cluster))
    assert stories[0].date == "2026-09-15"
    assert stories[0].id.startswith("2026-09-17")


# --- the build ------------------------------------------------------------


def test_build_writes_every_page_the_architecture_lists(tmp_path: Path) -> None:
    data_root = _prepare(tmp_path)
    out = tmp_path / "site"
    report = build_site(data_root, out)

    assert report.stories == 1
    for page in (
        "index.html",
        "style.css",
        "method/index.html",
        "status/index.html",
        f"story/{CLUSTER_ID}/index.html",
        "outlet/theverge/index.html",
        "outlet/arstechnica/index.html",
        "archive/2026-09-17/index.html",
    ):
        assert (out / page).exists(), page


def test_build_is_fast_enough(tmp_path: Path) -> None:
    """T40's AC: under ten seconds from fixture data."""
    data_root = _prepare(tmp_path)
    start = time.monotonic()
    build_site(data_root, tmp_path / "site")
    assert time.monotonic() - start < 10.0


def test_every_internal_link_resolves(tmp_path: Path) -> None:
    """T40's AC: the internal link check. Outbound links to the outlets
    are not checked -- they are not this project's to verify."""
    data_root = _prepare(tmp_path)
    out = tmp_path / "site"
    build_site(data_root, out)

    links = internal_links(out)
    assert links, "expected some internal links"
    missing = [
        (str(page), href)
        for page, href in links
        if not resolve_link(
            page, href.replace("/newscollection2027/", "/"), out
        ).exists()
    ]
    assert missing == []


def test_building_twice_writes_nothing_the_second_time(tmp_path: Path) -> None:
    """T42 pushes `site/` to gh-pages, so a build that found nothing new
    must produce no diff."""
    data_root = _prepare(tmp_path)
    out = tmp_path / "site"
    build_site(data_root, out)
    second = build_site(data_root, out)
    assert second.pages == 0


def test_quotes_reach_the_page_verbatim(tmp_path: Path) -> None:
    """CLAUDE.md: every claim and discrepancy carries a verbatim quote.
    The template never trims one to fit."""
    data_root = _prepare(tmp_path)
    out = tmp_path / "site"
    build_site(data_root, out)
    html = (out / "story" / CLUSTER_ID / "index.html").read_text()
    assert "it ships in the first quarter" in html
    assert "the Widget 4 will cost $499" in html


def test_a_headline_with_html_is_escaped(tmp_path: Path) -> None:
    """Every string on this site came out of an RSS feed or a model."""
    data_root = _prepare(
        tmp_path, payload=_payload(headline="Acme <script>alert(1)</script> ships")
    )
    out = tmp_path / "site"
    build_site(data_root, out)
    html = (out / "story" / CLUSTER_ID / "index.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_an_empty_data_root_still_builds(tmp_path: Path) -> None:
    """No stories is a real state, not an error: a story needs two
    outlets and an analysis that passes."""
    data_root = DataRoot(tmp_path / "data")
    data_root.resolve("analyses").mkdir(parents=True)
    report = build_site(data_root, tmp_path / "site")
    assert report.stories == 0
    assert (tmp_path / "site" / "index.html").exists()


def test_the_outlet_page_refuses_to_interpret_its_numbers(tmp_path: Path) -> None:
    """The one thing a reader would over-read, said on the page."""
    data_root = _prepare(tmp_path)
    out = tmp_path / "site"
    build_site(data_root, out)
    html = (out / "outlet" / "theverge" / "index.html").read_text()
    assert "quality score" in html
    assert "First to report" in html


def test_slugify_handles_an_awkward_outlet_name() -> None:
    assert slugify("BBC Technology") == "bbc-technology"
    assert slugify("Ars Technica.") == "ars-technica"
