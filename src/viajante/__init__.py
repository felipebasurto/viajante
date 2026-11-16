"""Local Google Flights and hotel search for scripts and agents.

Public names load on first access, so importing one submodule (for example
``viajante.models``) does not pull in the whole search stack. ``__all__`` is the
supported surface: the callables below, plus the report and query types. Other
modules are internal.

Callables: ``get_flights``, ``search_flights``, ``search_dates``, ``search_flex``,
``search_explore``, ``search_hotels``, ``search_trip``, ``search_hidden_city``,
``get_hotel_details``, ``lookup_airports``, ``validate_itinerary``,
``compare_award``, ``load_award_offer``, ``transfer_paths``.

``get_flights`` takes a route spec (``JFK-LHR:YYYY-MM-DD``) or trip objects, not
a sentence. ``search_hotels`` defaults to ``source="booking"`` and needs the
browser extra; pass ``source="google"`` for the HTTP search. Hotels need
``currency``. ``get_hotel_details(report, query_index, offer_index)`` reads a
report already in hand. The MCP tool of the same name takes ``selection_id``.
Airline filters on these functions are ``alliances`` and ``exclude_alliances``.
MCP and the CLI call those ``alliance`` and ``exclude_alliance``.

``search_hotel_rooms`` and ``search_split_tickets`` are not package-level
imports. Use the CLI or the MCP tools.
"""

from __future__ import annotations

import importlib
from typing import Any

_MODELS = (
    "AwardCompareReport",
    "AwardOffer",
    "CancellationEvidence",
    "ConstraintCheck",
    "DateCalendarReport",
    "EvidenceCompleteness",
    "ExploreReport",
    "FlexSearchReport",
    "FlightLeg",
    "FlightQuery",
    "HiddenCityReport",
    "HotelQuery",
    "HotelSearchReport",
    "ItineraryValidationReport",
    "MultiCity",
    "OfferEvidence",
    "PointsBalance",
    "PropertyTypeEvidence",
    "QueryFailure",
    "QuerySuccess",
    "RoundTrip",
    "SearchCoverage",
    "SearchError",
    "SearchErrorCode",
    "SearchReport",
    "Trip",
    "TripSearchReport",
)

_EXPORTS: dict[str, str] = {
    **dict.fromkeys(_MODELS, "viajante.models"),
    "compare_award": "viajante.points",
    "load_award_offer": "viajante.points",
    "transfer_paths": "viajante.points",
    "get_flights": "viajante.flights",
    "search_flights": "viajante.flights",
    "search_dates": "viajante.dates",
    "search_flex": "viajante.dates",
    "get_hotel_details": "viajante.details",
    "search_explore": "viajante.explore",
    "lookup_airports": "viajante.airports",
    "search_hidden_city": "viajante.skiplagged",
    "search_hotels": "viajante.hotels",
    "search_trip": "viajante.trip",
    "validate_itinerary": "viajante.validate",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'viajante' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
