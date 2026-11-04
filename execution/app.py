"""Module wiring + HTTP API + CLI.

  python -m execution.app serve  --port 8080          # HTTP: POST /signal, /kill, /resume, GET /status
  python -m execution.app replay signals.jsonl        # replay a signals file (one JSON per line)
  python -m execution.app kill "reason"               # manual kill switch
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass

from fastapi import FastAPI, Request

from .config import Mode, Settings
from .executor import Executor
from .journal import Journal
from .safety import Heartbeat, KillSwitch
from .store import Store
from .venue import GammaResolver, HttpMarketData, LiveVenue, MarketResolver, PaperVenue, Venue


@dataclass
class Module:
    settings: Settings
    venue: Venue
    store: Store
    journal: Journal
    kill: KillSwitch
    executor: Executor
    heartbeat: Heartbeat


def build_venue(s: Settings) -> Venue:
    s.assert_safe()
    if s.mode == Mode.PAPER:
        return PaperVenue(HttpMarketData(s.clob_host), s.paper_start_balance, s.paper_fee_bps)
    return LiveVenue(s.clob_host, s.chain_id, s.private_key, s.funder, s.signature_type)


def build(s: Settings | None = None, venue: Venue | None = None,
          resolver: MarketResolver | None = None, sleep=None) -> Module:
    s = s or Settings()
    venue = venue or build_venue(s)
    resolver = resolver or GammaResolver(s.gamma_host)
    store = Store(s.db_path)
    journal = Journal(s.journal_path, s.mode.value, s.alert_webhook_url)
    kill = KillSwitch(s.kill_file, journal)
    ex = Executor(s, venue, resolver, store, journal, kill, sleep=sleep)
    hb = Heartbeat(venue, kill, journal, s.heartbeat_interval_s, s.heartbeat_max_failures)
    journal.write("startup", reason="", real_orders=venue.sends_real_orders, kill_active=kill.active)
    return Module(s, venue, store, journal, kill, ex, hb)


# ---------------------------------------------------------------- HTTP
def create_http_app(m: Module):
    from contextlib import asynccontextmanager

    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(_app):
        hb = asyncio.create_task(m.heartbeat.run())
        yield
        hb.cancel()

    app = FastAPI(title="polymarket-execution", lifespan=lifespan)

    @app.post("/signal")
    async def signal(req: Request) -> dict:
        try:
            raw = await req.json()
        except Exception:  # noqa: BLE001
            raw = None
        sig, res = m.executor.accept(raw if isinstance(raw, dict) else {})
        if sig is not None:
            t = asyncio.create_task(m.executor.run(sig))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        return res

    @app.post("/kill")
    async def kill(reason: str = "manual") -> dict:
        m.kill.trip(f"manual:{reason}")
        await m.venue.cancel_all()
        return {"kill": True}

    @app.post("/resume")
    async def resume() -> dict:
        m.kill.reset("http")
        return {"kill": m.kill.active}

    @app.get("/status")
    async def status() -> dict:
        return {
            "mode": m.settings.mode.value,
            "real_orders": m.venue.sends_real_orders,
            "kill": m.kill.active,
            "kill_reason": m.kill.reason,
            "daily_loss": m.store.daily_realized_loss(),
            "unhedged": m.store.unhedged_count(),
            "balance": await m.venue.get_balance(),
            "heartbeat_failures": m.heartbeat.failures,
        }

    return app


# ---------------------------------------------------------------- CLI
async def _replay(m: Module, path: str) -> None:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    raw = {}
                print(json.dumps(await m.executor.handle(raw), ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser("execution")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve"); sv.add_argument("--port", type=int, default=8080)
    rp = sub.add_parser("replay"); rp.add_argument("file")
    kl = sub.add_parser("kill"); kl.add_argument("reason", nargs="?", default="manual")
    sub.add_parser("resume")
    a = p.parse_args(argv)

    s = Settings()
    if a.cmd in ("kill", "resume"):
        j = Journal(s.journal_path, s.mode.value)
        k = KillSwitch(s.kill_file, j)
        k.trip(f"manual:{a.reason}") if a.cmd == "kill" else k.reset("cli")
        print({"kill": k.active})
        return

    m = build(s)
    if a.cmd == "replay":
        asyncio.run(_replay(m, a.file))
    else:
        import uvicorn
        uvicorn.run(create_http_app(m), host="127.0.0.1", port=a.port)


if __name__ == "__main__":
    main(sys.argv[1:])
