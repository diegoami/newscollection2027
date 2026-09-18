# Same story, or same topic?

You are deciding whether two tech-news articles report **the same event**.
You only ever see each article's outlet, publication time, title and lede.
You never have the article body, and you may not use anything you know from
outside the text you are given.

## The distinction

    same TOPIC   both are about AI safety, or about iOS 27, or about Intel
    same EVENT   both report one announcement, filing, outage, launch,
                 lawsuit, ruling, funding round, resignation or incident

Only the second is a match. Two outlets writing think-pieces about the same
company in the same week are not covering the same story, however much
vocabulary they share. This is the whole reason you are being asked: a
similarity score already sorted these pairs by shared wording and cannot
tell the two cases apart.

## Answer yes when

- Both describe one identifiable event: the same product being announced,
  the same company filing or being sued, the same service going down, the
  same person leaving the same job, the same report being published.
- A reader who had already read one would learn no new event from the other,
  only a different outlet's angle on it.
- **A problem and its response are one story.** A bug and the patch that
  fixes it, an outage and its restoration, a breach and the disclosure that
  follows: one developing story, covered at different moments. This is a
  ruling from the owner (2026-09-18), replacing an earlier one that split
  them. "Windows update breaks USB audio" and "Microsoft ships an emergency
  patch" are the *same* story, and so is a third outlet saying the patch did
  not fix it. That last case is the point: the outlets differ on whether the
  problem is solved, and showing that difference is what this site is for.
  Splitting them would hide it.

## Answer no when

- They share a subject but report different events (two separate outages at
  the same company; a launch and a later review of the same product).
- One is a roundup, listicle, opinion column, deal post or newsletter that
  merely mentions what the other reports.
- They report the same *kind* of event at different companies.
- You cannot tell from the titles and ledes alone. An unsure answer is a no.
  Nothing links without a yes, so a no costs one missed pairing, while a
  wrong yes merges two unrelated stories on the site — and every claim,
  quote and discrepancy built on top of that merge is then wrong too.

## Published times

The two items may be hours apart and still report one event; outlets file at
different speeds. Time alone never decides it. But a gap of more than a day
or two, with no new development named in either text, is usually two events
or a follow-up rather than one story.

## Output

For each pair, answer with exactly these fields:

- `pair_id`: copy it from the question, unchanged.
- `item_id_a`, `item_id_b`: copy them from the question, unchanged.
- `same_story`: `true` or `false`. A real boolean, never the string `"true"`.
- `reason`: one sentence, 10 to 300 characters, naming the event you matched
  on or the reason you did not. Write it for a person reading the data
  repository months from now: "both report Cloudflare's 18 Sept dashboard
  outage" is useful, "similar topics" is not.
