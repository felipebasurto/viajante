"""Evidence stamping for flight results: Google Flights URLs and immutable offer evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from typing import Optional, Sequence

from viajante.google_flights import google_flights_url
from viajante.models import (
    FetchBackend,
    FlightOffer,
    OfferEvidence,
    QueryResult,
    QuerySuccess,
    SearchReport,
)


def _stamp_google_flights_urls(
    result: QueryResult,
    *,
    html_lang: str,
    currency: str,
    country: Optional[str],
) -> QueryResult:
    query_url = google_flights_url(
        result.query, html_lang=html_lang, currency=currency, country=country
    )
    if isinstance(result, QuerySuccess):

        def stamp(offer: FlightOffer) -> FlightOffer:
            return replace(
                offer,
                google_flights_url=google_flights_url(
                    result.query,
                    html_lang=html_lang,
                    currency=currency,
                    country=country,
                    booking_token=offer.booking_token,
                ),
            )

        return replace(
            result,
            offers=tuple(stamp(offer) for offer in result.offers),
            google_flights_url=query_url,
            recommendation=result.recommendation.map_offers(stamp)
            if result.recommendation
            else None,
        )
    return replace(result, google_flights_url=query_url)


def _stamp_offer_evidence(
    result: QueryResult,
    *,
    retrieved_at: datetime,
    fetch_backend: Optional[FetchBackend],
    currency: str,
) -> QueryResult:
    if not isinstance(result, QuerySuccess):
        return result
    query = result.query.to_dict()

    def stamp(offer: FlightOffer) -> FlightOffer:
        offer_data = dict(offer.to_dict(currency))
        offer_data.pop("evidence", None)
        canonical = json.dumps(
            {"query": query, "currency": currency, "offer": offer_data},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        query_url = result.google_flights_url
        offer_url = offer.google_flights_url
        if offer.booking_token and offer_url:
            url_kind = "booking"
        elif query_url:
            url_kind = "query"
        else:
            url_kind = "none"
        return replace(
            offer,
            evidence=OfferEvidence(
                evidence_id=f"gf_{hashlib.sha256(canonical).hexdigest()[:24]}",
                query=query,
                currency=currency,
                retrieved_at=retrieved_at,
                fetch_backend=fetch_backend,
                query_url=query_url,
                offer_url=offer_url,
                url_kind=url_kind,
            ),
            completeness=None,
        )

    return replace(
        result,
        offers=tuple(stamp(offer) for offer in result.offers),
        recommendation=result.recommendation.map_offers(stamp) if result.recommendation else None,
    )


def _report_with_evidence(
    results: Sequence[QueryResult],
    *,
    searched_at: datetime,
    locale: str,
    currency: str,
    fetch_backend: Optional[FetchBackend],
    fetch_ms: Optional[int],
) -> SearchReport:
    return SearchReport(
        searched_at=searched_at,
        queries=tuple(
            _stamp_offer_evidence(
                result,
                retrieved_at=searched_at,
                fetch_backend=fetch_backend,
                currency=currency,
            )
            for result in results
        ),
        locale=locale,
        currency=currency,
        fetch_backend=fetch_backend,
        fetch_ms=fetch_ms,
    )
