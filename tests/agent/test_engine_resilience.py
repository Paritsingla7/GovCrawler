"""_reporter/_checkpoint_loop used to catch only asyncio.CancelledError — one
transient exception (a network blip on send_heartbeat, a disk hiccup on
_save_checkpoint) killed the loop permanently. Losing heartbeats for good
means the cloud reaper wrongly flips a still-healthy crawl to `interrupted`
~150s later (plan.md §10.6). Both loops must swallow a stray exception and
keep going. Also covers: checkpoint has its own executor (no longer
contends with lead/visited writes on the single db_pool thread), and
visited_urls only counts successful fetches, not every attempt."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx

from agent.crawler.engine import CrawlerEngine, _QueueItem


def _engine():
    config = {"crawler": {}, "extraction": {}}
    return CrawlerEngine(config, cloud=None, job_id=1)


def test_reporter_survives_heartbeat_exception(monkeypatch):
    async def fast_sleep(_):
        return

    monkeypatch.setattr("agent.crawler.engine.asyncio.sleep", fast_sleep)

    calls = {"n": 0}

    class FakeCloud:
        async def send_heartbeat(self, metrics):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("blip")
            if calls["n"] >= 3:
                return True  # cancel_requested, stops the loop
            return False

    eng = _engine()
    eng._cloud = FakeCloud()

    async def runner():
        # A standalone dummy task to receive the "stop the crawl" cancel — not
        # the same task running _reporter itself, so cancelling it doesn't
        # also cancel this test's own await chain.
        eng._run_task = asyncio.ensure_future(asyncio.sleep(3600))
        await eng._reporter()
        eng._run_task.cancel()

    asyncio.run(runner())
    assert calls["n"] >= 3  # kept looping after the first call's exception


def test_checkpoint_loop_survives_save_exception():
    eng = _engine()
    eng._checkpoint_pool = ThreadPoolExecutor(max_workers=1)

    save_calls = {"n": 0}

    def fake_save():
        save_calls["n"] += 1
        if save_calls["n"] == 1:
            raise OSError("disk hiccup")

    eng._save_checkpoint = fake_save

    sleep_calls = {"n": 0}

    async def counting_sleep(_):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 3:
            raise asyncio.CancelledError()  # simulates the loop's task being cancelled

    async def runner():
        eng._loop = asyncio.get_running_loop()
        await eng._checkpoint_loop()

    with patch("agent.crawler.engine.asyncio.sleep", counting_sleep):
        asyncio.run(runner())  # _checkpoint_loop catches its own CancelledError, so this returns normally

    # call #1 raised OSError, but the loop kept going and ran call #2 anyway.
    assert save_calls["n"] >= 2
    eng._checkpoint_pool.shutdown(wait=True)


def test_checkpoint_pool_is_separate_from_db_pool():
    eng = _engine()
    eng._db_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
    eng._checkpoint_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="checkpoint")
    assert eng._db_pool is not eng._checkpoint_pool
    eng._db_pool.shutdown(wait=True)
    eng._checkpoint_pool.shutdown(wait=True)


def test_visited_count_only_increments_on_successful_fetch():
    eng = _engine()
    eng._loop = asyncio.new_event_loop()
    eng._db_pool = ThreadPoolExecutor(max_workers=1)
    eng._parse_pool = ThreadPoolExecutor(max_workers=1)
    eng._cloud = type("C", (), {"mark_visited": staticmethod(lambda url: None)})()

    async def fails(url, ctx):
        return None

    eng._fetch = fails
    item = _QueueItem(priority=0, counter=1, url="http://x.gov.in/a", depth=0, is_seed=False)

    asyncio.set_event_loop(eng._loop)
    eng._loop.run_until_complete(eng._process(item, None))
    eng._loop.close()

    assert eng._session_visited_count == 0  # fetch failed -> not counted
    eng._db_pool.shutdown(wait=True)
    eng._parse_pool.shutdown(wait=True)


def test_fetch_httpx_retries_once_on_transport_error(monkeypatch):
    eng = _engine()

    calls = {"n": 0}

    class FakeResponse:
        status_code = 200
        url = "http://x.gov.in/a"
        text = "<html>ok</html>"

    class FakeClient:
        async def get(self, url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectTimeout("blip")
            return FakeResponse()

    eng._client = FakeClient()
    eng._cfg["target_suffixes"] = []

    async def fast_sleep(_):
        return

    monkeypatch.setattr("agent.crawler.engine.asyncio.sleep", fast_sleep)

    result = asyncio.run(eng._fetch_httpx("http://x.gov.in/a"))
    assert result == "<html>ok</html>"
    assert calls["n"] == 2  # first attempt failed, retried once and succeeded
