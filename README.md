# viajante

> **Local flight and hotel search for AI agents.**  
> Plug it into your agent via MCP — it just works. No API keys, no accounts, no subscriptions.

`viajante` (Portuguese and Spanish for *traveller*) is a local search engine for Google Flights, Google Hotels, and Booking.com. It is designed from the ground up for LLM tool calling via the **Model Context Protocol (MCP)**, as well as everyday terminal use through a rich CLI.

It provides real live prices, seat and room availability, price trend medians, and direct booking links — while guaranteeing that data is never invented or hallucinated.

---

## The Main Idea: Plug It to an Agent

Give flight and accommodation superpowers to **Cursor**, **Claude Desktop**, **Antigravity**, **OpenCode**, or any other MCP-compatible assistant.

### 1. Configure the MCP Server

No clone. No local path. Sweep flights and Google Hotels need no Chromium.

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/felipebasurto/viajante.git[mcp]",
        "viajante-mcp"
      ]
    }
  }
}
```

After the package is on PyPI, the same block can use `"viajante[mcp]"` as `--from`.

Checkout contributors can still run `uv sync --extra mcp` and point `command` at `viajante-mcp` on PATH.

For Booking.com or `--fetch detail`, install the browser extra and Chromium: `pip install 'viajante[browser]' && playwright install chromium`.

### 2. What the Agent Can Do

Once connected, your agent automatically gains 7 purpose-built tools:

| MCP Tool | Best For | What It Does |
|---|---|---|
| `search_flights` | Specific routes & dates | Searches Google Flights for one-way, round-trip, or multi-city itineraries. Returns airlines, duration, layovers, and direct Google Flights URLs. |
| `search_dates` | Finding the cheapest days | Returns a cheapest-per-day price calendar across a date window (up to 31 days). |
| `search_flex` | Flexible travel (±N days) | Checks prices across ±N days around a target departure, automatically picks the cheapest date, and shops the full details. |
| `search_explore` | Open-ended destination triage | Finds cheap destinations from an origin city/airport based on Google's explore catalog, then prices the top options. |
| `search_hotels` | Accommodations | Searches hotels with **total-stay pricing** and free-cancellation filtering (Google Hotels HTTP shortlist by default; Booking.com via Playwright). |
| `search_trip` | Complete package | Automatically pairs flights and hotels on overlapping dates and calculates the true combined trip cost. |
| `lookup_airports` | City/IATA lookup | Instant offline lookup of major commercial passenger airports for any city name or code. |

### 3. Example Agent Interactions

- *"Find me the cheapest week to fly from Boston to London in November."*  
  ➡️ Agent calls `search_dates(origin="BOS", destination="LHR", from_date="2026-11-01", to_date="2026-11-30", nights=7)`.
- *"I want to take a trip from JFK around September 15 for 5 nights, flexible by 3 days. Show me the best option."*  
  ➡️ Agent calls `search_flex(routes=["JFK-LHR:2026-09-15"], flex=3, nights=5)`.
- *"Where can I fly cheaply from Tokyo next month for a week?"*  
  ➡️ Agent calls `search_explore(origin="NRT", start_date="2026-10-01", days=7)`.
- *"Find flights from SFO to Tokyo Oct 12–19 and a highly rated hotel with free cancellation."*  
  ➡️ Agent calls `search_trip(routes=["SFO-NRT:2026-10-12:2026-10-19"], trip="rt", hotel_location="Tokyo")`.

---

## Installation

Requires Python 3.10+ and [`uv`](https://docs.astral.sh/uv/). Agents: use the `uvx` MCP block above. No Chromium for sweep or Google Hotels.

```bash
# Library + CLI (sweep, Google Hotels, dates, flex, explore)
pip install viajante
# or: uv add viajante

# MCP stdio server
pip install 'viajante[mcp]'

# Playwright detail + Booking.com only
pip install 'viajante[browser]'
playwright install chromium
```

Checkout:

```bash
git clone https://github.com/felipebasurto/viajante.git
cd viajante
uv sync
uv sync --extra mcp
# Chromium only if you need detail or Booking:
uv sync --extra browser
uv run playwright install chromium
```

> **Fast HTTP vs. Browser:**  
> Fast HTTP Sweep (`curl_cffi`) requires **no browser, no Chromium, and zero startup delay**. It handles `search_dates`, `search_flex`, `search_explore`, Google Hotels, and batch flight searches out of the box.  
> Chromium is only loaded when detailed DOM scraping or Booking.com is explicitly requested.

---

## CLI Usage

Viajante has a full terminal interface matching every agent capability.

### 1. Flights (`viajante flights`)

Search specific routes and dates:

```bash
# Quick one-way sweep
uv run viajante flights JFK-LHR:2026-10-15 --fetch sweep

# Search with nearby alternative airports (e.g. LHR, LGW, STN)
uv run viajante flights BOS-LHR:2026-10-18 --nearby --fetch sweep

# Packaged round trip with layover rules
uv run viajante flights JFK-SIN:2026-11-03:2026-11-15 --trip rt --via IST --exclude-via DXB
```

**Output:**
```text
=== JFK -> LHR  2026-10-15 (max 1 stop(s)) ===
      412 USD  above typical 340 USD (+21%)  7 hr 10 min  direct  19:30 -> 07:40     British Airways
      289 USD  below typical 340 USD (−15%)  7 hr 25 min  direct  21:15 -> 09:40     Norse Atlantic
      355 USD  near typical 340 USD (+4%)   11 hr 40 min  1 stop  16:05 -> 10:45     Icelandair

  Cheapest nonstop:  289 USD  7 hr 25 min  direct  Norse Atlantic
  Cheapest 1-stop:   355 USD  11 hr 40 min  1 stop  Icelandair
```

### 2. Dates Calendar (`viajante dates`)

Generate a cheapest-per-day price grid with sparkline visualization:

```bash
# One-way calendar across October
uv run viajante dates LAX-NRT --from 2026-10-01 --to 2026-10-31

# Packaged 7-night round trip across a departure window
uv run viajante dates BOS-LHR --from 2026-11-01 --to 2026-11-30 --nights 7
```

**Output:**
```text
=== LAX -> NRT  2026-10-01 .. 2026-10-04 ===
    Mon   Tue   Wed   Thu   Fri   Sat   Sun
                      612   588   541     ·  1-4 Oct
  █▆▁·
  min 541 USD  median 588 USD  max 612 USD  cheapest 2026-10-03  (3 priced)
  2026-10-01      612 USD
  2026-10-02      588 USD
  2026-10-03      541 USD
  2026-10-04        —
```

### 3. Flexible Travel (`viajante flex`)

Search ±N days around a target date, automatically pick the cheapest departure, and shop the winning flight:

```bash
uv run viajante flex BOS-LHR --around 2026-10-12 --flex 3 --nights 7
```

**Output:**
```text
=== BOS -> LHR  around 2026-10-12 ±3  2026-10-09 .. 2026-10-15  (rt, 7 nights) ===
  chosen 2026-10-10  return 2026-10-17
      350 USD  below typical 440 USD  7 hr direct  18:00 -> 06:00  British Airways
```

### 4. Explore Destinations (`viajante explore`)

Discover and price cheap destinations from an origin:

```bash
# Explore top destinations from JFK for a 7-day trip
uv run viajante explore JFK --from 2026-10-15 --days 7

# Explore non-Asian destinations from Tokyo
uv run viajante explore NRT --from 2026-10-15 --days 7 --exclude-regions asia
```

**Output:**
```text
=== From JFK  2026-10-15  (7-day stay; dests priced on this date) ===
      148 USD  CUN  Cancún  Mexico
      221 USD  LIS  Lisbon  Portugal
      310 USD  MAD  Madrid  Spain
```

### 5. Hotels (`viajante hotels`)

Search hotels with **total-stay pricing** (not deceptive per-night rates) and free cancellation required by default:

```bash
# Google Hotels HTTP shortlist (Fast, ratings 0–5)
uv run viajante hotels Tokyo 2026-10-12 2026-10-16 --currency JPY --source google

# Booking.com evidence scrape (Ratings 0–10, requires Chromium)
uv run viajante hotels Tokyo 2026-10-12 2026-10-16 --currency JPY --min-rating 8.5 --entire-home
```

**Output:**
```text
=== Tokyo  2026-10-12 -> 2026-10-16 (4 nights, 2 adult(s), 1 room(s)) ===
  Filters: Free cancellation required; Minimum rating 8.5
  Booking chips: oos=1
  31,200 JPY total stay  rating 8.7  Hotel Kanda  Chiyoda
    Cancellation: free
    Lodging: hotel
  40,100 JPY total stay  rating 9.1  Shimokitazawa House  Setagaya
    Cancellation: free
    Lodging: entire home
    2 bedrooms, 1 bathroom, 3 beds
  Raw cards: 40; eligible: 12; shown: 2
```

### 6. Combined Trip Total (`viajante trip`)

Join flight fare and hotel stay into a single validated package price:

```bash
uv run viajante trip SIN-MEL:2026-11-06:2026-11-10 --hotel Melbourne --trip rt --adults 2 --fetch sweep
```

Prints flight offers, hotel stays, and the exact sum total when both succeed and dates overlap.

### 7. Airport IATA Lookup (`viajante airports`)

Offline airport lookup with major commercial hubs prioritized:

```bash
uv run viajante airports tokyo
# NRT  Tokyo Narita
# HND  Tokyo Haneda

uv run viajante airports london
# LHR, LGW, STN, LCY, LTN...
```

---

## Core Guarantees & Invariants

Viajante is designed to provide reliable, verifiable ground truth to agents:

1. **No Hallucinated Data:**  
   Viajante **never** invents a fare, currency conversion, typical baseline, booking token, bag fee, or layover city. If a field was not returned by the upstream provider, it is reported as `null` or omitted.
2. **Total Stay Hotel Pricing:**  
   Hotel prices are always for the entire requested duration, including estimated taxes. Never deceptive "per-night" base rates.
3. **Free Cancellation Default:**  
   Hotels default strictly to free cancellation. Non-refundable stays are only included when `--allow-non-refundable` is explicitly requested.
4. **Predictable Currency Handling:**  
   - Flights: Currency defaults to the official currency of the origin airport's country (e.g., JFK ➔ USD, LHR ➔ GBP, NRT ➔ JPY, GRU ➔ BRL), or can be set via `--currency`.
   - Hotels: Currency is required (since cities do not have a single home country airport).
   - Viajante never converts currencies via floating exchange rates; the calling agent or user handles FX.
5. **Direct Official Booking Links:**  
   Every offer includes the direct URL to Google Flights or Booking.com, including deep itinerary tokens (`booking_token`) when available, so users can verify and purchase in one click.

---

## Architecture: Fast Sweep vs. Detail

Viajante implements two distinct fetching strategies under one unified interface:

```
                  ┌────────────────────────────────────────┐
                  │              viajante                  │
                  └───────────────────┬────────────────────┘
                                      │
                 ┌────────────────────┴───────────────────┐
                 ▼                                        ▼
    ┌───────────────────────────┐           ┌───────────────────────────┐
    │     Fast HTTP Sweep       │           │      Browser Detail       │
    │  (curl_cffi TLS session)  │           │   (Playwright Chromium)   │
    ├───────────────────────────┤           ├───────────────────────────┤
    │ • Zero browser overhead   │           │ • Full DOM parsing        │
    │ • HTTP/2 multiplexed      │           │ • Booking.com evidence    │
    │ • Blazing fast batches    │           │ • Anti-bot jitter pacing  │
    │ • Dates, Flex, Explore    │           │ • Automatic fallback      │
    └───────────────────────────┘           └───────────────────────────┘
```

- **Sweep (`--fetch sweep`)**: Uses `curl_cffi` to mimic real browser TLS fingerprints over HTTP/2. Batches 10+ dates in seconds without launching Chromium.
- **Detail (`--fetch detail`)**: Uses headless Chromium via Playwright. Respects 4.5–6s inter-query jitter delays to prevent bot blocks.
- **Auto (`--fetch auto`, default)**: Uses Sweep for 3+ queries and Detail for 1–2 queries. If Sweep ever encounters unexpected markup drift or a block, it automatically falls back to Detail.

---

## Saving JSON Reports

Pass `--save <file.json>` to write atomic, machine-readable JSON reports:

```bash
uv run viajante flights JFK-LHR:2026-10-15 --fetch sweep --save results/flight.json
```

**JSON Structure:**
```json
{
  "schema_version": 1,
  "searched_at": "2026-09-06T18:30:00Z",
  "currency": "USD",
  "locale": "en",
  "fetch_backend": "sweep",
  "fetch_ms": 1420,
  "queries": [
    {
      "status": "ok",
      "query": {
        "trip": "one-way",
        "origin": "JFK",
        "destination": "LHR",
        "departure_date": "2026-10-15",
        "max_stops": 1,
        "adults": 1,
        "cabin": "economy"
      },
      "raw_count": 24,
      "eligible_count": 8,
      "offers": [
        {
          "airline": "Norse Atlantic",
          "departure": "21:15",
          "arrival": "09:40",
          "price": 289.0,
          "duration": "7 hr 25 min",
          "stops": "Nonstop",
          "stops_count": 0,
          "typical": 340.0,
          "vs_typical": "below",
          "vs_typical_pct": -15,
          "typical_deal": "below typical 340 USD (−15%)",
          "google_flights_url": "https://www.google.com/travel/flights?..."
        }
      ],
      "stops_compare": {
        "nonstop": { "price": 289.0, "airline": "Norse Atlantic" },
        "one_stop": { "price": 355.0, "airline": "Icelandair" }
      }
    }
  ]
}
```

Error codes that agents can handle programmatically: `no_results`, `rejected`, `blocked`, `markup_drift`, `fetch_failed`, `browser_unavailable`.

---

## Python API

You can also use Viajante directly as a typed Python library:

```python
from datetime import date
from viajante import FlightQuery, search_flights, search_hotels, HotelQuery

# Search Flights
flight_report = search_flights(
    [FlightQuery(origin="JFK", destination="LHR", departure_date=date(2026, 10, 15))],
    fetch="sweep",
    top=5,
)

for query in flight_report.queries:
    if query.status == "ok":
        for offer in query.offers:
            print(f"{offer.price} {flight_report.currency} - {offer.airline} ({offer.duration})")

# Search Hotels
hotel_report = search_hotels(
    [HotelQuery(location="Tokyo", check_in=date(2026, 10, 12), check_out=date(2026, 10, 16))],
    currency="JPY",
    source="google",
    top=5,
)

for query in hotel_report.queries:
    if query.status == "ok":
        for stay in query.offers:
            print(f"{stay.total_price} {hotel_report.currency} - {stay.title} (Rating: {stay.rating})")
```

---

## CLI Reference & Flags

<details>
<summary><strong>Click to expand full CLI flags reference</strong></summary>

### Flights (`viajante flights`)

Grammar: `ORIGIN-DEST:YYYY-MM-DD` or `ORIGIN-DEST:OUT:BACK`

| Flag | Default | Description |
|---|---|---|
| `--trip` | `one-way` | Trip type: `one-way`, `rt` (packaged round-trip), or `multi`. |
| `--max-stops` | `1` | Maximum connections (`0` nonstop, `1`, or `2`). |
| `--adults` | `1` | Number of adult passengers. |
| `--cabin` | `economy` | Cabin class: `economy`, `premium-economy`, `business`, `first`. |
| `--currency` | Origin country | ISO 4217 quote currency code. |
| `--nearby` | off | Expand origin/destination to nearby same-city airports (e.g. LHR+LGW+STN). |
| `--top` | `8` | Number of ranked offers to return. |
| `--sort` | `ranked` | Sort by `ranked` (fare + baggage buffer), `fare` / `price`, `duration`, `departure`, `arrival`. |
| `--airlines` / `--exclude-airlines` | off | Comma-separated airline IATA codes (e.g. `BA,KL`). |
| `--alliance` / `--exclude-alliance` | off | Airline alliances: `star`, `skyteam`, `oneworld`. |
| `--depart-window` | off | Departure time window (e.g. `06:00-12:00` or `7-12`). |
| `--via` / `--exclude-via` | off | Required or excluded connecting airport IATA codes (e.g. `--via IST`). |
| `--no-overnight` | off | Drop itineraries with overnight connections. |
| `--price-cap` | off | Maximum acceptable fare in quote currency. |
| `--fetch` | `auto` | `sweep` (HTTP/2), `detail` (Chromium), or `auto`. |
| `--save FILE` | off | Save report to JSON file. |

### Dates Calendar (`viajante dates`)

| Flag | Default | Description |
|---|---|---|
| `--from` / `--to` | required | Inclusive departure date window (cap: 31 days). |
| `--nights` | unset | Stay duration in nights (packages a round-trip stay for each day). |
| `--max-stops` / `--cabin` | `1` / `economy` | Stop count and cabin class. |
| `--nearby` | off | Expand to nearby airports. |
| `--save FILE` | off | Save calendar to JSON file. |

### Flex (`viajante flex`)

| Flag | Default | Description |
|---|---|---|
| `--around` | required | Target departure date (`YYYY-MM-DD`). |
| `--flex` | `3` | Days before and after `--around` to check (e.g. `3` = 7-day window). |
| `--nights` | unset | Stay duration in nights for round-trip packaging. |

### Explore (`viajante explore`)

| Flag | Default | Description |
|---|---|---|
| `--from` | today | Outbound travel date. |
| `--days` | `7` | Duration of stay (stored on report). |
| `--top` | `12` | Number of destination finalists to shop and rank. |
| `--exclude-regions` | off | Exclude IANA timezone regions (e.g. `asia`, `europe`). |
| `--include-airports` | off | Whitelist specific destination IATA codes. |

### Hotels (`viajante hotels`)

| Flag | Default | Description |
|---|---|---|
| `--currency` | required | ISO 4217 quote currency (e.g. `USD`, `EUR`, `JPY`). |
| `--adults` / `--rooms` | `2` / `1` | Guest and room count. |
| `--source` | `booking` | `google` (HTTP shortlist) or `booking` (Playwright Chromium). |
| `--min-rating` | off | Minimum guest rating (Google: 0–5, Booking: 0–10). |
| `--entire-home` | off | Filter for apartments and vacation rentals. |
| `--allow-non-refundable` | off | Opt-in to show non-refundable stays (default requires free cancellation). |

</details>

---

## Testing & Quality Gate

Tests are **100% offline**, never start Chromium, and never make live network requests:

```bash
# Run test suite
uv run python -m unittest discover -s tests -v

# Run code linter and formatting checks
uv run ruff check src tests
uv run ruff format --check src tests

# Run offline benchmark suite
uv run viajante bench

# Run prompt evaluation battery (190+ test cases)
uv run viajante bench --prompts
```

---

## Disclaimer

This is an unofficial, open-source project and is not affiliated with Google, Booking.com, or any airline. Please review the [Google Terms of Service](https://policies.google.com/terms) and [Booking.com Terms](https://www.booking.com/content/terms.html) before using this tool.
