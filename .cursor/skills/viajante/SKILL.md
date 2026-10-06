---
name: viajante
description: Search live Google Flights and hotel prices with the viajante CLI or MCP; check finalist room rates, validate itineraries and replies, and plan stay blocks or split costs offline. Use for flights, hotels, trip totals, cheapest week, flexible dates, destination triage, hidden-city/Skiplagged, award points math, group stays, or viajante MCP setup. No API keys. Never invent fares.
---

# viajante

Local Google Flights and hotel search. No implied home hub. Fetch locale is English. User prompts may be any language.

Currency is `--currency` / MCP `currency`, or inferred from a **named** origin airport country (JFK USD, LHR GBP, NRT JPY, GRU BRL). Google and Booking hotels require currency; opt-in Skiplagged hotels default to USD. If origin, dest, country, or currency is unproven (several airports, “Europe”, two currencies), ask or error. Do not invent IATA, `gl`, ISO 4217, fares, typicals, tokens, bag fees, or via lists. Viajante does not convert.

Flags: `src/viajante/cli.py` (`viajante <cmd> --help`). MCP signatures: `src/viajante/mcp_server.py`. This skill is not argparse.

**Install the MCP:** [mcp.md](mcp.md) — `uvx` + `viajante-mcp` in the client’s `mcpServers`. Checkout `.cursor/mcp.json` is contributors only.

## Dates

CLI rejects dates in the past. Compute ISO dates from **today**. Roll a named season to the next legal year if that month is already past. Never copy a calendar date from this skill, README, or old chat. Grammar: `ORIGIN-DEST:YYYY-MM-DD`. Two dates on one spec without `--trip` are two one-ways. `--trip rt` is one package. `--flex` is required. Explore needs `--from` or `--month`. `max_stops` is 0, 1, or 2; cannot require 3+. If the user needs 3+, say so and search with 2, or refuse — do not invent a fare.

Fuzzy timing (for example, “late October / early November”) is not an ISO window. Propose the computed `start` and `end`, then wait for the user's confirmation before searching. For `search_flights`, every `routes` entry is exactly `ORIGIN-DEST:YYYY-MM-DD`; for `search_dates`, `route` is `ORIGIN-DEST` and `start` / `end` are separate ISO dates. Do not replace the colon with whitespace.

## Which tool

| Need | MCP | CLI |
|------|-----|-----|
| Executing package and schema versions (offline) | `get_runtime_info` | `viajante --version` |
| Ambiguous city or IATA | `lookup_airports` | `viajante airports` |
| Named route and date | `search_flights` (`routes`) | `viajante flights` |
| Cheapest week (max 31 days) | `search_dates` (`route`, `start`, `end`) | `viajante dates` |
| ±N around one date, then one shop | `search_flex` (`route`, `around`, `flex`) | `viajante flex` |
| Dest triage from a named origin | `search_explore` (`origin`, `start` or `month`) | `viajante explore` |
| Stay only | `search_hotels` (`location`, `check_in`, `check_out`, `currency`; default source google) | `viajante hotels` (CLI default source Booking) |
| Room rates for one finalist (Skiplagged, USD) | `search_hotel_rooms` (`check_in`, `check_out`, then `hotel_id` or `hotel_name` + `city`) | `viajante hotel-rooms` |
| Flights then hotel | `search_trip` (`routes`, `location`) | `viajante trip` |
| Hidden-city / Skiplagged | `search_hidden_city` (`route`, `departure`) | `viajante hidden-city` |
| Named award vs cash (local) | `compare_awards` (`offer`) | `viajante awards` |
| Transfer table (local) | `lookup_transfers` (`program`, `points`) | `viajante points` |
| Validate selected flight offers (local) | `validate_itinerary` (`legs`, `constraints`) | — |
| Per-night roster into check-in/check-out blocks (local) | `plan_stay_blocks` (`roster`) | — |
| Split stay totals by nights per person (local) | `split_stay_costs` (`stays`, `roster`, `currency`) | — |
| Check a draft reply against this process's search evidence (local) | `verify_answer` (`answer`) | — |

Do not brute-force a date matrix when dates/flex/explore exist. `search_trip` rejects children/infants (hotel occupancy is adults-only). Omit `trip_total` if either side misses, dates miss, or currencies differ.

## Hidden-city

After a named-route `search_flights` (one origin, one dest, ISO date), if the dest is a hub or leisure trunk (JFK-MIA, LHR-JFK, LAX-CUN, …) or Google looks like a through-fare might undercut (`typical_deal` poor, odd one-stops), call `search_hidden_city` once with the same route and date. Sequential (process lock). Do not mix the two JSON payloads. Lead with `hidden_city: true` rows and the owned warnings. Print owned `ticketed_destination` / `layover_city` when present. Do not treat a hidden fare as a cheaper legal fare until the human opens `booking_url` (do not scrape Skiplagged). Skip explore, dates, flex calendars, multi-city, unproven dests, and any search that named `bags` / a checked bag. Omit hidden-city `currency` (Skiplagged cards are USD); do not copy a Google/origin quote keep (GBP, JPY, …). `no_results` / `blocked`: stop that Skiplagged leg; do not fill from Google. `currency_mismatch`: omit currency or pass the owned card code in the error (usually USD) and retry once. Do not treat that as an empty market.

## Hotels: ask once

| User intent | Action |
|-------------|--------|
| Named route/date only | Flights only |
| Trip with unclear lodging | Ask once |
| Explicit flights and hotels | `search_trip` |
| Hotels only | Hotels only |
| Explicit flights only | Flights only |

Free-cancellation filter is on by default. A silent card is not proof of free cancellation. Skiplagged search cannot apply it: read `applied.not_applied` and verify room terms with `search_hotel_rooms` before claiming cancellation is free. `--allow-non-refundable` only after explicit consent. User verifies the total on the provider before booking.

Hotel evidence rules:

- **Location is one named place.** A typo, a region ("Costa Brava", "Andalucía"), or several candidate towns: ask once and spell out the candidate. Never substitute a nearby town as a proxy. A result whose address is not the named place was not requested.
- **`total_price` is a total-stay quote, not per person.** Only claim the searched party total when `priced_adults` agrees. Unknown means unverified; known mismatches are excluded. Do not divide it, and do not compare it with a booking made for another `adults`, `rooms`, or dates. A per-person figure is labeled arithmetic, and only when occupancy and nights match.
- **The room split is unproven.** The request carries `adults` and `rooms` only. Say "searched as N adults, M rooms; confirm the sleeping arrangement on the provider". Do not write "4 triples" or "fits 12". The only capacity evidence is `sleeps`, `bedrooms`, and `beds`, and Google gives them for vacation rentals (`entire_home=true`), not for hotel rooms. `priced_adults` is the party Google priced; if it differs from the ask, the total is not for that party.
- **A headcount that changes by night: ask the roster, do not infer it.** Run `plan_stay_blocks` on the roster the user confirmed, then search one stay per block (`stays` on `search_hotels`, up to 8 per call, `adults` = the block's `headcount`). Blocks group the same people, not just equal headcounts. Report blocks separately, and read `property_matrix` for properties that appear in several blocks (a null means "not among that stay's returned offers", not unavailable; raise `top`). Rows are sorted by name, never by price. Pass `near` ({lat, lng}, CLI `--near LAT,LNG`) only for a point the user named: offers with coordinates then carry `distance_km`, a straight line. For a maximum distance, pass `max_distance_km` / `--max-distance-km` with that point; outside or unknown coordinates are excluded before the price cut. Read the latest confirmed roster after every correction, including departures; do not extend anyone to the final stay check-out. Do not guess who dropped out. To split what a group pays, run `split_stay_costs` with the chosen stays and the same roster instead of dividing by hand: each stay is shared only by the people who sleep in it, by their nights. It uses the currency and any per-person nightly fee the user names and never converts. Allocated cents sum exactly; `unallocated_nights` names nights no stay covers.
- **House, villa, or casa:** call `search_hotels` with `entire_home=true`. A hotel list is not an answer to that ask. If it returns nothing, say so. Airbnb is not a viajante source.
- **Property kind** is `place_types` (Google's own tags: `hotel`, `hostel`, `villa`, `apartment_complex`, ...) plus `class_label` ("3-star hotel"). A hotel search returns hostels, so read `place_types` before calling a result a hotel. Hostel prices are for the searched party as Google priced it; do not assume a private room.
- **`resolved_place`** is the place Google resolved the query text to ("Jávea" resolves to "Xàbia"). `place_bounds` is its viewport (south, west, north, east). Google also returns neighbors (a Jávea search lists Dénia hotels). Compare each offer's `latitude`/`longitude` with `place_bounds` and say when an offer sits outside it. Do not relabel it as the requested town.
- **A Google price for a hostel can be a bed in a dormitory.** Google carries no room type. Before calling a hostel offer private, run `search_hotel_rooms` by name for it and read the room titles.
- **General Google descriptions are property text, not priced-room evidence.** `details` preserves that text; private-room, capacity and cancellation fields use explicit unit chips. Shared rooms, dorm/private mixes and negated private rooms are unknown. Never promote a property's room inventory into the quoted room type. Policy claims (parties, pets, minimum age) require explicit property terms; silence is unverified.
- **Links:** `link_context` and `applied.url_context` are `stay`, `property`, `location`, or `none`. Google entity URLs carry the stay context when owned; never reuse `/travel/clk/hi` trackers. Say when context is missing. A stay URL preserves dates/adults/rooms, not guaranteed current price or availability. `verify_answer` proves provenance only; it does not open links.
- **Existing reservations:** obtain replacement quotes and terms before recommending cancellation. Do not assume changed dates keep prices or cancellation terms, or that reducing dorm beds keeps exclusive occupancy. Respect maximum capacity and rejected requests; surface contradictory bed/bathroom evidence. Copy exact cancellation deadlines in property-local time; missing deadlines stay unknown. Label proportional cost estimates as arithmetic, not new quotes. Airport transfers need route time and an arrival margin; check-out alone does not prove feasibility. Retain caveats and unallocated roster nights in the final answer.
- **Skiplagged is an opt-in second source** (`source="skiplagged"`, CLI `--source skiplagged`). USD only, so omit `currency` or pass USD and do the FX yourself. Up to 10 adults and 9 rooms per search; no `entire_home`. It matches the city loosely: read `resolved_place` and say when it is not the place asked for. Its search rows carry no cancellation. For 1-3 finalists call `search_hotel_rooms` with the offer's `provider_id`, or with `hotel_name` + `city` for a Google finalist (exact normalized name only; no match or several matches come back as `no_results`, never a guess). CLI uses `--hotel-id` or `--name` plus `--city`. Room rates support up to 5 rooms and come in provider order: read `occupancy_limit`, `refundable`, `free_cancellation`, and `taxes_and_fees` as listed. `occupancy_limit` is per provider room type, not proof a party fits across several rooms. Never mix its rows with Google or Booking prices or compare them as one list.
- **No background work.** Every MCP call is synchronous (progress is streamed when the client asks; cancelling the call stops the search). Never say you are still looking or will report back. A `deadline_seconds` result with `stopping_reason: "deadline"` is partial: unfinished queries are not loaded, not proven empty. Call the tool now, or name the next step and wait. Do not offer an action no tool performs (cancelling a booking).
- A changed constraint (hotels, then house, then hotels) is restated in one line before the next search.

## Fetch

Check `get_runtime_info` or `viajante --version` before relying on new flags/tools. Hotel reports stamp `viajante_version`. An npm MCP and a separate uv tool may be different versions; unpinned `uvx` can reuse an installed old tool. Explicitly upgrade or refresh the version and check again. Run batches sequentially and inspect every error. In zsh use a flags array expanded as `"${flags[@]}"`; a space-separated scalar is one argument.

`--fetch auto`: sweep for 3+ flight queries or packaged RT/multi; detail for other 1–2 when Playwright is installed. Sweep empty or `blocked` may fall back to detail once unless `rate_limited` is true. `markup_drift` does not. Sweep needs no Chromium. Detail and Booking sleep ~4.5–6s between queries. Never shorten that or parallelize. One MCP search at a time. A second search while one is running raises `a viajante search is already running in this process` immediately. That is not `MCP error -32001: Request timed out`; do not treat timeouts as lock-busy or retry them in a long wait loop. `lookup_airports` may run during a search.

`search_dates` is an HTTP calendar and has no `fetch` parameter; installing Chromium cannot switch it to detail. `fetch=detail` applies only to `search_flights`, and requires the browser extra and Chromium in the MCP environment. Optional MCP `country` is Google `gl` (origin market). Omit when unset. Do not pass a destination ISO.

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

## Itinerary assembly: PASS / FAIL / UNKNOWN

Preserve each selected offer's exact `query`, `price`, `price_text`, currency, occupancy,
bag request, and `evidence`. Never move a fare to another date, infer the reverse fare,
or invent a routing, operator, flight number, bag inclusion, or missing leg.

Do not use `blocked`, `rejected`, `markup_drift`, `fetch_failed`, or estimated rows in a
verified itinerary. `needs_bag_verify` means baggage is **UNKNOWN**, not included. An
empty `segments` array makes segment count, connection airports, per-segment clocks,
operators, and overnight checks **UNKNOWN**. A comparative rule such as “unless it
saves N” is **UNKNOWN** without two owned comparison candidates.

Call `validate_itinerary` before saying an assembled itinerary is compliant:

- **PASS**: the validator has owned evidence for the named constraint and it satisfies it.
- **FAIL**: owned evidence contradicts the constraint; do not present the candidate as feasible.
- **UNKNOWN**: required evidence is missing; stop at that leg and name the missing query or field.

Include the owned `google_flights_url` when claiming query evidence. A query URL
reproduces the request, not guaranteed live fare availability. Words such as
“optimal”, “only”, “unavoidable”, and “exhaustive” require a declared finite search
scope whose `coverage.complete` is true. Otherwise say “not found in the tested scope.”
Relaxed dates or constraints are a separate scenario and never make the original
scenario compliant.

## Recovery

| Situation | Action |
|-----------|--------|
| Browser access denied | Report the client access limitation, not a provider failure or broken URL. Use permitted read-only alternatives; do not bypass denial or re-request already authorized access. |
| Missing local screenshot | Say the image could not be read; do not claim visual inspection. Continue from available text/evidence. |
| `no_results` | Stop. Do not retry. |
| `rate_limited: true` | Stop provider searches. Wait 30–60 minutes; do not retry or change method. Direct Google 429 or data-less status 13 shares `google-rate-limit.json`; Skiplagged 429 uses `skiplagged-rate-limit.json` with no retry and a one-second live call pace. |
| `currency_mismatch` | Skiplagged keep missed (cards are USD). Omit `currency` or pass the owned code in the error and retry once. Do not convert. Do not treat as `no_results`. |
| `rejected` | Stop. The provider did not identify the cause. Check named IATA, but do not infer an invalid airport, unavailable route, or inventory cutoff. |
| `blocked` (including a short unknown HTML shell) | Stop that calendar. No flex, no `search_flights`, no browser recovery. Wait 30–60 minutes before a new batch. |
| `search_dates` returns `blocked` | Stop that calendar search. Do not set `fetch`, transfer consent/cookies, or scrape a separate browser tab. |
| `search_hidden_city` `blocked` / fetch failed | Stop. Do not treat a Google Flights tab as Skiplagged evidence. Wait 30–60 minutes. |
| Flex `markup_drift` with empty `days` | Compact calendar miss, not an empty market. Do not invent a cheapest week. Do not retry the same flex parse. A **named-date** `search_flights` is allowed. |
| `markup_drift` (other) | Stop. Do not retry the same parse. |
| Process busy (`already running`) | Wait for that search to finish. Do not start another search in this process. |
| MCP timeout `-32001` | Not the process lock. Do not 8×60s-retry it as lock-busy. |
| no eligible offers/stays, exit 0 | Widen filters or dates. Not a fetch failure. |
| `browser_unavailable` | For `search_flights` detail or Booking only: install browser extra **in the MCP env**, then Chromium. See [mcp.md](mcp.md). |
| `fetch_failed` / exit 2 | Wait 30–60 minutes. Retry failed queries only. |
| Exit 3 | Re-run only failed legs. |
| Detail/Booking consent break | Delete `pw_state_google.json` or `pw_state_booking.json` in the state dir. |

State dir: `VIAJANTE_STATE_DIR` or XDG. Exit 0/1/2/3 = all ok / bad input / all failed / partial.

The status 13 cooldown is a guess and can pause Google for 2 minutes when the
status has another cause. Google base/tax/fee breakdown (`record[6][2][44]`,
unconfirmed) and caller-named exchange rates remain outside this release.

Checkout tests: `uv run python -m unittest discover -s tests -v`. `viajante bench` is contributor-only (needs `tests/bench/`).
