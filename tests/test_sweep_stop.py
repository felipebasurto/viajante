"""A provider block stops feeding queued jobs into the sweep client."""

from __future__ import annotations

import asyncio
import json
import unittest

import _isolate  # noqa: F401
from viajante.google_flights import ChromeSweepClient, SweepHttpResponse, SweepPost

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


if __name__ == "__main__":
    unittest.main()
