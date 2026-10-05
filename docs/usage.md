# Usage guide

[Back to the README](../README.md)

This guide assumes Viajante is installed. See the
[quick start](../README.md#quick-start) for installation and
[browser support](../README.md#optional-browser-support) for Booking.com.
Examples use November 2026; replace the dates with future travel dates.

- [Flights](#flights)
- [Dates and flexible travel](#dates-and-flexible-travel)
- [Explore destinations](#explore-destinations)
- [Hotels](#hotels)
- [Flights and hotels together](#flights-and-hotels-together)
- [Saving results and handling errors](#saving-results-and-handling-errors)
- [Python library](#python-library)
- [MCP tool calls](#mcp-tool-calls)

## Flights

Use airport codes and ISO dates: `ORIGIN-DEST:YYYY-MM-DD`.

```bash
viajante flights JFK-LHR:2026-11-15 --fetch sweep
```

To search a round trip as one itinerary, add a return date and `--trip rt`:

```bash
viajante flights JFK-LHR:2026-11-15:2026-11-22 --trip rt --fetch sweep
```

Without `--trip rt`, a route with outbound and return dates produces two
separate one-way searches. For a multi-city itinerary, pass each leg with
`--trip multi`:

```bash
viajante flights JFK-LHR:2026-11-15 LHR-CDG:2026-11-19 CDG-JFK:2026-11-22 \
  --trip multi --fetch sweep
```

### Filters and ranking

The default search is for one adult in economy, with at most one stop. Flights
are ranked by fare plus any baggage buffer you specify, with up to eight
offers shown. The buffer defaults to zero.

```bash
viajante flights JFK-LHR:2026-11-15 --fetch sweep \
  --max-stops 0 --adults 2 --bags 1 --carry-on --top 5 --sort fare
```

Common options are listed below. Run `viajante flights --help` for the full
list and accepted values.

| Option | Meaning |
| --- | --- |
| `--currency GBP` | Request prices in a specific currency. No currency conversion is performed. |
| `--cabin business` | Select a cabin: `economy`, `premium-economy`, `business`, or `first`. |
| `--max-stops 0` | Set the maximum number of stops to `0`, `1`, or `2`. |
| `--airlines BA,AA` / `--exclude-airlines BA,AA` | Include or exclude airline codes in the shopping request. |
| `--alliance oneworld` / `--exclude-alliance star` | Include or exclude `oneworld`, `skyteam`, or `star`. |
| `--depart-window 06:00-12:00` | Keep flights departing within a local time window. |
| `--depart-after 08:00` / `--arrive-before 20:00` | Filter on reported local departure or arrival times. |
| `--via IST` / `--exclude-via DXB` | Filter on reported connecting airports. |
| `--no-overnight any` / `--require-overnight IST` | Filter overnight connections at all airports or named airport codes. |
| `--min-layover 1` / `--max-layover 8` | Filter reported layover durations for one-stop offers, in hours. |
| `--max-duration 16` | Limit the reported itinerary duration, in hours. |
| `--price-cap 500` | Remove returned fares above this amount in the quote currency. |
| `--nearby` | Include known airports in the same city as the named origin or destination. |
| `--include-airports LHR,LGW` / `--exclude-airports STN` | Restrict destinations or exclude origins and destinations; exclusions take priority. |
| `--selection pareto` | Retain price/duration/stops alternatives with bounded selection metadata; default `top`. |
| `--sort duration` | Order by `ranked`, `fare`/`price`, `duration`, `departure`, or `arrival`. |
| `--baggage-buffer 30` | Add a ranking allowance for recognized low-cost carriers, in the quote currency. |

Time, layover, duration, and price-cap filters operate on returned flight
details. Unknown details can remain in results for some filters, so inspect
the returned fields when a constraint is essential. These filters do not turn
a date-calendar cell or Explore catalog entry into a detailed flight offer.

An airport exclusion needs owned locations for every connection; unknown
connections are excluded from that filtered shortlist. Packaged round-trip and
multi-city offers apply local filters to all returned journeys before selecting
`--top`. Missing packaged journeys cannot prove named local filters or complete
itinerary validation.

`--bags N` and `--carry-on` ask Google Flights to price baggage. A baggage
buffer is your own ranking allowance, not a provider fee. The list of low-cost
carriers is partial, so absence from the list does not mean bags are included.
Check baggage terms before booking.

### Choosing a fetch mode

| Mode | Behavior |
| --- | --- |
| `--fetch sweep` | Use HTTP requests without launching a browser for the initial search. |
| `--fetch detail` | Use Playwright and Chromium. Requires the browser extra and a Chromium installation. |
| `--fetch auto` | Use sweep for three or more flight queries; for one or two, use detail when Playwright is installed, otherwise sweep. |

A sweep search that returns empty results or a `blocked` failure can fall back
to detail once when Playwright is installed. The report records this as
`fetch_backend: sweep_then_detail`. Rejected shopping requests and markup
changes do not trigger browser fallback. Successful sweep queries are not
repeated.

Sweep requests for flights, dates, flex, and explore accept `--proxy URL`
(`proxy` in MCP). Browser detail requests have built-in pacing and run
sequentially.

## Dates and flexible travel

Use `dates` to compare departure dates across an inclusive window of up to
31 days. Without `--nights`, it searches one-way fares. With `--nights`, each
departure is priced as a round trip of that length.

```bash
viajante dates BOS-LHR --from 2026-11-01 --to 2026-11-30 --nights 7
```

Use `flex` when you have a target departure date and can move a few days either
side of it:

```bash
viajante flex BOS-LHR --around 2026-11-15 --flex 3 --nights 7
```

This checks November 12–18, selects the cheapest calendar date, then runs one
flight search for that date. If that search returns no matching offers, the
result is empty; it does not keep searching other days.

Date grids stay in date order unless you request another sort. Sorting a grid
does not cut it down to a top-N list. When there is enough calendar data,
`typical` describes the same-route calendar median; it is omitted when the
calendar is unavailable, has fewer than three priced days, or the query is
multi-city.

## Explore destinations

Start with an origin airport. Viajante reads Google's Explore catalog and
searches flight prices for a shortlist of destinations on the departure date:

```bash
viajante explore JFK --from 2026-11-15 --top 5
viajante explore NRT --from 2026-11-15 --exclude-regions asia --top 3
```

`--include-airports` can restrict the destination list. `--exclude-regions`
uses IANA timezone regions such as `asia` or `europe`.

Explore sorts by price by default. Its `--days` value records the intended
stay length, but destination flight searches use the outbound date only.
Likewise, `--month YYYY-MM` selects that month's first day and records its
length; it does not compare every departure date in the month. Use `dates` or
`flex` once you have chosen a route.

For an open-ended trip, start with a small destination shortlist on fixed
dates, then compare nearby dates for the most promising routes.

Pricing failures are separate from empty results: JSON includes additive
`pricing_errors` with the affected query and provider error, and `coverage`
counts each attempted destination. The CLI prints those failures and returns
exit 2 when all pricing attempts fail, or exit 3 for mixed outcomes. MCP does
not cache a report containing pricing failures as a successful search.

## Hotels

Standalone hotel searches require `--currency`. Prices cover the entire stay.
The defaults are two adults, one room, and free cancellation required.

For Google Hotels, which uses HTTP:

```bash
viajante hotels Tokyo 2026-11-12 2026-11-16 \
  --currency JPY --source google --min-rating 4
```

For Booking.com, which requires Chromium:

```bash
viajante hotels Tokyo 2026-11-12 2026-11-16 \
  --currency JPY --source booking --min-rating 8.5 --entire-home
```

The CLI defaults to Booking.com. MCP defaults to Google Hotels. Google ratings
use a 0–5 scale; Booking.com ratings use 0–10, so choose the threshold for your
source.

`--allow-non-refundable` removes the default free-cancellation requirement.
On Booking.com, `--compare-cancellation` runs two sequential searches, with
and without that requirement. It skips the price comparison if either search
fails.

Requested filters, applied filters, and property details are reported
separately. If a free-cancellation filter was applied but the property card
does not state cancellation terms, the output says `filter applied; card
silent`. It does not mark the property's terms as confirmed free cancellation.
Property type, room counts, and cancellation terms can remain unknown.

Use `--near LAT,LNG` for a named reference point and `--max-distance-km N`
to enforce a radius before price ranking. N must be finite and positive and
requires `--near`; offers without coordinates cannot prove the radius and are
excluded. `distance_km` is a straight line, not a walking route. Without a
radius, `--near` only annotates distance. No city center is assumed.

Google descriptions can mention private rooms without pricing one. Parsed
room/capacity fields require unit evidence. Known priced-party mismatches and
insufficient single-unit capacity are excluded; unknown occupancy remains
unverified. Check finalist room rates, sleeping layout, total and cancellation.

Google entity and search URLs reproduce dates, adults, rooms and currency.
`link_context` and `applied.url_context` state whether a link identifies a
stay, property, location or none. A stay context does not guarantee the quoted
price or availability. `verify_answer` checks provenance, not link reachability.

## Flights and hotels together

Use `trip` to search flights and accommodation in sequence:

```bash
viajante trip JFK-LHR:2026-11-15:2026-11-22 --trip rt \
  --hotel London --adults 2 --currency GBP --fetch sweep --source google
```

Hotel dates are derived from the itinerary unless you specify them. A
`trip_total` is included only when flight and hotel results contain usable
prices, their dates overlap, and their currencies match. It is the sum of
the flight fare and hotel stay, not a reservation or a quote for every trip
expense. Trip searches support adult occupancy only.

Each requested dated flight journey contributes its cheapest owned fare.
Only nearby airport alternatives for the same dated journey share a minimum;
separate dates and different multi-city legs are not collapsed.

## Self-transfer through a named via

Use `self-transfer` to pair two separate one-way tickets through an airport you
name:

```bash
viajante self-transfer MAD-LHR-JFK:2026-11-10 --min-connection 3 --currency EUR
```

The command shops `MAD-LHR` and `LHR-JFK` in one sweep and pairs each returned
first-leg offer with each returned second-leg offer. Add `--second-date` when
the second ticket departs on a later day. Each pairing reports:

- `connection_minutes`: the gap between the first ticket's last arrival and the
  second ticket's first departure, in UTC. It is `null` when either clock, date
  or timezone is missing or ambiguous.
- `status`: `ok`, `too_short`, `too_long` or `unknown`. The bounds are the ones
  you name with `--min-connection` and `--max-connection`; without them there
  is no bound. A departure at or before the arrival is always `too_short`. An
  unmeasured gap is always `unknown`, never `ok`.
- `same_airport`: whether the first ticket lands at the airport the second
  departs from, or `null` when segments do not show it.
- `total_price`: the sum of both fares, only when both carry the same quote
  currency.

Every pairing has `protected: false`. The two tickets are separate: a delay on
the first does not protect the second, and checked bags must be collected and
re-checked. The measured margin is evidence, not an endorsed minimum
connection, and it does not show that baggage recheck, immigration or transit
rules allow the connection. Pairings with an out-of-bounds status sort after
the rest, then by total price and margin. `--top` caps both the per-leg
shortlist and the pairings shown; `eligible_pairings` is the count before the
cap. If either leg fails, the report keeps that leg's error and has no
pairings. Coverage covers only the named via, dates and returned offers.

## Saving results and handling errors

Search commands print tables. Add `--save FILE` to write JSON as well:

```bash
viajante flights JFK-LHR:2026-11-15 --fetch sweep --save /tmp/viajante-flights.json
```

Reports include query status, quote currency, and returned offers or error
details. Optional information, such as booking links and calendar medians,
appears only when it can be determined. Offers retain source text alongside
parsed fields. The report types and JSON fields are defined in
[`models.py`](../src/viajante/models.py).

| Error code | Meaning |
| --- | --- |
| `no_results` | The search returned no results. |
| `currency_mismatch` | Owned rows existed, but none matched the requested currency keep. Skiplagged cards are USD; viajante does not convert. |
| `rejected` | The provider rejected the request. |
| `blocked` | The provider blocked access or presented a challenge. |
| `markup_drift` | The response could not be read in the expected format. |
| `fetch_failed` | The request failed for another reason. |
| `browser_unavailable` | A required browser dependency or installation is missing. |

Do not treat a failed search as a price or availability result. If a site
blocks a request, avoid repeated retries. Report persistent parsing problems
with the command, version, and error, after removing personal information.

Browser state and failure diagnostics live outside the checkout, under
`VIAJANTE_STATE_DIR`, `$XDG_STATE_HOME/viajante`, or
`~/.local/state/viajante`, in that order of preference. JSON is saved only
when requested, using a temporary file followed by a rename.

## Python library

Use `get_flights` for a route string, or construct queries explicitly:

```python
from datetime import date

from viajante import FlightQuery, HotelQuery, search_flights, search_hotels

flights = search_flights(
    [FlightQuery(origin="JFK", destination="LHR", departure_date=date(2026, 11, 15))],
    currency="GBP",
    fetch="sweep",
    top=5,
)

hotels = search_hotels(
    [HotelQuery(location="London", check_in=date(2026, 11, 15), check_out=date(2026, 11, 22))],
    currency="GBP",
    source="google",
    top=5,
)

for result in hotels.queries:
    if result.status == "ok":
        for stay in result.offers:
            print(stay.total_price, hotels.currency, stay.title)
    else:
        print(result.error)
```

`get_flights` takes a route spec or trip objects, not prose. Turning a request
into a route is the calling agent's job. For other search types, use
`search_dates`, `search_flex`, `search_explore`, `search_trip`, or
`search_self_transfer`.

See the exported types in [`viajante.__init__`](../src/viajante/__init__.py)
for the Python interface.

## MCP tool calls

After configuring the [MCP server](../README.md#connect-an-ai-assistant), your
assistant sends structured arguments to the tools. For example, a request
to compare seven-night trips across November maps to:

```text
search_dates(route="BOS-LHR", start="2026-11-01", end="2026-11-30", nights=7)
```

For a departure that can move three days either side of November 15:

```text
search_flex(route="JFK-LHR", around="2026-11-15", flex=3, nights=7)
```

For flights and a hotel stay in one request:

```text
search_trip(routes=["JFK-LHR:2026-11-15:2026-11-22"], location="London", trip="rt")
```

For two separate tickets through a named via:

```text
search_self_transfer(origin="MAD", via="LHR", destination="JFK", departure="2026-11-10", min_connection_hours=3)
```

These are tool-call examples, not Python library calls. The MCP server uses
stdio and runs one search at a time. See the signatures in
[`mcp_server.py`](../src/viajante/mcp_server.py) for all arguments.


To diagnose installation drift, run `viajante --version` or call MCP
`get_runtime_info` (both available in 1.4.0+). Hotel JSON includes the executing
`viajante_version`. An npm MCP and a separately installed uv CLI can differ.
Unpinned `uvx` may reuse an old installed tool. Use `uv tool upgrade viajante`
for that installation, or refresh an explicitly published version:
`uvx --refresh --from 'viajante==VERSION' viajante --version`. Reload MCP after
upgrading; confirm the executing version before using new parameters.

## Verifiable times and diverse finalists

Use Pareto when the caller wants alternatives on price, duration and stops:

```bash
viajante flights JFK-LHR:2026-11-15 --fetch sweep --selection pareto --top 3
```

`selection="pareto"` is also accepted by `get_flights`, `search_flights` and
MCP `search_flights`. Explicit filters still apply. Comparison is limited to
returned candidates in that query and currency, with equivalent known baggage.
Incomplete metrics and unproven baggage equivalence cannot eliminate an offer.
The budget first reserves cheapest, shortest and fewest-stop alternatives,
then uses the requested sort on the frontier, then incomplete rows. Packaged
metrics require every journey. `top` remains the default selection mode.

Flight segments may include `arrival_date`, `departure_timezone` and
`arrival_timezone`. Missing facts remain absent; completeness reports dates
and zones as known/unknown. To validate selected rows locally:

```python
from viajante import get_flights, validate_itinerary

report = get_flights("JFK-LHR:2026-11-15", fetch="sweep")
result = report.to_dict()["queries"][0]
selected = [{"status": "ok", "query": result["query"], "offer": result["offers"][0]}]
validation = validate_itinerary(selected, {
    "arrival_deadline": "2026-11-16T12:00",
    "chronological": True,
})
```

The deadline is interpreted in the final destination's local IANA zone.
An explicit ISO offset must agree with that local time. Missing dates/zones,
ambiguous or nonexistent DST times return unknown. `chronological` checks
segment and selected-journey UTC order, including overlap. Optional
`min_stay_days` and `max_stay_days` count local date differences between a
journey arrival and the next journey departure from that same airport, including
packaged journeys. They do not infer a stay after the final flight.
`travel_start`/`travel_end` still bound query departure dates; `arrive_before`
and `depart_after` still test the existing local-clock bounds. Chronology
alone does not prove connection protection, immigration requirements or
sufficient time to change airports. Unknown never means compliant.

## Finalist details in MCP and Python

MCP Google flight shopping offers and hotel offers receive an opaque
`selection_id`. Flex shopping and nested trip offers also receive references.
References belong to this process and expire when their ledger group is
expelled from the last 20 groups. Unknown references fail without a request.
Successful cache replay within five minutes retains the original reference
and retrieval date and re-registers its snapshot when necessary.

Call `get_flight_details(selection_id)` for the existing segments, baggage,
links, age and unknown fields. `refresh=True` re-shops the original query;
it bypasses the response cache and retains cooldowns, retries and the process
lock. The original and new quotes are separate. Matching requires complete
segment identities across all journeys and the same occupancy/cabin/baggage
request. Tokens and prices may change. Incomplete identity, zero matches,
multiple matches or missing original context are inconclusive; no similar
flight substitutes for the selection. A unique match reports same-currency
`price_change` and original `filter_violations` without hiding its new fare.
Refund rules, extras and current availability remain unproven unless returned.

Call `get_hotel_details(selection_id)` to inspect original evidence and its
limits. With `room_rates=True`, Skiplagged room rates are a separate quote
in `room_quotes`; `original_quote` keeps the Google/Booking/Skiplagged price.
External finalists require exact normalized names and a named city identified
unambiguously by the offline catalogue (for example `Prague` or `London, GB`). An ambiguous location or a different
provider-resolved place raises an input error: repeat the hotel search with a
sufficiently identified city. Skiplagged finalists use their own provider ids.
Dates, adults and rooms are preserved, within the helper's limits (10 adults,
5 rooms). No match, homonyms or provider failures preserve the original quote
and return the helper's error. Rates remain in provider order and USD; their
cancellation and occupancy evidence does not attach to the original fare or
prove combined capacity. Missing cancellation deadlines remain unknown.

The library uses the original typed report and zero-based query/offer indices:

```python
from viajante import get_flight_details, get_hotel_details

snapshot = get_flight_details(report, 0, 0)
refreshed = get_flight_details(report, 0, 0, refresh=True)
hotel_snapshot = get_hotel_details(hotels, 0, 0)
room_quotes = get_hotel_details(hotels, 0, 0, room_rates=True)
```

A serialized report can supply snapshot evidence, but flight refresh needs the
original typed report's internal search context. There are no details CLI
commands. Run provider searches sequentially and verify finalist prices and
terms on the provider before booking.
