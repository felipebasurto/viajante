# How viajante is built

A map of the moving parts, written as the source for a "how it works" post.
Flags live in `viajante <cmd> --help`, JSON keys in `src/viajante/models.py`,
and the agent contract in `AGENTS.md`. This file explains *why* the pieces are
shaped the way they are.

## The stance: superpowers, not answers

Viajante is a tool for an agent, not a travel agent. It does the part an LLM
cannot do on its own: speak Google Flights' and Google Hotels' private wire
formats, fast, from one machine, and hand back evidence that is exactly what
the provider said. The agent does the part viajante should not: decide what
the user meant, which trade-off matters, and what to recommend.

That split explains most design choices:

- **Evidence, not a verdict.** Every offer keeps the raw provider text next to
  the parsed number. Results are ordered, not ranked into advice. There is no
  "best deal" line to parrot. The optional `recommendation` block is a
  transparent shortlist, not a verdict: its score dimensions and weights are
  published, and every highlight or trade-off restates a returned field (or
  says the field is unknown). The agent reads price, duration, stops, layover
  cities, clocks, rating, review count, and coordinates, and says *why*.
- **Primitives that compose.** `search_dates` (cheapest week), `search_flex`
  (±N days), `search_explore` (where can I go), `search_flights` (shop one
  route), `search_hotels`, `search_trip`. The agent chains them. Viajante does
  not try to plan a whole trip in one call.
- **Refuse to guess.** Unknown currency, a city with several airports, "Europe",
  a missing origin: viajante errors and lets the agent ask. It never invents a
  fare, an IATA code, an FX rate, a bag fee, or a "typical" price.
- **Checks the agent can run on itself.** `validate_itinerary` tests chosen
  offers against constraints (pass / fail / unknown, where unknown never
  becomes pass). `verify_answer` flags amounts, codes, dates, and links in a
  draft reply that no search in this process returned. `recheck_offer` asks
  Google once more whether a finalist still exists at its price; a blocked
  check is `check_failed`, never that the offer is gone.

When something tempts you to pre-digest the payload for the agent (summaries,
"cheapest is X" lines, recommendation prose), don't. A capable agent already
does that better, and a canned summary pushes it to repeat instead of reason.

## Surfaces

One library, three ways in:

| Surface | Entry | Notes |
| --- | --- | --- |
| MCP (stdio; opt-in local Streamable HTTP) | `viajante-mcp` → `mcp_server.py` → `mcp_handlers.py` | The main surface. 22 tools. |
| CLI | `viajante <cmd>` → `cli.py` | Same searches, human tables, `--save` JSON. |
| Library | `viajante.search_*`, `get_flights` | What both of the above call. |

| MCP tool | CLI | What it is |
| --- | --- | --- |
| `search_flights` | `flights` | Shop one or more routes (one-way, packaged RT, multi-city). |
| `get_hotel_details` | — | Stored hotel quote, plus an optional separate Skiplagged room quote. |
| `search_dates` | `dates` | Cheapest fare per day across a window (≤31 days). |
| `search_flex` | `flex` | Cheapest day in ±N around a date, then one shop. |
| `search_explore` | `explore` | Destinations from an origin, shortlist priced. |
| `get_runtime_info` | `--version` | Executing package/Python and hotel schema versions, offline. |
| `search_hotels` | `hotels` | Total-stay hotel prices (Google HTTP, Booking browser, or opt-in Skiplagged). |
| `search_hotel_rooms` | `hotel-rooms` | Skiplagged room rates for one named finalist, in USD. |
| `search_trip` | `trip` | Flights then one hotel, plus a sum when both succeed. |
| `search_split_tickets` | `flights --split-tickets` | Opt-in split tickets from real one-way quotes: a hub self-transfer or mixed one-ways (`split.py`). |
| `search_hidden_city` | `hidden-city` | Skiplagged, opt-in, never mixed with Google evidence. |
| `recheck_offer` | `recheck-offer` | One fresh Google Flights search matching an earlier offer by flight numbers and departure times: same price, price changed, not found, multiple matches, incomplete identity, or check failed (substituted only on request). |
| `lookup_airports` | `airports` | Offline IATA lookup. |
| `compare_awards` | `awards` | Award offer vs cash: cents per point, transfer paths. |
| `lookup_transfers` | `points` | Local card-to-program transfer table. |
| `validate_itinerary` | — | Local constraint check of selected offers. |
| `plan_stay_blocks` | — | Local roster-to-stay blocks for consecutive nights with the same people. |
| `split_stay_costs` | — | Local cost split per stay and person-night, with exact allocated cents. |
| `verify_answer` | — | Local check of a draft reply against the search ledger. |
| `price_history` | `history` | Local read of the opt-in observation log: per-query, per-currency facts. |
| `watch_price` | `watch` | Re-run one saved flight/hotel search on demand; change vs the last observation. |
| `get_guide` | — | The long operational guide (also the `viajante://guide` resource). |

The room-rate helper is in `viajante.skiplagged_hotels`; local stay arithmetic
is in `viajante.stays`. These helpers are not re-exported from `viajante`.

The MCP process holds one search lock: a second concurrent search fails
immediately instead of queueing. Lookups, local stay arithmetic, `price_history`, and the two
verifiers may run during a search. Identical successful searches within 5 minutes are replayed from an
in-process cache (`cached: true`) instead of asking Google again.

MCP client compatibility lives at the adapter edge. `mcp_server.py` registers each
tool with a title and read-only annotations (`openWorldHint` only for tools that go
through the search runner). `mcp_errors.py` turns a handler `ValueError` into the
JSON error body (`invalid_parameter` with an inferred `field`, or
`search_in_progress`) without touching the handlers. `mcp_guide.py` holds the short
server instructions and the guide. `SearchError.retry_until` carries the end of a
recorded cooldown from the classifier that saw the 429 to `to_dict`, which emits
`retry_after` / `retry_after_seconds`.

`control.py` holds the cooperative stop: a thread-local `SearchControl` carries a
`threading.Event` (cancel), an optional deadline, and the progress callback.
Search loops call `checkpoint()` between queries and attempts, sleeps use
`interruptible_sleep`, and sweep futures are polled, so a cancel or deadline
takes effect within about 50 ms of the next boundary. Cancel raises
`SearchCancelled` (a `BaseException`, so no retry or `except Exception` swallows
it); a deadline raises `SearchDeadline`, classified as error code `deadline`,
which is terminal and never retried or cached. `mcp_server.run_mcp_tool` wires
the MCP request to that control and forwards progress as
`notifications/progress` when the request has a `progressToken`.

## How a flight search travels

```
query (Trip)                       models.py
  │ encode                         tfs.py (URL), google_flights_rpc.py (RPC body)
  ▼
Chrome-TLS HTTP/2 session          google_flights.py (ChromeSweepClient)
  │ POST GetShoppingResults        many queries multiplexed on one connection
  ▼
wrb.fr envelope → itineraries      google_flights_rpc.py
  │ RawFlightCard (raw text kept)
  ▼
normalize, filter, rank            flights.py
  │ + same-route calendar median   typical.py (GetCalendarGrid/Graph)
  │ + next-leg shop for packaged RT
  ▼
SearchReport → JSON                models.py
```

### 1. The query

Everything starts as a frozen dataclass: `FlightQuery`, `RoundTrip`, or
`MultiCity` (`models.py`). Occupancy, cabin, stops, bags, and airline or
alliance filters are fields on the query, because Google prices them. Clock
windows, layover limits, via cities, overnight rules, and price caps are
*local* post-filters applied after parsing, because Google has no request
slot for them.

### 2. Two encodings of the same trip

- `tfs.py` builds the `tfs=` protobuf that Google Flights URLs carry. That is
  the link the agent can hand the user (`google_flights_url`).
- `google_flights_rpc.py` builds the JSON body for the frontend's own RPCs:
  `GetShoppingResults`, `GetCalendarGrid` (one-way), `GetCalendarGraph`
  (round-trip), and `GetExploreDestinations`. The body is a positional
  nested list (`f.req=[null, "<inner json>"]`); the slots are documented
  where they are built.

### 3. The sweep client

`ChromeSweepClient` is a process-wide `curl_cffi` `AsyncSession` impersonating
Chrome's TLS fingerprint over HTTP/2, with up to 8 streams on one connection.
A search with ten routes sends ten POSTs concurrently and waits roughly one
round-trip, not ten. There is no inter-query delay on this path. The client
completes Google's EU consent interstitial once per session when it appears.

The response is Google's anti-XSSI-prefixed, length-chunked stream. The
`first_wrb_data` scanner finds the first `wrb.fr` envelope and its JSON
payload; the parsers walk that into `RawFlightCard`s.

### 4. Detail mode (the browser)

`--fetch detail` loads the real results page in Playwright Chromium and parses
the DOM. It is slower (4.5 s + jitter between queries, 3 attempts with
exponential backoff) and optional (`viajante[browser]`). `--fetch auto` uses
sweep for 3+ queries or any packaged round-trip/multi-city (only sweep can
shop the return leg) and detail for other 1–2 query searches when Playwright
is installed. If sweep comes back empty or blocked, auto may fall back to
detail once and says so (`fetch_backend: sweep_then_detail`). A rate-limited
failure never falls back to detail.

### 5. From cards to offers

`flights.py` turns raw cards into `FlightOffer`s: parse price, clocks,
duration, layovers, flight numbers, and bags; drop what the local filters
reject; rank by fare (+ an optional named baggage buffer) or by duration or
clock; keep the top N. Then it stamps:

- `typical` / `vs_typical`: the median of the same route's cheapest-per-day
  calendar (`typical.py`). Fewer than three priced days, or a multi-city trip,
  means no stamp. It is never a market average from somewhere else.
- `stops_compare`: the cheapest nonstop vs cheapest one-stop, from the same
  payload.
- The return leg of a packaged round-trip, from a follow-up shop that sends the
  selected outbound slices.
- `evidence` / `completeness`: when and how each offer was fetched, and what
  is still unknown.
- `recommendation` (`recommend.py`): see "Recommendation and shortlist" in
  [usage](usage.md). It is built from the same parsed cards before the
  requirement filters, so a relaxation can be reported; it never changes
  `offers`.

### 6. Failures are typed

Every provider failure becomes a `SearchError` with a code: `no_results`,
`rejected`, `blocked`, `markup_drift`, `fetch_failed`, `browser_unavailable`.
Only failures that can succeed on a second try are retried:

- Sweep retries empty, drift, and 5xx once after 50 ms.
- A direct HTTP 429 resets the TLS session and writes a shared cooldown file in the
  state dir (2 min, doubling to 30 min). While it runs, every Google search in
  any viajante process on the machine fails instantly with `Not sent.` and
  `rate_limited: true`. A named `Retry-After` takes precedence over the guessed
  delay; a proxied response does not pause the machine's direct searches.
- A data-less RPC error envelope (`["wrb.fr", null, …, [13]]`) is `blocked`,
  not `markup_drift`, and records that same cooldown for direct sessions.
  Google Hotels also stamps `rate_limited: true`. This status is treated as
  throttling evidence, though it can have another cause. The browser path detects
  `google.com/sorry` right away instead of waiting for result cards.

### 7. One MCP envelope

Every MCP tool except `lookup_airports` returns the same top-level fields,
stamped in `envelope.py` from the report the handler already built (no second
code path): `status`, `completeness`, `empty_reason`, `empty_note`, `error_code`,
`retry_after` / `retry_after_seconds`, `observed_at`, `observed_at_basis`.

The envelope tallies the report's own units (query rows, date rows, explore
destinations, rooms, hidden-city offers) using their `empty_reason`, error code,
`rate_limited` and `timeout` flags, so it agrees with the existing counters and
does not re-derive them. `retry_after` is the latest per-error `retry_after` of the rate-limited errors, so a proxied 429 never borrows a direct cooldown. Offline
tools stamp `ok` / `complete` with the rest null.

Contract: `provider_empty` (the provider answered with nothing) is the only empty
that may be called "no flights/hotels found". `filtered_out` (the provider returned
rows, viajante's filters removed them all) must say filters removed results.
`not_loaded` (no usable response) must say the search did not complete.
`completeness` is `partial` when only some units answered, some came back
unproven, or the search is scope-bound, `blocked` when none did. A calendar day
that is missing or unpriced is `not_loaded`: the calendar cannot prove there are
no flights, so only a shop that answered empty is `provider_empty`. `error_code`
only accompanies the `empty_reason` it supports; an `ok` / `partial` result may
carry the worst failure's code with `empty_reason` null. An explore destination
without a price is `not_loaded`, never a usable row. `observed_at` is null when a
recorded cooldown answered every request (nothing was sent). `stamp_search`
raises on a payload shape it does not recognise instead of defaulting to
`no_results`. `verify_answer` is local: its `status` follows its verdict.

Machine-readable schema: the mcp SDK (the supported floor is 1.14.1; earlier
releases crash at startup on this module's postponed annotations) derives `outputSchema` and
`structuredContent` from a tool's return annotation. The envelope is a pydantic
model with `extra="allow"`, so tool-specific keys stay in the structured result.
`lookup_airports` keeps its bare list return and has no output schema (a list
cannot carry top-level fields without breaking its shape).

## Dates, flex, explore

- **Dates** asks the calendar RPC for cheapest-per-day prices across a window.
  If the calendar misses, it prices each day with multiplexed shopping calls.
- **Flex** reads the calendar around one date, picks the cheapest day, and
  shops only that day. A calendar parse miss stops there with an error; it does
  not guess a day.
- **Explore** asks `GetExploreDestinations` for the catalog from one origin,
  then prices the first N destinations with shopping + calendar on one
  multiplexed round-trip. Catalog prices are hints, not offers, so they carry
  no typical, token, or filters.

## Hotels

Three sources, one loop (`hotels.py`):

- **Google Hotels** (MCP default): the `AtySUc` RPC on `batchexecute`, over the
  same Chrome-TLS session. Encode and parse live in `google_hotels_rpc.py`.
  Each stay carries the total-stay price text, rating (0–5), review count,
  latitude/longitude, and a Google link. Owned `place_types`, `class_label`, and
  `priced_adults` distinguish property type and the party actually priced;
  vacation-rental chips feed `details` and parsed sleeps, bedrooms, and beds.
  General descriptions are retained but cannot prove the quoted unit's type,
  capacity or cancellation. Known priced-party mismatches and insufficient
  single-unit capacity are excluded; unknown remains an unverified shortlist.
  Navigation uses the owned entity ID at record[20], never an internal click
  tracker. `google_hotels_url.py` encodes ts stay dates, adults, rooms and currency
  for entity and search URLs. `link_context` / `applied.url_context` distinguish
  stay, property, location and none; a context is not live availability proof.
  Query results carry `resolved_place` and `place_bounds` from the provider.
  Free cancellation and vacation-rental
  type go into the request. With a named `min_rating`, the price-sorted page
  and a relevance-sorted page ride the same multiplexed round-trip, because the
  cheapest page is mostly low-rated.
- **Booking.com** (CLI default): Playwright, with chips in the URL and the
  card DOM parsed for cancellation, lodging kind, and unit counts. Slow on
  purpose; challenges are not hammered.
- **Skiplagged** (opt-in, `--source skiplagged`): its public MCP over HTTP, no
  key, no browser (`skiplagged_hotels.py`, transport shared with hidden-city).
  Quotes are USD only (omit currency or name USD); another named currency is
  `currency_mismatch` and nothing is converted. Search returns a total and a
  0–10 score but no cancellation or
  coordinates. It matches the city text loosely ("Costa Brava" matched Costa
  Mesa, California), so `resolved_place` carries the city slug Skiplagged
  actually searched. `hotel-rooms` / `search_hotel_rooms` fetches room-level
  rates for one finalist, by id or by exact name plus city (so a Google finalist
  can be checked): occupancy limit, refundable, free cancellation, taxes.
  Offers carry `provider_id`. Search supports at most 10 adults and 9 rooms,
  and rejects `entire_home`; room-rate requests support up to 5 rooms. An exact
  normalized name with no match or several matches returns `no_results`, never
  a guessed id. Skiplagged rows are never mixed with Google or Booking rows.
  HTTP 429 returns `blocked` with `rate_limited: true` without retries. Its
  separate cooldown file and one-second call pace live in `ratelimit.py` and
  `skiplagged.py`.

MCP `stays` batches up to 8 hotel queries with their own location, dates,
adults, and rooms (omitted occupancy inherits the top-level values). A
multi-stay report adds `property_matrix`, matching normalized property title
and address and listing each returned total in query order. Rows are sorted
by name, never by price; a null means absent from that stay's returned offers,
not unavailable. A named `near` point adds straight-line `distance_km` only
when an offer has owned coordinates; it does not assume a location or change
the price order. Optional `max_distance_km` requires that point and filters
outside or unknown coordinates using unrounded distance before ranking and top.
It does not measure walking distance or prove city-wide availability. Hotel JSON
also stamps the executing `viajante_version` without changing schema 2.

The loop keeps three things apart: what the caller *asked* for, which filter
chips were *applied*, and what each card *says*. `filter applied; card silent`
is a real state, not "free cancellation". `applied.not_applied` names unsupported
filters: Skiplagged's search cannot enforce the default free-cancellation ask.
Prices are always the total stay. Google and Booking require a named currency;
Skiplagged defaults to its owned USD quote.

## Local stay arithmetic

`stays.py` never fetches or recommends lodging. `plan_stay_blocks` takes a
caller-confirmed roster for consecutive nights and groups identical people
into check-in/check-out blocks (equal headcount alone is not enough).
`split_stay_costs` divides each chosen stay's total among only its occupants,
in proportion to their nights. It requires a named currency, accepts an
optional named per-person nightly fee, and allocates leftover cents so each
stay sums exactly. Uncovered roster nights are `unallocated_nights`; the
tool does not price them.

## Money and geography

There is no home market. Currency is either named (`--currency` / MCP
`currency`) or inferred from a named origin airport's country (`quote.py`:
JFK → USD, NRT → JPY, GRU → BRL). Viajante never converts; Google returns
amounts in the requested currency and the agent may convert for the user.
The fetch language is always English (`hl=en`); prompts can be in any
language.

## Testing

Unit tests run offline: no network, no Chromium.
They pin owned seams: request bytes, parse of recorded provider bodies,
filters, ranking, JSON shape. `viajante bench` wraps the suite, ruff, and an
owned parse corpus (`tests/bench/`) as a keep-or-revert gate for automated
improvement loops.

## Known limits and debt

- **Private formats move.** The RPC slot layouts are reverse-engineered; when
  Google ships a new frontend, parsers can drift. The error taxonomy exists so
  that drift shows up as `markup_drift` and not as a wrong fare.
- **Throttling is a guess.** Google publishes no quota. The cooldown lengths
  are marked with `ponytail:` comments and should learn from real recoveries.
  A data-less RPC status 13 now writes the same cooldown (direct sessions only);
  Skiplagged keeps its own `skiplagged-rate-limit.json` and a one-second pace.
  A status 13 unrelated to throttling can cause a 2-minute pause.
- **Room capacity and type need confirmation.** Skiplagged's `occupancy_limit`
  is the provider's number per room type, not proof that a party fits across
  several rooms. Google hostel totals may price dormitory beds; Google gives
  no room type. Check finalists by exact name with `search_hotel_rooms`, then
  confirm the room and terms on the provider.
- **Unconfirmed money fields stay out.** Google base/tax/fee breakdown
  (`record[6][2][44]`) needs provider evidence. Caller-named exchange rates
  need a design decision; neither is part of this release.
- **Detail mode cannot price return legs**; `auto` routes packaged trips to
  sweep for that reason.
- **Booking.com has no HTTP path**; it needs Playwright and is slow by design.

## Temporal evidence and hotel finalists

`RawSegment` retains the provider departure and arrival dates. The compact
parser reads arrival slot 21, already used for layover arithmetic; airport
zones come from the offline catalogue. Detail DOM cards can lack these
facts. `completeness.segment_dates` and `segment_timezones` expose those gaps.
`temporal.py` round-trips both folds of an IANA civil time through UTC. A
missing zone, nonexistent spring-forward time or ambiguous fall-back time
has no provable instant. `validate.py` compares UTC instants for chronology.
Pass selected legs in travel order. `arrival_deadline` and `chronological` read
that order. An `arrival_deadline` with an explicit offset is that UTC instant, including
when the arrival airport's civil offset is different. A naive deadline is
local civil time at the arrival airport. Stay bounds use local date differences
between journey arrival and the following departure at the same airport.
Fewer than two journeys is unknown. No stay is inferred after the final flight. Existing travel-window and clock
constraints retain their previous meanings. Ordering does not prove connection
protection, immigration eligibility or sufficient transfer margins.

`details.py` reads a stored hotel quote without a provider request.
`room_rates=true` calls the existing Skiplagged room helper. `room_rates`
must be a boolean. `evidence.py` attaches `selection_id` only to hotel offers
this process can open again. Flight, calendar, explore, and hidden-city rows
are left alone; hidden-city `evidence` is the string `confirmed`. A detail
read does not call `record`, so it does not consume a ledger slot or evict a
search. Unknown ids fail before provider contact. Hotel searches still share
the 20-group retention of the ledger. The MCP cache retains at most 20
successful calls for five minutes and replays their original hotel references.

Google and Booking provider ids are never passed as Skiplagged hotel ids.
External room lookup requires the exact normalized title and one catalogue
place: airports within 100 km, or the same metro, are one place. A city that
matches several places is inconclusive and sends nothing, unless the offer's
coordinates fall within 100 km of exactly one place. The returned quote must
then lie within 2 km of that hotel; otherwise the rates are not presented.
A single place whose provider-resolved name disagrees with the named city is
an input error. Skiplagged finalists use their own ids. Dates, adults, and
rooms are sent as requested. When the provider echoes different adults, rooms,
or dates, the result is `occupancy_mismatch` or `dates_mismatch` and the rates
are omitted. A missing echo stays unknown. When `room_rates` is true and the
provider does not echo adults, rooms, and dates, a returned quote is partial. A contradictory Skiplagged city
echo stops exact-name lookup before requesting room details. Original quotes
and ordered room rates carry their own provider, currency, timestamp, and
occupancy. A missing provider label stays unknown. USD room conditions cannot
prove terms of the original fare. Missing cancellation deadlines, contradictory
units, and unknown occupancy remain unverified. Room rates do not establish
combined capacity across rooms.
