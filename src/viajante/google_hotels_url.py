"""Google Hotels navigation context, separate from internal click tracking URLs.

The ts protobuf mirrors the owned AtySUc search parameters. Dates and currency
were observed in provider links; a non-default party/room count was checked in
Google's returned page and its AtySUc request echo. This reproduces a query,
not a guaranteed fare, room configuration or cancellation policy.
"""

from base64 import urlsafe_b64encode
from datetime import date
from urllib.parse import urlencode

from viajante.models import HotelQuery
from viajante.tfs import _len_delim, _string, _varint_field


def _date(value: date) -> bytes:
    return (
        _varint_field(1, value.year) + _varint_field(2, value.month) + _varint_field(3, value.day)
    )


def hotel_navigation_params(query: HotelQuery, *, currency: str, html_lang: str) -> str:
    """Encode the searched stay, party and rooms using Google's owned fields."""
    party = b""
    if query.adults != 2 or query.rooms != 1:
        party = _len_delim(
            2,
            _len_delim(1, _varint_field(1, 3)) * query.adults + _varint_field(2, query.rooms),
        )
    dates = (
        _len_delim(1, _date(query.check_in))
        + _len_delim(2, _date(query.check_out))
        + _varint_field(3, query.nights)
    )
    filters = _string(7, currency)
    if query.free_cancellation:
        filters += _varint_field(4, 1)
    context = (
        _varint_field(1, 2 if query.entire_home else 1)
        + party
        + _len_delim(
            3,
            _len_delim(1, _len_delim(3, b""))
            + _len_delim(2, _len_delim(2, dates) + _len_delim(6, _varint_field(2, 0))),
        )
        + _len_delim(5, _len_delim(1, filters) + _len_delim(3, b""))
    )
    return urlencode(
        {
            "q": f"{query.location} hotels",
            "hl": html_lang,
            "curr": currency,
            "ts": urlsafe_b64encode(context).decode("ascii").rstrip("="),
        }
    )
