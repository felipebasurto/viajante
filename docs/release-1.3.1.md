# 1.3.1 metadata release proposal

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

## Pending approval and publication

No v1.3.1 tag has been created or pushed. The prepared branch/PR is reviewable;
publishing this new version requires explicit confirmation. After approval,
merge with passing CI, tag the merge commit, and verify PyPI, npm, MCP Registry,
and the published npx entry. The v1.3.0 tag remains unchanged.

Skiplagged live hotel search remains an upstream timeout; live room-rate smoke
is still unverified. README follow-up and all provider evidence limitations
remain in the 1.3.0 preparation notes.
