# Viajante 1.4.6 release candidate

Prepared on 2026-10-07 as the 1.4.6 candidate after 1.4.5. Candidate version
stamps are aligned across Python metadata, `uv.lock`, npm metadata, and the
server and package entries in `server.json`. This is a review record, not a
publication record. Local gates and installed artifacts passed; PR CI and public
registry verification are separate release gates. This document does not authorize
a merge, tag, or publication.

The candidate changes the Google Flights production path used by flights,
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

The full offline test suite passed (1,725 tests, 2 skipped). Live checks ran
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

The offline bench reports `gate: ok`; Ruff lint and formatting pass. Wheel and
sdist builds passed. The installed wheel reports 1.4.6 and passes the stdio smoke
with the declared minimum MCP SDK 1.14.1 (22 tools). Its page decoder and
currency/party/cabin/selected-outbound checks also passed against the private
archived provider pages. The npm tarball contains only its three intended files;
an installed tarball passed CLI version and stdio checks through a private bridge
to the installed candidate wheel. This checks the shim and pin, not public PyPI
resolution of the unpublished candidate.

Supported-Python PR CI and public registry verification remain separate release
gates. Review the final diff and PR before merge. No tag or publication has been
created or authorized.
