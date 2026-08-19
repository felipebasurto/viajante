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

Sweep, `dates`, `explore`, and `--source google` do not start a browser.

Optional MCP extra:

```bash
uv sync --extra mcp
uv run viajante-mcp
```

Five tools. Stdio, no auth. One process lock, so two searches cannot overlap. `lookup_airports` stays unlocked. Hotel search defaults to Google on MCP; Booking stays opt-in. `viajante-mcp --help` prints the tool list.

## Flights

```bash
uv run viajante flights JFK-LHR:2026-09-15 --fetch sweep
```

```text
=== JFK -> LHR  2026-09-15 (max 1 stop(s)) ===
      412 €  7 hr 10 min  direct  19:30 -> 07:40     British Airways
      289 €      359 € ranked  7 hr 25 min  direct  21:15 -> 09:40     Norse Atlantic
      355 €  11 hr 40 min  1 stop  16:05 -> 10:45     Icelandair
```

One adult, one-way, economy. Up to eight offers, ordered by ranked total. That is fare plus a 70 EUR buffer on known low-cost carriers, and connections many times slower than the fastest nonstop (or shortest offer) are dropped so an overnight hop does not outrank a short direct. Norse is 289 € on fare. The buffer puts it behind British Airways at 359 € ranked. `--sort fare` or `--baggage-buffer 0` turns the buffer off. `--sort duration` orders by elapsed time.

Route grammar is `JFK-LHR:2026-09-15`. Several dates on one route: `JFK-LHR:2026-09-15,2026-09-16`.

`JFK-NRT:2026-10-09:2026-10-20` without `--trip` is outbound plus return as two one-way searches. `--trip rt` (alias `round-trip`) on that same grammar is one Google package. `--trip multi` takes two to six `ORIGIN-DEST:DATE` legs as one package. Multi-city detail is not supported yet.

`--fetch sweep` is one Chrome TLS session and viajante's shopping RPC. No Chromium. `--fetch detail` is the Playwright scrape. `--fetch auto` uses sweep for 3 or more queries and detail for 1 or 2. Empty or blocked sweep falls back to detail once for those legs.

A `booking_token` prints a Google Flights itinerary URL. No passenger fields, no booking POST. Nothing is written to disk unless you pass `--save`.

## Dates

```bash
uv run viajante dates LAX-NRT --from 2026-10-01 --to 2026-10-31
```

```text
=== LAX -> NRT  2026-10-01 .. 2026-10-31 ===
  2026-10-01      612 €
  2026-10-02      588 €
  2026-10-03      541 €
```

One row per day, cheapest quote (EUR). The window is at most 31 days. This uses viajante's date-grid RPC on the same TLS session as sweep. The calendar omits airline and stops when the body does not carry them. If that parse misses, each day is priced with the shopping sweep and still prints one row (`fetch_backend: sweep`). `--fetch detail` is accepted and ignored.

## Explore

```bash
uv run viajante explore JFK --from 2026-09-15 --days 7
```

```text
=== From JFK  2026-09-15  (7-day window) ===
      148 €  CUN  Cancún  Mexico
      221 €  LIS  Lisbon  Portugal
```

Destinations Google lists from that origin, then a priced `--top` shortlist (default 12) on `--from`. `--month 2026-09` uses the first of that month. `--adults`, `--cabin`, and `--max-stops` apply when pricing each destination. Do not expand this into an airport matrix.

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

## HTTP or Chromium

<p align="center">
  <img src="docs/assets/how-viajante-works.svg" alt="viajante search path: validate, then sweep HTTP or detail Chromium, then typed offers with raw card text" width="100%">
</p>

The CLI validates the route before anything starts. HTTP paths reuse one keep-alive session. Chromium paths sleep 4.5 to 6 seconds between queries on purpose. Consent cookies stay in your state directory, not in this checkout.

## Save JSON

For a cheapest-per-day grid, prefer `viajante dates`. A comma list still dumps full offer blocks when you need the cards:

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
        "cabin": "economy"
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
          "duration": "7 hr 25 min",
          "duration_hours": 7.42,
          "stops": "Nonstop",
          "stops_count": 0,
          "layover_city": null,
          "layover_hours": null,
          "flight_numbers": ["N0301"],
          "booking_token": "tok",
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

A failed query replaces `raw_count`, `eligible_count`, and `offers` with `"error": {"code": ..., "message": ...}`. Codes an agent can switch on: `no_results`, `rejected`, `blocked`, `markup_drift`, `fetch_failed`, `browser_unavailable`. Packaged `--trip rt` queries add `trip: "rt"` and `return_date`; each offer’s `legs` list has outbound then return clocks. Hotel reports use the same envelope, with `provider`, `price_basis: "total_stay"`, `fetch_backend`, `fetch_ms`, and an `applied` block for the filters that were actually sent. `flight_numbers` and `booking_token` are present when the compact shopping body has them. Otherwise they are `null`. Two-stop cards keep layovers on `legs` and leave `layover_city` empty. No booking flow. Do not invent CO2.

## CLI reference

Flight route grammar is `ORIGIN-DESTINATION:DATE[,DATE...]` with three-letter IATA codes and `YYYY-MM-DD` dates, or `ORIGIN-DESTINATION:OUT:BACK`. Without `--trip`, `OUT:BACK` is two one-way searches. `--trip rt` POSTs one packaged round-trip. Codes are case-insensitive. You can still pass a return leg as a second route.

| `flights` flag | Default | Behavior |
|---|---|---|
| `--trip` | `one-way` | `one-way`, `rt` / `round-trip`, or `multi`. `rt` and `multi` POST one package. |
| `--max-stops` | `1` | `0` direct only, `1` one stop, `2` two or fewer. |
| `--adults` | `1` | Adults on the search. |
| `--cabin` | `economy` | `economy`, `premium-economy`, `business`, or `first`. |
| `--top` | `8` | Offers kept per query after ranking and deduplication. |
| `--baggage-buffer` | `70` | EUR added to low-cost fares when ranking. `0` ranks on fare alone. |
| `--sort` | `ranked` | `ranked` uses fare+buffer for `--top` and hides very slow connections. `fare` uses cabin fare. `duration` uses elapsed time. |
| `--airlines` | off | Keep only these airline IATA codes (`BA,AA` or `JL,NH`). After parse, before `--top`. |
| `--exclude-airlines` | off | Drop these airline IATA codes (`F9,NK`). |
| `--depart-window` | off | Keep local departure hours in `START-END` inclusive (`6-20`). |
| `--fetch` | `auto` | `sweep` is HTTP. `detail` is Playwright. `auto` picks sweep for 3+ queries, detail for 1-2. |
| `--max-layover` | off | Drop connecting offers whose layover exceeds this many hours. Nonstops stay. |
| `--min-layover` | off | Drop connecting offers whose layover is shorter than this many hours. Nonstops stay. |
| `--max-duration` | off | Drop offers whose elapsed time exceeds this many hours. |
| `--save FILE` | off | Write the JSON report atomically. |

| `dates` flag | Default | Behavior |
|---|---|---|
| `--from` / `--to` | required | Inclusive departure window. Cap is 31 days. |
| `--max-stops` / `--adults` / `--cabin` | `1` / `1` / `economy` | Same meaning as `flights`. `dates` still caps stops at 1. |
| `--fetch` | `sweep` | Date-grid RPC. On a compact miss, each day is priced with shopping sweep. `detail` is ignored. |
| `--save FILE` | off | Write the calendar JSON atomically. |

| `explore` flag | Default | Behavior |
|---|---|---|
| `--from` / `--days` | date / `7` | Outbound date and trip-window label. |
| `--month` | off | First of `YYYY-MM` plus that month's length. Do not combine with `--from`. |
| `--top` | `12` | Destinations to price after the explore catalog. |
| `--max-stops` / `--adults` / `--cabin` | `1` / `1` / `economy` | Applied when pricing each destination. |
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

`search_flights(..., fetch="auto")` matches the CLI. Sweep does not start Chromium. `search_hotels(..., source="google")` is the HTTP shortlist. Booking still uses the same Chromium pacing as the CLI. `search_dates`, `search_explore`, and `lookup_airports` match the `dates`, `explore`, and `airports` commands.

## Limits

- Flights default to one-way. `--trip rt` / `round-trip` and `--trip multi` POST one package. `--max-stops` is `0`, `1`, or `2`. `--trip multi` cannot use `--fetch detail` yet. There is no flag to shorten Chromium delays or to parallelize requests.
- Quotes are requested in EUR so fares from different regions compare. That is a quote currency, not an audience. Flight cards render in English (`hl=en`). Booking cards use a fixed Spanish locale against the owned parser. Google Hotels uses English.
- Flight ranking adds a flat estimate for known low-cost carriers, not a fare quote. The low-cost list is partial. An airline missing from it is not evidence of a bag-inclusive fare. Confirm the checked bag on Google Flights before booking.
- Hotel cancellation, lodging kind, and bed counts are observed evidence. `unknown` means the card did not say. `--entire-home` therefore cannot remove every non-home. Confirm the final total and the cancellation terms on the site you book.
- Finding nothing eligible still exits `0` and prints `(no eligible offers)` or `(no eligible stays)`. Widen the filters or check the route.
- If Chromium is missing you get `browser_unavailable`. Run `uv run playwright install chromium`.
- After repeated failures, stop for 30 to 60 minutes and retry a small query set.

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

## Privacy

This tree is the public export of a private trip-planning repo. Do not commit scrapes, personal routes, or browser session files. Saved results (`results/`, `*.viajante.json`), logs, and Playwright artifacts are gitignored, and consent cookies live outside the checkout. Before pushing a fork, check `git status --short` and `git ls-files`.
