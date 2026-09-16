"""Tests for nc.feeds."""

from __future__ import annotations

import calendar
import time
from pathlib import Path

import pytest

from nc import feeds

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"

# Generous enough that a fixture dated in the past never trips the
# staleness check by accident; the staleness tests use their own
# thresholds instead.
GENEROUS_THRESHOLDS = feeds.FeedThresholds(
    user_agent="newscollection2027-test/0.1",
    lede_min_median_words=8,
    lede_max_equal_title_share=0.5,
    stale_after_hours=1_000_000,
)


def _outlet(slug: str, fixture: str) -> feeds.Outlet:
    return feeds.Outlet(
        slug=slug,
        display_name=slug,
        homepage="https://example.com/",
        feed_url=str(FIXTURES / fixture),
    )


def test_plain_text_strips_tags_and_entities() -> None:
    assert feeds._plain_text("<p>A &amp; B</p>") == "A & B"


def test_plain_text_collapses_whitespace() -> None:
    assert feeds._plain_text("line one\n   line two") == "line one line two"


def test_tracking_params_detects_utm() -> None:
    assert feeds._tracking_params("https://x.test/a?utm_source=x") == ["utm_source"]


def test_tracking_params_detects_non_utm_bbc_shape() -> None:
    # T09's finding: tracking parameters in the wild are not only utm_*.
    # The BBC uses at_campaign/at_medium, and a utm_-only rule misses it.
    url = "https://x.test/a?at_campaign=64&at_medium=custom7"
    assert sorted(feeds._tracking_params(url)) == ["at_campaign", "at_medium"]


def test_tracking_params_ignores_clean_query() -> None:
    assert feeds._tracking_params("https://x.test/a?id=64") == []


def test_check_feed_healthy_feed_passes() -> None:
    outlet = _outlet("healthy", "healthy.xml")
    result = feeds.check_feed(outlet, GENEROUS_THRESHOLDS)

    assert result.ok is True
    assert result.has_lede is True
    assert result.stale is False
    assert result.measurement is not None
    assert result.measurement.entries == 5
    assert result.measurement.newest_entry_date == "2026-01-05"


def test_check_feed_title_only_feed_fails_lede_gate() -> None:
    outlet = _outlet("title_only", "title_only.xml")
    result = feeds.check_feed(outlet, GENEROUS_THRESHOLDS)

    assert result.has_lede is False
    assert result.ok is False
    assert "no real lede" in result.failure_reasons
    assert result.measurement is not None
    assert result.measurement.lede_equals_title == 3


def test_check_feed_empty_feed_fails_with_zero_entries() -> None:
    # docs/OUTLETS.md: CNBC and Digital Trends answered 200/202 with zero
    # entries. A status-only check would pass both; this must not.
    outlet = _outlet("empty", "empty.xml")
    result = feeds.check_feed(outlet, GENEROUS_THRESHOLDS)

    assert result.ok is False
    assert result.measurement is None
    assert result.failure_reasons == ("zero entries",)


def test_check_feed_stale_feed_fails_even_with_a_real_lede() -> None:
    # docs/OUTLETS.md: Inquisitr served 30 entries but nothing in 7 days.
    # Reachable is not usable.
    outlet = _outlet("stale", "stale.xml")
    default_thresholds = feeds.FeedThresholds(
        user_agent="newscollection2027-test/0.1",
        lede_min_median_words=8,
        lede_max_equal_title_share=0.5,
        stale_after_hours=168,
    )
    result = feeds.check_feed(outlet, default_thresholds)

    assert result.has_lede is True  # the ledes themselves are fine
    assert result.stale is True
    assert result.ok is False
    assert "stale" in result.failure_reasons


def test_check_feed_records_tracking_params_on_entry_links() -> None:
    outlet = _outlet("tracked", "tracking_params.xml")
    result = feeds.check_feed(outlet, GENEROUS_THRESHOLDS)

    assert result.measurement is not None
    assert result.measurement.tracking_params_seen == ("at_campaign", "at_medium")


def test_check_feed_unreachable_url_fails_without_raising() -> None:
    outlet = _outlet("missing", "does-not-exist.xml")
    result = feeds.check_feed(outlet, GENEROUS_THRESHOLDS)

    assert result.ok is False
    assert result.measurement is None


def test_threshold_application_tightening_lede_words_flips_the_result() -> None:
    outlet = _outlet("healthy", "healthy.xml")
    strict = feeds.FeedThresholds(
        user_agent="newscollection2027-test/0.1",
        lede_min_median_words=1000,
        lede_max_equal_title_share=0.5,
        stale_after_hours=1_000_000,
    )
    result = feeds.check_feed(outlet, strict)

    assert result.has_lede is False
    assert result.ok is False


def test_check_feeds_runs_over_a_list() -> None:
    outlets = [_outlet("healthy", "healthy.xml"), _outlet("empty", "empty.xml")]
    results = feeds.check_feeds(outlets, GENEROUS_THRESHOLDS)

    assert [r.ok for r in results] == [True, False]


def test_load_outlets_reads_the_registry(tmp_path: Path) -> None:
    path = tmp_path / "outlets.yaml"
    path.write_text(
        "outlets:\n"
        "  - slug: example\n"
        "    display_name: Example\n"
        "    homepage: https://example.com/\n"
        "    feed_url: https://example.com/feed\n"
    )

    outlets = feeds.load_outlets(path)

    assert outlets == [
        feeds.Outlet(
            slug="example",
            display_name="Example",
            homepage="https://example.com/",
            feed_url="https://example.com/feed",
        )
    ]


def test_load_outlets_rejects_a_malformed_file(tmp_path: Path) -> None:
    path = tmp_path / "outlets.yaml"
    path.write_text("not_outlets: []\n")

    with pytest.raises(ValueError):
        feeds.load_outlets(path)


def test_load_thresholds_reads_config(tmp_path: Path) -> None:
    path = tmp_path / "feeds.yaml"
    path.write_text(
        "user_agent: test-agent\n"
        "lede_min_median_words: 8\n"
        "lede_max_equal_title_share: 0.5\n"
        "stale_after_hours: 168\n"
    )

    thresholds = feeds.load_thresholds(path)

    assert thresholds == feeds.FeedThresholds(
        user_agent="test-agent",
        lede_min_median_words=8,
        lede_max_equal_title_share=0.5,
        stale_after_hours=168,
    )


def test_load_outlets_default_registry_is_valid() -> None:
    outlets = feeds.load_outlets()

    assert len(outlets) == 11
    assert all(o.slug and o.feed_url.startswith("https://") for o in outlets)


def test_load_thresholds_default_config_is_valid() -> None:
    thresholds = feeds.load_thresholds()

    assert thresholds.lede_min_median_words > 0
    assert 0 < thresholds.lede_max_equal_title_share <= 1
    assert thresholds.stale_after_hours > 0


def test_result_to_dict_is_json_ready() -> None:
    outlet = _outlet("healthy", "healthy.xml")
    result = feeds.check_feed(outlet, GENEROUS_THRESHOLDS)

    payload = feeds.result_to_dict(result)

    assert payload["slug"] == "healthy"
    assert payload["ok"] is True
    assert payload["measurement"]["entries"] == 5


def test_result_to_dict_handles_missing_measurement() -> None:
    outlet = _outlet("empty", "empty.xml")
    result = feeds.check_feed(outlet, GENEROUS_THRESHOLDS)

    payload = feeds.result_to_dict(result)

    assert payload["measurement"] is None


def test_format_report_lists_every_outlet_and_a_summary() -> None:
    outlets = [_outlet("healthy", "healthy.xml"), _outlet("empty", "empty.xml")]
    results = feeds.check_feeds(outlets, GENEROUS_THRESHOLDS)

    report = feeds.format_report(results)

    assert "healthy" in report
    assert "empty" in report
    assert "1/2 feeds ok" in report


def test_fresh_feed_passes_the_configured_staleness_threshold() -> None:
    """The real 168h threshold, exercised against a feed that should pass.

    The other staleness tests widen the threshold until it cannot bite,
    so none of them prove the shipped value accepts a live feed. Pinning
    `now` two hours after the fixture's newest entry does.
    """
    thresholds = feeds.load_thresholds(Path("config/feeds.yaml"))
    outlet = feeds.Outlet(
        slug="healthy",
        display_name="Healthy",
        homepage="https://example.com/",
        feed_url=str(FIXTURES / "healthy.xml"),
    )
    newest = calendar.timegm(time.strptime("2026-01-05", "%Y-%m-%d")) + 12 * 3600

    fresh = feeds.check_feed(outlet, thresholds, now=newest + 2 * 3600)
    assert fresh.ok
    assert not fresh.stale

    # One hour past the threshold, the same feed is stale.
    stale = feeds.check_feed(
        outlet, thresholds, now=newest + (thresholds.stale_after_hours + 1) * 3600
    )
    assert not stale.ok
    assert stale.stale
    assert "stale" in stale.failure_reasons
