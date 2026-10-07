"""Bounded batches for public round-trip date sweeps. Offline; no provider calls."""

from __future__ import annotations

import re
import threading
import time
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import _isolate  # noqa: F401
from test_google_flights_public import _card, _context
from viajante.control import SearchCancelled, checkpoint, current_control
from viajante.dates import calendar_trip, search_dates
from viajante.google_flights import GoogleFlightsBlocked, SweepHttpResponse
from viajante.google_flights_public import PublicGoogleFlightsHttpSource
from viajante.models import FlightQuery
from viajante.parsers import parse_price
from viajante.ratelimit import (
    GOOGLE_RATE_LIMIT_FILE,
    note_rate_limited,
    rate_limit_advice,
    rate_limit_status,
)
from viajante.storage import default_state_dir

START = date(2099, 6, 1)
NIGHTS = 7
CONCURRENCY = 8


def _english(clock: str) -> str:
    hour, minute = (int(part) for part in clock.split(":"))
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _echo(clock: str) -> str:
    return f"<div>HAN–SIN {_english(clock)} Choose return round trip</div>"


class _Pages:
    def __init__(
        self, days: int, outbounds: int, *, bad_echo: set[tuple[int, int]] | None = None
    ) -> None:
        self.html: dict[str, str] = {}
        self.cards: dict[str, tuple] = {}
        self.trips: list = []
        self.outbound_urls: list[str] = []
        self.return_urls: list[str] = []
        self.dropped_return_urls: list[str] = []
        bad_echo = bad_echo or set()
        probe = PublicGoogleFlightsHttpSource(currency="EUR", client=object())
        for day_index in range(days):
            day = START + timedelta(days=day_index)
            trip = calendar_trip("HAN", "SIN", day, max_stops=0, adults=2, nights=NIGHTS)
            self.trips.append(trip)
            outs = []
            for n in range(outbounds):
                outs.append(
                    _card(
                        "HAN",
                        "SIN",
                        day,
                        f"{8 + n:02d}:00",
                        f"TA{day_index:02d}{n:02d}",
                        f"€{100 + n}",
                    )
                )
            board_id = f"out-{day_index}"
            outbound_url = probe._url(trip)
            self.outbound_urls.append(outbound_url)
            self.html[outbound_url] = _context() + f'<div data-page="{board_id}"></div>'
            self.cards[board_id] = tuple(outs)
            ranked = sorted(outs, key=lambda card: parse_price(card.price) or float("inf"))
            for n, card in enumerate(ranked):
                url = probe._url(trip, card.legs[0])
                if n >= 8:
                    self.dropped_return_urls.append(url)
                    continue
                clock = card.legs[0].segments[0].departure or card.departure or ""
                back = _card(
                    "SIN",
                    "HAN",
                    trip.return_date,
                    "09:00",
                    f"TB{day_index:02d}{n:02d}",
                    f"€{500 + day_index}",
                )
                page_id = f"ret-{day_index}-{n}"
                echo = "" if (day_index, n) in bad_echo else _echo(clock)
                self.html[url] = _context() + echo + f'<div data-page="{page_id}"></div>'
                self.cards[page_id] = (back,)
                self.return_urls.append(url)

    def parse(self, html: str, currency: str | None = None):
        match = re.search(r'data-page="([^"]+)"', html)
        if match is None:
            raise AssertionError("public page is missing its test marker")
        return self.cards[match.group(1)]


class _SerialClient:
    def __init__(self, pages: _Pages) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        self.urls.append(url)
        return SweepHttpResponse(200, self.pages.html[url], url)


class _BatchClient:
    def __init__(self, pages: _Pages, *, concurrency: int = CONCURRENCY) -> None:
        self.pages = pages
        self.concurrency = concurrency
        self.batches: list[list[str]] = []
        self.urls: list[str] = []
        self.waves = 0
        self.wave_sizes: list[int] = []
        self.timestamps: list[float] = []

    def _ok(self, url: str) -> SweepHttpResponse:
        return SweepHttpResponse(200, self.pages.html[url], url)

    def get_many(self, urls, *, timeout: float) -> list[SweepHttpResponse]:
        urls = list(urls)
        self.batches.append(urls)
        out = []
        for start in range(0, len(urls), self.concurrency):
            chunk = urls[start : start + self.concurrency]
            self.waves += 1
            self.wave_sizes.append(len(chunk))
            now = time.monotonic()
            for url in chunk:
                self.urls.append(url)
                self.timestamps.append(now)
                out.append(self._ok(url))
        return out


def _view(report) -> dict:
    return {
        "rows": [
            (
                row.departure_date,
                row.return_date,
                row.status,
                row.price,
                row.airline,
                row.stops_count,
                row.departure,
                row.arrival,
                row.scope_bound,
                tuple(error.code.value for error in row.page_errors),
                None if row.error is None else row.error.code.value,
                row.stops_compare,
            )
            for row in report.days
        ],
        "limit": report.coverage.scope.get("public_page_outbound_limit"),
        "strategy": report.coverage.strategy,
    }


def _search(client, pages: _Pages, **extra):
    sleeps: list[float] = []
    source = PublicGoogleFlightsHttpSource(currency="EUR", client=client, sleep=sleeps.append)
    end = START + timedelta(days=len(pages.trips) - 1)
    with patch("viajante.google_flights_public.parse_shopping_page", side_effect=pages.parse):
        report = search_dates(
            "HAN",
            "SIN",
            START,
            end,
            adults=2,
            max_stops=0,
            nights=NIGHTS,
            currency="EUR",
            source=source,
            **extra,
        )
    return report, sleeps


class PublicRoundTripDateBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(self._clear_cooldown)

    def _clear_cooldown(self) -> None:
        path = default_state_dir() / GOOGLE_RATE_LIMIT_FILE
        if path.exists():
            path.unlink()

    def test_31_day_round_trip_is_35_waves_and_matches_serial(self) -> None:
        pages = _Pages(31, 8)
        serial = _SerialClient(pages)
        batch = _BatchClient(pages)
        serial_report, serial_sleeps = _search(serial, pages)
        batch_report, batch_sleeps = _search(batch, pages)

        self.assertEqual(_view(serial_report), _view(batch_report))
        self.assertEqual(batch_report.days[0].price, 500)
        self.assertEqual(batch_report.days[-1].price, 530)
        self.assertTrue(all(row.status == "ok" and row.scope_bound for row in batch_report.days))
        self.assertEqual(sorted(serial.urls), sorted(batch.urls))
        self.assertEqual(len(batch.urls), 31 + 31 * 8)
        self.assertEqual(batch.batches, [pages.outbound_urls, pages.return_urls])
        self.assertEqual(batch.waves, 35)
        self.assertLessEqual(max(batch.wave_sizes), CONCURRENCY)
        self.assertEqual(batch.timestamps, sorted(batch.timestamps))
        self.assertEqual(batch.timestamps[0], batch.timestamps[CONCURRENCY - 1])
        self.assertGreater(batch.timestamps[-1], batch.timestamps[0])
        self.assertEqual(serial_sleeps, [])
        self.assertEqual(batch_sleeps, [])

    def test_batch_keeps_eight_returns_and_matches_serial_cards(self) -> None:
        pages = _Pages(1, 10)
        serial = _SerialClient(pages)
        batch = _BatchClient(pages)
        serial_report, _serial_sleeps = _search(serial, pages)
        batch_report, _batch_sleeps = _search(batch, pages)

        self.assertEqual(_view(serial_report), _view(batch_report))
        self.assertEqual(batch.batches[1], pages.return_urls)
        self.assertEqual(len(pages.return_urls), 8)
        self.assertEqual(len(pages.dropped_return_urls), 2)
        self.assertTrue(set(pages.dropped_return_urls).isdisjoint(batch.urls))
        self.assertEqual(sorted(serial.urls), sorted(batch.urls))
        self.assertEqual(batch_report.days[0].price, 500)
        self.assertEqual(batch_report.coverage.scope["public_page_outbound_limit"], 8)

    def test_failed_return_page_matches_the_serial_partial(self) -> None:
        pages = _Pages(1, 2, bad_echo={(0, 1)})
        serial_report, _ = _search(_SerialClient(pages), pages)
        batch_report, _ = _search(_BatchClient(pages), pages)
        self.assertEqual(_view(serial_report), _view(batch_report))
        self.assertEqual(batch_report.days[0].status, "ok")
        self.assertEqual(batch_report.days[0].price, 500)
        self.assertEqual(
            [error.code.value for error in batch_report.days[0].page_errors],
            ["markup_drift"],
        )

    def test_429_mid_batch_stops_pending_and_records_the_cooldown(self) -> None:
        self._clear_cooldown()
        pages = _Pages(4, 2)
        client = _RateLimitClient(pages, concurrency=2, trigger_at=2)
        report, _sleeps = _search(client, pages)

        self.assertEqual(len(client.batches), 2)
        self.assertEqual(client.return_sent, pages.return_urls[:4])
        self.assertEqual(report.days[0].status, "ok")
        self.assertEqual(report.days[0].price, 500)
        self.assertEqual(
            [row.error.code.value for row in report.days[1:]],
            ["blocked", "blocked", "blocked"],
        )
        self.assertTrue(report.days[1].error.rate_limited)
        state = rate_limit_status()
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["basis"], "heuristic_http_429")
        self.assertEqual(state["cooldown_s"], 120)

        with patch("viajante.google_flights.shared_chrome_sweep_client") as shared:
            follow = PublicGoogleFlightsHttpSource(currency="EUR")
            with self.assertRaises(GoogleFlightsBlocked):
                follow.fetch(FlightQuery("HAN", "SIN", START, adults=2, max_stops=0))
            shared.assert_not_called()

    def test_cancel_during_the_return_batch_sends_no_further_pages(self) -> None:
        pages = _Pages(3, 1)
        cancel = threading.Event()
        client = _CancelClient(pages, cancel)
        with self.assertRaises(SearchCancelled):
            _search(client, pages, cancel=cancel)
        self.assertEqual(client.sent, pages.outbound_urls + pages.return_urls[:2])
        self.assertLess(len(client.sent), len(pages.outbound_urls) + len(pages.return_urls))

    def test_deadline_keeps_finished_days_and_marks_the_rest(self) -> None:
        pages = _Pages(3, 1)
        client = _DeadlineClient(pages)
        report, _sleeps = _search(client, pages, deadline_seconds=30)
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.sent, pages.outbound_urls + pages.return_urls[:1])
        self.assertEqual(report.days[0].status, "ok")
        self.assertEqual(report.days[0].price, 500)
        self.assertTrue(report.days[0].scope_bound)
        self.assertEqual(
            [row.error.code.value for row in report.days[1:]],
            ["deadline", "deadline"],
        )
        self.assertEqual(report.coverage.stopping_reason, "deadline")
        self.assertFalse(report.coverage.complete)


class _RateLimitClient(_BatchClient):
    """One return wave returns 429; later waves are not sent. Records the cooldown."""

    def __init__(self, pages: _Pages, *, concurrency: int, trigger_at: int) -> None:
        super().__init__(pages, concurrency=concurrency)
        self.trigger_at = trigger_at
        self.return_sent: list[str] = []
        self._stopped = False

    def get_many(self, urls, *, timeout: float) -> list[SweepHttpResponse]:
        urls = list(urls)
        self.batches.append(urls)
        if len(self.batches) == 1:
            return [self._ok(url) for url in urls]
        out: list[SweepHttpResponse] = []
        start = 0
        while start < len(urls):
            if self._stopped:
                for url in urls[start:]:
                    out.append(
                        SweepHttpResponse(
                            0,
                            "Not sent. Remaining batch stopped after a provider block.",
                            url,
                            request_sent=False,
                            attempts=0,
                            stopped=True,
                        )
                    )
                break
            chunk = urls[start : start + self.concurrency]
            hit = False
            for offset, url in enumerate(chunk):
                self.return_sent.append(url)
                if start + offset == self.trigger_at:
                    hit = True
                    advice = rate_limit_advice(note_rate_limited())
                    out.append(SweepHttpResponse(429, "slow down", url, rate_limit=advice))
                else:
                    out.append(self._ok(url))
            self._stopped = hit
            start += len(chunk)
        return out


class _CancelClient:
    def __init__(self, pages: _Pages, cancel: threading.Event) -> None:
        self.pages = pages
        self.cancel = cancel
        self.sent: list[str] = []
        self.calls = 0

    def get_many(self, urls, *, timeout: float) -> list[SweepHttpResponse]:
        urls = list(urls)
        self.calls += 1
        if self.calls == 1:
            self.sent.extend(urls)
            return [SweepHttpResponse(200, self.pages.html[url], url) for url in urls]
        out = []
        for start in range(0, len(urls), 2):
            if start:
                self.cancel.set()
                checkpoint()
            chunk = urls[start : start + 2]
            self.sent.extend(chunk)
            out.extend(SweepHttpResponse(200, self.pages.html[url], url) for url in chunk)
        return out


class _DeadlineClient:
    def __init__(self, pages: _Pages) -> None:
        self.pages = pages
        self.sent: list[str] = []
        self.calls = 0

    def get_many(self, urls, *, timeout: float) -> list[SweepHttpResponse]:
        urls = list(urls)
        self.calls += 1
        if self.calls == 1:
            self.sent.extend(urls)
            return [SweepHttpResponse(200, self.pages.html[url], url) for url in urls]
        done, rest = urls[:1], urls[1:]
        self.sent.extend(done)
        control = current_control()
        if control is not None:
            control.mark_cut()
        return [SweepHttpResponse(200, self.pages.html[url], url) for url in done] + [
            SweepHttpResponse(0, "", url, deadline=True, request_sent=False, attempts=0)
            for url in rest
        ]
