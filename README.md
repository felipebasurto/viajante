# Viajante

Flight and hotel search for the terminal, Python, and AI assistants.

[![PyPI](https://img.shields.io/pypi/v/viajante.svg)](https://pypi.org/project/viajante/)
[![npm](https://img.shields.io/npm/v/viajante.svg)](https://www.npmjs.com/package/viajante)
[![Tests](https://github.com/felipebasurto/viajante/actions/workflows/test.yml/badge.svg)](https://github.com/felipebasurto/viajante/actions/workflows/test.yml)

Viajante searches Google Flights, Google Hotels, and Booking.com. Use it to
compare flights, find cheaper travel dates, explore destinations, or look up
places to stay. It runs on your computer and connects directly to the travel
sites; no API keys are required.

You can run a search from the command line, call the Python library, or connect
an AI assistant through the Model Context Protocol (MCP). Results include
prices, itinerary details, source text, and links where available.

[Usage guide](https://github.com/felipebasurto/viajante/blob/main/docs/usage.md)
· [Contributing](https://github.com/felipebasurto/viajante/blob/main/CONTRIBUTING.md)
· [Report an issue](https://github.com/felipebasurto/viajante/issues)

## Quick start

Requires **Python 3.10 or later**. Install from PyPI, or run through npx (still
needs [`uv`](https://docs.astral.sh/uv/) and Python 3.10+):

```bash
pip install viajante
npx -y viajante airports JFK
```

Search for a one-way flight or a hotel stay:

```bash
viajante flights JFK-LHR:2026-11-15 --fetch sweep
viajante hotels London 2026-11-15 2026-11-20 --currency GBP --source google
```

Use future dates when trying the examples. `JFK` and `LHR` are airport codes;
`viajante airports london` lists airports for a city so you can choose one.

These searches use HTTP and do not need a browser. Booking.com and the browser
mode for Google Flights require the optional
[browser setup](#optional-browser-support).

## Connect an AI assistant

Viajante provides a local MCP server over stdio. Requires
[`uv`](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.10+.
Add this entry to your assistant's MCP configuration:

```json
{
  "mcpServers": {
    "viajante": {
      "command": "npx",
      "args": ["-y", "viajante-mcp"]
    }
  }
}
```

Native Python (no Node):

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uvx",
      "args": ["--from", "viajante[mcp]", "viajante-mcp"]
    }
  }
}
```

This configuration supports Google Flights and Google Hotels without Chromium.
npx still needs `uvx` (and Python 3.10+) on PATH. If you use an existing
Python environment instead, install `pip install 'viajante[mcp]'` and configure
the client to run that environment's `viajante-mcp` executable.

Once connected, you can ask:

> Find a seven-night round trip from BOS to LHR, departing between November 1
> and November 30, 2026. Compare the departure dates.

> Search for hotels in Tokyo from November 12 to November 16, 2026, for two
> adults. Use JPY and require free cancellation.

The server exposes seven tools:

| Tool | Use it to |
| --- | --- |
| `search_flights` | Search one-way, round-trip, or multi-city flights. |
| `search_dates` | Compare the cheapest returned fare for each departure date in a window. |
| `search_flex` | Check dates around a target departure, then fetch flights for the cheapest day. |
| `search_explore` | Discover destinations from an origin airport and price a shortlist. |
| `search_hotels` | Find stays with total-stay prices and cancellation details where available. |
| `search_trip` | Search flights and hotels together and sum compatible results. |
| `lookup_airports` | Look up airport codes offline. |

## Use the command line

Each search command accepts `--save FILE` to write a JSON report. Run
`viajante <command> --help` for its options.

| Command | Purpose |
| --- | --- |
| `viajante flights` | Search specific routes and dates. |
| `viajante dates` | Compare departure dates across a window of up to 31 days. |
| `viajante flex` | Search a few days either side of a target date. |
| `viajante explore` | Find destinations from an origin airport. |
| `viajante hotels` | Search Google Hotels or Booking.com. |
| `viajante trip` | Search flights and a hotel stay in one request. |
| `viajante airports` | Find airport codes by city or code. |

For example, compare dates for a seven-night round trip:

```bash
viajante dates BOS-LHR --from 2026-11-01 --to 2026-11-30 --nights 7
```

See the [usage guide](https://github.com/felipebasurto/viajante/blob/main/docs/usage.md)
for round trips, flexible dates, filters, hotel searches, and saved reports.

## Use Python

`get_flights` accepts the same route format as the CLI and returns a typed
report:

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

The library also exports `FlightQuery`, `HotelQuery`, and the `search_*`
functions. The
[Python examples](https://github.com/felipebasurto/viajante/blob/main/docs/usage.md#python-library)
show how to build queries directly.

## Optional browser support

For Booking.com or Google Flights with `--fetch detail`, install Playwright
and Chromium in the environment that runs Viajante:

```bash
pip install 'viajante[browser]'
playwright install chromium
```

For the `uvx` MCP configuration, change `--from` to `viajante[mcp,browser]` and
install Chromium through that same environment:

```bash
uvx --from 'viajante[mcp,browser]' playwright install chromium
```

Flight searches default to `--fetch auto`: with Playwright installed, they use
the browser for one or two queries and HTTP for larger batches. Without
Playwright, they use HTTP. Hotel searches default to Booking.com in the CLI
and Google Hotels in MCP; use `--source google` for CLI hotel searches without
a browser.

## Understanding the results

- **Currency:** Flight searches use the origin airport's country to choose a
  currency when possible. You can set one explicitly with `--currency`.
  Standalone hotel searches require a currency. Viajante does not convert
  between currencies.
- **Baggage:** Use `--bags` or `--carry-on` to request baggage pricing from
  Google Flights. An optional `--baggage-buffer` affects ranking; it is a
  user-supplied amount, not a quoted bag fee, and defaults to zero.
- **Hotels:** Prices cover the requested stay. Free cancellation is required
  by default, but an applied search filter and a property's stated terms are
  recorded separately. Google ratings use a 0–5 scale; Booking.com uses 0–10.
- **Price comparisons:** When present, `typical` is a median from the same
  route's date calendar. It is not a historical market average.
- **Trip totals:** A combined total is shown only when both searches return
  usable prices, their dates overlap, and their currencies match. It adds the
  flight fare and hotel stay; it does not create a package booking.
- **Missing information:** Fields that cannot be determined remain unknown
  or are omitted. Source text is retained with offers to help you inspect
  the result.

Travel sites can change their pages, block requests, or return incomplete
results. Prices and availability can also change after a search. Check the
final fare, baggage allowance, hotel total, and cancellation terms on the
provider's site before booking.

## Contributing

Bug reports, documentation improvements, and code contributions are welcome.
The [contributor guide](https://github.com/felipebasurto/viajante/blob/main/CONTRIBUTING.md)
covers local setup and the offline test suite. For a bug report, include your
version, command, and error message, with personal information removed.

## License

Viajante is available under the
[MIT License](https://github.com/felipebasurto/viajante/blob/main/LICENSE).
It is an independent project and is not affiliated with Google, Booking.com,
or any airline. Use of those services is subject to their respective terms:
[Google](https://policies.google.com/terms) and
[Booking.com](https://www.booking.com/content/terms.html).

<!-- mcp-name: io.github.felipebasurto/viajante -->
