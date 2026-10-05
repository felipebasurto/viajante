# Verifiable journeys and finalist evidence: implementation validation

Implemented on `feat/verifiable-finalists`, based on candidate commit `36864c3`.
Package versions remain unchanged. This branch is for review; merge, tags and
publication remain separate release decisions.

The change adds owned segment arrival dates and catalogue IANA zones, UTC
chronology/deadline validation and local stay-day bounds, opt-in Pareto flight
selection, process-local finalist references, flight snapshots/exact refresh,
and separate Skiplagged hotel room quotes. Technical behavior and examples are
in [architecture](architecture.md) and [usage](usage.md).

## Executed offline checks

- `uv sync --locked --extra mcp`: locked environment synchronized.
- `uv run python -m unittest discover -s tests -v`: **1,094 passed**, including
  45 new tests across temporal evidence, Pareto, finalist details and real stdio.
- `uv run ruff check src tests` and `uv run ruff format --check src tests`: passed.
- `VIAJANTE_BENCH_LIVE=0 uv run viajante bench`: **gate: ok**. The baseline and
  scoring policy were not changed or optimized.
- `git diff --check`: passed.
- Real MCP stdio client/server: **18 tools**, Pareto enum/default schema,
  flight snapshot, exact refresh with changed price/token, successful cache
  replay retaining timestamp/id, hotel snapshot, separate EUR/USD quotes,
  and unknown reference rejection. Sources were offline fixtures.

Coverage includes date-line crossing, next-day arrival, DST ambiguity/gaps,
missing dates/zones, missed deadlines, overlapping segments, packaged stays,
short/long stays, Pareto ties/dominance/baggage/incomplete packages/budgets,
changed flight identities, multiple matches, filter violations before top,
cooldown without network/browser contact, ledger eviction/cache restoration,
flex/nested-trip/multi-stay references, exact hotel names/homonyms, missing or
contradictory cities, unknown occupancy/unit conflicts and provider failures
preserving the original quote.

`README.md`, `bench-baseline.json`, version stamps and the candidate branch
are preserved. No provider requests or Chromium runs were part of these checks.

## Pending live evidence

Live provider availability and the later release decision remain pending.
The next provider check should be small and sequential, stop at a challenge or
cooldown, and distinguish provider availability from the offline result above.
Incomplete flight identity remains inconclusive; some DOM cards and partial
packages cannot refresh an exact finalist. Hotel room conditions remain specific
to their own quote and do not resolve unknown original occupancy or unit conflicts.
