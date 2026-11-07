"""Exchange access. One Venue interface, two implementations:

* PaperVenue - reads the REAL order book, "fills" only if depth exists, never sends orders.
* LiveVenue  - py-clob-client, real FOK orders.

Executor does not know which one it is using.
"""
from __future__ import annotations

import asyncio
import json
import math
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class VenueError(Exception):
    """Exchange error. critical=True -> module must stop (spec §5.3)."""

    def __init__(self, msg: str, critical: bool = False):
        super().__init__(msg)
        self.critical = critical


# ---------------------------------------------------------------- order book
@dataclass
class Book:
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, size), descending
    asks: list[tuple[float, float]] = field(default_factory=list)  # (price, size), ascending

    def __post_init__(self) -> None:
        self.bids = sorted(((float(p), float(s)) for p, s in self.bids), key=lambda x: -x[0])
        self.asks = sorted(((float(p), float(s)) for p, s in self.asks), key=lambda x: x[0])

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    def ask_depth(self, limit: float) -> float:
        return sum(s for p, s in self.asks if p <= limit + 1e-12)

    def bid_depth(self, limit: float) -> float:
        return sum(s for p, s in self.bids if p >= limit - 1e-12)

    def vwap_buy(self, size: float, limit: float) -> float | None:
        return _vwap(self.asks, size, lambda p: p <= limit + 1e-12)

    def vwap_sell(self, size: float, limit: float) -> float | None:
        return _vwap(self.bids, size, lambda p: p >= limit - 1e-12)


def _vwap(levels: list[tuple[float, float]], size: float, ok) -> float | None:
    left, cost = size, 0.0
    for p, s in levels:
        if not ok(p):
            break
        take = min(left, s)
        cost += take * p
        left -= take
        if left <= 1e-12:
            return cost / size
    return None


@dataclass
class Fill:
    order_id: str
    status: str            # filled | rejected
    filled_size: float = 0.0
    avg_price: float = 0.0
    reason: str = ""
    raw: Any = None

    @property
    def filled(self) -> bool:
        return self.status == "filled" and self.filled_size > 0


def round_to_tick(price: float, tick: float, up: bool) -> float:
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 6)


# ---------------------------------------------------------------- interface
class Venue(ABC):
    sends_real_orders: bool = False

    @abstractmethod
    async def get_book(self, token_id: str) -> Book: ...
    @abstractmethod
    async def get_balance(self) -> float: ...
    @abstractmethod
    async def get_fee_bps(self, token_id: str) -> int: ...
    @abstractmethod
    async def get_tick_size(self, token_id: str) -> float: ...
    @abstractmethod
    async def buy_fok(self, token_id: str, limit_price: float, size: float) -> Fill: ...
    @abstractmethod
    async def sell_fok(self, token_id: str, limit_price: float, size: float) -> Fill: ...
    @abstractmethod
    async def get_filled_size(self, order_id: str) -> float: ...
    @abstractmethod
    async def cancel(self, order_id: str) -> bool: ...
    @abstractmethod
    async def cancel_all(self) -> None: ...
    @abstractmethod
    async def ping(self) -> None:
        """Heartbeat. Raises if the exchange does not respond."""


def fee_amount(notional: float, price: float, fee_bps: int) -> float:
    """Polymarket taker fee (docs.polymarket.com/trading/fees):

        fee = shares * feeRate * p * (1 - p)

    notional = shares * p, so fee = notional * feeRate * (1 - p).
    feeRate is taken from GET /fee-rate `base_fee` (basis points) -> base_fee / 10_000.
    Makers pay no fee; FOK orders are takers, so the fee always applies.
    """
    rate = fee_bps / 10_000
    return notional * rate * (1.0 - price)


# ---------------------------------------------------------------- market data
class MarketData(Protocol):
    async def get_book(self, token_id: str) -> Book: ...
    async def get_fee_bps(self, token_id: str) -> int: ...
    async def get_tick_size(self, token_id: str) -> float: ...
    async def ping(self) -> None: ...


class HttpMarketData:
    """Public CLOB endpoints, no keys. Used in paper mode."""

    def __init__(self, host: str, timeout: float = 5.0):
        self._c = httpx.AsyncClient(base_url=host.rstrip("/"), timeout=timeout)

    async def _get(self, path: str, **params) -> Any:
        try:
            r = await self._c.get(path, params=params)
        except httpx.HTTPError as e:
            raise VenueError(f"http_error:{e}", critical=False) from e
        if r.status_code >= 500:
            raise VenueError(f"api_{r.status_code}", critical=True)
        if r.status_code >= 400:
            raise VenueError(f"api_{r.status_code}:{r.text[:200]}")
        return r.json()

    async def get_book(self, token_id: str) -> Book:
        d = await self._get("/book", token_id=token_id)
        return Book(
            bids=[(x["price"], x["size"]) for x in d.get("bids", [])],
            asks=[(x["price"], x["size"]) for x in d.get("asks", [])],
        )

    async def get_fee_bps(self, token_id: str) -> int:
        d = await self._get("/fee-rate", token_id=token_id)
        return int(d.get("base_fee") or 0)

    async def get_tick_size(self, token_id: str) -> float:
        d = await self._get("/tick-size", token_id=token_id)
        return float(d["minimum_tick_size"])

    async def ping(self) -> None:
        await self._get("/time")


class MarketResolver(Protocol):
    async def resolve(self, market: str) -> tuple[str, str]:
        """market -> (token_up/yes, token_down/no)."""


class GammaResolver:
    """market id / slug / conditionId -> clobTokenIds via Gamma API."""

    def __init__(self, host: str):
        self._c = httpx.AsyncClient(base_url=host.rstrip("/"), timeout=5.0)
        self._cache: dict[str, tuple[str, str]] = {}

    async def resolve(self, market: str) -> tuple[str, str]:
        if market in self._cache:
            return self._cache[market]
        if market.startswith("0x"):
            r = await self._c.get("/markets", params={"condition_ids": market})
        elif market.isdigit():
            r = await self._c.get(f"/markets/{market}")
        else:
            r = await self._c.get("/markets", params={"slug": market})
        if r.status_code != 200:
            raise VenueError(f"market_lookup_{r.status_code}")
        d = r.json()
        m = d[0] if isinstance(d, list) and d else d
        if not m:
            raise VenueError("market_not_found")
        ids = m.get("clobTokenIds")
        ids = json.loads(ids) if isinstance(ids, str) else ids
        if not ids or len(ids) != 2:
            raise VenueError("market_not_binary")
        self._cache[market] = (str(ids[0]), str(ids[1]))
        return self._cache[market]


# ---------------------------------------------------------------- PAPER
class PaperVenue(Venue):
    sends_real_orders = False

    def __init__(self, data: MarketData, start_balance: float, fee_bps_override: int = 0):
        self.data = data
        self.balance = start_balance
        self.positions: dict[str, float] = {}
        self.fee_bps_override = fee_bps_override
        self.orders: dict[str, Fill] = {}

    async def get_book(self, token_id: str) -> Book:
        return await self.data.get_book(token_id)

    async def get_balance(self) -> float:
        return self.balance

    async def get_fee_bps(self, token_id: str) -> int:
        if self.fee_bps_override:
            return self.fee_bps_override
        return await self.data.get_fee_bps(token_id)

    async def get_tick_size(self, token_id: str) -> float:
        return await self.data.get_tick_size(token_id)

    async def buy_fok(self, token_id: str, limit_price: float, size: float) -> Fill:
        oid = f"paper-{uuid.uuid4()}"
        book = await self.data.get_book(token_id)
        px = book.vwap_buy(size, limit_price)
        if px is None:
            f = Fill(oid, "rejected", reason="paper_fok_not_filled")
        else:
            fee = fee_amount(px * size, px, await self.get_fee_bps(token_id))
            cost = px * size + fee
            if cost > self.balance + 1e-9:
                f = Fill(oid, "rejected", reason="paper_insufficient_balance")
            else:
                self.balance -= cost
                self.positions[token_id] = self.positions.get(token_id, 0.0) + size
                f = Fill(oid, "filled", size, px)
        self.orders[oid] = f
        return f

    async def sell_fok(self, token_id: str, limit_price: float, size: float) -> Fill:
        oid = f"paper-{uuid.uuid4()}"
        if self.positions.get(token_id, 0.0) + 1e-9 < size:
            f = Fill(oid, "rejected", reason="paper_no_position")
        else:
            book = await self.data.get_book(token_id)
            px = book.vwap_sell(size, limit_price)
            if px is None:
                f = Fill(oid, "rejected", reason="paper_fok_not_filled")
            else:
                fee = fee_amount(px * size, px, await self.get_fee_bps(token_id))
                self.balance += px * size - fee
                self.positions[token_id] -= size
                f = Fill(oid, "filled", size, px)
        self.orders[oid] = f
        return f

    async def get_filled_size(self, order_id: str) -> float:
        f = self.orders.get(order_id)
        return f.filled_size if f else 0.0

    async def cancel(self, order_id: str) -> bool:
        return True  # FOK never rests in the book

    async def cancel_all(self) -> None:
        return None

    async def ping(self) -> None:
        await self.data.ping()


# ---------------------------------------------------------------- LIVE
class LiveVenue(Venue):
    """Real orders via py-clob-client. Test with minimal size first!"""

    sends_real_orders = True

    def __init__(self, host: str, chain_id: int, private_key: str, funder: str, signature_type: int):
        if not private_key:
            raise RuntimeError("EXEC_PRIVATE_KEY is empty")
        from py_clob_client.client import ClobClient  # imported in live mode only

        self._c = ClobClient(
            host, chain_id=chain_id, key=private_key,
            signature_type=signature_type, funder=funder or None,
        )
        self._c.set_api_creds(self._c.create_or_derive_api_creds())

    async def _call(self, fn, *a, **kw):
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "status_code", None)
            critical = code is None or code in (401, 403) or code >= 500
            raise VenueError(f"{type(e).__name__}:{code}:{getattr(e, 'error_msg', e)}", critical) from e

    async def get_book(self, token_id: str) -> Book:
        b = await self._call(self._c.get_order_book, token_id)
        return Book(bids=[(x.price, x.size) for x in b.bids or []],
                    asks=[(x.price, x.size) for x in b.asks or []])

    async def get_balance(self) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        r = await self._call(self._c.get_balance_allowance,
                             BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(r["balance"]) / 1e6  # USDC, 6 decimals

    async def get_fee_bps(self, token_id: str) -> int:
        return int(await self._call(self._c.get_fee_rate_bps, token_id))

    async def get_tick_size(self, token_id: str) -> float:
        return float(await self._call(self._c.get_tick_size, token_id))

    async def _post_fok(self, token_id: str, side: str, price: float, size: float) -> Fill:
        from py_clob_client.clob_types import OrderArgs, OrderType

        signed = await self._call(self._c.create_order,
                                  OrderArgs(token_id=token_id, price=price, size=size, side=side))
        try:
            r = await self._call(self._c.post_order, signed, OrderType.FOK)
        except VenueError as e:
            if e.critical:
                raise
            return Fill("", "rejected", reason=str(e))  # FOK not filled - 4xx, not critical
        oid = r.get("orderID", "")
        if not r.get("success", False) or not oid:
            return Fill(oid, "rejected", reason=r.get("errorMsg", "post_failed"), raw=r)
        filled = await self.get_filled_size(oid)
        status = "filled" if filled > 0 else "rejected"
        return Fill(oid, status, filled, price, reason="" if filled else "not_matched", raw=r)

    async def buy_fok(self, token_id: str, limit_price: float, size: float) -> Fill:
        return await self._post_fok(token_id, "BUY", limit_price, size)

    async def sell_fok(self, token_id: str, limit_price: float, size: float) -> Fill:
        return await self._post_fok(token_id, "SELL", limit_price, size)

    async def get_filled_size(self, order_id: str) -> float:
        o = await self._call(self._c.get_order, order_id)
        return float((o or {}).get("size_matched") or 0)

    async def cancel(self, order_id: str) -> bool:
        try:
            await self._call(self._c.cancel, order_id)
            return True
        except VenueError:
            return False

    async def cancel_all(self) -> None:
        await self._call(self._c.cancel_all)

    async def ping(self) -> None:
        await self._call(self._c.get_server_time)
