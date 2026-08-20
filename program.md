# program.md

Experiment protocol for viajante. Humans edit this file to steer.
A looping agent reads it, runs **one** keep-or-revert experiment, then
stops. There is no loop script in this repo.

Before choosing a hypothesis, read `bench-history.md` (one screen).
Do not redo a listed keep or loss. Do not open `tests/prompts/holdout.jsonl`.
After a keep or revert, record this host's real `judge_mean`. Never invent
a figure. Holdout is a **human veto**, not the weekday keep.

## Goal

Raise weekday `judge_mean` without breaking the public contract. **KEEP
METRIC** is `judge_mean` (arithmetic mean of `score_1_100` on `judge=llm`
scored rows). Higher is better. `score_ms` is **not** the keep.

Gate = suite + `fail: 0`. A looping agent
may not delete `tests/prompts/` (or drop cases below the floors) to “win”.
Do not edit the judge or pad easy prompts. Humans may later make easy-tier
failures part of the gate; do not silently do that for insane/llm cases.

## One experiment

1. Read this file, `AGENTS.md`, and the current `bench-baseline.json`.
2. Pick **one** small hypothesis. Examples that fit this tree:
   - planner follows a named contract the judge already scores
   - packaged `--trip rt` vs two one-ways the user asked for
   - refuse rest-of-trip / trains / cars without inventing a fare
   - occupancy, cabin, or dests the prompt named
3. Change only the files that test that hypothesis. Keep the diff small.
   Do not edit `JUDGE_SYSTEM_PROMPT`, `invention_reason`, or existing
   prompt `expect` fields. Do not rewrite old easy prompts to be easier.
4. Run the gate and the keep metric from the checkout root:

   ```bash
   uv run viajante bench
   VIAJANTE_BENCH_JUDGE=1 uv run viajante bench --prompts
   ```

5. Parse the tiny stdout blocks:

   ```
   gate: ok
   ```

   ```
   fail: 0
   judge: ran
   judge_mean: 89.0
   ```

   If the judge skipped or `judge_mean:` is blank, there is no keep
   metric. Do not invent one.

6. **Keep** the change only if the gate is ok, `fail: 0`, **and**
   `judge_mean` is **strictly higher** than the last kept `judge_mean` on
   this host. If the delta is **< 3**, run the prompts bench once more
   and keep only if **both** runs are strictly up. Do not average runs
   into a fake mean. Then apply **Anti-maxxing**.
7. **Revert** the experiment files if the gate fails, `fail` is not 0,
   `judge_mean` is worse or equal, or Anti-maxxing vetoes. Do not revert
   unrelated work.
8. Open or update a pull request. **Never merge to main.** A human merges.
   Holdout is a human veto after the fact; do not open
   `tests/prompts/holdout.jsonl` to decide keep/revert. Do not rewrite
   `bench-baseline.json` to hide a loss (`score_ms` is not the keep).

If `gate: fail` or the judge skipped, there is no keep metric. Do not
invent one. Do not keep the change.

## Bench contract

`viajante bench` is offline. No Chromium. No live Google unless
`VIAJANTE_BENCH_LIVE=1` (off by default). That optional path may print
`sweep_ms` as extra. `sweep_ms` is **never** the keep/revert score.
`score_ms` is also **not** the keep; it is the speed loop's number only.

Gate (must pass or exit non-zero):

- suite (`viajante bench`: unittest + ruff)
- prompt battery `fail: 0`

KEEP METRIC (one number, higher is better):

- `judge_mean` = arithmetic mean of `score_1_100` on `judge=llm` scored
  rows when the judge ran. Blank / omitted when the judge skipped or
  there are no llm scores. Never invent.

`score_ms` = wall ms of the unittest suite + wall ms of the checked-in
corpus in `tests/bench/` (owned compact-shopping / `wrb.fr` / HTML card
parse). Not a network call. Record it if you like; do not keep on it.

The bench has no flags to skip tests, subset the parse corpus, or change
`--top`. Product defaults stay `DEFAULT_TOP = 8` and
`DEFAULT_BAGGAGE_BUFFER_EUR = 70`. `--prompts` is the quality battery,
never mixed into `score_ms`.

## Prompt battery (quality keep: judge_mean)

```bash
uv run viajante bench --prompts
```

Checked-in corpus: `tests/prompts/` (JSONL + README, smoke → brutal).
User prompts may be any language. Planned Google Flights / Google Hotels /
Booking queries and fetch locale stay English (`en` / `en-US`). Do not grow
card-evidence regexes per language; English cards generalize. Origins are
international; no city is the implied
home hub. Named outbound+return without "two one-way" / "without --trip rt"
/ "separate tickets" is packaged `--trip rt`; two one-ways only when the
user asked for that split. DeepSeek and the pre-judge harness zero a
plan that splits a packaged RT or packages `--trip rt` when the prompt
asked for two one-ways. Do not invent fares. Default run is offline deterministic cases. LLM-as-judge is
`VIAJANTE_BENCH_JUDGE=1` with DeepSeek `deepseek-chat` (`DEEPSEEK_API_KEY`
outside the repo, or `VIAJANTE_JUDGE_KEY` as override; optional
`DEEPSEEK_MODEL` / `VIAJANTE_JUDGE_MODEL`). There is no single correct
answer: record `score_1_100` plus a one-line reason, not pass/fail as
the only output. When the judge ran and `scores[]` is non-empty, stdout
prints `judge_mean:` to one decimal. Unset key prints `judge: skip` and
`judge_mean:` blank; do not invent a score or a mean.
Judge wall time and live scrapes are never `score_ms`.
Empty or dropped prompt files fail the prompts run. Holdout is **not**
in that weekday battery. Do not add `holdout.jsonl` to `manifest.json`.
Operator-only: `uv run viajante bench --prompts --holdout`. A human may
veto a keep after holdout; the agent must not open that file.

## Anti-maxxing

KEEP METRIC is `judge_mean`, not `score_ms`. Do not edit
`JUDGE_SYSTEM_PROMPT`, `invention_reason`, or existing prompt `expect`
fields to raise the 1–100 mean. Do not pad easy prompts. Invented
price/route stays automatic 0.

Existing smoke→insane rows are frozen except to fix a real planner bug
(wrong IATA, dropped dests). New hardness goes in new files
(brutal/holdout), not by rewriting old prompts to be easier.

Do not skip tests, shrink `tests/bench/` or `tests/prompts/`, weaken MCP
coverage, drop a fixture from `manifest.json`, add empty fixtures, lower
`--top` or the baggage buffer, or stub parsers. Do not count `sweep_ms`,
live Google, or LLM-judge latency as the keep.

Looping agents read this file and `bench-history.md` only. They
must not open `tests/prompts/holdout.jsonl` when choosing a hypothesis.
Holdout is the operator overfitting check and a **human veto**, not the
weekday keep.

## Constraints (already in AGENTS.md)

- Keep the repo lightweight. README, AGENTS.md, and `.cursor/skills`
  stay. Tests stay offline.
- No peer-scraper names in source, commits, or PR text.
- Do not shorten detail Playwright delays or add flags that parallelize
  Google Flights or Booking.com.
- Do not add other OTAs or a booking flow.
- Sweep stays HTTP (`curl_cffi`), detail stays Playwright, one public
  JSON contract.
- Private scrapes, personal routes, and browser session files stay out
  of this tree.

## Known leftover holes

These are product bugs. Do **not** “fix” them by deleting features or
tests.

- Packaged `--trip rt` still cannot invent a return leg when the compact
  body only contains the outbound flight. Query `return_date` is not a
  second leg. Wrapped or sibling return flights in that body should parse.

Late-evening compact clocks (including proto3-omitted hour 0) and Google
Hotels `--rooms` occupancy are covered by tests; do not regress them.

Touch leftover holes only if the gate still passes and `judge_mean` does
not get worse, or if you add a failing test first and the mean stays
honest.

## After the run

- Write the recorded `judge_mean` in the PR body next to this host's
  last keep. `score_ms` may be noted; it is not the keep.
- If it is a win, say so. A human merges and may run holdout as veto.
- If it is a loss or a fail, the PR should show the revert, or not exist.
- Stop. The scheduler starts the next experiment, not this checkout.
