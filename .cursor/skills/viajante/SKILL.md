---
name: viajante
description: Search live Google Flights and hotel prices locally with the viajante CLI or MCP (search_flights, search_dates, search_flex, search_explore, search_hotels, search_trip, lookup_airports). Use when the user asks about flights, hotels, trip totals, cheapest week, flexible dates, destination triage, or how to configure viajante MCP. No API keys. Never invent fares.
---

# viajante

Local Google Flights and hotel search. No implied home hub. Fetch locale is English. User prompts may be any language.

Currency is `--currency` / MCP `currency`, or inferred from a **named** origin airport country (JFK USD, LHR GBP, NRT JPY, GRU BRL). Hotels require currency. If origin, dest, country, or currency is unproven (several airports, “Europe”, two currencies), ask or error. Do not invent IATA, `gl`, ISO 4217, fares, typicals, tokens, bag fees, or via lists. Viajante does not convert.

Flags: `src/viajante/cli.py` (`viajante <cmd> --help`). MCP signatures: `src/viajante/mcp_server.py`. This skill is not argparse.

**Install the MCP:** [mcp.md](mcp.md) — `uvx` + `viajante-mcp` in the client’s `mcpServers`. Checkout `.cursor/mcp.json` is contributors only.

## Dates

CLI rejects dates in the past. Compute ISO dates from **today**. Never copy a calendar date from this skill, README, or old chat. Grammar: `ORIGIN-DEST:YYYY-MM-DD`. Two dates on one spec without `--trip` are two one-ways. `--trip rt` is one package. `--flex` is required. Explore needs `--from` or `--month`.

Fuzzy timing (for example, “late October / early November”) is not an ISO window. Propose the computed `start` and `end`, then wait for the user's confirmation before searching. For `search_flights`, every `routes` entry is exactly `ORIGIN-DEST:YYYY-MM-DD`; for `search_dates`, `route` is `ORIGIN-DEST` and `start` / `end` are separate ISO dates. Do not replace the colon with whitespace.

## Which tool

| Need | MCP | CLI |
|------|-----|-----|
| Ambiguous city or IATA | `lookup_airports` | `viajante airports` |
| Named route and date | `search_flights` (`routes`) | `viajante flights` |
| Cheapest week (max 31 days) | `search_dates` (`route`, `start`, `end`) | `viajante dates` |
| ±N around one date, then one shop | `search_flex` (`route`, `around`, `flex`) | `viajante flex` |
| Dest triage from a named origin | `search_explore` (`origin`, `start` or `month`) | `viajante explore` |
| Stay only | `search_hotels` (`location`, `check_in`, `check_out`, `currency`; default source google) | `viajante hotels` (CLI default source Booking) |
| Flights then hotel | `search_trip` (`routes`, `location`) | `viajante trip` |

Do not brute-force a date matrix when dates/flex/explore exist. `search_trip` rejects children/infants (hotel occupancy is adults-only). Omit `trip_total` if either side misses, dates miss, or currencies differ.

## Hotels: ask once

| User intent | Action |
|-------------|--------|
| Named route/date only | Flights only |
| Trip with unclear lodging | Ask once |
| Explicit flights and hotels | `search_trip` |
| Hotels only | Hotels only |
| Explicit flights only | Flights only |

Free-cancellation filter is on by default. A silent card is not proof of free cancellation. `--allow-non-refundable` only after explicit consent. User verifies the total on the provider before booking.

## Fetch

`--fetch auto`: sweep for 3+ flight queries; detail for 1–2 when Playwright is installed. Sweep empty or `blocked` may fall back to detail once. `markup_drift` does not. Sweep needs no Chromium. Detail and Booking sleep ~4.5–6s between queries. Never shorten that or parallelize. One MCP search at a time.

`search_dates` is an HTTP calendar and has no `fetch` parameter; installing Chromium cannot switch it to detail. `fetch=detail` applies only to `search_flights`, and requires the browser extra and Chromium in the MCP environment.

Unnamed `baggage_buffer` is 0. Prefer `bags` / `carry_on` on the request. Do not invent a bag fee.

## Destination triage

No implied home hub. Use the origin the user named. If unnamed, ask.

1. Shortlist from vibe plus a rough band for **that** origin only. Do not invent a fare or reuse another origin's band.
2. Fixed natural dates first (one out + one back per dest). `--top 3`.
3. ±1 day only on 1–3 finalists. Around/±N on a named route is flex. Cheapest week is dates.
4. Retry only failed legs.

## Report

Copy owned numbers. Print `typical_deal` only when `typical` is present (same-route calendar median, not a price-trend history). Print `stops_compare` when present. JSON keys: `src/viajante/models.py`. Booking/Google URLs are optional.

Only values returned by a Viajante payload are search evidence. Do not use a manually operated Google Flights tab to continue an MCP failure or present its price as a Viajante result.

After Booking, 1–3 finalists may get a browser second opinion (same dates, occupancy, visible total). Do not write those into `--save` JSON.

## Recovery

| Situation | Action |
|-----------|--------|
| `no_results` | Stop. Do not retry. |
| `rejected` | Stop. Check IATA with `lookup_airports`. |
| `blocked` | Wait 30–60 minutes. Do not start a new batch. |
| `search_dates` returns `blocked` | Stop that calendar search. Do not set `fetch`, transfer consent/cookies, or scrape a separate browser tab. |
| `markup_drift` | Stop. Do not retry the same parse. |
| no eligible offers/stays, exit 0 | Widen filters or dates. Not a fetch failure. |
| `browser_unavailable` | For `search_flights` detail or Booking only: install browser extra **in the MCP env**, then Chromium. See [mcp.md](mcp.md). |
| `fetch_failed` / exit 2 | Wait 30–60 minutes. Retry failed queries only. |
| Exit 3 | Re-run only failed legs. |
| Detail/Booking consent break | Delete `pw_state_google.json` or `pw_state_booking.json` in the state dir. |

State dir: `VIAJANTE_STATE_DIR` or XDG. Exit 0/1/2/3 = all ok / bad input / all failed / partial.

Checkout tests: `uv run python -m unittest discover -s tests -v`. `viajante bench` is contributor-only (needs `tests/bench/`).
