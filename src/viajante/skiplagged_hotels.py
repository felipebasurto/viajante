"""Opt-in Skiplagged hotels over its public MCP. No Google or Booking mix-in.

Skiplagged cards are USD. Search lists stays (total and rating only live in the
tool's markdown table); details lists room rates with occupancy and refund flags.
Viajante never converts and never merges these rows with another provider.
"""

from __future__ import annotations

import difflib
import random
import re
import threading
import time
import unicodedata
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Optional

from viajante.airports import canonical_city_name, lookup_airports
from viajante.control import SearchDeadline, checkpoint, controlled, interruptible_sleep
from viajante.models import (
    FETCH_LANGUAGE,
    AppliedHotelFilters,
    HotelPage,
    HotelQuery,
    HotelRoomRate,
    HotelRoomsReport,
    RawHotelCard,
    SearchError,
    SearchErrorCode,
)
from viajante.orchestration import (
    MAX_ATTEMPTS,
    NON_RETRIABLE_CODES,
    classify_failure,
    retry_backoff_seconds,
)
from viajante.ratelimit import SKIPLAGGED_RATE_LIMIT_FILE, cooldown_until
from viajante.skiplagged import (
    SKIPLAGGED_MCP_URL,
    RpcPost,
    SkiplaggedError,
    SkiplaggedRateLimited,
    _call_mcp,
    _rpc_post,
)

SKIPLAGGED_HOTELS_TOOL = "sk_hotels_search"
SKIPLAGGED_HOTEL_DETAILS_TOOL = "sk_hotel_details"
SKIPLAGGED_HOTEL_CURRENCY = "USD"
SKIPLAGGED_HOTELS_URL = "https://skiplagged.com/hotels"
MAX_SEARCH_ADULTS = 10
MAX_SEARCH_ROOMS = 9
MAX_DETAIL_ROOMS = 5
PAGE_LIMIT = 100

_SEARCH_SLUG = re.compile(r"/hotels/\d+/(.+?)-hotels/")
_HOTEL_ID_IN_URL = re.compile(r"/hotel/(\d+)/")
_RATING = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*10")
_MONEY = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_LINK = re.compile(r"\]\(([^)]+)\)")
_HOTELS_HEADING = re.compile(r"^\s*#\s*Hotels\s+in\s+(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


class SkiplaggedNoHotels(Exception):
    """The city did not match, or the city has no stays for these dates."""


class SkiplaggedAmbiguousName(Exception):
    """More than one Skiplagged hotel carries the requested name."""


class SkiplaggedParseMiss(ValueError):
    """The tool answered but its table no longer matches the structured rows."""


def build_applied_filters(
    query: HotelQuery,
    *,
    html_lang: str = FETCH_LANGUAGE,
    currency: str,
) -> AppliedHotelFilters:
    # Skiplagged takes no cancellation or type filter, and its search rows carry no
    # cancellation evidence, so the free-cancellation request cannot drop or confirm anything.
    del html_lang, currency
    return AppliedHotelFilters(
        chips=(),
        url=SKIPLAGGED_HOTELS_URL,
        not_applied=("free_cancellation",) if query.free_cancellation else (),
    )


def validate_search_party(adults: int, rooms: int) -> None:
    if adults > MAX_SEARCH_ADULTS:
        raise ValueError(
            f"source skiplagged takes at most {MAX_SEARCH_ADULTS} adults per search; "
            "split the party into separate stays"
        )
    if rooms > MAX_SEARCH_ROOMS:
        raise ValueError(f"source skiplagged takes at most {MAX_SEARCH_ROOMS} rooms per search")


def _text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


def _check_error(result: Any) -> None:
    if isinstance(result, dict) and result.get("isError"):
        message = _text(result).strip() or "Skiplagged hotel tool error"
        if "no matching city" in message.casefold():
            raise SkiplaggedNoHotels(message)
        raise SkiplaggedError(message)


def _table_rows(text: str) -> dict[str, dict[str, Any]]:
    """Per-hotel total and review score from the tool's markdown table, keyed by hotel id."""
    rows: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        if not line.startswith("| **"):
            continue
        cells = line.strip().removeprefix("| ").removesuffix(" |").split(" | ")
        if len(cells) != 6:
            continue
        link = _LINK.search(cells[5])
        hotel = _HOTEL_ID_IN_URL.search(link.group(1)) if link else None
        total = _MONEY.search(cells[3])
        if hotel is None or total is None:
            continue
        rating = _RATING.search(cells[1])
        rows[hotel.group(1)] = {
            "total": f"${total.group(1)}",
            "rating": rating.group(1) if rating else None,
        }
    return rows


def _priced_in_usd(card: Mapping[str, Any]) -> bool:
    """False when the structured price names a currency other than USD.

    The structured ``price`` is a nightly rate; the stay total comes from the table.
    """
    price = card.get("price")
    currency = price.get("currency") if isinstance(price, dict) else None
    return not isinstance(currency, str) or currency.strip().upper() == SKIPLAGGED_HOTEL_CURRENCY


def parse_search_page(result: Any) -> HotelPage:
    _check_error(result)
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    cards = structured.get("results") if isinstance(structured, dict) else None
    if not isinstance(cards, list) or not cards:
        raise SkiplaggedNoHotels("Skiplagged returned no hotels for this city and dates.")
    table = _table_rows(_text(result))
    if not table:
        raise SkiplaggedParseMiss("hotel table did not match the structured results")
    parsed: list[RawHotelCard] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        hotel_id = str(card.get("id", "")).removeprefix("hotel_")
        row = table.get(hotel_id)
        name = card.get("name")
        if row is None or not isinstance(name, str) or not name.strip():
            continue
        if not _priced_in_usd(card):
            continue  # The table's "$" total is only owned when the card says USD.
        amenities = card.get("amenities")
        stars = card.get("rating")
        parsed.append(
            RawHotelCard(
                title=name,
                address=card.get("location") if isinstance(card.get("location"), str) else None,
                total_price=row["total"],
                rating=row["rating"],
                details=", ".join(a for a in amenities if isinstance(a, str))
                if isinstance(amenities, list)
                else "",
                link=card.get("deepLink") if isinstance(card.get("deepLink"), str) else None,
                class_label=stars.get("text") if isinstance(stars, dict) else None,
                provider_id=hotel_id or None,
            )
        )
    if not parsed:
        raise SkiplaggedParseMiss("no structured hotel matched a priced table row")
    url = structured.get("searchUrl") if isinstance(structured, dict) else None
    slug = _SEARCH_SLUG.search(url) if isinstance(url, str) else None
    return HotelPage(
        cards=tuple(parsed),
        resolved_place=slug.group(1) if slug else None,
        search_url=url if isinstance(url, str) else None,
    )


class SkiplaggedHotelsSource:
    """Hotel loop source over `sk_hotels_search`. USD only."""

    def __init__(self, *, rpc: RpcPost = _rpc_post, url: str = SKIPLAGGED_MCP_URL) -> None:
        self._rpc = rpc
        self._url = url

    def fetch(self, query: HotelQuery, applied: AppliedHotelFilters, limit: int) -> HotelPage:
        del applied
        validate_search_party(query.adults, query.rooms)
        result = _call_mcp(
            {
                "city": query.location,
                "checkin": query.check_in.isoformat(),
                "checkout": query.check_out.isoformat(),
                "numAdults": query.adults,
                "numRooms": query.rooms,
                "limit": max(1, min(limit, PAGE_LIMIT)),
                "sort": "price",
            },
            rpc=self._rpc,
            url=self._url,
            tool=SKIPLAGGED_HOTELS_TOOL,
        )
        return parse_search_page(result)

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


def _echo_int(detail: Mapping[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        value = detail.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _echo_date(detail: Mapping[str, Any], *keys: str) -> Optional[date]:
    for key in keys:
        value = detail.get(key)
        if not isinstance(value, str):
            continue
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            continue
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _flag(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _rate(row: Any) -> Optional[HotelRoomRate]:
    if not isinstance(row, dict):
        return None
    total = _number(row.get("totalPriceInDollars"))
    title = row.get("title")
    if total is None or total <= 0 or not isinstance(title, str) or not title.strip():
        return None
    limit = row.get("occupancyLimit")
    beds = row.get("bedTypes")
    link = row.get("bookingLink")
    return HotelRoomRate(
        title=title.strip(),
        total_price=total,
        price_per_night=_number(row.get("pricePerNightInDollars")),
        taxes_and_fees=_number(row.get("taxesAndFeesInDollars")),
        occupancy_limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
        refundable=_flag(row.get("refundable")),
        free_cancellation=_flag(row.get("freeCancellation")),
        bed_types=tuple(b for b in beds if isinstance(b, str)) if isinstance(beds, list) else (),
        booking_link=link if isinstance(link, str) else None,
    )


def parse_rooms_report(
    result: Any,
    *,
    hotel_id: str,
    check_in: date,
    check_out: date,
    adults: int,
    rooms: int,
    searched_at: datetime,
) -> HotelRoomsReport:
    _check_error(result)
    detail = result.get("structuredContent") if isinstance(result, dict) else None
    if not isinstance(detail, dict):
        raise SkiplaggedParseMiss("hotel details had no structured content")
    rates = tuple(rate for row in detail.get("rooms") or [] if (rate := _rate(row)) is not None)
    if not rates:
        raise SkiplaggedNoHotels("Skiplagged listed no bookable room rates for these dates.")
    place = detail.get("location") if isinstance(detail.get("location"), dict) else {}
    count = detail.get("reviewCount")
    return HotelRoomsReport(
        searched_at=searched_at,
        hotel_id=hotel_id,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        rooms=rooms,
        currency=SKIPLAGGED_HOTEL_CURRENCY,
        name=detail.get("hotelName") if isinstance(detail.get("hotelName"), str) else None,
        address=detail.get("address") if isinstance(detail.get("address"), str) else None,
        city=detail.get("cityName") if isinstance(detail.get("cityName"), str) else None,
        star_rating=_number(detail.get("starRating")),
        review_rating=_number(detail.get("reviewRating")),
        review_count=count if isinstance(count, int) and not isinstance(count, bool) else None,
        latitude=_number(place.get("lat")),
        longitude=_number(place.get("lng")),
        link=detail.get("bookingLink") if isinstance(detail.get("bookingLink"), str) else None,
        rates=rates,
        answered_adults=_echo_int(detail, "numAdults", "adults"),
        answered_rooms=_echo_int(detail, "numRooms", "rooms"),
        answered_check_in=_echo_date(detail, "checkin", "checkIn", "check_in"),
        answered_check_out=_echo_date(detail, "checkout", "checkOut", "check_out"),
    )


def skiplagged_failure(exc: BaseException) -> SearchError:
    if isinstance(exc, SearchDeadline):
        return classify_failure(exc)
    if isinstance(exc, SkiplaggedRateLimited):
        return SearchError(
            code=SearchErrorCode.BLOCKED,
            message=str(exc),
            rate_limited=True,
            retry_until=cooldown_until(str(exc), SKIPLAGGED_RATE_LIMIT_FILE),
        )
    if isinstance(exc, (SkiplaggedNoHotels, SkiplaggedAmbiguousName)):
        return SearchError(code=SearchErrorCode.NO_RESULTS, message=str(exc))
    if isinstance(exc, SkiplaggedParseMiss):
        return SearchError(
            code=SearchErrorCode.MARKUP_DRIFT, message="Skiplagged hotel parse missed."
        )
    return SearchError(code=SearchErrorCode.FETCH_FAILED, message=str(exc) or type(exc).__name__)


def _normalized_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    # Ignore Latin accents as before; other scripts' marks can change identity.
    text = "".join(
        unicodedata.normalize("NFD", char)[0]
        if unicodedata.name(char, "").startswith("LATIN")
        else char
        for char in text
    )
    return " ".join(
        "".join(
            char if char.isalnum() or unicodedata.category(char).startswith("M") else " "
            for char in text
        ).split()
    )


def _provider_city_heading(text: str) -> Optional[tuple[str, Optional[str]]]:
    match = _HOTELS_HEADING.search(text)
    if match is None:
        return None
    # Some provider headings include a country after a comma. Preserve an
    # explicit ISO code so it can be compared without inventing country names.
    parts = [part.strip() for part in match.group(1).split(",", 1)]
    country = (
        parts[1].upper() if len(parts) == 2 and re.fullmatch(r"[A-Za-z]{2}", parts[1]) else None
    )
    return parts[0], country


def _longest_catalogue_city(place: str) -> tuple[Optional[str], tuple]:
    """Find an airport-catalogue city at the start of a provider URL slug.

    Slugs such as ``prague-czech-republic`` add a country after the city, so
    test successively shorter token prefixes. A longer owned city such as
    ``san-jose-del-cabo-mexico`` remains distinct from ``San Jose``.
    """
    tokens = _normalized_name(place.replace("-", " ")).split()
    for end in range(len(tokens), 0, -1):
        candidate = " ".join(tokens[:end])
        hits = tuple(
            airport
            for airport in lookup_airports(candidate, limit=100)
            if canonical_city_name(airport.city) == canonical_city_name(candidate)
        )
        if hits:
            return canonical_city_name(candidate), hits
    return None, ()


def resolve_hotel_id(
    name: str,
    city: str,
    check_in: date,
    check_out: date,
    *,
    adults: int,
    rooms: int = 1,
    rpc: RpcPost = _rpc_post,
) -> tuple[int, str]:
    """Skiplagged id of the one hotel in ``city`` whose name matches ``name`` exactly.

    The match is on the normalized name only (case, accents and punctuation ignored). Zero or
    several matches are typed errors that list what Skiplagged returned; nothing is guessed.
    """
    result = _call_mcp(
        {
            "city": city,
            "checkin": check_in.isoformat(),
            "checkout": check_out.isoformat(),
            "numAdults": adults,
            "numRooms": rooms,
            "limit": PAGE_LIMIT,
            "sort": "price",
        },
        rpc=rpc,
        tool=SKIPLAGGED_HOTELS_TOOL,
    )
    page = parse_search_page(result)
    requested_city = city.split(",", 1)[0].strip()
    requested_canonical = canonical_city_name(requested_city)
    requested_country = city.split(",", 1)[1].strip().upper() if "," in city else None
    heading = _provider_city_heading(_text(result))
    if heading:
        heading_city, heading_country = heading
        if canonical_city_name(heading_city) != requested_canonical or (
            requested_country and heading_country and requested_country != heading_country
        ):
            raise SkiplaggedNoHotels(
                f"Skiplagged resolved {city!r} to {heading_city!r}; "
                "the requested city was not searched. Nothing was guessed."
            )
    if page.resolved_place:
        resolved_city = _normalized_name(page.resolved_place.replace("-", " "))
        requested_city = _normalized_name(requested_city)
        if resolved_city != requested_city and not resolved_city.startswith(requested_city + " "):
            raise SkiplaggedNoHotels(
                f"Skiplagged resolved {city!r} to {page.resolved_place!r}; "
                "the requested city was not searched. Nothing was guessed."
            )
        catalog_city, city_hits = _longest_catalogue_city(page.resolved_place)
        if catalog_city is not None and catalog_city != requested_canonical:
            raise SkiplaggedNoHotels(
                f"Skiplagged resolved {city!r} to a different city ({catalog_city}); "
                "the requested city was not searched. Nothing was guessed."
            )
        if (
            requested_country
            and city_hits
            and not any(airport.country == requested_country for airport in city_hits)
        ):
            raise SkiplaggedNoHotels(
                f"Skiplagged resolved {city!r} to a different country; "
                "the requested city was not searched. Nothing was guessed."
            )
    wanted = _normalized_name(name)
    hits = [card for card in page.cards if wanted and _normalized_name(card.title) == wanted]
    if len(hits) == 1 and hits[0].provider_id:
        return int(hits[0].provider_id), hits[0].title
    place = page.resolved_place or city
    if hits:
        listed = "; ".join(f"{card.provider_id} ({card.address or 'no address'})" for card in hits)
        raise SkiplaggedAmbiguousName(
            f"{len(hits)} Skiplagged hotels are named {name!r} in {place}: {listed}. "
            "Pass the hotel id."
        )
    by_name = {_normalized_name(card.title): card.title for card in page.cards}
    close = [by_name[key] for key in difflib.get_close_matches(wanted, by_name, n=5, cutoff=0.5)]
    hint = f" Closest returned: {', '.join(close)}." if close else ""
    raise SkiplaggedNoHotels(
        f"No Skiplagged hotel named {name!r} among the {len(page.cards)} it returned for {place} "
        f"on these dates (a hotel with no availability is not listed).{hint} Nothing was guessed."
    )


@controlled
def search_hotel_rooms(
    hotel_id: Optional[int],
    check_in: date,
    check_out: date,
    *,
    hotel_name: Optional[str] = None,
    city: Optional[str] = None,
    adults: int = 2,
    rooms: int = 1,
    rpc: RpcPost = _rpc_post,
    sleep: Callable[[float], None] = interruptible_sleep,
    random_gen: Any = None,
    cancel: Optional[threading.Event] = None,
) -> HotelRoomsReport:
    """Room rates for one Skiplagged hotel, by id or by exact name in a city.

    Failures come back as a typed error in the report.
    """
    if hotel_id is None:
        if not (hotel_name and hotel_name.strip() and city and city.strip()):
            raise ValueError("pass hotel_id, or hotel_name together with city")
    elif hotel_name or city:
        raise ValueError("pass hotel_id or hotel_name with city, not both")
    elif hotel_id <= 0:
        raise ValueError("hotel_id must be positive")
    if check_out <= check_in:
        raise ValueError("check_out must be after check_in")
    if adults <= 0 or adults > MAX_SEARCH_ADULTS:
        raise ValueError(f"adults must be 1 to {MAX_SEARCH_ADULTS} with source skiplagged")
    if rooms <= 0 or rooms > MAX_DETAIL_ROOMS:
        raise ValueError(f"rooms must be 1 to {MAX_DETAIL_ROOMS} for hotel room rates")
    random_gen = random_gen or random.Random()
    started = time.perf_counter()
    error: Optional[SearchError] = None
    report: Optional[HotelRoomsReport] = None
    resolved_id = hotel_id
    for attempt in range(MAX_ATTEMPTS):
        try:
            checkpoint()
            if resolved_id is None:
                resolved_id, _matched = resolve_hotel_id(
                    hotel_name or "",
                    city or "",
                    check_in,
                    check_out,
                    adults=adults,
                    rooms=rooms,
                    rpc=rpc,
                )
            result = _call_mcp(
                {
                    "hotelId": resolved_id,
                    "checkin": check_in.isoformat(),
                    "checkout": check_out.isoformat(),
                    "numAdults": adults,
                    "numRooms": rooms,
                },
                rpc=rpc,
                tool=SKIPLAGGED_HOTEL_DETAILS_TOOL,
            )
            report = parse_rooms_report(
                result,
                hotel_id=str(resolved_id),
                check_in=check_in,
                check_out=check_out,
                adults=adults,
                rooms=rooms,
                searched_at=datetime.now(timezone.utc),
            )
            report = replace(report, requested_name=hotel_name)
            break
        except Exception as exc:  # noqa: BLE001 - typed into the report below
            error = skiplagged_failure(exc)
            if error.code in NON_RETRIABLE_CODES:
                break
            if attempt + 1 < MAX_ATTEMPTS:
                sleep(retry_backoff_seconds(attempt, random_gen))
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    if report is None:
        report = HotelRoomsReport(
            searched_at=datetime.now(timezone.utc),
            hotel_id=str(resolved_id) if resolved_id is not None else None,
            check_in=check_in,
            check_out=check_out,
            adults=adults,
            rooms=rooms,
            currency=SKIPLAGGED_HOTEL_CURRENCY,
            requested_name=hotel_name,
            error=error,
        )
    return replace(report, fetch_ms=fetch_ms)
