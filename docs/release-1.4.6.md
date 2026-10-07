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
  page errors when any packages were completed.
- Dates and flex use explicit per-day public-page GETs for their named window,
  capped at 31 days. Successful rows remain present if other days fail. Flex
  then makes one additional fresh shop for the selected day. Ordinary flight
  searches no longer make a hidden 31-day typical lookup.
- `VIAJANTE_SWEEP_MODE` accepts `standard` (up to 8 concurrent GETs) or
  `conservative` (up to 2); invalid values fail before provider access.

## Explicit limits and failure behavior

The public-page path refuses named checked-bag or carry-on constraints and
airline or alliance filters because its page evidence cannot prove them. It
also refuses multi-city. These requests fail before networking instead of
silently losing constraints. Explicit browser detail remains available where
its evidence supports the requested features. Public Explore catalog recovery
is not implemented; `explore` fails closed and asks for named destinations.

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

The full offline test suite passed (1,668 tests, 2 skipped). A live stdio MCP
check on runtime 1.4.6 verified the public flight path in conservative mode:
an unsupported baggage-constrained query failed preflight with no request and
zero attempts; the corresponding unconstrained one-way search returned three
offers with `ok` / `complete`; and a round-trip search returned three complete
packages with both journeys present, `ok` / `partial`, and no page errors.
The partial round-trip status reflects the documented eight-outbound scope
bound. Archived page evidence also verified currency, passenger, cabin, and
selected-outbound echoes without committing those private captures.

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
