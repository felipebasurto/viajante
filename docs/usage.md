# Usage guide

[Back to the README](../README.md)

This guide assumes Viajante is installed. See the
[quick start](../README.md#quick-start) for installation and
[browser support](../README.md#optional-browser-support) for Booking.com.
Examples use November 2026; replace the dates with future travel dates.

The [1.4.5 release record](release-1.4.5.md) lists changes from 1.4.1. The
[1.4.6 candidate record](release-1.4.6.md) documents the public-page flight
transport and its current limits; release validation is still pending.

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
separate one-way searches. The public-page flight transport currently refuses
`--trip multi` before making a request; it does not approximate a multi-city
route with separate searches.

### Filters and ranking

The default search is for one adult in economy, with at most one stop. Flights
are ranked by fare plus any baggage buffer you specify, with up to eight
offers shown. The buffer defaults to zero.

```bash
viajante flights JFK-LHR:2026-11-15 --fetch sweep \
  --max-stops 0 --adults 2 --top 5 --sort fare
```

Common options are listed below. Run `viajante flights --help` for the full
list and accepted values.

| Option | Meaning |
| --- | --- |
| `--currency GBP` | Request prices in a specific currency. No currency conversion is performed. |
| `--cabin business` | Select a cabin: `economy`, `premium-economy`, `business`, or `first`. |
| `--max-stops 0` | Set the maximum number of stops to `0`, `1`, or `2`. |
| `--airlines BA,AA` / `--exclude-airlines BA,AA` | Include or exclude airline codes. Public-page sweep only; see the limits below. |
| `--alliance oneworld` | Include `oneworld`, `skyteam`, or `star`. Public-page sweep only. `--exclude-alliance` is refused in 1.4.6. |
| `--carry-on` | Request one carry-on bag for the whole party (Google's counter is a party total). Public-page sweep only. |
| `--bags N` | Checked bags. Refused in 1.4.6: no transport can verify them. |
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

The default `auto` and explicit `sweep` modes use Google's public results page.
A carry-on, airline include, airline exclusion, or alliance include rides the
page request, and every page read must echo it (the `Bags` or `Airlines`
filter chip, and for alliances the airline catalog) or the read fails as
`markup_drift`. Google registers an airline exclusion without applying it, so
Viajante also drops cards that show an excluded airline or no carrier at all.
Excluded airlines' codeshare rows disappear from the page, and operating-carrier
evidence on a card is limited, so an exclusion is not proof about who operates
each segment. Checked bags, a zero carry-on, and alliance exclusion are refused
before any request because no page evidence can prove them. Browser detail
reads no filter echo and refuses all bag and carrier filters. A carry-on
filter does not prove an offer's bag allowance; verify it on Google Flights.
`dates` and `flex` stay on the public-page transport. A baggage buffer is only
a local ranking allowance and never proves a bag is included.

For `--trip rt`, the page's displayed outbound amounts are selection references,
not standalone fares. Viajante checks at most eight outbound candidates on
selected return pages; only the provider's returned package total is reported.
The report is scope-bound because additional outbound choices were not checked.

### Metro city codes

Name a metro code instead of an airport to search every airport in that city
group:

```bash
viajante flights NYC-LON:2026-11-15 --currency USD
```

`NYC` searches `JFK`, `EWR` and `LGA`; `LON` searches `LHR`, `LGW`, `STN`,
`LTN`, `LCY` and `SEN`. Run `viajante airports NYC` to list a metro's
airports. Each expanded query carries `nearby_label` `metro NYC`, and
`--exclude-airports` still removes members. Only a code you name expands: `JFK`
searches `JFK` alone. Metro codes work on one-way and `--trip rt` routes in
`flights` and `trip`, not on open-jaw or multi-city routes, and not in
`dates`, `flex`, or `explore`. Combining `--nearby` with a metro code
is an error, because expanding only the other side would drop part of the
request. Name metros or airports on both sides instead (`NYC-LON`).
A route whose two ends are the same metro is rejected: `LON-LON`, `JFK-NYC`,
and `LHR-LGW` do not expand. The error names the origin, the destination, and
the metro.

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

### Recommendation and shortlist

A successful flights query (CLI, `search_flights`, and the flights half of
`search_trip`) may carry a `recommendation` block, and carries none when the
provider returned nothing. It adds to `offers`; it never replaces them, and
`--sort` keeps its meaning.

```json
{
  "relaxed_requirements": [],
  "price_comparison": "compared",
  "scoring": {"weights": {"price": 0.5, "duration": 0.35, "stops": 0.15}},
  "shortlist": [
    {"labels": ["recommended", "fastest"], "score": 66.0,
     "requirements": {"max_stops": "met"},
     "highlights": ["Nonstop", "Duration 7 hr 0 min", "Fare $620"],
     "tradeoffs": ["Checked bag fee unknown", "Fare rules (refund, change) not shown"],
     "offer": {"airline": "Delta", "price": 620.0}}
  ]
}
```

(abridged; each `offer` is the full offer object, with `evidence`.)

**Requirements.** `--max-stops`, `--depart-window`, `--depart-after`,
`--arrive-before`, `--max-duration`, `--bags`, and `--carry-on` are hard. The
pick meets all that you named. If no offer does, the fewest are relaxed, and
`relaxed_requirements` names exactly which. Ties relax the earlier name first
in this order: `arrive_before`, `depart_after`, `depart_window`, `max_duration`,
`carry_on`, `bags`, `max_stops`. Each shortlist entry shows `met`, `unmet`, or
`unknown` per requirement. An unknown departure/arrival clock, or an unknown
stop count under `--max-stops 0`, cannot prove a requirement; unknown bag counts
or duration stay `unknown` and are called out in `tradeoffs`. Other filters
(`--price-cap`, airlines, `--via`, overnight, layover bounds) are never relaxed,
and round-trip and multi-city packages are not relaxed at all. A relaxed pick
may fail a filter that emptied `offers`, so read `relaxed_requirements` before
presenting it as a match. Over MCP, a query with `empty_reason` `filtered_out`
(envelope status `no_results` when every query is `filtered_out`) that still has
a `recommendation` means the pick is a relaxed one, not an exact match.

**Shortlist.** Up to three entries with different stop counts or departure
slots (`night` 00:00-05:59, `morning` 06:00-11:59, `afternoon` 12:00-17:59,
`evening` 18:00-23:59). `recommended` is the best score; `cheapest` and
`fastest` are the lowest ranking cost (fare plus any named baggage buffer) and
the shortest known duration. When the cheapest or fastest offer shares its stop
count and slot with another pick, the entry is the best by that measure among
different ones, labelled `cheapest_distinct` / `fastest_distinct`, and a note
says what was not listed. A free third slot is an `alternative`. Offers with the
same carrier, clocks, and stop count are one flight; the cheaper fare is kept.
Connections longer than three times the fastest nonstop (or the shortest known
offer when no nonstop exists) are left out of the comparison and the `ranked`
shortlist. `--sort price` retains those connections in the fare-ordered offers.

**Score.** `100 * (1 - weighted penalty)`, higher is better. The price and
duration penalties are how much worse an offer is than the best in the compared
pool, as a fraction capped at 1 (a 5-minute gap or a few percent barely
register). The stops penalty is stops divided by 2. An unknown duration or stop
count takes the worst penalty, 1. The weights are price 0.5, duration 0.35,
stops 0.15. Ties break by ranking cost, duration, departure, carrier.

**Currency.** Offers whose currencies differ, or whose currency is unproven, are
not compared on price: `price_comparison` says `skipped_mixed_currency` or
`skipped_unknown_currency`, the price weight is 0, and there is no `cheapest`.
The other weights are rescaled (duration 0.7, stops 0.3), as `scoring.weights`
shows. Viajante does not convert.

**Wording.** `highlights` and `tradeoffs` restate returned fields only: fare
text, duration, stops, layover city and length, clocks, carrier, bag counts. A
missing fact reads as unknown ("Checked bag fee unknown", "Carry-on not shown").
Fare rules are never in the provider rows, so each entry says "Fare rules
(refund, change) not shown"; refundable or flexible fares cannot be required.
There are no savings claims and no reference prices.

### Choosing a fetch mode

| Mode | Behavior |
| --- | --- |
| `--fetch sweep` | Fetch the public Google Flights results page over Chrome-TLS HTTP/2; no browser or unsigned shopping RPC is used. |
| `--fetch detail` | Use Playwright and Chromium. Requires the browser extra and a Chromium installation. |
| `--fetch auto` | Use the public-page sweep for every flight search, regardless of query count or browser availability. |

A public sweep never falls back to detail after an empty result, parse failure,
or provider block. A missing or malformed `ds:1` bootstrap payload is a parse
failure; only a recognized provider-empty shape means no results. Round-trip
searches check at most eight outbound candidates through selected return pages.
The public page bootstraps no multi-city results, so `auto` and `sweep` refuse
`--trip multi` before any request. Use an explicit `--fetch detail` for
multi-city; it drives the browser through each leg and is not yet verified
against the live provider in 1.4.6. Detail refuses bag and carrier filters.

Flights, dates, and flex sweep requests accept `--proxy URL` (`proxy` in MCP).
`VIAJANTE_SWEEP_MODE=standard` allows at most 8 concurrent GETs; `conservative`
allows 2. Any other value fails before provider access. This is a local
concurrency setting, not a provider quota guarantee. Browser detail requests
have built-in pacing and run sequentially.

### Split tickets (opt-in)

The CLI rejects `--split-tickets` combined with `--arrive-before`, `--depart-after`,
`--depart-window`, `--max-duration`, `--min-layover`, `--max-layover`, `--via`,
`--exclude-via`, `--no-overnight`, `--require-overnight`, `--exclude-airports`,
`--include-airports`, or a non-zero `--baggage-buffer` before any search runs.
These filters do not yet apply to split itineraries. `--split-via` and
`--split-min-connection` are the split connection controls. `--top` also caps
split pairings in the requested currency.

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
one-ways. Over MCP it carries the result envelope: `ok` with any itinerary (`partial`
if a fetch failed), the failure's status with `not_loaded` when none was found
and a fetch failed or a cooldown ran, `no_results` / `filtered_out` when every
pairing was rejected, and `provider_empty` only when every leg came back empty.

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

This checks November 12–18 with one public-page GET per day (up to 31), selects
the cheapest returned date, then makes one additional fresh public-page search
for that date when a provider block has not stopped the search. Day failures
remain attached to their rows; an unpriced or failed day is never treated as a
fare. `dates` and `flex` use public-page GETs even if their accepted fetch
setting says `detail`; they do not launch Playwright.

`dates` uses the same explicit per-day GET fanout across its requested window,
up to 31 days. Successful dates stay visible when other dates fail. An ordinary
`flights` query does not add an automatic 31-day typical-price lookup; dates and
flex use only the windows the caller explicitly named.

Date grids stay in date order unless you request another sort. Sorting a grid
does not cut it down to a top-N list. When there are at least three priced days,
`typical` describes the same-route median of the explicitly searched days. It is
omitted when fewer than three days are priced or the query is multi-city.

## Explore destinations

The public-page transport does not currently support Google's Explore
destination catalog. `explore` fails closed before networking. Name destinations
and search those routes with `flights`, then use `dates` or `flex` for a named
route's dates; no destinations or prices are inferred from a catalog.

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
Only nearby or metro airport alternatives for the same dated journey share a
minimum;
separate dates and different multi-city legs are not collapsed.

## Re-checking an offer

Fares move and flights are retimed. Before presenting a finalist as current,
re-check it. `viajante recheck-offer` takes an offer from an earlier result (or
a `{query, offer}` row) and runs one fresh Google Flights search; it never
replays a cache.

```bash
viajante flights JFK-LHR:2026-11-15 --fetch sweep --save /tmp/flights.json
# put one offer (or {"query": ..., "offer": ...}) from that file into offer.json
viajante recheck-offer --offer offer.json --save /tmp/recheck.json
```

The offer is matched by itinerary identity: flight numbers plus scheduled
departure time for every segment. It needs a full segment identity (flight
number, origin, destination, departure clock); otherwise the result is
`incomplete_identity`, no search is sent, and `missing` lists what to add.
`--allow-loose-match` (MCP `allow_loose_match`) opts in to matching by carrier
plus departure times; the result says `loose_match: true`. The result is
exactly one outcome (`previous.source` is `search_evidence` only when the MCP
ledger holds an offer with that `evidence_id`, price, currency and the same
segments, and previous leg times then come from that offer; otherwise
`caller_supplied`, and the old amount, itinerary and `differences[].previous`
are returned but not recorded as owned evidence):

| Outcome | Meaning |
| --- | --- |
| `same_price` | The identical itinerary was found at the same amount and currency. |
| `price_changed` | The identical itinerary was found at another amount. `previous` and `current` carry each amount with its own currency. A `currency` that differs from the offer's own is refused (exit 1): viajante does not convert. |
| `multiple_matches` | More than one fresh offer matches the identity. None is picked and no price verdict is given; `candidates` lists each price, currency and journey times. Exit 0. |
| `incomplete_identity` | The offer lacks flight numbers, airports or clocks. `check_completed` is false, `missing` names the fields, and no search was sent (`checked_at` is null). Exit 2. |
| `not_found` | A completed check found nothing. `reason` is `provider_empty`, `filtered` (Google returned offers but none passed the query's own constraints), or `not_among_offers`. A close alternative (a different flight number at similar times) is listed as `closest_candidate` with its `differences`, for information only. |
| `substituted` | Only with `--allow-substitute` (MCP `allow_substitute`): no identical itinerary, but a close alternative between the same end airports (a shared flight number, or on every journey a first departure within 90 minutes of the original on the same marketing carrier). `differences` lists what provably differs; `null` is unknown. |
| `check_failed` | The check did not run to an answer; this says nothing about whether the offer still exists. `check_completed` is false, with `reason` and `error`. `reason` is `blocked`, `rate_limited`, `markup_drift`, `rejected`, `fetch_failed`, `browser_unavailable`, `currency_mismatch`, or `incomplete_offers` (fresh round-trip or multi-city offers came back without every journey, because the follow-up search for the next journey failed or was ambiguous, and nothing matched). Do not retry a rate limit. |

Over MCP the result also carries the shared envelope (read it first). Found outcomes
(`same_price`, `price_changed`, `substituted`, `multiple_matches`) are `ok` / `complete`.
`not_found` is `no_results` / `complete` with `empty_reason` `provider_empty` or
`filtered_out`; `not_among_offers` stays `ok` because the provider did return flights
(`partial` when the result notes that only the N cheapest of M offers were compared).
`check_failed` is the failure's status (`rate_limited`, `blocked`, `timeout`, `failed`),
`blocked` completeness (`partial` for `incomplete_offers`), `empty_reason` `not_loaded`
and the error's code, with `retry_after` when a cooldown is recorded. `incomplete_identity`
is `failed` / `blocked` with `error_code` `incomplete_identity`. `observed_at` is
`checked_at`, null when nothing was sent. A completed check is recorded in the evidence
ledger (caller-typed values stripped); `check_failed` and `incomplete_identity` are not.

The query (the offer's evidence query, or `--query FILE`) is replayed: cabin,
stops, bags, carry-on and airline or alliance filters ride the search request.
A `price_cap` in it is not sent; instead a matched fresh offer that breaks it is
reported in `filter_violations` (`price_cap`, and for locally provable cases
`max_stops`, `airlines`, `exclude_airlines`). `filters_replayed` lists what rode the
request and `filters_checked` what was only checked locally (`price_cap` is never
sent). `max_stops` and `exclude_airlines` breaches are defensive, because the search
already applies them; an `airlines` allow list passes when any carrier on the offer
is allowed, as in the search. Cabin, carry-on and alliance filters are proven by the
fresh page's echo, not on the offer. With `--allow-loose-match`, a connecting leg without segments is still
`incomplete_identity` (its stops cannot be compared); origin and destination are
compared in every match. `incomplete_identity`, `check_failed` and input errors are
never recorded in the MCP evidence ledger.

Every result has `checked_at` except `incomplete_identity` and a check the machine-wide cooldown refused (nothing was sent), where it is null. A hand-built
identity needs `price`, `legs[].segments[]` (flight number, origin,
destination and departure clock), a `query` with `adults`, `cabin`, and
`max_stops`, and a currency; none of them is guessed. The CLI exits 0 when the
check ran to an answer (any outcome but `check_failed` and
`incomplete_identity`), 1 for bad input, and 2 otherwise. Round trips carry a
note: each fresh outbound carries the one return that is unique at its cheapest
package price, so a still-buyable original return can read as a different
itinerary. A re-check is not a booking guarantee: the price and terms are
confirmed only on the provider's own page. The fresh search compares up to the
100 cheapest one-way offers (20 for packaged trips); the result notes any
truncation.

## Price history and watches

Viajante can remember the prices it observed, locally, so you can see how a
query moved between your own checks. It is off by default. Opt in for the CLI
and the MCP server with an environment variable:

```bash
export VIAJANTE_PRICE_HISTORY=1     # record; unset it (or set 0) to stop
```

For an MCP client, put the variable in the server's `env` block. Each real
search result with at least one priced offer appends one line to
`price-history.jsonl` in the state directory: route or stay identity, the query
and filters that change price (dates, passengers, cabin, stops, bags, airline
and layover filters, `top`, `sort`, and so on), the cheapest returned amount and
its currency, the number of offers, provider, backend, and `observed_at`.
Failures, empty results, rate-limited searches, and replayed MCP cache hits are
never recorded. A `recheck-offer` is a real search and is recorded too when the opt-in is on. Entries are never edited; the file keeps the newest 2000.
Currencies are never converted.

```bash
viajante history --route JFK-LHR --date 2027-03-01
viajante history --kind hotel --location Lisbon
viajante history --clear            # delete the whole log
```

`viajante history` and the MCP `price_history` tool return one series per
exact query and currency, with first seen, last seen, lowest, highest, and the
change since the previous observation. Queries that differ in any recorded
parameter, and observations in different currencies, are separate series and
are never compared. One observation is reported as such. There is no
prediction. If the log exists but cannot be read, `price_history` returns
`read_error` with `series` null (unknown, not empty) and a failed, blocked
result (`error_code` `history_unreadable`).

A watch is a saved `search_flights` or `search_hotels` argument set that you
re-run on demand:

```bash
viajante watch jfk-lhr --kind flight \
  --params '{"routes": ["JFK-LHR:2027-03-01"], "currency": "USD"}'
viajante watch jfk-lhr              # run again later: reports the change
viajante watch --list
viajante watch jfk-lhr --remove
```

A watch run records its own observation even when the global opt-in is off. It
goes through the normal search path, so it respects the Google cooldown, the
5-minute cache (a cached run says so and records nothing), and the single
search lock. A proxy is never stored in a watch. Saving builds the watch's
queries first, so a bad route, date or filter is rejected and not persisted.
Saving under an existing name replaces that watch. `--kind` is `flight` or
`hotel`, the same words `history --kind` and the MCP tools use.

The log and the watches file are updated under an exclusive lock (a `.lock`
file beside each in the state directory), so a scheduled `viajante watch`
running next to the MCP server cannot drop each other's entries. If the watches
file exists but cannot be read (corrupt or unreadable), listing reports it as
unreadable (`watches: null`) rather than an empty list, and saving or removing
refuses and leaves the file as it was.

Viajante sends no notifications and runs no scheduler. To check periodically,
call `viajante watch NAME` from your own cron job or agent, at a low frequency
(a few times a day at most, never in a tight loop): Google rate-limits, and
after a limit viajante pauses Google searches on the machine for minutes.

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
| `blocked` | The provider blocked access or presented a challenge; a raw RPC status 13 is reported without assuming its cause. |
| `markup_drift` | The response could not be read in the expected page or `ds:1` format. |
| `fetch_failed` | The request failed for another reason. |
| `browser_unavailable` | A required browser dependency or installation is missing. |

Do not treat a failed search as a price or availability result. If a site
blocks a request, avoid repeated retries. Report persistent parsing problems
with the command, version, and error, after removing personal information.

Browser state and failure diagnostics live outside the checkout, under
`VIAJANTE_STATE_DIR`, `$XDG_STATE_HOME/viajante`, or
`~/.local/state/viajante`, in that order of preference. JSON is saved only
when requested, using a temporary file followed by a rename. The opt-in price
history (`price-history.jsonl`) and saved watches (`price-watches.json`) live
there too.

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

Each tool result starts with a shared envelope: `status`, `completeness`,
`empty_reason`, `retry_after`, `observed_at` (`lookup_airports` returns a plain
list). Only `empty_reason: provider_empty` means the provider found nothing;
`filtered_out` means filters removed results the provider returned, and
`not_loaded` means the search did not complete.

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

To re-check a finalist from an earlier `search_flights` result, pass its offer:

```text
recheck_offer(offer={...offer from search_flights...})
```

These are tool-call examples, not Python library calls. The MCP server uses
stdio by default (local Streamable HTTP is opt-in, see below) and runs one
search at a time. See the signatures in
[`mcp_server.py`](../src/viajante/mcp_server.py) for all arguments.

### Progress, cancellation, and deadlines

- **Progress.** If the client sends a `progressToken`, the server emits
  `notifications/progress` while a search runs: `[i/n]` lines become
  `progress=i`, `total=n`; other lines carry a message and a still-increasing
  value. `[i/n]` marks the i-th query starting, not finishing. At most about one
  notification per 250 ms; the newest held line is flushed when the search
  finishes, and progress never passes the total. A notification that cannot be
  sent never breaks the search (logged once to stderr).
  No token, no notifications.
- **Cancellation.** A client `notifications/cancelled` stops the search between
  queries, retries, and sleeps and frees the search lock, so the next call does
  not hit `a viajante search is already running`. A query already in flight at
  the provider is not interrupted; the search stops as soon as it returns. A
  cancelled search is not cached, not recorded for `verify_answer`, and writes
  no rate-limit cooldown.
- **Deadline.** `deadline_seconds` (positive, finite) on `search_flights`,
  `search_dates`, `search_flex`, `search_explore`, `search_hotels`, and
  `search_trip` bounds a call and must be a number (a string or boolean is
  rejected). `VIAJANTE_MCP_DEADLINE_SECONDS` sets a default for the MCP process,
  is validated at startup (the server exits with a clear error if it is not a
  positive number), and an explicit argument wins. On expiry the payload is
  partial: queries that finished keep their rows, the rest are errors with
  `error.code` `deadline`, `coverage.complete` is `false`, and
  `coverage.stopping_reason` is `"deadline"`. An unfinished query is not proof of
  no availability. Deadline results are not cached, and neither is any search a
  deadline cut in any way (the search control records the cut). A cut that leaves
  no failed row, such as the typical-price lookup, keeps the fare that already
  arrived with `typical` null (unknown, not cached as "no typical") and marks the
  coverage `complete: false`, `stopping_reason: "deadline"`. A deadline inside a follow-up call (for example the
  return leg of a round trip) makes that query a `deadline` row; it is never
  reported as a complete result with fewer legs. Hotel payloads carry a
  `coverage` object only when a deadline cut them. When several routes share one
  multiplexed request, responses that had already arrived are kept and only the
  rest are `deadline`; a response still in flight is not interrupted until it
  returns or the deadline cuts the wait. In the result envelope a deadline is a
  timeout: when every query was cut, `status` is `timeout`, `completeness`
  `partial`, `empty_reason` `not_loaded`, and `error_code` `deadline`; with at
  least one row of evidence the result stays `ok` / `partial`. A deadline error
  carries no `retry_after`.
- **Output.** MCP text is compact JSON (no indentation); keys are unchanged.


### MCP client compatibility

The `tools/list` and `instructions` sizes quoted in the changelog and PR were measured
over a real stdio session as the client sees them:
`ListToolsResult.model_dump_json(by_alias=True, exclude_none=True)` and
`len(initialize.instructions.encode())`. A different serializer gives different
absolute bytes (a raw compact-JSON measure read 30615 before and 33303 after) but
the same roughly 2.7 KB difference.

- **Annotations and titles.** All 22 tools carry a title and annotations
  (`readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`),
  except `watch_price`, which writes (it saves the watch and records the
  observation): `readOnlyHint: false`, `idempotentHint: false`,
  `destructiveHint: false`. `openWorldHint` is `true` for the tools that ask a
  provider (`search_flights`, `search_dates`, `search_flex`, `search_explore`,
  `search_hotels`, `search_hotel_rooms`, `search_trip`, `search_split_tickets`,
  `search_hidden_city`, `recheck_offer`, `watch_price`, `get_hotel_details`)
  and `false` for the local ones. `get_hotel_details` with `room_rates` false
  does not take the search worker. This needs `mcp>=1.14.1`.
- **Guide.** The server instructions hold only the load-bearing rules. The full
  operational guide is the `viajante://guide` resource (markdown) and the
  `get_guide` tool for clients without resource support.
- **Invalid input.** Still an `isError` result. The text is the SDK's exact
  `Error executing tool <name>: ` prefix followed by JSON:
  `{"error": {"code": "invalid_parameter", "field": "origin", "message": "..."}}`.
  Strip that prefix and parse the remainder only if it starts with `{`; any other
  error text is not from this contract. This covers handler checks and missing or
  mistyped arguments, and a top-level argument the tool does not declare (a
  misspelled filter is rejected, never ignored). `message` lists every failure as
  `name: reason`. `field` is set only when every failure is on one top-level
  parameter; otherwise it is `null`, as it is when a handler message does not name
  exactly one parameter. A concurrent search is `code: "search_in_progress"`.
  The `message` of a handler check is the same sentence earlier versions raised.
  A viajante-side failure (a decode error, or a result shape the envelope cannot read)
  is not `invalid_parameter`: its text is not JSON.
- **Rate limits.** A rate-limited search error keeps `rate_limited: true` and its
  message, and adds `retry_after` (ISO 8601 UTC) and `retry_after_seconds` (integer)
  from the recorded cooldown. Both are omitted when no cooldown was recorded, for
  example a proxied 429. The envelope's top-level `retry_after` fields repeat the
  latest of these per-error values exactly, so they are null whenever the errors carry none.
  Sweep provider failures may also include `diagnostics`: endpoint host and path,
  `http_status`, raw `rpc_status`, `request_sent`, `attempts`, and
  `cooldown_basis`. A locally active cooldown has `request_sent: false`,
  `attempts: 0`, and null HTTP/RPC status. HTTP 429 or raw RPC status 13 stops
  pending sweep work without a replay or browser fallback. Status 13 is a
  provider response; by itself it does not identify the cause or prove an IP
  block.
- **Local HTTP transport.** `viajante-mcp --transport streamable-http [--host 127.0.0.1]
  [--port 8000]` serves `http://127.0.0.1:8000/mcp`. It has no authentication and is
  not meant to be hosted. A non-loopback `--host` prints a warning: every client
  searches from this machine's IP and the machine-wide provider cooldown applies to
  all of them. Loopback binds reject a foreign `Host` (421) or `Origin` (refused) header,
  which stops DNS-rebinding from a web page. Client entry: `{"mcpServers": {"viajante": {"url": "http://127.0.0.1:8000/mcp"}}}`;
  Claude Code: `claude mcp add --transport http viajante http://127.0.0.1:8000/mcp`.

To diagnose installation drift, run `viajante --version` or call MCP
`get_runtime_info` (both available in 1.4.0+). Hotel JSON includes the executing
`viajante_version`. An npm MCP and a separately installed uv CLI can differ.
Unpinned `uvx` may reuse an old installed tool. Use `uv tool upgrade viajante`
for that installation, or refresh an explicitly published version:
`uvx --refresh --from 'viajante==VERSION' viajante --version`. Reload MCP after
upgrading; confirm the executing version before using new parameters.

## Owned segment times

Flight segments may include `arrival_date`, `departure_timezone` and
`arrival_timezone`. Missing facts remain absent; completeness reports
`segment_dates` and `segment_timezones` as known or unknown. To validate
selected rows locally:

```python
from viajante import get_flights, validate_itinerary

report = get_flights("JFK-LHR:2026-11-15", fetch="sweep")
result = report.to_dict()["queries"][0]
selected = [{"status": "ok", "query": result["query"], "offer": result["offers"][0]}]
validation = validate_itinerary(selected, {
    "arrival_deadline": "2026-11-16T12:00Z",
    "chronological": True,
})
```

Pass the selected legs in travel order. `arrival_deadline` and `chronological`
read that order. An `arrival_deadline` with an explicit offset is compared in UTC, even when
that offset differs from the arrival airport's zone. A deadline without an
offset is local civil time at the arrival airport. Missing dates or zones,
and ambiguous or nonexistent DST times, stay unknown. `chronological` checks
segment and selected-journey UTC order, including overlap. Optional
`min_stay_days` and `max_stay_days` count local date differences between a
journey arrival and the next journey departure from that same airport, including
packaged journeys. Fewer than two journeys is unknown. They do not infer a stay after the final flight.
`travel_start`/`travel_end` still bound query departure dates; `arrive_before`
and `depart_after` still test the existing local-clock bounds. Chronology
alone does not prove connection protection, immigration requirements or
sufficient time to change airports. Unknown never means compliant.

## Hotel finalist details

A hotel search stamps `selection_id` on each returned stay. Flight, calendar,
explore, and hidden-city rows do not. The id belongs to this process. A read
does not search and does not push the search out of `verify_answer`. Unknown
ids fail before any provider call.

`get_hotel_details(selection_id)` returns the stored quote. `room_rates` must
be a boolean. `room_rates` false is `ok` / `complete` and may run during a
search. `room_rates=true` asks Skiplagged for a separate USD room quote
in `room_quotes`; `original_quote` keeps the price that was searched. The
envelope on that quote follows the room quote: `ok`, `no_results` /
`provider_empty`, or the provider failure status with that error's
`retry_after` fields. The city must be one place (`Prague`, or `London, GB`).
Springfield, Portland, and Columbus match more than one place: nothing is
sent, `error_code` is `ambiguous_city`, completeness is `partial`, and
`room_rates_status` is `inconclusive`, unless the original offer's coordinates
identify one place and the returned quote is that property. A coordinate miss
is `property_mismatch` (`no_results` / `filtered_out`). Skiplagged finalists
use their own provider ids. If the provider echoes different adults, rooms, or
dates, the result is `occupancy_mismatch` or `dates_mismatch`
(`no_results` / `filtered_out`) and `room_quotes` stays empty. A missing echo
is `echo: unknown`. When `room_rates` is true and the provider does not echo
adults, rooms, and dates, a returned quote is `partial` and is not `ok` / `complete`.
Rates remain in provider order and USD. Their cancellation and occupancy
evidence does not attach to the original fare or prove combined capacity.

```python
from viajante import get_hotel_details

hotel_snapshot = get_hotel_details(hotels, 0, 0)
room_quotes = get_hotel_details(hotels, 0, 0, room_rates=True)
```

There is no details CLI command. Verify the final total and the room terms on
the provider before booking.
