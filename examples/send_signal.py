"""Send one test signal to a running server: python examples/send_signal.py <market-slug>"""
import datetime
import json
import sys
import urllib.request
import uuid

slug = sys.argv[1] if len(sys.argv) > 1 else "PUT_SLUG_HERE"
sig = {
    "signal_id": str(uuid.uuid4()),
    "market": slug,
    "size": 5,
    "price_up": 0.48,
    "price_down": 0.49,
    "lag_seconds": 5,
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
req = urllib.request.Request(
    "http://127.0.0.1:8080/signal",
    data=json.dumps(sig).encode(),
    headers={"Content-Type": "application/json"},
)
print(urllib.request.urlopen(req).read().decode())
