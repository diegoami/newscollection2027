"""Tests for T11's feed-item normalization in nc.feeds.

One test per configured outlet's real fixture (docs/PLAN.md T11 AC),
plus the specific cases docs/OUTLETS.md's "Findings for other tasks"
called out by name: BBC's non-utm_ tracking params, the Guardian's
lede exceeding the 60-word cap, and the six date formats across the
set landing in UTC.
"""

from __future__ import annotations

from hashlib import sha1
from pathlib import Path

import feedparser

from nc import feeds

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"

FETCHED = "2026-09-16T15:00:00Z"
WORD_CAP = 60

# The 11 outlets in config/outlets.yaml, each with its T09-captured fixture.
OUTLET_SLUGS = [
    "theverge",
    "arstechnica",
    "theregister",
    "engadget",
    "techcrunch",
    "theguardian",
    "bleepingcomputer",
    "wired",
    "tomshardware",
    "bbc-technology",
    "zdnet",
]


def _outlet(slug: str) -> feeds.Outlet:
    return feeds.Outlet(
        slug=slug,
        display_name=slug,
        homepage="https://example.com/",
        feed_url="https://example.com/feed",
    )


def _parse(slug: str) -> feedparser.FeedParserDict:
    return feedparser.parse(str(FIXTURES / f"{slug}.xml"))


def _normalize(slug: str) -> list[feeds.Item]:
    outlet = _outlet(slug)
    parsed = _parse(slug)
    return feeds.normalize_entries(outlet, parsed, FETCHED, WORD_CAP)


def test_configured_outlets_match_the_fixture_set() -> None:
    # config/outlets.yaml is the source of truth for which outlets ship;
    # this pins the fixture list to it so a registry change is caught.
    configured = {o.slug for o in feeds.load_outlets()}
    assert configured == set(OUTLET_SLUGS)


def test_all_11_fixtures_parse_without_bozo() -> None:
    for slug in OUTLET_SLUGS:
        parsed = _parse(slug)
        assert not parsed.bozo, f"{slug}: feedparser flagged malformed XML"
        assert parsed.entries, f"{slug}: fixture has no entries"


def test_normalize_every_outlet_fixture_produces_real_items() -> None:
    for slug in OUTLET_SLUGS:
        items = _normalize(slug)
        assert items, f"{slug}: normalization dropped every entry"
        for item in items:
            assert item.id, f"{slug}: empty id"
            assert len(item.id) == 40, f"{slug}: id is not a sha1 hex digest"
            assert item.url, f"{slug}: empty url"
            assert item.title, f"{slug}: empty title"
            assert item.lede, f"{slug}: empty lede"
            assert item.outlet == slug
            # ISO 8601 UTC: exactly a trailing Z, no local offset.
            assert item.published.endswith("Z"), item.published
            assert "+" not in item.published
            assert item.fetched == FETCHED
            assert isinstance(item.tags, tuple)
            # id is a sha1 of the canonical url, not of the raw feed link.
            assert item.id == sha1(item.url.encode("utf-8")).hexdigest()
            assert item.id == feeds.item_id(item.url)


def test_normalize_is_idempotent_across_two_runs() -> None:
    for slug in OUTLET_SLUGS:
        first = [item.id for item in _normalize(slug)]
        second = [item.id for item in _normalize(slug)]
        assert first == second
        assert len(first) == len(set(first)), f"{slug}: duplicate ids in one run"


def test_bbc_at_params_are_stripped_and_id_matches_clean_url() -> None:
    items = _normalize("bbc-technology")
    tracked = items[0]
    # canonical_url already stripped them by the time normalize_entry ran.
    assert "at_medium" not in tracked.url
    assert "at_campaign" not in tracked.url

    raw_with_params = (
        "https://www.bbc.co.uk/news/articles/c6n07ypqz8kzo"
        "?at_medium=RSS&at_campaign=rss"
    )
    raw_without_params = "https://www.bbc.co.uk/news/articles/c6n07ypqz8kzo"
    assert feeds.item_id(raw_with_params) == feeds.item_id(raw_without_params)
    assert tracked.id == feeds.item_id(raw_without_params)


def test_bbc_fixture_actually_has_at_params_on_an_entry_link() -> None:
    # Guards the test above against a fixture edit that removes the case
    # docs/OUTLETS.md specifically asked T11 to cover.
    parsed = _parse("bbc-technology")
    links = [getattr(entry, "link", "") for entry in parsed.entries]
    assert any("at_medium" in link and "at_campaign" in link for link in links)


def test_guardian_lede_exceeds_60_words_before_the_cap() -> None:
    # docs/OUTLETS.md: the Guardian's lede median is 107 words, so this
    # fixture is the one that actually exercises the cap.
    parsed = _parse("theguardian")
    uncapped_lengths = [len(feeds._lede_of(e).split()) for e in parsed.entries]
    assert any(length > WORD_CAP for length in uncapped_lengths)


def test_guardian_normalized_ledes_never_exceed_the_cap() -> None:
    items = _normalize("theguardian")
    for item in items:
        assert len(item.lede.split()) <= WORD_CAP


def test_lede_word_cap_from_config_is_exercised_end_to_end() -> None:
    config = feeds.load_normalize_config()
    parsed = _parse("theguardian")
    items = feeds.normalize_entries(
        _outlet("theguardian"), parsed, FETCHED, config.lede_word_cap
    )
    assert any(
        len(feeds._lede_of(e).split()) > config.lede_word_cap for e in parsed.entries
    )
    assert all(len(item.lede.split()) <= config.lede_word_cap for item in items)


# --- the six date formats, each asserted against a known UTC instant ---


def test_date_format_iso8601_with_negative_offset_theverge() -> None:
    # "2026-09-16T08:00:00-04:00" -> 12:00:00Z
    items = _normalize("theverge")
    published = next(
        i.published for i in items if i.title.startswith("A brief history of AI")
    )
    assert published == "2026-09-16T12:00:00Z"


def test_date_format_rfc822_zero_offset_arstechnica() -> None:
    # "Wed, 16 Sep 2026 14:19:01 +0000" -> unchanged in UTC
    items = _normalize("arstechnica")
    assert items[0].published == "2026-09-16T14:19:01Z"


def test_date_format_rfc822_positive_offset_theregister() -> None:
    # "Wed, 16 Sep 2026 14:46:00 +0200" -> 12:46:00Z
    items = _normalize("theregister")
    assert items[0].published == "2026-09-16T12:46:00Z"


def test_date_format_rfc822_gmt_name_theguardian() -> None:
    # "Wed, 16 Sep 2026 10:42:07 GMT" -> unchanged in UTC
    items = _normalize("theguardian")
    assert items[0].published == "2026-09-16T10:42:07Z"


def test_date_format_rfc822_negative_offset_bleepingcomputer() -> None:
    # "Wed, 16 Sep 2026 10:00:10 -0400" -> 14:00:10Z
    items = _normalize("bleepingcomputer")
    published = next(i.published for i in items if "ransomware" in i.title.lower())
    assert published == "2026-09-16T14:00:10Z"


def test_date_format_iso8601_zulu_zdnet() -> None:
    # "2026-09-16T12:05:35Z" -> unchanged
    items = _normalize("zdnet")
    assert items[0].published == "2026-09-16T12:05:35Z"


def test_missing_date_falls_back_to_fetched() -> None:
    outlet = _outlet("no_date")
    parsed = _parse("no_date")
    items = feeds.normalize_entries(outlet, parsed, FETCHED, WORD_CAP)

    assert len(items) == 1
    assert items[0].published == FETCHED
    assert items[0].fetched == FETCHED


def test_entry_with_no_link_is_dropped_not_given_a_made_up_id() -> None:
    outlet = _outlet("synthetic")
    entry = feedparser.FeedParserDict(title="No link here", summary="No link here")

    assert feeds.normalize_entry(outlet, entry, FETCHED, WORD_CAP) is None


def test_canonical_url_lowercases_scheme_and_host_only() -> None:
    url = "HTTPS://Example.COM/Some/Mixed-Case/Path?utm_source=x"
    assert feeds.canonical_url(url) == "https://example.com/Some/Mixed-Case/Path"


def test_canonical_url_strips_default_https_port() -> None:
    assert feeds.canonical_url("https://example.com:443/a") == "https://example.com/a"


def test_canonical_url_keeps_a_non_default_port() -> None:
    assert (
        feeds.canonical_url("https://example.com:8443/a")
        == "https://example.com:8443/a"
    )


def test_canonical_url_drops_the_fragment() -> None:
    assert feeds.canonical_url("https://example.com/a#comments") == (
        "https://example.com/a"
    )


def test_canonical_url_preserves_trailing_slash_as_given() -> None:
    assert feeds.canonical_url("https://example.com/a/") == "https://example.com/a/"
    assert feeds.canonical_url("https://example.com/a") == "https://example.com/a"


def test_canonical_url_is_idempotent() -> None:
    url = "https://Example.com/a?utm_source=x&id=64"
    once = feeds.canonical_url(url)
    twice = feeds.canonical_url(once)
    assert once == twice


def test_canonical_url_strips_neowin_and_slashdot_shaped_utm_params() -> None:
    # docs/OUTLETS.md: neowin uses utm_source, slashdot utm_medium and
    # utm_source. Not configured outlets, but the shape is real.
    assert feeds.canonical_url("https://x.test/a?utm_source=feed") == (
        "https://x.test/a"
    )
    assert feeds.canonical_url("https://x.test/a?utm_medium=rss&utm_source=feed") == (
        "https://x.test/a"
    )


def test_author_extracted_from_rss_email_display_name_shape() -> None:
    # engadget: "<author>staff@engadget.com (Ian Carlos Campbell)</author>"
    items = _normalize("engadget")
    assert all(item.author and "@" not in item.author for item in items)
    assert "Ian Carlos Campbell" in [item.author for item in items]


def test_author_passes_through_a_plain_name_unchanged() -> None:
    items = _normalize("theguardian")
    assert items[0].author == "Robert Booth UK technology editor"


def test_author_is_none_when_the_feed_gives_none() -> None:
    # bbc-technology's entries carry no <author>/dc:creator at all.
    items = _normalize("bbc-technology")
    assert all(item.author is None for item in items)


def test_tags_are_plain_strings_from_the_feed_categories() -> None:
    items = _normalize("arstechnica")
    first = items[0]
    assert first.tags
    assert all(isinstance(tag, str) and tag for tag in first.tags)


def test_tags_are_empty_not_missing_when_the_feed_has_none() -> None:
    items = _normalize("bbc-technology")
    assert all(item.tags == () for item in items)


def test_html_is_stripped_and_entities_unescaped_in_lede() -> None:
    items = _normalize("theverge")
    for item in items:
        assert "<" not in item.lede
        assert "&amp;" not in item.lede


def test_load_normalize_config_default_is_valid() -> None:
    config = feeds.load_normalize_config()
    assert config.lede_word_cap == WORD_CAP
