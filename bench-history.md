# Loop history (one screen)

Read this before picking a hypothesis. Do **not** redo a listed keep or loss.
Do **not** plot `score_ms` across PRs (different VMs). Only same-host deltas count.
Checked-in `bench-baseline.json` `score_ms` is a **fossil**, not keep. Do not rewrite it.

Full shipped-PR log (cold `--help` transcripts, judged means, speed table):
`docs/archive/2026-09-01-bench-history.md`
(source `bench-history.md`, git blob `51c37cabe34edd476b2e3aeacdadc6043754c84d`).
Git history is enough if the archive is missing.

## Weekday keep (do not invent a mean)

KEEP METRIC is `judge_mean` (mean of `score_1_100` on `judge=llm` scored rows).
Gate = suite + `fail:0`. Keep iff `judge_mean` is **strictly higher** than the last
kept mean on this host; Δ < 3 → second run, both strictly up. Holdout is a
**human veto**. `score_ms` is not the keep. Do not edit `JUDGE_SYSTEM_PROMPT` or
pad easy prompts.

Last **kept** `judge_mean`: **97.9** on `60d9ed4` (26 scored). Last judged **95.7**
on `22da78a` (35 scored, 23 skip) is **not** a keep. Later operator means below
97.9 are also not keeps. Record this host’s real mean in the PR; never invent one.

`bench.py`, `prompt_bench.py`, `tests/bench/`, and `tests/prompts/` are read-only
to the looping agent. Import-only / unittest-cache / FastMCP-off-tests / IATA-regex
work a MCP user cannot feel = **veto**. Cold `viajante --help` worse = **veto**.
Replay / 2×MAD / live `sweep_ms` are not weekday keep.

## Do not redo (shipped)

Planner / honesty / product surfaces already in tree (see archive for PR numbers):
open-jaw pairs; midnight clocks; invented price/route = 0; `typical_eur` calendar
median; IST overnight+via keep both; flex calendar-then-one-shop; `--via` /
`--exclude-via`; around/±N → `intent=flex`; leftover honesty notes; secret dest
quote; bags/carry-on; `work_back_by` / IDL; `via_regions` note; `--price-cap`
local; nearby; occupancy/currency/country on dates/flex/explore; shop post-filters
on explore/trip; `stops_compare` / `google_flights_url` leftover parity;
`--arrive-before` / `--depart-after`; `--exclude-airports` / `--include-airports`;
`--exclude-regions`; overnight on dates/flex/explore; `--sort` on dates/explore;
nonstop+`min_layover` leftover note. Speed keeps #3–#30 and sweep #56: do not
relaunch those import/IATA/Lexbor/MCP-off experiments.

## Honesty leftovers (leave them)

- Compact `--trip rt` with only the outbound in the bytes still has one leg. Do not invent the return.
- Typical is omitted when the calendar is thin or the trip is multi-city. Packaged RT uses the same-stay grid.
- Booking challenge timeout. Untouched.
- Do not shorten Playwright detail delays. No live Google as the reward.
- Never invent a fare, typical, token, or via list.
