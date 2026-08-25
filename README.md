<p align="center">
  <img src="docs/assets/viajante-hero.svg" alt="viajante: local flight and hotel search from any IATA pair, no API keys" width="100%">
</p>
<p align="center">
  <img src="docs/assets/viajante-flip.gif" alt="VIAJANTE split-flap wordmark flipping into place" width="80%">
</p>

viajante searches Google Flights and hotels from your machine. Any IATA pair or city. No API keys, no account. Quotes are requested in EUR so a New York–Tokyo fare and a Sydney–Auckland fare compare.

The name is Portuguese/Spanish for traveller. Shortlist a route. Do not brute-force every date and city.

Unofficial, and not affiliated with Google or Booking.com. Either site can change markup and break the parsers. Read the [Google Terms of Service](https://policies.google.com/terms), [Booking.com terms](https://www.booking.com/content/terms.html), and your own obligations before you use this.

## Install

Needs [uv](https://docs.astral.sh/uv/) and Python 3.10 or newer.

```bash
uv sync
```

Then `uv run viajante`. The lockfile is the dependency graph CI uses. `pip install -e .` works if you install Chromium yourself, and you lose the locked transitive versions.

Chromium is only for `--fetch detail` and Booking hotels:

```bash
uv run playwright install chromium
```

Sweep, `dates`, `flex`, `explore`, and `--source google` do not start a browser.

Optional MCP extra:

```bash
uv sync --extra mcp
uv run viajante-mcp
```

Seven tools on stdio, no auth: `search_flights`, `search_dates`, `search_flex`, `search_trip`, `search_explore`, `lookup_airports`, `search_hotels`. One process lock, so two searches cannot overlap. `lookup_airports` stays unlocked. Hotel search defaults to Google on MCP; Booking stays opt-in. `viajante-mcp --help` prints the tool list.

## Flights

```bash
uv run viajante flights JFK-LHR:2026-09-15 --fetch sweep
uv run viajante flights BOS-LHR:2026-09-18 --nearby --fetch sweep
uv run viajante flights JFK-SIN:2026-11-03 --via IST --exclude-via DXB --fetch sweep
```

```text
=== JFK -> LHR  2026-09-15 (max 1 stop(s)) ===
      412 €  above typical 340 € (+21%)  7 hr 10 min  direct  19:30 -> 07:40     British Airways
      289 €      359 € ranked  below typical 340 € (−15%)  7 hr 25 min  direct  21:15 -> 09:40     Norse Atlantic
      355 €  near typical 340 € (+4%)  11 hr 40 min  1 stop  16:05 -> 10:45     Icelandair
  Cheapest nonstop:  289 €  7 hr 25 min  direct  Norse Atlantic
  Cheapest 1-stop:   355 €  11 hr 40 min  1 stop  Icelandair
```

One adult, one-way, economy. Up to eight offers, ordered by ranked total. That is fare plus a 70 EUR buffer on known low-cost carriers, and connections many times slower than the fastest nonstop (or shortest offer) are dropped so an overnight hop does not outrank a short direct. Norse is 289 € on fare. The buffer puts it behind British Airways at 359 € ranked. `--sort fare` or `--sort price` or `--baggage-buffer 0` turns the buffer off. `--sort duration` orders by elapsed time. `--sort departure` / `--sort arrival` order by local clocks. `--bags N` and `--carry-on` put those counts on the shopping request so returned prices are for that bag selection. Default is unset (same prices as before). If a compact card includes checked/carry counts, they are parsed onto the offer; missing bag data stays omitted. Offers whose parsed counts contradict the request are dropped. The 70 EUR buffer is a guess used only when bag counts are still unknown.

Successful queries also print cheapest nonstop vs cheapest 1-stop from that same parsed set, using cabin fare (`price_eur`), not ranked total. A missing bucket is omitted. When only connections remain, the CLI prints `no nonstop` and still shows the 1-stop. `--save` JSON `stops_compare` is the same pair (`nonstop` / `one_stop`); MCP `search_flights` returns it on the query. No extra Google request. Never a guessed fare.

`typical_eur` is the median of owned cheapest-per-day calendar prices for that same origin-destination (up to 31 days from the queried date). Packaged `--trip rt` uses that same-stay calendar (same nights). `vs_typical` is `below`, `near` (±10%), or `above`. `vs_typical_pct` is the signed percent versus that median. `typical_deal` is the English one-liner (`below typical 340 € (−15%)`). When that median exists, `cheapest_date` / `cheapest_eur` point at the cheapest owned day in the same window. Typical fields are `null` (cheapest keys absent) when the compact calendar misses, has fewer than three priced days, or the query is multi-city. Never a guessed market average.

Route grammar is `JFK-LHR:2026-09-15`. Several dates on one route: `JFK-LHR:2026-09-15,2026-09-16`. `--nearby` expands origin or destination to owned same-city IATA (London LHR/LGW/STN/LTN/LCY, Tokyo NRT/HND) and searches each as a labeled alternative. Default off, so LHR stays LHR. Named open-jaw airports stay named (LGW stays LGW). Never invents a code.

`--via` / `--exclude-via` parse then filter on owned `layover_city` and `legs[].layovers`. Unknown layover cannot prove include (drop) or exclude (keep). Nonstops drop for `--via` and stay for `--exclude-via`. There is no shopping-POST via slot and no invented via list.

`JFK-NRT:2026-10-09:2026-10-20` without `--trip` is outbound plus return as two one-way searches. `--trip rt` (alias `round-trip`) on that same grammar is one Google package. `--trip multi` takes two to six `ORIGIN-DEST:DATE` legs as one package. Multi-city detail is not supported yet.

`--fetch sweep` is one Chrome TLS session (HTTP/2 multiplex) and viajante's shopping RPC. No Chromium. Empty/drift/5xx gets one 50 ms retry; the happy path does not sleep. HTTP 429 resets that TLS session, waits 50 ms, and continues remaining jobs on a fresh session (happy path still no sleep). A sequential remesure harness may still stop on 429. `--fetch detail` is the Playwright scrape. `--fetch auto` uses sweep for 3 or more queries and detail for 1 or 2. Empty or blocked sweep falls back to detail once for those legs.

A successful offer includes a Google Flights URL built from the owned route, dates, trip kind, cabin, occupancy, and currency. When a `booking_token` is present in the compact shopping body, the offer URL is the deeper itinerary link. The query carries the search URL. Multi-city uses the owned tfs encoder (#51); the field is omitted only when that encode cannot run and there is no token. No passenger fields, no booking POST. Nothing is written to disk unless you pass `--save`.

## Dates

```bash
uv run viajante dates LAX-NRT --from 2026-10-01 --to 2026-10-31
uv run viajante dates BOS-LHR --from 2026-11-01 --to 2026-11-30 --nights 5
```

```text
=== LAX -> NRT  2026-10-01 .. 2026-10-04 ===
    Mon   Tue   Wed   Thu   Fri   Sat   Sun
                      612   588   541     ·  1-4 Oct
  █▆▁·
  min 541 €  median 588 €  max 612 €  cheapest 2026-10-03  (3 priced)
  2026-10-01      612 €
  2026-10-02      588 €
  2026-10-03      541 €
  2026-10-04        —
```

English week lines plus a sparkline of owned daily prices. Empty days are `·` in the grid (and `—` in the row list), never a guessed fare. `min` / `median` / `max` / cheapest date are computed only from priced days and omitted when fewer than three days have a price. The window is at most 31 days. Default is one-way. `--nights 5` (or `--trip rt --nights 5`) prices a packaged stay of that length for each outbound day. `--nearby` expands origin or dest to owned same-city IATA and searches each as a labeled alternative (default off; no invented codes). `--depart-window`, `--max-layover`, `--min-layover`, and `--max-duration` post-filter shopping-sweep cards the same way as `search_flights`; compact calendar cells have no departure or layover clock and stay unfiltered. This uses viajante's date-grid RPC on the same TLS session as sweep. The calendar omits airline and stops when the body does not carry them. If that parse misses, each day is priced with the shopping sweep and still prints one row (`fetch_backend: sweep`). `--fetch detail` is accepted and ignored. The `--save` JSON `summary` block is the same numbers.

## Flex

```bash
uv run viajante flex BOS-LHR --around 2026-09-12 --flex 3 --nights 7
```

```text
=== BOS -> LHR  around 2026-09-12 ±3  2026-09-09 .. 2026-09-15  (rt, 7 nights) ===
  chosen 2026-09-10  return 2026-09-17
      350 €  below typical 440 €  7 hr         direct           18:00 -> 06:00     British Airways
```

`--around` plus `--flex N` is the inclusive window (at most 31 days). The owned date-grid RPC finds the cheapest legal departure in that window, then one shopping POST prices that day. `--nights N` (or `--trip rt --nights N`) packages the stay. `--nearby` expands origin or dest to owned same-city IATA and searches each as a labeled alternative (default off; no invented codes). `--depart-window`, `--max-layover`, `--min-layover`, and `--max-duration` post-filter that winning-day shop the same way as `search_flights`; the compact grid still has no clock. A calendar miss or a window with no priced day is empty: no per-day shopping sweep, no invented fare. `typical_eur` is the median of priced days in that same grid when there are at least three; `vs_typical` compares the shopping fare to that median. Fetch locale stays English.

## Explore

```bash
uv run viajante explore JFK --from 2026-09-15 --days 7
```

```text
=== From JFK  2026-09-15  (7-day window) ===
      148 €  CUN  Cancún  Mexico
      221 €  LIS  Lisbon  Portugal
```

Destinations Google lists from that origin, then a priced `--top` shortlist (default 12) on `--from`. `--month 2026-09` uses the first of that month. `--adults`, `--children`, `--infants-in-seat`, `--infants-on-lap`, `--cabin`, and `--max-stops` apply when pricing each destination (occupancy also rides the explore catalog RPC). Unnamed occupancy stays 0. `--nearby` expands the origin to owned same-city IATA and searches each as a labeled alternative (default off; no invented codes). `--depart-window`, `--max-layover`, `--min-layover`, and `--max-duration` post-filter dest shop cards; a dest with no surviving fare is dropped. Compact catalog places have no layover clock. Do not expand this into an airport matrix of destinations.

## Airports

```bash
uv run viajante airports tokyo
uv run viajante airports london
uv run viajante airports JFK
```

Offline IATA search. City queries rank major passenger airports first (`london` is LHR/LGW/STN/LCY/LTN before Biggin Hill). `FlightQuery` rejects unknown codes. `XXX` is not an airport.

## Hotels

```bash
uv run viajante hotels Tokyo 2026-10-12 2026-10-16 --min-rating 8.5
```

```text
=== Tokyo  2026-10-12 -> 2026-10-16 (4 nights, 2 adult(s), 1 room(s)) ===
  Filters: Free cancellation required; Minimum rating 8.5
  Booking chips: oos=1
  312 € total stay  rating 8.7  Hotel Kanda  Chiyoda
    Cancellation: free
    Lodging: hotel
  401 € total stay  rating 9.1  Shimokitazawa House  Setagaya
    Cancellation: free
    Lodging: entire home
    2 bedrooms, 1 bathroom, 3 beds
  Raw cards: 40; eligible: 12; shown: 2
```

Prices are totals for the whole stay, not per night. Free cancellation is required unless you pass `--allow-non-refundable`. `--compare-cancellation` runs Booking twice, free-cancellation chip on then off, and prints a joined table. Do not combine it with `--allow-non-refundable` or `--source google`.

`--source google` is the HTTP shortlist (ratings 0–5). Stay totals include tax. `--min-rating` tops out at 5 on that source. Booking is the CLI default evidence path (ratings 0–10). MCP hotel search defaults to Google.

The output separates what you asked for (`Filters`), what the site was told, and what the card showed. Those three can disagree. `--min-rating` is a local filter. Only free cancellation and `--entire-home` are pushed to the provider. Do not treat a silent cancellation card as `free`. Non-property titles such as `closed` are dropped.

## Trip total

```bash
uv run viajante trip SIN-MEL:2026-11-06:2026-11-10 --hotel Melbourne --trip rt --adults 2 --fetch sweep
```

When a search names flights and a hotel on overlapping dates, viajante prints the owned cabin fare, the owned hotel stay, and their sum. Hotel `price_basis` stays `total_stay`. If either side misses (empty offers, fetch error, dates that do not overlap), the sum is omitted — never invented. `--adults` applies to both searches. Hotel check-in/out default to the earliest and latest flight dates when the route has two dates; override with `--check-in` / `--check-out`. Flight shop uses the same owned `--bags` / `--via` / `--airlines` / `--price-cap` post-filters as `viajante flights`; unnamed stays unset. `--nearby` expands origin or dest to owned same-city IATA (default off; named open-jaw stays; no invented codes). Nearby alternatives contribute the cheapest owned fare in that city group, not a sum of every airport. MCP `search_trip` is the same join (Google hotels by default). `--save` writes both nested reports plus `trip_total` only when both sides hit.

## HTTP or Chromium

<p align="center">
  <img src="docs/assets/how-viajante-works.svg" alt="viajante search path: validate, then sweep HTTP or detail Chromium, then typed offers with raw card text" width="100%">
</p>

The CLI validates the route before anything starts. HTTP sweep reuses one Chrome TLS session with HTTP/2 multiplex. Empty/drift/5xx retries once after 50 ms; HTTP 429 resets TLS, waits 50 ms, and continues remaining jobs on a fresh session. Happy path does not sleep. Chromium paths sleep 4.5 to 6 seconds between queries on purpose. Consent cookies stay in your state directory, not in this checkout.

## Save JSON

For a cheapest-per-day grid, prefer `viajante dates`. For around a date ±N, prefer `viajante flex` (calendar, then one shopping search). A comma list still dumps full offer blocks when you need the cards:

```bash
uv run viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14 --save results/dates.viajante.json
uv run viajante flights \
  JFK-LHR:2026-09-15,2026-09-16,2026-09-17 \
  --max-stops 0 \
  --top 5 \
  --save results/search.viajante.json
```

Each `flights` date is searched sequentially and printed as its own block. Progress goes to stderr so you can pipe the table on its own. A 10-date sweep is a few seconds of HTTP. The same batch on `--fetch detail` still sleeps 4.5 to 6 seconds between queries.

`--save` writes one report per run. Every offer keeps the scraped text beside the parsed number.

```json
{
  "schema_version": 1,
  "searched_at": "2026-08-11T10:32:00Z",
  "currency": "EUR",
  "locale": "en",
  "fetch_backend": "sweep",
  "fetch_ms": 2410,
  "queries": [
    {
      "status": "ok",
      "query": {
        "trip": "one-way",
        "origin": "JFK",
        "destination": "LHR",
        "departure_date": "2026-09-15",
        "max_stops": 1,
        "adults": 1,
        "cabin": "economy",
        "google_flights_url": "https://www.google.com/travel/flights?tfs=...&hl=en&tfu=EgQIABABIgA&curr=EUR"
      },
      "raw_count": 24,
      "eligible_count": 1,
      "offers": [
        {
          "airline": "Norse Atlantic",
          "departure": "21:15",
          "arrival": "09:40",
          "price": "€289",
          "price_eur": 289.0,
          "typical_eur": 340.0,
          "vs_typical": "below",
          "vs_typical_pct": -15,
          "typical_deal": "below typical 340 € (−15%)",
          "cheapest_date": "2026-09-16",
          "cheapest_eur": 300.0,
          "duration": "7 hr 25 min",
          "duration_hours": 7.42,
          "stops": "Nonstop",
          "stops_count": 0,
          "layover_city": null,
          "layover_hours": null,
          "flight_numbers": ["N0301"],
          "booking_token": "tok",
          "google_flights_url": "https://www.google.com/travel/flights?hl=en&curr=EUR&booking_token=tok",
          "baggage_buffer_eur": 70,
          "needs_bag_verify": true,
          "legs": [
            {
              "departure": "21:15",
              "arrival": "09:40",
              "duration": "7 hr 25 min",
              "stops": "Nonstop",
              "segments": [],
              "layovers": []
            }
          ]
        }
      ]
    }
  ]
}
```

A failed query replaces `raw_count`, `eligible_count`, and `offers` with `"error": {"code": ..., "message": ...}`. Codes an agent can switch on: `no_results`, `rejected`, `blocked`, `markup_drift`, `fetch_failed`, `browser_unavailable`. Packaged `--trip rt` queries add `trip: "rt"` and `return_date`; each offer’s `legs` list has outbound then return clocks. `typical_eur` / `vs_typical` / `vs_typical_pct` / `typical_deal` are filled from that same-route date-grid median when it exists (packaged RT uses the same-stay grid). Otherwise they are `null`. `cheapest_date` / `cheapest_eur` appear only when that median exists and the cheapest owned day is in the grid; they are omitted, not invented, when the grid missed. `stops_compare` is an extra success key with `nonstop` and/or `one_stop` sides from the eligible parsed set (cabin fare). Omit a side when that bucket is empty; omit the whole block when both are empty. Hotel reports use the same envelope, with `provider`, `price_basis: "total_stay"`, `fetch_backend`, `fetch_ms`, and an `applied` block for the filters that were actually sent. `flight_numbers` and `booking_token` are present when the compact shopping body has them. Otherwise they are `null`. `google_flights_url` is on the query and each offer when viajante can build it from owned route/date/cabin/occupancy/currency bytes, or from an owned `booking_token`. Multi-city uses the owned tfs encoder; the field is omitted only when that encode cannot run and there is no token. `checked_bags` / `carry_on` appear on an offer only when those counts were in the compact bytes; they are omitted, not invented, when the card is silent. Query `bags` / `carry_on` appear only when the caller requested them. Query `children` / `infants_in_seat` / `infants_on_lap` appear only when those counts are non-zero. Two-stop cards keep layovers on `legs` and leave `layover_city` empty. No booking flow. Do not invent CO2.

## CLI reference

Flight route grammar is `ORIGIN-DESTINATION:DATE[,DATE...]` with three-letter IATA codes and `YYYY-MM-DD` dates, or `ORIGIN-DESTINATION:OUT:BACK`. Without `--trip`, `OUT:BACK` is two one-way searches. `--trip rt` POSTs one packaged round-trip. Codes are case-insensitive. You can still pass a return leg as a second route.

| `flights` flag | Default | Behavior |
|---|---|---|
| `--trip` | `one-way` | `one-way`, `rt` / `round-trip`, or `multi`. `rt` and `multi` POST one package. |
| `--max-stops` | `1` | `0` direct only, `1` one stop, `2` two or fewer. |
| `--adults` | `1` | Adults on the search. |
| `--children` | `0` | Children aged 2–11. Omitted from JSON while 0. |
| `--infants-in-seat` | `0` | Infants with their own seat. Omitted from JSON while 0. |
| `--infants-on-lap` | `0` | Infants on lap (cannot exceed `--adults`). Omitted from JSON while 0. |
| `--cabin` | `economy` | `economy`, `premium-economy`, `business`, or `first`. |
| `--currency` | `EUR` | ISO 4217 code sent as Google `curr`. |
| `--country` | unset | ISO country sent as Google `gl`. Omitted when unset; not a home-hub default. |
| `--bags` | unset | Checked bags on the shopping request. Omit to leave the slot empty. |
| `--carry-on` | unset | Ask the shopping request for one carry-on. Omit to leave the slot empty. |
| `--nearby` | off | Expand origin or dest to owned same-city IATA and search each as a labeled alternative. Default off. Named open-jaw airports stay. Never invents a code. |
| `--top` | `8` | Offers kept per query after ranking and deduplication. |
| `--baggage-buffer` | `70` | EUR added to low-cost fares when ranking. `0` ranks on fare alone. |
| `--sort` | `ranked` | `ranked` uses fare+buffer for `--top` and hides very slow connections. `fare` / `price` use cabin fare. `duration` uses elapsed time. `departure` / `arrival` use local clocks. |
| `--airlines` | off | Restrict the shopping request to these airline IATA codes (`BA,KL`). Detail still post-filters parsed cards. |
| `--exclude-airlines` | off | Exclude these airline IATA codes from the shopping request (`DL`). |
| `--alliance` | off | Restrict the shopping request to these alliances (`oneworld`, `skyteam`, `star`). Uses IATA designators, not a member list. |
| `--exclude-alliance` | off | Exclude these alliances from the shopping request. |
| `--depart-window` | off | Keep local departures in `START-END` inclusive. Hours (`6-20`) keep the whole end hour. Clocks (`06:00-20:00`) are exact. |
| `--fetch` | `auto` | `sweep` is HTTP/2 on one Chrome TLS session. `detail` is Playwright. `auto` picks sweep for 3+ queries, detail for 1-2. |
| `--max-layover` | off | Drop connecting offers whose layover exceeds this many hours. Nonstops stay. |
| `--min-layover` | off | Drop connecting offers whose layover is shorter than this many hours. Nonstops stay. |
| `--via` | off | Keep connecting offers whose parsed layover matches these IATA codes (`IST`). Post-filter on owned `layover_city` / `legs[].layovers`. Unknown layover cannot prove include (drop). Nonstops drop. |
| `--exclude-via` | off | Drop connecting offers whose parsed layover matches these IATA codes (`DXB`). Unknown layover stays. Nonstops stay. |
| `--max-duration` | off | Drop offers whose elapsed time exceeds this many hours. |
| `--price-cap` | unset | Drop owned fares above this EUR amount. Inclusive. Unnamed stays unset. Index 7 on the shopping POST stays `None` (RPC layout unknown). Never invents a cap or a fare. |
| `--save FILE` | off | Write the JSON report atomically. |

| `dates` flag | Default | Behavior |
|---|---|---|
| `--from` / `--to` | required | Inclusive departure window. Cap is 31 days. |
| `--trip` / `--nights` | `one-way` / unset | One-way cheapest-per-day. `--nights N` (or `--trip rt --nights N`) is one packaged stay per departure day. `multi` is not supported. |
| `--max-stops` / `--adults` / `--children` / `--infants-in-seat` / `--infants-on-lap` / `--cabin` | `1` / `1` / `0` / `0` / `0` / `economy` | Same meaning as `flights` (`0`, `1`, or `2` stops). Occupancy rides `calendar_trip` and the shopping POST. Unnamed stays 0. |
| `--fetch` | `sweep` | Date-grid RPC. On a compact miss, each day is priced with shopping sweep. `detail` is ignored. |
| `--nearby` | off | Expand origin or dest to owned same-city IATA and search each as a labeled alternative. Default off. Never invents a code. |
| `--bags` / `--carry-on` / `--airlines` / `--exclude-airlines` / `--alliance` / `--exclude-alliance` / `--via` / `--exclude-via` / `--depart-window` / `--max-layover` / `--min-layover` / `--max-duration` | unset | Same owned shop post-filters as `flights`. Alliances ride the shopping POST (no owned member list). Applied to sweep-fallback cards only. Compact calendar cells have no clock and no airline/alliance and stay unfiltered. Unnamed stays unset. |
| `--save FILE` | off | Write the calendar JSON atomically. |

| `explore` flag | Default | Behavior |
|---|---|---|
| `--from` / `--days` | date / `7` | Outbound date and trip-window label. |
| `--month` | off | First of `YYYY-MM` plus that month's length. Do not combine with `--from`. |
| `--top` | `12` | Destinations to price after the explore catalog. |
| `--max-stops` / `--adults` / `--children` / `--infants-in-seat` / `--infants-on-lap` / `--cabin` | `1` / `1` / `0` / `0` / `0` / `economy` | Applied when pricing each destination. Occupancy rides the explore catalog RPC and dest shopping POSTs. Unnamed stays 0. |
| `--bags` / `--carry-on` / `--airlines` / `--exclude-airlines` / `--alliance` / `--exclude-alliance` / `--via` / `--exclude-via` | unset | Same owned shop post-filters as `flights` / dates-flex. Alliances ride dest shopping POSTs (no owned member list). Unnamed stays unset. Destinations whose cheapest surviving offer contradicts are dropped. Compact catalog places are not post-filtered. Do not invent dests to fill `--top`. |
| `--depart-window` / `--max-layover` / `--min-layover` / `--max-duration` | unset | Same owned shop post-filters as `flights`. Applied to dest shop cards. Unknown clock or layover cannot prove the filter. Nonstops stay for max/min layover. |
| `--price-cap` | unset | Drop destinations whose cheapest surviving owned fare exceeds this EUR amount. Unknown price cannot prove the cap. |
| `--nearby` | off | Expand origin to owned same-city IATA and search each as a labeled alternative. Default off. Never invents a code. |
| `--save FILE` | off | Write the explore JSON atomically. |

| `hotels` flag | Default | Behavior |
|---|---|---|
| `--adults` / `--rooms` | `2` / `1` | Occupancy for the stay. |
| `--top` | `8` | Stays shown after filtering and ranking. |
| `--source` | `booking` | `booking` is Playwright evidence (CLI default). `google` is the HTTP shortlist (MCP default). |
| `--min-rating` | off | Booking 0–10. Google Hotels 0–5. |
| `--entire-home` | off | Require entire homes. Cards with unknown property type may remain. |
| `--allow-non-refundable` | off | Include stays without free cancellation. |
| `--compare-cancellation` | off | Two sequential Booking searches and a joined price table. |
| `--save FILE` | off | Write the JSON report atomically. |

| Exit code | Meaning |
|---|---|
| `0` | Every query finished without a fetch failure, including queries that found nothing eligible. |
| `1` | The command input is invalid. |
| `2` | Every query failed. |
| `3` | Some queries finished and some failed. |

## Python

```python
from datetime import date

from viajante import FlightQuery, search_flights

report = search_flights(
    [
        FlightQuery(
            origin="JFK",
            destination="LHR",
            departure_date=date(2026, 9, 15),
            max_stops=1,
        )
    ],
    top=5,
    fetch="sweep",
)

for result in report.queries:
    if result.status == "ok":
        for offer in result.offers:
            print(offer.price_eur, offer.airline)
```

Hotels use the same report pattern:

```python
from datetime import date

from viajante import HotelQuery, search_hotels

report = search_hotels(
    [
        HotelQuery(
            location="Tokyo",
            check_in=date(2026, 10, 12),
            check_out=date(2026, 10, 16),
        )
    ],
    top=5,
)

for result in report.queries:
    if result.status == "ok":
        for offer in result.offers:
            print(offer.total_price_eur, offer.title)
```

`search_flights(..., fetch="auto")` matches the CLI. Sweep does not start Chromium. `search_hotels(..., source="google")` is the HTTP shortlist. Booking still uses the same Chromium pacing as the CLI. `search_dates(..., trip="one-way")` is the cheapest-per-day grid with a `summary` block when three or more days are priced; pass `nights` (implies `trip="rt"`) for a packaged stay. `search_flex(..., around=..., flex=3)` is that grid plus one shopping search on the cheapest legal day. Named `children` / `infants_in_seat` / `infants_on_lap` ride those calendar/shop POSTs the same way as `search_flights` (unnamed stays 0). Named `alliance` / `exclude_alliance` ride those shopping POSTs the same way as `search_flights`. `search_trip` joins owned flight fare and hotel stay when dates overlap; flight shop takes the same bags/via/airlines/alliance/`price_cap` filters as `search_flights`. `search_explore` and `lookup_airports` match the `explore` and `airports` commands (explore occupancy rides catalog + dest shops).

## Limits

- Flights default to one-way. `--trip rt` / `round-trip` and `--trip multi` POST one package. `--max-stops` is `0`, `1`, or `2`. `--trip multi` cannot use `--fetch detail` yet. There is no flag to shorten Chromium delays or to parallelize requests. Sweep HTTP/2 multiplexes on one TLS session. HTTP 429 resets TLS and continues remaining jobs; a sequential remesure harness may still stop on 429.
- Quotes are requested in EUR so fares from different regions compare. That is a quote currency, not an audience. Flight and hotel cards render in English (`hl=en` / `lang=en`, `locale=en-US`). Planner prompts may be any language; fetch queries stay English.
- Flight ranking adds a flat estimate for known low-cost carriers, not a fare quote. The low-cost list is partial. An airline missing from it is not evidence of a bag-inclusive fare. Confirm the checked bag on Google Flights before booking.
- Hotel cancellation, lodging kind, and bed counts are observed evidence. `unknown` means the card did not say. `--entire-home` therefore cannot remove every non-home. Confirm the final total and the cancellation terms on the site you book.
- Finding nothing eligible still exits `0` and prints `(no eligible offers)` or `(no eligible stays)`. Widen the filters or check the route.
- If Chromium is missing you get `browser_unavailable`. Run `uv run playwright install chromium`.
- After repeated failures, stop for 30 to 60 minutes and retry a small query set. Never invent a fare, a typical median, a booking token, or a via list.

## Browser state

Playwright consent cookies persist as `pw_state_google.json` and `pw_state_booking.json` at the first of these that applies:

1. `$VIAJANTE_STATE_DIR/`
2. `$XDG_STATE_HOME/viajante/`
3. `~/.local/state/viajante/`

Delete the affected provider file if consent or scraping breaks. It is recreated on the next run.

## Tests

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
```

Fully offline. They never launch Chromium and never touch the network. `tests/test_google_flights.py` pins query encoding and drives synthetic compact shopping bodies plus HTML markup through the owned parsers. CI runs the suite on Python 3.10 through 3.14.

## Bench

```bash
uv run viajante bench
```

Offline keep-or-revert score for a looping agent. Unittest + ruff must pass (`gate: ok`); `score_ms` is unittest wall time plus the owned `tests/bench/` compact-shopping / card-parse corpus. Lower is better. `program.md` is the experiment protocol. `bench-baseline.json` holds the last human-merged win. No live Google unless `VIAJANTE_BENCH_LIVE=1`, and that extra is never the score.

The graded prompt battery is a separate quality contract, not `score_ms`:

```bash
uv run viajante bench --prompts
```

Graded battery (smoke → savage, includes `i18n.jsonl` on brutal). #62 counted **186**. Smoke→insane stay English; savage and i18n add multilingual hardness while the plan still emits English IATA and English flight fetch locale. International origins. No implied home hub. Deterministic cases stay offline (owned prompt→query planner vs IATA / route grammar / trip kind / occupancy / alliance / depart-window). Around/±N plans `intent=flex`; cheapest week plans `intent=dates`; packaged RT stays. Occupancy is omitted when the prompt has no count. Alliance codes are not invented. Each row prints `plan_ms`; the summary prints `plan_p50_ms` / `plan_p90_ms` / `plan_max_ms`. That is not `score_ms` and not `judge_mean`. `VIAJANTE_BENCH_JUDGE=1` runs a DeepSeek 1–100 quality score (`DEEPSEEK_API_KEY` or `VIAJANTE_JUDGE_KEY`, model `deepseek-chat`) on the open-ended rows; without a key those print `judge: skip` and invent no score. Nested or broken judge JSON is recovered when `score` and `reason` are present (`JUDGE_SYSTEM_PROMPT` is untouched). Optional live find-flights timer: `VIAJANTE_BENCH_SWEEP=1` or `viajante bench --prompts --timeit-sweep` (first 8 planned IATA+date flights, HTTP sweep, no Playwright). Off by default; skipped print is `sweep_ms:` blank. Live Google is never the keep metric. Do not delete `tests/prompts/` to “win” the speed loop. Holdout (`viajante bench --prompts --holdout`) is not in that weekday battery.

Keep baseline remains **97.9** (`60d9ed4`, 26 scored). Last judged **95.7** on `22da78a` (35 scored, 23 skip) is not a keep versus 97.9 on `4faaabd`. Gate is suite + `fail:0`. `score_ms` is not the product trophy.

## Privacy

This tree is the public export of a private trip-planning repo. Do not commit scrapes, personal routes, or browser session files. Saved results (`results/`, `*.viajante.json`), logs, and Playwright artifacts are gitignored, and consent cookies live outside the checkout. Before pushing a fork, check `git status --short` and `git ls-files`.
