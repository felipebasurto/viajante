---
name: verify
description: Drive this viajante checkout the way a user does, through its `viajante` CLI and its stdio MCP server (`viajante-mcp`), and capture proof (command, stdout/stderr, exit code, saved JSON, MCP envelopes). Use to prove a flight, dates/flex/explore, hotel, local-tool, or MCP change works on the real app rather than only in the offline tests, or when asked to run, smoke-test, or verify viajante.
---

# Verify viajante

Viajante has no UI. A user touches it in two ways, and both run the same code:

- **CLI** (primary): `viajante <cmd>`, which prints tables and writes JSON only with `--save FILE`.
- **MCP server**: `viajante-mcp` over stdio (loopback Streamable HTTP is opt-in). An agent
  calls tools such as `search_flights`, and every tool except `lookup_airports` returns the
  shared envelope (`status`, `completeness`, `empty_reason`, `error_code`, ...).

There's also a Python library (`from viajante import get_flights`) and a static landing
page (`landing/index.html`), but they're secondary. This skill does not cover the landing page.

Read [features/README.md](features/README.md) before driving anything. It indexes one recipe
per feature.

## Before you start: live searches hit real providers

`flights`, `dates`, `flex`, `explore`, `hotels`, `trip`, `recheck-offer`, `watch`, and
`hidden-city` send real requests to Google, Booking, or Skiplagged from this machine's IP.
Nothing about this is mocked. A Google 429, a data-less RPC status 13, or three
public-page status-13 frames or missing Explore payloads inside 2 minutes sets a
**machine-wide cooldown** (2 min, or the `Retry-After` Google names, up to 30 min)
that blocks every viajante process, including the user's own MCP sessions. One
status-13 error frame does not.

- Run `doctor.sh` first. If its `live` line says `BLOCKED`, run no live search and report the
  time it gives.
- Keep live proofs small: one route, one date, `--top 3`. Each sweep already fans out
  internally (8 GETs in `standard` mode). A date window is one page per day.
- If a run prints `PROVIDER COOLDOWN recorded`, stop all live searches. Don't retry and don't
  switch `--fetch` mode. Report it.
- Pick dates in the future. Provider pages for past dates are empty or refused.
- Budget a session. About 8 live searches in 3 minutes was enough for Google to answer the
  Explore catalog with status 13. Prove the change you made, not the whole map, and space
  live recipes out.
- Offline proof (parsers, ranking, JSON keys) belongs to `uv run viajante bench` and the unit
  tests, not this skill. This skill is for the real path.

The `mcp__viajante__*` tools in your own session are **not this checkout**. They run an
installed copy (an npm, `uvx`, or git-main build, often an older version), so a result from
them proves nothing about local changes. Drive the checkout with the helpers below.

## Launch

There is no long-running server to start. A CLI run, or one stdio MCP session, is the whole app.

1. From the repo root, build the env once: `uv sync --locked`. The dev group includes `mcp`
   and `playwright`.
2. Only for `--fetch detail` or Booking hotels (the default `hotels --source`), you need
   Chromium: `uv run playwright install chromium`. `doctor.sh` reports whether it's present.
3. Choose a run dir outside the checkout. AGENTS.md forbids state and captures in the repo:

   ```bash
   RUN=~/.cache/viajante-verify/$(date +%Y%m%d-%H%M%S)-<feature>
   ```

4. Run every command through `drive.sh`. Each call is self-contained, and "ready" just means
   the command exited. Read `<label>.exit`.

The optional loopback HTTP server is the only background process. Its recipe, with PID
tracking, is in [features/mcp-server.md](features/mcp-server.md).

## Doctor

```bash
.claude/skills/verify/scripts/doctor.sh
```

This check is read-only. It verifies that `viajante --version` matches `pyproject.toml`, that
`import viajante` resolves into this checkout's `src/` (not an installed wheel),
`.venv/bin/viajante-mcp` exists, and Chromium is present. It also reports whether a Google
or Skiplagged cooldown is active in the real state dir (`live ok` or `live BLOCKED ...`).
It exits non-zero only when the checkout itself can't run. Run it first, and again whenever
a result looks like it came from the wrong version.

## Drive

```bash
.claude/skills/verify/scripts/drive.sh "$RUN" <label> <command...>
```

- `drive.sh` runs `<command>` with `uv run --locked` from the repo root, using
  `VIAJANTE_STATE_DIR=$RUN/state`. This isolates browser state, consent cookies, price
  history, and watches from the user's real `~/.local/state/viajante`, so two runs can go
  side by side.
- Provider cooldowns are deliberately **not** isolated. `drive.sh` copies the real cooldown
  files in before the run. If the run records a newer one, it copies that back to the real
  state dir so other processes back off too, and saves a copy as evidence.
- It writes `$RUN/evidence/<label>.cmd|.stdout|.stderr|.exit` and exits with the command's
  own code. Booking failure dumps (`booking-last-failure.*`) are copied into evidence.

CLI, for example a live one-way sweep:

```bash
.claude/skills/verify/scripts/drive.sh "$RUN" flights-oneway \
  viajante flights JFK-LHR:2026-11-18 --fetch sweep --top 3 --save "$RUN/evidence/flights-oneway.json"
```

MCP, using one stdio session against the checkout's server, with calls run in order:

```bash
.claude/skills/verify/scripts/drive.sh "$RUN" mcp-airports \
  python .claude/skills/verify/scripts/mcp_call.py \
  lookup_airports '{"query": "tokyo"}' \
  get_runtime_info '{}'
```

`mcp_call.py` prints one JSON line per step: `initialize` (server name and version),
`list_tools`, then one `call_tool` per call with `is_error`, `structured` (the envelope plus
payload), and `text` for errors. It exits 1 if any call is an MCP error. Calls share one
session, so `verify_answer` sees the earlier searches' evidence ledger.

Exit codes for CLI searches (`cli_report.py`): `0` every query succeeded, `3` some failed,
`2` all failed, `1` bad arguments (for example `hotels` without `--currency`). A capability
viajante refuses per query (checked bags on the public path, for example) is a failed query:
exit `2` with an `ERROR: Not sent. ...` line. `recheck-offer` uses `2` for "check not
completed", including `incomplete_identity`.

## Evidence

Everything lands in `$RUN/evidence/`, which is outside the checkout and survives cleanup.
Name the run dir in your report.

What a proof needs:

- **The real user path.** Use the CLI command or MCP tool a user would call, with the
  arguments they would pass. Don't import internal functions or call parsers on fixtures.
  That's what the bench is for.
- **Action and result.** Capture the `.cmd` file and the output: the printed table, the
  `--save` JSON, or the MCP `structured` envelope. A table alone isn't enough for a JSON-key
  change; save the JSON.
- **Read the envelope or status first.** For MCP, check `status`, `completeness`,
  `empty_reason`, and `error_code` before the rows. For CLI JSON, check each
  `queries[].status` and `error`. Never report `filtered_out` or `not_loaded` as "no flights
  found". Only `provider_empty` means that.
- **Side effects.** When a feature writes state (`watch`, price history with
  `VIAJANTE_PRICE_HISTORY=1`, `--save`), show the file in `$RUN/state` or `$RUN/evidence`
  with `cat` or `ls`, and read it back through a second user-facing command such as
  `viajante history` or `viajante watch --list`.
- **No-network paths.** Commands that claim to send nothing (`airports`, `awards`, `points`,
  `feedback --no-browser`, `validate_itinerary`, input refusals with exit 1) should finish
  instantly and leave no cooldown file. A refusal's proof is the exit code plus the message
  (`Not sent.` for a per-query refusal). The fact that a request fails doesn't prove nothing
  was sent. For a refusal, also check that `$RUN/state` gained no `google-*.json`.
- **Failures are results too.** `blocked`, `markup_drift`, and `fetch_failed` are real
  outcomes of the real path. Report them with the stderr and diagnostics. Don't retry them
  into a success.

## Cleanup

```bash
.claude/skills/verify/scripts/cleanup.sh "$RUN"
```

This kills only the PIDs listed in `$RUN/pids` (written when you background the HTTP
server) and deletes `$RUN/state`. It never touches `$RUN/evidence`, the real state dir, or
any other `viajante-mcp`: the user runs several installed copies, and they aren't yours.
Stdio MCP sessions and Playwright's Chromium exit with their command, so normally there's
nothing to kill. Run cleanup after failed attempts too.

## Helpers

All of these live in `.claude/skills/verify/scripts/`. Run them from the repo root.

| Script | Invocation | Does |
| --- | --- | --- |
| `doctor.sh` | `doctor.sh` | Read-only health check and live-search gate |
| `drive.sh` | `drive.sh <run-dir> <label> <command...>` | Runs one command against the run's state and saves the evidence |
| `mcp_call.py` | `drive.sh <run-dir> <label> python .claude/skills/verify/scripts/mcp_call.py <tool> '<json>' [...]` | One stdio MCP session against the checkout's server |
| `cleanup.sh` | `cleanup.sh <run-dir>` | Stops listed PIDs, removes `state/`, keeps `evidence/` |
