---
name: judge-pairs
description: Decide whether borderline article pairs (pending-pairs/*.json in the data root) report the same event, writing one judgment file per pair to judgments/<pair_id>.json. Use when asked to judge pairs, work the judge queue, or run the nightly judge step.
---

# Judge borderline pairs

Two articles scored close enough to be worth asking about, and a similarity
score cannot tell "same story" from "same topic". You decide, from the
outlet, title and lede alone.

Read `prompts/judge.md` first and follow it exactly. It is the same prompt
the API backend uses, and `nc bench-judge` only means something if both
answer the same question.

## Procedure

1. Run `nc judge` to list the pairs awaiting an answer, highest score
   first. Answer them in that order.
2. For each pair, write `<data root>/judgments/<pair_id>.json` — one JSON
   object, exactly these fields:

   ```json
   {"backend":"claude_code","item_id_a":"...","item_id_b":"...",
    "judged_at":"2026-09-17T04:00:00Z","model":"claude-sonnet-5",
    "pair_id":"...","reason":"...","same_story":true}
   ```

   - `pair_id`, `item_id_a`, `item_id_b`: copy from the question, unchanged.
     The validator checks them against the pending-pair file and rejects a
     judgment whose ids do not match.
   - `same_story`: a JSON boolean. Never the string `"true"` — the loader
     rejects it rather than risk reading it as truthy.
   - `reason`: one sentence, 10 to 300 characters, per `prompts/judge.md`.
   - `backend`: `claude_code`.
   - `model`: the model you are running as, if you know it; otherwise the
     `claude_code_model` in `config/judge.yaml`.
   - `judged_at`: UTC, `YYYY-MM-DDTHH:MM:SSZ`.
3. After every ten files, run `nc judge --validate`. It prints every
   rejected judgment and why. Fix each one and validate again. Give up on a
   pair after two retries, delete that judgment file and move on — an
   unjudged pair is simply asked again tomorrow.
4. Stop when `nc judge` shows nothing, or when you have done the run's
   limit (`max_pairs_per_run` in `config/judge.yaml`).

## Rules

- Answer no when you are unsure. Nothing links without a yes, so a no costs
  one missed pairing, while a wrong yes merges two unrelated stories and
  every claim and discrepancy built on that merge is then wrong too.
- Never rewrite a judgment file that already exists. A judgment records what
  a backend said at a moment; a run that re-judges nothing writes nothing,
  which is what keeps the data repository quiet under a cron.
- Never edit anything outside `<data root>/judgments/` — not pending-pair
  files, not clusters, and never `labels/pairs.jsonl`. Those labels are the
  human answers `nc bench-judge` scores you against; writing to them would
  destroy the only independent check on this step.
- Do not look for the similarity score, and do not go and read the articles.
  You judge the text in the pair file, nothing else.
