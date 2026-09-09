# Contributing to Viajante

Thanks for taking the time to contribute. Documentation fixes, bug reports,
and focused code changes are all welcome. For a larger change, open an issue
first so we can discuss the behavior and scope.

## Local setup

Install Python 3.10 or later and [uv](https://docs.astral.sh/uv/), then:

```bash
git clone https://github.com/felipebasurto/viajante.git
cd viajante
uv sync --locked
```

The development environment includes the MCP and Playwright dependencies.
Chromium is only needed for manual browser searches:

```bash
uv run playwright install chromium
```

## Check your changes

Run the offline gate from the checkout:

```bash
uv run viajante bench
```

It runs the unit tests, lint and formatting checks, and the checked-in parser
fixtures. It does not start Chromium or make live travel searches by default.
The gate needs `tests/bench/` and is not intended for an installed wheel.

For a focused check, you can run the tools separately:

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
uv run ruff format --check src tests
```

Changes to natural-language planning should also run the prompt battery:

```bash
uv run viajante bench --prompts
```

LLM judging and live benchmarks are opt-in. An offline pass verifies the
local code and fixtures; it does not establish that a provider's current
pages still work. Read [program.md](program.md) before benchmark experiments
and leave the baseline and human holdout unchanged.

## Before opening a pull request

- Keep the change focused and describe the user-visible behavior it fixes.
- For code changes, add an offline regression test when it helps demonstrate
  the bug. Keep network access and Chromium out of tests.
- Preserve report fields and the distinction between requested filters,
  observed information, and missing data.
- Include the checks you ran and any remaining limitations in the PR.
- Keep credentials, browser sessions, reservations, personal travel reports,
  and raw live-search captures out of commits and public issues.

The module map and repository conventions are in [AGENTS.md](AGENTS.md).

## Reporting a bug

[Open an issue](https://github.com/felipebasurto/viajante/issues) with your
Python and Viajante versions, the command or API call, the expected behavior,
and the error you received. Remove private travel details and credentials
before sharing logs.
