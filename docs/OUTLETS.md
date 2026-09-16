# Outlets

Candidate feeds for the ingest step, scored, with a recommended starting
set. T10 turns the recommended set into `config/outlets.yaml`.

Compiled 2026-09-16. 42 candidates: the 27 the old project scraped, plus
15 English-language tech outlets that belong in a cross-outlet comparison
and were missing from that list.

## Measurement status: nothing here was fetched

Read this before using any number in this file.

T09 asks for scores taken from live fetches. The session that compiled
this ran behind an organization egress proxy that denied every candidate
host. All 42 feed URLs were probed with `curl`; all 42 returned exit 56,
`CONNECT tunnel failed, response 403`, and the proxy logged
`connect_rejected` for each host. The WebFetch path returned
`EGRESS_BLOCKED` for the same hosts. github.com and pypi.org are
reachable; no news domain is.

So:

- No entry count, items-per-day figure, HTTP status, lede word count or
  link-cleanliness value was measured. Every such field in
  `config/outlets.candidates.yaml` is `null` and `measured: false`.
- The feed URLs are the outlets' documented or conventional feed paths.
  None was confirmed to resolve. `documented` means the outlet publishes
  a feeds page or advertises the URL in its markup; `convention` means it
  is the platform default, usually WordPress `/feed/`; `search` means it
  came from a web search and nothing else.
- The scores below are priors: editorial judgment about overlap, cadence
  and known feed behaviour. They are not measurements, and the YAML marks
  every one of them `score_basis: prior`.
- The acceptance criterion "the recommended set has no outlet whose feed
  is title-only" is therefore asserted, not verified. T10 is the gate:
  `nc feeds check` must confirm it before `config/outlets.yaml` is
  written, and any recommended outlet whose feed turns out to be
  title-only is dropped there, not here.

## How to re-measure

From a session or a runner with outbound HTTPS to the news domains:

```
nc feeds check --candidates config/outlets.candidates.yaml
```

That command is T10's deliverable. Until it exists, a throwaway probe
does the same job: fetch each feed URL, count entries, strip HTML from
the summary and content fields, compare the result with the title,
report the median word count, derive items per day from the published
timestamps, and look for `utm_` and other tracking parameters in the
entry links. Keep such probes out of the repository; `feedparser` enters
`pyproject.toml` at T11, not before.

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

Maximum 60.

`lede` carries the highest weight and is also a hard gate: an outlet
whose feed is title-only cannot enter the recommended set at any score.
The reason is the product. Every claim and every discrepancy on the site
carries a verbatim quote from an item's title or lede, and v1 never
reads article bodies. A title-only feed gives the analysis step one
sentence per item, so there is nothing to compare beyond the headline
wording, and the outlet joins a cluster without contributing evidence.
Overlap is weighted next because a cluster is only emitted with two or
more outlets: an excellent feed nobody overlaps with produces no
clusters at all. Reachability, link hygiene and terms are one point each
because they are fixable or binary — a redirector can be followed,
tracking parameters are stripped at T11 anyway, and terms are a go or
no-go for the owner rather than a score.

The `terms` column is the weakest in the table: no robots.txt and no
terms page was read for any candidate, every YAML entry records
`terms: not_checked`, and none of this is legal advice.

## Scoring table

Sorted by score. Every candidate has a score and a one-line reason.
The score columns are the criteria keys above; `set` marks membership of
the recommended starting set.

| outlet | L | O | V | R | C | T | score | set | reason |
|---|--:|--:|--:|--:|--:|--:|--:|---|---|
| `theverge` | 5 | 5 | 5 | 5 | 4 | 3 | 57 | yes | highest-volume generalist, overlaps with everything else |
| `arstechnica` | 5 | 5 | 4 | 5 | 4 | 3 | 55 | yes | broad daily coverage, entries carry a real summary paragraph |
| `theregister` | 5 | 4 | 4 | 5 | 5 | 4 | 54 | yes | enterprise and security with a distinctly different framing |
| `engadget` | 4 | 5 | 4 | 4 | 3 | 3 | 49 | yes | consumer and platform news, same beats as Verge and Ars |
| `techcrunch` | 4 | 5 | 4 | 4 | 3 | 3 | 49 | yes | startup, funding and AI beat that the others follow same-day |
| `theguardian` | 4 | 4 | 3 | 5 | 5 | 4 | 48 | yes | general-press framing of the stories the trade press runs |
| `bleepingcomputer` | 4 | 4 | 3 | 3 | 4 | 3 | 44 | yes | breach and malware stories that Register and ZDNET also run |
| `wired` | 4 | 4 | 3 | 4 | 3 | 2 | 43 | yes | mainstream-analytical framing of the same platform stories |
| `tomshardware` | 4 | 3 | 4 | 4 | 3 | 2 | 42 | yes | silicon and hardware stories shared with Ars and The Register |
| `bbc-technology` | 3 | 4 | 2 | 5 | 5 | 3 | 41 | yes | low volume but only covers stories everyone else covers |
| `404media` | 5 | 2 | 1 | 4 | 5 | 3 | 40 | — | excellent feed, too little volume to cluster reliably |
| `nytimes-technology` | 4 | 4 | 2 | 4 | 3 | 1 | 40 | — | good feed, but the terms of service restrict reuse |
| `zdnet` | 3 | 4 | 4 | 4 | 2 | 2 | 40 | yes | wide overlap, but the feed carries deals and affiliate posts |
| `cnet` | 3 | 4 | 4 | 3 | 2 | 2 | 39 | — | consumer overlap is real, feed is dominated by buying guides |
| `techdirt` | 4 | 2 | 2 | 4 | 4 | 5 | 39 | — | commentary on the news rather than reporting of it |
| `venturebeat` | 4 | 3 | 2 | 4 | 3 | 2 | 38 | — | real AI and enterprise overlap, thin daily volume |
| `androidauthority` | 4 | 2 | 4 | 3 | 2 | 2 | 37 | — | Android vertical, overlaps only inside that niche |
| `macrumors` | 4 | 2 | 3 | 4 | 2 | 2 | 36 | — | Apple vertical, feed served through a redirector |
| `neowin` | 4 | 2 | 3 | 3 | 3 | 2 | 36 | — | solid feed, mostly Microsoft-adjacent, narrow overlap |
| `platformer` | 5 | 1 | 1 | 4 | 5 | 2 | 36 | — | newsletter cadence, a few posts a week, no cluster partners |
| `9to5mac` | 4 | 2 | 3 | 3 | 2 | 2 | 35 | — | Apple vertical, overlaps only inside that niche |
| `gizmodo` | 3 | 3 | 3 | 3 | 2 | 2 | 34 | — | newsroom churn and AI-written articles weaken it as a baseline |
| `pcmag` | 3 | 3 | 3 | 3 | 2 | 2 | 34 | — | reviews-first mix, limited breaking-news overlap |
| `cnbc` | 3 | 3 | 3 | 3 | 2 | 1 | 33 | — | business framing, opaque feed endpoint, unclear terms |
| `digitaltrends` | 3 | 2 | 4 | 3 | 2 | 2 | 33 | — | high volume but mostly reviews, deals and how-to |
| `slashdot` | 4 | 1 | 3 | 4 | 1 | 3 | 33 | — | aggregator: its lede quotes other outlets, so it double-counts |
| `axios-technology` | 3 | 3 | 2 | 3 | 2 | 2 | 32 | — | short brevity ledes and an undocumented feed endpoint |
| `thehackernews` | 4 | 2 | 2 | 3 | 1 | 2 | 32 | — | security-only overlap and FeedBurner link wrapping |
| `mashable` | 3 | 2 | 3 | 3 | 2 | 2 | 31 | — | drifted to entertainment and deals, weak news overlap |
| `thenextweb` | 3 | 2 | 2 | 3 | 3 | 2 | 30 | — | output shrank after the FT acquisition, European-events focus |
| `techrepublic` | 3 | 2 | 2 | 3 | 2 | 2 | 29 | — | B2B how-to and research, few same-day news overlaps |
| `businessinsider-tech` | 2 | 3 | 3 | 2 | 2 | 1 | 28 | — | hard paywall and truncated feed entries |
| `digit-fyi` | 3 | 1 | 1 | 3 | 3 | 2 | 25 | — | Scottish regional B2B and events, no global story overlap |
| `forbes` | 2 | 2 | 3 | 2 | 2 | 1 | 25 | — | contributor network, low and uneven signal |
| `inverse` | 3 | 1 | 2 | 2 | 2 | 1 | 24 | — | pivoted to science, gaming and culture after the BDG changes |
| `inquisitr` | 2 | 1 | 3 | 2 | 1 | 1 | 21 | — | celebrity and clickbait aggregation, barely tech at all |
| `qz` | 2 | 1 | 2 | 3 | 2 | 1 | 21 | — | AI-written articles after the 2025 sale, not a usable baseline |
| `techtimes` | 2 | 1 | 3 | 2 | 1 | 1 | 21 | — | rewrites other outlets, adds no independent claim |
| `inc` | 2 | 1 | 2 | 2 | 2 | 1 | 20 | — | small-business advice, not tech news |
| `technicalhint` | 1 | 0 | 1 | 1 | 1 | 1 | 9 | — | marginal SEO blog with no newsroom |
| `recode` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | — | brand retired into Vox.com in 2023, no feed of its own |
| `reuters` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | — | public RSS withdrawn around 2020, syndication is a paid product |

## Feed URLs

Unverified, see the measurement status section. `kind` is `site` for an
outlet-wide feed and `section` where a section feed is the sane choice.

| outlet | feed url | kind, how found |
|---|---|---|
| `theverge` | `https://www.theverge.com/rss/index.xml` | site, documented |
| `arstechnica` | `https://arstechnica.com/feed/` | site, documented |
| `theregister` | `https://www.theregister.com/headlines.atom` | site, documented |
| `engadget` | `https://www.engadget.com/rss.xml` | site, documented |
| `techcrunch` | `https://techcrunch.com/feed/` | site, convention |
| `theguardian` | `https://www.theguardian.com/uk/technology/rss` | section, documented |
| `bleepingcomputer` | `https://www.bleepingcomputer.com/feed/` | site, convention |
| `wired` | `https://www.wired.com/feed/rss` | site, documented |
| `tomshardware` | `https://www.tomshardware.com/feeds/all` | site, documented |
| `bbc-technology` | `https://feeds.bbci.co.uk/news/technology/rss.xml` | section, documented |
| `404media` | `https://www.404media.co/rss/` | site, documented |
| `nytimes-technology` | `https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml` | section, documented |
| `zdnet` | `https://www.zdnet.com/news/rss.xml` | section, documented |
| `cnet` | `https://www.cnet.com/rss/news/` | section, documented |
| `techdirt` | `https://www.techdirt.com/feed/` | site, convention |
| `venturebeat` | `https://venturebeat.com/feed/` | site, convention |
| `androidauthority` | `https://www.androidauthority.com/feed/` | site, convention |
| `macrumors` | `https://feeds.macrumors.com/MacRumors-All` | site, documented |
| `neowin` | `https://www.neowin.net/news/rss/` | site, documented |
| `platformer` | `https://www.platformer.news/rss/` | site, documented |
| `9to5mac` | `https://9to5mac.com/feed/` | site, convention |
| `gizmodo` | `https://gizmodo.com/feed` | site, convention |
| `pcmag` | `https://www.pcmag.com/feeds/rss/latest` | site, documented |
| `cnbc` | `https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss25&id=19854910` | section, documented |
| `digitaltrends` | `https://www.digitaltrends.com/feed/` | site, convention |
| `slashdot` | `https://rss.slashdot.org/Slashdot/slashdotMain` | site, documented |
| `axios-technology` | `https://api.axios.com/feed/technology` | section, search |
| `thehackernews` | `https://feeds.feedburner.com/TheHackersNews` | site, documented |
| `mashable` | `https://mashable.com/feeds/rss/all` | site, documented |
| `thenextweb` | `https://thenextweb.com/feed` | site, convention |
| `techrepublic` | `https://www.techrepublic.com/rssfeeds/articles/` | site, documented |
| `businessinsider-tech` | `https://www.businessinsider.com/sai/rss` | section, documented |
| `digit-fyi` | `https://www.digit.fyi/feed/` | site, convention |
| `forbes` | `https://www.forbes.com/innovation/feed2/` | section, documented |
| `inverse` | `https://www.inverse.com/rss` | site, convention |
| `inquisitr` | `https://www.inquisitr.com/feed/` | site, convention |
| `qz` | `https://qz.com/rss` | site, convention |
| `techtimes` | `https://www.techtimes.com/rss/sections/tech.xml` | section, documented |
| `inc` | `https://www.inc.com/rss/` | site, convention |
| `technicalhint` | `https://www.technicalhint.com/feed/` | site, convention |
| `recode` | none | no public feed |
| `reuters` | none | no public feed |

## Recommended starting set

11 outlets, inside the 8 to 12 the task asks for.

This is deliberately not the top 11 rows of the table. The score ranks
a feed; the set has to cluster. 404 Media, the New York Times, Techdirt
and CNET all score at or above the bottom of the chosen set and are still
out — on volume, terms, role and noise respectively — while ZDNET is in
at 40 because it overlaps with nearly everything else here. Where score
and overlap disagree, overlap wins, because a cluster is only emitted
when two outlets carry the same story.

- **Ars Technica** — the widest daily generalist here, and its entries
  carry a full summary paragraph, the most useful lede shape we can get.
- **The Verge** — highest volume of the generalists and the outlet most
  likely to be the second member of an arbitrary cluster.
- **Engadget** — the same consumer and platform beats as Ars and The
  Verge, with a different house framing of the same announcements.
- **TechCrunch** — funding, startup and AI stories the rest of the set
  picks up within a day; the most obvious gap in the old list.
- **The Register** — enterprise, silicon and security written from a
  deliberately different angle, which is where discrepancies come from.
- **BleepingComputer** — breaches and malware in detail; the specialist
  that supplies the numbers the generalists then round off.
- **WIRED** — analytical framing of the same platform stories, and the
  outlet most likely to disagree about significance rather than fact.
- **ZDNET** — broad enterprise and consumer overlap, included with a
  caveat, see below.
- **Tom's Hardware** — GPUs, chips and supply chain, overlapping with
  Ars and The Register on exactly the stories that carry hard numbers.
- **The Guardian (Technology)** — general-press treatment of the trade
  press's stories, and the section feed keeps its other desks out.
- **BBC News (Technology)** — low volume by design: it covers only
  stories everyone else also covers, so nearly every item should cluster.

### Will they actually overlap

Yes, with a caveat about the tails. Seven of the 11 — Ars, The Verge,
Engadget, TechCrunch, WIRED, ZDNET and the Guardian — run the same daily
platform stories: Apple, Google, Meta, OpenAI, Microsoft, Nvidia,
antitrust, EU and US regulation. Any of those should be a two-outlet
cluster most days. Security stories bind The Register, BleepingComputer
and ZDNET; hardware and silicon bind Tom's Hardware, Ars and The
Register; the largest stories pull in the Guardian and the BBC. The set
is three overlapping communities sharing the generalists, not 11
independent streams.

The tails will not cluster. Tom's Hardware component reviews,
BleepingComputer's smaller advisories and Guardian technology features
have no partner in the set and will be dropped as singletons. That is
the design working, not a defect, but it means items per day is not
clusters per day and the ratio is unknown until ingest runs.

Two risks specific to this set. ZDNET is here for overlap and is the
weakest on lede quality — deals and affiliate posts in the feed, and a
history of thin descriptions — so it is the first outlet T10 should
measure and the first to drop if the gate fails. BleepingComputer sits
behind bot protection, so the second thing T10 should confirm is that
its feed answers a plain client at all.

## Rejected and why

By category. The per-row reasons are in the scoring table.

**Dead or gone.** `recode`, brand retired into Vox.com in 2023, with no
feed of its own; `reuters`, whose public RSS was withdrawn around 2020
and whose syndication is now a paid product. Both are scored 0 with a
reason rather than dropped from the table.

**Not an independent voice.** `qz` — Quartz was sold in 2025, its
editorial staff cut, and it published AI-written articles; checking
another outlet's claim against generated text tells us nothing. Its
score is about trustworthiness, not about the feed. `slashdot` — an
aggregator whose entry text quotes the outlets we already ingest, so it
would re-enter their claims under a second outlet name and inflate every
cluster it touches. `techtimes`, `inquisitr` and `technicalhint` —
rewrite and SEO operations with no newsroom.

**Wrong scope.** `inc` (small-business advice), `inverse` (science and
culture since the BDG changes), `digit-fyi` (Scottish regional B2B and
events, alive and publishing but the wrong beat entirely), `forbes`
(contributor network, uneven signal), `techrepublic` (B2B how-to and
research), `techdirt` (commentary on the news rather than reporting of
it, and worth revisiting later as a deliberate framing counterweight).

**Right outlet, wrong shape for v1.** `404media` and `platformer`
publish a handful of items a week: excellent feeds with nobody to
cluster against inside a four-day window. `businessinsider-tech` is hard
paywalled with truncated entries.

**Verticals.** `9to5mac`, `macrumors`, `androidauthority`, `neowin` and
`thehackernews` have strong feeds but each overlaps only inside its own
niche. Worth revisiting if the site grows per-topic sections.

**Close calls.** `venturebeat` is the first reserve: real AI and
enterprise overlap with TechCrunch, rejected on daily volume and a
sponsored-post mix alone. `cnet` is the second: genuine consumer overlap
buried under buying guides. `gizmodo` was excluded on newsroom churn and
the AI-written posts its previous owner ran in 2025, which is again a
judgment about trustworthiness rather than about the feed, and it is the
closest of the three. `nytimes-technology` scores well and was excluded
on terms alone; if the owner is comfortable with the New York Times
terms of service it is the strongest single addition available.
`axios-technology` was excluded because its ledes are short by editorial
policy — exactly the risk the lede gate exists to catch — and because
its feed endpoint is undocumented.

## Caveats

- Nothing was fetched. Every number in this file is a prior, and the
  whole table needs re-running once a session has egress. See the
  measurement status section for the evidence.
- Feed URLs may have moved. Several are conventions rather than
  documented paths. Where one 404s, discover the real URL from the
  homepage `<link rel="alternate" type="application/rss+xml">` element
  and record it in the YAML.
- Terms were read for nobody. Before launch the owner should at least
  read the terms of the recommended set and confirm that quoting a
  headline and lede with attribution and a link is acceptable.
- Lede judgments are the softest part of the table. Feeds change shape
  without notice and several of these outlets have moved between full
  text, summary and title-only over the years. The gate has to be
  enforced by measurement at T10, not by this file.
- Posting frequency is unknown for every candidate, so the volume scores
  are estimates of editorial output, not counts from timestamps. That
  matters most for the reserves, whose rejection turns on volume.
- Overlap is an assertion about editorial beats, not a measurement. It
  can only be confirmed after ingest and clustering have run for a few
  days. If the recommended set produces too few clusters, add
  VentureBeat and then CNET before widening any other criterion.
- One feed per outlet was assumed throughout; section feeds are used for
  the Guardian, the BBC, ZDNET and the rejected general-news candidates.
  If a site-wide feed proves too noisy at T10, prefer its section feed
  over dropping the outlet.
- 42 candidates were scored. None was fetched.
