"""Local Google Flights and hotel search for scripts and agents."""

from viajante.airports import lookup_airports
from viajante.dates import search_dates, search_flex
from viajante.explore import search_explore
from viajante.flights import get_flights, search_flights
from viajante.hotels import search_hotels
from viajante.models import (
    AwardCompareReport,
    AwardOffer,
    CancellationEvidence,
    DateCalendarReport,
    ExploreReport,
    FlexSearchReport,
    FlightLeg,
    FlightQuery,
    HiddenCityReport,
    HotelQuery,
    HotelSearchReport,
    MultiCity,
    PointsBalance,
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
from viajante.points import compare_award, load_award_offer, transfer_paths
from viajante.prompt_plan import plan_prompt
from viajante.skiplagged import search_hidden_city
from viajante.trip import search_trip

__all__ = [
    "AwardCompareReport",
    "AwardOffer",
    "CancellationEvidence",
    "DateCalendarReport",
    "ExploreReport",
    "FlexSearchReport",
    "FlightLeg",
    "FlightQuery",
    "HiddenCityReport",
    "HotelQuery",
    "HotelSearchReport",
    "MultiCity",
    "PointsBalance",
    "PropertyTypeEvidence",
    "QueryFailure",
    "QuerySuccess",
    "RoundTrip",
    "SearchError",
    "SearchErrorCode",
    "SearchReport",
    "Trip",
    "TripSearchReport",
    "compare_award",
    "get_flights",
    "load_award_offer",
    "lookup_airports",
    "plan_prompt",
    "search_dates",
    "search_explore",
    "search_flex",
    "search_flights",
    "search_hidden_city",
    "search_hotels",
    "search_trip",
    "transfer_paths",
]
