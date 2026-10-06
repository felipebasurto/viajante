# Agent notes for viajante

Operative argparse, JSON keys, and ranking defaults live in source. This file is
the agent contract: where to edit, traps, and what must not be invented.
`README.md` is owned elsewhere (do not edit it in a docs-cleanup).

> **Authority:** this checkout (`git@github.com:felipebasurto/viajante.git`) is the source of truth.

## Design stance

Viajante gives an agent superpowers; it does not replace the agent's judgment.
It fetches owned provider evidence fast and refuses to guess. The calling agent
reads the payload, weighs trade-offs, and recommends. Do not add summaries,
"cheapest is X" lead lines, or recommendation prose to results; add evidence
(a field the provider returned) or a primitive the agent can compose. See
`docs/architecture.md`.

## Where to edit

- `tfs` bytes, cabin, or occupancy in the Google Flights URL: `src/viajante/tfs.py`
- Compact shopping RPC encode/parse: `src/viajante/google_flights_rpc.py`
- Google CSS, consent, empty vs markup, sweep HTTP client: `src/viajante/google_flights.py`
- Routes, LCC buffer, nearby expand, flight ranking, or `get_flights`: `src/viajante/flights.py`
- Booking URL, chips, or DOM cards: `src/viajante/booking.py`
- Hotel evidence filters or ranking: `src/viajante/hotels.py`
- Google Hotels HTTP shortlist: `src/viajante/google_hotels.py`, `src/viajante/google_hotels_rpc.py`
- Google Hotels navigation context: `src/viajante/google_hotels_url.py`
- Executing package diagnostics: `src/viajante/runtime.py`
- Delays or retry classification: `src/viajante/orchestration.py`
- Shared provider cooldown state: `src/viajante/ratelimit.py`
- Chromium session: `src/viajante/browser.py`
- `--save` or the state directory: `src/viajante/storage.py`
- Flags or printed tables: `src/viajante/cli.py`
- Stdio / local HTTP MCP tools: `src/viajante/mcp_server.py`, `src/viajante/mcp_handlers.py`
- MCP input-error JSON body: `src/viajante/mcp_errors.py`
- MCP server instructions and the `viajante://guide` text: `src/viajante/mcp_guide.py`
- Low-cost carrier list (partial): `src/viajante/flights.py` (`LOW_COST_NAMES`)
- Airline aliases and alliance shopping codes: `src/viajante/carriers.py`
- Offline keep-or-revert bench: `src/viajante/bench.py`
- Bench baseline: `bench-baseline.json` (update only when a human merges a win)
- Owned parse corpus: `tests/bench/`
- Domain types or JSON keys: `src/viajante/models.py`
- Typical vs same-route calendar median: `src/viajante/typical.py`
- Raw card text to numbers/enums: `src/viajante/parsers.py`
- Offline IATA lookup: `src/viajante/airports.py`
- Origin-country cash currency for Google `curr` (no FX): `src/viajante/quote.py`
- Cheapest-per-day calendar and flex window (calendar then one shop): `src/viajante/dates.py`
- Explore destinations from an origin: `src/viajante/explore.py`
- Owned trip total (flight fare + hotel stay): `src/viajante/trip.py`
- Opt-in Skiplagged MCP (not Google mix-in): `src/viajante/skiplagged.py`
- Skiplagged hotel search and room rates: `src/viajante/skiplagged_hotels.py`
- Local award CPP, transfer table, imported offers: `src/viajante/points.py`
- Offline evidence-bound itinerary validation: `src/viajante/validate.py`
- Offline stay blocks and per-person cost split: `src/viajante/stays.py`
- MCP evidence ledger and `verify_answer`: `src/viajante/evidence.py`
- Repo junk cleaner: `scripts/clean-repo.py`

`google_flights.py` owns URL building, consent, card parsing, typed provider failures, the sweep HTTP client, and `GoogleFlightsSource`. `google_flights_rpc.py` owns the compact shopping request and `wrb.fr` parse. `booking.py` owns Booking.com URL/chips, consent, card extract, and `BookingHotelsSource`. Session lifecycle lives in `browser.py`. `flights.py` and `hotels.py` are the search loops: pure and offline-testable outside the browser source. `trip.py` joins owned flight fare and hotel stay when dates overlap; it omits the sum if either side missed.

## Public contract

CLI: `viajante flights`, `dates`, `flex`, `explore`, `airports`, `hotels`, `hotel-rooms`, `trip`, `hidden-city`, `awards`, `points`, `bench`.
MCP (stdio): `search_flights`, `search_dates`, `search_flex`, `search_trip`, `search_explore`, `lookup_airports`, `search_hotels`, `search_hotel_rooms`, `search_hidden_city`, `compare_awards`, `lookup_transfers`, `validate_itinerary`, `plan_stay_blocks`, `split_stay_costs`, `verify_answer`, `get_runtime_info`, `get_guide`.
Library: `get_flights` (route spec or trips; natural language is the caller's job), plus the `search_*` functions and `validate_itinerary`. Sweep `--proxy` / MCP `proxy` on flights, dates, flex, explore. `search_hidden_city` is Skiplagged-only and does not mix Google evidence. Skiplagged cards are USD; named keep is USD/omit. A keep that matches no owned card is `currency_mismatch` (owned quote stamped), not silent `no_results`. Viajante does not convert. `compare_award`, `lookup_transfers`, `validate_itinerary`, `plan_stay_blocks`, and `split_stay_costs` are local; validation returns pass/fail/unknown and does not invent seats or fill missing evidence.
Flags and defaults: `src/viajante/cli.py` (`viajante <cmd> --help`). MCP signatures: `src/viajante/mcp_server.py`. JSON keys: `src/viajante/models.py`.

English fetch. Prompts any language. Product voice is English. A Spanish
(or other) prompt is caller *input*, not product voice. No implied home hub.
Currency is `--currency` / MCP `currency`, or inferred from a named origin
airport's owned country (JFK USD, LHR GBP, NRT JPY, GRU BRL). Unproven
asks (error: currency required). Google and Booking hotels have no origin:
currency is required. Opt-in Skiplagged hotels default to their owned USD quote.
If country, destination, or currency is not proven (a city with several
airports, “Europe”, unnamed origin, two possible currencies), ask or error.
Unknown cannot prove include. Do not invent IATA, `gl`, or ISO 4217 from vibe.
Viajante does not convert; the MCP caller does FX.
Optional `gl` / MCP `country` is Google origin-market geolocation; omit when
unset; do not default `gl` to a home hub; do not pass a destination ISO.
Never invent a fare, typical, token, via list, bag count, dest, price cap,
or exchange rate.

> **CLI sketches in `.cursor/skills/viajante/SKILL.md` are not argparse.** If a
> skill line omits a flag `cli.py` defines, the code wins. Do not silently
> “complete” the skill into a second CLI contract.

Successful flights, dates, flex, and explore may carry owned `google_flights_url`
(`booking_token` wins when present on a shop offer; omit if encode cannot run)
and `stops_compare`. Flights offers, dates priced rows, and shopped explore dests
may carry `typical` / `vs_typical` / `vs_typical_pct` / `typical_deal`. A flex
report stamps `typical` / `vs_typical` only at report level. Flex offers and day
rows use the full triple. Stamp rules live with the search loops (`flights.py`,
`dates.py`, `explore.py`, `typical.py`). Compact calendar cells and Explore
catalog places are not offers: they may carry a query URL; they do not grow a
token, buffer stamp, overnight/via filter, or `stops_compare`. Never invent a
dest typical from the explore catalog mix or from other dests.

Schema v2 flight offers carry immutable `evidence` and explicit `completeness`.
The URL evidence reproduces a query, not guaranteed current fare availability.
Empty segments cannot prove segment count, operators, clocks, airport changes,
or overnight constraints. Run `validate_itinerary` before calling an assembled
route compliant. A fail is infeasible; unknown never becomes pass. Relaxed
constraints are a separate scenario. Search `coverage` is bounded to its named
scope and is not proof over unsearched routes, dates, gateways, or permutations.

`--nearby` is opt-in same-city IATA (default off; open-jaw not rewritten; no
invented codes). `--exclude-airports` / `--include-airports` are named owned
IATA lists (same parse as via). Include is dests only, not origins. Exclude wins
on overlap. `--exclude-regions` is explore-only (owned IANA tz prefixes). Named
origin/dest in an exclude list is empty unless `--nearby` already owned a
non-excluded same-city code. Unnamed stays unset. Do not rewrite to a substitute
dest. `--via` / `--exclude-via` / `--no-overnight` / `--require-overnight` filter
on owned layover city+clock; unknown cannot prove include or exclude.
`--arrive-before` / `--depart-after` are named HH:MM on owned clocks. Named
`--price-cap` is a local post-filter of owned amounts in the quote currency
(shopping index 7 stays `None`). Flex is calendar then one shop. A calendar
`CompactParseMiss` stamps `error` `markup_drift` with empty `days` and no shop;
an empty priced window has day rows and no error.
Trip total is omitted if either side misses, dates do not overlap, or
currencies differ. `search_trip` / `viajante trip` reject child or infant
occupancy (hotel occupancy is adults-only). Nearby alternatives take the
cheapest owned fare in that city group, not a sum.

`--baggage-buffer` / MCP `baggage_buffer` is a ranking add-on in the same quote
currency. **Unnamed is 0.** Named `N` is used as-is (not FX-converted). Compare
bags with `--bags N` / `--carry-on` on the shopping request so Google prices
them. Do not invent a bag fee. Compact date-grid cells and Explore catalog
places omit the stamp. Dates sweep-fallback / shopped day rows pick the day's
winner by fare+buffer. Explore dest ranking applies a named buffer only when
`--sort ranked`.

## Invariants

- Validate CLI input before starting Chromium. Reject departure dates in the past.
  Compute ISO dates from today; roll a named season to the next legal year.
  Do not send a past `start`.
- Two flight fetch modes, one public contract. Sweep: one Chrome TLS session
  (`curl_cffi`), HTTP/2 multiplex, owned shopping RPC, HTML fallback if compact
  parse misses. Detail: Playwright (`viajante[browser]`). `--fetch {auto,sweep,detail}`:
  auto uses sweep for 3+ flight queries or any packaged RT/multi (only sweep
  shops the next leg), and detail for other 1–2 when Playwright is importable. Auto without Playwright stays on sweep. Sweep empty or `blocked` may
  fall back to detail once (`fetch_backend: sweep_then_detail`) only when Playwright
  is installed; do not re-run successful sweep legs. Shopping `ErrorResponse` and
  owned markup drift fail without Chromium. Do not silently mix backends unless
  that fallback fired.
- Sweep inter-query delay is 0. No Playwright/Chromium required for sweep. One
  lazy Chromium per process, only when detail runs. Install Chromium with
  `pip install 'viajante[browser]' && playwright install chromium`. Flights block
  images, media, and fonts. Booking blocks images and media (fonts stay). Use
  Playwright's Chromium UA for detail; do not spoof a stale Chrome/macOS UA.
- Detail delays: 4.5s + up to 1.5s jitter between queries; 3 attempts with 8s
  exponential backoff + jitter; browser reset after each failed attempt. No flags
  to shorten detail delays or parallelize requests. Progress goes to stderr.
- Retry only what can succeed on a second try. Sweep HTTP retries empty/drift/5xx
  once after 50 ms; happy path does not sleep. After that, `markup_drift` still
  fails without Chromium. HTTP 429 resets TLS, waits 50 ms, continues remaining
  jobs. A real direct (unproxied) Google 429, or a data-less RPC status 13, also
  writes `google-rate-limit.json` in the state dir: a guessed cooldown (2 min, doubling per repeat limit up to 30 min, or
  a named `Retry-After`). While it runs, new flight/hotel Google searches in any
  process send nothing and fail `blocked` with `rate_limited: true` (plus
  `retry_after` ISO UTC and `retry_after_seconds` only when a recorded cooldown
  names one; a proxied 429 records none); a search
  already running keeps its replay. Rate-limited failures do not fall back to
  detail. MCP search tools replay an identical successful call for 5 min
  (`cached: true`) instead of asking Google again. `no_results`, `rejected`, `blocked`,
  `markup_drift`, and `browser_unavailable` do not get a Playwright second attempt.
  A Skiplagged 429 does the same in `skiplagged-rate-limit.json` (`blocked`,
  `rate_limited`, no retry, live calls paced 1s apart). A calendar
  `blocked` (including a short unknown HTML shell, or a data-less wrb.fr
  error envelope such as status 13) stops that calendar; no
  flex, shop, or browser recovery. Booking card-wait timeouts fail immediately.
  Do not hammer Booking after a challenge.
  `rejected` and `markup_drift` do not fall back to detail.
- Every offer keeps raw text beside parsed fields. Sweep and detail clocks are
  24-hour `HH:MM`. Omit typical / cheapest keys when the compact calendar misses,
  has fewer than three priced days, or the query is multi-city. Never invent a
  market average.
- JSON output only with `--save`. Browser state lives outside the checkout
  (`VIAJANTE_STATE_DIR` or XDG state dir), always write-temp-then-rename. Booking
  fetch failures dump `booking-last-failure.html` / `.txt` there; do not commit
  those files.

### Flights

- Keep the owned quote currency, `hl=en` / `locale="en-US"` for flights, ranked/fare sort, and the
  baggage buffer in both modes. JSON `locale` stays `"en"`. Currency is `curr`,
  independent of `hl`. Planner prompts may be any language; the plan still emits
  English IATA and English fetch locale.
- Occupancy and cabin are query fields. Shopping constraints index 6 is
  `[adults, children, infants_in_seat, infants_on_lap]`. `--bags N` / `--carry-on`
  fill index 10; leave both unset so that slot stays `None`. A non-zero buffer
  implies `needs_bag_verify` while bag counts are unknown. Never invent a bag fee
  or bag count. Callers must verify baggage on Google Flights before booking.
- `max_stops` is 0, 1, or 2. The product cannot require 3+ stops. If the user
  needs 3+, say so and search with 2, or refuse; do not invent a fare.
  Default trip kind is one-way.
  `ORIGIN-DEST:OUT:BACK` without `--trip` is two one-ways. `--trip rt` / `multi`
  POST one package. `--sort ranked` (default on flights) selects `--top` by
  fare+buffer (`DEFAULT_TOP` in `flights.py`). Explore unnamed sort stays
  `price`. Dates unnamed sort stays date order. Sort is order, not a `--top` cut
  on the date grid.
- `--airlines` / `--exclude-airlines` / `--alliance` / `--exclude-alliance` ride
  the shopping request. Alliances have no member list here. `--depart-window`,
  clocks, layover hours, `--via`, overnight, duration, and `--price-cap` are
  local post-filters after parse, before `--top`. “morning” / “late” / “Europe”
  / a city vibe does not invent a clock, dest, or alliance code.
- Named `max_stops` 0 plus a named positive `min_layover` (or a via that needs a
  layover) keeps both and stamps one English note that the pair cannot both be
  satisfied. Do not drop one. Do not invent a one-stop. Overnight IST
  `keep_connect` is still `require_overnight ∪ via`. Contradiction keeps both
  overnight constraints.
- The low-cost carrier list is partial. Absence from it is not evidence that a
  fare includes a bag.

### Hotels

- A multi-stay hotel search carries `property_matrix`: each property's total per
  stay, null where it was not among that stay's returned offers (not proof of
  unavailability), sorted by name, never ranked. `near` is a point the caller names;
  offers with owned coordinates then carry a straight-line `distance_km`. No point is assumed.
  Optional `max_distance_km` / `--max-distance-km` requires that point, is finite
  and positive, and excludes outside/unknown coordinates before ranking and `top`.
  An empty shortlist is not proof of no availability in the radius.
  MCP `stays` accepts up to 8 location/check_in/check_out objects with optional
  adults and rooms, inheriting top-level occupancy when omitted. Google query
  results carry owned `resolved_place` / `place_bounds`; offers may carry
  `sleeps`, `place_types`, `class_label`, and `priced_adults`. A priced party
  that differs from the ask does not prove a total for the requested party.
  Google hostel totals may price dormitory beds: no room type is supplied.
  Check finalists by exact name with `search_hotel_rooms` before calling them private.
- Hotel prices are total-stay prices. Keep requested filters, applied chips, and
  observed card evidence distinct. `--source booking` (CLI default) is Playwright
  evidence; `--source google` is the HTTP shortlist. MCP hotel search defaults to
  Google. Booking ratings are 0–10; Google Hotels ratings are 0–5. Drop
  non-property titles such as `closed`. Google Hotels HTTP uses the hotel search
  loop's 3 attempts with 8s backoff (same pace as Booking Playwright), not the
  flight-sweep 50 ms once-retry.
- `--source skiplagged` is opt-in and never mixed with Google or Booking rows.
  Its quotes are USD: an unnamed currency is USD, another named currency is
  `currency_mismatch`, and nothing converts. At most 10 adults per search (the
  caller splits a larger party), at most 9 rooms, and no `--entire-home`. Skiplagged matches the
  city loosely, so `resolved_place` is the owned echo; a place that is not the
  one asked for was not searched. Search cards carry no cancellation, so `applied.not_applied` stamps
  `free_cancellation` and the CLI says so. For 1-3
  finalists `search_hotel_rooms` / `hotel-rooms` (by id, or by exact normalized
  name plus city; no match or several is `no_results`, never a guess) returns provider room rates
  with `occupancy_limit`, `refundable`, `free_cancellation`, and `taxes_and_fees`
  (at most 5 rooms per room-rate request); do not rank them or
  infer that a party fits across rooms from `occupancy_limit`.
- Free cancellation is required by default. Only an explicit caller or CLI
  opt-out may include non-refundable stays. If `oos=1` is applied and the card
  does not mention cancellation, print `filter applied; card silent` — do not
  store `free` in JSON.
- `--compare-cancellation` runs two sequential Booking scrapes. Do not
  parallelize. If one query fails, print both results and skip the join.
- `lodging_kind` is observed card evidence. Apartment in the title may infer
  `entire_home` when the card is silent. Do not infer `hotel` from the word hotel
  in the title. Do not claim cancellation, lodging kind, or unit counts when
  unknown.
  `lodging_evidence_conflict` flags explicit room labels that contradict an
  entire-unit chip. Both lodging kind and property type then remain unknown;
  preserve the title and raw evidence instead of choosing one label.
- Callers must verify the final total and cancellation terms on Booking.com
  before booking.
- Other OTAs are not scrapers in this tree. After Booking, for 1–3 finalists,
  follow `.cursor/skills/viajante/SKILL.md` (user's browser harness; unverified
  second opinion). Do not invent prices from snippets or write them into `--save`
  JSON.
- `viajante trip` / MCP `search_trip` run flights then hotels sequentially (one
  lock). Child or infant occupancy is rejected (hotel occupancy is adults-only).
  Hotel `price_basis` stays `total_stay`. Never invent a fare or a stay.

## Local stay arithmetic and known limits

Known release limits: a data-less status 13 can have a cause other than throttling
and pause Google searches for 2 minutes. Google base/tax/fee breakdown
(`record[6][2][44]`, unconfirmed) and caller-named exchange rates remain outside
this release pending evidence and a design decision.

Local `plan_stay_blocks` groups consecutive nights with identical people, not
just identical headcounts. `split_stay_costs` uses named currency and optional
per-person nightly fee, allocates exact cents per stay only among its occupants,
and reports uncovered roster nights as `unallocated_nights`. Neither fetches
prices or converts currency.

## MCP client surface

Every tool has a title and read-only annotations; `openWorldHint` is true only for
tools on the search runner (`search_*`). Invalid input stays an `isError` result
whose text is the SDK's exact `Error executing tool <name>: ` prefix followed by
`{"error": {"code": "invalid_parameter", "field": <param or null>, "message"}}`;
clients strip that prefix and parse the rest only if it starts with `{`.
`search_in_progress` is the busy code. Missing or mistyped arguments (the SDK's
pydantic check) and undeclared top-level arguments (a misspelled filter) get the same
body; `field` is set only when every failure is on one top-level parameter. Other
`field` values are set only when the message names exactly one parameter or quotes
exactly one parameter's value; never guess it. Decode errors are not blamed on the caller.
Long guidance lives in `viajante://guide` / `get_guide`; the server instructions
keep only currency, bags, evidence, hidden-city sequencing, empty-is-not-absent
and rate limits. Do not drop a rule from both. Floor `mcp>=1.14.1`; loopback HTTP
passes `transport_security` explicitly because the SDK only does it itself from 1.23.

## Tests

Prefer the locked checkout workflow:

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
uv run viajante bench
```

`viajante bench` is the offline gate: unittest + `ruff check` / `ruff format --check`,
then `gate` and `score_ms` (`tests_ms` + owned `tests/bench/` parse). No Chromium.
No live Google unless `VIAJANTE_BENCH_LIVE=1`; that extra `sweep_ms` is never the
score. **Do not optimize `score_ms`.**

`pip install -e .` still works; `uv` is the reproducible path. Tests are offline.
They must not launch Chromium or use the network. CI runs the suite on Python
3.10 through 3.14.

Pin owned seams, not upstream HTML rewriting. A renamed or dropped JSON key is a
breaking change. `tests/test_mcp.py` imports FastMCP when the `mcp` extra is
installed (`mcp>=1.14.1,<2`).

Stdio MCP: `npx -y @viajante/mcp`, or
`uvx --from 'viajante[mcp]' viajante-mcp`, or checkout `uv sync --extra mcp`
then `viajante-mcp`. Local Streamable HTTP is opt-in
(`viajante-mcp --transport streamable-http [--host 127.0.0.1] [--port N]`): no
auth, loopback by default, never hosted, no `remotes` in `server.json`; a
non-loopback host warns. Keep
the one-search process lock: a second search raises
`a viajante search is already running in this process` immediately.
`MCP error -32001: Request timed out` is not that lock; do not retry timeouts
as lock-busy. `lookup_airports` may run during a search. Playwright is extra
`viajante[browser]`.

## Trip-planning search strategy

A named route and date is flights only; do not add hotels or `search_trip`
unless the user asked for a stay. After that `search_flights`, if the route is
a common hub or leisure trunk or the Google payload suggests a through-fare,
call `search_hidden_city` once with the same named route and date. Sequential
(process lock). Do not mix Skiplagged and Google payloads. Skip when `bags`
were named, and skip explore, dates, flex, and multi-city. Do not invent a
beyond city. Omit hidden-city `currency` (Skiplagged cards are USD); do not
copy a Google/origin quote keep (GBP, JPY, …). Lead with `hidden_city: true` rows; confirm on
`booking_url` (do not scrape Skiplagged).

When helping pick destinations (not a single named route/date), follow
`.cursor/skills/viajante/SKILL.md` → **Destination triage**: shortlist by vibe
and a rough price band for the origin the user named (if origin is unnamed, ask;
do not invent a band from another origin), scrape fixed natural dates first, and
only then expand ±1 day on 1–3 finalists. If country, destination, or currency
is not proven (a city with several airports, “Europe”, two possible currencies),
ask or error. Unknown cannot prove include. Around/±N on a named route is
`viajante flex`; cheapest week is `viajante dates`. Do not brute-force full date
matrices across a long destination list in one run.

## Private-data boundary

This tree is the public export. Do not add scraped caches, CSVs, personal trip
scripts or routes, reservation data, browser session files, or paths from a
private repository. Origin-agnostic heuristic bands in the viajante skill are
allowed; an origin-specific fare table is not. Live scrapes and personal trip
JSON are not. Never invent a fare, typical, token, or via list.

## Hotel planning and installation evidence

Check `viajante --version` (1.4.0+) or MCP `get_runtime_info` before using a
new feature. Hotel JSON carries `viajante_version`; schema 2 is additive.
An npm MCP is pinned to its own Python version; it does not upgrade a separately
installed uv tool. `uvx --from viajante` may reuse that installed tool. Refresh
an explicit version or upgrade the installed tool; verify the executing version.

Google navigation uses owned `record[20]` entity IDs, never `/travel/clk/hi`
trackers. `link_context` and `applied.url_context` are stay/property/location/none.
Stay means the searched dates, adults and rooms are encoded; it does not prove
current fare, capacity, private room, refundable terms or availability.
`verify_answer` checks provenance only, not URL reachability or travel feasibility.

Google general descriptions remain in `details`, but cannot prove the priced
unit's room type, capacity or cancellation. Only explicit unit chips feed those
fields. Shared rooms, dorm/private mixes and negated private rooms are not private.
Known `priced_adults` mismatches and insufficient single-unit capacity are excluded;
unknown occupancy is an unverified shortlist candidate, not a verified party total.

Use the latest confirmed nightly roster and `plan_stay_blocks`: same people,
not just same headcount. Do not extend someone's final night to another stay's
check-out. Retain `unallocated_nights` from `split_stay_costs`. Keep quotes and
arithmetic estimates distinct; modifying dates/occupancy requires a new quote.
Get replacement availability and terms before recommending cancellations.
Removing dorm beds may remove exclusive use of the room. Respect room capacity,
rejected requests and contradictory bathroom/bed information. Use exact local
cancellation deadlines from the reservation; do not invent missing deadlines.
A latest check-out time does not prove a flight transfer fits: verify route time
and airport arrival margin. Preserve warnings in the final comparison.

A browser permission denial, an HTTP empty click tracker, a provider challenge
and a missing screenshot are different failures. Report the actual one. Use
permitted read-only alternatives without bypassing a denial, and do not ask again
for access already authorized. Never claim a screenshot was inspected if unreadable.
Run hotel searches sequentially. In zsh, shared flags must be an array expanded as
`"${flags[@]}"`, not a space-separated scalar. Use CLI help before launching a batch.
