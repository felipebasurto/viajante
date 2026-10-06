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

### Split tickets (opt-in)

`--split-tickets` adds separately ticketed alternatives built only from real
one-way quotes. It costs extra searches, so it is off by default and capped.

```bash
# One-way: origin to hub on one ticket, hub to destination on another
viajante flights JFK-NRT:2026-11-10 --fetch sweep --split-tickets --split-via HKG,ICN
# Round trip: cheapest outbound one-way plus cheapest return one-way vs the package
viajante flights --trip rt JFK-NRT:2026-11-10:2026-11-24 --split-tickets
```

| Option | Meaning |
| --- | --- |
| `--split-via CODES` | Up to 5 connection airports to try (every one named is searched). Unnamed, hubs are the layover airports in the packaged results shown. An unknown code or more than 5 is an error naming `via`. |
| `--split-max-hubs N` | Hubs to try (default 3, or every `--split-via` airport; at most 5). Each hub is 2 searches, or 3 with `--split-overnight`. |
| `--split-min-connection HOURS` | Minimum gap between tickets at the hub (default 3). A planning default, not provider evidence. |
| `--split-overnight` | Also search the second ticket on the next day. |
| `--split-leg-stops N` | Maximum stops on each hub ticket (default 0). |

Each result is labelled `split_ticket: true` and `connection_protected: false`
(`self_transfer: true` for a hub) and lists both tickets with their own offer
and Google Flights link. A missed connection between separate tickets is not
rebooked by either airline, and bags may need to be collected and checked in
again. Confirm each ticket on its own link.

The total is summed only when every ticket has the same currency; otherwise it
is `null` and the tickets stay listed (nothing converts). Totals are rounded to
the currency's minor unit. `vs_packaged` compares with the cheapest returned
packaged offer quoted in the same currency, and carries a non-negative `savings`
(`direction: "cheaper"`) or `extra_cost` (`direction: "costlier"`). Results are
ordered within one currency at a time (the requested currency first, unknown
totals last), and `--top` applies to the requested currency (other groups are
capped, below).

A hub connection needs ticket 1 to land at the hub airport and ticket 2 to leave
from it (owned segment airports), plus an owned arrival and departure time at the
hub. The gap is measured in UTC, each time converted with the hub's catalogue
timezone, so date-line and after-midnight arrivals stay correct. A missing
timezone, or a local time that does not exist or happens twice around a DST
change, leaves the timing unproven: the pair is never given a number. Rejected
hub pairs are counted in `rejected` (`airport_mismatch`, `airport_unproven`,
`timing_unproven`, `connection_too_short`), not shown.

Mixed one-ways pair the cheapest outbound and return where the return departs
after the outbound lands, compared the same way in UTC (`return_before_arrival`
is rejected), one pair per currency. When the timing cannot be proven, a pair is
kept only if no proven pair exists in its currency; it says `timing_proven: false`
with a `timing_note` (the return may leave before the outbound lands; not
verified), and is counted in `rejected.timing_unproven` (dropped) and
`coverage.scope.timing_unproven_kept` (kept). Unknown timing never sorts above
proven timing. Rows in other currencies and rows with an unknown total are capped
at 3 per group (`other_currency_row_cap`); `omitted_other_currency` counts the
rest and the CLI says so.

A recorded Google cooldown stops the extra searches and the command exits
non-zero. `--save` adds the report under `split_tickets`. The MCP tool is
`search_split_tickets`; one-way routes use `via` / hubs, `trip="rt"` uses mixed
one-ways.

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
`search_dates`, `search_flex`, `search_explore`, or `search_trip`.

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
