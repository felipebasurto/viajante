# 1.4.0 hotel evidence and navigation candidate

Prepared on 2026-10-05. This is a candidate, not a published release. Main merge,
tagging and PyPI/npm/MCP Registry publication require human confirmation.
All six version stamps are aligned; hotel schema 2 gains additive fields.
README is owned elsewhere and remains unchanged.

## Behavior

- Google internal `/travel/clk/hi` click endpoints can return an empty HTTP 204
  even immediately in the search session. Links now use the provider's entity
  ID at record[20]. Missing or malformed IDs never fall back to a click tracker.
- Entity and search navigation encode dates, adults, rooms, currency and requested
  cancellation/property filters in Google's `ts` context. `link_context` and
  `applied.url_context` distinguish stay/property/location/none. Context is not
  proof of a current price, room layout, private occupancy or refundable terms.
- Google general descriptions remain available in `details` but no longer prove
  the priced unit's lodging kind, capacity or cancellation. Explicit unit chips
  still supply evidence. Shared rooms and dorm/private mixes stay unknown;
  entire cottage and entire villa chips are recognized.
- Known `priced_adults` mismatches and insufficient single-unit capacity are
  excluded. Unknown occupancy stays explicitly unverified.
- Optional `max_distance_km` (CLI `--max-distance-km`) requires a named `near`
  point and a finite positive value. Unrounded straight-line distance filters
  outside or unlocated offers before ranking and top. Without a radius,
  distance remains annotation only; no city center is assumed.
- CLI `--version`, offline MCP `get_runtime_info` and hotel JSON
  `viajante_version` expose the executing installation. MCP has 16 tools.
- Agent contracts now preserve the latest nightly roster, replacement quote
  requirements, exact cancellation deadlines, dorm exclusivity, transfer
  feasibility, missing evidence and sequential zsh-safe searches.

## Validation

- Locked sync, 1043 offline unittests, Ruff lint/format and offline bench passed
  (`gate: ok`). No benchmark baseline changes or score optimization.
- Synthetic regressions cover entity navigation and malformed IDs, provider
  description isolation, capacity/party contradictions, radius boundaries,
  filtering before top, invalid arguments before fetch, CLI/MCP forwarding,
  runtime versions, changed rosters and unallocated nights.
- Wheel and sdist built outside the checkout; installed-wheel CLI and actual
  stdio MCP passed with exactly 16 tools, the radius parameter, runtime version
  1.4.0/schema 2 and local roster arithmetic. CI now checks these installed seams.
- npm tarball built outside the checkout. An offline Node process interception
  verifies the wrapper executes uvx with its exact Python version and arguments.
  No candidate registry install or publication is claimed.
- Two small sequential live Google searches (hotel and vacation-rental contexts)
  succeeded. Six generated navigation URLs returned nonempty HTTP 200 pages.
  Returned check-in/out inputs and adult controls matched; search-page AtySUc
  request echoes preserved the requested room counts. Entity pages retained
  the encoded context. Room allocation and current quoted totals remain unverified.
  This is HTTP/HTML evidence, not a browser interaction test.
- The separate local uv tool was upgraded from 1.1.2 to the currently published
  1.3.1. Its real stdio entry and published npx 1.3.1 each exposed 15 tools and
  passed local roster arithmetic. They do not yet include this candidate's fixes.

Raw provider captures, live prices, personal reservations and trip scripts were
kept outside this public tree. No Booking reservation was modified.

## Remaining release boundaries

Browser UI interaction and Booking live navigation were not exercised. Hotel
room-rate availability through the optional second provider remains unverified
by this change. The inherited status-13 cooldown heuristic, unconfirmed Google
tax/fee breakdown and lack of currency conversion remain unchanged.

After approval and publication, refresh/reload the user's MCP and explicitly
upgrade the separate CLI, then verify the executing 1.4.0 version on both.
Public PyPI/npm/Registry checks belong to that separately authorized release.
