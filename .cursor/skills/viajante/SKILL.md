---
name: viajante
description: Search live Google Flights and hotel prices locally with the viajante CLI or MCP (search_flights, search_dates, search_flex, search_explore, search_hotels, search_trip, lookup_airports). Use when the user asks about flights, hotels, trip totals, cheapest week, flexible dates, or destination triage. No API keys. Never invent fares.
---

# viajante

Local Google Flights and hotel search. No implied home hub. Fetch locale is English. User prompts may be any language.

Currency is `--currency` / MCP `currency`, or inferred from a **named** origin airport country (JFK USD, LHR GBP, NRT JPY, GRU BRL). Hotels require currency. If origin, dest, country, or currency is unproven (several airports, “Europe”, two currencies), ask or error. Do not invent IATA, `gl`, ISO 4217, fares, typicals, tokens, bag fees, or via lists. Viajante does not convert.

Flags and MCP signatures live in `src/viajante/cli.py` and `src/viajante/mcp_server.py`. This skill is not argparse.

## MCP (this checkout)

Server code: `src/viajante/mcp_server.py`. Console script: `viajante-mcp`. Stdio only. No auth. One search at a time.

Project config is `.cursor/mcp.json`. Cursor loads it for this workspace. After `uv sync --extra mcp`, reload MCP in Cursor Settings.

Sweep and Google Hotels need no Chromium. For Booking.com or `fetch=detail`, the **same** process needs the browser extra and Chromium:

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uv",
      "args": ["run", "--extra", "mcp", "--extra", "browser", "viajante-mcp"]
    }
  }
}
```

```bash
uv sync --extra mcp --extra browser
uv run playwright install chromium
```

Installing `viajante[browser]` in another venv does not change this MCP process.

## MCP (no clone)

Claude Desktop, Cursor user MCP, or any client that takes `mcpServers`:

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/felipebasurto/viajante.git[mcp]",
        "viajante-mcp"
      ]
    }
  }
}
```

Browser-enabled uvx (Chromium must be installed into **that** uvx env):

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/felipebasurto/viajante.git[mcp,browser]",
        "viajante-mcp"
      ]
    }
  }
}
```

```bash
uvx --from 'git+https://github.com/felipebasurto/viajante.git[mcp,browser]' playwright install chromium
```

PyPI `pip install viajante` is not published yet. Use git until `pypi.org/pypi/viajante/json` returns 200.

## Tools

| Tool | When |
|------|------|
| `lookup_airports` | City or IATA is unnamed or ambiguous |
| `search_flights` | Named route and date (`routes` like `JFK-LHR:YYYY-MM-DD`) |
| `search_dates` | Cheapest week (`route`, `start`, `end`) |
| `search_flex` | ±N around one date (`route`, `around`, `flex`) |
| `search_explore` | Dest triage from a named origin (`origin`, `start` or `month`) |
| `search_hotels` | Stay only. `location`, `check_in`, `check_out`, `currency`. Default source google |
| `search_trip` | Flights then hotels. `routes` plus `location`. Rejects children/infants (hotel occupancy is adults-only) |

Checkout CLI: `uv run viajante <cmd> --help`. Same intents. Hotels CLI default source is Booking.

## Dates

The CLI rejects dates in the past. Compute ISO dates from **today**. Never copy a calendar date out of this skill or from aged README samples. Grammar: `ORIGIN-DEST:YYYY-MM-DD`. Two dates on one spec without `--trip` are two one-ways. `--trip rt` is one package. `--flex` and explore `--from`/`--month` are required (no hidden defaults).

## Pick a command

- Named route and date: `search_flights` / `viajante flights`
- Cheapest week: `search_dates` / `viajante dates` (31-day cap)
- Around one date, ±N, then one shop: `search_flex` / `viajante flex`
- Where is cheap from this origin: `search_explore` / `viajante explore`
- Flights plus hotel: `search_trip` / `viajante trip` (omit `trip_total` if either side misses, dates miss, or currencies differ)
- Do not brute-force a date matrix or every airport when those commands exist

## Hotels: ask once

| User intent | Action |
|-------------|--------|
| Named route/date only | Flights only |
| Trip with unclear lodging | Ask once |
| Explicit flights and hotels | `search_trip` |
| Hotels only | Hotels only |
| Explicit flights only | Flights only |

Free cancellation is the default filter. A silent card is not proof of free cancellation. `--allow-non-refundable` only after explicit consent. Remind the user to verify the total and terms on the provider before booking.

## Fetch

`--fetch auto` (default): sweep for 3+ flight queries; detail for 1–2 when Playwright is installed. Sweep empty or `blocked` may fall back to detail once. `markup_drift` and shopping rejects do not. Sweep needs no Chromium. Detail and Booking sleep ~4.5–6s between queries on purpose. Never shorten those delays or parallelize. One MCP search at a time.

Unnamed `baggage_buffer` is 0. Prefer `bags` / `carry_on` on the shopping request. Do not invent a bag fee.

## Destination triage

No implied home hub. Use the origin the user named. If origin is unnamed, ask.

1. Shortlist from vibe plus a rough band for **that** origin only. Do not invent a fare or reuse another origin's band.
2. Fixed natural dates first (one out + one back per dest). `--top 3`.
3. ±1 day only on 1–3 finalists. Around/±N on a named route is flex. Cheapest week is dates.
4. Retry only failed legs.

## Report

Copy owned numbers. Print `typical_deal` only when `typical` is present (`typical` is a same-route calendar median, not a price-trend history). Print `stops_compare` when present. `--save` JSON keys: `src/viajante/models.py`. Do not invent keys. Booking/Google URLs are optional.

After Booking, for 1–3 finalists only, a browser second opinion is allowed (same dates and occupancy, visible total). Do not write those into `--save` JSON.

## Recovery

| Situation | Action |
|-----------|--------|
| `no_results` | Stop. Do not retry. |
| `rejected` | Stop. Check IATA with `lookup_airports`. |
| `blocked` | Wait 30–60 minutes. Do not start a new batch. |
| `markup_drift` | Stop. Do not retry the same parse. |
| no eligible offers/stays, exit 0 | Widen filters or dates. Not a fetch failure. |
| `browser_unavailable` | Install browser extra **in the MCP env**, then Chromium. |
| `fetch_failed` / exit 2 | Wait 30–60 minutes. Retry failed queries only. |
| Exit 3 | Re-run only failed legs. |
| Consent/markup break | Delete `pw_state_google.json` or `pw_state_booking.json` in the state dir. |

State dir: `VIAJANTE_STATE_DIR` or XDG. Exit 0/1/2/3 = all ok / bad input / all failed / partial.

## Tests (checkout)

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
```

`viajante bench` is contributor-only (needs `tests/bench/`). Do not run it from an installed wheel.
