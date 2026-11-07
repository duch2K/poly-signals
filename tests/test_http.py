import time
from fastapi.testclient import TestClient

from execution.app import create_http_app

from .conftest import make_signal


def test_http_signal_kill_status(env):
    m = env(heartbeat_interval_s=60)
    with TestClient(create_http_app(m)) as c:
        sig = make_signal()
        assert c.post("/signal", json=sig).json()["status"] == "accepted"
        assert c.post("/signal", json=sig).json()["status"] == "duplicate"
        assert c.post("/signal", content=b"not json").json()["status"] == "rejected"
        for _ in range(50):  # execution runs in the background
            res = c.get(f"/signals/{sig['signal_id']}").json()
            if res["final"]:
                break
            time.sleep(0.02)
        assert res["final"] and res["status"] == "completed"
        assert "completed" in [e["action"] for e in res["events"]] and res["reason"] == "pair_hedged"
        assert c.get("/signals/nope").json()["status"] == "unknown"
        assert c.post("/kill", params={"reason": "test"}).json() == {"kill": True}
        assert c.post("/signal", json=make_signal()).json()["reason"].startswith("kill_switch_active")
        st = c.get("/status").json()
        assert st["mode"] == "paper" and st["real_orders"] is False and st["kill"] is True
        assert c.post("/resume").json() == {"kill": False}
