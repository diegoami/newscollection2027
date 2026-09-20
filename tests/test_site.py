"""Tests for T40's `nc.site` (`nc build`).

docs/PLAN.md T40's acceptance criteria: "builds from fixture data in
under ten seconds; an internal link check test passes". Both are here,
along with the rules that decide what may reach the site at all.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from nc import runlog
from nc.cluster import (
    STATUS_ANALYZED,
    STATUS_SUPERSEDED,
    Cluster,
    ClusterItem,
    render,
)
from nc.contract import Analysis, analysis_path, render_analysis
from nc.site import (
    SiteConfig,
    build_site,
    external_url,
    internal_links,
    load_site_config,
    normalize_base_path,
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
                url="https://www.theverge.com/acme-widget-4",
            ),
            ClusterItem(
                item_id=ARS,
                outlet="arstechnica",
                title="Acme announces Widget 4 for Q1",
                lede="Acme said the Widget 4 will cost $499.",
                published="2026-09-17T11:00:00Z",
                url="https://arstechnica.com/acme-widget-4-q1",
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

    base = load_site_config().base_path
    links = internal_links(out)
    assert links, "expected some internal links"
    missing = [
        (str(page), href)
        for page, href in links
        if not resolve_link(page, href, out, base).exists()
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


# --- T43: the status page reads the run log -------------------------------


def _run(
    date: str,
    status: str = "ok",
    analyses: int = 3,
    pending: int = 2,
    rejected: int = 1,
    duration: float | None = 612.0,
    note: str = "",
) -> runlog.Run:
    return runlog.Run(
        date=date,
        finished_at=f"{date}T02:10:00Z",
        status=status,
        counts=runlog.RunCounts(
            analyses_written=analyses, pending=pending, rejected=rejected
        ),
        started_at=f"{date}T02:00:00Z",
        duration_seconds=duration,
        agent_seconds=(None if duration is None else duration - 12.0),
        steps=(runlog.Step(name="cluster", seconds=4.5, ok=True),),
        note=note,
    )


def _status_page(tmp_path: Path, runs: list[runlog.Run]) -> str:
    out = tmp_path / "site"
    build_site(_prepare(tmp_path), out, runs=runs)
    return (out / "status" / "index.html").read_text(encoding="utf-8")


def test_the_status_page_shows_the_last_runs_counts_rejects_and_durations(
    tmp_path: Path,
) -> None:
    """T43's acceptance criterion, item by item."""
    page = _status_page(tmp_path, [_run("2026-09-18")])
    row = page[page.index("<tbody>") : page.index("</tbody>")]

    # counts: analysed, still pending, rejected on disk
    assert re.findall(r'class="num">(\d+)</td>', row)[:3] == ["3", "2", "1"]
    # durations: total, and the model's own time
    assert "10m 12s" in row
    assert "10m 00s" in row
    # and the per-step breakdown of the newest run
    assert "<h2>2026-09-18, step by step</h2>" in page
    assert "cluster" in page


def test_the_status_page_shows_when_the_last_successful_run_finished(
    tmp_path: Path,
) -> None:
    """T52. The age is rendered by the browser from this timestamp, not
    computed at build time -- see the next test for why."""
    page = _status_page(tmp_path, [_run("2026-09-18")])
    assert '<time datetime="2026-09-18T02:10:00Z">' in page


def test_a_failed_run_does_not_become_the_last_successful_one(
    tmp_path: Path,
) -> None:
    runs = [
        _run("2026-09-18", status="failed", note="nc build broke"),
        _run("2026-09-17"),
    ]
    page = _status_page(tmp_path, runs)

    assert '<time datetime="2026-09-17T02:10:00Z">' in page
    assert "stopped on purpose" in page
    assert "nc build broke" in page


def test_an_empty_night_still_counts_as_successful(tmp_path: Path) -> None:
    """`empty` means the pipeline ran and found no work, which is what
    most nights look like once the backlog is clear. Treating it as a
    failure would light the page up every quiet night."""
    page = _status_page(tmp_path, [_run("2026-09-18", status="empty", analyses=0)])
    assert "No successful run recorded yet" not in page
    assert '<time datetime="2026-09-18T02:10:00Z">' in page


def test_the_status_page_is_byte_stable_with_a_run_log(tmp_path: Path) -> None:
    """The reason the age is not rendered server-side. `nc build` runs on
    every data change, and a page carrying "6 hours ago" would differ on
    every build -- one gh-pages commit per deploy, for no change (T42).
    """
    out = tmp_path / "site"
    data_root = _prepare(tmp_path)
    runs = [_run("2026-09-18")]
    build_site(data_root, out, runs=runs)
    first = (out / "status" / "index.html").read_text(encoding="utf-8")
    time.sleep(0.05)
    report = build_site(data_root, out, runs=runs)

    assert (out / "status" / "index.html").read_text(encoding="utf-8") == first
    assert report.pages == 0


def test_an_unknown_duration_is_a_dash_not_a_zero(tmp_path: Path) -> None:
    """A run that kept no journal has an unknown duration. Zero is a
    claim, and the wrong one."""
    page = _status_page(tmp_path, [_run("2026-09-18", duration=None)])
    assert "&mdash;" in page or "—" in page


def test_no_run_log_is_a_correct_page_not_a_broken_one(tmp_path: Path) -> None:
    page = _status_page(tmp_path, [])
    assert "No successful run recorded yet" in page


def test_the_status_page_reads_the_data_root_when_no_runs_are_passed(
    tmp_path: Path,
) -> None:
    """`nc build` takes no `--runs` flag: the records are in the data
    root, next to everything else the build reads."""
    data_root = _prepare(tmp_path)
    runlog.write_run(data_root, _run("2026-09-18"))

    out = tmp_path / "site"
    build_site(data_root, out)

    page = (out / "status" / "index.html").read_text(encoding="utf-8")
    assert '<time datetime="2026-09-18T02:10:00Z">' in page


# --- the base path (Netlify, or any root domain) ---------------------------


def test_the_base_path_is_normalized_however_it_is_written() -> None:
    """`/nc`, `nc/` and ` /nc/ ` all mean one thing to whoever wrote them
    and produce three different, mostly broken, sets of hrefs. This is
    the one place that is decided."""
    for raw in ("/newscollection2027/", "newscollection2027", "/newscollection2027"):
        assert normalize_base_path(raw) == "/newscollection2027/"
    for raw in ("", "/", "   "):
        assert normalize_base_path(raw) == "/"


def test_a_root_domain_build_writes_root_links(tmp_path: Path) -> None:
    """The Netlify case. docs/ARCHITECTURE.md names Netlify as the host
    after prototyping, and the whole move is this one config line."""
    out = tmp_path / "site"
    build_site(_prepare(tmp_path), out, config=SiteConfig(base_path="/"))

    page = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/style.css"' in page or 'href="/"' in page
    assert "/newscollection2027/" not in page


def test_every_internal_link_resolves_on_a_root_domain_too(tmp_path: Path) -> None:
    """The link check is what would have caught the hard-coded prefix:
    with the wrong base every link on the site is a 404, and nothing else
    about the build looks wrong."""
    out = tmp_path / "site"
    build_site(_prepare(tmp_path), out, config=SiteConfig(base_path="/"))

    missing = [
        (str(page), href)
        for page, href in internal_links(out)
        if not resolve_link(page, href, out, "/").exists()
    ]
    assert missing == []


def test_the_shipped_config_is_the_github_pages_prefix() -> None:
    """Changing this is the deploy step, not a code change -- but it has
    to match where T42's workflow publishes, or the live site 404s."""
    assert load_site_config().base_path == "/newscollection2027/"


# --- linking back to the article ------------------------------------------


def test_a_story_page_links_to_every_article_it_quotes(tmp_path: Path) -> None:
    """The gap that reached the published site: three outlets quoted and
    no way to open any of them. A quote a reader cannot go and check is
    the one thing this project is not for."""
    out = tmp_path / "site"
    build_site(_prepare(tmp_path), out)

    page = (out / "story" / CLUSTER_ID / "index.html").read_text(encoding="utf-8")
    for item in _cluster().items:
        assert f'href="{item.url}"' in page, item.outlet


def test_outbound_links_are_marked_as_leaving_the_site(tmp_path: Path) -> None:
    out = tmp_path / "site"
    build_site(_prepare(tmp_path), out)
    page = (out / "story" / CLUSTER_ID / "index.html").read_text(encoding="utf-8")
    assert 'rel="noopener nofollow"' in page


def test_a_url_that_is_not_http_is_not_rendered_as_a_link() -> None:
    """Item urls come out of RSS feeds, which are not this project's to
    trust. Autoescape makes an href safe to quote and says nothing about
    its scheme, and `javascript:` in an href is script execution on our
    own origin."""
    assert external_url("https://example.com/a") == "https://example.com/a"
    assert external_url("http://example.com/a") == "http://example.com/a"
    assert external_url("  https://example.com/a  ") == "https://example.com/a"
    for bad in (
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>",
        "file:///etc/passwd",
        "",
        "   ",
        "/newscollection2027/",
    ):
        assert external_url(bad) == "", bad


def test_a_hostile_url_reaches_the_page_as_text_not_a_link(tmp_path: Path) -> None:
    """End to end, because the guard is only worth having if the
    template actually applies it."""
    hostile = _cluster(
        items=(
            replace(_cluster().items[0], url="javascript:alert(1)"),
            _cluster().items[1],
        )
    )
    out = tmp_path / "site"
    build_site(_prepare(tmp_path, cluster=hostile), out)

    page = (out / "story" / CLUSTER_ID / "index.html").read_text(encoding="utf-8")
    assert "javascript:" not in page
    # The item is still listed, just not as a link.
    assert hostile.items[0].title in page


def test_a_cluster_with_no_urls_still_builds(tmp_path: Path) -> None:
    """A cluster file written before the url was carried. The page shows
    a heading rather than a broken link, which is what it did before."""
    older = _cluster(items=tuple(replace(item, url="") for item in _cluster().items))
    out = tmp_path / "site"
    report = build_site(_prepare(tmp_path, cluster=older), out)

    assert report.stories == 1
    page = (out / "story" / CLUSTER_ID / "index.html").read_text(encoding="utf-8")
    assert 'rel="noopener nofollow"' not in page
    assert older.items[0].title in page


# --- the claims table, and the cards --------------------------------------

# A second item from an outlet already in the cluster. This is the case
# that made the claims table unlinked in the first place: `theverge`
# appears twice, so the column header maps to two articles and linking
# it would send half the readers to the wrong one. Every link below is
# per *item* instead, which has no such ambiguity.
VERGE_2 = "d" * 40


def _two_from_one_outlet() -> tuple[Cluster, dict[str, Any]]:
    cluster = _cluster(
        items=(
            *_cluster().items,
            ClusterItem(
                item_id=VERGE_2,
                outlet="theverge",
                title="Acme's Widget 4 is up for preorder",
                lede="Preorders opened an hour after the announcement.",
                published="2026-09-17T12:00:00Z",
                url="https://www.theverge.com/acme-widget-4-preorder",
            ),
        )
    )
    payload = _payload()
    payload["claims"][0]["sources"].append(
        {
            "outlet": "theverge",
            "item_id": VERGE_2,
            "quote": "Preorders opened an hour after the announcement",
        }
    )
    return cluster, payload


def test_each_quote_in_the_claims_table_links_to_its_own_article(
    tmp_path: Path,
) -> None:
    """The largest surface on a story page, and it had no links at all.
    The link is keyed on the quote's `item_id`, never on the column's
    outlet name: both quotes below are theverge's and they came from
    different articles."""
    cluster, payload = _two_from_one_outlet()
    out = tmp_path / "site"
    build_site(_prepare(tmp_path, cluster=cluster, payload=payload), out)
    page = (out / "story" / CLUSTER_ID / "index.html").read_text(encoding="utf-8")

    for item in cluster.items:
        quote = next(
            source["quote"]
            for source in payload["claims"][0]["sources"]
            if source["item_id"] == item.item_id
        )
        assert f'<blockquote cite="{item.url}"><a href="{item.url}"' in page
        assert quote in page


def test_a_quote_whose_item_has_no_url_stays_plain_text(tmp_path: Path) -> None:
    older = _cluster(items=tuple(replace(item, url="") for item in _cluster().items))
    out = tmp_path / "site"
    build_site(_prepare(tmp_path, cluster=older), out)
    page = (out / "story" / CLUSTER_ID / "index.html").read_text(encoding="utf-8")

    assert "<blockquote cite=" not in page
    assert "it ships in the first quarter" in page


def test_a_story_card_links_its_outlets_to_the_articles(tmp_path: Path) -> None:
    """A card's outlet chips used to point at `/outlet/<slug>/`, our own
    page about the outlet, which is not the source. They point at the
    articles now; the outlet pages are still reached from the Outlets
    table on the front page."""
    out = tmp_path / "site"
    build_site(_prepare(tmp_path), out)
    page = (out / "index.html").read_text(encoding="utf-8")

    for item in _cluster().items:
        assert f'<a href="{item.url}" rel="noopener nofollow"' in page
    assert '<a href="/newscollection2027/outlet/theverge/">theverge</a>' in page


def test_a_card_shows_one_chip_per_item_not_per_outlet(tmp_path: Path) -> None:
    """Two theverge pieces in one story are two chips, each going to its
    own article. Collapsing them would mean guessing which one a reader
    clicking `theverge` wanted."""
    cluster, payload = _two_from_one_outlet()
    out = tmp_path / "site"
    build_site(_prepare(tmp_path, cluster=cluster, payload=payload), out)
    page = (out / "index.html").read_text(encoding="utf-8")
    card = page.split('<p class="outlets">')[1]

    for item in cluster.items:
        assert f'href="{item.url}"' in card


def test_a_card_does_not_link_an_outlet_whose_url_is_hostile(tmp_path: Path) -> None:
    """Same guard as the story page: the card is a second template and
    would otherwise be a second way in."""
    hostile = _cluster(
        items=(
            replace(_cluster().items[0], url="javascript:alert(1)"),
            _cluster().items[1],
        )
    )
    out = tmp_path / "site"
    build_site(_prepare(tmp_path, cluster=hostile), out)
    page = (out / "index.html").read_text(encoding="utf-8")

    assert "javascript:" not in page
    assert '<span class="unlinked"' in page
    assert "theverge" in page


# --- pages that stopped being current -------------------------------------


def test_a_story_that_went_stale_loses_its_page(tmp_path: Path) -> None:
    """`publishable` refuses to publish a stale analysis, but refusing to
    write a page is not the same as removing the one already there. On a
    reused checkout the old page stayed, still served by URL, still
    quoting outlets against a membership that has since changed."""
    out = tmp_path / "site"
    data_root = _prepare(tmp_path)
    build_site(data_root, out)
    page = out / "story" / CLUSTER_ID / "index.html"
    assert page.exists()

    # The cluster gains a member: the analysis on disk is now for an
    # older version and stops being publishable.
    moved = _cluster(version=2)
    path = data_root.resolve("clusters", moved.date, f"{moved.id}.json")
    path.write_text(render(moved), encoding="utf-8")

    report = build_site(data_root, out)

    assert report.stories == 0
    assert report.skipped_stale == 1
    assert report.removed >= 1
    assert not page.exists()
    # The site itself is still there -- pruning is not cleaning.
    assert (out / "index.html").exists()
    assert (out / "style.css").exists()


def test_a_build_removes_an_outlet_page_that_no_longer_has_stories(
    tmp_path: Path,
) -> None:
    out = tmp_path / "site"
    data_root = _prepare(tmp_path)
    build_site(data_root, out)
    assert (out / "outlet" / "theverge" / "index.html").exists()

    stray = out / "outlet" / "defunctnews" / "index.html"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("<!doctype html>stale", encoding="utf-8")

    report = build_site(data_root, out)

    assert report.removed == 1
    assert not stray.exists()
    assert not stray.parent.exists()  # the empty directory goes too
    assert (out / "outlet" / "theverge" / "index.html").exists()


def test_a_second_build_with_nothing_new_writes_and_removes_nothing(
    tmp_path: Path,
) -> None:
    """T42 pushes `site/` to `gh-pages`, so an unchanged build must
    produce no diff at all -- pruning must not undo that."""
    out = tmp_path / "site"
    data_root = _prepare(tmp_path)
    build_site(data_root, out)

    report = build_site(data_root, out)

    assert (report.pages, report.removed) == (0, 0)
