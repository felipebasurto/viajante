---
name: viajante
description: Search Google Flights and Booking.com locally for trip planning with viajante. Use when the user asks about flight prices, route/date comparisons, hotels, accommodation, or a trip that may need both.
---

# viajante

Local flight and hotel search from any IATA pair or city. No implied home hub. Quotes default to EUR. Flights default to one-way, one adult, economy; `--trip rt` packages a round-trip. Hotel prices are totals for the full stay. CLI hotels default to Booking; MCP hotels default to Google.

Fetch locale is English (`hl=en` / `lang=en`, `locale=en-US`). User prompts may be any language; planned queries stay English IATA and English fetch. Never invent a fare, typical, token, or via list.

## Invocation

Prefer the checkout CLI:

```bash
uv run viajante ...
```

After `uv sync` and `uv run playwright install chromium`, the entry point is available. Do not use a global `viajante` binary from another checkout.

## Commands

```bash
uv run viajante flights ORIGIN-DEST:YYYY-MM-DD[,YYYY-MM-DD...] [--trip {one-way,rt,multi}] [--max-stops {0,1,2}] [--adults N] [--children N] [--infants-in-seat N] [--infants-on-lap N] [--cabin CABIN] [--currency CODE] [--country CC] [--bags N] [--carry-on] [--price-cap EUR] [--nearby] [--top N] [--baggage-buffer EUR] [--sort {ranked,fare,price,duration,departure,arrival}] [--airlines CODES] [--exclude-airlines CODES] [--alliance NAMES] [--exclude-alliance NAMES] [--depart-window START-END] [--fetch {auto,sweep,detail}] [--max-layover HOURS] [--min-layover HOURS] [--via CODES] [--exclude-via CODES] [--max-duration HOURS] [--save FILE]
uv run viajante dates ORIGIN-DEST --from YYYY-MM-DD --to YYYY-MM-DD [--trip {one-way,rt}] [--nights N] [--max-stops {0,1,2}] [--adults N] [--cabin CABIN] [--bags N] [--carry-on] [--price-cap EUR] [--airlines CODES] [--exclude-airlines CODES] [--via CODES] [--exclude-via CODES] [--depart-window START-END] [--max-layover HOURS] [--min-layover HOURS] [--max-duration HOURS] [--nearby] [--fetch {auto,sweep,detail}] [--save FILE]
uv run viajante flex ORIGIN-DEST --around YYYY-MM-DD --flex N [--nights N] [--trip {one-way,rt}] [--max-stops {0,1,2}] [--adults N] [--cabin CABIN] [--top N] [--bags N] [--carry-on] [--price-cap EUR] [--airlines CODES] [--exclude-airlines CODES] [--via CODES] [--exclude-via CODES] [--depart-window START-END] [--max-layover HOURS] [--min-layover HOURS] [--max-duration HOURS] [--nearby] [--save FILE]
uv run viajante explore ORIGIN --from YYYY-MM-DD [--days N] [--month YYYY-MM] [--top N] [--adults N] [--cabin CABIN] [--max-stops {0,1}] [--bags N] [--carry-on] [--price-cap EUR] [--airlines CODES] [--exclude-airlines CODES] [--via CODES] [--exclude-via CODES] [--depart-window START-END] [--max-layover HOURS] [--min-layover HOURS] [--max-duration HOURS] [--nearby] [--save FILE]
uv run viajante airports QUERY
uv run viajante hotels LOCATION CHECK_IN CHECK_OUT [--source {booking,google}] [--adults N] [--rooms N] [--top N] [--min-rating SCORE] [--entire-home] [--allow-non-refundable] [--compare-cancellation] [--save FILE]
uv run viajante trip ORIGIN-DEST:YYYY-MM-DD[:YYYY-MM-DD] --hotel LOCATION [--check-in DATE] [--check-out DATE] [--trip {one-way,rt,multi}] [--adults N] [--rooms N] [--bags N] [--carry-on] [--price-cap EUR] [--airlines CODES] [--exclude-airlines CODES] [--via CODES] [--exclude-via CODES] [--nearby] [--fetch {auto,sweep,detail}] [--source {booking,google}] [--save FILE]
uv run viajante bench
uv run viajante bench --prompts
uv run viajante bench --prompts --timeit-sweep
```

Route grammar: `JFK-LHR:2026-09-15`, or several dates comma-separated on one route. `JFK-NRT:2026-10-09:2026-10-20` without `--trip` is sugar for outbound + return as two one-way queries. `--trip rt` POSTs one package. `--nearby` expands origin or dest to owned same-city IATA on flights, dates, flex, explore, and trip (default off; named open-jaw airports stay; no invented codes). You can still pass a return leg as a second route.

MCP (stdio, no auth): `uv sync --extra mcp` then `viajante-mcp`. Tools: `search_flights`, `search_dates`, `search_flex`, `search_trip`, `search_explore`, `lookup_airports`, `search_hotels`. Flight filters, dates `nights`/`trip`/`max_stops`, flex `around`/`flex`/`nights`, explore `month`/`adults`/`cabin`/`max_stops`, `search_trip` for an owned fare+stay sum (same bags/via/airlines/`price-cap`/`depart-window`/`max-layover`/`min-layover`/`max-duration` shop filters as flights), hotels `source=google` by default. Keep the one-search process lock.

## Smoke

```bash
# Offline (always safe)
uv run python -m unittest discover -s tests -v

# Fast HTTP shortlist (no Chromium)
uv run viajante flights JFK-LHR:2026-09-15 --fetch sweep --top 3
uv run viajante flights BOS-LHR:2026-09-18 --nearby --fetch sweep --top 3
uv run viajante flights JFK-SIN:2026-11-03 --via IST --exclude-via DXB --fetch sweep --top 3
uv run viajante dates BOS-LHR --from 2026-09-01 --to 2026-09-14 --fetch sweep
uv run viajante flex BOS-LHR --around 2026-09-12 --flex 3 --nights 7 --fetch sweep
uv run viajante explore JFK --from 2026-09-15 --days 7
uv run viajante airports tokyo

# Playwright max evidence
uv run viajante flights JFK-LHR:2026-12-04 --fetch detail --top 3

# Owned fare + stay sum (omit the total if either side misses)
uv run viajante trip SIN-MEL:2026-11-06:2026-11-10 --hotel Melbourne --trip rt --fetch sweep

# One live hotel query
uv run viajante hotels Tokyo 2026-12-04 2026-12-07 --top 3
```

## Hotels: ask once

| User intent | Action |
|-------------|--------|
| Price check for a named route/date only | Flights only. Do not ask about hotels. |
| Trip with dates and unclear lodging | Ask once whether to search Booking.com hotels. |
| Explicit flights and hotels | Run `viajante trip` (or both tools). Print owned fare + stay + sum when both succeed. Do not invent a missing side. |
| Hotels only | Hotels only. |
| Explicit "no hotel" / "flights only" | Flights only. Do not ask. |

Do not run a hotel search without confirmation when lodging intent is unclear.

## Multi-leg trips

```bash
uv run viajante flights JFK-LHR:2026-09-25 LHR-JFK:2026-09-27 --max-stops 0
```

Each route and each comma-separated date is a separate sequential query. `--max-stops` applies to every leg in that invocation. Progress lines go to stderr as `[i/N] ORIGIN -> DEST DATE`.

## Fetch modes (flights)

- **sweep**: one Chrome TLS session (HTTP/2 multiplex). POST the owned shopping RPC, parse `wrb.fr` itineraries, fall back to the owned HTML card parser only if that misses. Fast shortlist. No Chromium. No 4.5s inter-query delay. Empty/drift/5xx retries once after 50 ms; happy path does not sleep. HTTP 429 resets TLS, waits 50 ms, and continues remaining jobs on a fresh session. A sequential remesure harness may still stop on 429.
- **detail**: existing Playwright path. Max evidence (times, bags, full card set). Current 4.5s+jitter pacing.
- **auto** (default): sweep when the invocation has 3+ flight queries, detail for 1–2. If sweep returns empty or a block, fall back to detail once for those legs only (`fetch_backend: sweep_then_detail` on stderr and in `--save` JSON). Unknown airports / shopping rejects and compact markup misses fail immediately without Chromium.

Use sweep to shortlist a 10–20 route batch. Use `--bags N` / `--carry-on` on sweep when the user asked for bags. Use `--fetch detail` (or a second invocation) when they want times or the full card set. Do not mix backends across legs of one report unless that fallback fired.

For “when is this route cheap?” use `viajante dates ORIGIN-DEST --from --to` (31-day cap, English week calendar plus sparkline and min/median/max from priced days only; `summary` omitted under three priced days). For a stay, add `--nights N` so it is one packaged calendar, not a month of one-day searches. For “around this date, ±N days, then price the winner” use `viajante flex ORIGIN-DEST --around DATE --flex N` (calendar grid, then one shopping search on the cheapest legal day). A calendar miss or empty window is empty: no invented fare. Named `--depart-window` post-filters shop cards on dates sweep-fallback, flex winning-day shop, and explore dest pricing the same way as `search_flights`; compact calendar cells have no clock and stay unfiltered. For “where is cheap from this airport?” use `viajante explore ORIGIN --from --days`. For flights plus a hotel on overlapping dates, use `viajante trip` / `search_trip` (owned fare + stay + sum; omit the sum if either side misses, dates do not overlap, or currencies differ). `--nearby` is opt-in same-city IATA on flights, dates, flex, explore, and trip (default off; named open-jaw airports stay; no invented codes). Do not brute-force comma date lists or every airport when these commands exist. `viajante airports tokyo` resolves IATA codes offline.

## Timing

Sweep inter-query delay is 0. Detail and hotels still sleep about 4.5 to 6 seconds between queries on purpose. Never shorten detail/hotel delays or parallelize Google Flights or Booking.com requests.

## Destination triage (before date buffers)

Do **not** open with a full outbound×return date matrix across many cities. There is no implied home hub: use the origin the user named. Prefer a sweep shortlist on fixed dates, then `--fetch detail` on 1–3 finalists. Prefer:

1. **Shortlist from vibe + expected band**. Drop cities that are clearly out of budget before scraping. The MAD table below is only for MAD-origin weekend/puente trips; do not treat Madrid as the default origin.
2. **Fixed natural dates first** — one outbound + one return per destination (e.g. Fri→Tue for a Monday holiday bridge). `--top 3`, `--save` if comparing.
3. **±1 day only on 1–3 finalists** the user picks or that already look competitive. Never expand dates on the whole longlist in one batch. Around/±N on a named route is `viajante flex`, not a comma date list. Cheapest week is `viajante dates`.
4. On partial failure, retry only the failed legs.

### Rough MAD direct RT bands (cabin only; MAD origin only)

Order-of-magnitude for short MAD weekend/puente trips, **direct** ida+vuelta, `--baggage-buffer 0`. Not live quotes; holidays and lead time move them a lot. Recheck with a fixed-date scrape before recommending. Skip this table when the origin is not MAD.

| Band | Typical RT (EUR) | Destinations (examples) | When to bother with ±1 |
|------|------------------|-------------------------|------------------------|
| Cheap short-haul | ~50–100 | OPO, LIS, other nearby Iberia/Ryanair hops | Often worth it: small EUR swings, more useful daylight |
| Mid classic | ~150–250 | FCO/ROM, BUD, RAK | Only if fixed dates are already near budget or user wants that vibe |
| Expensive for a puente | ~280+ | PRG, PMO, NAP (and similar on holiday peaks) | Skip ±1 unless the user insists; fixed dates usually already expensive |

Use bands to **exclude or deprioritize**, not to invent prices in the reply. After a live scrape, report real numbers; if a city lands far above its band, say so and do not expand dates unless asked.

## `--save`

Use `--save results/<name>.viajante.json` for date matrices, round trips, or downstream parsing. Skip it for a single price answer in chat. Paths under `results/` and `*.viajante.json` are gitignored.

Read `queries[].status`. `"ok"` with empty `offers` is not a fetch failure. Successful flights may carry `google_flights_url`, `typical_eur` / `vs_typical` / `vs_typical_pct`, and `stops_compare`. Trip reports add `trip_total` only when both sides hit. Hotel reports also carry `provider`, `price_basis`, `applied`, `eligible_count`, and evidence enums. Do not invent keys.

## Agent rules

- Use the CLI or the installed `search_flights` / `search_dates` / `search_flex` / `search_trip` / `search_explore` / `lookup_airports` / `search_hotels` APIs. Do not write a one-off scraper.
- Run provider queries sequentially.
- Do not add flags or code that shorten detail or hotel delays or backoff. Sweep already uses a zero inter-query delay; do not parallelize.
- After rate-limit failures, stop for 30-60 minutes before another search. Sweep may already have continued remaining jobs on a fresh TLS session; that is not permission to start a new batch.
- Never invent a fare, typical, token, or via list. Occupancy, alliance, depart-window, and via/exclude-via are only what the prompt named (omit occupancy when there is no count; do not invent an alliance code or a via list). Overnight IST `keep_connect` is still `require_overnight ∪ via`. Around/±N is `viajante flex`; cheapest week is `viajante dates`; packaged RT stays stay packaged.

### Flights

- Keep the scrape locale on English (`hl=en` / `lang=en`, `locale=en-US`) for stable rendered evidence and the JSON `locale: "en"` contract. Planner prompts may be any language; fetch queries stay English.
- Ranking adds 70 EUR to known low-cost fares by default when bag counts are still unknown. `--bags N` / `--carry-on` put those counts on the shopping request so prices come back for that selection. Default is unset. If a compact card includes checked/carry counts, they are parsed; missing bag data stays omitted. Default `--sort ranked` orders by that total; `--sort fare` / `--sort price` order by cabin fare; `--sort duration` by elapsed time; `--sort departure` / `--sort arrival` by local clocks. `--depart-window 06:00-20:00` (or hours `6-20`) drops departures outside that inclusive window on `search_flights` / `search_trip` and on dates sweep-fallback, flex winning-day shop, and explore dest cards that carry a clock. Compact date-grid cells have no clock; do not invent one. `--airlines` / `--exclude-airlines` / `--alliance` / `--exclude-alliance` ride the shopping request (alliances have no member list here). `--via` / `--exclude-via` parse then filter on owned `layover_city` / `legs[].layovers`. Unknown layover cannot prove include (drop) or exclude (keep). Nonstops drop for `--via` and stay for `--exclude-via`. Never invent a via list. `--nearby` expands origin or dest to owned same-city IATA; default off; named open-jaw airports stay; never invent a code. Occupancy (`--children`, `--infants-in-seat`, `--infants-on-lap`) and `--currency` / `--country` are query fields; omit occupancy from the plan when the user gave no count. Report the ranked total when a buffer was added. Use `--baggage-buffer 0` for hand luggage only.
- The low-cost list is partial. Never tell the user an airline includes a bag because it is absent from the list.
- Remind the user to verify checked baggage on Google Flights before booking.
- Successful flight JSON includes `google_flights_url` on the query and each offer when viajante can build it from owned route/date/cabin/occupancy/currency (or an owned `booking_token`). Multi-city uses the owned tfs encoder. Omit the field only when that encode cannot run and there is no token. Do not invent a token or a fare.
- When an offer has `typical_eur` / `vs_typical` / `vs_typical_pct`, print the English `typical_deal` line (e.g. `below typical 340 € (−15%)`). Packaged round-trip uses the same-stay calendar median (same origin/destination and nights). `cheapest_date` / `cheapest_eur` (when present) are the cheapest owned day in that same window. If those fields are null or omitted, skip the comparison. Do not invent a market average.
- Successful searches also print cheapest nonstop vs cheapest 1-stop from that same parsed set (cabin fare). `--save` / MCP `stops_compare` is the same pair. A missing bucket is omitted (`no nonstop` on the CLI when only connections remain). This is not a second Google request.

### Hotels

- Free cancellation is on by default; use `--allow-non-refundable` only after explicit user consent.
- `--compare-cancellation` runs two sequential Booking searches (with the free-cancellation chip, then without) and prints a joined price table. Do not combine it with `--allow-non-refundable`. Do not parallelize. If one of the two queries fails, skip the join.
- `--adults` defaults to 2. Override for solo travelers.
- `--min-rating` is applied locally after the scrape. Booking is 0–10; Google Hotels is 0–5. It is not a Booking chip.
- Treat cancellation, `lodging_kind`, and bed/bedroom/bathroom counts as observed evidence. Do not present unknown card evidence as confirmed. Do not guess “hotel” from the property title.
- Remind the user to verify the final total and cancellation terms on Booking.com before booking.
- `viajante trip` / `search_trip` is flights then hotels, one lock. Print owned fare + stay + sum when dates overlap and both sides return prices. Omit `trip_total` if either side misses, dates do not overlap, or currencies differ. Flight shop uses the same owned bags/via/airlines/`price-cap` post-filters as `search_flights`; unnamed stays unset. Never invent a missing side.

## Second opinion in the browser

viajante does not scrape other OTAs or hotel official sites. After Booking, for **1–3 finalists** (the stays you would actually book, or the ones the user names), use the user's browser harness (Cursor browser / computer-use / Playwright MCP — whatever this session has). Skip this step if there is no browser tool.

1. Google the property title + city + check-in + check-out + adult count.
2. List whatever useful options show up: official site, chain site, other aggregators. Do not rank or prefer one source over another.
3. Only quote a price if the page shows the **same dates, occupancy, and a visible total**. Snippets, ads, and “from €X” are not quotes.
4. Report those figures as an **unverified second opinion**, with the URL. Do not merge them into `--save` JSON, do not write a scraper, and do not run this for every card in a long Booking list.
5. If dates or cancellation terms do not match, say so. The user still confirms the total on the site they book.

## Recovery

| Situation | Action |
|-----------|--------|
| `no_results` | Stop. Do not retry. Do not wait. |
| `rejected` | Stop. The route or date was invalid. Check IATA codes with `viajante airports`. |
| `blocked` | Wait 30-60 minutes. Sweep may have already fallen back to detail once. HTTP 429 already reset TLS and continued remaining jobs on a fresh session; do not start a new batch. |
| `markup_drift` | Stop. Do not retry the same parse. Sweep HTTP may already have retried empty/drift/5xx once after 50 ms. |
| `(no eligible offers)` / `(no eligible stays)` with exit 0 | Widen filters or try other dates. Do not retry the same query as a fetch failure. |
| `browser_unavailable` | Run `uv run playwright install chromium`. |
| `fetch_failed` / exit 2 | Wait 30-60 minutes. Retry failed queries only. For Booking, inspect `booking-last-failure.html` in the state dir before retrying. |
| Exit 3 (partial failure) | Re-run only the failed route or date legs. Do not re-run the whole batch. |
| Consent or markup break | Delete `pw_state_google.json` or `pw_state_booking.json` in the state dir, then retry one query. |

Exit codes: `0` all queries finished, `1` bad input, `2` all queries failed, `3` some failed.

## Browser state

Stored outside the repo at `VIAJANTE_STATE_DIR` or the XDG state dir. Delete `pw_state_google.json` or `pw_state_booking.json` if that provider's consent flow breaks. Booking dumps `booking-last-failure.html` next to the session file when cards never appear.

## Tests

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
uv run viajante bench
uv run viajante bench --prompts
```

`viajante bench` is the weekday speed loop (`gate` + `score_ms`). `--prompts` is the graded quality contract (offline deterministic tiers; DeepSeek 1–100 judge is opt-in via `VIAJANTE_BENCH_JUDGE=1` and `DEEPSEEK_API_KEY` / `VIAJANTE_JUDGE_KEY`). Nested or broken judge JSON is recovered when score and reason are present. Rows print `plan_ms`; optional `VIAJANTE_BENCH_SWEEP=1` / `--timeit-sweep` times HTTP sweep for up to 8 planned flights (never the keep metric). Keep baseline remains **97.9**. Last judged **95.7** is not a keep. Gate is suite + `fail:0`. `score_ms` is not the product trophy. Do not delete `tests/prompts/` to make the speed loop look better.
