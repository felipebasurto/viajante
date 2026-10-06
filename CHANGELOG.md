# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Opt-in split tickets: `viajante flights --split-tickets` and MCP `search_split_tickets` build separately ticketed itineraries only from real one-way quotes they fetch. A one-way route is paired through hubs (named with `--split-via` / `via`, up to 5 airports, otherwise the layover airports seen in the packaged results) with a configurable minimum connection (`--split-min-connection`, default 3 hours; `--split-overnight` also searches the second ticket on the next day). A `--trip rt` route pairs the cheapest outbound one-way with the cheapest return one-way and compares them with the packaged round-trip as a whole.
- `search_split_tickets` returns the MCP result envelope (`stamp_split`): any itinerary is `ok` (`partial` when a leg failed); a failed leg or cooldown with none is that failure's status with `not_loaded` and the per-error retry fields; answered legs whose pairings were all rejected are `no_results` / `filtered_out`; only when every searched leg is provider-empty (whatever the packaged fare returned) is it `provider_empty`; `observed_at` is null when every fetch was a recorded cooldown. Leg rows carry `raw_count`, and `max_extra_searches` counts only named `via` airports that can be tried. The tool is registered with a title and read-only annotations (`openWorldHint` true), its guidance is a "Split tickets" section of the guide, and the server now has 19 tools. A cooldown recorded before a split search carries its `retry_after` fields.
- Every split itinerary is labelled `split_ticket: true`, `connection_protected: false`, and `self_transfer` (true for a hub), carries each ticket's own offer and `google_flights_url`, and a warning that a missed connection is not protected and bags may need re-checking. `total` is summed only when every part shares one owned currency and is `null` otherwise; `vs_packaged` appears only when the split total and the best packaged offer share a currency and carries a non-negative `savings` or `extra_cost` (with `direction`), so both directions are verifiable by `verify_answer`. Totals are rounded to the currency's minor unit. Nothing is split out of a round-trip price, estimated, or converted.
- Hub tickets must meet at the hub airport (ticket 1's last segment lands there, ticket 2's first segment leaves from it); anything else is rejected as `airport_mismatch` or `airport_unproven`. Mixed one-ways pair the cheapest outbound and return where the return departs after the outbound lands, rejecting `return_before_arrival`, one pair per currency (prices in different currencies never compete); a pair without an owned arrival date says `timing_proven: false` with a `timing_note`, and is counted in `rejected.timing_unproven` / `coverage.scope.timing_unproven_kept`. Ranking and `top` work within one currency at a time (other currencies and unknown totals keep at most 3 rows each; `omitted_other_currency` counts the rest), the packaged baseline is the cheapest offer in the requested currency, and the CLI exits non-zero when the split search stopped on a Google cooldown. Segments now carry an optional `arrival_date` (additive).
- Split searches are capped (at most 5 hubs, 2 queries per hub or 3 with overnight; 2 for mixed one-ways), run sequentially under the existing one-search lock, and stop at a recorded Google cooldown (`rate_limited: true`). Reports include `coverage` with the hubs tried and the reason the search stopped.
- Sweep segments carry the provider's `arrival_date` when it returns one. A hub connection needs an owned arrival and departure moment, converted to UTC with each airport's catalogue timezone so date-line and after-midnight arrivals stay correct; a missing timezone or a nonexistent or ambiguous local time around a DST change is unproven and counted under `rejected.timing_unproven` (or `timing_proven: false` for mixed one-ways) instead of being guessed. Unknown timing never sorts above proven timing.
- `via` / `--split-via` names up to 5 connection airports; an unknown code or a sixth airport is an error that names the field. Named airports are all searched (`max_hubs` defaults to the number named); unnamed, hubs still come from the packaged layovers.
- Flight results carry an additive `recommendation` block per successful query (`search_flights`, `search_trip`, and the CLI). It holds one recommended offer plus a shortlist of up to three genuinely different offers (different stop count or departure slot), labelled `recommended`, `cheapest`, `fastest`, `*_distinct`, or `alternative`. Each entry has `highlights` and `tradeoffs` written only from fields the provider returned; a missing field is worded as unknown ("Checked bag fee unknown", "Fare rules (refund, change) not shown").
- The recommendation respects the named hard requirements (`max_stops`, `depart_window`, `depart_after`, `arrive_before`, `max_duration`, `bags`, `carry_on`). When no offer meets all of them it relaxes the fewest, breaking ties in a fixed documented order, and reports them in `relaxed_requirements`; each entry carries per-requirement `met` / `unmet` / `unknown`. Round-trip and multi-city packages are not relaxed.
- Scoring is deterministic and documented: price 0.5, duration 0.35, stops 0.15, penalties relative to the best offer in the compared pool (capped at 1), unknown values take the worst penalty. Offers with different or unproven currencies are never price-compared or price-scored (`price_comparison`). Near-identical offers (same carrier, clocks, and stop count) are deduplicated to the cheaper fare. Existing `offers`, their order, and the `ranked` sort are unchanged.
- A query whose `empty_reason` is `filtered_out` can still carry a `recommendation`; the pick is then a relaxed one (see `relaxed_requirements`), not an exact match. The MCP guide and the `search_flights` description say so.
- `viajante recheck-offer` / MCP `recheck_offer` re-check an earlier Google Flights offer with one fresh search (never the 5-minute MCP replay cache). The offer is matched by itinerary identity: flight numbers plus scheduled departure times per segment, with strict rules. Exactly one outcome: `same_price`, `price_changed` (old and new amounts in the offer's own currency; a different `currency` is refused, never converted), `multiple_matches` (more than one identical fresh offer: `candidates` listed, none picked, no price verdict), `incomplete_identity` (the offer lacks flight numbers, airports or clocks: `missing` names them and no search is sent; `allow_loose_match` opts in to carrier plus times, labelled `loose_match`), `not_found` (a completed check; `reason` `provider_empty`, `filtered` or `not_among_offers`; a close alternative is listed as `closest_candidate` for information only), `substituted` (only with `allow_substitute`: closest alternative on the same end airports with a shared flight number, or the same marketing carrier within 90 minutes of the first departure, plus the provable non-empty `differences` including stops and route), or `check_failed` (`check_completed: false` with `reason` and `error`: blocked, rate limited, any other provider error, or `incomplete_offers` when fresh round-trip/multi-city offers lack a journey and nothing matched). The query is replayed (cabin, stops, bags, airline and alliance filters); a `price_cap` is not sent but reported in `filter_violations` when the matched offer breaks it (`filters_replayed` lists what rode the request, `filters_checked` what was only checked locally; `max_stops` and `exclude_airlines` violations are defensive, the search already applies them; an `airlines` allow list is satisfied by any carrier on the offer, like the search). Loose matching refuses a connecting leg without segments (`incomplete_identity`), and airports are compared during matching. A check that ran to a provider answer (a match, a substitution, or any completed `not_found`) is recorded as evidence with caller-typed values stripped; `incomplete_identity`, `check_failed` and input errors record nothing. Over MCP the result carries the shared envelope (`found` is `ok`, `not_found` is `no_results` for `provider_empty` / `filtered_out` and `ok` for `not_among_offers`, `check_failed` is its failure status with `not_loaded`, `incomplete_identity` is `failed` / `blocked`; `observed_at` is `checked_at`, which is null when nothing was sent: `incomplete_identity` or a check the Google cooldown refused). Loose matching reads the offer's `stops_count` when a single-journey leg has no `stops`. A failed check never reads as a gone offer, and the CLI exits 2 for `check_failed` and `incomplete_identity`. The check respects the machine-wide Google cooldown and the one-search process lock; the previous price, itinerary and leg times are recorded as owned evidence only when this process's ledger holds an offer with that `evidence_id`, price, currency and the same segments (`previous.source`); caller-supplied values, including `differences[].previous`, are returned but never registered; input errors name the offending field. It is not a booking guarantee; the price is confirmed only on the provider's own page.
- MCP tools carry human titles and read-only annotations (`readOnlyHint`, `destructiveHint: false`, `idempotentHint`); `openWorldHint` is true only for the tools that ask a provider.
- MCP input errors have a stable JSON body, `{"error": {"code": "invalid_parameter", "field": ..., "message": ...}}`, with `field` set only when the message names one parameter. A concurrent search is `search_in_progress`. Missing, mistyped and undeclared top-level arguments (a misspelled filter) get the same body, with `field` set only when every failure is on one parameter. Invalid input still fails the call (`isError`) and a handler's message keeps its wording.
- Rate-limited search errors add `retry_after` (ISO 8601 UTC) and `retry_after_seconds` from the recorded cooldown. They are omitted when no cooldown was recorded (a proxied 429), and the message is unchanged. The end of a cooldown is rounded up to a whole second once, so `retry_after` is never earlier than the real end and `retry_after_seconds` is derived from it. The envelope's top-level `retry_after` / `retry_after_seconds` repeat these per-error values exactly, so a proxied 429 during a direct cooldown no longer shows a top-level value.
- Opt-in local Streamable HTTP: `viajante-mcp --transport streamable-http [--host 127.0.0.1] [--port 8000]`. Stdio stays the default. No authentication and no `remotes` entry in `server.json`; a non-loopback host prints a warning, and loopback binds refuse a foreign `Host` or `Origin` header (set explicitly, because the SDK only does so itself from 1.23).
- `viajante://guide` resource (markdown) and a `get_guide` tool carry the long operational guidance.
- Every MCP tool except `lookup_airports` returns one typed envelope: `status`, `completeness`, `empty_reason` (`provider_empty`, `filtered_out`, `not_loaded`), `error_code`, `retry_after`, `observed_at` and `observed_at_basis`. It is derived from the existing counters and error codes, and is published as the tool's `outputSchema` / `structuredContent`.
- `empty_reason` on query, date, and explore rows; `timeout` on typed errors when the cause was a timeout. Both appear only when set.
- Contract: only `provider_empty` may be called "no flights/hotels found".
- `scripts/mcp-smoke.py` and a CI job that runs the stdio MCP smoke on the minimum supported SDK.

### Changed

- The MCP server instructions shrink to the load-bearing rules (the result envelope, evidence, currency, bags, empty-is-not-absent, rate limits, hidden-city sequencing) and point at the guide; the full envelope text lives in the guide. `get_guide` carries the envelope too. No rule was removed; a test checks that every sentence of the previous instructions is in the new instructions or the guide.
- The MCP extra now requires `mcp>=1.14.1,<2`. Earlier SDKs crash at startup on the server module's postponed annotations (or, on 1.6, serve no output schemas).
- A calendar day that is missing or unpriced is `not_loaded`, never `provider_empty`; a priced calendar with such gaps is `partial`.
- A sweep request that raises before any HTTP response (reset, timeout) is a transport failure: a multiplexed batch replays once on a fresh session, there is no per-query retry on top, and the result is `fetch_failed` (`timeout` when it timed out). It no longer reads as an HTTP 429 rate limit. Google Hotels multi-post searches replay once too. A calendar/explore POST that raises is `fetch_failed`, not `markup_drift`.
- An explore destination without a price (its shop failed or came back empty) is `not_loaded`, not a usable row.
- `verify_answer` reports `status: failed` when its verdict is `ok: false`.

## [1.4.1] - 2026-10-05

### Added

- Hotel schema 2 adds `lodging_evidence_conflict`, exposing explicit room/entire-unit contradictions while retaining the provider title and raw text. The CLI prints the conflicting labels as evidence.

### Fixed

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
