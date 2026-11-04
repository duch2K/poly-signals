# Polymarket Execution — complete-set arbitrage

Receives a signal → runs risk checks → places leg 1 → waits `lag_seconds` → places leg 2 →
if leg 2 fails, unwinds leg 1 → journals every step. **Paper mode by default**: no real orders.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # paper
pip install -e ".[dev,live]"     # + py-clob-client for live
cp .env.example .env
pytest                           # 25 tests = spec acceptance criteria
```

## Run

```bash
python -m execution.app serve --port 8080      # HTTP + heartbeat
python -m execution.app replay examples/signals.jsonl
python -m execution.app kill "reason"          # manual stop
python -m execution.app resume                 # clear stop (manual only)
```

HTTP:

| method | path | purpose |
|---|---|---|
| POST | `/signal` | JSON signal (spec §3). Response: `accepted / rejected / duplicate` + `reason` |
| POST | `/kill?reason=...` | kill switch + `cancel_all` |
| POST | `/resume` | clear kill switch |
| GET | `/status` | mode, kill state, daily loss, unhedged legs, balance, heartbeat |

The server binds to `127.0.0.1` only and has no auth — do not expose it.

### Signal format

```json
{
  "signal_id": "uuid",
  "market": "market slug | numeric id | 0x conditionId",
  "size": 10.0,
  "price_up": 0.48,
  "price_down": 0.49,
  "lag_seconds": 20,
  "timestamp": "ISO-8601"
}
```

All fields required. `size` = shares per leg. Duplicate `signal_id` is ignored (persisted in SQLite).

## Layout

```
execution/
  config.py    settings (.env, prefix EXEC_), guard against accidental live mode
  signals.py   signal model + validation
  store.py     SQLite: signal_id dedup, legs, daily PnL
  journal.py   JSON-lines journal + alerts (webhook)
  venue.py     Venue: PaperVenue (real order book, no orders) / LiveVenue (py-clob-client, FOK)
  risk.py      the 6 pre-trade checks (spec §4)
  safety.py    KillSwitch (file) + Heartbeat
  executor.py  two-leg logic + unwind
  app.py       wiring, HTTP, CLI
tests/         acceptance tests
```

## Execution flow

1. `accept`: validate → dedup (SQLite, survives restart) → kill switch? → signal age.
2. Snapshot: both books, balance, fees, tick size. Order limit = signal price + `max_slippage`.
3. Checks: size per market, daily loss, unhedged legs, liquidity, slippage
   + real edge `1 − vwap_up − vwap_down − fees ≥ min_edge`, balance / balance cap.
4. The **less liquid** side goes first. FOK = all-or-nothing.
5. Wait `lag_seconds`; interruptible by the kill switch.
6. Leg 2 limit is capped at `1 − price_1 − fees − min_edge` → the pair is profitable or does not fill.
7. Verify fill (`get_filled_size`). Not filled / partial → `cancel` → sell leg 1 at best bid
   (no lower than `bid − unwind_max_slippage`); the loss counts toward the daily limit.
8. Unwind fails after `unwind_retries` → leg marked `stuck`, kill switch, alert. Resolve manually.

## Auto-stop (kill switch = file `data/KILL`)

* manual (CLI / HTTP);
* daily realized loss ≥ `max_daily_loss`;
* heartbeat: `heartbeat_max_failures` consecutive failures (+ `cancel_all`);
* critical API error (401/403/5xx/no response) or any unexpected exception;
* failed unwind.

The file survives restarts. Clear it manually only.

## Balance cap

`balance > max_balance` → `withdraw` alert with the amount to withdraw (fires once until balance drops).
With `balance_limit_blocks_trading=true`, no new trades are opened.

## Journal

`data/journal.jsonl`, one JSON object per line:

```json
{"ts":"...","mode":"paper","signal_id":"...","action":"rejected","reason":"liquidity:up=3.00,down=100.00,need=10.00"}
```

Actions: `startup, received, duplicate, rejected, risk_ok, leg1_sent, leg1_filled, leg1_failed,
leg2_sent, leg2_failed, leg2_skipped, completed, unwind_sent, unwind_retry, unwound, error,
heartbeat_fail, kill, resume, alert`.

## TODO before live

- [ ] Verify the fee formula against docs.polymarket.com (`fee_amount` is an upper bound for now).
- [ ] `LiveVenue`: actual fill price from `makingAmount/takingAmount` (currently = limit price).
- [ ] Market minimum order size (`min_order_size`) — add to risk checks.
- [ ] Market resolution: mark legs `settled`, otherwise `max_size_per_market` accumulates exposure.
- [ ] Optional: merge YES+NO back into USDC to free capital before resolution.
- [ ] Confirm that the daily reset should use UTC.
- [ ] One week of paper trading on live signals → live with `size` 1–5 and a small `max_daily_loss`.
- [ ] Dedicated bot-only wallet with minimal USDC. Private key only in `.env`, never in git.
