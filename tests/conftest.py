import uuid
from datetime import datetime, timezone

import pytest

from execution.app import build
from execution.config import Settings
from execution.venue import Book, PaperVenue

UP, DOWN = "tok_up", "tok_down"


class StaticData:
    """In-memory order book instead of HTTP."""

    def __init__(self):
        self.books = {
            UP: Book(bids=[(0.46, 100)], asks=[(0.48, 100)]),
            DOWN: Book(bids=[(0.47, 100)], asks=[(0.49, 100)]),
        }
        self.fee_bps = 0
        self.ping_ok = True

    async def get_book(self, t):
        return self.books[t]

    async def get_fee_bps(self, t):
        return self.fee_bps

    async def get_tick_size(self, t):
        return 0.01

    async def ping(self):
        if not self.ping_ok:
            raise ConnectionError("no response")


class StaticResolver:
    async def resolve(self, market):
        return UP, DOWN


def make_signal(**kw):
    d = {
        "signal_id": str(uuid.uuid4()),
        "market": "m1",
        "size": 10.0,
        "price_up": 0.48,
        "price_down": 0.49,
        "lag_seconds": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    d.update(kw)
    return d


@pytest.fixture
def env(tmp_path):
    def _make(sleep_hook=None, **overrides):
        s = Settings(
            _env_file=None,
            db_path=tmp_path / "db.sqlite",
            journal_path=tmp_path / "journal.jsonl",
            kill_file=tmp_path / "KILL",
            order_check_interval_s=0,
            **overrides,
        )
        data = StaticData()
        venue = PaperVenue(data, s.paper_start_balance)

        async def sleep(sec):
            if sleep_hook:
                sleep_hook(data)

        m = build(s, venue=venue, resolver=StaticResolver(), sleep=sleep)
        m.data = data
        return m

    return _make
