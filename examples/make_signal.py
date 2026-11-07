"""Build a test signal from a REAL market's current order book and send it to the running server.

    python examples/make_signal.py <market-slug> [size] [lag_seconds]

Prices in the signal = current best asks, so the risk checks run against real data.
Prints the signal and the server's answer. Final result is in data/journal.jsonl.
"""
import datetime
import json
import sys
import uuid

import httpx

slug = sys.argv[1]
size = float(sys.argv[2]) if len(sys.argv) > 2 else 5
lag = float(sys.argv[3]) if len(sys.argv) > 3 else 5

GAMMA = "https://gamma-api.polymarket.com"
slug = slug.strip().rstrip("/").split("/")[-1].split("?")[0]  # accept a full polymarket.com URL too

markets = httpx.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10).json()
if not markets:
    # maybe it is an EVENT slug (the polymarket.com/event/... URL) -> list its markets
    events = httpx.get(f"{GAMMA}/events", params={"slug": slug}, timeout=10).json()
    if not events:
        sys.exit(f"market or event not found: {slug!r}")
    open_markets = [x for x in events[0].get("markets", []) if not x.get("closed")]
    if not open_markets:
        sys.exit(f"event {slug!r} has no open markets")
    if len(open_markets) > 1:
        print(f"{slug!r} is an event with {len(open_markets)} markets. Pick one market slug:")
        for x in open_markets:
            print("  ", x["slug"])
        sys.exit(1)
    markets = open_markets
    slug = markets[0]["slug"]
    print(f"event -> market slug: {slug}")

if markets[0].get("closed"):
    sys.exit(f"market {slug!r} is closed")
ids = markets[0]["clobTokenIds"]
ids = json.loads(ids) if isinstance(ids, str) else ids


def best_ask(token: str) -> float:
    book = httpx.get("https://clob.polymarket.com/book", params={"token_id": token}, timeout=10).json()
    asks = [float(a["price"]) for a in book.get("asks", [])]
    if not asks:
        sys.exit(f"no asks for token {token}")
    return min(asks)


up, down = best_ask(ids[0]), best_ask(ids[1])
print(f"best ask up={up} down={down} sum={up + down:.4f} -> {'EDGE' if up + down < 1 else 'no edge'}")

sig = {
    "signal_id": str(uuid.uuid4()),
    "market": slug,
    "size": size,
    "price_up": up,
    "price_down": down,
    "lag_seconds": lag,
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
print(json.dumps(sig, indent=2))
print(httpx.post("http://127.0.0.1:8080/signal", json=sig, timeout=10).json())
