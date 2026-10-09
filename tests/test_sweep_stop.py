"""A provider block stops feeding queued jobs into the sweep client."""

from __future__ import annotations

import asyncio
import json
import unittest
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante.google_flights import (
    SWEEP_TRANSPORT_STATUS,
    ChromeSweepClient,
    SweepHttpResponse,
    SweepPost,
    _settle_batch,
)
from viajante.google_flights_public import _replay_response

RPC_13_BODY = ")]}'\n\n" + json.dumps(
    [["wrb.fr", "GetShoppingResults", None, None, None, [13], "generic"]]
)


class _BatchClient(ChromeSweepClient):
    def __init__(self, trigger: SweepHttpResponse) -> None:
        self._asyncio = asyncio
        self._streams = 2
        self._trigger = trigger
        self._started: list[int] = []
        self._second_started = asyncio.Event()
        self._first_returned = asyncio.Event()

    async def _apost(self, url, data, headers, timeout, cancel_event=None):
        index = int(url.rsplit("/", 1)[-1])
        self._started.append(index)
        if index == 1:
            self._second_started.set()
            await self._first_returned.wait()
            return SweepHttpResponse(200, "ok", url)
        if index == 0:
            await self._second_started.wait()
            self._first_returned.set()
            return self._trigger
        raise AssertionError(f"queued job {index} was sent after a stop condition")


class SweepStopPolicyTests(unittest.TestCase):
    def _run_batch(self, trigger: SweepHttpResponse) -> tuple[_BatchClient, list]:
        client = _BatchClient(trigger)
        jobs = [SweepPost(f"https://example.test/{i}", "{}", {}) for i in range(5)]
        out = [None] * len(jobs)
        responses = asyncio.run(client._apost_many(jobs, 1.0, out))
        return client, responses

    def _assert_queued_jobs_were_not_sent(
        self, client: _BatchClient, responses: list[SweepHttpResponse]
    ) -> None:
        self.assertEqual(client._started, [0, 1])
        for response in responses[2:]:
            self.assertFalse(response.request_sent)
            self.assertEqual(response.attempts, 0)
            self.assertTrue(response.stopped)
            self.assertEqual(response.status, 0)
            self.assertEqual(
                response.text,
                "Not sent. Remaining batch stopped after a provider block.",
            )

    def test_http_429_stops_queued_jobs_without_replay(self) -> None:
        client, responses = self._run_batch(
            SweepHttpResponse(429, "slow down", "https://example.test/0")
        )
        self._assert_queued_jobs_were_not_sent(client, responses)
        self.assertTrue(responses[0].request_sent)
        self.assertEqual(responses[0].attempts, 1)
        self.assertTrue(responses[1].request_sent)
        self.assertEqual(responses[1].attempts, 1)

    def test_raw_rpc_13_stops_queued_jobs_without_fake_http_429(self) -> None:
        client, responses = self._run_batch(
            SweepHttpResponse(200, RPC_13_BODY, "https://example.test/0")
        )
        self._assert_queued_jobs_were_not_sent(client, responses)
        self.assertEqual(responses[0].status, 200)
        self.assertTrue(responses[0].request_sent)
        self.assertEqual(responses[0].attempts, 1)


class BatchWaitTests(unittest.TestCase):
    def _client(self, streams: int) -> ChromeSweepClient:
        client = ChromeSweepClient.__new__(ChromeSweepClient)
        client._streams = streams
        return client

    def test_the_wait_grows_with_the_number_of_waves(self) -> None:
        # 31 requests over 2 streams are 16 waves, so one 30 s timeout cannot cover them all.
        self.assertEqual(self._client(2)._batch_timeout(31, 30.0), 480.0)
        self.assertEqual(self._client(8)._batch_timeout(31, 30.0), 120.0)
        self.assertEqual(self._client(8)._batch_timeout(1, 30.0), 30.0)

    def test_a_timed_out_batch_keeps_answered_jobs_and_stamps_the_rest(self) -> None:
        answered = SweepHttpResponse(200, "ok", "https://example.test/a")
        jobs = [
            SweepPost("https://example.test/a", "", {}),
            SweepPost("https://example.test/b", "", {}),
        ]
        settled = _settle_batch([answered, None], [True, True], jobs, deadline=False)
        self.assertIs(settled[0], answered)
        self.assertEqual(settled[1].status, SWEEP_TRANSPORT_STATUS)
        self.assertTrue(settled[1].request_sent)

    def test_only_a_page_sent_twice_is_attempt_two(self) -> None:
        url = "https://example.test/day"
        unsent = SweepHttpResponse(0, "", url, request_sent=False, attempts=0)
        sent = SweepHttpResponse(429, "", url, request_sent=True, attempts=1)
        self.assertEqual(_replay_response(unsent, sent).attempts, 1)
        self.assertEqual(_replay_response(sent, sent).attempts, 2)

    def test_a_timed_out_wait_cancels_the_queued_work(self) -> None:
        client = ChromeSweepClient.__new__(ChromeSweepClient)
        client._asyncio = asyncio
        client._loop = asyncio.new_event_loop()
        self.addCleanup(client._loop.close)
        future = Future()
        with patch("viajante.google_flights.wait_for_future", side_effect=FutureTimeout()):
            with patch.object(client._asyncio, "run_coroutine_threadsafe", return_value=future):
                coro = asyncio.sleep(0)
                self.addCleanup(coro.close)  # the patch never hands it to a loop
                with self.assertRaises(FutureTimeout):
                    client._submit(coro, timeout=1.0)
        self.assertTrue(future.cancelled())


if __name__ == "__main__":
    unittest.main()
