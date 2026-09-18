"""T40: `nc build`, the static site.

docs/ARCHITECTURE.md, Site: "Static pages, no JavaScript framework,
minimal CSS, works on a phone." The pages are `/`, `/story/<id>/`,
`/outlet/<slug>/`, `/archive/<date>/`, `/method/` and `/status/`.

**Only current analyses are published.** docs/ARCHITECTURE.md defines
current: the cluster is not superseded and the analysis's
`cluster_version` equals the cluster's. Anything else is history, and
history on the front page is worse than an empty front page -- it is a
story page quoting outlets against a membership that has since changed.
`publishable` is the one place that rule is applied, and every page
downstream reads its result.

**The validator runs again here, and that is not redundant.** `nc
validate` decided when the analysis was written; this decides at build
time, against the cluster as it is now. A cluster that gained an outlet
since, or an analysis file edited by hand, would otherwise reach the
site. CLAUDE.md gives the validator one home and the site does not get
to skip it.

**Every quote on the site carries its outlet, item id and text**
(CLAUDE.md: "No exceptions"). That is not a template decision -- the
contract guarantees it and the templates would have nothing to render
without it -- but it does mean the templates never summarize a quote,
never trim it to fit, and never show a claim without its sources.

**Byte-stable output.** The built site is pushed to `gh-pages` by T42,
so a run that found nothing new must produce identical bytes or every
build is a commit. No timestamps are rendered except ones that come
from the data, collections are sorted, and `nc build` writes a file
only when its content differs.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from nc import runlog
from nc.cluster import STATUS_SUPERSEDED, Cluster, load_clusters
from nc.contract import Analysis, analyses_dir
from nc.stats import compute_outlet_stats
from nc.store import DataRoot

DEFAULT_SITE_DIR = Path("site")
DEFAULT_TEMPLATE_DIR = Path("src/nc/templates")
DEFAULT_SITE_CONFIG_PATH = Path("config/site.yaml")

# How many days the front page shows before a story is archive-only.
# Not a threshold in CLAUDE.md's sense (it decides nothing about the
# data), a presentation choice: the clustering window is four days, so
# a front page covering less would hide stories still gaining outlets.
FRONT_PAGE_DAYS = 4


@dataclass(frozen=True)
class SiteConfig:
    """Deployment facts the templates need. Currently one.

    `base_path` is the URL prefix every internal link carries. A GitHub
    Pages project site serves from `/newscollection2027/`; Netlify and
    any root domain serve from `/`. Hard-coding it in the templates made
    the site correct for exactly one host and silently broken on the
    other -- every link a 404 -- so it moved to `config/site.yaml`.
    """

    base_path: str = "/"


def normalize_base_path(raw: str) -> str:
    """Always exactly one leading and one trailing slash.

    A base path that is `/nc` or `nc/` or `` all mean the same thing to
    whoever wrote it, and all produce different, mostly broken, hrefs.
    This is the one place that is decided.
    """
    trimmed = raw.strip().strip("/")
    return f"/{trimmed}/" if trimmed else "/"


def load_site_config(path: Path = DEFAULT_SITE_CONFIG_PATH) -> SiteConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")
    return SiteConfig(base_path=normalize_base_path(str(raw.get("base_path", "/"))))


def external_url(raw: str) -> str:
    """An outbound article link, or empty if it is not one to follow.

    Item urls come out of RSS feeds, which are not this project's to
    trust. Jinja's autoescape makes an href attribute safe to *quote*
    but says nothing about its scheme, and `javascript:` in an href is
    script execution on our origin. Only http and https get rendered;
    anything else renders as no link at all, which is what the site did
    before these links existed.
    """
    stripped = raw.strip()
    return (
        stripped
        if stripped[:7].lower() in ("http://",) or stripped[:8].lower() == "https://"
        else ""
    )


def slugify(outlet: str) -> str:
    """`/outlet/<slug>/`. Outlet names in `config/outlets.yaml` are
    already lowercase ascii ids (`theverge`, `bbc-technology`), so this
    is a guard against a future one with a space or a dot rather than a
    transformation anything currently depends on."""
    return re.sub(r"[^a-z0-9]+", "-", outlet.lower()).strip("-")


@dataclass(frozen=True)
class SourceLink:
    """One outlet's article, as a story card renders it."""

    outlet: str
    url: str
    title: str


@dataclass(frozen=True)
class Story:
    """One publishable analysis with the cluster it describes."""

    analysis: Analysis
    cluster: Cluster

    @property
    def id(self) -> str:
        return self.cluster.id

    @property
    def date(self) -> str:
        """The earliest item's date, not the cluster id's prefix.

        The id is anchored when the cluster is born, and the anchor is
        the earliest item *known at that moment*; a later run can add an
        earlier one (docs/ARCHITECTURE.md says the true earliest is
        always in `items`). Ordering and archiving by the id's date
        would put a story on the wrong day whenever that happens.
        """
        return min(item.published for item in self.cluster.items)[:10]

    @property
    def outlets(self) -> list[str]:
        return sorted({item.outlet for item in self.cluster.items})

    @property
    def severity(self) -> int:
        """Highest discrepancy severity, as a number for sorting only."""
        ranks = {"low": 1, "medium": 2, "high": 3}
        return max((ranks[d.severity] for d in self.analysis.discrepancies), default=0)

    @property
    def item_by_id(self) -> dict[str, object]:
        return {item.item_id: item for item in self.cluster.items}

    @property
    def url_by_id(self) -> dict[str, str]:
        """item id -> the article's url, for linking a quote back to the
        thing it was quoted from. Empty for a cluster written before the
        url was carried; the template falls back to plain text."""
        return {item.item_id: external_url(item.url) for item in self.cluster.items}

    @property
    def source_links(self) -> list[SourceLink]:
        """Every item in the cluster as an outbound link, outlet order.

        **One entry per item, not per outlet.** A cluster can hold two
        pieces from the same outlet -- `2026-09-15-b07489` holds zdnet's
        "may mess with your audio" and zdnet's "out-of-band update fixes
        audio glitch" -- so an outlet name maps to one article only by
        luck. Collapsing them to one chip per outlet would send half the
        readers who click "zdnet" to the wrong zdnet article. Two chips
        reading `zdnet` is the honest rendering: the story really does
        have two zdnet pieces in it.
        """
        return [
            SourceLink(
                outlet=item.outlet,
                url=external_url(item.url),
                title=item.title,
            )
            for item in sorted(
                self.cluster.items, key=lambda i: (i.outlet, i.published, i.item_id)
            )
        ]


@dataclass(frozen=True)
class BuildReport:
    pages: int = 0
    stories: int = 0
    skipped_stale: int = 0
    skipped_invalid: list[tuple[str, list[str]]] = field(default_factory=list)


def publishable(
    data_root: DataRoot,
) -> tuple[list[Story], int, list[tuple[str, list[str]]]]:
    """Every analysis that may go on the site, and what was refused.

    `(stories, stale, invalid)`. Stale and invalid are counted
    separately because they mean different things to whoever reads the
    build log: stale is the pipeline working as designed (a cluster
    changed and is queued for re-analysis), invalid is something wrong.
    """
    from nc.contract import validate_analysis

    clusters = {cluster.id: cluster for cluster in load_clusters(data_root)}
    stories: list[Story] = []
    stale = 0
    invalid: list[tuple[str, list[str]]] = []

    for path in sorted(analyses_dir(data_root).rglob("*.json")):
        cluster = clusters.get(path.stem)
        try:
            analysis = Analysis.model_validate_json(path.read_text("utf-8"))
        except ValueError as exc:
            invalid.append((path.stem, [f"file: {exc}"]))
            continue
        if cluster is None:
            invalid.append((path.stem, ["no cluster file"]))
            continue
        if (
            cluster.status == STATUS_SUPERSEDED
            or analysis.cluster_version != cluster.version
        ):
            stale += 1
            continue
        _, problems = validate_analysis(json.loads(path.read_text("utf-8")), cluster)
        if problems:
            invalid.append((path.stem, problems))
            continue
        stories.append(Story(analysis=analysis, cluster=cluster))

    # Newest first, then by how many outlets covered it, then by
    # severity -- docs/ARCHITECTURE.md orders the front page by outlets
    # then severity, and the date comes first so today's news is at the
    # top of today's page.
    stories.sort(
        key=lambda story: (
            story.date,
            len(story.outlets),
            story.severity,
            story.id,
        ),
        reverse=True,
    )
    return stories, stale, invalid


def environment(template_dir: Path = DEFAULT_TEMPLATE_DIR) -> Environment:
    """Autoescaped and strict.

    `StrictUndefined` because a typo in a template name should fail the
    build, not render an empty page and publish it. Autoescape because
    every string on this site came out of an RSS feed or a model, and
    neither is trusted to be HTML-safe.
    """
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["slug"] = slugify
    env.filters["pct"] = lambda value: f"{value * 100:.0f}%"
    env.filters["duration"] = runlog.format_duration
    env.filters["external"] = external_url
    return env


def _write(path: Path, text: str) -> bool:
    """Write only when the bytes differ -- `nc.cluster._write_if_changed`
    for the site, and for the same reason: T42 pushes `site/` to
    `gh-pages`, so a build that found nothing new must produce no diff."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def build_site(
    data_root: DataRoot,
    out_dir: Path = DEFAULT_SITE_DIR,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
    runs: list[runlog.Run] | None = None,
    config: SiteConfig | None = None,
) -> BuildReport:
    """Render the whole site into `out_dir`.

    `runs` defaults to whatever `runs/` holds (T43). It stays injectable
    so a test can render the status page against a run it constructed --
    an empty status page is a correct page for a pipeline that has not
    logged a run yet, and both cases need covering.
    """
    env = environment(template_dir)
    settings = load_site_config() if config is None else config
    env.globals["base"] = settings.base_path
    history = runlog.load_runs(data_root) if runs is None else runs
    stories, stale, invalid = publishable(data_root)
    stats = compute_outlet_stats([(s.analysis, s.cluster) for s in stories])
    by_outlet: dict[str, list[Story]] = {}
    for story in stories:
        for outlet in story.outlets:
            by_outlet.setdefault(outlet, []).append(story)
    by_date: dict[str, list[Story]] = {}
    for story in stories:
        by_date.setdefault(story.date, []).append(story)

    dates = sorted(by_date, reverse=True)
    front = [story for story in stories if story.date in dates[:FRONT_PAGE_DAYS]]

    pages = 0
    pages += _write(
        out_dir / "index.html",
        env.get_template("index.html").render(
            stories=front, stats=stats, dates=dates, total=len(stories)
        ),
    )
    pages += _write(out_dir / "style.css", env.get_template("style.css").render())
    pages += _write(
        out_dir / "method" / "index.html",
        env.get_template("method.html").render(stats=stats, total=len(stories)),
    )
    pages += _write(
        out_dir / "status" / "index.html",
        env.get_template("status.html").render(
            runs=history,
            last_success=runlog.last_successful(history),
            stale=stale,
            invalid=invalid,
            total=len(stories),
        ),
    )
    for story in stories:
        pages += _write(
            out_dir / "story" / story.id / "index.html",
            env.get_template("story.html").render(story=story),
        )
    for entry in stats:
        pages += _write(
            out_dir / "outlet" / slugify(entry.outlet) / "index.html",
            env.get_template("outlet.html").render(
                entry=entry, stories=by_outlet.get(entry.outlet, [])
            ),
        )
    for date in dates:
        pages += _write(
            out_dir / "archive" / date / "index.html",
            env.get_template("archive.html").render(
                date=date, stories=by_date[date], dates=dates
            ),
        )

    return BuildReport(
        pages=pages,
        stories=len(stories),
        skipped_stale=stale,
        skipped_invalid=invalid,
    )


def clean(out_dir: Path = DEFAULT_SITE_DIR) -> None:
    """Remove the built site. Used by `nc build --clean`, never
    automatically: a build that silently deleted pages it then failed to
    rewrite would publish a half-empty site."""
    if out_dir.exists():
        shutil.rmtree(out_dir)


def format_build_report(report: BuildReport, out_dir: Path) -> str:
    lines = [
        f"build: {report.stories} story page(s), {report.pages} file(s) "
        f"written to {out_dir}"
    ]
    if report.skipped_stale:
        lines.append(
            f"build: {report.skipped_stale} analysis file(s) skipped as stale "
            "(the cluster moved on; they are queued for re-analysis)"
        )
    for cluster_id, problems in report.skipped_invalid:
        lines.append(f"build: SKIPPED {cluster_id}, invalid at build time:")
        lines.extend(f"    {problem}" for problem in problems)
    return "\n".join(lines)


def internal_links(out_dir: Path = DEFAULT_SITE_DIR) -> list[tuple[Path, str]]:
    """Every site-relative href in the built site, for the link check.

    Only `href`s that start with `/` or are relative: an outbound link
    to an outlet's own article is not this project's to verify, and a
    test that fetched them would fail whenever a newspaper had a bad
    afternoon.
    """
    found: list[tuple[Path, str]] = []
    for path in sorted(out_dir.rglob("*.html")):
        for match in re.finditer(r'href="([^"]+)"', path.read_text("utf-8")):
            href = match.group(1)
            if href.startswith(("http://", "https://", "#", "mailto:")):
                continue
            found.append((path, href))
    return found


def resolve_link(page: Path, href: str, out_dir: Path, base_path: str = "/") -> Path:
    """Where a site-relative href points on disk.

    `base_path` is stripped first: the site is written with the prefix it
    will be served under, which is not a directory inside `out_dir`.
    """
    if base_path != "/" and href.startswith(base_path):
        href = "/" + href[len(base_path) :]
    target = (
        (out_dir / href.lstrip("/")) if href.startswith("/") else (page.parent / href)
    )
    return target / "index.html" if target.suffix == "" else target
