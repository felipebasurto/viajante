"""Local Google Flights and hotel search for scripts and agents."""

from viajante.airports import lookup_airports
from viajante.dates import search_dates, search_flex
from viajante.explore import search_explore
from viajante.flights import get_flights, search_flights
from viajante.hotels import search_hotels
from viajante.models import (
    CancellationEvidence,
    DateCalendarReport,
    ExploreReport,
    FlexSearchReport,
    FlightLeg,
    FlightQuery,
    HotelQuery,
    HotelSearchReport,
    MultiCity,
    PropertyTypeEvidence,
    QueryFailure,
    QuerySuccess,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
    Trip,
    TripSearchReport,
)
from viajante.prompt_plan import plan_prompt
from viajante.trip import search_trip

__all__ = [
    "CancellationEvidence",
    "DateCalendarReport",
    "ExploreReport",
    "FlexSearchReport",
    "FlightLeg",
    "FlightQuery",
    "HotelQuery",
    "HotelSearchReport",
    "MultiCity",
    "PropertyTypeEvidence",
    "QueryFailure",
    "QuerySuccess",
    "RoundTrip",
    "SearchError",
    "SearchErrorCode",
    "SearchReport",
    "Trip",
    "TripSearchReport",
    "get_flights",
    "lookup_airports",
    "plan_prompt",
    "search_dates",
    "search_explore",
    "search_flex",
    "search_flights",
    "search_hotels",
    "search_trip",
]
