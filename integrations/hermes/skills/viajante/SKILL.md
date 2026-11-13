---
name: viajante
description: Search flights and hotels with the viajante MCP tools and report only what the provider returned. Use for fares, dates, routes, airports, hotel stays and price checks.
compatibility: Needs the viajante MCP server (uv and Python 3.10 or later on the machine).
---

# viajante

viajante returns provider evidence. You weigh it: price, duration, stops, clocks, rating. It never converts currency and never guesses.

## Which path

- **Tools are listed.** You are connected. Read `status` and `empty_reason` before the rows. `get_guide` is the long rules. `get_runtime_info` is the running version.
- **Nothing installed, you have a shell.** Primary: `npx -y @viajante/mcp` (needs uv and Python 3.10+). No Node: `uvx --from viajante[mcp] viajante-mcp`. If `viajante-mcp` says it needs the mcp extra, you are not in a checkout: use that uvx line, not `uv sync`.
- **CLI only.** `viajante <cmd> --help` and `viajante --version`. Hotels need `--currency`.
- **No shell and no local process.** You cannot search. Loopback HTTP has no auth and is never hosted. `server.json` has no remotes. Do not invent a URL.

## Before a search

- Name the origin and destination as IATA codes, or call `lookup_airports`. Ask when a city has several airports.
- Currency: pass `currency`, or name an origin airport so it is proven. Hotels always need `currency`. Never assume one.
- One search runs at a time. A `search_in_progress` error is not a timeout; wait, do not retry blindly.
- `carry_on` is checked on the page. Checked bags are refused. `max_stops` is 0, 1 or 2.

## Read the envelope first

Every tool except `lookup_airports` returns `status`, `completeness`, `empty_reason`, `error_code`.

- Only `empty_reason: provider_empty` means "none found".
- `filtered_out`: viajante's filters removed rows the provider returned.
- `not_loaded`, `blocked`, `markup_drift`: the search did not complete. Availability is unknown; say what was searched.
- `completeness: partial`: read each query row.
- `rate_limited: true`: wait until `retry_after`, tell the user, send nothing else.

## Tools

- `search_flights`: a named route and dates. `search_dates`: cheapest days in a window. `search_flex`: plus or minus N days around a date. `search_explore`: destination triage from an origin.
- `search_hotels`, `search_hotel_rooms`, `get_hotel_details`. Free cancellation is the default; non-refundable stays only on request.
- `recheck_offer`: one fresh search for a finalist. `check_failed` does not mean the offer is gone.
- `validate_itinerary` before calling an assembled route compliant. Unknown is never a pass.

## Answering

- State only amounts, codes, dates and links a tool returned. Never invent a fare, bag fee, IATA code or exchange rate.
- Pass the draft to `verify_answer` before replying.
- Remind the traveller to verify the total and the cancellation terms on the provider before booking.
