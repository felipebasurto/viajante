"""CLI for the local price history and on-demand watch (parsers and runners)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from viajante.history import (
    ENV_RECORD,
    HISTORY_FILE,
    MAX_ENTRIES,
    clear_history,
    price_history,
)
from viajante.storage import write_json_atomic
from viajante.watch import list_watches, remove_watch, watch_price_tool

HISTORY_EXAMPLES = f"""\
Recording is off by default. Opt in for every search (CLI and MCP) with:
  export {ENV_RECORD}=1
Disable it by unsetting the variable (or setting it to 0). The log is
{HISTORY_FILE} in the state directory, capped at {MAX_ENTRIES} entries.

Examples:
  viajante history --route JFK-LHR --date 2027-03-01
  viajante history --kind hotel --location "Lisbon"
  viajante history --clear
"""

WATCH_EXAMPLES = """\
Examples:
  viajante watch --list
  viajante watch jfk-lhr --kind flights --params '{"routes": ["JFK-LHR:2027-03-01"]}'
  viajante watch jfk-lhr
  viajante watch jfk-lhr --remove

A watch runs only when you run it: viajante sends no notifications and has no
scheduler. To check at a low frequency, call `viajante watch NAME` from your own
cron or agent no more than a few times a day; Google rate-limits, and a cooldown
or the 5-minute cache means a run records nothing.
"""


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f} {currency}"


def add_parsers(sub: Any) -> None:
    history = sub.add_parser(
        "history",
        help="Prices this machine observed for a query (local; opt-in recording)",
        description="Read the local price log. No network; never predicts or converts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HISTORY_EXAMPLES,
    )
    history.add_argument("--kind", choices=("flight", "hotel"), default=None)
    history.add_argument("--route", default=None, metavar="ORIGIN-DEST", help="e.g. JFK-LHR")
    history.add_argument(
        "--date", default=None, metavar="YYYY-MM-DD", help="A departure, return or stay date"
    )
    history.add_argument("--location", default=None, help="Hotel location as searched")
    history.add_argument("--query-key", default=None, help="Exact query key from a prior listing")
    history.add_argument("--currency", default=None, help="Only observations in this currency")
    history.add_argument("--limit", type=int, default=20, help="Observations shown per series")
    history.add_argument("--clear", action="store_true", help="Delete the whole local log")
    history.add_argument("--save", default=None, metavar="FILE", help="Write the JSON atomically")

    watch = sub.add_parser(
        "watch",
        help="Re-run a saved flight or hotel query now and report the change",
        description="User-triggered re-run of a saved query. No scheduler, no notifications.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=WATCH_EXAMPLES,
    )
    watch.add_argument("name", nargs="?", default=None)
    watch.add_argument("--list", action="store_true", help="List saved watches")
    watch.add_argument("--remove", action="store_true", help="Delete the named watch")
    watch.add_argument("--kind", choices=("flights", "hotels"), default=None)
    watch.add_argument("--params", default=None, metavar="JSON", help="search_* arguments to save")
    watch.add_argument("--params-file", default=None, metavar="FILE")
    watch.add_argument("--save", default=None, metavar="FILE", help="Write the JSON atomically")


def _emit(args: argparse.Namespace, payload: Mapping[str, Any]) -> None:
    if args.save:
        write_json_atomic(payload, Path(args.save))
        print(f"\nSaved {args.save}")


def _describe(item: Mapping[str, Any]) -> str:
    query = item["query"]
    if item["kind"] == "hotel":
        return (
            f"{query['location']} {query['check_in']} to {query['check_out']} "
            f"({query['adults']} adults, {query['rooms']} rooms)"
        )
    dates = query["departure_date"] + (
        f" / {query['return_date']}" if "return_date" in query else ""
    )
    route = f"{query['origin']}-{query['destination']}"
    return f"{route} {dates} ({query['trip']}, {query['adults']} adults)"


def _print_change(change: Mapping[str, Any] | None, currency: str) -> None:
    if change is None:
        return
    print(
        f"    change since {change['previous']['observed_at']}: "
        f"{change['price_change']:+,.2f} {currency} "
        f"({change['percent']:+.1f}%, {change['direction']})"
    )


def run_history(args: argparse.Namespace) -> int:
    if args.clear:
        try:
            valid, unreadable = clear_history()
        except OSError as exc:
            print(f"error: could not clear the price history: {exc}", file=sys.stderr)
            return 1
        noun = "observation" if valid == 1 else "observations"
        extra = ""
        if unreadable:
            extra = f" and {unreadable} unreadable line{'' if unreadable == 1 else 's'}"
        print(f"Cleared {valid} {noun}{extra}.")
        return 0
    try:
        payload = price_history(
            kind=args.kind,
            route=args.route,
            date=args.date,
            location=args.location,
            query_key=args.query_key,
            currency=args.currency,
            limit=args.limit,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if payload.get("read_error"):
        print(f"error: {payload['note']}", file=sys.stderr)
        return 1
    if not payload["series"]:
        print(payload["note"])
    for item in payload["series"]:
        trend = item["trend"]
        currency = item["currency"]
        print(f"\n=== {item['kind']} {_describe(item)}  [{currency}]  key {item['query_key']} ===")
        for obs in item["observations"]:
            label = f"  {obs['cheapest_label']}" if obs["cheapest_label"] else ""
            print(
                f"  {obs['observed_at']}  {_money(obs['cheapest'], currency):>16}  "
                f"{obs['offers']} offers  {obs['provider']}{label}"
            )
        if item["observations_omitted"]:
            print(f"  ({item['observations_omitted']} older observations not shown)")
        print(
            f"  first {_money(trend['first_seen']['cheapest'], currency)}  "
            f"last {_money(trend['last_seen']['cheapest'], currency)}  "
            f"lowest {_money(trend['lowest']['cheapest'], currency)}  "
            f"highest {_money(trend['highest']['cheapest'], currency)}"
        )
        _print_change(trend["change_since_previous"], currency)
        print(f"  {trend['note']}")
    _emit(args, payload)
    return 0


def run_watch(args: argparse.Namespace) -> int:
    try:
        if args.list or args.name is None:
            rows = list_watches()
            for row in rows:
                print(f"{row['name']}  {row['kind']}  saved {row['saved_at']}")
            if not rows:
                print("No saved watches.")
            _emit(args, {"watches": rows})
            return 0
        if args.remove:
            print(f"Removed {args.name}." if remove_watch(args.name) else f"No watch {args.name}.")
            return 0
        raw = (
            Path(args.params_file).read_text(encoding="utf-8") if args.params_file else args.params
        )
        params = json.loads(raw) if raw is not None else None
        if params is not None and not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
        result = watch_price_tool(args.name, kind=args.kind, params=params)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for item in result["results"]:
        currency = item["currency"]
        current = item["current"]
        print(
            f"\n{item['kind']} {_describe(item)}: "
            f"{_money(current['cheapest'], currency)} at {current['observed_at']}"
        )
        _print_change(item["change"], currency)
        if item.get("note"):
            print(f"  {item['note']}")
    for error in result["errors"]:
        print(f"error: {error['code']}: {error['message']}", file=sys.stderr)
    if result.get("note"):
        print(result["note"])
    _emit(args, result)
    return 2 if result["errors"] and not result["results"] else 0
