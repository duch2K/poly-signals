"""Execution logic (spec §6):

1. risk checks -> 2. first leg -> 3. wait lag_seconds -> 4. second leg
-> 5. verify fill; failed -> unwind first leg -> 6. journal.

FOK orders only (all-or-nothing): nothing rests in the book, nothing to cancel.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .config import Settings
from .journal import Journal
from .risk import RiskChecker, Snapshot
from .safety import KillSwitch
from .signals import Signal, parse_signal
from .store import Store
from .venue import Fill, MarketResolver, Venue, VenueError, fee_amount, round_to_tick


class Executor:
    def __init__(self, s: Settings, venue: Venue, resolver: MarketResolver,
                 store: Store, journal: Journal, kill: KillSwitch,
                 sleep: Callable[[float], Awaitable[None]] | None = None):
        self.s, self.venue, self.resolver = s, venue, resolver
        self.store, self.journal, self.kill = store, journal, kill
        self.risk = RiskChecker(s, store)
        self._sleep = sleep or asyncio.sleep
        self._entry_lock = asyncio.Lock()   # risk check + first leg are atomic
        self._withdraw_alerted = False

    # ------------------------------------------------------------ entry
    def accept(self, raw: dict) -> tuple[Signal | None, dict]:
        """Fast synchronous part: validation, dedup, kill switch, signal age."""
        sid = str(raw.get("signal_id")) if isinstance(raw, dict) and raw.get("signal_id") else None
        sig, err = parse_signal(raw)
        if sig is None:
            return None, self._reject(sid, err)
        sid = str(sig.signal_id)
        if not self.store.claim_signal(sid):
            self.journal.write("duplicate", sid, reason="duplicate_signal_id")
            return None, {"status": "duplicate", "reason": "duplicate_signal_id"}
        self.journal.write("received", sid, reason="", market=sig.market, size=sig.size,
                           price_up=sig.price_up, price_down=sig.price_down, lag=sig.lag_seconds)
        if self.kill.active:
            return None, self._reject(sid, f"kill_switch_active:{self.kill.reason}")
        age = (datetime.now(timezone.utc) - sig.timestamp).total_seconds()
        if age > self.s.max_signal_age_s:
            return None, self._reject(sid, f"stale_signal:{age:.1f}s")
        if sig.lag_seconds > self.s.max_lag_s:
            return None, self._reject(sid, f"lag_too_long:{sig.lag_seconds}>{self.s.max_lag_s}")
        return sig, {"status": "accepted", "reason": ""}

    async def handle(self, raw: dict) -> dict:
        sig, res = self.accept(raw)
        if sig is None:
            return res
        return await self.run(sig)

    def _reject(self, sid: str | None, reason: str) -> dict:
        self.journal.write("rejected", sid, reason=reason)
        if sid:
            self.store.set_signal_status(sid, "rejected")
        return {"status": "rejected", "reason": reason}

    # ------------------------------------------------------------ main flow
    async def run(self, sig: Signal) -> dict:
        sid = str(sig.signal_id)
        leg1_id: int | None = None
        try:
            async with self._entry_lock:
                if self.kill.active:
                    return self._reject(sid, f"kill_switch_active:{self.kill.reason}")
                tok_up, tok_down = await self.resolver.resolve(sig.market)
                snap = await self._snapshot(sig, tok_up, tok_down)
                self._check_withdraw(snap.balance)
                rr = self.risk.check(sig, snap)
                if not rr.ok:
                    return self._reject(sid, rr.reason)
                self.journal.write("risk_ok", sid, reason="all_checks_passed", edge=round(rr.expected_edge, 5))

                # less liquid side goes first: it is harder to fill
                up_first = snap.book_up.ask_depth(snap.limit_up) <= snap.book_down.ask_depth(snap.limit_down)
                if up_first:
                    t1, lim1, fee1, t2, p2_sig, fee2 = tok_up, snap.limit_up, snap.fee_bps_up, tok_down, sig.price_down, snap.fee_bps_down
                else:
                    t1, lim1, fee1, t2, p2_sig, fee2 = tok_down, snap.limit_down, snap.fee_bps_down, tok_up, sig.price_up, snap.fee_bps_up

                self.journal.write("leg1_sent", sid, reason="", token=t1, price=lim1, size=sig.size,
                                   side="up" if up_first else "down")
                f1 = await self.venue.buy_fok(t1, lim1, sig.size)
                if not f1.filled:
                    self.store.set_signal_status(sid, "leg1_failed")
                    self.journal.write("leg1_failed", sid, reason=f1.reason or "not_filled", order_id=f1.order_id)
                    return {"status": "leg1_failed", "reason": f1.reason}
                leg1_id = self.store.add_leg(sid, sig.market, t1, f1.filled_size, f1.avg_price)
                self.journal.write("leg1_filled", sid, reason="", order_id=f1.order_id,
                                   size=f1.filled_size, price=f1.avg_price)

            # ---- wait (outside the lock, interruptible by kill switch)
            await self._wait_lag(sig.lag_seconds)
            if self.kill.active:
                self.journal.write("leg2_skipped", sid, reason=f"kill_switch_during_lag:{self.kill.reason}")
                return await self._unwind(sid, leg1_id, t1, f1, fee1, "kill_switch")

            # ---- second leg: limit price keeps the pair profitable
            tick2 = await self.venue.get_tick_size(t2)
            per_share_fee1 = fee_amount(f1.avg_price, f1.avg_price, fee1)
            max_p2 = 1.0 - f1.avg_price - per_share_fee1 - self.s.min_edge - fee2 / 10_000
            lim2 = round_to_tick(min(p2_sig + self.s.max_slippage, max_p2), tick2, up=False)
            if lim2 <= 0:
                self.journal.write("leg2_skipped", sid, reason=f"no_edge_after_leg1:max_p2={max_p2:.4f}")
                return await self._unwind(sid, leg1_id, t1, f1, fee1, "no_edge_after_leg1")

            self.journal.write("leg2_sent", sid, reason="", token=t2, price=lim2, size=f1.filled_size)
            f2 = await self.venue.buy_fok(t2, lim2, f1.filled_size)
            filled2 = await self._confirm(f2)
            if filled2 + 1e-9 < f1.filled_size:
                if f2.order_id:
                    await self.venue.cancel(f2.order_id)
                self.journal.write("leg2_failed", sid, reason=f2.reason or f"partial:{filled2}",
                                   order_id=f2.order_id, filled=filled2)
                if filled2 > 0:
                    # partial: filled2 is hedged, unwind the rest of leg 1
                    self._lock_pnl(sid, sig.market, t2, filled2, f1, f2, fee1, fee2)
                    rest = Fill(f1.order_id, "filled", f1.filled_size - filled2, f1.avg_price)
                    return await self._unwind(sid, leg1_id, t1, rest, fee1, "leg2_partial")
                return await self._unwind(sid, leg1_id, t1, f1, fee1, "leg2_failed")

            self.store.set_leg_state(leg1_id, "hedged")
            pnl = self._lock_pnl(sid, sig.market, t2, filled2, f1, f2, fee1, fee2)
            self.store.set_signal_status(sid, "completed")
            self.journal.write("completed", sid, reason="pair_hedged", order_id=f2.order_id,
                               price1=f1.avg_price, price2=f2.avg_price, size=filled2,
                               locked_pnl=round(pnl, 6))
            await self._post_trade_balance_check()
            return {"status": "completed", "reason": "", "locked_pnl": pnl}

        except VenueError as e:
            return self._fatal(sid, leg1_id, f"api_error:{e}", trip=e.critical)
        except Exception as e:  # noqa: BLE001 - anything unexpected = stop
            return self._fatal(sid, leg1_id, f"critical_error:{type(e).__name__}:{e}", trip=True)

    # ------------------------------------------------------------ helpers
    async def _snapshot(self, sig: Signal, tok_up: str, tok_down: str) -> Snapshot:
        bu, bd, bal, fu, fd, tu, td = await asyncio.gather(
            self.venue.get_book(tok_up), self.venue.get_book(tok_down), self.venue.get_balance(),
            self.venue.get_fee_bps(tok_up), self.venue.get_fee_bps(tok_down),
            self.venue.get_tick_size(tok_up), self.venue.get_tick_size(tok_down),
        )
        return Snapshot(
            book_up=bu, book_down=bd, balance=bal, fee_bps_up=fu, fee_bps_down=fd,
            limit_up=min(0.999, round_to_tick(sig.price_up + self.s.max_slippage, tu, up=False)),
            limit_down=min(0.999, round_to_tick(sig.price_down + self.s.max_slippage, td, up=False)),
        )

    async def _wait_lag(self, seconds: float) -> None:
        left = seconds
        while left > 0 and not self.kill.active:
            step = min(0.5, left)
            await self._sleep(step)
            left -= step
        if seconds <= 0:
            await self._sleep(0)

    async def _confirm(self, f: Fill) -> float:
        """Check order status after submission (spec §5.1)."""
        if not f.order_id or not f.filled:
            return 0.0
        filled = 0.0
        for _ in range(self.s.order_check_retries):
            filled = await self.venue.get_filled_size(f.order_id)
            if filled + 1e-9 >= f.filled_size:
                return filled
            await self._sleep(self.s.order_check_interval_s)
        return filled

    def _lock_pnl(self, sid, market, t2, size, f1, f2, fee1, fee2) -> float:
        leg2_id = self.store.add_leg(sid, market, t2, size, f2.avg_price)
        self.store.set_leg_state(leg2_id, "hedged")
        fees = fee_amount(f1.avg_price * size, f1.avg_price, fee1) + fee_amount(f2.avg_price * size, f2.avg_price, fee2)
        pnl = size * (1.0 - f1.avg_price - f2.avg_price) - fees
        self.store.record_pnl(sid, "locked", pnl)
        return pnl

    async def _unwind(self, sid: str, leg_id: int | None, token: str, f1: Fill, fee1: int, why: str) -> dict:
        """Unwind an unhedged leg: sell it back near best bid (bounded)."""
        size = f1.filled_size
        for attempt in range(1, self.s.unwind_retries + 1):
            book = await self.venue.get_book(token)
            if book.best_bid is None:
                self.journal.write("unwind_retry", sid, reason="no_bids", attempt=attempt)
                await self._sleep(self.s.order_check_interval_s)
                continue
            tick = await self.venue.get_tick_size(token)
            lim = max(tick, round_to_tick(book.best_bid - self.s.unwind_max_slippage, tick, up=False))
            self.journal.write("unwind_sent", sid, reason=why, token=token, price=lim, size=size, attempt=attempt)
            f = await self.venue.sell_fok(token, lim, size)
            if f.filled:
                sell_fee = await self.venue.get_fee_bps(token)
                pnl = (size * (f.avg_price - f1.avg_price)
                       - fee_amount(f1.avg_price * size, f1.avg_price, fee1)
                       - fee_amount(f.avg_price * size, f.avg_price, sell_fee))
                self.store.record_pnl(sid, "realized", pnl)
                if leg_id is not None:
                    self.store.set_leg_state(leg_id, "unwound")
                self.store.set_signal_status(sid, "unwound")
                self.journal.write("unwound", sid, reason=why, order_id=f.order_id,
                                   price=f.avg_price, realized_pnl=round(pnl, 6))
                self._check_daily_loss()
                await self._post_trade_balance_check()
                return {"status": "unwound", "reason": why, "realized_pnl": pnl}
            self.journal.write("unwind_retry", sid, reason=f.reason or "not_filled", attempt=attempt)
            await self._sleep(self.s.order_check_interval_s)

        if leg_id is not None:
            self.store.set_leg_state(leg_id, "stuck")
        self.store.set_signal_status(sid, "stuck")
        self.journal.write("error", sid, reason=f"unwind_failed:{why}", token=token, size=size)
        self.kill.trip(f"unwind_failed:{sid}")
        return {"status": "stuck", "reason": f"unwind_failed:{why}"}

    def _fatal(self, sid: str, leg_id: int | None, reason: str, trip: bool) -> dict:
        self.journal.write("error", sid, reason=reason)
        if leg_id is not None:
            self.store.set_leg_state(leg_id, "stuck")   # needs manual review
        self.store.set_signal_status(sid, "error")
        if trip:
            self.kill.trip(reason)
        return {"status": "error", "reason": reason}

    def _check_daily_loss(self) -> None:
        loss = self.store.daily_realized_loss()
        if loss >= self.s.max_daily_loss:
            self.kill.trip(f"daily_loss_limit:{loss:.2f}>={self.s.max_daily_loss}")

    def _check_withdraw(self, balance: float) -> None:
        """Spec §5.2: balance > cap -> withdraw alert (once, until it drops back)."""
        if balance > self.s.max_balance:
            if not self._withdraw_alerted:
                self.journal.alert("withdraw", f"balance_over_limit:{balance:.2f}>{self.s.max_balance}",
                                   balance=balance, withdraw_amount=round(balance - self.s.max_balance, 2))
                self._withdraw_alerted = True
        else:
            self._withdraw_alerted = False

    async def _post_trade_balance_check(self) -> None:
        try:
            self._check_withdraw(await self.venue.get_balance())
        except Exception as e:  # noqa: BLE001
            self.journal.write("error", reason=f"balance_check_failed:{e}")
