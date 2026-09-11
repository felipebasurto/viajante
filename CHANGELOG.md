# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-09-11

### Added

- `viajante hidden-city` and MCP `search_hidden_city`: Skiplagged search. Does not mix Google Flights. Named `currency` is a keep-filter of owned card ISO 4217; unnamed keeps each card's currency (not origin cash).
- `viajante awards`, `viajante points`, and MCP `compare_awards` / `lookup_transfers`: local award vs cash math and a transfer table. No live seats.

### Fixed

- Hidden-city `--top` keeps the cheapest owned fares, not Skiplagged value-sort.
- Skiplagged MCP handshake reuses the session. `#trip=~` and nested legs or ticketed-dest keys stamp `hidden_city`, `layover_city`, and `ticketed_destination` when owned. Unknown beyond cities stay omitted.

### Changed

- After a named hub or leisure-trunk `search_flights`, the agent may run `search_hidden_city` once with the same route and date. Sequential. Do not mix payloads. Skip when `bags` were named. Confirm on `booking_url`; do not scrape Skiplagged.
- After `main` moves, `uvx` can cache an old tool list. Reload MCP, `uvx --refresh`, or point the client at a local `viajante-mcp`.

## [1.1.2] - 2026-09-10

### Fixed

- A second MCP search in the same process now raises `a viajante search is already running in this process` immediately, instead of sitting in the one-worker queue until the client times out (`-32001`). `lookup_airports` still runs during a search.
- `search_flex` calendar parse misses now set `error` to `markup_drift` with empty `days`. An empty priced window is still not an error.
- Short unknown Google Flights HTML (under 200 characters of main HTML) is `blocked`. Longer unknown markup stays `markup_drift`.
- `lookup_airports` maps Lisboa to Lisbon (LIS), Ciudad de México to Mexico City (MEX), and includes New Chitose (CTS) for Sapporo.
- Sweep HTTP completes Google's EU consent interstitial so EU IPs are not treated as blocked. Captcha `/sorry/` is unchanged.

### Changed

- MCP and skill docs distinguish process-busy from `-32001`, stop on calendar `blocked`, treat optional `country` as Google `gl` (origin market, not destination ISO), and state that `max_stops` is 0, 1, or 2.

## [1.0.0] - 2026-09-09

First public release.

[1.2.0]: https://github.com/felipebasurto/viajante/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/felipebasurto/viajante/compare/v1.0.0...v1.1.2
[1.0.0]: https://github.com/felipebasurto/viajante/releases/tag/v1.0.0
