# 1.4.0 hotel evidence and navigation release

Prepared and authorized for publication on 2026-10-05. The user explicitly
approved merging PR #51 and publishing v1.4.0. Publication results will be
recorded after the workflow and public installation checks complete.
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
- Strict Twine package-metadata checks passed for the built wheel and sdist.
- [PR #51 CI](https://github.com/felipebasurto/viajante/actions/runs/37303928609)
  passed all seven jobs: Python 3.10–3.14, lint, and installed-wheel distribution
  smoke. The head verified there is `b99b5c3`; later documentation changes must
  also finish CI before merging.
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

## Publication procedure

The existing `.github/workflows/publish.yml` runs on a pushed `v*` tag. It builds
wheel/sdist, publishes PyPI, publishes the npm wrapper pinned to that same Python
version, waits for npm propagation, then registers both transports in MCP Registry.
The latest 1.3.1 [publish run](https://github.com/felipebasurto/viajante/actions/runs/37006011348)
completed all four jobs. PyPI uses the GitHub `pypi` environment and OIDC; npm's
token-free path uses Trusted Publishing with provenance. That success establishes
the previous configuration worked, not that future registry access is guaranteed.

Preparation verified that v1.4.0 is absent from the remote Git tags and that the
PyPI and npm 1.4.0 metadata endpoints return 404. Repeat these checks if another
release or candidate change intervenes. Version stamps and `mcpName` are already
aligned; the changelog entries are dated 2026-10-05 for release 1.4.0.

The user approved this sequence on 2026-10-05:

1. Confirm the approved PR head and all seven checks are green. Finalize the
   changelog's 1.4.0 release date, then merge PR #51 into main using that exact
   approved head. Recheck the resulting main commit and its CI.
2. Create and push the annotated `v1.4.0` tag at that validated main commit.
   Pushing the tag starts publication; it is not a preparation-only operation.
3. Watch the publish workflow through build, PyPI, npm and MCP Registry. Verify
   PyPI has both wheel and sdist, npm reports version 1.4.0 with the correct
   `mcpName`, and Registry lists 1.4.0 with both transports.
4. Test the public installations over real stdio with exactly 16 tools,
   `get_runtime_info` reporting 1.4.0/schema 2, the radius parameter, and local
   roster/cost-split calls. A successful upload alone does not complete release.
5. Explicitly upgrade the separate CLI and refresh/reload the user's MCP. Check
   both executing versions; an already running process does not update itself.

Post-publication entry points:

```bash
uvx --refresh --from 'viajante==1.4.0' viajante --version
uvx --refresh --from 'viajante[mcp]==1.4.0' viajante-mcp
npx -y @viajante/mcp@1.4.0
```

If PyPI is already published but npm or Registry failed, inspect public metadata
and the failed job first. The existing recovery dispatch resumes npm/Registry
from the same tag without repeating the PyPI job:

```bash
gh workflow run publish.yml -f release_tag=v1.4.0
```

If PyPI itself failed, recover the failed tag run after checking which exact
artifacts uploaded. Never retag a different commit or replace a published npm
version. A content/metadata correction after publication needs a new version.

Reference: [uv publishing](https://docs.astral.sh/uv/guides/package/),
[npm Trusted Publishing](https://docs.npmjs.com/trusted-publishers/),
[PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/).
