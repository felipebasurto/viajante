# Agent notes for viajante

## Where to edit

- `tfs` bytes, cabin, or adults in the Google Flights URL: `src/viajante/tfs.py`
- Compact shopping RPC encode/parse: `src/viajante/google_flights_rpc.py`
- Google CSS, consent, empty vs markup, sweep HTTP client: `src/viajante/google_flights.py`
- Routes, LCC buffer, or flight ranking: `src/viajante/flights.py`
- Booking URL, chips, or DOM cards: `src/viajante/booking.py`
- Hotel evidence filters or ranking: `src/viajante/hotels.py`
- Delays or retry classification: `src/viajante/orchestration.py`
- Chromium session: `src/viajante/browser.py`
- `--save` or the state directory: `src/viajante/storage.py`
- Flags or printed tables: `src/viajante/cli.py`
- Offline keep-or-revert bench: `src/viajante/bench.py`
- Loop protocol: `program.md` (humans edit this to steer)
- Bench baseline: `bench-baseline.json` (update only when a human merges a win)
- Owned parse corpus: `tests/bench/`
- Domain types or JSON keys: `src/viajante/models.py`
- Raw card text to numbers/enums: `src/viajante/parsers.py`
- Offline IATA lookup: `src/viajante/airports.py`
- Cheapest-per-day calendar: `src/viajante/dates.py`
- Explore destinations from an origin: `src/viajante/explore.py`

`google_flights.py` owns URL building, consent, card parsing, typed provider failures, the sweep HTTP client, and `GoogleFlightsSource`. `google_flights_rpc.py` owns the compact shopping request and `wrb.fr` parse. `booking.py` owns Booking.com URL/chips, consent, card extract, and `BookingHotelsSource`. Session lifecycle lives in `browser.py`. `flights.py` and `hotels.py` are the search loops: pure and offline-testable outside the browser source.

## Invariants

- Validate CLI input before starting Chromium. That includes rejecting departure dates in the past.
- Two flight fetch modes, one public contract. `sweep` is one Chrome TLS session (`curl_cffi`) reused across legs: POST the owned shopping RPC, parse `wrb.fr` itineraries, and fall back to the owned HTML card parser only if that compact parse misses. `detail` is the Playwright scrape. `--fetch {auto,sweep,detail}`: auto uses sweep for 3+ flight queries and detail for 1–2. If sweep returns empty or a block, fall back to detail once for those legs only and record `fetch_backend: sweep_then_detail`. Do not re-run successful sweep legs. Shopping `ErrorResponse` (unknown airport / invalid query) and owned Google markup drift fail immediately without Chromium. Do not silently mix backends across legs of one report unless that fallback fired.
- Sweep must not use the 4.5s browser delay (inter-query delay is 0). Sweep must work with no Playwright/Chromium install. One lazy Chromium per process, and only when detail actually runs. Flights block images, media, and fonts. Booking blocks images and media only (fonts stay; they can be required to render the list). Do not spoof a stale Chrome/macOS user-agent; use Playwright's Chromium UA for detail.
- Detail delays: 4.5s + up to 1.5s jitter between queries; 3 attempts with 8s exponential backoff + jitter; browser reset after each failed attempt.
- No flags to shorten detail delays or parallelize requests. Progress output is allowed and goes to stderr.
- Retry only what can succeed on a second try. `no_results`, `rejected`, `blocked`, `markup_drift`, and `browser_unavailable` fail immediately (no second attempt). Booking card-wait timeouts (`BookingResultsTimeout`, still `fetch_failed`) also fail immediately. Do not hammer Booking after a challenge page. Sweep empty or `blocked` may still fall back to detail once; `rejected` and `markup_drift` do not.
- Every offer keeps raw text beside parsed fields (`price`/`price_eur`, `duration`/`duration_hours`, `stops`/`stops_count`). Sweep and detail clocks are 24-hour `HH:MM`. 1-stop cards may also carry `layover_city` / `layover_hours`.
- JSON output only with `--save`. Browser state lives outside the checkout (`VIAJANTE_STATE_DIR` or XDG state dir), and is always written to a temp file and renamed. Booking fetch failures dump `booking-last-failure.html` / `.txt` there for diagnosis; do not commit those files.

### Flights

- Sweep is the fast shortlist (owned shopping RPC, Chrome TLS session, HTML fallback, no Chromium). Detail is max evidence (Playwright, full card set, current delays). Keep EUR, `hl=en` for flights, ranked/fare sort, and the baggage buffer in both modes. Card parsing is owned by viajante and keeps raw stop/price labels. This does not apply to hotels, which use `lang=es` against our own parser.
- The flight scrape locale is `hl=en` with `locale="en-US"` for stable rendered evidence and the existing JSON `locale: "en"` contract. Currency comes from `curr=EUR`, independently of `hl`.
- `max_stops` is 0, 1, or 2 per query; filtering follows each query's value. `--max-layover` / `--min-layover` (hours) filter connecting offers by layover on both sweep and detail; nonstops stay. `--max-duration` drops offers whose elapsed time exceeds that many hours. Default trip kind is one-way. `ORIGIN-DEST:OUT:BACK` without `--trip` is sugar for two one-way queries (out then return). `--trip rt` / `round-trip` and `--trip multi` POST one packaged shopping request. `adults` and `cabin` are query fields (CLI `--adults` / `--cabin`).
- The baggage buffer is an input (`--baggage-buffer`, default 70 EUR), not a constant. A non-zero buffer implies `needs_bag_verify`. Default `--sort ranked` selects `--top` by fare+buffer and hides connections many times slower than the fastest nonstop or shortest offer. `--sort fare` uses fare; `--sort duration` uses elapsed time. `--airlines`, `--exclude-airlines`, `--depart-window`, `--max-duration`, and `--min-layover` are local post-filters after parse, before `--top`. The ranked total must be visible when a buffer was added. Callers must verify baggage on Google Flights before booking.
- `viajante dates ORIGIN-DEST --from DATE --to DATE` is the cheapest-per-day table (date-grid RPC, 31-day cap). A compact calendar miss falls back to one shopping sweep per day on the same TLS session. `viajante explore ORIGIN --from DATE --days N` is the dest shortlist from the Explore catalog, then a priced `--top`. `viajante airports QUERY` is offline IATA lookup. `FlightQuery` rejects unknown codes.
- The low-cost carrier list is partial. Absence from it is not evidence that a fare includes a bag.

### Hotels

- Hotel prices are total-stay prices. Keep requested filters, applied chips, and observed card evidence distinct. `--source booking` (CLI default) is Playwright evidence; `--source google` is the HTTP shortlist. MCP hotel search defaults to Google. Booking ratings are 0–10; Google Hotels ratings are 0–5. Booking search URLs include `order=price`. Hotel reports carry `fetch_ms` / `fetch_backend` like flight reports. Drop non-property titles such as `closed`.
- Hotel searches require free cancellation by default. Only an explicit caller or CLI opt-out may include non-refundable stays. If `oos=1` is applied and the card does not mention cancellation, print `filter applied; card silent` — do not store `free` in JSON.
- `--compare-cancellation` runs two sequential Booking scrapes (free cancellation on, then off) and joins stays by title+address. Do not parallelize. If one query fails, print both results and skip the join.
- `lodging_kind` is observed card evidence (`entire_home` / `private_room` / `hotel` / `unknown`). If the card is silent, apartment/apartamento/casa in the title may infer `entire_home`. Do not infer `hotel` from the word hotel in the title. Do not claim cancellation, lodging kind, or unit counts when unknown.
- Callers must verify the final total and cancellation terms on Booking.com before booking.
- Other OTAs are not scrapers in this tree. After Booking, for 1–3 finalists, the viajante skill says to use the user's browser harness (Google the property; list official site, aggregators, and other hits as options, without preferring one). Unverified second opinion. Do not invent prices from snippets or write them into `--save` JSON.

## Tests

Prefer the locked checkout workflow:

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
uv run viajante bench
```

`viajante bench` is the offline keep-or-revert command. It runs the unittest suite plus `ruff check` / `ruff format --check`, then prints `gate` and `score_ms` (`tests_ms` + owned `tests/bench/` parse). No Chromium. No live Google unless `VIAJANTE_BENCH_LIVE=1`, and that extra `sweep_ms` is never the score. Read `program.md` before running a loop experiment.

`pip install -e .` still works, but `uv` is the reproducible path for this tree. Tests are offline. They must not launch Chromium or use the network. CI runs the suite on Python 3.10 through 3.14.

`tests/test_google_flights.py` pins owned TFS encoding, the compact shopping fixture → `RawFlightCard` seam, and HTML fallback. Test the owned boundary (`RawFlightCard`, typed empty/markup/block failures), not upstream HTML rewriting. `tests/test_booking.py` is the Booking page seam (`build_applied_filters`, cards, empty vs markup). `tests/test_hotels.py` is eligibility, ranking, and the search loop.

`tests/test_json_contract.py` and `tests/test_hotel_json_contract.py` pin the flight and hotel JSON shapes. A renamed or dropped key is a breaking change for anything reading `--save` output. `tests/test_dates.py`, `tests/test_explore.py`, and `tests/test_airports.py` cover the calendar window, explore catalog, and offline IATA lookup. `tests/test_mcp.py` imports FastMCP when the `mcp` extra is installed (`mcp>=1.6,<2`).

Stdio MCP: `uv sync --extra mcp` then `viajante-mcp`. Tools: `search_flights`, `search_dates`, `search_explore`, `search_hotels`, `lookup_airports`. No Streamable HTTP. Keep the one-search process lock.

## Trip-planning search strategy

When helping pick destinations (not a single named route/date), follow `.cursor/skills/viajante/SKILL.md` → **Destination triage**: shortlist by vibe and rough MAD price band, scrape fixed natural dates first, and only then expand ±1 day on 1–3 finalists. Do not brute-force full date matrices across a long destination list in one run.

## Private-data boundary

This tree is the public export. Do not add scraped caches, CSVs, personal trip scripts or routes, reservation data, browser session files, or paths from a private repository. Heuristic price *bands* in the viajante skill are allowed; live scrapes and personal trip JSON are not.
