# Viajante 1.4.6 release

Prepared on 2026-10-07 as the 1.4.6 release after 1.4.5. Version
stamps are aligned across Python metadata, `uv.lock`, npm metadata, and the
server and package entries in `server.json`. Local gates and PR CI passed.
The maintainer authorized merge, tag, and publication. Public registry and
installed-client verification remain separate release gates; their results
will be recorded below after publication.

The release changes the Google Flights production path used by flights,
dates, and flex to read results from the public Google Flights page. See the
[user guide](usage.md) for CLI behavior and the [architecture notes](architecture.md)
for the request and parse flow.

## Public-page flight search

- `--fetch auto` and `--fetch sweep` use public-page GETs over the shared
  Chrome-TLS HTTP/2 client. Auto no longer depends on Playwright availability
  or query count. `--fetch detail` remains an explicit Playwright option.
- The page's `AF_initDataCallback` `ds:1` data is extracted with balanced,
  string-aware scanning and parsed as JSON. Scripts are not evaluated. The
  extracted data array goes directly to the owned shopping-data decoder.
  Missing or unreadable bootstrap data is a parse failure; a recognized empty
  result shape remains provider-empty.
- Round-trip search reads an outbound board and checks up to eight owned
  outbound candidates on selected return pages. Selection retains each
  physical segment's origin, destination, departure date, carrier code, and
  bare flight number in the TFS request. The return page must echo the selected
  outbound route and clock. The returned amount is the provider's package
  total; outbound and return amounts are never added. The eight-candidate cap
  is explicitly scope-bound, and follow-up failures remain visible as partial
  page errors when any packages were completed. Dates and flex retain this
  scope bound and those errors; empty selected return pages alone do not prove
  that the entire round-trip query has no flights.
- Dates and flex use explicit per-day public-page GETs for their named window,
  capped at 31 days. Successful rows remain present if other days fail. Flex
  then makes one additional fresh shop for the selected day. Ordinary flight
  searches no longer make a hidden 31-day typical lookup.
- `VIAJANTE_SWEEP_MODE` accepts `standard` (up to 8 concurrent GETs) or
  `conservative` (up to 2); invalid values fail before provider access.

## Explicit limits and failure behavior

The public-page path sends a carry-on (a party total: Google's counter caps at
the number of adults), airline includes and exclusions, and alliance includes.
Every page read must echo each filter (the `Bags` / `Airlines` chips, plus an
airline-catalog row per alliance) or it fails as `markup_drift`. Google
registers an airline exclusion without applying it, so Viajante drops cards
with an excluded or unknown carrier; excluded codeshare rows vanish from the
page and operator evidence is limited. Checked bags, a zero carry-on, and
alliance exclusion have no provable echo and are refused before networking
instead of silently losing constraints. The public page bootstraps no
multi-city results: sweep refuses it, and an explicit `--fetch detail` drives
the browser through each leg. Detail reads no filter echo and refuses bag and
carrier filters. Explore reads the catalog request the public Explore page
issues inside Chromium (`viajante[browser]`), never a request Viajante sends
itself. The request must echo the origin and date, priced rows must prove
their origin and destination, and only the default one-adult economy state is
accepted. A status 13 on that request stops the search and records the shared
cooldown; during validation it appeared on most catalog loads.

HTTP 429 and raw RPC status 13 stop pending sweep work without replay or
browser fallback. Error diagnostics add only the endpoint host and path, HTTP and raw
RPC status, whether the request was sent, attempt count, and cooldown basis.
An active local cooldown reports no request sent, zero attempts, and null
HTTP/RPC status. Status 13 alone does not establish an IP block or identify
the cause. A direct response may still trigger the existing guessed cooldown;
the cooldown basis identifies that heuristic. Search availability remains
unknown after a blocked or unreadable response.

Private live probes exercised selected-return TFS requests and observed a raw
RPC status 13 while a separate browser session could still return page data.
Those observations do not isolate a unique status-13 cause. Raw captures and
personal query data remain outside this public repository.

## Compatibility

The public meaning of `auto` changes: flight searches use the public-page
sweep for every query count, even when Playwright is installed. Callers that
need DOM evidence must request `detail` explicitly. Sweep does not fall back to
detail after an empty result, parse failure, or provider block. Named filters
that this transport cannot verify now fail before the provider request, which
is safer than returning an unconstrained fare under a constrained query.

The search result envelope remains additive. Per-error diagnostics are
optional and do not replace `status`, `empty_reason`, `error_code`, cooldown
fields, or `observed_at`. Hotel providers and the opt-in Skiplagged tools are
outside this flight transport change.

## Validation and release handoff

The pre-review candidate passed the offline suite (1,725 tests, 2 skipped). Live checks ran
serially through the real stdio MCP server in conservative mode, at least 45
seconds apart, with no provider block or cooldown recorded:

| Case | Result |
| --- | --- |
| One-way, two adults, carry-on, nonstop | `ok` / `complete`; Bags chip echoed |
| Checked bags; alliance exclusion; multi-city on sweep | `failed` / `rejected` preflight, nothing sent |
| Airline include | `ok` / `complete`; every card carries the airline (codeshares count) |
| Airline exclusion | `ok` / `complete`; no card shows the excluded airline |
| Alliance include | `ok` / `complete`; catalog row echoed |
| Connecting round trip | `ok` / `partial` (eight-outbound bound); package totals only |
| Dates with an airline filter; flex with carry-on | `ok` / `complete`; filters encoded on every page |
| Re-check of an airline-filtered offer | `same_price`, previous source `search_evidence` |
| Split tickets via a named hub | `ok`; separate-ticket warning, same-currency total |
| Trip, carry-on flight side | `ok` / `complete`; flight query keeps carry-on |
| Explore, then owned destination shops | `ok` / `complete`; three destinations priced by shops |
| Multi-city with `--fetch detail` | `timeout` / `not_loaded`: result cards never rendered; unverified |

The first domestic United States checks failed closed as `markup_drift`
because the page labelled the cabin `Economy (include Basic)`; that label is
now accepted for economy, and `Economy (exclude Basic)` is not.

The offline bench reports `gate: ok`; Ruff lint and formatting pass. Pre-review
wheel and sdist builds passed. The installed wheel reports 1.4.6 and passes the stdio smoke
with the declared minimum MCP SDK 1.14.1 (22 tools). Its page decoder and
currency/party/cabin/selected-outbound checks also passed against the private
archived provider pages. The npm tarball contains only its three intended files;
an installed tarball passed CLI version and stdio checks through a private bridge
to the installed candidate wheel. This checks the shim and pin, not public PyPI
resolution of the unpublished candidate.

## PR review corrections

The reviewed candidate's eight findings are corrected:

- Explore keeps the real HTTP status, endpoint and numeric `Retry-After`, stops
  on HTTP 429 or RPC status 13, and records cooldowns only for direct responses.
  A blocked proxied search also stops its remaining jobs without a global cooldown.
- RPC failure scanning checks every row and chunk; status 13 takes precedence
  over another error or a usable data row.
- Explore checks cancellation and deadlines between browser waits and before
  recording a cooldown. An expired search remains `not_loaded`, never provider-empty.
- Airline matching uses exact owned codes, including returned codeshares. Name
  aliases are a fallback only when codes are absent, with word boundaries.
- Multi-city reselection requires a unique owned segment identity and departure
  clock and uses the current selected row's evidence. The final amount still
  comes from the provider's last board, never a sum of journey prices.
- An empty flight page must still echo a requested alliance in its catalog.
- Runtime carrier, multi-city and Explore capability flags remain booleans;
  `exclude_alliances`, `multi_city_fetch` and `explore_catalog_scope` add limits.
- The contradictory public-filter refusal paragraph in `AGENTS.md` is updated.

After these corrections the offline suite passes (1,741 tests, 2 skipped),
including 16 new regression tests. Ruff lint/format and the offline bench pass.
These corrections made no new live provider requests; the live and installed
artifact checks above describe the pre-review candidate. Multi-city remains
unverified live.

PR CI passed for the corrected candidate on Python 3.10–3.14, the minimum MCP
SDK, built distributions, and lint. Main CI, tag publication, and public
registry verification are separate gates in the authorized release workflow.
