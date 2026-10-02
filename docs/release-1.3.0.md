# 1.3.0 release preparation

Prepared on 2026-10-02 from `develop` at `8e69a35`. Version 1.3.0 is a
proposal, not a published release. No tag was created or pushed, and `main`
was not changed. The changelog date is the preparation date; update it if
publication happens later.

## Validation performed

Local runtime: Python 3.13.7 on macOS arm64. These commands completed successfully:

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run viajante bench
uv build --no-sources
uv sync --extra mcp
```

- Unit tests: 1031, OK. Ruff check passed; all 66 Python files passed formatting.
- Offline bench: `gate: ok`, `tests_ms: 291`, `parse_ms: 2`, `score_ms: 293`.
  This is recorded evidence, not an optimization target; the baseline is unchanged.
- Built `viajante-1.3.0.tar.gz` and `viajante-1.3.0-py3-none-any.whl`.
  Wheel metadata and all six version stamps match 1.3.0, including the lockfile.
  The new stay, cooldown, and Skiplagged hotel runtime modules are packaged.
- `npm pack --dry-run --json` in `npm/` passed for `@viajante/mcp@1.3.0`.
  The shim reads that version and pins the matching PyPI package.
- A real `viajante-mcp` subprocess completed MCP initialization and `tools/list`
  over stdio: exactly 15 tools, protocol 2025-11-25. The hotel schema exposes
  both `stays` and `near`.
- Real stdio calls to `plan_stay_blocks` and `split_stay_costs` passed assertions
  with a synthetic roster: block headcounts, occupant-night shares, exact cents,
  named nightly fees, and uncovered nights. No provider access was needed.
- One minimal Google Hotels stdio search returned `status: ok` with one offer.
  One Skiplagged hotel stdio search returned `fetch_failed`, without
  `rate_limited`. It was not manually retried; live room-rate smoke was not run
  because no owned hotel id was returned. Skiplagged live readiness remains open.
- The MCP SDK emitted a non-fatal Pydantic `lifespan` forward-reference warning
  on stderr; initialization and the verified calls still succeeded.

These checks do not establish live Booking/detail-flight readiness, every
provider path, the other CI Python versions, or post-publication installation.
Raw live responses and personal travel data are not included in this preparation.

## Compatibility and known limits

The six commits after `5140a50` add hotel evidence and tools. The complete
release since v1.2.1 also removes the exported `plan_prompt` API and prose input
to `get_flights`, plus prompt benchmark flags. These changes are listed under
Removed in the changelog; the complete release is not strictly additive.
Before tagging, explicitly decide whether 1.3.0 is acceptable or a major
version is needed for those API removals. Callers must now turn prose into a
route spec or `Trip` objects themselves.

- Skiplagged is USD only and does not convert. `occupancy_limit` is a provider
  room-type number, not proof of group capacity across several rooms.
- Google hostel prices may be dormitory beds; Google has no room type. Confirm
  finalists by exact name with `search_hotel_rooms`, then on the provider.
- A data-less Google status 13 may cause a guessed 2-minute cooldown without
  real throttling. On `rate_limited`, stop and wait 30–60 minutes without
  retrying or switching method.
- Google base/tax/fee breakdown (`record[6][2][44]`, unconfirmed) and a
  caller-named exchange rate remain outside this release.

## Publication decisions and follow-up

The publish workflow runs on pushed `v*` tags. PyPI depends on the build;
npm and MCP Registry both depend on PyPI and can run in parallel. The registry
job takes its version from the tag; the built Python and npm versions come
from the checked-in manifests, so they must match.

Human confirmation is required for the final version and whether to merge
`develop` into `main` first or tag the prepared `develop` commit as before.
`main` was 63 commits behind at the preparation base. No merge or tag is
authorized by this preparation.

Before claiming Skiplagged live readiness, repeat a single hotel search after
a 30–60 minute wait, retain its typed error if it fails, and only use a returned
`provider_id` for one room-rate smoke. Stop immediately on `rate_limited`.

README is intentionally untouched. Its owner should change “twelve tools” to
15, add `search_hotel_rooms`, `plan_stay_blocks`, and `split_stay_costs` to the
MCP table, add CLI `hotel-rooms` and opt-in Skiplagged hotels, describe `stays`
and `near`, and qualify mandatory hotel currency with the Skiplagged USD
exception. Explain unsupported cancellation and provider room-capacity limits.
