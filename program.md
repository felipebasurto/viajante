# program.md

Experiment protocol for viajante. Humans edit this file to steer.
A looping agent reads it, runs **one** keep-or-revert experiment, then
stops. There is no loop script in this repo.

## Goal

Make viajante faster without breaking the public contract. The score is
offline wall time. Lower is better.

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
   strictly lower. Do not average runs into a fake score.
7. **Revert** the experiment files if the gate fails or `score_ms` is
   worse or equal. Do not revert unrelated work on the branch.
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

The bench has no flags to skip tests, subset the corpus, or change
`--top`. Product defaults stay `DEFAULT_TOP = 8` and
`DEFAULT_BAGGAGE_BUFFER_EUR = 70`.

## Hard to game

- Do not skip tests, shrink `tests/bench/`, or drop a fixture from
  `manifest.json`.
- Do not add empty fixtures to “win”. New files belong there only when
  they are real owned parse cases already covered by tests.
- Do not lower `--top` or the baggage buffer to make ranking cheaper.
- Do not delete tests, skip ruff, or stub parsers to go faster.
- Do not count `sweep_ms` or live Google in the score.

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

- Late-evening arrival clocks can still come out null on some compact
  shapes.
- Packaged `--trip rt` can miss return legs.
- Google Hotels `--rooms` is ignored.

Touch them only if the bench still passes and `score_ms` does not get
worse, or if you add a failing test first and the score stays honest.

## After the run

- Write the recorded `score_ms` (and `tests_ms` / `parse_ms`) in the PR
  body next to the baseline.
- If it is a win, say so and leave baseline update for the human merge.
- If it is a loss or a fail, the PR should show the revert, or not exist.
- Stop. The scheduler starts the next experiment, not this checkout.
