"""`python -m viajante`: the CLI for a sandbox where the console script is not on PATH."""

from viajante.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
