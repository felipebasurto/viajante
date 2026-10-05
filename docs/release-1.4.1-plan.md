# 1.4.1 candidate: hotel evidence correction and cleanup

Implemented on 2026-10-05 from the 1.4.0 baseline (`dcab9fc`). All six
version stamps are 1.4.1. The original changelog was Unreleased; this was a tested
candidate at the time of implementation. The combined release was explicitly
authorized on 2026-10-05; see [the release record](release-1.4.1.md).

## Hotel evidence correction

A provider title can explicitly describe a room while the unit chip says
`Entire cottage`, `Entire house` or another entire-home type. Normalization
now exposes the additive schema-2 boolean `lodging_evidence_conflict` and
sets both `lodging_kind` and `property_type_evidence` to `unknown`.

- Original title and raw text remain available for the calling agent.
- The CLI prints the conflicting room and entire-home labels as evidence.
- Explicit private/shared/hotel/dormitory and common single/double/twin room
  labels can contradict an entire-unit chip. A private bathroom alone cannot.
- Entire-house chips are recognized alongside cottage, villa and home chips.
- Google general descriptions still cannot prove the priced unit, capacity
  or cancellation. Missing unit chips do not create invented conflicts.
- Unknown candidates remain unverified; requesting `entire_home` alone does
  not prove every returned candidate satisfies it.

Synthetic regressions cover contradictory titles, contradictions within unit
chips, unambiguous units, description isolation, private bathrooms, preserved
capacity evidence, JSON fields and CLI output. No live provider records or
personal itinerary were added to the public tree.

## Completed cleanup

1. **MCP forwarding:** all 15 async tool adapters now use `**locals()`; the
   direct `get_runtime_info` tool is unchanged. Tool signatures, defaults,
   docstrings and return shapes are preserved, apart from documenting the new
   evidence field. Search and lookup workers remain separate. Registered-tool
   tests check the exact handler, worker, argument values and return shape
   with both explicit arguments and defaults. Any future incidental locals
   would fail those checks.
2. **HTTP injection:** removed the redundant urllib/opener path and migrated
   its two tests to `SweepHttpClient` fakes. The default HTML helper uses the
   shared Chrome TLS client and respects cooldown before opening a session.
   The existing compact/HTML, consent, empty/drift, retry and rate-limit tests
   remain. A residual opener reference found during installed live testing
   was removed from `reset`; a new regression covers shared versus injected
   session resets.
3. **Carrier helper:** removed `airline_names_longest_first`, with no in-repo
   callers or supported top-level export.
4. **Parser wrapper:** removed `parse_hotels_body` and migrated callers in
   both hotel parser and evidence regression tests to `parse_hotels_page`.
   Tests that expect an exception call the parser directly.

The removed helper imports and `opener` constructor keyword were outside
supported top-level library exports. This does not assert compatibility with
undocumented external imports. No dependencies, README, benchmark corpus or
benchmark baseline were changed. Ignored bytecode residue remains outside
product scope.

## Validation

- Locked environment: 1,049 offline tests passed, Ruff lint/format passed,
  and `viajante bench` returned `gate: ok`. No live requests ran in the suite.
- Same-SDK comparison against 1.4.0 confirmed identical input/output schemas
  and annotations for all 16 MCP tools. Hotel result JSON gains only the
  documented additive evidence field.
- Wheel and sdist built outside the checkout; strict Twine metadata checks
  passed. The installed wheel reports 1.4.1 and reproduces the synthetic
  contradiction as unknown with a conflict flag.
- Actual installed-wheel stdio MCP passed the 16-tool and argument-name
  checks, runtime diagnostics, changed-roster grouping and exact-cent split.
- The packed npm 1.4.1 tarball passed actual stdio checks for all 16 tools and
  local arithmetic. A private uvx bridge asserted its exact 1.4.1 Python pin
  and dispatched the installed candidate wheel. This verifies the tarball
  against the local wheel, not a public npm/PyPI 1.4.1 installation.
- One bounded live Google Hotels search succeeded. Returned offers were
  inside the named radius; Google omitted unit/party evidence in this response,
  which stayed unknown. Replaying two previously captured explicit unit-label
  contradictions through the installed normalizer marked them unknown/conflicted.
- Two generated navigation URLs returned the correct nonempty property pages
  with matching date and traveler controls, through the production Chrome TLS
  HTML helper. This is HTTP evidence, not browser interaction.
- A separate live flight request returned a data-less RPC status 13. After
  the reset fix, MCP returned typed `blocked`/`rate_limited` evidence and a
  cooldown, without the removed-attribute error. No live flight fare readiness
  is claimed and no additional Google searches were sent after that block.

Raw captures, logs and test bridges stay outside this public checkout.
Final price, room allocation, cancellation terms, browser UI interaction and
Booking live navigation remain unverified by this change.

## Before publication

- Review this implementation and the PR's complete Python 3.10–3.14,
  installed-wheel and lint CI results.
- The user explicitly authorized the 1.4.1 release after PRs #50, #52 and #53
  were reviewed and merged into develop. Changelog entries are now dated
  2026-10-05. Main CI and publication checks follow in the release record.
- When approved, finalize the changelog date, validate main, push the version
  tag and follow PyPI → npm → MCP Registry publication and public-install
  verification. Keep provider blocks distinct from package readiness.
