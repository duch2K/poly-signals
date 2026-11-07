"""Module settings. Loaded from .env / environment variables (prefix EXEC_)."""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXEC_", env_file=".env", extra="ignore")

    # --- mode ---
    mode: Mode = Mode.PAPER  # paper ONLY by default
    live_confirm: str = ""   # live requires EXEC_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY

    # --- limits (spec §4) ---
    max_size_per_market: float = Field(20.0, gt=0)   # USDC per market (both legs)
    max_daily_loss: float = Field(10.0, gt=0)        # USDC, reached -> stop
    max_unhedged: int = Field(1, ge=0)               # unhedged legs at once
    min_liquidity_mult: float = Field(1.0, ge=1.0)   # book depth >= size * mult
    max_slippage: float = Field(0.01, ge=0)          # tolerance vs signal price (absolute, 1 cent)
    min_edge: float = Field(0.005, ge=-0.1)          # min profit per share after fees;
                                                     # NEGATIVE = paper test only (accepts a small loss)
    max_balance: float = Field(200.0, gt=0)          # account balance cap
    balance_limit_blocks_trading: bool = True        # balance over cap -> no new trades
    max_signal_age_s: float = Field(30.0, gt=0)      # reject stale signals
    max_lag_s: int = Field(120, ge=0)

    # --- execution ---
    unwind_max_slippage: float = Field(0.03, ge=0)   # how far below best bid we accept when unwinding
    unwind_retries: int = Field(3, ge=1)
    order_check_retries: int = Field(5, ge=1)
    order_check_interval_s: float = Field(0.5, ge=0)

    # --- safety ---
    heartbeat_interval_s: float = Field(5.0, gt=0)
    heartbeat_max_failures: int = Field(3, ge=1)
    kill_file: Path = Path("data/KILL")

    # --- storage / journal ---
    db_path: Path = Path("data/execution.sqlite")
    journal_path: Path = Path("data/journal.jsonl")
    alert_webhook_url: str = ""  # optional: POST JSON (Telegram bot, Slack, etc.)

    # --- paper ---
    paper_start_balance: float = 100.0
    paper_fee_bps: int = 0  # 0 -> fee from API (fee-rate), otherwise this value

    # --- Polymarket (live only) ---
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    private_key: str = ""        # dedicated bot-only wallet
    funder: str = ""             # Polymarket proxy address (if used)
    signature_type: int = 1

    def assert_safe(self) -> None:
        if self.mode == Mode.LIVE and self.live_confirm != "I_UNDERSTAND_REAL_MONEY":
            raise RuntimeError(
                "LIVE mode requires EXEC_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY"
            )
        if self.mode == Mode.LIVE and self.min_edge < 0:
            raise RuntimeError("negative EXEC_MIN_EDGE is a paper-only test setting, refused in LIVE mode")
