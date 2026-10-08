"""MCP entry (stdio, or opt-in local Streamable HTTP). Importable only with the mcp extra."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Annotated, Any, Callable, Literal, NoReturn, Optional, Sequence, TypeVar, Union

from viajante.control import SearchControl, active
from viajante.envelope import COMPLETENESS, OBSERVED_BASES, STATUSES, stamp_local
from viajante.explore import DEFAULT_EXPLORE_TOP
from viajante.flights import DEFAULT_TOP
from viajante.mcp_errors import structured_error, unknown_arguments_body, validation_body
from viajante.mcp_guide import GUIDE, INSTRUCTIONS
from viajante.mcp_handlers import (
    compare_awards_tool,
    get_hotel_details_tool,
    lookup_airports_tool,
    lookup_transfers_tool,
    plan_stay_blocks_tool,
    recheck_offer_tool,
    search_dates_tool,
    search_explore_tool,
    search_flex_tool,
    search_flights_tool,
    search_hidden_city_tool,
    search_hotel_rooms_tool,
    search_hotels_tool,
    search_split_tickets_tool,
    search_trip_tool,
    split_stay_costs_tool,
    validate_itinerary_tool,
    verify_answer_tool,
)
from viajante.models import EmptyReason
from viajante.runtime import get_runtime_info as runtime_info
from viajante.split import DEFAULT_MIN_CONNECTION_HOURS
from viajante.watch import price_history_tool, watch_price_tool

_T = TypeVar("_T")
_SEARCH_BUSY = threading.Lock()
_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viajante-mcp")
_SEARCH_BUSY_MESSAGE = "a viajante search is already running in this process"
_CANCEL_GRACE_SECONDS = 1.0
_PROGRESS_INTERVAL_SECONDS = 0.25
_INDEXED_PROGRESS = re.compile(r"^\s*\[(\d+)/(\d+)\]")
DEADLINE_ENV = "VIAJANTE_MCP_DEADLINE_SECONDS"
_DEADLINE_DOC = """
        deadline_seconds stops starting queries after that many seconds; unfinished
        queries are error code deadline (not loaded, not empty). See get_guide."""

_HELP = INSTRUCTIONS
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_TOOL_ERROR_PREFIX = "Error executing tool {name}: "
_USAGE = f"""\
viajante-mcp is the MCP server for local flight and hotel search (stdio by default).

usage: viajante-mcp [--transport {{stdio,streamable-http}}] [--host HOST] [--port PORT]

  --transport   stdio (default) or streamable-http (local, opt-in, no authentication)
  --host        streamable-http bind address (default {_DEFAULT_HOST})
  --port        streamable-http port (default {_DEFAULT_PORT}); the endpoint is /mcp

Install:  uvx --from 'git+https://github.com/felipebasurto/viajante.git[mcp]' viajante-mcp
Checkout: uv sync --extra mcp && viajante-mcp

Tools: search_flights, get_hotel_details, search_dates, search_flex, search_explore,
search_hotels, search_hotel_rooms, search_trip, search_split_tickets, lookup_airports,
search_hidden_city, compare_awards, lookup_transfers, validate_itinerary, recheck_offer,
plan_stay_blocks, split_stay_costs, verify_answer, price_history, watch_price,
get_runtime_info, get_guide.

Server instructions (the full guide is the viajante://guide resource):

"""


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


_CONTEXT_FACTORY: Optional[Callable[[], Any]] = None
_CURRENT: list = []
_ENV_DEADLINE: Optional[float] = None
_PROGRESS_WARNED = False
# Replaced in build_server by a strict number type (pydantic ships with the mcp extra).
_DeadlineSeconds: Any = Optional[float]


def env_deadline_seconds() -> Optional[float]:
    text = os.environ.get(DEADLINE_ENV, "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{DEADLINE_ENV} must be a positive number of seconds, got {text!r}")
    return value


class ProgressRelay:
    """Worker-thread progress strings to MCP notifications/progress on the server loop.

    ``[i/n]`` becomes progress i of total n; any other line is message-only and moves the
    value by a small step, so the value only ever increases and never passes the total
    (a line that cannot advance within the total is dropped). A per-day sweep sends
    ``[i/n]`` after that day finishes, not for a day the deadline cut. A round-trip date
    window reads groups of at most the sweep concurrency (that group's outbound boards,
    then those days' return pages) and still sends ``[i/n]`` only after that day finishes;
    other searches send it as the query starts. At most
    one notification per interval; the newest held line is flushed when the interval ends
    and when the search finishes. A notification that cannot be sent never breaks the search.
    """

    def __init__(self, ctx: Any, loop: asyncio.AbstractEventLoop, interval: float) -> None:
        self._ctx = ctx
        self._loop = loop
        self._interval = interval
        self._lock = threading.Lock()
        self._futures: list = []
        self._value: Optional[float] = None
        self._total: Optional[float] = None
        self._sent_at = -math.inf
        self._pending: Optional[tuple[float, Optional[float], str]] = None
        self._timer: Optional[threading.Timer] = None
        self._closed = False

    def __call__(self, text: str) -> None:
        message = " ".join(str(text).split())
        if not message:
            return
        with self._lock:
            if self._closed:
                return
            indexed = _INDEXED_PROGRESS.match(message)
            floor = 0.0 if self._value is None else self._value
            if indexed:
                self._total = float(indexed.group(2))
                value = max(float(indexed.group(1)), floor + 0.001)
            elif self._value is None:
                value = 0.0
            else:
                value = floor + 0.001
            if self._total is not None and value > self._total:
                return
            self._value = value
            self._pending = (value, self._total, message)
            wait = self._sent_at + self._interval - time.monotonic()
            if wait <= 0:
                self._send_locked()
            elif self._timer is None:
                self._timer = threading.Timer(wait, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            self._timer = None
            if not self._closed:
                self._send_locked()

    def _send_locked(self) -> None:
        if self._pending is None:
            return
        value, total, message = self._pending
        self._pending = None
        self._sent_at = time.monotonic()
        try:
            self._futures.append(
                asyncio.run_coroutine_threadsafe(self._report(value, total, message), self._loop)
            )
        except RuntimeError:
            self._closed = True

    async def _report(self, value: float, total: Optional[float], message: str) -> None:
        try:
            await self._ctx.report_progress(value, total, message)
        except Exception as exc:
            global _PROGRESS_WARNED
            if not _PROGRESS_WARNED:
                _PROGRESS_WARNED = True
                print(f"viajante: progress notification failed: {exc}", file=sys.stderr)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = None
            if self._timer is not None:
                self._timer.cancel()

    async def drain(self) -> None:
        with self._lock:
            if not self._closed:
                self._send_locked()
        self.close()
        await asyncio.gather(
            *(asyncio.wrap_future(f) for f in self._futures), return_exceptions=True
        )


def _progress_relay(loop: asyncio.AbstractEventLoop) -> Optional[ProgressRelay]:
    """A relay only when this request carries a progressToken; otherwise nothing is sent."""
    if _CONTEXT_FACTORY is None:
        return None
    try:
        ctx = _CONTEXT_FACTORY()
        meta = ctx.request_context.meta
    except (LookupError, ValueError, AttributeError):
        return None
    if meta is None or getattr(meta, "progressToken", None) is None:
        return None
    return ProgressRelay(ctx, loop, _PROGRESS_INTERVAL_SECONDS)


def _reraise(exc: ValueError, params: dict[str, object]) -> NoReturn:
    converted = structured_error(exc, params)
    if converted is exc:
        raise exc
    raise converted from exc


async def run_mcp_tool(fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
    """Run a search on the one-worker pool. Fail immediately if a search is in flight.

    Cancelling the request sets the search's cancel event, so the worker stops at its next
    checkpoint and releases the lock; a cancelled search returns, caches, and records nothing.
    """
    if "deadline_seconds" in kwargs and kwargs["deadline_seconds"] is None:
        kwargs["deadline_seconds"] = _ENV_DEADLINE
    loop = asyncio.get_running_loop()
    relay = _progress_relay(loop)
    control = SearchControl(progress=relay)
    finished = threading.Event()
    if not _SEARCH_BUSY.acquire(blocking=False):
        holder = _CURRENT[0] if _CURRENT else None
        if holder is None or not holder[0].cancel.is_set():
            _reraise(ValueError(_SEARCH_BUSY_MESSAGE), kwargs)
        # A cancelled search is unwinding: give it a moment instead of a spurious busy error.
        await loop.run_in_executor(None, holder[1].wait, _CANCEL_GRACE_SECONDS)
        if not _SEARCH_BUSY.acquire(blocking=False):
            _reraise(ValueError(_SEARCH_BUSY_MESSAGE), kwargs)
    _CURRENT[:] = [(control, finished)]

    def run() -> _T:
        try:
            with active(control):
                return fn(*args, **kwargs)
        finally:
            _CURRENT.clear()
            _SEARCH_BUSY.release()
            finished.set()

    try:
        future = loop.run_in_executor(_SEARCH_EXECUTOR, run)
    except BaseException:
        _CURRENT.clear()
        _SEARCH_BUSY.release()
        raise
    try:
        result = await asyncio.shield(future)
    except asyncio.CancelledError:
        control.cancel.set()
        if relay is not None:
            relay.close()
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        raise
    except ValueError as exc:
        if relay is not None:
            relay.close()
        _reraise(exc, kwargs)
    except BaseException:
        if relay is not None:
            relay.close()
        raise
    if relay is not None:
        await relay.drain()
    return result


async def run_lookup_tool(fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
    """Airport lookup stays off the search worker so it can run during a search."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))
    except ValueError as exc:
        _reraise(exc, kwargs)


def _compact_json(text: str) -> str:
    try:
        return json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=False)
    except ValueError:
        return text


def _unlabelled(node: Any) -> Any:
    """Drop the titles pydantic generates for every field; they only repeat the field name."""
    if isinstance(node, list):
        return [_unlabelled(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _unlabelled(prop) for name, prop in value.items()}
        else:
            out[key] = _unlabelled(value)
    return out


def _compact_blocks(result: Any) -> Any:
    """Re-serialize JSON text blocks without indentation. Keys and values are untouched."""
    if isinstance(result, tuple):
        return (_compact_blocks(result[0]), *result[1:])
    if isinstance(result, list):
        return [
            block.model_copy(update={"text": _compact_json(block.text)})
            if getattr(block, "type", None) == "text"
            else block
            for block in result
        ]
    return result


def _with_deadline_doc(fn: Callable[..., Any]) -> Callable[..., Any]:
    fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n" + _DEADLINE_DOC + "\n        "
    return fn


def _room_rates_flag(value: object) -> bool:
    # Reject before bool coercion, which would treat "yes" as true.
    if not isinstance(value, bool):
        raise ValueError("room_rates must be a boolean")
    return value


def _loopback_security(host: str):
    """Reject a foreign Host or Origin. The SDK only does this by itself from 1.23."""
    from mcp.server.transport_security import TransportSecuritySettings

    names = ["127.0.0.1", "localhost", "[::1]"]
    own = f"[{host.strip('[]')}]" if ":" in host else host
    if own not in names:
        names.append(own)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{name}:*" for name in names],
        allowed_origins=[f"http://{name}:*" for name in names],
    )


def build_server(*, host: Optional[str] = None, port: Optional[int] = None):
    global _ROOM_RATES, _CONTEXT_FACTORY, _ENV_DEADLINE, _DeadlineSeconds
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    from pydantic import BaseModel, BeforeValidator, ConfigDict, StrictFloat, StrictInt

    _ENV_DEADLINE = env_deadline_seconds()
    _DeadlineSeconds = Optional[Union[StrictInt, StrictFloat]]
    _ROOM_RATES = Annotated[bool, BeforeValidator(_room_rates_flag)]

    class ToolEnvelope(BaseModel):
        model_config = ConfigDict(extra="allow")

        status: Literal[STATUSES]
        completeness: Literal[COMPLETENESS]
        empty_reason: Optional[EmptyReason]
        empty_note: Optional[str]
        error_code: Optional[str]
        retry_after: Optional[str]
        retry_after_seconds: Optional[int]
        observed_at: Optional[str]
        observed_at_basis: Optional[Literal[OBSERVED_BASES]]

    class GuideEnvelope(ToolEnvelope):
        guide: str

    options: dict[str, object] = {}
    if host is not None:
        options = {"host": host, "port": port}
        if _is_loopback(host):
            options["transport_security"] = _loopback_security(host)

    class ViajanteServer(FastMCP):
        async def list_tools(self):
            return [
                tool.model_copy(
                    update={
                        "inputSchema": _unlabelled(tool.inputSchema),
                        "outputSchema": _unlabelled(tool.outputSchema),
                    }
                )
                for tool in await super().list_tools()
            ]

        async def call_tool(self, name, arguments):
            # The SDK rejects missing or mistyped arguments before any handler runs, with
            # pydantic text. Give those the same JSON body as a handler's ValueError.
            # An argument the tool does not declare is a misspelled filter, never ignored.
            declared = next(
                (
                    t.inputSchema.get("properties", {})
                    for t in await self.list_tools()
                    if t.name == name
                ),
                None,
            )
            unknown = sorted(set(arguments or {}) - set(declared)) if declared is not None else []
            if unknown:
                raise ValueError(
                    _TOOL_ERROR_PREFIX.format(name=name) + unknown_arguments_body(unknown)
                )
            try:
                return _compact_blocks(await super().call_tool(name, arguments))
            except Exception as exc:
                cause = exc.__cause__
                if not (isinstance(cause, ValueError) and callable(getattr(cause, "errors", None))):
                    raise
                body = validation_body(
                    cause.errors(include_url=False, include_context=False, include_input=False)
                )
                raise type(exc)(_TOOL_ERROR_PREFIX.format(name=name) + body) from cause

    server = ViajanteServer("viajante", instructions=_HELP, **options)
    _CONTEXT_FACTORY = getattr(server, "get_context", None)

    def tool(
        title: str,
        *,
        network: bool,
        envelope: bool = True,
        writes: bool = False,
        returns: type = ToolEnvelope,
    ):
        # Tools only read unless `writes`; openWorldHint is True only when the tool asks a
        # provider. A writing tool is neither read-only nor idempotent (it appends), but
        # it never deletes: destructiveHint stays False.
        register = server.tool(
            title=title,
            annotations=ToolAnnotations(
                readOnlyHint=not writes,
                destructiveHint=False,
                idempotentHint=not writes,
                openWorldHint=network,
            ),
        )

        def decorate(fn):
            if envelope:
                # `from __future__ import annotations` makes the return a string that cannot
                # see the class above; FastMCP only builds an outputSchema from a real annotation.
                fn.__annotations__["return"] = returns
            return register(fn)

        return decorate

    @server.resource(
        "viajante://guide",
        name="guide",
        title="viajante operational guide",
        description="Long operational rules for the viajante tools: evidence, currency, "
        "rate limits, hotels and stays.",
        mime_type="text/markdown",
    )
    def guide_resource() -> str:
        return GUIDE

    @tool("Runtime info", network=False)
    def get_runtime_info() -> dict:
        """Offline executing package, Python and hotel schema versions.

        Check before searches; an npm MCP does not upgrade a separate uv tool
        installation. May run during a search. No network or personal paths.
        """
        return stamp_local(dict(runtime_info()))

    @tool("Search flights", network=True)
    @_with_deadline_doc
    async def search_flights(
        routes: list[str],
        trip: str = "one-way",
        max_stops: int = 1,
        adults: int = 1,
        cabin: str = "economy",
        top: int = DEFAULT_TOP,
        fetch: str = "auto",
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        depart_window: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        max_duration: float | None = None,
        min_layover: float | None = None,
        max_layover: float | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        baggage_buffer: int | None = None,
        sort: str = "ranked",
        bags: int | None = None,
        carry_on: int | None = None,
        price_cap: int | None = None,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        currency: str | None = None,
        country: str | None = None,
        nearby: bool = False,
        proxy: str | None = None,
        deadline_seconds: _DeadlineSeconds = None,
    ) -> dict:
        """Google Flights search for named routes and dates. Each routes entry is
        ORIGIN-DEST:YYYY-MM-DD. Use search_dates for the cheapest week and search_flex for
        +/-N days. fetch=detail needs the browser extra and Chromium. Currency is currency, or
        inferred from a named origin's owned country; if unproven, ask. Viajante does not
        convert. Do not invent a bag fee. max_stops is 0, 1 or 2. Metro codes (LON, NYC)
        expand on one-way and rt routes. A successful query may carry recommendation:
        evidence, not a verdict. Check relaxed_requirements before calling it a match. After a
        named hub or leisure trunk, search_hidden_city may run once with the same route and
        date, never when bags were named. Rules and wording: get_guide.
        """
        return dict(await run_mcp_tool(search_flights_tool, **locals()))

    @tool("Hotel finalist details", network=True)
    async def get_hotel_details(selection_id: str, room_rates: _ROOM_RATES = False) -> dict:
        """Read a hotel offer this process returned. room_rates must be a boolean.

        False returns the stored quote, does not search, and may run during another
        search. True asks Skiplagged for a separate USD room quote and takes the
        one-search lock. That quote is not the original stay when the city matches
        more than one place, the returned coordinates do not match the hotel, or
        the provider echoes different adults, rooms, or dates (occupancy_mismatch,
        dates_mismatch, property_mismatch). A missing echo is echo unknown.
        When the provider does not echo adults, rooms, and dates, a returned
        quote is partial. A read does not evict stored searches. Unknown or
        evicted ids send nothing.
        """
        return dict(
            await (run_mcp_tool if room_rates else run_lookup_tool)(
                get_hotel_details_tool, selection_id=selection_id, room_rates=room_rates
            )
        )

    @tool("Cheapest-dates calendar", network=True)
    @_with_deadline_doc
    async def search_dates(
        route: str,
        start: str,
        end: str,
        max_stops: int = 1,
        adults: int = 1,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        cabin: str = "economy",
        trip: str = "one-way",
        nights: int | None = None,
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        bags: int | None = None,
        carry_on: int | None = None,
        price_cap: int | None = None,
        nearby: bool = False,
        depart_window: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        max_duration: float | None = None,
        min_layover: float | None = None,
        max_layover: float | None = None,
        currency: str | None = None,
        country: str | None = None,
        baggage_buffer: int | None = None,
        sort: str | None = None,
        proxy: str | None = None,
        deadline_seconds: _DeadlineSeconds = None,
    ) -> dict:
        """Cheapest-per-day calendar for one named route, up to 31 days. route is ORIGIN-DEST;
        start and end are ISO dates. Use search_flex for +/-N around one date. This calendar
        is HTTP only and has no fetch mode. If it returns blocked, stop: do not switch to a
        browser or follow with search_flex or search_flights. Currency and country as in
        search_flights. Viajante does not convert.
        """
        return dict(await run_mcp_tool(search_dates_tool, **locals()))

    @tool("Flexible-date flight search", network=True)
    @_with_deadline_doc
    async def search_flex(
        route: str,
        around: str,
        flex: int,
        max_stops: int = 1,
        adults: int = 1,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        cabin: str = "economy",
        trip: str = "one-way",
        nights: int | None = None,
        top: int = DEFAULT_TOP,
        baggage_buffer: int | None = None,
        sort: str = "ranked",
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        bags: int | None = None,
        carry_on: int | None = None,
        price_cap: int | None = None,
        nearby: bool = False,
        depart_window: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        max_duration: float | None = None,
        min_layover: float | None = None,
        max_layover: float | None = None,
        currency: str | None = None,
        country: str | None = None,
        proxy: str | None = None,
        deadline_seconds: _DeadlineSeconds = None,
    ) -> dict:
        """Flex window of +/-N days around one date, then one shopping search on the cheapest
        day. route is ORIGIN-DEST; around is an ISO date. Use search_dates for a cheapest-week
        calendar; do not brute-force a date matrix. Currency and country as in search_flights.
        A markup_drift day is not no_results: do not invent a cheapest week.
        A named-date search_flights is allowed.
        """
        return dict(await run_mcp_tool(search_flex_tool, **locals()))

    @tool("Explore destinations", network=True)
    @_with_deadline_doc
    async def search_explore(
        origin: str,
        start: str | None = None,
        days: int = 7,
        top: int = DEFAULT_EXPLORE_TOP,
        month: str | None = None,
        adults: int = 1,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        cabin: str = "economy",
        max_stops: int = 1,
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        exclude_regions: str | None = None,
        bags: int | None = None,
        carry_on: int | None = None,
        price_cap: int | None = None,
        nearby: bool = False,
        depart_window: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        max_duration: float | None = None,
        min_layover: float | None = None,
        max_layover: float | None = None,
        currency: str | None = None,
        country: str | None = None,
        sort: str = "price",
        baggage_buffer: int | None = None,
        proxy: str | None = None,
        deadline_seconds: _DeadlineSeconds = None,
    ) -> dict:
        """Destinations from one origin, then a priced shortlist. Give start (ISO date) or month,
        not both. Needs the browser extra; one adult in economy only. Currency and country as
        in search_flights. Viajante does not convert. A destination without a price is
        not_loaded, not a usable row.
        """
        return dict(await run_mcp_tool(search_explore_tool, **locals()))

    @tool("Search hotels", network=True)
    @_with_deadline_doc
    async def search_hotels(
        location: str | None = None,
        check_in: str | None = None,
        check_out: str | None = None,
        adults: int = 2,
        rooms: int = 1,
        top: int = DEFAULT_TOP,
        min_rating: float | None = None,
        entire_home: bool = False,
        free_cancellation: bool = True,
        source: str = "google",
        currency: str | None = None,
        stays: list[dict] | None = None,
        near: dict[str, float] | None = None,
        max_distance_km: float | None = None,
        deadline_seconds: _DeadlineSeconds = None,
    ) -> dict:
        """Hotel search. Quotes are total-stay prices, not per person. Currency is required (no
        origin airport), except source skiplagged, whose quotes are USD. Quotes come back in
        the requested currency as the provider returned them. Viajante does not convert and
        never invents ISO 4217. location is one named place: ask when it is a typo, a region,
        or several candidate towns. The request carries adults and rooms only; do not state a
        room split. Google offers are in price order, not advice: weigh location, latitude and
        longitude, and review_count yourself. stays batches up to 8
        location/check_in/check_out objects in one call. property_matrix lists each property's
        total per stay, null where it was not returned (not proof of unavailability), sorted
        by name. near is a point you name, {lat, lng}; max_distance_km needs it.
        entire_home=true asks for a house or villa; those offers carry sleeps, bedrooms and
        beds. lodging_evidence_conflict marks contradictory room or unit labels. Full rules:
        get_guide.
        """
        return dict(await run_mcp_tool(search_hotels_tool, **locals()))

    @tool("Hotel room rates", network=True)
    async def search_hotel_rooms(
        check_in: str,
        check_out: str,
        hotel_id: int | None = None,
        hotel_name: str | None = None,
        city: str | None = None,
        adults: int = 2,
        rooms: int = 1,
    ) -> dict:
        """Room rates for one Skiplagged hotel, for 1-3 finalists. Name it by hotel_id (the
        provider_id of a skiplagged search_hotels offer) or by hotel_name plus city. A name
        must match exactly (case, accents and punctuation ignored); no match or several
        matches is no_results, never a guess. Use the name form for a Google finalist: a
        Google hostel price can be a dorm bed, and these rates say what the room is. Rates are
        USD, not converted, in provider order. total_price is the whole stay for the party and
        rooms searched. occupancy_limit is per room type and is not proof that a party fits
        across rooms. Skiplagged only: do not mix these rows with Google or Booking prices.
        """
        return dict(await run_mcp_tool(search_hotel_rooms_tool, **locals()))

    @tool("Search flights and hotel", network=True)
    @_with_deadline_doc
    async def search_trip(
        routes: list[str],
        location: str,
        check_in: str | None = None,
        check_out: str | None = None,
        trip: str = "one-way",
        max_stops: int = 1,
        adults: int = 1,
        rooms: int = 1,
        cabin: str = "economy",
        top: int = DEFAULT_TOP,
        fetch: str = "auto",
        baggage_buffer: int | None = None,
        sort: str = "ranked",
        bags: int | None = None,
        carry_on: int | None = None,
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        price_cap: int | None = None,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        currency: str | None = None,
        country: str | None = None,
        min_rating: float | None = None,
        entire_home: bool = False,
        free_cancellation: bool = True,
        source: str = "google",
        nearby: bool = False,
        deadline_seconds: _DeadlineSeconds = None,
    ) -> dict:
        """Flights then one hotel stay, under one lock. Currency follows the flight origin or an
        explicit code; if unproven, ask. Viajante does not convert. The same currency goes to
        the hotel. Stay dates default to the flights' window. Unnamed baggage_buffer is 0. A
        positive carry_on is verified on the page; named bags are refused. Flight queries
        carry the same recommendation block as search_flights. trip_total is omitted if either
        side missed.
        """
        return dict(await run_mcp_tool(search_trip_tool, **locals()))

    @tool("Split-ticket itineraries", network=True)
    async def search_split_tickets(
        route: str,
        trip: str = "one-way",
        max_stops: int = 1,
        adults: int = 1,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        cabin: str = "economy",
        top: int = DEFAULT_TOP,
        fetch: str = "sweep",
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        bags: int | None = None,
        carry_on: int | None = None,
        price_cap: int | None = None,
        via: str | None = None,
        max_hubs: int | None = None,
        min_connection_hours: float = DEFAULT_MIN_CONNECTION_HOURS,
        allow_overnight: bool = False,
        leg_max_stops: int = 0,
        currency: str | None = None,
        country: str | None = None,
        proxy: str | None = None,
    ) -> dict:
        """Opt-in separately ticketed itineraries built from real one-way quotes. route is
        ORIGIN-DEST:DATE (one-way: self-transfer via a hub) or, with trip="rt",
        ORIGIN-DEST:OUT:BACK (cheapest outbound plus cheapest return one-way). It runs the
        packaged search first (packaged_report), then up to max_hubs hubs (default 3, at most
        5): 2 queries per hub, 3 with allow_overnight. via lists up to 5 connection airports;
        unnamed, hubs are the packaged layover airports. It stops at a recorded rate limit: do
        not retry. Currency as in search_flights. Every itinerary says split_ticket true and
        connection_protected false. A missed connection between tickets is not rebooked, and
        bags may need re-checking. Each ticket is confirmed on its own google_flights_url.
        min_connection_hours (default 3) is a planning default, not provider evidence. total
        is null unless every part shares one currency. Not for multi-city. Full rules:
        get_guide.
        """
        return dict(await run_mcp_tool(search_split_tickets_tool, **locals()))

    @tool("Look up airports", network=False, envelope=False)
    async def lookup_airports(query: str, limit: int = 20) -> list:
        return await run_lookup_tool(lookup_airports_tool, **locals())

    @tool("Search hidden-city fares", network=True)
    async def search_hidden_city(
        route: str,
        departure: str,
        return_date: str | None = None,
        adults: int = 1,
        top: int = DEFAULT_TOP,
        currency: str | None = None,
    ) -> dict:
        """Skiplagged search for a named route. Opt-in; never mixed with Google Flights. Run it
        once after search_flights on a named common route and date. Skip it for named bags,
        explore, dates, flex, multi-city and unproven destinations. Hidden-city tickets can
        violate airline contracts. Lead with hidden_city true rows and confirm on booking_url;
        Viajante does not book. currency is an optional keep of the card currency: Skiplagged
        cards are USD, so omit it or pass USD. A keep that matches no card is
        currency_mismatch. Nothing is converted.
        """
        return dict(await run_mcp_tool(search_hidden_city_tool, **locals()))

    @tool("Compare award to cash", network=False)
    async def compare_awards(
        offer: dict,
        cash_price: float | None = None,
        currency: str | None = None,
        balances: list | None = None,
    ) -> dict:
        """Compare a named award offer to cash locally. Does not invent seats.

        offer must include origin, destination, departure_date, program, points,
        and evidence. confirmed evidence needs a named source. Unnamed cash_price
        omits cpp_cents. Do not treat estimated or user_supplied as live inventory.
        """
        return dict(await run_lookup_tool(compare_awards_tool, **locals()))

    @tool("Look up point transfers", network=False)
    async def lookup_transfers(
        program: str,
        points: int,
        balances: list | None = None,
    ) -> dict:
        """Local card-to-program transfer table. Not live award availability."""
        return dict(await run_lookup_tool(lookup_transfers_tool, **locals()))

    @tool("Validate itinerary", network=False)
    async def validate_itinerary(
        legs: list[dict],
        constraints: dict,
        currency: str | None = None,
    ) -> dict:
        """Validate selected v2 flight offers locally, without fetching. Pass legs in travel
        order, each with its exact query and one selected offer. The result is tri-state:
        unknown evidence never becomes pass. arrival_deadline is an ISO date and time: an
        explicit offset is compared in UTC, a naive time is local at the arrival airport.
        chronological checks owned segment instants. min_stay_days and max_stay_days use owned
        journey dates.
        """
        return dict(await run_lookup_tool(validate_itinerary_tool, **locals()))

    @tool("Re-check offer", network=True)
    async def recheck_offer(
        offer: dict,
        query: dict | None = None,
        currency: str | None = None,
        country: str | None = None,
        fetch: str | None = None,
        proxy: str | None = None,
        allow_loose_match: bool = False,
        allow_substitute: bool = False,
    ) -> dict:
        """Re-check an earlier flight offer with one fresh Google Flights search (never the
        replay cache). offer is a prior search_flights offer, or a {query, offer} row, or
        enough of one: price plus every segment's flight_number, origin, destination and
        departure clock. query defaults to the offer's evidence query and is replayed (cabin,
        stops, bags, airline filters). A price_cap is not sent; a break is reported in
        filter_violations. Read the outcome: check_failed means the check did not run, never
        that the offer is gone. Confirm the price on the provider's page; this is not a
        booking guarantee. Full rules: get_guide.
        """
        return dict(await run_mcp_tool(recheck_offer_tool, **locals()))

    @tool("Plan stay blocks", network=False)
    async def plan_stay_blocks(roster: dict[str, list[str]]) -> dict:
        """Local: group consecutive nights with the same people into blocks.

        roster maps each night (YYYY-MM-DD, the night that starts that day) to the
        names sleeping. Nights must be consecutive. Each block gives check_in,
        check_out, nights, headcount and people, so one search_hotels stay per block
        can use headcount as adults. Never decides where anyone sleeps.
        """
        return dict(await run_lookup_tool(plan_stay_blocks_tool, **locals()))

    @tool("Split stay costs", network=False)
    async def split_stay_costs(
        stays: list[dict],
        roster: dict[str, list[str]],
        currency: str,
        fee_per_person_night: float | None = None,
    ) -> dict:
        """Local: split each stay's total among the people who sleep there.

        stays are {name, check_in, check_out, total}; roster is as for
        plan_stay_blocks. A stay's rate is its total over the person-nights the
        roster puts inside it, so someone who never sleeps there pays nothing toward
        it. currency is named by the caller and never converted. fee_per_person_night
        is a per-night charge the caller names (a city tax) added per person. Cents
        are allocated so each stay sums exactly. Nights no stay covers come back as
        unallocated_nights. Arithmetic only: it does not price or recommend a stay.
        """
        return dict(await run_lookup_tool(split_stay_costs_tool, **locals()))

    @tool("Verify draft answer", network=False)
    async def verify_answer(answer: str) -> dict:
        """Check a draft reply against this process's recent search payloads.

        Call before sending a reply that quotes fares, stays, dates, airport
        codes, or links. Lists each amount, currency, IATA code, ISO date, and
        URL in the draft that no recorded search returned. Drop or re-search
        every unowned row; do not quote it as found. Sums you computed are
        unowned unless a payload carries them (e.g. trip_total). Local; may run
        during a search.
        """
        return dict(await run_lookup_tool(verify_answer_tool, **locals()))

    @tool("Price history", network=False)
    async def price_history(
        kind: str | None = None,
        route: str | None = None,
        date: str | None = None,
        location: str | None = None,
        query_key: str | None = None,
        currency: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Local read of prices this machine observed (opt-in: VIAJANTE_PRICE_HISTORY=1 in the
        server environment). One exact query in one currency is a series: first_seen,
        last_seen, lowest, highest, and the change since the previous observation. One
        observation reports no trend. Currencies are never merged or converted, and nothing is
        forecast. route is ORIGIN-DEST; date matches a departure, return, check-in or
        check-out; location names a hotel stay. An unreadable log gives read_error and series
        null: unknown, not empty. May run during a search.
        """
        return dict(await run_lookup_tool(price_history_tool, **locals()))

    @tool("Watch a price", network=True, writes=True)
    async def watch_price(
        name: str | None = None,
        kind: str | None = None,
        params: dict | None = None,
    ) -> dict:
        """Re-run a saved flight or hotel search and report the change. With no arguments, list
        saved watches (watches null and status failed when the file cannot be read). With
        name, kind (flight or hotel) and params (the search_flights or search_hotels
        arguments): validate, save, then run. Saving under an existing name replaces that
        watch. With only name, run the saved search. The run records an observation even when
        the history opt-in is off. A cached or rate-limited run records nothing. No scheduler
        and no notification: do not call it in a loop.
        """
        return dict(await run_mcp_tool(watch_price_tool, **locals()))

    @tool("Operational guide", network=False, returns=GuideEnvelope)
    def get_guide() -> dict:
        """The long operational guide (markdown); the same text as the viajante://guide resource.

        For clients that do not read MCP resources. Returns {"guide": markdown}. Local;
        may run during a search.
        """
        return stamp_local({"guide": GUIDE})

    return server


def _parse_args(args: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="viajante-mcp", add_help=False)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parsed = parser.parse_args(args)
    if parsed.transport == "stdio" and (parsed.host is not None or parsed.port is not None):
        parser.error("--host and --port apply only to --transport streamable-http")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return parsed


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in {"-h", "--help"} for arg in args):
        # Help must work without the mcp extra.
        print(_USAGE + _HELP.strip())
        return
    parsed = _parse_args(args)
    http = parsed.transport == "streamable-http"
    host = parsed.host or _DEFAULT_HOST
    port = parsed.port or _DEFAULT_PORT
    if http and not _is_loopback(host):
        print(
            f"warning: binding to {host}, not loopback. viajante-mcp has no authentication; "
            "every client that can reach this port searches from this machine's IP, and the "
            "machine-wide provider cooldown applies to all of them.",
            file=sys.stderr,
        )
    try:
        server = build_server(host=host, port=port) if http else build_server()
    except ValueError as exc:
        raise SystemExit(f"viajante-mcp: {exc}") from exc
    except ImportError as exc:
        raise SystemExit(
            "viajante-mcp requires the mcp extra. Install with: uv sync --extra mcp"
        ) from exc
    server.run(transport=parsed.transport)


if __name__ == "__main__":
    main()
