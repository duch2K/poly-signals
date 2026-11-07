import json, uuid, datetime, urllib.request
sig = {
    "signal_id": "eaccb7c0-437c-4ad6-a5a9-9e0c7a349019",
    "market": "will-gina-raimondo-win-the-2028-democratic-presidential-nomination-676",
    "size": 5.0,
    "price_up": 0.002,
    "price_down": 0.999,
    "lag_seconds": 5.0,
    "timestamp": "2026-11-07T15:02:36.186475+00:00"
}
req = urllib.request.Request("http://127.0.0.1:8080/signal",
      data=json.dumps(sig).encode(), headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
