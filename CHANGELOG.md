# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.4.1] - 2026-10-05

### Added

- Compact flight segments add owned `arrival_date` and catalogue IANA `departure_timezone` / `arrival_timezone`; completeness explicitly reports segment dates and zones. Local itinerary validation supports destination-local `arrival_deadline`, UTC `chronological` ordering and intervening `min_stay_days` / `max_stay_days`. Missing or ambiguous civil times remain unknown.
- Opt-in flight `selection="pareto"` (library/MCP) and `--selection pareto` (CLI) retain diverse price/duration/stops alternatives within each query and currency. Equivalent known baggage is required for dominance; incomplete candidates remain. Selection metadata reports scope, frontier, incomplete candidates and budget truncation. Defaults stay `top`.
- Process-local MCP offer `selection_id` references and `get_flight_details` / `get_hotel_details` tools. Flight refresh matches complete segment identities before filtering/truncation, keeps old and new quotes separate and reports price changes and filter violations. Hotel room rates use the existing Skiplagged helper as separate USD evidence. Library details use the original report and query/offer indices; no details CLI is added.
- Self-transfer primitive: `viajante self-transfer ORIGIN-VIA-DESTINATION:DATE`, MCP `search_self_transfer` and library `search_self_transfer` / `join_self_transfer` shop two one-way legs through a named via and pair them on separate tickets. Each pairing reports the UTC `connection_minutes`, `same_airport`, a `status` against caller-named bounds (unnamed means no bound; unmeasured is `unknown`, never `ok`), `protected: false`, and a `total_price` only when both fares share a currency. A failed leg is kept as error evidence with no pairings.
- Cached MCP replay restores evicted finalist references while retaining the original retrieval time and ids. Ledger references and successful response caches are bounded to 20 report groups/entries.

- Hotel schema 2 adds `lodging_evidence_conflict`, exposing explicit room/entire-unit contradictions while retaining the provider title and raw text. The CLI prints the conflicting labels as evidence.

### Fixed

- Exact-name Skiplagged room lookup preserves requested rooms during name resolution and refuses an owned city echo that contradicts the requested city.

- A room title that conflicts with an entire-home unit chip no longer proves an entire home. Both lodging kind and property type stay unknown; Google property descriptions still cannot prove the priced unit. Entire-house chips are recognized alongside cottage and villa chips.
- Itinerary validation includes return and multi-city dates and leaves missing packaged journeys, segment counts, and layover evidence unknown.
- Packaged flight filters are checked after attaching the next journey and before final top selection. Unknown connection locations cannot prove airport exclusions.
- Trip totals preserve separate dated journeys and full multi-city leg identities. Answer verification binds each amount to its owned currency and cached searches refresh the evidence ledger.
- New detail searches respect Google's machine-wide cooldown. Skiplagged 429s preserve rate-limit flags and record cooldowns after session recovery.
- Explore reports expose per-query pricing failures separately from empty results, retain accurate coverage, avoid caching failed pricing, and print failures with the appropriate CLI exit status.
- MCP cancellation keeps the process busy until its worker finishes. Atomic writes use unique temporary files, and nested hotel stays reject non-integer occupancy instead of coercing it.
- The locked PyJWT dependency is updated to 2.15.1 to address the dependency audit findings.

### Changed

- Reduce all 15 async MCP adapters to parameter forwarding while preserving tool signatures, defaults, return shapes and search/lookup workers. The direct runtime tool is unchanged.
- Remove unused internal carrier and hotel parser helpers and migrate parser tests to `parse_hotels_page(...).cards`.
- Remove the redundant internal urllib/opener injection path. HTTP HTML fetches use the shared Chrome TLS client or an injected `SweepHttpClient`; the default path respects provider cooldown. The removed `opener` constructor keyword and parser wrapper were outside the supported library exports.

## [1.4.0] - 2026-10-05

### Added

- Optional hotel `max_distance_km` / `--max-distance-km`: requires a named `near` point and filters outside or unlocated offers using unrounded straight-line distance before ranking and top.
- CLI `--version` and offline MCP `get_runtime_info` expose the executing package version. Hotel schema 2 adds `viajante_version`, `max_distance_km`, offer `link_context`, and `applied.url_context`.

### Fixed

- Google Hotels navigation uses owned entity IDs instead of internal click trackers that can return empty HTTP 204 pages. Entity and search links preserve stay dates, adults, rooms and currency; malformed or missing IDs do not become guessed property links.
- General Google property descriptions no longer prove the quoted unit's lodging kind, capacity or cancellation. Explicit unit chips remain evidence; shared rooms, dorm/private mixes and negated private rooms stay unknown. Entire cottage and villa chips are recognized.
- Known priced-party mismatches and insufficient single-unit capacity are excluded. Unknown occupancy stays explicitly unverified.
- Agent contracts preserve the latest nightly roster, distinguish arithmetic estimates from replacement quotes, and require exact cancellation deadlines, dorm exclusivity and transfer checks. CLI and MCP installations are checked separately for version drift.

## [1.3.1] - 2026-10-02

### Fixed

- npm package metadata includes `mcpName`, required to register the npm transport with MCP Registry. Python and npm versions remain aligned; runtime tools and schemas are unchanged.
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

[Unreleased]: https://github.com/felipebasurto/viajante/compare/v1.3.1...develop
[1.3.1]: https://github.com/felipebasurto/viajante/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/felipebasurto/viajante/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/felipebasurto/viajante/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/felipebasurto/viajante/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/felipebasurto/viajante/compare/v1.0.0...v1.1.2
[1.0.0]: https://github.com/felipebasurto/viajante/releases/tag/v1.0.0
