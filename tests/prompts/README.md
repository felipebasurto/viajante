# Prompt battery

Graded prompts for viajante, ordered smoke → easy → medium → hard → insane.
This is the **quality** contract. It is not `score_ms`.

All prompts are **English**. Origins and destinations are international
(North America, LatAm, Europe, Africa, Middle East, South Asia, East Asia,
SE Asia, Oceania). No city is the implied home hub.

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

LLM-as-judge (open-ended / insane cases) is opt-in. There is no single
correct answer: DeepSeek scores quality from 1 to 100 (constraints, sane
routing, honesty about what viajante can/cannot do, no invented bookings).
Each scored row records `score_1_100` plus a one-line reason.

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
`score_ms`. Live Google is still `VIAJANTE_BENCH_LIVE=1` on the speed
bench only; this battery does not scrape.

## Files

Listed in `manifest.json`. Empty or dropped files fail the prompts run,
same spirit as the parse corpus floors.

Each JSONL row: `id`, `tier`, `lang` (`en`), `prompt`, `expect`, `judge`
(`deterministic` or `llm`). `expect` is the checkable contract (origin IATA,
dest, dates, trip kind, max stops, adults, cabin, refuse reasons).

## Tiers

- **smoke / easy**: one city pair, one date (BOS–LHR, NRT–ICN, GRU–SCL, …),
  airports lookup, cheapest Friday, invalid IATA, missing date.
- **medium**: packaged RT vs two one-ways, open jaw, max-stops, cabin,
  adults, explore, dates calendar, hotel nights / rooms.
- **hard**: flexible dates, layover caps, night-arrival clocks, packaged
  RT return legs, rooms occupancy, refuse booking/trains/cars, destinations
  and prices only.
- **insane**: multi-hop fantasy that still has a contract, including
  Halifax → Fiji via two European airports, one sub-Saharan, one Indian,
  one Chinese, and New Zealand. Other insane rows start in Halifax,
  Vancouver, Boston, Singapore, Auckland, Cairo, São Paulo — not Madrid.

Humans may later gate easy-tier failures. Do not silently gate insane/llm
cases.
