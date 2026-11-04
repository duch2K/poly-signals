"""Pre-trade checks (spec §4). Each -> (ok, reason)."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .signals import Signal
from .store import Store
from .venue import Book, fee_amount


@dataclass
class Snapshot:
    """Market/account state at check time."""
    book_up: Book
    book_down: Book
    balance: float
    fee_bps_up: int
    fee_bps_down: int
    limit_up: float      # order limit price (signal + slippage, tick-rounded)
    limit_down: float


@dataclass
class RiskResult:
    ok: bool
    reason: str = ""
    expected_edge: float = 0.0   # profit per share after fees (worst-case prices)
    withdraw_alert: bool = False


def worst_case_edge(sig: Signal, snap: Snapshot) -> float:
    pu = snap.book_up.vwap_buy(sig.size, snap.limit_up)
    pd = snap.book_down.vwap_buy(sig.size, snap.limit_down)
    if pu is None or pd is None:
        return -1.0
    fees = fee_amount(pu, pu, snap.fee_bps_up) + fee_amount(pd, pd, snap.fee_bps_down)
    return 1.0 - pu - pd - fees


class RiskChecker:
    def __init__(self, s: Settings, store: Store):
        self.s = s
        self.store = store

    def check(self, sig: Signal, snap: Snapshot) -> RiskResult:
        s = self.s
        withdraw = snap.balance > s.max_balance

        # 1. size per market (both legs at worst price + existing exposure)
        cost = sig.size * (snap.limit_up + snap.limit_down)
        exposure = self.store.market_exposure(sig.market)
        if exposure + cost > s.max_size_per_market:
            return RiskResult(False, f"max_size_per_market:{exposure + cost:.2f}>{s.max_size_per_market}", withdraw_alert=withdraw)

        # 2. daily loss
        loss = self.store.daily_realized_loss()
        if loss >= s.max_daily_loss:
            return RiskResult(False, f"daily_loss_limit:{loss:.2f}>={s.max_daily_loss}", withdraw_alert=withdraw)

        # 3. unhedged positions
        unhedged = self.store.unhedged_count()
        if unhedged >= s.max_unhedged:
            return RiskResult(False, f"unhedged_positions:{unhedged}>={s.max_unhedged}", withdraw_alert=withdraw)

        # 4. liquidity on both sides
        need = sig.size * s.min_liquidity_mult
        du = snap.book_up.ask_depth(snap.limit_up)
        dd = snap.book_down.ask_depth(snap.limit_down)
        if du < need or dd < need:
            return RiskResult(False, f"liquidity:up={du:.2f},down={dd:.2f},need={need:.2f}", withdraw_alert=withdraw)

        # 5. slippage + the core idea: both sides < $1 after fees
        bu, bd = snap.book_up.best_ask, snap.book_down.best_ask
        if bu is None or bd is None:
            return RiskResult(False, "slippage:empty_book", withdraw_alert=withdraw)
        if bu > sig.price_up + s.max_slippage or bd > sig.price_down + s.max_slippage:
            return RiskResult(False, f"slippage:ask_up={bu},ask_down={bd}", withdraw_alert=withdraw)
        edge = worst_case_edge(sig, snap)
        if edge < s.min_edge:
            return RiskResult(False, f"no_edge:{edge:.4f}<{s.min_edge}", withdraw_alert=withdraw)

        # 6. account balance
        if cost > snap.balance:
            return RiskResult(False, f"insufficient_balance:{snap.balance:.2f}<{cost:.2f}", withdraw_alert=withdraw)
        if withdraw and s.balance_limit_blocks_trading:
            return RiskResult(False, f"balance_over_limit:{snap.balance:.2f}>{s.max_balance}", withdraw_alert=True)

        return RiskResult(True, "ok", expected_edge=edge, withdraw_alert=withdraw)
