# 1.3.1 metadata release

Prepared on 2026-10-02. Version 1.3.0 is published on PyPI and npm, and the
published npx entry passed stdio smoke with all 15 tools and both local stay
tools. MCP Registry rejected its npm package because `mcpName` was missing.
The existing published npm version cannot be replaced.

Version 1.3.1 adds `mcpName: io.github.felipebasurto/viajante` to the npm
manifest and aligns all six version stamps. Runtime code, tools, and JSON
schemas are unchanged. The patch also includes the release workflow recovery
and npm-availability fixes already merged into `main`.

## Validation

- `uv sync --locked` passed.
- Full offline unittest suite: 1031 tests, OK.
- Ruff lint and formatting passed (66 files).
- `viajante bench`: `gate: ok`, `tests_ms: 301`, `parse_ms: 2`, `score_ms: 303`.
  No baseline change or score optimization.
- `uv build --no-sources` built the 1.3.1 wheel and sdist.
- A real npm tarball was built outside the checkout; its manifest contains
  version 1.3.1 and `mcpName` exactly matching the server's namespace.
- Real checkout MCP stdio: exactly 15 tools; both local stay tools passed.
  No new travel-provider requests were made for this patch.

## Publication completed

Version 1.3.1 and its tag were explicitly confirmed on 2026-10-02. PR #49
merged into `4862283f802081376a69bdff86709c83f109a0de`; merge CI run
`37005956186` passed Python 3.10–3.14, lint, and installed-wheel CLI/MCP smoke.
Tag `v1.3.1` points to that merge commit. The v1.3.0 tag remains unchanged.

Publish run [37006011348](https://github.com/felipebasurto/viajante/actions/runs/37006011348)
completed all four jobs successfully: build, PyPI, npm, and MCP Registry.
npm used the configured Trusted Publisher and signed provenance. The
availability gate waited for npm propagation before registering the server.

Authoritative public endpoints confirmed the PyPI wheel and sdist, npm version
1.3.1 with the required `mcpName`, and MCP Registry version 1.3.1 with matching
PyPI and npm transports. Real stdio smoke against both published `uvx` and
`npx -y @viajante/mcp@1.3.1` entries returned exactly 15 tools;
`plan_stay_blocks` and `split_stay_costs` passed synthetic roster and exact-cent
assertions. No travel-provider calls were made for this metadata patch.
The first `uvx` resolution used stale package-index metadata; `--refresh`
resolved and installed the published version successfully.

No version, merge, or tag decision remains pending. `main` and `develop` were
synchronized after publication.

Skiplagged live hotel search remains an upstream timeout; live room-rate smoke
is still unverified. README follow-up and all provider evidence limitations
remain in the 1.3.0 preparation notes.
