# Agent notes for viajante

This file is the agent contract: the design stance, where to edit, hard
invariants, and wording rules. Per-feature behavior that callers see is in
[docs/usage.md](docs/usage.md); the reasons behind the design are in
[docs/architecture.md](docs/architecture.md). Argparse, JSON keys, and ranking
defaults live in source. `README.md` is owned elsewhere (do not edit it in a
docs cleanup).

> **Authority:** this checkout (`git@github.com:felipebasurto/viajante.git`) is the source of truth.

## Design stance

Viajante gives an agent superpowers; it does not replace the agent's judgment.
It fetches owned provider evidence fast and refuses to guess. Add evidence (a
field the provider returned) or a primitive the agent can compose. Do not add
summaries, "cheapest is X" lead lines, or recommendation prose to results.

The flight `recommendation` block is an existing shortlist with role labels
(`top_score`, `lowest_price`, `shortest`, `alternative`). Those labels name a pick's
role, not a verdict, and every highlight or trade-off must restate a returned field;
no comparative lead lines. Do not
add new prose labels to any result.

## Where to edit

- `tfs` bytes, cabin, or occupancy in the Google Flights URL: `src/viajante/tfs.py`
- Public Google Flights `ds:1` page decode: `src/viajante/google_flights_page.py`
- Public Flights page GETs and selected round-trip return pages: `src/viajante/google_flights_public.py`
- Public Explore catalog Chromium run (the page's own catalog request, captured): `src/viajante/google_flights_explore_browser.py`; its gate and failure rules stay in `google_flights_public.py`
- Public sweep GET concurrency: `src/viajante/sweep_config.py`
- Compact shopping RPC encode/parse: `src/viajante/google_flights_rpc.py`
- Google CSS, consent, empty vs markup, sweep HTTP client: `src/viajante/google_flights.py`; browser detail source and its CSS card parser: `src/viajante/google_flights_detail.py`
- Reject-only Google consent cookies kept across CLI processes (`google-consent.json`): `src/viajante/consent.py`
- Route specs, metro and nearby expand, trip include/exclude: `src/viajante/flight_routes.py`
- Flight post-filters (clocks, layover, via, overnight, bag requirements): `src/viajante/flight_filters.py`
- Offer normalization, LCC buffer, flight ranking, per-query recommendation: `src/viajante/flight_offers.py`
- Flight evidence stamps (Google Flights URLs, offer evidence): `src/viajante/flight_evidence.py`
- Round-trip / multi-city next-leg completion and package filters: `src/viajante/flight_packages.py`
- Search loop, `search_flights`, or `get_flights`: `src/viajante/flights.py`; Google failure classification: `src/viajante/google_flights_public.py` (`classify_failure`)
- Per-query `recommendation` (requirements, relaxation, score, shortlist): `src/viajante/recommend.py`
- Booking URL, chips, or DOM cards: `src/viajante/booking.py`
- Hotel evidence filters or ranking: `src/viajante/hotels.py`
- Google Hotels HTTP shortlist: `src/viajante/google_hotels.py`, `src/viajante/google_hotels_rpc.py`
- Google Hotels navigation context: `src/viajante/google_hotels_url.py`
- Executing package diagnostics: `src/viajante/runtime.py`
- Delays or retry classification: `src/viajante/orchestration.py`
- Shared provider cooldown state: `src/viajante/ratelimit.py`
- Chromium session: `src/viajante/browser.py`
- `--save` or the state directory: `src/viajante/storage.py`
- Flags: `src/viajante/cli.py`; printed tables and exit codes: `src/viajante/cli_report.py`
- Stdio / local HTTP MCP tools: `src/viajante/mcp_server.py` (descriptions, client-type overrides), `src/viajante/mcp_handlers.py` (parameters and defaults; the client schema is derived from these signatures)
- MCP result envelope: `src/viajante/envelope.py`
- MCP input-error JSON body: `src/viajante/mcp_errors.py`
- MCP server instructions and the `viajante://guide` text: `src/viajante/mcp_guide.py`
- Low-cost carrier list (partial): `src/viajante/flight_offers.py` (`LOW_COST_NAMES`)
- Airline aliases and alliance tfs carrier codes: `src/viajante/carriers.py`
- Offline keep-or-revert bench: `src/viajante/bench.py`
- Bench baseline: `bench-baseline.json` (update only when a human merges a win)
- Owned parse corpus: `tests/bench/`
- Domain types or JSON keys: `src/viajante/models_common.py` (shared primitives, errors, evidence, coverage), `models_flights.py`, `models_dates_explore.py`, `models_hotels.py`, `models_trip.py`, `models_hidden_city.py`, `models_awards.py`, `models_stays.py`, `models_validation.py`; `models.py` only re-exports them
- Same-route median from requested priced days: `src/viajante/typical.py`
- Raw card text to numbers/enums: `src/viajante/parsers.py`
- Offline IATA lookup and metro groups: `src/viajante/airports.py`
- Origin-country cash currency for Google `curr` (no FX): `src/viajante/quote.py`
- Cheapest-per-day window and flex window: `src/viajante/dates.py`
- Explore destinations from an origin: `src/viajante/explore.py`
- Owned trip total (flight fare + hotel stay): `src/viajante/trip.py`
- Opt-in split tickets: `src/viajante/split.py`
- Opt-in Skiplagged MCP (not Google mix-in): `src/viajante/skiplagged.py`; captured responses: `tests/fixtures/skiplagged/`
- Skiplagged hotel search and room rates: `src/viajante/skiplagged_hotels.py`
- Local award CPP, transfer table, imported offers: `src/viajante/points.py`
- Offline evidence-bound itinerary validation and UTC civil-time resolution: `src/viajante/validate.py`, `src/viajante/temporal.py`
- Hotel finalist snapshot and separate room quote: `src/viajante/details.py`
- Fresh re-check of an earlier flight offer: `src/viajante/recheck.py`
- Offline stay blocks and per-person cost split: `src/viajante/stays.py`
- MCP evidence ledger and `verify_answer`: `src/viajante/evidence.py`
- Opt-in observation log, per-query trend, recording hooks: `src/viajante/history.py`
- Saved on-demand `watch` and the `price_history` / `watch_price` tool bodies: `src/viajante/watch.py`; their CLI: `src/viajante/history_cli.py`
- Search control (cancel, deadline, progress): `src/viajante/control.py`
- Repo junk cleaner: `scripts/clean-repo.py`

## Public contract

- CLI (`viajante <cmd> --help`): `flights` (`--split-tickets`), `dates`, `flex`,
  `explore`, `airports`, `hotels`, `hotel-rooms`, `trip`, `hidden-city`,
  `awards`, `points`, `recheck-offer`, `history`, `watch`, `bench`.
- MCP (stdio, or opt-in loopback Streamable HTTP): `search_flights`,
  `search_dates`, `search_flex`, `search_trip`, `search_explore`,
  `lookup_airports`, `search_hotels`, `search_hotel_rooms`, `get_hotel_details`,
  `search_split_tickets`, `search_hidden_city`, `compare_awards`,
  `lookup_transfers`, `validate_itinerary`, `recheck_offer`, `plan_stay_blocks`,
  `split_stay_costs`, `verify_answer`, `price_history`, `watch_price`,
  `get_runtime_info`, `get_guide`. Signatures: `src/viajante/mcp_handlers.py` (derived into the client schema by `mcp_server.py`).
- Library: `get_flights` (a route spec or trip objects; turning prose into a
  route is the caller's job), the `search_*` functions, `get_hotel_details`, and
  `validate_itinerary`.
- `proxy` (CLI `--proxy`, MCP `proxy`) applies to flights, dates, flex, explore.
- A skill's CLI sketch is not argparse. If a flag in a skill is missing from
  `cli.py`, the code wins; do not complete a second CLI contract.

## Wording and evidence rules

- Every MCP tool except `lookup_airports` returns the envelope stamped by
  `envelope.py`. Read `status`, `completeness`, and `empty_reason` before the rows.
  Field rules: [docs/architecture.md](docs/architecture.md#7-one-mcp-envelope).
- Only `provider_empty` may be told to a traveller as "no flights/hotels found".
  `filtered_out` means viajante's filters removed rows the provider returned. `not_loaded`
  means the search did not complete. Never merge the three.
- An unpriced explore destination is `not_loaded`, and so is a date row that
  carries no reason of its own. An unpriced cell does not prove there are no flights.
  A date day shopped on the public page that returns no cards is `provider_empty`.
- `error_code` rides only with the `empty_reason` it supports. An `ok` or
  `partial` result may still carry the worst failure's code.
- A new search tool must stamp through `stamp_search` (or `stamp_split`,
  `stamp_recheck`, `stamp_local` for their shapes). `stamp_search` raises on a
  payload shape it does not recognise and never defaults to `no_results`. A test
  enforces that every tool advertises the envelope schema.
- Unknown never proves include, pass, met, or compliant. Empty is not absence.
- Never invent a fare, typical, token, via list, bag count, bag fee, destination,
  price cap, IATA code, `gl`, ISO 4217 code, room capacity, or exchange rate.
- Results are evidence, not advice. Do not add summaries or recommendation prose
  to them.

## Currency, locale, origin

- Fetch locale is English (`hl=en`; JSON `locale` is `"en"`). Prompts may be in any
  language; product voice is English. No implied home hub.
- Currency is `--currency` / MCP `currency`, or inferred from a named origin
  airport's owned country (JFK USD, LHR GBP, NRT JPY, GRU BRL). If it is not
  proven, ask or error. Google and Booking hotels require a named currency.
  Skiplagged hotels and hidden-city cards are USD. A hidden-city card's
  `hidden_city` comes only from Skiplagged's `attributes`; without them it is null
  (unknown), never false.
- Viajante never converts. The MCP caller does FX. A keep that matches no owned
  card is `currency_mismatch`, with the owned quote stamped, never `no_results`.
- Ask or error when country, destination, or currency is not proven: a city with
  several airports, "Europe", an unnamed origin, or two possible currencies.
- `gl` / MCP `country` is Google origin-market geolocation. Omit it when unset.
  Do not default it to a hub or pass a destination ISO.

## Flights

- `auto` and `sweep` read Google's public results page over the shared Chrome-TLS
  HTTP/2 client. `auto` never selects browser detail. `detail` uses Playwright
  (`viajante[browser]`) and is explicit only.
- A public sweep never falls back to detail after an empty page, a parse failure,
  or a provider block. Its `ds:1` data is extracted as balanced JSON and never
  executed. Do not fabricate an RPC envelope. Flights, dates, and flex send no
  unsigned shopping or calendar RPC. A status 13 on the explore catalog is recorded
  as a cooldown like any other.
- Refused before any network call: checked bags, `carry_on` 0, alliance exclusion
  on the public path, and multi-city on sweep (use explicit `--fetch detail`).
  Detail refuses every bag and carrier filter. Never silently drop a filter.
- A missing filter echo on a page is `markup_drift`, never an unfiltered result.
  Google does not apply airline exclusions, so the loop drops excluded and
  carrier-unknown cards.
- Clock, layover, via, overnight, duration, airport, and price-cap filters are
  local, applied after parse and before `--top`. An unknown clock, stop count,
  or layover cannot prove an include or an exclude.
- Public round trips check at most eight outbound candidates, one return page each.
  The provider's package total is the price. Never sum one-way amounts. The result
  is scope-bound.
- `max_stops` is 0, 1, or 2. The product cannot require 3+ stops; say so or refuse.
  Default trip kind is one-way. `ORIGIN-DEST:OUT:BACK` without `--trip` is two
  one-ways. `--trip multi` needs explicit detail.
- Metro codes expand only when named on a one-way or `rt` route. `--nearby` with a
  named metro is an error. A route whose two ends share a metro is rejected.
  Open-jaw, multi-city, dates, flex, explore, hotels, and hidden-city do not take
  metro codes.
- `recommendation` is additive. It never changes `offers` or `--sort`, never
  relaxes a filter silently (the relaxed requirement is named in
  `relaxed_requirements`), and offers in an unproven or mixed currency are not
  price-compared. Details: [docs/usage.md](docs/usage.md#recommendation-and-shortlist).
- Baggage: an unnamed buffer is 0. A buffer is a ranking allowance, never a fee.
  A non-zero buffer marks `needs_bag_verify`. The low-cost carrier list is partial,
  so its absence proves nothing about bags.
- Schema v2 offers carry immutable `evidence` and `completeness`. A query URL
  reproduces a query, not current availability. Run `validate_itinerary` before
  calling an assembled route compliant. A fail is infeasible; unknown is never pass.
- Date and flex `typical` comes only from at least three priced days of the
  caller's explicit window, on the same route. It is never a market average.
  An ordinary flight search does no hidden 31-day lookup.

## Split tickets, hidden-city, local tools

- Split tickets pair real, separately ticketed one-way quotes only. Never derive a
  leg from a round-trip price or estimate one. Every itinerary says
  `split_ticket: true` and `connection_protected: false`. Never convert. `total` is
  summed only in one shared currency. Hub connections need proven airport and
  timing (UTC via catalogue zones). Caps: `MAX_SPLIT_HUBS` = 5, `MAX_VIA` = 5.
  The search stops at a recorded cooldown. Details: [docs/usage.md](docs/usage.md#split-tickets-opt-in).
- Named filters apply to split itineraries from owned evidence only. A hub split reads its
  journey clocks, each ticket duration, and the hub connection; `via` and `exclude_via` name
  hubs. A mixed pair has no connection, so any connection filter drops it. Unknown clocks
  never prove a named bound. `src/viajante/split_filters.py`.
- Hidden-city is Skiplagged-only and never mixed with Google evidence. Its keep is
  USD or omitted. Do not invent a beyond city.
- `compare_awards`, `lookup_transfers`, `validate_itinerary`, `plan_stay_blocks`,
  `split_stay_costs`, `verify_answer`, `lookup_airports`, and `get_runtime_info`
  are local. They make no provider call and may run during a search.

## Hotels

- Prices are total-stay. Keep requested filters, applied filters, and observed card
  evidence distinct. `filter applied; card silent` is not confirmed free cancellation.
- Free cancellation is required by default. Only an explicit opt-out may include
  non-refundable stays.
- Ratings: Google 0–5, Booking 0–10. Drop non-property titles such as `closed`.
- Lodging kind and property type come from observed card evidence only. Never infer
  `hotel` from the word "hotel" in a title. A conflict leaves both unknown.
- `property_matrix` is never ranked, and a null means absent from that stay's
  returned offers, not unavailable. `near` is a point the caller names; none is
  assumed. Distances are straight-line.
- Skiplagged is opt-in and never mixed with Google or Booking rows. Its quotes are
  USD, hotels take at most 10 adults and 9 rooms, hidden-city flights at most 9
  adults, and `resolved_place` is the place it actually searched. A hotel's total is
  the stay total from the provider's table; the structured price is nightly and is
  never used as a total. A card whose structured price is not USD is dropped. Room rates cover at most 5 rooms per request. Ids are valid only
  within this process. A name matches exactly after normalization; no match or
  several matches is `no_results`, never a guess.
- Room rates never apply to the original price, and their capacity never proves a
  combined party fits across rooms. Google's descriptions are property text, not
  priced-room evidence; a hostel total may be a dormitory bed.
- `trip` runs flights then hotels under one lock. Its `trip_total` is present only
  when both sides are priced, the dates overlap, and the currencies match. Child and
  infant occupancy are rejected; hotel occupancy is adults-only.
- `--compare-cancellation` runs two sequential Booking searches. If either fails,
  the join is skipped.
- Callers verify the final total and the cancellation terms on the provider before
  booking.

## Price history

- Recording is opt-in: `VIAJANTE_PRICE_HISTORY=1`, or a `watch` run. Only a real
  search success with an offer is logged. MCP cache replays, failures, empty
  results, and cooldown-blocked searches are never logged.
- A series is one query key in one currency. Series are never merged or converted,
  and one observation is no trend. Never forecast or estimate a missing day.
- `watch_price` runs only when called: no scheduler, no notification. A proxy is
  never stored. `watch_price` is the one tool here that writes.
- Only a missing log is an empty log. Any other read error stops an append or
  `--clear` and is reported.

## Stay arithmetic and temporal evidence

- Stay blocks group consecutive nights with identical people, not identical
  headcounts. Cost splits allocate only to occupants, with exact cents, and name
  uncovered nights. Neither fetches prices nor converts currency.
- IANA zones come from the airport catalogue. A missing, ambiguous, or nonexistent
  civil time stays unknown. `arrival_deadline` with an explicit offset is compared in
  UTC. A naive deadline is local at the arrival airport.
- Temporal order does not prove connection protection, immigration eligibility, or
  transfer margins.

## Rechecks

- `recheck_offer` is one fresh search. It skips the MCP replay cache, runs under the
  search lock, and respects the Google cooldown.
- It matches on a full segment identity (flight number, origin, destination,
  departure clock). Missing identity is `incomplete_identity`, and no search is sent.
- `check_failed` is not evidence that an offer is gone. Never turn a rate limit
  into "gone". `previous.source` is `search_evidence` only when the ledger holds a
  matching offer; anything else is `caller_supplied`, and is not recorded.
- A currency different from the offer's own is refused, never converted.

## MCP control

- One search runs per process. A second search fails at once with
  `search_in_progress`. That is not an MCP `-32001` timeout.
- A client `progressToken` receives `notifications/progress`. A cancellation stops
  the search between queries, retries, and sleeps, and frees the lock. A cancelled
  search is never cached, never recorded in the ledger, and never writes a cooldown.
- `deadline_seconds` returns a partial result. Unfinished queries are `deadline`,
  never `no_results`, and the result is not cached.
- A `SearchDeadline` is classified as code `deadline` and is never retried; never
  let it fall into a retry, a `fetch_failed` or a `no_results`. Cancellation is a
  `BaseException` and passes through.
- An identical successful search is replayed for 5 minutes (`cached: true`). The
  replay cache holds at most 20 entries.
- Invalid input is an `isError` result: the SDK's exact `Error executing tool <name>: `
  prefix, then `{"error": {"code": "invalid_parameter", ...}}`. A viajante-side
  decode error or an unreadable result shape is never blamed on the caller.
- The server needs `mcp>=2.3.0` (MCP SDK 2.x, `MCPServer`). Loopback HTTP passes
  `transport_security` to `run()` explicitly. It has no auth, binds loopback by default, is never hosted, and has no
  `remotes` in `server.json`.
- Freshness hints (SEP-2549): `tools/list`, `resources/*`, `resources/templates/list` and
  `server/discover` carry `ttl_ms` 3600000 and `cache_scope` `public`. They are fixed for
  a running server and no answer depends on the caller. Tool results have no hint in the
  protocol and get none here. Do not put caller-specific data in these results.
- Keep the server instructions and `viajante://guide` consistent with each other and
  with this file.

## Providers, cooldowns, and pacing

- A direct Google 429, or a data-less RPC status 13, writes
  `google-rate-limit.json` in the state directory. The cooldown is a guess: 2 minutes,
  doubling per repeat up to 30 minutes, or a named `Retry-After`. While it runs,
  every Google flight or hotel search in any process fails `blocked` with
  `rate_limited: true` and sends nothing. A proxied 429 writes no cooldown.
- A raw status 13 does not prove an IP block or a cause. Diagnostics carry the
  endpoint host and path, the HTTP or RPC status, `request_sent`, `attempts`, and
  the cooldown basis. Never include query parameters.
- HTTP 429 and status 13 stop pending sweep work. Nothing replays and nothing falls
  back to detail. Empty results, drift, and HTTP 5xx may be retried once.
- A Skiplagged 429 writes `skiplagged-rate-limit.json` and does not retry. Live
  Skiplagged calls are paced 1 second apart.
- Detail: 4.5 s plus up to 1.5 s of jitter between queries; 3 attempts with 8 s
  exponential backoff; a browser reset after each failed attempt. No flag shortens
  these delays, and nothing parallelizes them. Progress goes to stderr.
- Booking challenges are not hammered. Card-wait timeouts fail at once.
- `VIAJANTE_SWEEP_MODE` accepts `standard` (8 concurrent GETs) or `conservative`
  (2). Anything else fails before network work. It is a local concurrency choice,
  not a provider quota.
- Use Playwright's Chromium user agent for detail. Do not spoof a stale browser UA.

## State and private data

- JSON output only with `--save`. Browser state and failure dumps live under
  `VIAJANTE_STATE_DIR`, `$XDG_STATE_HOME/viajante`, or `~/.local/state/viajante`,
  never in the checkout. Writes are temp-file then rename.
- `google-consent.json` in that state dir holds only the Google-domain cookies a
  declined consent (reject) left behind. Accepted consent is never stored. It
  expires after 30 days and is ignored when unreadable. Do not extend it to accept
  or non-Google cookies.
- Do not commit `booking-last-failure.html` / `.txt`, scraped caches, CSVs, personal
  trip scripts or routes, reservation data, browser session files, or paths from a
  private repository. An origin-specific fare table is not allowed.

## Install and version

- Primary agent install is `npx -y @viajante/mcp` (needs `uv` and Python 3.10+). The
  no-Node form is `uvx --from viajante[mcp] viajante-mcp`. Keep `README.md`, `llms.txt`,
  `mcp_guide.py`, the skill, and the landing page's copy-message on that same order.
- Check `viajante --version` or MCP `get_runtime_info` before using a new feature.
  Hotel JSON carries `viajante_version`. An npm MCP and a separately installed uv
  tool can run different versions; an unpinned `uvx` may reuse an old install.
  Confirm the executing version before relying on new parameters.

## Tests and bench

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
uv run viajante bench
```

- The suite is offline: no network and no Chromium. It isolates itself from the
  caller's environment through `tests/_isolate.py`. Every `tests/test_*.py` must begin
  with `import _isolate  # noqa: F401`, and a test enforces that. Keep new tests off
  the real state directory.
- Pin owned seams: request bytes, recorded provider bodies, filters, ranking, JSON
  keys. A renamed or dropped JSON key is a breaking change.
- `viajante bench` is the keep-or-revert gate. Do not optimize `score_ms`. Live
  Google runs only with `VIAJANTE_BENCH_LIVE=1`, and that extra time is never the
  score. Update `bench-baseline.json` only when a human merges a win.
- Playwright is the extra `viajante[browser]`. The MCP extra is `viajante[mcp]`.
- Known limits and open debt: [docs/architecture.md](docs/architecture.md#known-limits-and-debt).
