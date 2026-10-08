# How viajante is built

A map of the moving parts, written as the source for a "how it works" post.
Flags live in `viajante <cmd> --help`, JSON keys in `src/viajante/models.py`,
and the agent contract in `AGENTS.md`. This file explains *why* the pieces are
shaped the way they are.

## The stance: superpowers, not answers

Viajante is a tool for an agent, not a travel agent. It does the part an LLM
cannot do on its own: read Google Flights' public results page and Google
Hotels' provider evidence, fast, from one machine, and hand back facts that are
exactly what the provider returned. The agent does the part viajante should not:
decide what the user meant, which trade-off matters, and what to recommend.

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
| `search_explore` | `explore` | Public Explore page catalog via the page's own browser-issued request; default occupancy/cabin only, refuses non-default before networking. |
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

Loopback HTTP passes `transport_security` explicitly, because the SDK only does that itself from 1.23 and this
package supports 1.14.1 and later.

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
  │ encode                         tfs.py (public URL)
  ▼
Chrome-TLS HTTP/2 GET              google_flights.py (ChromeSweepClient)
  │ public Google Flights page     standard 8 / conservative 2 concurrent GETs
  ▼
AF_initDataCallback ds:1           google_flights_page.py (balanced JSON, no eval)
  │ decoded shopping data          google_flights_rpc.py (shared data decoder)
  ▼
RawFlightCard (provider text kept)
  │ normalize, filter, rank        flights.py
  ▼
SearchReport → JSON                models.py
```

### 1. The query

Everything starts as a frozen dataclass: `FlightQuery`, `RoundTrip`, or
`MultiCity` (`models.py`). Occupancy, cabin, stops, bags, and airline or
alliance filters are fields on the query. The public-page path sends carry-on,
airline, and alliance-include filters and requires the page to echo each one;
it fail-closes checked bags and alliance exclusion because no page evidence
proves them. Clock windows, layover limits, via cities, overnight rules,
and price caps are
*local* post-filters applied after parsing, because Google has no request
slot for them.

### 2. The public URL and provider data

- `tfs.py` builds the `tfs=` protobuf in public Google Flights URLs. Those URLs
  reproduce the searched route and are returned as evidence; they do not promise
  that the price is still available.
- `google_flights_page.py` reads the page's `AF_initDataCallback` `ds:1` value
  with balanced string-aware scanning and `json.loads`. It does not execute
  page scripts. Missing or malformed bootstrap data is a parse failure, while
  a recognized empty result shape remains provider-empty.
- `google_flights_rpc.py` owns compact-provider decoding. The public-page path
  passes its decoded `data` array to the same `parse_shopping_data` function
  used by the RPC adapter; it does not invent a `wrb.fr` wrapper. Retained RPC shopping constraints put occupancy at index 6 as
  `[adults, children, infants_in_seat, infants_on_lap]`.

### 3. The public-page sweep

`ChromeSweepClient` is a process-wide `curl_cffi` `AsyncSession` impersonating
Chrome's TLS fingerprint over HTTP/2. The default `standard` mode sends at most
8 concurrent GETs; `VIAJANTE_SWEEP_MODE=conservative` lowers this to 2. An
unknown mode fails before provider work. Flight, dates, and flex searches use
the public results page; their production path does not send unsigned shopping
or calendar RPC requests. `VIAJANTE_SWEEP_MODE` controls concurrency, not
provider quotas or a request rate guarantee. The client completes Google's EU
consent interstitial once per session when it appears.

One-way page rows are checked against the requested route, date, and stop limit.
For packaged round trips, the first page supplies outbound candidates. Viajante
tries at most eight outbound choices, sorted by their displayed amount, and
opens a return page for each. The return URL encodes the outbound's owned
physical segments, including carrier and bare flight number. Before accepting
return rows, Viajante checks that the page echoes the selected route and clock.
The accepted price is the provider's returned package total; Viajante never
sums two one-way amounts or reports the outbound amount as a standalone fare.
The eight-outbound cap makes the result scope-bound, and a failed follow-up can
leave a partial report with a page error.

A carry-on rides tfs field 13 (`BaggageFilter`) as a party total, and every
page must echo the `Bags` chip with the same count. Airline includes and
exclusions and alliance includes ride each leg's carrier fields; every page
must echo the `Airlines` chip, and an alliance must also appear as a row in
the page's airline catalog. A pure airline include must hold on every card.
Google registers an airline exclusion without applying it, so the search loop
drops cards that show an excluded carrier or no carrier evidence. A missing
echo is `markup_drift`, never a silently unfiltered result. Checked bags, a
zero carry-on, and alliance exclusion have no provable echo and are refused
before sending; remove one only for a separately described scenario. The
public page bootstraps no multi-city results, so sweep refuses multi-city and
names `--fetch detail`. The Explore destination catalog is not in any public
page bootstrap: `explore` loads the Explore page in Chromium and captures the
catalog request the page issues, checks its origin, date, cabin and occupancy
echo, and keeps only priced rows whose token proves origin and destination.
A status 13 there records the shared cooldown like any other. It does not
substitute destinations or infer prices.

### 4. Detail mode (the browser)

`--fetch detail` loads the real results page in Playwright Chromium and parses
the DOM. It is slower (4.5 s + jitter between queries, 3 attempts with
exponential backoff) and optional (`viajante[browser]`). `--fetch auto` uses
sweep for flight searches, regardless of Playwright availability or query
count. Use `--fetch detail` only when browser DOM evidence is specifically
needed and the requested itinerary is supported there. Detail is the only
multi-city path: it selects each leg's first owned card in the browser and
reads the final package. Detail reads no filter echo, so it refuses bag and
carrier filters before launching Chromium. A public sweep never
falls back to browser detail after an empty page, parse failure, or provider
block. A failed search remains unknown unless the provider returned a
recognized empty result.

### 5. From cards to offers

`flights.py` turns raw cards into `FlightOffer`s: parse price, clocks,
duration, layovers, flight numbers, and bags; drop what the local filters
reject; rank by fare (+ an optional named baggage buffer) or by duration or
clock; keep the top N. Then it stamps:

- `typical` / `vs_typical`: date and flex reports may derive a median only from
  their explicitly requested same-route daily fares. Fewer than three priced
  days, or a multi-city trip, means no stamp. A normal flight search has no
  hidden 31-day lookup. It is never a market average from somewhere else.
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

- Empty results, markup drift, and HTTP 5xx may be retried once. The public-page
  sweep does not replay a request after HTTP 429 or raw RPC status 13, and does
  not switch to browser detail after a block. A batch stops
  scheduling pending work on a block; unsent work reports `request_sent: false`
  and `attempts: 0`.
- A direct HTTP 429 resets the TLS session and writes a shared cooldown file in the
  state dir (2 min, doubling to 30 min). While it runs, every Google search in
  any viajante process on the machine fails instantly with `Not sent.` and
  `rate_limited: true`. A named `Retry-After` takes precedence over the guessed
  delay; a proxied response does not pause the machine's direct searches.
- A raw data-less RPC status 13 is preserved in additive diagnostics even when
  the body also resembles a shopping `ErrorResponse`. For a direct session it
  records the existing guessed cooldown; it is not replayed and pending work
  stops. Status 13 alone does not prove an IP block or explain the cause. Error
  diagnostics report only the endpoint host and path, HTTP/RPC status, whether the
  request was sent, attempts, and cooldown basis; they omit query parameters.
  An already active cooldown reports no request sent, zero attempts, and null
  HTTP/RPC status. The browser path detects `google.com/sorry` right away
  instead of waiting for result cards.

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

- **Flights** does not add a hidden 31-day price lookup for `typical`.
- **Dates** explicitly fans out public-page GETs across the requested inclusive
  window, capped at 31 days. Each day is an independent row; successful days
  remain visible when other days fail.
- **Flex** uses the same bounded per-day GETs for its named window, chooses the
  cheapest returned day, then makes one fresh public-page shop for that date.
  It does not guess a winner from failed or unpriced days.
- **Explore** reads the catalog request the Explore page issues in Chromium
  (one adult, economy), then shops each owned destination over the public
  page. A status 13 on the catalog stops it and records the shared cooldown.

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
  need a design decision; neither is implemented.
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
