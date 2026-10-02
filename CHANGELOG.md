# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Release publishing can resume npm and MCP Registry from an existing tag without republishing PyPI. MCP Registry waits for the npm version to become publicly available, and token-free npm publishing removes setup-node's empty auth entry before using OIDC.

## [1.3.0] - 2026-10-02

### Added

- MCP `search_hotels` accepts up to 8 `stays`, each with location, dates, and optional adults and rooms. Multi-stay reports carry `property_matrix`: each property's total per stay, sorted by name, never by price. A null means it was not among that stay's returned offers, not that it is unavailable.
- Hotel `near` (`{lat, lng}` in MCP, `--near LAT,LNG` in CLI): offers with owned coordinates carry straight-line `distance_km` to the named point. No point is assumed.
- Google Hotels evidence: offer `sleeps`, `place_types`, `class_label`, and `priced_adults`; query-result `resolved_place` and `place_bounds`. Vacation-rental chips feed `details`, allowing bedrooms, beds, and lodging kind to be parsed.
- Opt-in hotel source `skiplagged` (MCP `source`, CLI `--source`): USD only, at most 10 adults and 9 rooms, no `entire_home`, never mixed with Google or Booking. Offers carry `provider_id`; `resolved_place` identifies the city actually searched when Skiplagged loosely resolves a place name.
- MCP `search_hotel_rooms` / CLI `viajante hotel-rooms`: Skiplagged room rates with provider `occupancy_limit`, `refundable`, `free_cancellation`, and `taxes_and_fees`, in provider order. Select by hotel id or exact normalized name plus city; no match or several matches returns `no_results` with returned candidates when available, never a guessed match. Room-rate requests support up to 10 adults and 5 rooms.
- Offline MCP `plan_stay_blocks` groups consecutive nights with the same people into stay blocks. `split_stay_costs` shares each stay only among its occupants by their nights, using a named currency and optional per-person nightly fee. Allocated cents sum exactly; uncovered nights are `unallocated_nights`. Neither tool fetches prices or converts currency.
- Additive hotel JSON field `applied.not_applied` names filters a source cannot apply, including Skiplagged's unsupported free-cancellation search filter. Other additive hotel fields are `provider_id`, `distance_km`, report `near` / `property_matrix`, and the Google evidence fields above; no hotel JSON keys were renamed.
- MCP `verify_answer`: flags amounts, currencies, IATA codes, ISO dates, and links in a draft reply that no search in this process returned.
- Google rate-limit cooldown: a direct HTTP 429 pauses Google searches on the machine (2 min doubling to 30 min, or a named `Retry-After`); errors carry `rate_limited: true`. Identical successful MCP searches within 5 minutes return `cached: true`.
- Google Hotels offers carry owned `latitude`, `longitude`, and `review_count`. A named `min_rating` also fetches the relevance-sorted page on the same multiplexed round-trip.
- `docs/architecture.md`: how the request path, fetch modes, hotels, and failure taxonomy fit together.

### Changed

- `--fetch auto` uses sweep for packaged round-trip and multi-city searches, since only sweep shops the return leg.
- `viajante points` is the transfer-table lookup only (mirrors MCP `lookup_transfers`). Cents-per-point math lives in `viajante awards` / `compare_awards`.
- Explore prices all shortlisted destinations on one multiplexed sweep round-trip.
- Google Hotels record detection no longer special-cases `€` (no user-visible change; non-euro totals already parsed).
- Internal signatures no longer default `currency` to EUR; every model, source, and parser takes the owned quote currency explicitly.
- Internal cleanup: one offer-filter bundle, one JSON save path, shared CLI flag helpers (−1.5k lines, JSON unchanged).

### Removed

- The natural-language planner (`prompt_plan.py`, `plan_prompt`) and the graded prompt battery (`viajante bench --prompts`, `--holdout`, `--timeit-sweep`, `tests/prompts/`, LLM judge). The calling agent plans; viajante fetches evidence. `get_flights` now takes a route spec or trips and rejects prose with `ValueError`.
- The looping-agent protocol docs (`program.md`, `bench-history.md`, `docs/archive/`). The offline `viajante bench` gate stays.

### Fixed

- A data-less Google wrb.fr status 13 envelope is `blocked` and now records the same cooldown as HTTP 429 for direct sessions only. Google Hotels reports `blocked` with `rate_limited: true`, rather than `rejected`. Shared provider cooldown state lives in `ratelimit.py`.
- Skiplagged HTTP 429 is `blocked` with `rate_limited: true`, without retries. Its separate `skiplagged-rate-limit.json` pauses new calls; real calls are paced one second apart.
- Detail fetch fails fast on `google.com/sorry` instead of waiting minutes for result cards.
- `viajante hidden-city` and MCP `search_hidden_city`: Skiplagged cards are USD. A named keep that matches no owned card currency is `currency_mismatch` (owned quote stamped), not silent `no_results`. Viajante does not convert. Omit currency or pass USD; do not advertise EUR as a Skiplagged quote.
- MCP `search_dates` round-trip calendar path: when `return_date` is set, the calendar sweep uses owned outbound+return pairs. Historical smoke evidence is in `docs/smoke-2026-09-17.md`.
- MCP `validate_itinerary`: never reports `feasible=true` without owned provenance; incomplete evidence stays unknown/infeasible.

### Known limitations

- Skiplagged quotes are USD and are never converted. A room type's `occupancy_limit` is the provider's number, not proof that a group fits across several rooms. Its search cannot apply free cancellation; confirm individual room terms.
- Google hostel prices may be dormitory beds: Google supplies no room type. Check finalists by exact name with `search_hotel_rooms` and confirm the room on the provider before treating it as private.
- A data-less status 13 may have another cause; its guessed cooldown can pause Google searches for 2 minutes even without a real rate limit.
- Google base/tax/fee breakdown (`record[6][2][44]`, unconfirmed) and caller-named exchange rates remain outside this release, pending evidence and a design decision.

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

[Unreleased]: https://github.com/felipebasurto/viajante/compare/v1.3.0...develop
[1.3.0]: https://github.com/felipebasurto/viajante/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/felipebasurto/viajante/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/felipebasurto/viajante/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/felipebasurto/viajante/compare/v1.0.0...v1.1.2
[1.0.0]: https://github.com/felipebasurto/viajante/releases/tag/v1.0.0
