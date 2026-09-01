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

> **CLI contract:** the lines below are usage sketches. Operative flags and
> defaults live in `src/viajante/cli.py` (`uv run viajante <cmd> --help`). This
> skill is not argparse. If a sketch omits a flag the code defines, use the code.
> Do not invent a flag the code does not have.

```bash
uv run viajante flights ORIGIN-DEST:YYYY-MM-DD
uv run viajante dates ORIGIN-DEST --from YYYY-MM-DD --to YYYY-MM-DD
uv run viajante flex ORIGIN-DEST --around YYYY-MM-DD --flex N
uv run viajante explore ORIGIN --from YYYY-MM-DD
uv run viajante airports QUERY
uv run viajante hotels LOCATION CHECK_IN CHECK_OUT
uv run viajante trip ORIGIN-DEST:YYYY-MM-DD[:YYYY-MM-DD] --hotel LOCATION
uv run viajante bench
uv run viajante bench --prompts
```

Route grammar: `JFK-LHR:2026-09-15`, or several dates comma-separated on one route. `JFK-NRT:2026-10-09:2026-10-20` without `--trip` is sugar for outbound + return as two one-way queries. `--trip rt` POSTs one package. You can still pass a return leg as a second route.

`--nearby` is opt-in same-city IATA on flights, dates, flex, explore, and trip (default off; named open-jaw airports stay; no invented codes). `--exclude-airports` / `--include-airports` are named owned IATA lists (same parse as via). Include is dests only. Exclude wins on overlap. Named origin/dest in an exclude list is empty. Do not rewrite to a substitute. `--exclude-regions` is explore-only (owned IANA tz prefixes; unknown tz cannot prove keep). `--via` / `--exclude-via` / `--no-overnight` / `--require-overnight` filter owned layover city+clock; unknown cannot prove include or exclude. `--arrive-before` / `--depart-after` are named HH:MM. Named `--price-cap` is a local owned-EUR post-filter. Compact calendar cells and Explore catalog places are not offers and stay unfiltered.

MCP (stdio, no auth): `uv sync --extra mcp` then `viajante-mcp`. Tools: `search_flights`, `search_dates`, `search_flex`, `search_trip`, `search_explore`, `lookup_airports`, `search_hotels`. Signatures: `src/viajante/mcp_server.py`. Keep the one-search process lock.

## Smoke

```bash
# Offline (always safe)
uv run python -m unittest discover -s tests -v

# Fast HTTP shortlist (no Chromium)
uv run viajante flights JFK-LHR:2026-09-15 --fetch sweep --top 3
uv run viajante flights BOS-LHR:2026-09-18 --nearby --fetch sweep --top 3
uv run viajante flights JFK-SIN:2026-11-03 --via IST --exclude-via DXB --fetch sweep --top 3
uv run viajante dates BOS-LHR --from 2026-09-01 --to 2026-09-14 --fetch sweep
uv run viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14 --sort duration
uv run viajante flex BOS-LHR --around 2026-09-12 --flex 3 --nights 7 --fetch sweep
uv run viajante explore JFK --from 2026-09-15 --days 7 --exclude-airports HND
uv run viajante explore JFK --from 2026-09-15 --days 7 --include-airports NRT,HND
uv run viajante explore NRT --from 2026-09-15 --days 7 --exclude-regions asia
uv run viajante explore JFK --from 2026-09-15 --days 7 --sort duration
uv run viajante explore JFK --from 2026-09-15 --days 7 --no-overnight any
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

- **sweep**: one Chrome TLS session (HTTP/2 multiplex). POST the owned shopping RPC, parse `wrb.fr` itineraries, fall back to the owned HTML card parser only if that misses. Fast shortlist. No Chromium. No 4.5s inter-query delay. Empty/drift/5xx retries once after 50 ms; happy path does not sleep. HTTP 429 resets TLS, waits 50 ms, and continues remaining jobs on a fresh session.
- **detail**: Playwright. Max evidence. Current 4.5s+jitter pacing.
- **auto** (default): sweep when the invocation has 3+ flight queries, detail for 1–2. If sweep returns empty or a block, fall back to detail once for those legs only (`fetch_backend: sweep_then_detail`). Unknown airports / shopping rejects and compact markup misses fail immediately without Chromium.

Use sweep to shortlist a 10–20 route batch. Use `--bags N` / `--carry-on` on sweep when the user asked for bags. Use `--fetch detail` when they want times or the full card set. Do not mix backends across legs of one report unless that fallback fired.

For “when is this route cheap?” use `viajante dates` (31-day cap; `summary` omitted under three priced days). For a stay, add `--nights N`. For “around this date, ±N days, then price the winner” use `viajante flex` (calendar then one shop; miss = empty). For “where is cheap from this airport?” use `viajante explore`. For flights plus a hotel on overlapping dates, use `viajante trip` / `search_trip` (omit the sum if either side misses, dates do not overlap, or currencies differ). Do not brute-force comma date lists or every airport when these commands exist. `viajante airports tokyo` resolves IATA codes offline.

Named `--sort` / `--baggage-buffer` on dates and explore follow `src/viajante/cli.py` and `src/viajante/flights.py`. Unnamed dates stay date order. Unnamed explore stays cheapest-first. `--baggage-buffer` ranks explore dests only when sort is ranked. Compact cells have no duration or clock: do not invent one to sort. Sort is order, not a cut. Compact cells omit the buffer stamp. The buffer (`DEFAULT_BAGGAGE_BUFFER_EUR` in `flights.py`) is ranking, not a fare.

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

Read `queries[].status`. `"ok"` with empty `offers` is not a fetch failure. JSON keys live in `src/viajante/models.py`. Successful flights may carry `google_flights_url`, `typical_eur` / `vs_typical` / `vs_typical_pct`, and `stops_compare`. Dates priced rows stamp typical from the owned calendar `summary` (omit when thin). Explore dests that already ran a shopping POST stamp typical from the same-route calendar path flights use; catalog-only dests omit. Compact calendar cells and Explore catalog places omit `stops_compare`. Trip reports add `trip_total` only when both sides hit. Do not invent keys.

## Agent rules

- Use the CLI or the installed MCP tools. Do not write a one-off scraper.
- Run provider queries sequentially.
- Do not add flags or code that shorten detail or hotel delays or backoff. Sweep already uses a zero inter-query delay; do not parallelize.
- After rate-limit failures, stop for 30-60 minutes before another search. Sweep may already have continued remaining jobs on a fresh TLS session; that is not permission to start a new batch.
- Never invent a fare, typical, token, or via list. Copy only what the prompt **named** (occupancy, alliance, clocks, via, overnight, include/exclude airports, exclude-regions). Unnamed stays unset. Vibe words do not invent a clock, dest, alliance code, buffer, or bags. Named `max_stops` 0 plus a named positive `min_layover` keeps both and stamps one English note. Around/±N is `viajante flex`; cheapest week is `viajante dates`; packaged RT stays packaged.

### Flights

- Keep the scrape locale on English (`hl=en` / `lang=en`, `locale=en-US`). Planner prompts may be any language; fetch queries stay English.
- Ranking adds `DEFAULT_BAGGAGE_BUFFER_EUR` (`src/viajante/flights.py`) to known low-cost fares when bag counts are still unknown. That 70 is **ranking, not a fare**. `--bags N` / `--carry-on` put counts on the shopping request. Default is unset. Never invent a bag count from the buffer. Report the ranked total when a buffer was added. Use `--baggage-buffer 0` for hand luggage only. The low-cost list is partial — never tell the user an airline includes a bag because it is absent.
- Remind the user to verify checked baggage on Google Flights before booking.
- Print `typical_deal` when `typical_eur` is present. If those fields are null or omitted, skip the comparison. Do not invent a market average.
- Print cheapest nonstop vs cheapest 1-stop from the same parsed set when `stops_compare` exists. This is not a second Google request.

### Hotels

- Free cancellation is on by default; use `--allow-non-refundable` only after explicit user consent.
- `--compare-cancellation` runs two sequential Booking searches. Do not combine it with `--allow-non-refundable`. Do not parallelize. If one of the two queries fails, skip the join.
- `--adults` defaults to 2. Override for solo travelers.
- `--min-rating` is applied locally after the scrape. Booking is 0–10; Google Hotels is 0–5. It is not a Booking chip.
- Treat cancellation, `lodging_kind`, and bed/bedroom/bathroom counts as observed evidence. Do not present unknown card evidence as confirmed. Do not guess “hotel” from the property title.
- Remind the user to verify the final total and cancellation terms on Booking.com before booking.
- `viajante trip` / `search_trip` is flights then hotels, one lock. Print owned fare + stay + sum when dates overlap and both sides return prices. Omit `trip_total` if either side misses, dates do not overlap, or currencies differ. Never invent a missing side.

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

`viajante bench` is the offline gate (`gate` + `score_ms`). `--prompts` is the graded quality contract. Keep metric is `judge_mean` in `program.md` (strictly above the last kept run). Gate is suite + `fail:0`. `score_ms` is not the keep. Do not delete `tests/prompts/` to make the gate look better. Do not open `tests/prompts/holdout.jsonl`. Do not edit `JUDGE_SYSTEM_PROMPT`.
