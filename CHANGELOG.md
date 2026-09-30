# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- MCP `verify_answer`: flags amounts, currencies, IATA codes, ISO dates, and links in a draft reply that no search in this process returned.
- Google rate-limit cooldown: a real HTTP 429 pauses Google searches on the machine (2 min doubling to 30 min); errors carry `rate_limited: true`. Identical successful MCP searches within 5 minutes return `cached: true`.
- Google Hotels offers carry owned `latitude`, `longitude`, and `review_count`. A named `min_rating` also fetches the relevance-sorted page on the same multiplexed round-trip (Tokyo, 3 nights, min 4.5: 1 eligible stay before, 6 after).
- `docs/architecture.md`: how the request path, fetch modes, hotels, and failure taxonomy fit together.

### Changed

- `--fetch auto` uses sweep for packaged round-trip and multi-city searches, since only sweep shops the return leg.
- `viajante points` is the transfer-table lookup only (mirrors MCP `lookup_transfers`). Cents-per-point math lives in `viajante awards` / `compare_awards`.
- Explore prices all shortlisted destinations on one multiplexed sweep round-trip.
- Google Hotels record detection no longer special-cases `€` (no user-visible change; non-euro totals already parsed).
- Internal cleanup: one offer-filter bundle, one JSON save path, shared CLI flag helpers (−1.5k lines, JSON unchanged).

### Removed

- The natural-language planner (`prompt_plan.py`, `plan_prompt`) and the graded prompt battery (`viajante bench --prompts`, `--holdout`, `--timeit-sweep`, `tests/prompts/`, LLM judge). The calling agent plans; viajante fetches evidence. `get_flights` now takes a route spec or trips and rejects prose with `ValueError`.

### Fixed

- A data-less wrb.fr error envelope (status 13, sent while Google throttles an IP) is `blocked`, not `markup_drift`.
- Detail fetch fails fast on `google.com/sorry` instead of waiting minutes for result cards.

- `viajante hidden-city` and MCP `search_hidden_city`: Skiplagged cards are USD. A named keep that matches no owned card currency is `currency_mismatch` (owned quote stamped), not silent `no_results`. Viajante does not convert. Omit currency or pass USD; do not advertise EUR as a Skiplagged quote.
- MCP `search_dates` round-trip calendar path: when `return_date` is set, the calendar sweep uses owned outbound+return pairs (smoke 2026-09-17 LHR→BKK, nights=14, bags=1, nearby → 31/31 cells; cheapest owned 412 GBP on 2026-11-17). See `docs/smoke-2026-09-17.md`.
- MCP `validate_itinerary` evidence-safe validator (PR #41 / issues #34–#40 on `develop`, not `main`): never reports `feasible=true` without owned provenance; incomplete evidence stays unknown/infeasible. Issues #34–#40 closed against the develop merge.

## [1.2.1] - 2026-09-11

### Added

- npm package `@viajante/mcp`: `npx -y @viajante/mcp` execs `uvx` against the matching PyPI version. Requires Python 3.10+ and uv.

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

[1.2.1]: https://github.com/felipebasurto/viajante/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/felipebasurto/viajante/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/felipebasurto/viajante/compare/v1.0.0...v1.1.2
[1.0.0]: https://github.com/felipebasurto/viajante/releases/tag/v1.0.0
