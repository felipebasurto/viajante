# Viajante 1.4.5 release candidate

Prepared on 2026-10-06 from reviewed `develop` commit `8573c56`, relative to
published 1.4.1 / `main` commit `e5091a1`. The requested next version is 1.4.5;
this candidate does not create releases for the intervening version numbers.
All six package version stamps are 1.4.5: Python metadata, the lockfile's local
package, npm metadata, and the server and two package entries in `server.json`.

This is the review record for the PR into `main`. It is not a publication
record. The detailed entries are in [CHANGELOG](../CHANGELOG.md#145---2026-10-06);
commands and contracts are in [usage](usage.md) and [architecture](architecture.md).

## Flight search and local validation

- **Metro search:** a named owned metro code expands to its member airports for
  one-way and packaged round-trip searches, including the flight half of trip
  search. An airport does not expand automatically. Unsupported trip kinds,
  `nearby` combined with a metro, routes within the same metro, and plans above
  18 provider queries are rejected before fetching. Airport lookup exposes the
  members; unknown codes are never filled in.
- **Segment times:** compact offers retain provider arrival dates and catalogue
  IANA departure/arrival zones. Completeness exposes missing dates and zones.
  The public `local_instant` helper checks both DST folds. Local itinerary
  validation adds `arrival_deadline`, `chronological`, `min_stay_days` and
  `max_stay_days`. Offset deadlines compare UTC; naive deadlines use the final
  destination's civil time. Missing, ambiguous or nonexistent times remain
  unknown. Stay bounds require the next journey from the same airport and do
  not invent a stay after the final flight.
- **Recommendation:** a per-query block adds a deterministic choice and up to
  three distinct alternatives with requirement checks, highlights and tradeoffs
  based on returned fields. Price/duration/stops weights are 0.5/0.35/0.15;
  currencies are never converted or compared across currency groups. Unknown
  duration/stops incur the worst penalty. Near-identical flights are deduplicated.
  When a one-way has no exact match, the fewest named requirements are relaxed,
  with this tie order: arrival clock, departure clock, departure window,
  duration, carry-on, checked bags, stops. The relaxed names are explicit.
  Price caps, carrier, via, overnight and layover filters are never relaxed;
  packages are not relaxed. A recommendation beside `filtered_out` is a separate
  relaxed scenario, not evidence that the exact request was satisfied.
- **Ranked selection:** connecting flights longer than three times the fastest
  nonstop (or shortest known offer if none exists) are omitted from ranked
  selection and recommendation comparison. `sort=price` retains fare ordering
  without this omission. This is a behavior change from 1.4.1.
- **Fresh re-check:** `recheck_offer` / `viajante recheck-offer` makes a fresh
  Google search under the existing process lock and provider cooldown, bypassing
  the five-minute search replay. It requires complete itinerary identity before
  concluding `same_price`, `price_changed`, `multiple_matches` or `not_found`.
  Incomplete fresh segments or missing package journeys are `check_failed` /
  `incomplete_offers` unless a complete exact match exists. Loose matching and
  substitution are explicit opt-ins. Original quote, fresh quote, identity
  differences and filter violations remain separate. A failed or blocked check
  never proves the old flight disappeared. Only ledger-matched old evidence is
  owned; caller-supplied old amounts and clocks are not registered as evidence.

## Separate tickets

`search_split_tickets` / `viajante flights --split-tickets` compares independently
fetched one-way tickets with the owned packaged fare. One-way routes pair through
up to five named airports or layover airports from the packaged offer; round trips
pair outbound and return one-ways. Multi-city hub splitting is unsupported.
Each ticket keeps its own offer and URL. Totals and the packaged comparison require
one owned currency and respect its minor unit; nothing is derived from a package
price or converted. The comparison states savings or extra cost explicitly.

Every pairing is `split_ticket: true`, `connection_protected: false`; hub pairs
also say `self_transfer: true`. Hub airport continuity and UTC connection timing
must be proven. The default minimum of three hours is a planning setting, not a
provider guarantee. Missing/DST-ambiguous timing rejects a hub pair. Mixed round
trips with unproven arrival timing remain labelled unproven and are retained only
when no proven pair exists in that currency. Ranking never compares currencies.
Extra searches are capped, sequential and stop at a recorded Google cooldown.
Coverage names the hubs actually tried and the stopping reason.

The CLI forwards `--top` to the split search and rejects unsupported named clock,
layover, via, overnight, airport and baggage-buffer filters before any fetch.
Use the split-specific connection/via options. Baggage, transit and airport
transfer feasibility still need verification; separate tickets are unprotected.

## Hotel finalists and partial searches

- `get_hotel_details` is a new top-level Python export and MCP tool. MCP hotel
  offers carry process-local `selection_id` references. Unknown or evicted
  references fail locally. A snapshot read does not use the search worker or
  record new evidence; replay retains its original timestamp/reference.
- `room_rates=true` fetches a separate Skiplagged USD room quote using the
  original dates and occupancy. Exact property name, city and original
  coordinates identify the finalist. Ambiguous cities send nothing. Mismatched
  property, dates or occupancy produce no accepted room quote. Missing provider
  echoes stay unknown/partial. Accepted fresh room quotes alone enter the ledger;
  their cancellation/capacity evidence never changes the original stay quote.
- Exact-name room lookup preserves the requested room count and rejects an
  owned city echo that contradicts the requested city.
- Google Hotels retains primary-page cards after a secondary transport failure
  or deadline. `page_errors`, incomplete coverage and a partial MCP envelope
  expose the missing evidence; the CLI prints a notice and these results are
  not replay-cached. A secondary transport failure receives only the owned
  once-replay on a fresh session.

## MCP contracts and transport

The installed server exposes **22 tools**, up from 16 in 1.4.1. The six additions
are `get_hotel_details`, `search_split_tickets`, `recheck_offer`, `price_history`,
`watch_price` and `get_guide`. Existing tools remain registered.

| Area | 1.4.5 contract and client impact |
| --- | --- |
| Result envelope | Every tool except the bare-list `lookup_airports` exposes `status`, `completeness`, `empty_reason`, `empty_note`, `error_code`, retry fields and observation fields through its output schema and structured content. Existing payload fields remain alongside them. |
| Empty results | `provider_empty` alone proves an answered empty search. `filtered_out` means returned rows were removed locally. `not_loaded` means availability is unknown. Unpriced calendar cells and unshopped/failed Explore rows do not prove absence. |
| Input errors | `isError` text keeps the SDK prefix followed by a JSON `error` object: `invalid_parameter`, a provable top-level `field` or null, and `message`. Missing, mistyped and undeclared parameters use the same body. Concurrent searches use `search_in_progress`; internal decode/shape errors are not caller input errors. |
| Retry information | Direct recorded rate limits add UTC `retry_after` and integer `retry_after_seconds`; envelope values repeat owned per-error fields. Proxied 429s without a cooldown do not invent retry times. |
| Text and guide | MCP text is compact JSON; library and CLI saved JSON are unaffected by this formatting change. Long guidance is in `viajante://guide` and `get_guide`; the required `guide` string is advertised in its output schema. |
| Annotations | All tools advertise titles and annotations. `watch_price` writes local state and is neither read-only nor idempotent. Provider-facing tools declare open-world behavior. |
| SDK requirement | The MCP extra now requires `mcp>=1.14.1,<2` instead of `>=1.6,<2`. Existing environments below the new floor must upgrade. Python remains 3.10+. |
| Transport | Stdio remains the packaged default. Opt-in local Streamable HTTP uses `--transport streamable-http`, defaults to loopback and explicitly validates Host/Origin on the SDK floor. It has no authentication. No hosted remote is advertised in `server.json`. |

### Progress, cancellation and deadlines

- Clients supplying a `progressToken` receive progress notifications throttled
  to about one per 250 ms, with monotonic values and a final flush. A missing
  token sends no notifications. Notification failures are logged once and do
  not break the search.
- Cancellation is cooperative between queries, retries, pacing/backoff sleeps
  and multiplexed response checkpoints. It frees the worker/lock when the
  worker exits and prevents cache, ledger, history and cooldown writes for that
  cancelled result. It is not a guarantee of preempting every provider call.
- Optional `deadline_seconds` applies to flights, dates, flex, explore, hotels
  and trip (library and MCP); `VIAJANTE_MCP_DEADLINE_SECONDS` provides a server
  default. Values must be numeric, finite and positive, including before cache
  replay. Invalid environment defaults stop startup.
- Completed rows survive a deadline; unfinished queries carry `deadline`,
  coverage is incomplete with stopping reason `deadline`, and empty availability
  is never inferred. Typical-price, package follow-up and secondary hotel-page
  cuts retain completed evidence. An all-deadline envelope is `timeout` /
  `partial` / `not_loaded`, with no invented retry interval. Deadline-cut results
  are never cached. Cached successes preserve timestamps and finalist references.

## Opt-in local history and watches

`VIAJANTE_PRICE_HISTORY=1` records priced real flight/hotel observations in the
state directory, off by default. Failures, empty results, cooldowns, cancellations
and cached replays are not observations. The newest 2,000 valid entries are kept.
`viajante history` / `price_history` report first/last/low/high/change facts per
exact query and currency; a single observation is explicitly a single observation,
not a trend or forecast. `history --clear` is explicit local deletion.

`viajante watch` / `watch_price` saves validated flight/hotel argument sets and
runs them only on demand. Re-saving a name replaces that watch. There is no
built-in scheduler or notification service. One search lock and machine-wide
cooldowns still apply. Unreadable history/watch files are surfaced as read errors,
never empty state. Saving refuses an unreadable watch file and preserves its bytes.
Invalid history rows are skipped for calculations but preserved when appending.
Recording failures keep the owned price and expose `recording_error`.

Read-modify-write cycles use cross-process file locks; state writes use unique
temporary files, `fsync`, rename and private file modes. On platforms without
either POSIX `flock` or Windows locking, the fallback cannot guarantee concurrent
updates. All tests and bench subprocesses isolate the state directory and strip
the history opt-in so fixture fares never enter personal history.

## Other corrections and maintenance

- Sweep transport exceptions are typed `fetch_failed` (or timeout), not guessed
  429s or markup drift. A multiplexed batch replays once on a fresh TLS session
  without stacked per-query retries; calendar and Explore POST transport failures
  retain their correct classification.
- `verify_answer` reports `status: failed` when its provenance verdict is false;
  owned split-ticket savings and extra costs can be checked. Provenance still
  does not prove reachability or travel feasibility.
- Added regression suites cover the envelope, client compatibility, cancellation,
  deadlines, history, storage, metro expansion, temporal validation, finalists,
  re-checks, recommendation and split tickets. CLI and real stdio regressions
  cover filtering, `top`, references and accepted versus rejected evidence.
- CI checks Python 3.10–3.14, the minimum MCP SDK, installed-wheel behavior and
  Ruff. The secondary-loopback integration test probes address availability and
  skips only that address when macOS does not configure it. Cooldown and bench
  assertions use fixed clocks; product scoring and the bench baseline are unchanged.
- Usage, architecture, agent contracts, MCP guide, skill guidance and `llms.txt`
  document the new surface. This release preparation leaves the existing README
  edits from reviewed develop unchanged. Historical 1.4.1 notes are restored to
  their published content; corrections introduced after publication belong here.

## Integrated PRs

| PR | Scope |
| --- | --- |
| [#55](https://github.com/felipebasurto/viajante/pull/55) | Segment zones/dates, metro expansion, local validation and hotel finalist details |
| [#56](https://github.com/felipebasurto/viajante/pull/56) | Local price history, saved watches, strict reads and locked state updates |
| [#57](https://github.com/felipebasurto/viajante/pull/57) | Opt-in separate-ticket search, owned comparisons, bounded coverage and split envelope |
| [#58](https://github.com/felipebasurto/viajante/pull/58) | Fresh flight-offer re-check and explicit identity/provenance outcomes |
| [#59](https://github.com/felipebasurto/viajante/pull/59) | Deterministic recommendation and distinct shortlist |
| [#60](https://github.com/felipebasurto/viajante/pull/60) | Typed result envelope, empty-result semantics and transport classifications |
| [#61](https://github.com/felipebasurto/viajante/pull/61) | Client schemas/errors/annotations, guide, SDK floor and local HTTP |
| [#62](https://github.com/felipebasurto/viajante/pull/62) | Progress, cooperative cancellation, deadlines and integration fixes |
| [#63](https://github.com/felipebasurto/viajante/pull/63) | Required guide output schema |
| [#64](https://github.com/felipebasurto/viajante/pull/64) | Recommendation wording and relaxed-result guidance |
| [#65](https://github.com/felipebasurto/viajante/pull/65) | Review corrections for identity, split filters/top, hotel coordinates/ledger, partial pages and cache/control integration |

## Validation and release handoff

The 1.4.5 candidate passed these pre-publication checks:

- Locked Python 3.13.7 / MCP 1.29.0: all 1,610 offline tests passed, with two
  explicit macOS skips (the child-only isolation probe and unavailable secondary
  loopback address). Ruff lint/format passed for all 98 source/test/script files.
  `viajante bench` returned `gate: ok`; the real stdio smoke passed.
- Python 3.10 / minimum MCP 1.14.1: real stdio smoke and 201 focused contract,
  compatibility, guide and envelope tests passed, with the unavailable secondary
  loopback address skipped. Full Python 3.10–3.14 coverage runs in release PR CI.
- Wheel and sdist built with `uv build --no-sources`; strict Twine checks passed
  for both. Rebuilding the wheel from the sdist produced identical package code
  and data (49 files), including airport data, `py.typed` and third-party notices.
- The wheel installed outside the checkout using the locked MCP dependencies.
  Its CLI reported 1.4.5 and resolved JFK. A real stdio session verified the
  exact 22-tool set, runtime 1.4.5/hotel schema 2, required guide schema, local
  airport lookup, invalid-argument contract and an exact-cent synthetic cost split.
- The actual packed npm tarball installed locally. Its metadata and `mcpName`
  matched 1.4.5. Its CLI and real stdio MCP passed through a private bridge that
  asserts the shim's exact `viajante==1.4.5` / `viajante[mcp]==1.4.5` pins and
  dispatches the installed candidate wheel. Its tool schemas matched the wheel.
- A real stdio schema comparison against `main` 1.4.1 under the same MCP SDK
  retained all 16 existing tools and every existing input property's type,
  default and required status. Six tools and six optional `deadline_seconds`
  parameters are added. New envelope/error behavior and ranked filtering are
  explicit client-visible changes, as documented above.
- On 2026-10-06, the exact 1.4.5 PyPI, npm and MCP Registry endpoints returned
  404, and no local fetched `v1.4.5` tag existed. The Registry's known 1.4.1
  endpoint returned 200, confirming the route used for the absence check.
  Repeat absence checks immediately before publication.

No publication or live-provider success is implied by these package checks.

The release PR is ready for review and merge only with green checks on its exact
head. Merging `main`, creating/pushing `v1.4.5`, and publication remain separate
actions. When authorized, use the existing tag workflow: PyPI, then npm pinned
to Python 1.4.5, then both stdio package entries in the MCP Registry. Verify main
CI and tag identity, and do not replace an existing version or tag.

After publication, independently verify the PyPI wheel/sdist, npm version and
`mcpName`, Registry package versions and real public installations:

```sh
uvx --refresh --from 'viajante==1.4.5' viajante --version
uvx --refresh --from 'viajante[mcp]==1.4.5' viajante-mcp
npx -y @viajante/mcp@1.4.5
```

Both public MCP routes must expose all 22 tools and report runtime 1.4.5. A packed
npm candidate tested through a local-wheel bridge is pre-publication evidence,
not proof that PyPI/npm public installs work. No live Google, Booking or Skiplagged
searches run as part of the offline release gate. Provider availability, current
fares, private rooms, cancellation, capacity, baggage and transfer feasibility
still need provider evidence for the actual trip; this candidate does not claim
new live-provider validation. Private captures and validation logs stay outside
the public repository.
