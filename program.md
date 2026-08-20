# program.md

Experiment protocol for viajante. Humans edit this file to steer.
A looping agent reads it, runs **one** keep-or-revert experiment, then
stops. There is no loop script in this repo.

Before choosing a hypothesis, read `bench-history.md` (one screen).
Do not redo a listed keep or loss. Do not open `tests/prompts/holdout.jsonl`.
After a keep or revert, append one row to the speed table with this host's
real numbers. Never invent a figure.

## Goal

Make viajante faster without breaking the public contract. The score is
offline wall time. Lower is better.

The graded prompt battery (`viajante bench --prompts`) is the **quality**
contract. It is not `score_ms`. The weekday speed loop still uses
`viajante bench` with no flags. A looping agent may not delete
`tests/prompts/` (or drop cases below the floors) to “win”. Humans may
later make easy-tier failures part of the gate; do not silently do that
for insane/llm cases.

## One experiment

1. Read this file, `AGENTS.md`, and the current `bench-baseline.json`.
2. Pick **one** small hypothesis. Examples that fit this tree:
   - fewer allocations or copies in `parse_shopping_body` / `_first_wrb_data`
   - cheaper `wrb.fr` walk or itinerary collect
   - less work in `parse_flight_cards` / `parse_http_flight_cards`
   - smaller shopping RPC encode (`build_shopping_request` / TFS)
   - CLI / import startup that the unittest suite actually pays for
3. Change only the files that test that hypothesis. Keep the diff small.
4. Run the bench from the checkout root:

   ```bash
   uv run viajante bench
   ```

5. Parse the tiny stdout block:

   ```
   gate: ok
   tests_ms: 531
   parse_ms: 12
   score_ms: 543
   ```

6. **Keep** the change only if `gate: ok` **and** `score_ms` is **strictly
   lower** than `score_ms` in `bench-baseline.json`. `score_ms` is wall
   time on this machine. A few tens of ms can be noise; if the delta is
   that small, run the bench once more and keep only if both runs are
   strictly lower. Do not average runs into a fake score. Then apply
   **Anti-maxxing** (cold `viajante --help`).
7. **Revert** the experiment files if the gate fails, `score_ms` is worse
   or equal, or cold `--help` got slower. Do not revert unrelated work.
8. Open or update a pull request. **Never merge to main.** A human merges.
   Update `bench-baseline.json` only in a winning PR, and only when a
   human is ready to merge that win. Do not rewrite the baseline to hide
   a loss.

If `gate: fail`, there is no score. Do not invent one. Do not keep the
change.

## Bench contract

`viajante bench` is offline. No Chromium. No live Google unless
`VIAJANTE_BENCH_LIVE=1` (off by default). That optional path may print
`sweep_ms` as extra. `sweep_ms` is **never** the keep/revert score.

Gate (must pass or exit non-zero):

- `python -m unittest discover -s tests`
- `ruff check src tests`
- `ruff format --check src tests`

Metric (one number, lower is better):

- `score_ms` = wall ms of the unittest suite + wall ms of the checked-in
  corpus in `tests/bench/` (owned compact-shopping / `wrb.fr` / HTML card
  parse). Not a network call.

The bench has no flags to skip tests, subset the parse corpus, or change
`--top`. Product defaults stay `DEFAULT_TOP = 8` and
`DEFAULT_BAGGAGE_BUFFER_EUR = 70`. `--prompts` is a different command:
the quality battery, never mixed into `score_ms`.

## Prompt battery (quality, not score)

```bash
uv run viajante bench --prompts
```

Checked-in corpus: `tests/prompts/` (JSONL + README, smoke → brutal).
Prompts are English. Origins are international; no city is the implied
home hub. Named outbound+return without "two one-way" / "without --trip rt"
/ "separate tickets" is packaged `--trip rt`; two one-ways only when the
user asked for that split. DeepSeek and the pre-judge harness zero a
plan that splits a packaged RT or packages `--trip rt` when the prompt
asked for two one-ways. Do not invent fares. Default run is offline deterministic cases. LLM-as-judge is
`VIAJANTE_BENCH_JUDGE=1` with DeepSeek `deepseek-chat` (`DEEPSEEK_API_KEY`
outside the repo, or `VIAJANTE_JUDGE_KEY` as override; optional
`DEEPSEEK_MODEL` / `VIAJANTE_JUDGE_MODEL`). There is no single correct
answer: record `score_1_100` plus a one-line reason, not pass/fail as
the only output. Unset key prints `judge: skip`; do not invent a score.
Judge wall time and live scrapes are never `score_ms`.
Empty or dropped prompt files fail the prompts run. Holdout is **not**
in that weekday battery. Do not add `holdout.jsonl` to `manifest.json`.
Operator-only: `uv run viajante bench --prompts --holdout`.

## Anti-maxxing

`score_ms` is unittest + the owned parse corpus. A keep that only moves
import taxes or unittest-only caches is suspect. After BEFORE/AFTER
`uv run viajante bench`, also time a COLD CLI on this host:
`uv run viajante --help` twice, discard the first, keep the second wall
ms. If that cold help is strictly worse than this host's pre-change cold
help, REVERT even if `score_ms` won. Record both numbers in the PR.

Do not skip tests, shrink `tests/bench/` or `tests/prompts/`, weaken MCP
coverage, drop a fixture from `manifest.json`, add empty fixtures, lower
`--top` or the baggage buffer, or stub parsers to win `score_ms`. Do not
count `sweep_ms`, live Google, or LLM-judge latency.

Do not edit `JUDGE_SYSTEM_PROMPT`, `invention_reason`, or existing prompt
`expect` fields to raise the 1–100 mean. Invented price/route stays
automatic 0.

Existing smoke→insane rows are frozen except to fix a real planner bug
(wrong IATA, dropped dests). New hardness goes in new files
(brutal/holdout), not by rewriting old prompts to be easier.

Looping speed agents read this file and `bench-history.md` only. They
must not open `tests/prompts/holdout.jsonl` when choosing a hypothesis.
Holdout is the operator overfitting check, not the weekday 90.

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

Touch leftover holes only if the bench still passes and `score_ms` does
not get worse, or if you add a failing test first and the score stays
honest.

## After the run

- Write the recorded `score_ms` (and `tests_ms` / `parse_ms`) in the PR
  body next to the baseline. For a speed keep, also record cold
  `viajante --help` ms (pre-change vs second run), per Anti-maxxing.
- If it is a win, say so and leave baseline update for the human merge.
- If it is a loss or a fail, the PR should show the revert, or not exist.
- Stop. The scheduler starts the next experiment, not this checkout.
