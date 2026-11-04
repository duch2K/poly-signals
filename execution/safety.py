"""Kill switch and heartbeat (spec §5.1, §5.3).

Kill switch = a file on disk. Present -> trading stopped, survives restarts.
Reset manually only: delete the file (or POST /resume).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from .journal import Journal
from .venue import Venue


class KillSwitch:
    def __init__(self, path: Path | str, journal: Journal):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.journal = journal

    @property
    def active(self) -> bool:
        return self.path.exists()

    @property
    def reason(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def trip(self, reason: str) -> None:
        if not self.active:
            self.path.write_text(f"{datetime.now(timezone.utc).isoformat()} {reason}\n", encoding="utf-8")
            self.journal.write("kill", reason=reason)
            self.journal.alert("kill_switch", reason)

    def reset(self, who: str = "manual") -> None:
        if self.active:
            self.path.unlink()
            self.journal.write("resume", reason=f"kill_switch_reset_by:{who}")


class Heartbeat:
    """Pings the exchange. N failures in a row -> kill + cancel all orders."""

    def __init__(self, venue: Venue, kill: KillSwitch, journal: Journal,
                 interval_s: float, max_failures: int):
        self.venue, self.kill, self.journal = venue, kill, journal
        self.interval_s, self.max_failures = interval_s, max_failures
        self.failures = 0

    async def beat(self) -> bool:
        try:
            await asyncio.wait_for(self.venue.ping(), timeout=self.interval_s)
            self.failures = 0
            return True
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            self.journal.write("heartbeat_fail", reason=f"{type(e).__name__}:{e}", failures=self.failures)
            if self.failures >= self.max_failures and not self.kill.active:
                self.kill.trip(f"heartbeat_lost:{self.failures}")
                try:
                    await self.venue.cancel_all()
                except Exception as ce:  # noqa: BLE001
                    self.journal.write("error", reason=f"cancel_all_failed:{ce}")
            return False

    async def run(self) -> None:
        while True:
            await self.beat()
            await asyncio.sleep(self.interval_s)
