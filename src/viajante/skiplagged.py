"""Opt-in Skiplagged MCP client. No Google mix-in. Does not book."""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from viajante.airports import is_known_iata
from viajante.models import (
    FETCH_LANGUAGE,
    HIDDEN_CITY_SOURCE,
    HIDDEN_CITY_WARNINGS,
    FlightQuery,
    HiddenCityOffer,
    HiddenCityReport,
    SearchError,
    SearchErrorCode,
    normalize_currency,
)
from viajante.quote import resolve_quote_currency
from viajante.storage import write_json_atomic

SKIPLAGGED_MCP_URL = "https://mcp.skiplagged.com/mcp"
SKIPLAGGED_FLIGHTS_TOOL = "sk_flights_search"
_PROTOCOL = "2025-03-26"
_TIMEOUT_SECONDS = 30
RpcPost = Callable[[str, dict[str, Any], Mapping[str, str]], tuple[int, Mapping[str, str], str]]


class SkiplaggedError(RuntimeError):
    """Skiplagged MCP request failed."""


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


def _call_mcp(
    arguments: Mapping[str, Any],
    *,
    rpc: RpcPost,
    url: str = SKIPLAGGED_MCP_URL,
) -> Any:
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
    if status >= 400:
        raise SkiplaggedError(f"Skiplagged MCP initialize failed ({status}).")
    _rpc_result(_sse_json(body))
    session_id = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
    call_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": SKIPLAGGED_FLIGHTS_TOOL, "arguments": dict(arguments)},
    }
    status, _call_headers, body = rpc(url, call_payload, _headers(session_id=session_id))
    if status >= 400:
        raise SkiplaggedError(f"Skiplagged MCP search failed ({status}).")
    return _rpc_result(_sse_json(body))


def _as_mapping(value: Any) -> Optional[dict[str, Any]]:
    return value if isinstance(value, dict) else None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("flights", "itineraries", "offers", "results", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    return []


def _tool_rows(result: Any) -> list[Any]:
    payload = result
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if structured is not None:
            rows = _as_list(structured)
            if rows:
                return rows
            if isinstance(structured, dict):
                payload = structured
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    continue
                rows = _as_list(decoded)
                if rows:
                    return rows
                if isinstance(decoded, dict):
                    payload = decoded
    return _as_list(payload)


def _first_str(row: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _numberish(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text[:1] in {"$", "€"}:
            text = text[1:].strip()
        try:
            return float(text)
        except ValueError:
            return None
    if isinstance(value, dict):
        for nested_key in ("amount", "value", "total", "fare"):
            nested = _numberish(value.get(nested_key))
            if nested is not None:
                return nested
    return None


def _first_number(row: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        parsed = _numberish(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _nested_iata(row: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        nested = row.get(key)
        if isinstance(nested, dict):
            code = _iata_or_none(_first_str(nested, "airport", "iata", "code"))
            if code:
                return code
        elif isinstance(nested, str):
            code = _iata_or_none(nested)
            if code:
                return code
    return None


def _hidden_from_attributes(row: Mapping[str, Any]) -> Optional[bool]:
    attrs = row.get("attributes")
    if not isinstance(attrs, list):
        return None
    tokens = {str(item).strip().casefold() for item in attrs}
    if tokens & {"hidden-city", "hidden_city", "hidden city", "skiplagging", "skiplagged"}:
        return True
    if "standard" in tokens:
        return False
    return None


def _iata_or_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    code = value.strip().upper()
    if len(code) == 3 and is_known_iata(code):
        return code
    return None


def _bool_flag(row: Mapping[str, Any], *keys: str) -> Optional[bool]:
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            folded = value.strip().casefold()
            if folded in {"1", "true", "yes", "hidden", "hidden_city", "hidden-city"}:
                return True
            if folded in {"0", "false", "no"}:
                return False
    return None


def _stops_count(row: Mapping[str, Any]) -> Optional[int]:
    value = _first_number(row, "stops_count", "stops", "stopCount", "maxStops", "layovers")
    if value is None:
        layover = _first_str(row, "layover", "layover_city", "layoverCity")
        if layover:
            return 1
        return None
    count = int(value)
    if count < 0:
        return None
    return count


def parse_skiplagged_offers(
    result: Any,
    *,
    origin: str,
    destination: str,
    departure_date: date,
    currency: str,
    return_date: Optional[date] = None,
) -> tuple[HiddenCityOffer, ...]:
    """Normalize a Skiplagged MCP tool result. Never invents a fare."""
    offers: list[HiddenCityOffer] = []
    for row in _tool_rows(result):
        mapped = _as_mapping(row)
        if mapped is None:
            continue
        price = _first_number(mapped, "price", "fare", "total", "amount", "totalPrice")
        if price is None or price <= 0:
            continue
        price_obj = mapped.get("price")
        nested_currency = price_obj.get("currency") if isinstance(price_obj, dict) else None
        row_currency = _first_str(mapped, "currency", "curr") or (
            nested_currency if isinstance(nested_currency, str) else None
        )
        if row_currency:
            try:
                offer_currency = normalize_currency(row_currency)
            except ValueError:
                continue
        else:
            offer_currency = currency
        row_origin = (
            _iata_or_none(_first_str(mapped, "origin", "from", "originAirport"))
            or _nested_iata(mapped, "departure")
            or origin
        )
        row_dest = (
            _iata_or_none(_first_str(mapped, "destination", "to", "destinationAirport"))
            or _nested_iata(mapped, "arrival")
            or destination
        )
        ticketed = _iata_or_none(
            _first_str(mapped, "ticketed_destination", "ticketedDestination", "finalDestination")
        )
        layover = _first_str(mapped, "layover_city", "layoverCity", "layover", "via")
        layover_iata = _iata_or_none(layover)
        hidden = _bool_flag(
            mapped,
            "hidden_city",
            "hiddenCity",
            "is_hidden_city",
            "isHiddenCity",
            "skiplagged",
        )
        if hidden is None:
            hidden = _hidden_from_attributes(mapped)
        if hidden is None:
            hidden = bool(ticketed and ticketed != row_dest)
        url = _first_str(
            mapped,
            "booking_url",
            "bookingUrl",
            "deepLink",
            "url",
            "link",
            "flightURL",
        )
        offers.append(
            HiddenCityOffer(
                origin=row_origin,
                destination=row_dest,
                departure_date=departure_date,
                price=price,
                currency=offer_currency,
                evidence="confirmed",
                source=HIDDEN_CITY_SOURCE,
                airline=_first_str(mapped, "airline", "airlines", "carrier", "airlineName"),
                duration=_first_str(mapped, "duration", "durationText", "time"),
                stops_count=_stops_count(mapped),
                layover_city=layover_iata or layover,
                ticketed_destination=ticketed,
                hidden_city=bool(hidden),
                return_date=return_date,
                booking_url=url,
            )
        )
    return tuple(offers)


def _classify(exc: BaseException) -> SearchError:
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
        )
    return SearchError(
        code=SearchErrorCode.FETCH_FAILED,
        message="Skiplagged MCP request failed.",
    )


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
) -> HiddenCityReport:
    """Search Skiplagged via its public MCP. Opt-in. Does not mix Google results."""
    if top <= 0:
        raise ValueError("top must be positive")
    if adults < 1:
        raise ValueError("adults must be at least 1")
    today = date.today()
    if departure_date < today:
        raise ValueError(f"departure date is in the past: {departure_date.isoformat()}")
    if return_date is not None and return_date < departure_date:
        raise ValueError("return date must not be before departure")
    FlightQuery(origin, destination, departure_date, adults=adults)
    currency = resolve_quote_currency(currency, origin.strip().upper())
    started = time.perf_counter()
    arguments: dict[str, Any] = {
        "origin": origin.strip().upper(),
        "destination": destination.strip().upper(),
        "departureDate": departure_date.isoformat(),
        "limit": top,
        "adults": adults,
    }
    if return_date is not None:
        arguments["returnDate"] = return_date.isoformat()
    try:
        result = _call_mcp(arguments, rpc=rpc or _rpc_post)
        offers = parse_skiplagged_offers(
            result,
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            currency=currency,
            return_date=return_date,
        )
        error = None
        if not offers:
            error = SearchError(
                code=SearchErrorCode.NO_RESULTS,
                message="Skiplagged returned no priced itineraries for this route and date.",
            )
    except Exception as exc:
        offers = ()
        error = _classify(exc)
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    return HiddenCityReport(
        searched_at=_utc_now(),
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        currency=currency,
        offers=offers[:top],
        error=error,
        return_date=return_date,
        fetch_ms=fetch_ms,
        warnings=HIDDEN_CITY_WARNINGS,
        locale=FETCH_LANGUAGE,
    )


def write_hidden_city_report_atomic(report: HiddenCityReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)
