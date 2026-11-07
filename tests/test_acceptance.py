"""Acceptance tests (spec §9)."""
import asyncio

from execution.config import Mode, Settings
from execution.venue import Book, LiveVenue
from execution import app as app_mod

from .conftest import DOWN, UP, make_signal


def run(coro):
    return asyncio.run(coro)


def actions(m):
    return [r["action"] for r in m.journal.read_all()]


# ---------------------------------------------------------------- happy path
def test_pair_completes_in_paper(env):
    m = env()
    r = run(m.executor.handle(make_signal()))
    assert r["status"] == "completed"
    assert abs(r["locked_pnl"] - 10 * (1 - 0.48 - 0.49)) < 1e-9
    assert m.venue.positions == {UP: 10, DOWN: 10}
    assert m.store.unhedged_count() == 0
    assert {"received", "risk_ok", "leg1_sent", "leg1_filled", "leg2_sent", "completed"} <= set(actions(m))


# ---------------------------------------------------------------- signal
def test_duplicate_signal_ignored(env):
    m = env()
    sig = make_signal()
    assert run(m.executor.handle(sig))["status"] == "completed"
    r = run(m.executor.handle(sig))
    assert r["status"] == "duplicate"
    assert m.venue.positions == {UP: 10, DOWN: 10}  # did not buy a second time


def test_duplicate_survives_restart(env):
    m = env()
    sig = make_signal()
    run(m.executor.handle(sig))
    m2 = env()  # same db_path
    assert run(m2.executor.handle(sig))["status"] == "duplicate"


def test_missing_field_rejected(env):
    m = env()
    sig = make_signal()
    del sig["lag_seconds"]
    r = run(m.executor.handle(sig))
    assert r["status"] == "rejected" and "lag_seconds" in r["reason"]


def test_invalid_values_rejected(env):
    m = env()
    for bad in ({"size": 0}, {"price_up": 1.2}, {"price_down": -0.1}, {"signal_id": "x"}):
        r = run(m.executor.handle(make_signal(**bad)))
        assert r["status"] == "rejected", bad


def test_stale_signal_rejected(env):
    m = env()
    r = run(m.executor.handle(make_signal(timestamp="2020-01-01T00:00:00Z")))
    assert r["reason"].startswith("stale_signal")


# ---------------------------------------------------------------- limits
def test_max_size_per_market(env):
    m = env(max_size_per_market=5)
    r = run(m.executor.handle(make_signal()))
    assert r["reason"].startswith("max_size_per_market")
    assert m.venue.positions == {}


def test_daily_loss_limit(env):
    m = env()
    m.store.record_pnl("old", "realized", -10.0)
    r = run(m.executor.handle(make_signal()))
    assert r["reason"].startswith("daily_loss_limit")


def test_unhedged_limit(env):
    m = env()
    m.store.add_leg("old", "m0", UP, 1, 0.5)
    r = run(m.executor.handle(make_signal()))
    assert r["reason"].startswith("unhedged_positions")


def test_liquidity(env):
    m = env()
    m.data.books[DOWN] = Book(bids=[(0.47, 100)], asks=[(0.49, 3)])
    r = run(m.executor.handle(make_signal()))
    assert r["reason"].startswith("liquidity")


def test_slippage(env):
    m = env()
    m.data.books[UP] = Book(bids=[(0.50, 100)], asks=[(0.52, 100)])
    r = run(m.executor.handle(make_signal()))
    assert r["reason"].startswith(("slippage", "liquidity"))
    assert m.venue.positions == {}


def test_no_edge_with_fees(env):
    m = env()
    m.data.fee_bps = 1500  # fee per share ~0.075 > 0.03 edge
    r = run(m.executor.handle(make_signal()))
    assert r["reason"].startswith("no_edge")


def test_balance_limit_and_withdraw_alert(env):
    m = env(max_balance=50)  # paper balance 100 > 50
    r = run(m.executor.handle(make_signal()))
    assert r["reason"].startswith("balance_over_limit")
    alerts = [x for x in m.journal.read_all() if x["action"] == "alert"]
    assert alerts and alerts[0]["alert"] == "withdraw" and alerts[0]["withdraw_amount"] == 50


def test_withdraw_alert_without_blocking(env):
    m = env(max_balance=50, balance_limit_blocks_trading=False)
    r = run(m.executor.handle(make_signal()))
    assert r["status"] == "completed"
    assert any(x.get("alert") == "withdraw" for x in m.journal.read_all())


# ---------------------------------------------------------------- second leg
def test_leg2_fails_first_leg_unwound(env):
    def move_market(data):  # market moved against us during lag
        data.books[UP] = Book(bids=[(0.55, 100)], asks=[(0.60, 100)])
        data.books[DOWN] = Book(bids=[(0.55, 100)], asks=[(0.60, 100)])

    m = env(sleep_hook=move_market)
    r = run(m.executor.handle(make_signal(lag_seconds=1)))
    assert r["status"] == "unwound"
    assert "leg2_failed" in actions(m) and "unwound" in actions(m)
    assert m.store.unhedged_count() == 0
    assert sum(m.venue.positions.values()) == 0


def test_unwind_loss_trips_daily_limit(env):
    def crash(data):
        data.books[UP] = Book(bids=[(0.10, 100)], asks=[(0.90, 100)])
        data.books[DOWN] = Book(bids=[(0.10, 100)], asks=[(0.90, 100)])

    m = env(sleep_hook=crash, max_daily_loss=2, unwind_max_slippage=0.5)
    r = run(m.executor.handle(make_signal(lag_seconds=1)))
    assert r["status"] == "unwound" and r["realized_pnl"] < -2
    assert m.kill.active and "daily_loss_limit" in m.kill.reason
    assert run(m.executor.handle(make_signal()))["reason"].startswith("kill_switch_active")


def test_unwind_impossible_stops_module(env):
    def no_bids(data):
        data.books[UP] = Book(bids=[], asks=[(0.90, 100)])
        data.books[DOWN] = Book(bids=[], asks=[(0.90, 100)])

    m = env(sleep_hook=no_bids)
    r = run(m.executor.handle(make_signal(lag_seconds=1)))
    assert r["status"] == "stuck"
    assert m.kill.active
    assert m.store.unhedged_count() == 1


# ---------------------------------------------------------------- safety
def test_kill_switch_stops_trading(env):
    m = env()
    m.kill.trip("manual:test")
    r = run(m.executor.handle(make_signal()))
    assert r["reason"].startswith("kill_switch_active")
    assert m.venue.positions == {}
    m.kill.reset()
    assert run(m.executor.handle(make_signal()))["status"] == "completed"


def test_kill_during_lag_unwinds(env):
    holder = {}

    def press_kill(data):
        holder["m"].kill.trip("manual:during_lag")

    m = env(sleep_hook=press_kill)
    holder["m"] = m
    r = run(m.executor.handle(make_signal(lag_seconds=5)))
    assert r["status"] == "unwound"
    assert "leg2_sent" not in actions(m)


def test_heartbeat_failure_stops(env):
    m = env(heartbeat_max_failures=3)
    m.data.ping_ok = False
    for _ in range(3):
        run(m.heartbeat.beat())
    assert m.kill.active and "heartbeat_lost" in m.kill.reason


def test_critical_api_error_stops(env):
    m = env()

    async def boom(t):
        raise RuntimeError("api down")

    m.venue.get_book = boom
    r = run(m.executor.handle(make_signal()))
    assert r["status"] == "error"
    assert m.kill.active


# ---------------------------------------------------------------- paper
def test_paper_never_builds_live_client(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("LiveVenue must not be created in paper mode")

    monkeypatch.setattr(LiveVenue, "__init__", forbidden)
    v = app_mod.build_venue(Settings(_env_file=None, mode=Mode.PAPER))
    assert v.sends_real_orders is False


def test_live_requires_explicit_confirm():
    try:
        app_mod.build_venue(Settings(_env_file=None, mode=Mode.LIVE))
    except RuntimeError as e:
        assert "LIVE_CONFIRM" in str(e)
    else:
        raise AssertionError("live started without confirm")


# ---------------------------------------------------------------- journal
def test_every_record_has_reason_and_rejects_explain(env):
    m = env(max_size_per_market=1)
    run(m.executor.handle(make_signal()))
    run(m.executor.handle({"garbage": 1}))
    recs = m.journal.read_all()
    assert all({"ts", "signal_id", "action", "reason", "mode"} <= r.keys() for r in recs)
    assert all(r["reason"] for r in recs if r["action"] == "rejected")


def test_concurrent_signals_share_lock(env):
    """Module built outside the event loop (like `serve`), then two signals at once.
    Guards against 'Future attached to a different loop' on Python 3.9."""
    m = env(max_size_per_market=100)

    async def both():
        return await asyncio.gather(m.executor.handle(make_signal()), m.executor.handle(make_signal()))

    res = asyncio.run(both())
    assert [r["status"] for r in res] == ["completed", "completed"]
    assert not m.kill.active


def test_fee_formula_matches_polymarket_docs():
    from execution.venue import fee_amount
    # docs example: 100 shares, crypto rate 0.07 (700 bps), p=0.50 -> $1.75
    assert abs(fee_amount(100 * 0.5, 0.5, 700) - 1.75) < 1e-9
    # politics 0.04 (400 bps), p=0.10 -> $0.36
    assert abs(fee_amount(100 * 0.1, 0.1, 400) - 0.36) < 1e-9


def test_paper_negative_min_edge_buys_losing_pair(env):
    """Paper test setting: accept up to 5 cents loss per share so the buy path runs on normal books."""
    m = env(min_edge=-0.05)
    m.data.books[UP] = Book(bids=[(0.50, 100)], asks=[(0.52, 100)])   # 0.52 + 0.49 = 1.01
    r = run(m.executor.handle(make_signal(price_up=0.52)))
    assert r["status"] == "completed" and r["locked_pnl"] < 0
    assert m.venue.positions == {UP: 10, DOWN: 10}


def test_negative_min_edge_refused_in_live():
    try:
        app_mod.build_venue(Settings(_env_file=None, mode=Mode.LIVE, live_confirm="I_UNDERSTAND_REAL_MONEY",
                                     min_edge=-0.01))
    except RuntimeError as e:
        assert "paper-only" in str(e)
    else:
        raise AssertionError("negative min_edge accepted in live")


def test_order_prices_never_reach_one(env):
    """Signal near 1.0 + slippage must not produce a limit >= 1 (API rejects it)."""
    m = env(min_edge=-0.1)
    m.data.books[UP] = Book(bids=[(0.01, 100)], asks=[(0.02, 100)])
    m.data.books[DOWN] = Book(bids=[(0.97, 100)], asks=[(0.98, 100)])
    r = run(m.executor.handle(make_signal(price_up=0.02, price_down=0.98)))
    assert r["status"] == "completed"
    sent = [x["price"] for x in m.journal.read_all() if x["action"] in ("leg1_sent", "leg2_sent")]
    assert sent and all(p < 1.0 for p in sent)
