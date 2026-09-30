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
  "best deal" line to parrot. The agent reads price, duration, stops, layover
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
  draft reply that no search in this process returned.

When something tempts you to pre-digest the payload for the agent (summaries,
"cheapest is X" lines, recommendation prose), don't. A capable agent already
does that better, and a canned summary pushes it to repeat instead of reason.

## Surfaces

One library, three ways in:

| Surface | Entry | Notes |
| --- | --- | --- |
| MCP (stdio) | `viajante-mcp` → `mcp_server.py` → `mcp_handlers.py` | The main surface. 12 tools. |
| CLI | `viajante <cmd>` → `cli.py` | Same searches, human tables, `--save` JSON. |
| Library | `viajante.search_*`, `get_flights` | What both of the above call. |

| MCP tool | CLI | What it is |
| --- | --- | --- |
| `search_flights` | `flights` | Shop one or more routes (one-way, packaged RT, multi-city). |
| `search_dates` | `dates` | Cheapest fare per day across a window (≤31 days). |
| `search_flex` | `flex` | Cheapest day in ±N around a date, then one shop. |
| `search_explore` | `explore` | Destinations from an origin, shortlist priced. |
| `search_hotels` | `hotels` | Total-stay hotel prices (Google HTTP or Booking browser). |
| `search_trip` | `trip` | Flights then one hotel, plus a sum when both succeed. |
| `search_hidden_city` | `hidden-city` | Skiplagged, opt-in, never mixed with Google evidence. |
| `lookup_airports` | `airports` | Offline IATA lookup. |
| `compare_awards` | `awards` | Award offer vs cash: cents per point, transfer paths. |
| `lookup_transfers` | `points` | Local card-to-program transfer table. |
| `validate_itinerary` | — | Local constraint check of selected offers. |
| `verify_answer` | — | Local check of a draft reply against the search ledger. |

The MCP process holds one search lock: a second concurrent search fails
immediately instead of queueing. Lookups and the two verifiers may run during
a search. Identical successful searches within 5 minutes are replayed from an
in-process cache (`cached: true`) instead of asking Google again.

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
detail once and says so (`fetch_backend: sweep_then_detail`).

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

### 6. Failures are typed

Every provider failure becomes a `SearchError` with a code: `no_results`,
`rejected`, `blocked`, `markup_drift`, `fetch_failed`, `browser_unavailable`.
Only failures that can succeed on a second try are retried:

- Sweep retries empty, drift, and 5xx once after 50 ms.
- HTTP 429 resets the TLS session and writes a shared cooldown file in the
  state dir (2 min, doubling to 30 min). While it runs, every Google search in
  any viajante process on the machine fails instantly with `Not sent.` and
  `rate_limited: true`.
- A data-less RPC error envelope (`["wrb.fr", null, …, [13]]`) is `blocked`,
  not `markup_drift`. Google sends it when it is throttling the IP, often
  while plain shopping still answers. The browser path detects
  `google.com/sorry` right away instead of waiting for result cards.

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

Two sources, one loop (`hotels.py`):

- **Google Hotels** (MCP default): the `AtySUc` RPC on `batchexecute`, over the
  same Chrome-TLS session. Encode and parse live in `google_hotels_rpc.py`.
  Each stay carries the total-stay price text, rating (0–5), review count,
  latitude/longitude, and a Google link. Free cancellation and vacation-rental
  type go into the request. With a named `min_rating`, the price-sorted page
  and a relevance-sorted page ride the same multiplexed round-trip, because the
  cheapest page is mostly low-rated.
- **Booking.com** (CLI default): Playwright, with chips in the URL and the
  card DOM parsed for cancellation, lodging kind, and unit counts. Slow on
  purpose; challenges are not hammered.

The loop keeps three things apart: what the caller *asked* for, which filter
chips were *applied*, and what each card *says*. `filter applied; card silent`
is a real state, not "free cancellation". Prices are always the total stay.
Hotels have no origin airport, so currency is always required.

## Money and geography

There is no home market. Currency is either named (`--currency` / MCP
`currency`) or inferred from a named origin airport's country (`quote.py`:
JFK → USD, NRT → JPY, GRU → BRL). Viajante never converts; Google returns
amounts in the requested currency and the agent may convert for the user.
The fetch language is always English (`hl=en`); prompts can be in any
language.

## Testing

~1,200 unit tests run offline in about a second: no network, no Chromium.
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
  The RPC status-13 signal does not yet write the shared cooldown file.
- **Internal `currency="EUR"` defaults** remain in lower-level signatures and
  dataclasses as test fixtures. Every public entry point resolves or requires
  currency first.
- **Detail mode cannot price return legs**; `auto` routes packaged trips to
  sweep for that reason.
- **Booking.com has no HTTP path**; it needs Playwright and is slow by design.
