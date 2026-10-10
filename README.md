# Viajante

Flight and hotel search for your terminal, Python, and AI assistants.
Runs on your machine, no API keys.

[![PyPI](https://img.shields.io/pypi/v/viajante.svg)](https://pypi.org/project/viajante/)
[![npm](https://img.shields.io/npm/v/@viajante/mcp.svg)](https://www.npmjs.com/package/@viajante/mcp)
[![Tests](https://github.com/felipebasurto/viajante/actions/workflows/test.yml/badge.svg)](https://github.com/felipebasurto/viajante/actions/workflows/test.yml)

It reads Google Flights, Google Hotels, and Booking.com directly and returns
prices, itinerary details, and links. It reports what the provider returned
and says so when something is unknown; it never invents a fare.

[Website](https://viajante.felipebasurto.com) ·
[Usage guide](https://github.com/felipebasurto/viajante/blob/main/docs/usage.md) ·
[Architecture](https://github.com/felipebasurto/viajante/blob/main/docs/architecture.md) ·
[Changelog](https://github.com/felipebasurto/viajante/blob/main/CHANGELOG.md)

## Quick start

Requires **Python 3.10+**.

```bash
pip install viajante

viajante flights JFK-LHR:2026-11-15 --fetch sweep
viajante hotels London 2026-11-15 2026-11-20 --currency GBP --source google
```

Use future dates. Not sure of a code? `viajante airports london`.

Prefer not to install? `uvx --from viajante viajante airports JFK`, or
`npx -y -p @viajante/mcp viajante airports JFK` (both need [`uv`](https://docs.astral.sh/uv/)).

These searches use plain HTTP. Booking.com and `--fetch detail` need the
[browser extra](#optional-browser-support).

## Connect an AI assistant

Add this to your assistant's MCP configuration (needs
[`uv`](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.10+):

```json
{
  "mcpServers": {
    "viajante": {
      "command": "npx",
      "args": ["-y", "@viajante/mcp"]
    }
  }
}
```

Without Node, use `"command": "uvx"` with
`"args": ["--from", "viajante[mcp]", "viajante-mcp"]`. That covers Google
Flights and Hotels with no Chromium. The npm entry also includes the browser
extra, which `search_explore` needs.

Then ask in plain language:

> Find a seven-night round trip from BOS to LHR, departing any day in
> November 2026. Compare the dates.

> Hotels in Tokyo, November 12 to 16, two adults, free cancellation, in JPY.

Every tool result (except `lookup_airports`) carries one envelope: `status`,
`completeness`, `empty_reason`, `retry_after`, `observed_at`. Only
`empty_reason: provider_empty` means the provider found nothing. A client that
only takes a URL can use `viajante-mcp --transport streamable-http`; it has no
auth, so keep it on loopback ([details](https://github.com/felipebasurto/viajante/blob/main/docs/usage.md#mcp-client-compatibility)).

<details>
<summary>All 22 MCP tools</summary>

| Tool | What it does |
| --- | --- |
| `search_flights` | One-way, round-trip, or multi-city flights |
| `search_dates` | Cheapest fare per departure date in a window |
| `search_flex` | Dates around a target, then flights for the cheapest day |
| `search_explore` | Destinations from an origin, with a priced shortlist |
| `search_hotels` | Stays with total-stay prices and cancellation terms |
| `search_hotel_rooms` | Skiplagged room rates for one named hotel |
| `get_hotel_details` | A hotel offer from this session, plus an optional room quote |
| `search_trip` | Flights and hotel together, summed when compatible |
| `search_split_tickets` | Opt-in separately ticketed itineraries (connections not protected) |
| `search_hidden_city` | Opt-in Skiplagged hidden-city fares |
| `recheck_offer` | One fresh search to re-check an earlier offer |
| `validate_itinerary` | Check a proposed itinerary against search evidence |
| `compare_awards` | Cents-per-point math for a named award offer |
| `lookup_transfers` | Points transfer-partner table |
| `lookup_airports` | Airport and metro codes, offline |
| `plan_stay_blocks` | Group consecutive nights with the same people |
| `split_stay_costs` | Split stay totals among who slept there |
| `verify_answer` | Flag amounts, codes, dates, or links no search returned |
| `price_history` | Prices this machine recorded for a query (opt-in) |
| `watch_price` | Re-run a saved search and report the change |
| `get_runtime_info` | Installed version, offline |
| `get_guide` | Long operational guide |

</details>

## Command line

Run `viajante <command> --help` for options. Add `--save FILE` to write a JSON report.

| Command | Purpose |
| --- | --- |
| `flights` | Specific routes and dates (`--split-tickets` is opt-in) |
| `dates` | Cheapest departure day across a window of up to 31 days |
| `flex` | A few days either side of a target date |
| `explore` | Destinations from an origin airport |
| `hotels`, `hotel-rooms` | Google Hotels, Booking.com, or Skiplagged |
| `trip` | Flights and a hotel stay in one request |
| `recheck-offer` | Re-check a saved offer against a fresh search |
| `airports` | Find airport codes |
| `hidden-city`, `awards`, `points` | Skiplagged fares, award value, transfer partners |
| `history`, `watch` | Recorded prices and saved re-runs (opt-in) |

```bash
viajante dates BOS-LHR --from 2026-11-01 --to 2026-11-30 --nights 7
```

## Python

```python
from viajante import get_flights

report = get_flights("JFK-LHR:2026-11-15", fetch="sweep", top=5)

for result in report.queries:
    if result.status == "ok":
        for offer in result.offers:
            print(offer.price, report.currency, offer.airline, offer.duration)
    else:
        print(result.error)
```

`FlightQuery`, `HotelQuery`, and the `search_*` functions are exported too;
see the [Python examples](https://github.com/felipebasurto/viajante/blob/main/docs/usage.md#python-library).

## Optional browser support

```bash
pip install 'viajante[browser]'
playwright install chromium
```

For `uvx`, use `viajante[mcp,browser]` in `--from` and install Chromium from
that same environment.

## Good to know

- **Currency.** Taken from `--currency`, or from a named origin airport's
  country. Hotels always need one. Viajante never converts between currencies.
- **Baggage.** `--carry-on` asks Google to price one carry-on. Checked bags
  (`--bags`) are refused, because no transport can verify them.
  `--baggage-buffer` only affects ranking; it is not a quoted fee.
- **Hotels.** Prices are for the whole stay. Free cancellation is required
  unless you opt out. Google rates 0–5, Booking.com 0–10.
- **Recommendation.** Flight results may include a short `recommendation`
  block. It adds to the offer list and never replaces it.
- **Unknown stays unknown.** Missing fields are left out or marked unknown.

Travel sites change their pages, block requests, and update prices at any time.
Confirm the final fare, baggage, hotel total, and cancellation terms with the
provider before you book.

## Contributing

Bug reports and focused pull requests are welcome; see
[CONTRIBUTING.md](https://github.com/felipebasurto/viajante/blob/main/CONTRIBUTING.md).

## License

[MIT](https://github.com/felipebasurto/viajante/blob/main/LICENSE). Independent project, not affiliated with Google, Booking.com,
or any airline. Use of those services is subject to their terms:
[Google](https://policies.google.com/terms),
[Booking.com](https://www.booking.com/content/terms.html).

<!-- mcp-name: io.github.felipebasurto/viajante -->
