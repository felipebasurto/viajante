# Prompt battery

Graded prompts for viajante, ordered smoke → easy → medium → hard → insane →
brutal → savage. This is the **quality** contract. It is not `score_ms`.

Smoke→brutal prompts are **English**, except `i18n.jsonl` (brutal tier).
Savage may be other languages (including Yoruba, Swahili, Amharic, Georgian,
Basque, Icelandic, Khmer, Tamil, Quechua, Welsh, Zulu, Mongolian). The plan
must still emit English search queries and English flight fetch locale
(`hl=en` / `en` / `en-US`). Do not add hotel or flight evidence regexes in
those languages; English cards generalize. Origins and destinations are
international (North America, LatAm, Europe, Africa, Middle East, South
Asia, East Asia, SE Asia, Oceania). No city is the implied home hub.

`viajante bench` (no flags) stays the weekday keep-or-revert loop: unittest +
ruff + owned `tests/bench/` parse. Do not delete these prompts to “win”
that loop.

## Run

```bash
uv run viajante bench --prompts
# or: VIAJANTE_BENCH_PROMPTS=1 uv run viajante bench
```

Deterministic cases are offline. They check the owned prompt→query planner
against the CLI boundary (IATA, route grammar, trip kind, occupancy).

LLM-as-judge (open-ended / insane / brutal / savage cases) is opt-in. There
is no single correct answer: DeepSeek scores quality from 1 to 100
(constraints, sane routing, honesty about what viajante can/cannot do, no
invented bookings). Each scored row records `score_1_100` plus a one-line
reason.

```bash
VIAJANTE_BENCH_JUDGE=1 uv run viajante bench --prompts
```

The key is `DEEPSEEK_API_KEY` in the environment (or `VIAJANTE_JUDGE_KEY`
as override). Optional `DEEPSEEK_MODEL` / `VIAJANTE_JUDGE_MODEL` default
to `deepseek-chat`. Endpoint is
`https://api.deepseek.com/v1/chat/completions`. Never hardcode a key.
Never put a key in source or a PR. A local `.env` is gitignored.

If both `DEEPSEEK_API_KEY` and `VIAJANTE_JUDGE_KEY` are unset, those
cases print `judge: skip`. Do not invent a score. Judge latency is never
`score_ms`. Each weekday row prints `plan_ms`; the summary prints
`plan_p50_ms` / `plan_p90_ms` / `plan_max_ms`. That is not the keep
metric. Optional live find-flights timer (off by default):

```bash
VIAJANTE_BENCH_SWEEP=1 uv run viajante bench --prompts
# same: uv run viajante bench --prompts --timeit-sweep
```

Caps at 8 planned IATA+date flight queries and uses the MCP
`search_flights` HTTP sweep path (`fetch=sweep`, no Playwright). If sweep
is off, stdout prints `sweep_ms:` blank. Live Google is never
`judge_mean` or `score_ms`. Live Google on the speed bench is still
`VIAJANTE_BENCH_LIVE=1` only; that extra is also not the score.

## Files

Listed in `manifest.json`. Empty or dropped files fail the prompts run,
same spirit as the parse corpus floors. `holdout.jsonl` is **not** listed;
see `holdout.md`. Looping speed agents must not open it to pick work.

Each JSONL row: `id`, `tier`, `lang` (ISO 639-1; smoke→insane: `en`;
i18n/savage may be another language), `prompt`, `expect`, `judge`
(`deterministic` or `llm`). `expect` is the checkable contract (origin IATA,
dest, dates, trip kind, max stops, adults, cabin, refuse reasons).
Non-English prompts must plan English fetch `locale` / city strings.

## Tiers

- **smoke / easy**: one city pair, one date (BOS–LHR, NRT–ICN, GRU–SCL, …),
  airports lookup, cheapest Friday, invalid IATA, missing date.
- **medium**: packaged RT vs two one-ways, open jaw, max-stops, cabin,
  adults, explore, dates calendar, hotel nights / rooms.
- **hard**: flexible dates, flex around-date window, layover caps, night-arrival clocks, packaged
  RT return legs, rooms occupancy, refuse booking/trains/cars, destinations
  and prices only.
- **insane**: multi-hop fantasy that still has a contract, including
  Halifax → Fiji via two European airports, one sub-Saharan, one Indian,
  one Chinese, and New Zealand. Other insane rows start in Halifax,
  Vancouver, Boston, Singapore, Auckland, Cairo, São Paulo — not Madrid.
- **brutal**: one prompt stacks several product surfaces (hotels occupancy,
  named airports such as LHR vs LGW / EWR vs JFK, cabin only with real
  collocation, time windows, baggage, work-back-by, layover/stop caps,
  flights+hotels together, refuse trains/cars/checkout). Includes a
  handful of unsatisfiable stacks (exclusive airports, nonstop+layover,
  first+economy, bags both ways, weekday-no-fly+Monday depart, same-day
  clocks, IST overnight required and forbidden, return-before-out,
  book-and-don't-book, secret dests with invented fares) so the planner
  and judge can miss. No invented fares or hotel prices in `expect`.
  Smoke→insane stay intact.
- **i18n** (`i18n.jsonl`, brutal tier): non-English prompts (French, German,
  Japanese) whose planned fetch query and `locale` are English. Not a
  rewrite of frozen smoke→insane rows.
- **savage**: new hardness only. Non-English user prompts (rare languages
  plus a few FR/DE/JA/PT/AR/KO), timezone / IDL / `work_back_by` vs local
  clocks that look possible and are not, packaged RT vs two one-ways,
  hotel+flight coupling (rooms vs adults, check-in vs inbound clock), and
  subtle contradictions that look satisfiable. Not copies of
  `brutal-impossible-*`. The planner should refuse or keep every constraint.
  No invented fares or hotel prices in `expect`. Do not rewrite
  smoke→brutal or holdout to make this slice.

Humans may later gate easy-tier failures. Do not silently gate insane/llm,
brutal/llm, or savage/llm cases.
