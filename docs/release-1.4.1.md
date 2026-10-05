# 1.4.1 correctness and hotel evidence release

Prepared and explicitly authorized for publication on 2026-10-05 after PRs
#50, #52 and #53 were reviewed and merged into `develop` at `8ab0986`.
All six version stamps are 1.4.1; the changelog is dated 2026-10-05.
Publication results will be reported in the GitHub release after the workflow
and public-install checks complete. README remains unchanged.

## Changes

- Hotel schema 2 adds `lodging_evidence_conflict`. Explicit room and entire-unit
  contradictions keep both lodging kind and property type unknown, preserve
  provider evidence, and appear in CLI output. Entire-house chips are recognized.
- Itinerary validation includes return and multi-city dates. Missing packaged
  journeys, segments and layovers remain unknown. Packaged filters run after
  journey attachment and before final top selection, with bounded candidate
  refill; unknown connections cannot prove airport exclusions.
- Trip totals preserve dated journeys and full multi-city leg identities.
  Answer verification binds amounts to their owned currencies, and cached
  searches refresh the evidence ledger.
- New detail searches respect Google's machine-wide cooldown. Skiplagged
  rate-limit flags and cooldown recording survive session recovery.
- Explore exposes pricing failures separately from empty results, preserves
  accurate coverage, avoids caching failed pricing, and returns appropriate
  CLI exit statuses.
- Cancelled MCP searches retain the process lock until their worker finishes.
  Atomic writes use unique temporary files with cleanup; nested hotel stays
  reject non-integer occupancy. Locked PyJWT is updated to 2.15.1.
- All 15 async MCP adapters use parameter forwarding. Unused internal helpers
  and the redundant urllib/opener path are removed; public tool signatures,
  defaults and worker separation remain intact.

## Validation before publication

- The combined `develop` tree passed all 1,074 offline tests, Ruff lint/format,
  and `viajante bench` (`gate: ok`). No provider searches ran in these checks.
- Wheel and sdist builds passed. An installed-wheel stdio smoke returned all
  16 expected tools, runtime version 1.4.1/hotel schema 2, and airport lookup.
- [Combined PR CI](https://github.com/felipebasurto/viajante/actions/runs/37348694244)
  passed Python 3.10–3.14, lint and installed-wheel distribution smoke.
- The merged remote develop tree exactly matched the locally validated
  integration tree. The changelog conflict retained both PRs' release notes.
- Original cleanup validation and bounded live evidence remain documented in
  [the candidate record](release-1.4.1-plan.md). Its 1,049-test count and
  no-dependency-change statement describe PR #52 before the audit fixes.

## Publication and remaining limits

Merge the authorized develop release into main after green PR CI, verify main
CI, and push an annotated `v1.4.1` tag at that exact commit. The existing workflow
publishes PyPI, then npm pinned to Python 1.4.1, then both MCP Registry transports.
Verify public metadata and real uvx/npx stdio installations before reporting
publication complete. An existing published version or tag must not be replaced.

Package readiness does not prove live-provider readiness. The candidate's
live flight request returned data-less status 13 and typed blocked/cooldown
rather than a fare. Browser UI and Booking live navigation remain unverified.
Final totals, party capacity, room allocation, cancellation terms and transfer
feasibility still require provider evidence. No price conversion is introduced.
