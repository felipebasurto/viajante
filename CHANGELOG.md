# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Older per-release records were folded into this file; their full text is in git
history up to commit 6ee0fb7.

## [Unreleased]

### Fixed

- `--help` examples use future dates and no longer show `--bags`, which the
  public page refuses.
- The README, usage guide, and `llms.txt` no longer say `--bags` asks Google to
  price checked bags. Only `--carry-on` rides the request.
- Four CLI tests used a departure date that is now in the past.

## [1.5.0] - 2026-10-08

### Changed

- **Breaking (flight `recommendation`, schema 2):** shortlist entries point to
  their offer by `evidence_id` and `google_flights_url`; the full `offer` is
  embedded only when it is not in `offers` (a relaxed pick). `relaxation_order`
  and `scoring` are gone, and `weights` appears only when prices could not be
  compared. Labels are role names: `top_score`, `lowest_price`, `shortest`
  (plus `_distinct` forms), `alternative`. Highlights restate returned fields
  and no longer compare ("Lowest fare among N compared" is removed). A one-way
  result with eight offers is about 23% smaller.
- **Round-trip packages:** `stops_count`, `stops`, `duration` and
  `duration_hours` now describe the whole trip (worst leg's stops, summed leg
  time), `null` when a leg is unknown. They used to describe the outbound only.
  `layover_city` and `layover_hours` are `null`; per-journey layovers are in
  `legs[].layovers`.
- **Speed:** packaged round trips read their return pages in one concurrent
  batch (about twice as fast); public-page `ds:1` extraction is about 6x faster
  on a large page; MCP `tools/list` is about 29% smaller.
- **Consent:** a declined Google consent is kept between CLI calls (only the
  `SOCS` cookie, 30 days, never an accepted consent), so a new process skips
  the consent round trip (3 requests to 1 in one live check on 2026-10-08).
- **MCP SDK 2.x:** the server runs on `MCPServer` and needs `mcp>=2.3.0`.
  Transport settings go to `run()`. A failed tool is a result with `is_error`.
- **Split tickets** take the same clock, duration, layover, via, overnight, and
  airport filters as one-way search, judged per ticket and hub connection.
- **Skiplagged:** the parser reads the shape the provider actually returns,
  checked against captured responses in `tests/fixtures/skiplagged/`.
  `hidden_city` is `null` when Skiplagged sends no `attributes`.
- Cooldown records name the viajante version and endpoint that wrote them.
- MCP responses carry one-hour freshness hints (`ttl_ms`) on the catalog,
  resources, and `server/discover`. Progress is reported when the request has a
  `progressToken`.
- Locked dependencies upgraded (`mcp` 2.3.0, `playwright` 1.63.0, `pydantic`
  2.14.0, `curl-cffi` 0.16.3, and others).
- `flights.py` and `models.py` are split into smaller modules; `viajante.models`
  still exports every name and JSON is unchanged. MCP parameters are declared
  once in `mcp_handlers.py`.

### Fixed

- **Empty vs failed:** a nonstop search that returns a results page with no
  itineraries is `no_results` / `provider_empty`, not `markup_drift`. A search
  deadline is never retried as a fetch failure.
- **Hotels:** Booking's free-cancellation filter sends `fc=2` (it sent `oos=1`).
  A failed widening page is recorded in `page_errors`. A price in another ISO
  code is not relabelled; when every card does so the query is
  `currency_mismatch`. `get_hotel_details` answers inconclusively when a room
  city cannot be proven. A negated free-cancellation phrase after the words
  ("free cancellation not available") is unknown; 1.4.7 read it as free.
- **Filters:** `min_layover` checks every connection. `no_overnight` /
  `require_overnight` `any` applies to hub splits. Mixed round-trip splits
  filter before choosing the cheapest pair. Round-trip packages keep both legs'
  carriers, so an excluded return carrier drops the package.
- **Prices and parsing:** `parse_price` reads a trailing ISO code (KWD and BHD
  were 1000x too high). Non-finite stay totals and award inputs are rejected.
  Award cents-per-point refuses taxes above the cash price. Curaçao, Sint
  Maarten, and Panama origins no longer infer a currency.
- **Dates and flex:** `search_dates` and flex `typical`, `vs_typical`, and the
  summary use every day's cheapest eligible fare before any buffer.
  `sort="price"` shows each day's cheapest fare. `explore --month` for the
  current month starts today.
- **Providers:** `Retry-After` is finite and capped at 30 minutes. Google's
  consent page is dismissed every time it appears. A timed-out batch keeps the
  pages that arrived. The detail path stops after a block. Skiplagged errors
  are rejected once, not retried; hidden-city refuses more than 9 adults.
- **MCP:** handler argument errors carry the `Error executing tool <name>:`
  prefix. `watch_price` validation no longer replays a cached search. Progress
  into a phase with a smaller total is no longer dropped.

### Removed

- The unreachable unsigned RPC transport, the legacy RPC calendar and automatic
  typical lookup, unused carrier helpers, and the never-set offer
  `cheapest_date` / `cheapest` fields.
- The sweep-to-detail fallback for empty or failed flight queries.

## [1.4.7] - 2026-10-08

### Changed

- Round-trip date sweeps (`search_dates`, and `search_flex` with a stay) are much
  faster: days run in groups of the sweep concurrency (8, or 2 with
  `VIAJANTE_SWEEP_MODE=conservative`). A 31-day window takes about 35 request
  rounds instead of 279.
- A transport error or HTTP 5xx during a date sweep is retried once instead of
  stopping the window. A 429 still stops the rest and applies the cooldown.
- MCP progress for date sweeps reports a day only after it finished.
- `@viajante/mcp` starts the server with the browser extra so `search_explore`
  works. Install Chromium once with
  `uvx --from 'viajante[mcp,browser]==<version>' playwright install chromium`.

### Added

- The MCP guide tells assistants to pass `deadline_seconds` on long sweeps.
- The missing-Chromium hint names the exact install command.

## [1.4.6] - 2026-10-07

### Changed

- `auto` and `sweep` read Google's public results page. Browser detail is
  explicit and never a fallback.
- Packaged round trips inspect at most eight outbound candidates and keep the
  provider's package total. Ordinary searches do no hidden 31-day lookup.

### Added

- `VIAJANTE_SWEEP_MODE=standard|conservative` sets sweep concurrency (8 or 2).
- Provider-block diagnostics: endpoint host and path, HTTP or RPC status, sent
  state, attempts, cooldown basis. A raw status 13 does not identify a cause.
- Public-page sweep sends `--carry-on`, `--airlines`, `--exclude-airlines`, and
  `--alliance`, and fails the read unless the page echoes each filter. Airline
  exclusion is also enforced locally.
- `--trip multi --fetch detail` for multi-city in the browser (not yet verified
  live).
- `explore` reads the catalog request Google's public Explore page makes in
  Chromium, checking its origin and date echo.

### Fixed

- Explore keeps HTTP 429 and `Retry-After`, detects status 13 in every row, stops
  pending searches after a block, and honours cancellation and deadlines.
- Airline filters match owned codes exactly (including codeshares); name aliases
  are a fallback only when codes are absent.
- Multi-city reselection rejects ambiguous or incomplete identities. Empty pages
  still require the alliance catalog echo.

### Known limitations

- The public sweep refuses checked bags, a zero carry-on, alliance exclusion, and
  multi-city before any request. Detail refuses every bag and carrier filter.
- Explore needs Chromium, one adult, economy, and often returned status 13.
- Round-trip results cover at most eight outbound candidates.

## [1.4.5] - 2026-10-07

### Added

- **MCP control:** `notifications/progress` with a client `progressToken`; real
  cancellation that stops between queries, retries, and sleeps, frees the search
  lock, and is never cached, recorded, or written as a cooldown; optional
  `deadline_seconds` (and `VIAJANTE_MCP_DEADLINE_SECONDS`) that returns a
  partial result with unfinished queries marked `deadline`, never empty.
- **Result envelope:** every MCP tool except `lookup_airports` returns one typed
  envelope (`status`, `completeness`, `empty_reason`, `error_code`,
  `retry_after`, `observed_at`), published as `outputSchema`. Only
  `provider_empty` means "none found". Rate-limit errors carry `retry_after`.
- **Split tickets (opt-in):** `viajante flights --split-tickets` and
  `search_split_tickets` build separately ticketed itineraries from real one-way
  quotes, through hubs (`--split-via`, up to 5) or as mixed one-ways for a round
  trip. Every itinerary says `split_ticket: true` and
  `connection_protected: false`. Totals are summed only in one shared currency,
  and hub timing is proven in UTC or left unproven. Searches are capped,
  sequential, and stop at a cooldown.
- **Recommendation:** flight results gain an additive `recommendation` per
  successful query: one pick plus up to three genuinely different options with
  highlights and trade-offs written only from returned fields. It respects named
  requirements, reports any it relaxes in `relaxed_requirements`, scores
  deterministically, and never price-compares across currencies.
- **Re-check:** `viajante recheck-offer` and `recheck_offer` run one fresh search
  (never the replay cache) and match by flight numbers and departure times.
  Outcomes: `same_price`, `price_changed`, `multiple_matches`,
  `incomplete_identity`, `not_found`, `substituted`, `check_failed`. A failed
  check never means the offer is gone.
- **Hotels:** `get_hotel_details` reads an offer this process returned, with an
  optional separate Skiplagged room quote.
- **Time:** segments carry `arrival_date` and IANA zones; `validate_itinerary`
  checks `arrival_deadline`, `chronological`, and stay length.
- **Metro codes** (`LON`, `NYC`, `PAR`, `TYO`, ...) on one-way and round-trip
  routes, capped at 18 queries per call.
- **Price history (opt-in):** `VIAJANTE_PRICE_HISTORY=1` logs real priced
  results; `viajante history` and `price_history` read one query in one
  currency, with no forecast or conversion. `viajante watch` and `watch_price`
  re-run a saved search on demand. Files are written under a lock and read
  strictly; an unreadable log is never treated as empty.
- **Transport and docs:** opt-in loopback Streamable HTTP
  (`--transport streamable-http`), the `viajante://guide` resource and
  `get_guide`, tool titles and annotations, a stable `invalid_parameter` error
  body, and a stdio MCP smoke test in CI.

### Changed

- MCP text results are compact JSON (a 3-route result shrinks by about a third).
- A deadline is a `timeout` in the envelope. The server instructions are shorter
  and point to the guide.
- A missing or unpriced calendar day is `not_loaded`, never `provider_empty`.
- A transport failure is `fetch_failed` (or `timeout`), no longer a 429.

### Fixed

- Re-check queries must match the original route and dates before any request.
- Cancelled responses cannot write cooldowns or enter history, evidence, or cache.
- Hotel room lookup rejects a different provider city or property; names keep
  non-Latin text and resolve aliases such as Lisboa and Ciudad de México.
- Google Hotels keeps its primary cards when an additional page fails and
  reports it in `page_errors` as partial.
- Offline tests are deterministic.

## [1.4.1] - 2026-10-05

### Added

- Hotel schema 2 adds `lodging_evidence_conflict` for explicit room vs entire-unit
  contradictions; the CLI prints the conflicting labels.

### Fixed

- A conflicting room title no longer proves an entire home; kind and property type
  stay unknown. Entire-house chips are recognized.
- Itinerary validation covers return and multi-city dates and leaves missing
  evidence unknown. Packaged filters run after the next journey is attached.
- Trip totals keep separate dated journeys; answer verification binds each amount
  to its currency.
- New detail searches respect Google's machine-wide cooldown. Skiplagged 429s
  record cooldowns.
- MCP cancellation keeps the process busy until its worker finishes.
- PyJWT is updated to 2.15.1 for the dependency audit.

### Changed

- The 15 async MCP adapters only forward parameters; signatures and results are
  unchanged. Unused parser helpers and the internal urllib opener path are
  removed.

## [1.4.0] - 2026-10-05

### Added

- Hotel `max_distance_km` / `--max-distance-km` (needs a named `near` point).
- CLI `--version` and MCP `get_runtime_info` report the executing version. Hotel
  schema 2 adds `viajante_version`, `link_context`, and `applied.url_context`.

### Fixed

- Google Hotels links use owned entity IDs and keep dates, adults, rooms, and
  currency.
- Property descriptions no longer prove the quoted unit's kind, capacity, or
  cancellation. Known party mismatches are excluded; unknown occupancy stays
  unverified.

## [1.3.1] - 2026-10-02

### Fixed

- The npm package includes `mcpName`, needed to register with the MCP Registry.
- Release publishing can resume npm and the MCP Registry from an existing tag
  without republishing PyPI.

## [1.3.0] - 2026-10-02

### Added

- `search_hotels` takes up to 8 `stays` and returns a `property_matrix` (never
  ranked; null means absent from that stay's offers, not unavailable).
- Hotel `near` (`--near LAT,LNG`) adds straight-line `distance_km`.
- Google Hotels evidence: `sleeps`, `place_types`, `class_label`, `priced_adults`,
  `resolved_place`, `place_bounds`, coordinates, and `review_count`.
- Opt-in hotel source `skiplagged` (USD only, never mixed with other sources) and
  `search_hotel_rooms` / `viajante hotel-rooms` for room rates.
- Offline `plan_stay_blocks` and `split_stay_costs`; `verify_answer`.
- Google rate-limit cooldown (2 minutes doubling to 30, or `Retry-After`) with
  `rate_limited: true`; identical MCP searches replay for 5 minutes.
- `docs/architecture.md`.

### Changed

- `--fetch auto` uses sweep for round-trip and multi-city. Explore prices every
  shortlisted destination in one round trip.
- `viajante points` is the transfer lookup only; cents-per-point is `awards`.
- Internal signatures no longer default `currency` to EUR.

### Removed

- The natural-language planner and the graded prompt battery (`bench --prompts`,
  `--holdout`, `--timeit-sweep`, `tests/prompts/`, LLM judge). `get_flights`
  rejects prose with `ValueError`.
- The looping-agent docs (`program.md`, `bench-history.md`, `docs/archive/`).

### Fixed

- A data-less status 13 is `blocked` and records the same cooldown as a 429 for
  direct sessions. Skiplagged 429 is `blocked` without retries and has its own
  cooldown file.
- Detail fetch fails fast on `google.com/sorry`.
- `search_dates` round trips use owned outbound and return pairs.
- A hidden-city `currency` that matches no card is `currency_mismatch`.
- `validate_itinerary` never reports `feasible=true` without owned provenance.

### Known limitations

- Skiplagged quotes are USD and never converted; its `occupancy_limit` does not
  prove a group fits across rooms, and it cannot filter free cancellation.
- A Google hostel price may be a dormitory bed.
- A data-less status 13 may have another cause; the guessed cooldown can pause
  Google searches for 2 minutes.

## [1.2.1] - 2026-09-11

### Added

- npm package `@viajante/mcp`: `npx -y @viajante/mcp` runs `uvx` against the
  matching PyPI version.

## [1.2.0] - 2026-09-11

### Added

- `viajante hidden-city` and `search_hidden_city` (Skiplagged, never mixed with
  Google).
- `viajante awards`, `viajante points`, `compare_awards`, `lookup_transfers`:
  local award vs cash math and a transfer table, with no live seats.

### Fixed

- Hidden-city `--top` keeps the cheapest owned fares. The Skiplagged MCP handshake
  reuses its session.

### Changed

- After a named-route `search_flights`, an agent may run `search_hidden_city`
  once, sequentially, and never when bags were named.

## [1.1.2] - 2026-09-10

### Fixed

- A second MCP search in the same process fails at once instead of queueing
  until a client timeout.
- `search_flex` parse misses are `markup_drift`; very short unknown HTML is
  `blocked`.
- `lookup_airports` maps Lisboa (LIS), Ciudad de México (MEX), and adds CTS.
- The EU consent interstitial is completed so EU IPs are not treated as blocked.

## [1.0.0] - 2026-09-09

First public release.

[Unreleased]: https://github.com/felipebasurto/viajante/compare/v1.5.0...develop
[1.5.0]: https://github.com/felipebasurto/viajante/compare/v1.4.7...v1.5.0
[1.4.7]: https://github.com/felipebasurto/viajante/compare/v1.4.6...v1.4.7
[1.4.6]: https://github.com/felipebasurto/viajante/compare/v1.4.5...v1.4.6
[1.4.5]: https://github.com/felipebasurto/viajante/compare/v1.4.1...v1.4.5
[1.4.1]: https://github.com/felipebasurto/viajante/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/felipebasurto/viajante/releases/tag/v1.4.0
[1.3.1]: https://github.com/felipebasurto/viajante/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/felipebasurto/viajante/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/felipebasurto/viajante/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/felipebasurto/viajante/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/felipebasurto/viajante/compare/v1.0.0...v1.1.2
[1.0.0]: https://github.com/felipebasurto/viajante/releases/tag/v1.0.0
