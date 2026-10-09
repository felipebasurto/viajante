"""Opt-in Skiplagged MCP client. No Google mix-in. Does not book.

Shape verified against live captures (tests/fixtures/skiplagged, 2026-10-08):
sk_flights_search returns structuredContent.flights[] cards with price
{amount, currency}, departure/arrival {airport, dateTime}, layovers (int),
attributes (hidden-city / standard / nonstop / one-stop) and deepLink.
Layover airport comes from the markdown table in the same reply.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from viajante.airports import is_known_iata
from viajante.control import (
    SearchDeadline,
    check_cancelled,
    checkpoint,
    controlled,
    interruptible_sleep,
)
from viajante.models import (
    FETCH_LANGUAGE,
    HIDDEN_CITY_WARNINGS,
    FlightQuery,
    HiddenCityOffer,
    HiddenCityReport,
    SearchError,
    SearchErrorCode,
    normalize_currency,
)
from viajante.ratelimit import (
    SKIPLAGGED_RATE_LIMIT_FILE,
    cooldown_until,
    note_rate_limited,
    rate_limit_advice,
    rate_limit_status,
)

SKIPLAGGED_MCP_URL = "https://mcp.skiplagged.com/mcp"
SKIPLAGGED_FLIGHTS_TOOL = "sk_flights_search"
MAX_SEARCH_ADULTS = 9  # sk_flights_search inputSchema: adults maximum 9
_PROTOCOL = "2025-03-26"
_TIMEOUT_SECONDS = 30
_SESSION_LOCK = threading.Lock()
_SESSION_IDS: dict[tuple[object, str], str] = {}
RpcPost = Callable[[str, dict[str, Any], Mapping[str, str]], tuple[int, Mapping[str, str], str]]


class SkiplaggedError(RuntimeError):
    """Skiplagged MCP request failed."""


class SkiplaggedRateLimited(SkiplaggedError):
    """Skiplagged answered HTTP 429. Retrying right away only extends the block."""


class SkiplaggedShapeError(SkiplaggedError):
    """Skiplagged answered, but not in the shape the captured payloads have."""


# ponytail: Skiplagged publishes no quota. A burst of calls got an HTTP 429 on session start;
# one second between live calls is a guess. Upgrade: learn it from observed recoveries.
MIN_CALL_INTERVAL_SECONDS = 1.0
_PACE_LOCK = threading.Lock()
_LAST_CALL = [0.0]


def _is_live(rpc: RpcPost) -> bool:
    # Injected test transports skip the cooldown file and pacing so they never touch real state.
    return rpc is _rpc_post


def _guard_cooldown(rpc: RpcPost) -> None:
    if not _is_live(rpc):
        return
    state = rate_limit_status(file=SKIPLAGGED_RATE_LIMIT_FILE)
    if state is not None:
        advice = rate_limit_advice(state, sent=False, provider="Skiplagged")
        raise SkiplaggedRateLimited(advice)


def _pace(rpc: RpcPost) -> None:
    if not _is_live(rpc):
        return
    with _PACE_LOCK:
        wait = _LAST_CALL[0] + MIN_CALL_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            interruptible_sleep(wait)
        _LAST_CALL[0] = time.monotonic()


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return str(value)
    return ""


def _check_status(status: int, rpc: RpcPost, headers: Mapping[str, str], url: str) -> None:
    check_cancelled()
    if status != 429:
        return
    try:
        retry_after: Optional[float] = float(_header(headers, "retry-after"))
    except ValueError:
        retry_after = None
    if _is_live(rpc):
        state = note_rate_limited(retry_after, file=SKIPLAGGED_RATE_LIMIT_FILE, endpoint=url)
        raise SkiplaggedRateLimited(
            rate_limit_advice(state, provider="Skiplagged", reason="HTTP 429")
        )
    raise SkiplaggedRateLimited("Skiplagged is rate-limiting this machine (HTTP 429).")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _headers(*, session_id: Optional[str] = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": _PROTOCOL,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _rpc_post(
    url: str,
    payload: dict[str, Any],
    headers: Mapping[str, str],
) -> tuple[int, Mapping[str, str], str]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            return int(response.status), dict(response.headers.items()), body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return int(exc.code), dict(exc.headers.items() if exc.headers else ()), body


def _sse_json(body: str) -> Any:
    text = body.strip()
    if text.startswith("{"):
        return json.loads(text)
    chunks: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            chunks.append(line[5:].strip())
    if not chunks:
        raise SkiplaggedError("Skiplagged MCP returned no JSON payload.")
    return json.loads("\n".join(chunks))


def _rpc_result(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise SkiplaggedError("Skiplagged MCP returned a non-object payload.")
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "Skiplagged MCP error")
        raise SkiplaggedError(message)
    if "result" not in payload:
        raise SkiplaggedError("Skiplagged MCP returned no result.")
    return payload["result"]


def _handshake(rpc: RpcPost, url: str) -> str:
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "viajante", "version": "1.0.0"},
        },
    }
    status, headers, body = rpc(url, init_payload, _headers())
    _check_status(status, rpc, headers, url)
    if status >= 400:
        raise SkiplaggedError(f"Skiplagged MCP initialize failed ({status}).")
    _rpc_result(_sse_json(body))
    session_id = _header(headers, "mcp-session-id")
    notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    status, notify_headers, _notify_body = rpc(url, notify, _headers(session_id=session_id or None))
    _check_status(status, rpc, notify_headers, url)
    if status >= 400:
        raise SkiplaggedError(f"Skiplagged MCP initialize failed ({status}).")
    return session_id


def _session_id(rpc: RpcPost, url: str) -> str:
    key = (rpc, url)
    with _SESSION_LOCK:
        cached = _SESSION_IDS.get(key)
        if cached is not None:
            return cached
        session_id = _handshake(rpc, url)
        _SESSION_IDS[key] = session_id
        return session_id


def _drop_session(rpc: RpcPost, url: str) -> None:
    with _SESSION_LOCK:
        _SESSION_IDS.pop((rpc, url), None)


def _post_call(rpc: RpcPost, url: str, payload: dict[str, Any], session_id: str) -> tuple[int, str]:
    status, headers, body = rpc(url, payload, _headers(session_id=session_id or None))
    _check_status(status, rpc, headers, url)
    return status, body


def _call_mcp(
    arguments: Mapping[str, Any],
    *,
    rpc: RpcPost,
    url: str = SKIPLAGGED_MCP_URL,
    tool: str = SKIPLAGGED_FLIGHTS_TOOL,
) -> Any:
    checkpoint()
    _guard_cooldown(rpc)
    _pace(rpc)
    session_id = _session_id(rpc, url)
    call_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool, "arguments": dict(arguments)},
    }
    status, body = _post_call(rpc, url, call_payload, session_id)
    if status in {400, 404} and session_id:
        _drop_session(rpc, url)
        session_id = _session_id(rpc, url)
        status, body = _post_call(rpc, url, call_payload, session_id)
    if status >= 400:
        raise SkiplaggedError(f"Skiplagged MCP search failed ({status}).")
    return _rpc_result(_sse_json(body))


def _result_text(result: Any) -> str:
    """Join the markdown text blocks of a tool result (the table the card list is shown in)."""
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        item["text"]
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return "\n".join(parts)


def _flight_cards(result: Any) -> list[Any]:
    """The `structuredContent.flights` cards of a successful sk_flights_search call.

    Captured 2026-10-08: every reply carries `structuredContent` with `flights`
    (a list, possibly empty) and `pagination`. Anything else is a shape change.
    """
    if not isinstance(result, dict):
        raise SkiplaggedShapeError("Skiplagged MCP returned a non-object result.")
    if result.get("isError") is True:
        raise SkiplaggedError(_result_text(result) or "Skiplagged MCP tool reported an error.")
    structured = result.get("structuredContent")
    flights = structured.get("flights") if isinstance(structured, dict) else None
    if not isinstance(flights, list):
        raise SkiplaggedShapeError("Skiplagged flight search returned no flights list.")
    return flights


_LAYOVER_CITY = re.compile(r"Layover in ([A-Z]{3})\b")
_BOOK_TRIP = re.compile(r"\]\([^)\s]*#(trip=[^)\s]+)\)")


def _layover_by_trip(text: str) -> dict[str, Optional[str]]:
    """Layover airport per `#trip=` fragment, read from the markdown table rows.

    The card has no layover field. Each table row's Book link carries the same
    `#trip=` fragment as the card's `deepLink`. A row with other than exactly one
    layover, or a fragment that two rows disagree on, maps to None (unknown).
    """
    found: dict[str, Optional[str]] = {}
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        link = _BOOK_TRIP.search(line)
        if link is None:
            continue
        codes = _LAYOVER_CITY.findall(line)
        code = codes[0] if len(codes) == 1 else None
        key = link.group(1)
        if key in found and found[key] != code:
            found[key] = None
        else:
            found.setdefault(key, code)
    return found


def _iata_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) == 3 and is_known_iata(code):
        return code
    return None


def _leg_airport(leg: Any) -> Optional[str]:
    if isinstance(leg, dict):
        return _iata_or_none(leg.get("airport"))
    return None


def _attribute_tokens(value: Any) -> Optional[set[str]]:
    if not isinstance(value, list):
        return None
    return {str(item).strip().casefold() for item in value}


def _hidden_city(card: Mapping[str, Any], return_leg: Any) -> Optional[bool]:
    """True or False from the legs' ``attributes``; None when no leg carries them."""
    legs = [card, return_leg] if isinstance(return_leg, dict) else [card]
    seen = [
        tokens for leg in legs if (tokens := _attribute_tokens(leg.get("attributes"))) is not None
    ]
    if not seen:
        return None
    return any("hidden-city" in tokens for tokens in seen)


def _stops(card: Mapping[str, Any]) -> Optional[int]:
    count = card.get("layovers")
    if isinstance(count, bool) or not isinstance(count, (int, float)) or count < 0:
        return None
    return int(count)


def _offer_from_card(
    card: Any,
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: Optional[date],
    layover_by_trip: Mapping[str, Optional[str]],
) -> Optional[HiddenCityOffer]:
    if not isinstance(card, dict):
        return None
    price_block = card.get("price")
    if not isinstance(price_block, dict):
        return None
    amount = price_block.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        return None
    currency = normalize_currency(price_block.get("currency"))
    row_origin = _leg_airport(card.get("departure")) or origin
    row_dest = _leg_airport(card.get("arrival")) or destination
    return_leg = card.get("returnFlight")
    deep_link = card.get("deepLink")
    deep_link = deep_link.strip() if isinstance(deep_link, str) and deep_link.strip() else None
    fragment = urlparse(deep_link).fragment if deep_link else ""
    airline = card.get("airlines")
    duration = card.get("duration")
    stops_count = _stops(card)
    layover_city = layover_by_trip.get(fragment)
    if isinstance(return_leg, dict):
        # The card's own fields describe the outbound. A round-trip value is reported only
        # when the return agrees; the layover city belongs to the outbound alone when the
        # return is nonstop.
        if _stops(return_leg) != stops_count:
            stops_count = None
        if return_leg.get("duration") != duration:
            duration = None
        if return_leg.get("airlines") != airline:
            airline = None
        if _stops(return_leg) != 0:
            layover_city = None
    return HiddenCityOffer(
        origin=row_origin,
        destination=row_dest,
        departure_date=departure_date,
        price=float(amount),
        currency=currency,
        evidence="confirmed",
        airline=airline.strip() if isinstance(airline, str) and airline.strip() else None,
        duration=duration if isinstance(duration, str) and duration else None,
        stops_count=stops_count,
        layover_city=layover_city,
        # Not in the captured payload: no field names the beyond city. Stays null.
        ticketed_destination=None,
        hidden_city=_hidden_city(card, return_leg),
        return_date=return_date,
        booking_url=deep_link,
    )


def parse_skiplagged_offers(
    result: Any,
    *,
    origin: str,
    destination: str,
    departure_date: date,
    currency: Optional[str] = None,
    return_date: Optional[date] = None,
) -> tuple[HiddenCityOffer, ...]:
    """Normalize a sk_flights_search result. Never invents a fare or an FX pairing.

    A card with no usable price or currency is skipped. Currency is kept as
    the card's own; a named keep filters on it and nothing converts.
    """
    wanted = normalize_currency(currency) if currency else None
    cards = _flight_cards(result)
    layover_by_trip = _layover_by_trip(_result_text(result))
    offers: list[HiddenCityOffer] = []
    for card in cards:
        try:
            offer = _offer_from_card(
                card,
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=return_date,
                layover_by_trip=layover_by_trip,
            )
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
        if offer is None:
            continue
        if wanted and offer.currency != wanted:
            continue
        offers.append(offer)
    return tuple(offers)


def _classify(exc: BaseException) -> SearchError:
    if isinstance(exc, SkiplaggedRateLimited):
        return SearchError(
            code=SearchErrorCode.BLOCKED,
            message=str(exc),
            rate_limited=True,
            retry_until=cooldown_until(str(exc), SKIPLAGGED_RATE_LIMIT_FILE),
        )
    if isinstance(exc, SkiplaggedShapeError):
        return SearchError(
            code=SearchErrorCode.MARKUP_DRIFT,
            message=str(exc) or "Skiplagged MCP reply changed shape.",
        )
    if isinstance(exc, SkiplaggedError):
        message = str(exc) or "Skiplagged MCP request failed."
        folded = message.casefold()
        if "block" in folded or "429" in folded:
            return SearchError(code=SearchErrorCode.BLOCKED, message=message)
        return SearchError(code=SearchErrorCode.FETCH_FAILED, message=message)
    if isinstance(exc, (URLError, TimeoutError, OSError)):
        return SearchError(
            code=SearchErrorCode.FETCH_FAILED,
            message="Skiplagged MCP could not be reached.",
            timeout=isinstance(exc, TimeoutError),
        )
    return SearchError(
        code=SearchErrorCode.FETCH_FAILED,
        message="Skiplagged MCP request failed.",
    )


def _report_currency(
    named: Optional[str],
    offers: tuple[HiddenCityOffer, ...],
) -> Optional[str]:
    if named:
        return named
    owned = {offer.currency for offer in offers}
    if len(owned) == 1:
        return next(iter(owned))
    return None


@controlled
def search_hidden_city(
    origin: str,
    destination: str,
    departure_date: date,
    *,
    return_date: Optional[date] = None,
    adults: int = 1,
    top: int = 8,
    currency: Optional[str] = None,
    rpc: Optional[RpcPost] = None,
    cancel: Optional[threading.Event] = None,
) -> HiddenCityReport:
    """Search Skiplagged via its public MCP. Opt-in. Does not mix Google results.

    Named currency is a keep of owned card ISO 4217. Skiplagged cards are USD.
    A keep that matches no owned card is currency_mismatch, not no_results.
    """
    if top <= 0:
        raise ValueError("top must be positive")
    if adults < 1:
        raise ValueError("adults must be at least 1")
    if adults > MAX_SEARCH_ADULTS:
        raise ValueError(
            f"source skiplagged takes at most {MAX_SEARCH_ADULTS} adults per search; "
            "split the party into separate searches"
        )
    today = date.today()
    if departure_date < today:
        raise ValueError(f"departure date is in the past: {departure_date.isoformat()}")
    if return_date is not None and return_date < departure_date:
        raise ValueError("return date must not be before departure")
    FlightQuery(origin, destination, departure_date, adults=adults)
    named_currency = normalize_currency(currency) if currency else None
    started = time.perf_counter()
    arguments: dict[str, Any] = {
        "origin": origin.strip().upper(),
        "destination": destination.strip().upper(),
        "departureDate": departure_date.isoformat(),
        "limit": top,
        "sort": "price",
        "adults": adults,
    }
    if return_date is not None:
        arguments["returnDate"] = return_date.isoformat()
    offers: tuple[HiddenCityOffer, ...] = ()
    owned: tuple[HiddenCityOffer, ...] = ()
    error: Optional[SearchError] = None
    try:
        result = _call_mcp(arguments, rpc=rpc or _rpc_post)
        owned = parse_skiplagged_offers(
            result,
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
        )
    except SearchDeadline:
        raise
    except Exception as exc:
        error = _classify(exc)
    else:
        offers = tuple(
            offer for offer in owned if named_currency is None or offer.currency == named_currency
        )
        offers = tuple(sorted(offers, key=lambda offer: offer.price))[:top]
        if not offers:
            if owned and named_currency:
                seen = ", ".join(sorted({offer.currency for offer in owned}))
                error = SearchError(
                    code=SearchErrorCode.CURRENCY_MISMATCH,
                    message=(
                        f"Skiplagged priced this route in {seen}; "
                        f"no rows matched requested keep {named_currency}. "
                        "Viajante does not convert."
                    ),
                )
            else:
                error = SearchError(
                    code=SearchErrorCode.NO_RESULTS,
                    message="Skiplagged returned no priced itineraries for this route and date.",
                )
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    report_currency = (
        _report_currency(None, owned)
        if error is not None and error.code == SearchErrorCode.CURRENCY_MISMATCH
        else _report_currency(named_currency, offers)
    )
    return HiddenCityReport(
        searched_at=_utc_now(),
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        currency=report_currency,
        offers=offers,
        error=error,
        return_date=return_date,
        fetch_ms=fetch_ms,
        warnings=HIDDEN_CITY_WARNINGS,
        locale=FETCH_LANGUAGE,
    )
