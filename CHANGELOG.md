# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.1.2]: https://github.com/felipebasurto/viajante/compare/v1.0.0...v1.1.2
[1.0.0]: https://github.com/felipebasurto/viajante/releases/tag/v1.0.0
