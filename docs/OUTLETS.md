# Outlets

Candidate feeds for the ingest step, scored from a live measurement, with
a recommended starting set. T10 turns the recommended set into
`config/outlets.yaml`.

Compiled and measured 2026-09-16. 42 candidates: the 27 the old project
scraped, plus 15 English-language tech outlets that belong in a
cross-outlet comparison and were missing from that list.

## Measurement: fetched on a runner, not in this sandbox

The development sandbox has no outbound HTTPS to news domains. The
organization egress proxy denies CONNECT to every candidate host: `curl`
exits 56 with `CONNECT tunnel failed, response 403` and WebFetch returns
`EGRESS_BLOCKED`. github.com and pypi.org are reachable and nothing else
is. That is a permanent fact of this setup, not a transient failure, so
every measurement of a news feed has to happen somewhere else.

It happened on a GitHub Actions `ubuntu-latest` runner:
`scripts/probe_feeds.py`, driven by `.github/workflows/feeds-probe.yml`,
one `feedparser` fetch per url on 2026-09-16 at about 13:30 UTC with the
user agent `newscollection2027/0.1 (+feed evaluation)`.

What came back:

- 40 feed urls across 42 candidates. `recode` and `reuters` have no feed
  url to probe; both stay in the table as scored rows with a reason.
- 32 urls returned entries. 8 returned none.
- **All 32 feeds that returned entries carry a real lede.** Not one
  title-only feed among them. The lowest median is 12 words (Ars
  Technica, Android Authority) and only one entry in the whole sweep had
  a lede identical to its title (1 of 25 at Forbes). The acceptance
  criterion "the recommended set has no outlet whose feed is title-only"
  is now measured rather than asserted.
- Four feeds ship whole article bodies rather than ledes, and three ship
  entry links with tracking parameters. Both matter to T11; see findings.

### What each field means

`config/outlets.candidates.yaml` records, per candidate:

| field | meaning |
|---|---|
| `http_status` | status of the fetch that produced the entries; 301 and 302 mean the feed answered after a redirect |
| `reachable` | true when the host served a feed response, including CNBC's empty one; false when it refused the request (403, 429) or the path is gone (404) |
| `entries_per_fetch` | entries in one fetch, which is a cap the outlet chooses, not an output rate |
| `has_lede` | median lede at least 8 words and fewer than half the entries with a lede equal to the title |
| `lede_words_median`, `lede_words_max` | words left after HTML is stripped from summary, description or first content block |
| `lede_equals_title` | entries whose lede is the title again |
| `items_last_7d` | entries published in the 7 days before the fetch |
| `median_gap_hours` | median gap between consecutive entries in the feed |
| `age_of_newest_hours` | hours between the newest entry and the fetch |
| `canonical_links_clean`, `tracking_params_seen` | tracking query parameters on entry links |
| `terms` | always `not_checked`; nothing was read |
| `failure` | why a feed returned nothing, where it returned nothing |

The earlier schema had a single `items_per_day`. It is gone, because
neither obvious way to compute it survives contact with real feeds:
dividing entries by the newest-to-oldest span collapses to nearly zero
when one stale entry sits at the bottom of a feed, and goes to infinity
for a feed whose entries all land within an hour. `items_last_7d`,
`median_gap_hours` and `age_of_newest_hours` each say something a rate
cannot, and a feed is only healthy if all three look right. The schema
also gained `lede_words_max`, which is what exposes the full-text feeds.

### What one fetch cannot tell you

- It is one sample at one moment. Gaps, ages and counts are a snapshot,
  and a burst or a quiet afternoon moves them.
- `items_last_7d` is a floor for any feed whose entries all fall inside
  the window: the feed was full, so it may well publish more than it
  serves. 24 of the 32 are in that state.
- Overlap cannot be measured this way at all. Knowing which outlets carry
  the same story needs the clustering step over several days of items.
- Tracking parameters were read from the entry link's query string. No
  redirect chain was resolved, so a wrapper host that redirects cleanly
  would not show up here.
- No robots.txt, feed documentation or terms page was read for any
  candidate. `terms` stays a prior everywhere.

## How to re-measure

From a runner, or any machine with egress to the news domains:

```
uv run --no-project --with feedparser --with pyyaml python scripts/probe_feeds.py
```

or trigger `feeds-probe` with `workflow_dispatch`. The probe writes
`feed-probe.json` and uploads it as an artifact. T10 replaces both the
script and the workflow with:

```
nc feeds check --candidates config/outlets.candidates.yaml
```

## Scoring

Six criteria, 0 to 5 each, weighted:

| criterion | key | weight | what it covers |
|---|---|--:|---|
| lede | L | 4 | entries carry a real summary distinct from the title |
| overlap | O | 3 | covers the same stories as the rest of the set |
| volume | V | 2 | enough items a day to cluster, without a deals firehose |
| reachability | R | 1 | feed resolves without bot protection or redirect games |
| canonical | C | 1 | entry links are clean, no tracking parameters, no redirector |
| terms | T | 1 | terms, robots and feed docs appear to permit aggregation with attribution |

Maximum 60. The weights are unchanged from the first pass; only the
scores moved.

`lede` carries the highest weight and is also a hard gate: an outlet
whose feed is title-only cannot enter the recommended set at any score.
The reason is the product. Every claim and every discrepancy on the site
carries a verbatim quote from an item's title or lede, and v1 never reads
article bodies. A title-only feed gives the analysis step one sentence
per item, so the outlet joins a cluster without contributing evidence.
Overlap is weighted next because a cluster is only emitted with two or
more outlets: an excellent feed nobody overlaps with produces no clusters
at all. Reachability, link hygiene and terms are one point each because
they are fixable or binary.

Four criteria are now scored from the measurement by fixed bands, so the
table can be regenerated from the numbers:

| criterion | band |
|---|---|
| lede | `lede_words_median`: 5 at 30 or more, 4 at 20, 3 at 12, 2 at 8, 1 below; 0 when no entries came back |
| volume | `items_last_7d`: 5 at 40 or more, 4 at 20, 3 at 10, 2 at 4, 1 at 1, 0 at none. A cap-bound feed is scored from `median_gap_hours` instead: 5 at 2 h or less, 4 at 6 h, 3 at 12 h, 2 at 24 h, 1 slower. Capped at 3 when the newest entry is over 12 h old |
| reachability | 5 for 200 with entries, 4 for a redirect to entries, 1 for a feed that answered or was blocked but returned nothing, 0 for 404 or no feed url |
| canonical | 5 for no tracking parameter, 3 for `utm_` only, 2 for non-standard parameters, 0 when nothing was measured |

`overlap` and `terms` stay priors. Overlap is a prior by nature — one
fetch per outlet cannot show which outlets carry the same story, and only
clustering can — and terms are a prior because nobody read them. The YAML
records this per criterion in `score_basis`, with values `measured`,
`prior` and `unmeasured`; the eight feeds that returned nothing have
`unmeasured` for lede, volume and canonical, because a blocked or missing
feed tells us nothing about its ledes.

## Measured numbers

The 32 feeds that returned entries, sorted by score. Gaps and ages are
hours. `links` lists the tracking parameters found on entry links.

| outlet | http | entries | 7d | gap | newest | lede med | lede max | links |
|---|--:|--:|--:|--:|--:|--:|--:|---|
| `theverge` | 200 | 10 | 10 | 1.4 | 1.5 | 56 | 56 | clean |
| `techcrunch` | 200 | 20 | 20 | 0.5 | 0.1 | 21 | 41 | clean |
| `theguardian` | 200 | 32 | 23 | 6.0 | 1.6 | 107 | 301 | clean |
| `bleepingcomputer` | 200 | 15 | 15 | 1.5 | 1.2 | 26 | 43 | clean |
| `arstechnica` | 200 | 20 | 20 | 0.6 | 9.5 | 12 | 17 | clean |
| `engadget` | 200 | 20 | 20 | 0.5 | 1.5 | 16 | 28 | clean |
| `wired` | 200 | 50 | 50 | 0.1 | 1.5 | 22 | 38 | clean |
| `techdirt` | 200 | 10 | 10 | 4.0 | 1.0 | 56 | 56 | clean |
| `9to5mac` | 200 | 100 | 100 | 0.6 | 0.0 | 39 | 188 | clean |
| `macrumors` | 200 | 20 | 20 | 0.7 | 0.8 | 370 | 1718 | clean |
| `thehackernews` | 200 | 50 | 47 | 1.3 | 1.5 | 60 | 71 | clean |
| `thenextweb` | 200 | 10 | 10 | 0.4 | 1.2 | 63 | 65 | clean |
| `nytimes-technology` | 200 | 28 | 28 | 2.6 | 4.3 | 23 | 29 | clean |
| `pcmag` | 200 | 100 | 100 | 0.2 | 0.5 | 27 | 42 | clean |
| `theregister` | 302 | 50 | 50 | 0.8 | 0.7 | 14 | 25 | clean |
| `tomshardware` | 301 | 50 | 50 | 0.3 | 1.2 | 25 | 45 | clean |
| `cnet` | 200 | 25 | 20 | 2.0 | 1.5 | 19 | 32 | clean |
| `mashable` | 200 | 100 | 100 | 0.1 | 1.0 | 20 | 34 | clean |
| `techrepublic` | 200 | 20 | 20 | 0.2 | 17.0 | 40 | 45 | clean |
| `404media` | 200 | 15 | 15 | 2.9 | 3.0 | 21 | 32 | clean |
| `digit-fyi` | 200 | 15 | 15 | 2.2 | 0.0 | 71 | 76 | clean |
| `forbes` | 200 | 25 | 25 | 0.2 | 0.2 | 20 | 30 | clean |
| `neowin` | 200 | 40 | 40 | 0.5 | 0.7 | 24 | 31 | `utm_source` |
| `slashdot` | 200 | 15 | 15 | 4.5 | 0.4 | 432 | 778 | `utm_medium`, `utm_source` |
| `zdnet` | 301 | 25 | 17 | 7.6 | 1.4 | 19 | 32 | clean |
| `androidauthority` | 200 | 80 | 80 | 0.3 | 0.6 | 12 | 24 | clean |
| `bbc-technology` | 200 | 21 | 15 | 17.2 | 3.5 | 15 | 25 | `at_campaign`, `at_medium` |
| `inc` | 200 | 39 | 39 | 0.4 | 0.3 | 20 | 37 | clean |
| `qz` | 200 | 21 | 21 | 0.1 | 0.1 | 21 | 25 | clean |
| `inverse` | 200 | 50 | 47 | 1.6 | 1.0 | 19 | 32 | clean |
| `inquisitr` | 200 | 30 | 0 | 1.9 | 376.8 | 55 | 58 | clean |
| `platformer` | 200 | 15 | 2 | 95.4 | 37.0 | 17 | 28 | clean |

## Feeds that returned nothing

| outlet | http | what happened |
|---|--:|---|
| `gizmodo` | 403 | bot-blocked; the body was not well-formed XML |
| `venturebeat` | 429 | rate-limited; the body was an error page, not a feed |
| `cnbc` | 200 | answered 200 with zero entries |
| `digitaltrends` | 202 | answered 202 with zero entries, a bot-mitigation interstitial |
| `axios-technology` | 404 | feed path gone: 404 and a syntax error |
| `businessinsider-tech` | 404 | feed path gone: 404 and not well-formed |
| `techtimes` | 404 | feed path gone: 404 |
| `technicalhint` | 404 | feed path gone: 404 |

Two of these are worth retrying, six are not. Gizmodo and VentureBeat are
blocked, not gone: a 403 and a 429 are what a live site says to an
unfamiliar client, and one retry with backoff and a browser-shaped
User-Agent may well get a feed. CNBC and Digital Trends answered with a
success status and no items, which is a different failure and is not
fixed by retrying. The four 404s are dead paths; the real url has to be
rediscovered from the homepage `<link rel="alternate">` element before
those candidates mean anything.

## Scoring table

Sorted by score. Every candidate has a score and a one-line reason. The
score columns are the criteria keys above; `set` marks membership of the
recommended starting set. L, V, R and C are measured where a feed
answered, O and T are priors throughout.

| outlet | L | O | V | R | C | T | score | set | reason |
|---|--:|--:|--:|--:|--:|--:|--:|---|---|
| `theverge` | 5 | 5 | 5 | 5 | 5 | 3 | 58 | yes | highest-volume generalist, overlaps with everything else |
| `techcrunch` | 4 | 5 | 5 | 5 | 5 | 3 | 54 | yes | startup, funding and AI beat that the others follow same-day |
| `theguardian` | 5 | 4 | 4 | 5 | 5 | 4 | 54 | yes | general-press framing of the stories the trade press runs |
| `bleepingcomputer` | 4 | 4 | 5 | 5 | 5 | 3 | 51 | yes | breach and malware stories that Register and ZDNET also run |
| `arstechnica` | 3 | 5 | 5 | 5 | 5 | 3 | 50 | yes | broad daily coverage; the feed summary is one short sentence |
| `engadget` | 3 | 5 | 5 | 5 | 5 | 3 | 50 | yes | consumer and platform news, same beats as Verge and Ars |
| `wired` | 4 | 4 | 5 | 5 | 5 | 2 | 50 | yes | mainstream-analytical framing of the same platform stories |
| `techdirt` | 5 | 2 | 4 | 5 | 5 | 5 | 49 | — | commentary on the news rather than reporting of it |
| `9to5mac` | 5 | 2 | 5 | 5 | 5 | 2 | 48 | — | Apple vertical, overlaps only inside that niche |
| `macrumors` | 5 | 2 | 5 | 5 | 5 | 2 | 48 | — | Apple vertical; ships full article bodies rather than ledes |
| `thehackernews` | 5 | 2 | 5 | 5 | 5 | 2 | 48 | — | security-only overlap; the feed itself measured well |
| `thenextweb` | 5 | 2 | 5 | 5 | 5 | 2 | 48 | — | European-events focus and thin overlap with the rest of the set |
| `nytimes-technology` | 4 | 4 | 4 | 5 | 5 | 1 | 47 | — | good feed, but the terms of service restrict reuse |
| `pcmag` | 4 | 3 | 5 | 5 | 5 | 2 | 47 | — | reviews-first mix, limited breaking-news overlap |
| `theregister` | 3 | 4 | 5 | 4 | 5 | 4 | 47 | yes | enterprise and security with a distinctly different framing |
| `tomshardware` | 4 | 3 | 5 | 4 | 5 | 2 | 46 | yes | silicon and hardware stories shared with Ars and The Register |
| `cnet` | 3 | 4 | 4 | 5 | 5 | 2 | 44 | — | consumer overlap is real, feed is dominated by buying guides |
| `mashable` | 4 | 2 | 5 | 5 | 5 | 2 | 44 | — | drifted to entertainment and deals, weak news overlap |
| `techrepublic` | 5 | 2 | 3 | 5 | 5 | 2 | 44 | — | B2B how-to and research, few same-day news overlaps |
| `404media` | 4 | 2 | 4 | 5 | 5 | 3 | 43 | — | excellent feed; breaks stories rather than following them, so overlap is thin |
| `digit-fyi` | 5 | 1 | 4 | 5 | 5 | 2 | 43 | — | Scottish regional B2B and events, no global story overlap |
| `forbes` | 4 | 2 | 5 | 5 | 5 | 1 | 43 | — | contributor network, low and uneven signal |
| `neowin` | 4 | 2 | 5 | 5 | 3 | 2 | 42 | — | solid feed, mostly Microsoft-adjacent, narrow overlap |
| `slashdot` | 5 | 1 | 4 | 5 | 3 | 3 | 42 | — | aggregator: its lede quotes other outlets, so it double-counts |
| `zdnet` | 3 | 4 | 3 | 4 | 5 | 2 | 41 | yes | wide overlap; clean feed, second-slowest cadence in the set |
| `androidauthority` | 3 | 2 | 5 | 5 | 5 | 2 | 40 | — | Android vertical, overlaps only inside that niche |
| `bbc-technology` | 3 | 4 | 3 | 5 | 2 | 3 | 40 | yes | low volume but only covers stories everyone else covers |
| `inc` | 4 | 1 | 5 | 5 | 5 | 1 | 40 | — | small-business advice, not tech news |
| `qz` | 4 | 1 | 5 | 5 | 5 | 1 | 40 | — | AI-written articles after the 2025 sale, not a usable baseline |
| `inverse` | 3 | 1 | 5 | 5 | 5 | 1 | 36 | — | pivoted to science, gaming and culture after the BDG changes |
| `inquisitr` | 5 | 1 | 0 | 5 | 5 | 1 | 34 | — | nothing published in the probe window, and celebrity aggregation anyway |
| `platformer` | 3 | 1 | 1 | 5 | 5 | 2 | 29 | — | newsletter cadence, a few posts a week, no cluster partners |
| `gizmodo` | 0 | 3 | 0 | 1 | 0 | 2 | 12 | — | bot-blocked on probe; the trustworthiness doubts stand regardless |
| `venturebeat` | 0 | 3 | 0 | 1 | 0 | 2 | 12 | — | rate-limited on probe; the overlap case is real but unverifiable |
| `axios-technology` | 0 | 3 | 0 | 0 | 0 | 2 | 11 | — | feed path 404s, nothing to score |
| `cnbc` | 0 | 3 | 0 | 1 | 0 | 1 | 11 | — | feed answered 200 with zero entries, so there is nothing to ingest |
| `businessinsider-tech` | 0 | 3 | 0 | 0 | 0 | 1 | 10 | — | feed path 404s, nothing to score |
| `digitaltrends` | 0 | 2 | 0 | 1 | 0 | 2 | 9 | — | 202 bot-mitigation interstitial, zero entries |
| `techtimes` | 0 | 1 | 0 | 0 | 0 | 1 | 4 | — | feed path 404s, nothing to score |
| `technicalhint` | 0 | 0 | 0 | 0 | 0 | 1 | 1 | — | feed path 404s; marginal SEO blog with no newsroom |
| `recode` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | — | brand retired into Vox.com in 2023, no feed of its own |
| `reuters` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | — | public RSS withdrawn around 2020, syndication is a paid product |

Measuring flattened the middle of the table. Reachability and link
hygiene were priors that ranged from 1 to 5 and are now 5 for almost
everything that answered, so they stopped separating outlets, and the
verticals rose: 9to5Mac, MacRumors, TheHackerNews and TNW all sit at 48
on the strength of fast, rich, clean feeds. What keeps them out is
overlap, the one criterion a fetch cannot measure. The ranking is a
ranking of feeds; the set is a choice about clustering.

## Feed URLs

`kind` is `site` for an outlet-wide feed and `section` where a section
feed is the sane choice. `verified` means this url returned entries on
2026-09-16.

| outlet | feed url | kind, how found | verified |
|---|---|---|---|
| `theverge` | `https://www.theverge.com/rss/index.xml` | site, documented | yes |
| `arstechnica` | `https://arstechnica.com/feed/` | site, documented | yes |
| `theregister` | `https://www.theregister.com/headlines.atom` | site, documented | yes |
| `engadget` | `https://www.engadget.com/rss.xml` | site, documented | yes |
| `techcrunch` | `https://techcrunch.com/feed/` | site, convention | yes |
| `theguardian` | `https://www.theguardian.com/uk/technology/rss` | section, documented | yes |
| `bleepingcomputer` | `https://www.bleepingcomputer.com/feed/` | site, convention | yes |
| `wired` | `https://www.wired.com/feed/rss` | site, documented | yes |
| `tomshardware` | `https://www.tomshardware.com/feeds/all` | site, documented | yes |
| `bbc-technology` | `https://feeds.bbci.co.uk/news/technology/rss.xml` | section, documented | yes |
| `404media` | `https://www.404media.co/rss/` | site, documented | yes |
| `nytimes-technology` | `https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml` | section, documented | yes |
| `zdnet` | `https://www.zdnet.com/news/rss.xml` | section, documented | yes |
| `cnet` | `https://www.cnet.com/rss/news/` | section, documented | yes |
| `techdirt` | `https://www.techdirt.com/feed/` | site, convention | yes |
| `venturebeat` | `https://venturebeat.com/feed/` | site, convention | no |
| `androidauthority` | `https://www.androidauthority.com/feed/` | site, convention | yes |
| `macrumors` | `https://feeds.macrumors.com/MacRumors-All` | site, documented | yes |
| `neowin` | `https://www.neowin.net/news/rss/` | site, documented | yes |
| `platformer` | `https://www.platformer.news/rss/` | site, documented | yes |
| `9to5mac` | `https://9to5mac.com/feed/` | site, convention | yes |
| `gizmodo` | `https://gizmodo.com/feed` | site, convention | no |
| `pcmag` | `https://www.pcmag.com/feeds/rss/latest` | site, documented | yes |
| `cnbc` | `https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss25&id=19854910` | section, documented | no |
| `digitaltrends` | `https://www.digitaltrends.com/feed/` | site, convention | no |
| `slashdot` | `https://rss.slashdot.org/Slashdot/slashdotMain` | site, documented | yes |
| `axios-technology` | `https://api.axios.com/feed/technology` | section, search | no |
| `thehackernews` | `https://feeds.feedburner.com/TheHackersNews` | site, documented | yes |
| `mashable` | `https://mashable.com/feeds/rss/all` | site, documented | yes |
| `thenextweb` | `https://thenextweb.com/feed` | site, convention | yes |
| `techrepublic` | `https://www.techrepublic.com/rssfeeds/articles/` | site, documented | yes |
| `businessinsider-tech` | `https://www.businessinsider.com/sai/rss` | section, documented | no |
| `digit-fyi` | `https://www.digit.fyi/feed/` | site, convention | yes |
| `forbes` | `https://www.forbes.com/innovation/feed2/` | section, documented | yes |
| `inverse` | `https://www.inverse.com/rss` | site, convention | yes |
| `inquisitr` | `https://www.inquisitr.com/feed/` | site, convention | yes |
| `qz` | `https://qz.com/rss` | site, convention | yes |
| `techtimes` | `https://www.techtimes.com/rss/sections/tech.xml` | section, documented | no |
| `inc` | `https://www.inc.com/rss/` | site, convention | yes |
| `technicalhint` | `https://www.technicalhint.com/feed/` | site, convention | no |
| `recode` | none | no public feed | n/a |
| `reuters` | none | no public feed | n/a |

Of the 24 urls taken from outlet documentation, 21 answered with entries.
Of the 15 taken from platform convention alone, 11 did. The one url that
came from a web search and nothing else, Axios, 404s.

## Recommended starting set

11 outlets, unchanged from the first pass: The Verge, Ars Technica, The
Register, Engadget, TechCrunch, The Guardian (Technology),
BleepingComputer, WIRED, Tom's Hardware, BBC News (Technology), ZDNET.

The measurement did not disqualify anyone in it, and it did not make a
strong enough case for anyone outside it. Every member passed the lede
gate on measurement:

- lede medians run from 12 words (Ars Technica) to 107 (the Guardian),
  every one of them above the 8-word floor, and not one member had a
  single entry whose lede repeated its title.
- the 11 feeds returned at least 290 items in the probe week. That is a
  floor: 8 of the 11 served a full window, so their `items_last_7d`
  equals `entries_per_fetch` and is bounded by the feed, not the
  newsroom.
- 8 of the 11 post at a median gap under 2 hours. Only the Guardian
  (6.0), ZDNET (7.6) and the BBC (17.2) are slower.
- 10 of the 11 serve clean entry links. The BBC is the exception.
- 8 answered 200 directly; The Register (302) and Tom's Hardware and
  ZDNET (301) answered after a redirect, which costs a point of
  reachability and nothing else.

### The two arguments the measurement forced

**BBC Technology is the slowest member** — 17.2 hours between items, 15
in the week, and the only member whose links carry tracking parameters,
which are also the non-standard `at_campaign` and `at_medium` rather than
`utm_`. It stays, for the reason it was picked: it publishes about two
items a day and they are the stories everyone else also ran, so its hit
rate into clusters should be far higher than its volume suggests. It also
serves 21 entries covering more than a week, so a three-hourly ingest
cannot miss anything of it. Its dirty links cost a point, not a seat:
stripping them is T11's job and the parameters are now recorded there.

**ZDNET measured better than its prior** — the first pass put it in with
a warning that its ledes were the weakest in the set, thin and padded
with deals posts. Measured: 19-word ledes, no entry with a lede equal to
its title, clean links. Its actual weakness is cadence, 7.6 hours and 17
items a week, which is the second-slowest here. That is a better reason
to keep it than the one it had.

### Who nearly changed the set

- **CNET** (44) is the closest. Same overlap prior as ZDNET, faster
  (2.0 h, 20 items a week), clean links, 19-word ledes. What keeps it out
  is the buying-guide mix, and that is exactly what a single fetch cannot
  see: the probe counted entries, it did not classify them. CNET is now
  the first reserve, replacing VentureBeat, whose feed did not answer.
- **NYT Technology** (47) measured as well as anything here: 28 items a
  week, 2.6 h gap, 23-word ledes, clean links. It is out on terms, which
  are still unread. It remains the strongest single addition available if
  the owner is comfortable with them.
- **404 Media** (43) is the one rejection whose stated reason the
  measurement destroyed. It was rejected for publishing "a handful of
  items a week". It published 15 in the probe week, the same as
  BleepingComputer, at a 2.9 h median gap. The volume argument is dead;
  the overlap argument is what keeps it out, and it is a prior: 404 Media
  breaks stories rather than following them, so its items are likely to
  be singletons in this set rather than second members of a cluster.
- **PCMag** (47) has the strongest measurements of any excluded generalist
  — 100 entries a fetch, 0.2 h gap, 27-word ledes — and the same doubt as
  CNET, one the probe cannot settle.

### What would change my mind

Concrete, and checkable once ingest has run for a week:

- If the BBC contributes fewer than about one clustered item a day, drop
  it. The set goes to 10 and nothing is lost.
- If ZDNET clusters on fewer than half its items, swap it for CNET, which
  measured faster on every axis and carries the same overlap prior.
- If the set as a whole emits too few clusters, add in this order: CNET,
  then NYT Technology if the owner accepts the terms, then 404 Media.
  VentureBeat is no longer first reserve; it has to answer a fetch first.
- If any member's median lede falls below 8 words at T10, it leaves on
  the gate, whatever it scores.
- If two outlets in the set turn out to be near-duplicates of each other
  in clusters, drop the lower-scoring one rather than adding anyone.

### Will they actually overlap

Unchanged and still a prior. Seven of the 11 — Ars, The Verge, Engadget,
TechCrunch, WIRED, ZDNET and the Guardian — run the same daily platform
stories: Apple, Google, Meta, OpenAI, Microsoft, Nvidia, antitrust, EU
and US regulation. Security stories bind The Register, BleepingComputer
and ZDNET; hardware and silicon bind Tom's Hardware, Ars and The
Register; the largest stories pull in the Guardian and the BBC. The set
is three overlapping communities sharing the generalists, not 11
independent streams.

The tails will not cluster. Tom's Hardware component reviews,
BleepingComputer's smaller advisories and Guardian technology features
have no partner in the set and will be dropped as singletons. That is the
design working, not a defect, but it means items a week is not clusters a
week and the ratio is unknown until ingest runs.

One thing the measurement did settle: nothing in the set is at risk of
being missed by a three-hourly ingest. The tightest feed is WIRED, whose
50 entries at a 0.1 h median gap cover roughly five hours — an estimate
from the median, not a measured span, but comfortably wider than the
ingest interval. The Verge's 10-entry feed at 1.4 h covers roughly
fourteen.

## Rejected and why

By category. The per-row reasons are in the scoring table.

**Feed did not answer.** `axios-technology`, `businessinsider-tech`,
`techtimes` and `technicalhint` 404: those feed paths are gone and the
candidates cannot be scored on anything until a real url is found.
`cnbc` (200) and `digitaltrends` (202) answered with zero entries.
`gizmodo` (403) and `venturebeat` (429) were blocked; both are worth one
retry with backoff and a real User-Agent before being written off, and
VentureBeat's AI and enterprise overlap makes it the one to retry first.

**Dead or gone.** `recode`, retired into Vox.com in 2023; `reuters`,
whose public RSS was withdrawn around 2020 and whose syndication is a
paid product. Neither has a feed url, so neither was probed; both are
scored 0 with a reason rather than dropped from the table.

**Not an independent voice.** `qz` — sold in 2025, editorial staff cut,
AI-written articles; checking another outlet's claim against generated
text tells us nothing, and its 21 clean 21-word entries do not change
that. `slashdot` — an aggregator whose entry text quotes the outlets we
already ingest, confirmed by measurement: a 432-word median body, which
is other people's copy. `techtimes`, `inquisitr` and `technicalhint` —
rewrite and SEO operations with no newsroom.

**Wrong scope.** `inc`, `inverse`, `digit-fyi`, `forbes`, `techrepublic`
and `techdirt`. All six measured as working feeds — `digit-fyi` and
`techdirt` have some of the richest ledes in the sweep — and all six are
on the wrong beat for a tech-news comparison. Techdirt is worth
revisiting later as a deliberate framing counterweight.

**Right outlet, wrong shape for v1.** `platformer` published 2 items in
the week with the newest 37 hours old: the newsletter-cadence prior held
exactly. `404media` is no longer rejected on volume, only on overlap.

**Verticals.** `9to5mac`, `macrumors`, `androidauthority`, `neowin` and
`thehackernews` all measured excellently and all score 40 to 48, above
two members of the recommended set. Each overlaps only inside its own
niche, which is worth 2 points of overlap and 6 weighted points. Worth
revisiting first if the site grows per-topic sections.

**Close calls.** `cnet` first reserve, `nytimes-technology` on terms
alone, `pcmag` on an unmeasurable mix, `404media` on overlap. See the
section above.

## Findings for other tasks

These came out of the measurement and belong in the tasks that will act
on them.

**T11, canonicalization.** Tracking parameters in the wild are not only
`utm_*`. The BBC uses `at_campaign` and `at_medium`, Neowin `utm_source`,
Slashdot `utm_medium` and `utm_source`. Item ids are a sha1 of the
canonical url, so a parameter the stripper misses gives the same article
two different ids on two fetches and duplicates it across the site.
Fixtures for T11 should include a BBC item with `at_*` parameters
specifically, because a `utm_`-only rule passes every other feed in the
set and fails that one.

**T11, lede extraction.** Four feeds ship whole article bodies rather
than ledes: Slashdot (432-word median, 778 max), MacRumors (370, 1718),
digit.fyi (71) and TheHackerNews (60). The Guardian is a milder case at
107 median and 301 max. T11's rule — strip HTML, take the first 60 words
of summary or content — handles all of them, but the fixtures should
include one so the cap is actually exercised.

**T30, quote validation.** For those full-content feeds the verbatim
quote haystack is the entire article body, not a headline and a lede. A
quote can then validate against text from the middle of an article, which
is not what "a verbatim quote from that item's title or lede" is supposed
to mean. Open question for T30's design: does the validator check quotes
against the stored lede — the capped 60 words — or against everything the
feed sent? The first is the honest reading of the rule and is what the
stored item should contain.

**T10, `nc feeds check`.** Reachable is not usable. CNBC returned 200 and
Digital Trends 202, both with zero entries; a check that only looks at
the HTTP status passes both. Freshness is the other half: Inquisitr
returned 30 entries with none in the last 7 days and the newest 377 hours
old. `nc feeds check` should fail a feed that returns no entries and flag
one whose newest entry is stale, as well as reporting counts, newest date
and lede presence.

**T10 and T13, retry versus dead.** A 403 or 429 means blocked, not gone.
VentureBeat and Gizmodo deserve one retry with backoff and a real
User-Agent before being written off; the four 404s deserve url
rediscovery instead. The check should say which of the two it is.

## Caveats

- One fetch, one moment. Every count, gap and age here is a snapshot from
  2026-09-16 around 13:30 UTC. A feed that was bursting looks fast and a
  feed that was quiet looks slow.
- `entries_per_fetch` is what the outlet chooses to serve, so
  `items_last_7d` is a floor wherever the whole feed fell inside the
  window, which is 24 of the 32 feeds that answered. The Verge serving 10
  entries does not mean The Verge publishes 10 items a week.
- Overlap is still an assertion about editorial beats. It is the
  second-heaviest criterion and nothing in this measurement touched it.
  Only clustering over several days can confirm or refute it.
- Terms were read for nobody. `terms` is a prior for all 42 candidates,
  no robots.txt or terms page was fetched, and none of this is legal
  advice. Before launch the owner should at least read the terms of the
  recommended set and confirm that quoting a headline and lede with
  attribution and a link is acceptable.
- Link cleanliness means no tracking parameters in the entry link's query
  string. Redirect chains were not resolved, so a wrapper host such as
  `feeds.feedburner.com` or `feeds.macrumors.com` that redirects cleanly
  would be recorded as clean. T11 should resolve and canonicalize those
  before trusting the item id.
- The lede gate was measured for the 32 feeds that answered and is
  unknown for the 8 that did not. Those 8 score 0 for lede, which keeps
  them out, but that is absence of evidence rather than a title-only
  feed.
- Feed shape changes without notice. Several of these outlets have moved
  between full text, summary and title-only over the years, and four of
  them are serving full text today. T10 re-measures before writing
  `config/outlets.yaml`, and the gate is enforced there, not here.
- One feed per outlet throughout; section feeds are used for the
  Guardian, the BBC, ZDNET, CNET, the NYT and the rejected general-news
  candidates. If a site-wide feed proves too noisy at T10, prefer its
  section feed over dropping the outlet.
- 42 candidates scored, 40 urls probed, 32 measured with entries.
