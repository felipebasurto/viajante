# 1.4.1 backlog

Recorded on 2026-10-05 against released 1.4.0 (`dcab9fc`). These are planned
changes, not implemented fixes or a publication decision. Version stamps stay
at 1.4.0 until a release candidate is prepared.

## Priority: contradictory hotel unit evidence

A live check of the published MCP found a property whose title explicitly
described a private room, while its Google unit chip said `Entire cottage`.
The offer carried `lodging_kind: entire_home` and
`property_type_evidence: entire_home`, without exposing the contradiction.
The chip proves what Google returned, not that the conflicting unit is an
entire home.

Relevant seams:

- `src/viajante/google_hotels_rpc.py`: provider title and `unit_details`.
- `src/viajante/hotels.py`: `_normalize_card` isolates Google unit evidence
  and currently omits the title when calling `parse_lodging_kind`.
- `src/viajante/parsers.py`: property-type and lodging-kind evidence parsing.
- `tests/test_hotel_evidence_context.py`: synthetic evidence regressions.

Acceptance criteria:

- [ ] Detect explicit private/shared-room evidence that contradicts an
  entire-home unit chip; ambiguous evidence must not become verified
  `entire_home`.
- [ ] Preserve the original title and raw evidence for the calling agent.
  Any conflict field must be additive and factual, without recommendation
  prose. Decide its exact shape during implementation.
- [ ] Cover synthetic private-room/entire-cottage and private-room/entire-house
  contradictions, plus unambiguous entire-home units.
- [ ] Preserve description isolation: general property prose must not prove
  the priced unit, cancellation, capacity or room privacy.
- [ ] Keep unknown candidates explicitly unverified; requesting
  `entire_home` alone never proves every returned candidate satisfies it.
- [ ] Check both the JSON evidence and how the CLI displays an ambiguous unit.

Use synthetic fixtures such as `Example Private Room`; do not commit the live
property record, prices, personal itinerary or HTTP captures.

## Separate cleanup work

The supplied audit was checked against current source. All four proposals
are recorded below; estimated line savings are not acceptance criteria.

### 1. Reduce MCP argument forwarding

There are 15 async tool adapters, plus the direct synchronous
`get_runtime_info` tool. An AST and handler-signature check confirmed that
the 15 adapters forward every parameter unchanged and their handlers accept
those names as keywords.

- [ ] Consider the proposed `**locals()` reduction, or retain explicit
  forwarding if it makes the public contract easier to maintain. The current
  compatibility check does not make future local variables safe to forward.
- [ ] Preserve each tool's signature, defaults, docstring and return shape;
  `lookup_airports` does not use the same `dict(...)` wrapper as the others.
- [ ] Preserve `run_mcp_tool` versus `run_lookup_tool`, the process search
  lock, lookup concurrency, caching and evidence recording.
- [ ] Verify forwarded values and the installed 16-tool MCP schema, not just
  a reduction in source lines. `get_runtime_info` needs no forwarding change.

### 2. Remove the redundant urllib/opener path

All production calls to `fetch_search_html` in this checkout pass `client=`.
Only two tests construct `GoogleFlightsHttpSource(opener=...)`, currently in
`tests/test_google_flights.py:616` and `:642`.

- [ ] Move those tests to a fake `SweepHttpClient` while preserving their
  empty-versus-drift assertions and offline execution.
- [ ] Remove the redundant opener adapter and stdlib GET/decode helpers only
  after checking compatibility of the directly importable helper and source
  constructor. No in-repo callers is not proof of no external callers.
- [ ] Define the behavior of `fetch_search_html` without an injected client
  before deleting its current default path.
- [ ] Preserve HTTP failure classification, consent handling, compressed
  response decoding through the production client, retry/cooldown behavior
  and the compact-shopping-to-HTML fallback.

Relevant file: `src/viajante/google_flights.py`. The audit's approximately
120-line deletion is a proposal, not a measured or implemented change.

### 3. Remove unused carrier helper

`airline_names_longest_first` in `src/viajante/carriers.py` has no callers
in this checkout.

- [ ] Confirm it is outside the supported library exports and remove it if
  that compatibility check holds.
- [ ] Preserve carrier alias matching and shopping-code behavior.

### 4. Remove the hotel parser convenience wrapper

`parse_hotels_body` in `src/viajante/google_hotels_rpc.py` is the one-line
`parse_hotels_page(text).cards` wrapper. Its in-repo callers are tests in both
`tests/test_google_hotels.py` and `tests/test_hotel_evidence_context.py`.

- [ ] Check import compatibility, then migrate every test caller before
  removal.
- [ ] Preserve card tuples and the existing empty/blocked/rejected/drift
  exception assertions.

## Excluded from this cleanup

- Ignored `__pycache__` residue is local hygiene, not a tracked product bug.
- Do not merge helpers merely because their names look similar: rounded and
  raw distance helpers, and the clock parsers, guard different boundaries.
- Preserve model/JSON contracts, `tests/bench/` and `bench-baseline.json`.
- Leave `README.md` unchanged and add no dependencies for these cuts.
- Do not claim a complete repo audit from this check: only the supplied four
  findings and the observed hotel contradiction were reviewed here.

## Gates before calling 1.4.1 ready

- [ ] Locked offline suite, Ruff lint/format and `viajante bench` gate pass.
- [ ] Focused synthetic conflict regressions and preserved HTTP failure tests
  pass without network access or Chromium.
- [ ] Installed wheel and npm wrapper retain all 16 tools, expected schemas,
  return shapes and version diagnostics.
- [ ] Repeat bounded live navigation, dates/party and radius checks. Record
  HTTP evidence separately from browser interaction; a successful page fetch
  does not verify booking terms or room allocation.
- [ ] Align all six version stamps and write the changelog only when these
  changes have actually landed in a release candidate.
- [ ] Obtain a new release decision before main merge, tagging or publication;
  the earlier approval covered 1.4.0.
