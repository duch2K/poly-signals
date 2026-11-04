"""Action journal (spec §7): one JSON line per event - time, signal_id, action, reason."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("execution")


class Journal:
    def __init__(self, path: Path | str, mode: str, alert_webhook_url: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.alert_webhook_url = alert_webhook_url
        self._lock = threading.Lock()

    def write(self, action: str, signal_id: str | None = None, reason: str = "", **extra: Any) -> dict:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "signal_id": signal_id,
            "action": action,
            "reason": reason,
            **extra,
        }
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        log.info(line)
        return rec

    def alert(self, kind: str, reason: str, **extra: Any) -> None:
        """Alert: write to journal + optional webhook. Webhook failure never crashes the module."""
        self.write("alert", reason=reason, alert=kind, **extra)
        if self.alert_webhook_url:
            try:
                httpx.post(
                    self.alert_webhook_url,
                    json={"alert": kind, "reason": reason, "mode": self.mode, **extra},
                    timeout=5,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("alert webhook failed: %s", e)

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
