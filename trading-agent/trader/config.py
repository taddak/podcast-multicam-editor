"""Configuration: built-in defaults, overridden by a TOML file, then by CLI flags.

Secrets never live in the TOML file. They come from the environment (or a local
`.env` file): EXCHANGE_API_KEY, EXCHANGE_API_SECRET, EXCHANGE_API_PASSWORD and
ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .security import PROJECT_DIR, SECRET_KEYS

STABLECOINS = {"USDT", "USDC", "EURC", "EURT", "EURI", "EURQ", "EURR", "DAI", "PYUSD", "FDUSD", "TUSD", "USDG", "RLUSD", "USDE"}

DEFAULT_SYMBOLS = [
    "BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR", "DOGE/EUR",
    "LINK/EUR", "AVAX/EUR", "DOT/EUR", "LTC/EUR", "SUI/EUR", "TRX/EUR",
]


@dataclass
class RunConfig:
    mode: str = "paper"                      # paper | live
    name: str = "week1"                      # run folder name under runs_dir; reusing it resumes the run
    capital: float = 10.0                    # budget in the quote currency the agent may use
    duration_hours: float = 168.0            # one week
    tick_seconds: int = 60                   # how often stops / take-profits are checked
    decision_interval_minutes: int = 120     # default time between brain decisions (Claude picks its own pace)
    min_decision_interval_minutes: int = 30
    max_decision_interval_minutes: int = 360
    equity_log_minutes: int = 15
    close_all_at_end: bool = True
    runs_dir: str = "runs"
    max_live_capital: float = 50.0           # live mode refuses a larger capital (guards against typos)


@dataclass
class ExchangeConfig:
    id: str = "bitvavo"                      # any ccxt id: bitvavo, kraken, myokx, bybiteu, coinbase, ...
    quote: str = "EUR"
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    min_quote_volume_24h: float = 250_000.0  # skip illiquid markets
    max_spread_pct: float = 0.35
    taker_fee: float | None = None           # override the fee ccxt reports (fraction, 0.0025 = 0.25 %)
    paper_slippage_pct: float = 0.05
    options: dict = field(default_factory=dict)  # passed straight to the ccxt constructor


@dataclass
class RiskConfig:
    max_positions: int = 2
    max_position_pct: float = 100.0          # max share of equity in one position
    min_order_buffer: float = 1.3            # position >= exchange minimum x this, so it stays sellable after a drop
    default_stop_loss_pct: float = 3.0
    min_stop_loss_pct: float = 1.0
    max_stop_loss_pct: float = 8.0
    default_take_profit_pct: float = 6.0
    max_take_profit_pct: float = 40.0
    trailing_stop: bool = True
    daily_loss_limit_pct: float = 8.0        # halt new entries for the rest of the UTC day
    max_drawdown_pct: float = 25.0           # kill switch: close everything and stop
    max_trades_per_day: int = 8
    symbol_cooldown_minutes: int = 120       # no re-entry right after a stop-loss on the same symbol


@dataclass
class BrainConfig:
    type: str = "claude"                     # claude | rules
    model: str = "claude-opus-5-5"
    effort: str = "medium"                   # low | medium | high | xhigh | max
    max_tool_rounds: int = 6
    llm_budget_usd: float = 5.0              # hard cap on Claude API spend for the run
    web_search: bool = False                 # let Claude search the web for news (extra cost)
    fallback_to_rules: bool = True           # use the rule brain when Claude fails or the budget is spent


@dataclass
class Config:
    run: RunConfig = field(default_factory=RunConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)

    @property
    def run_dir(self) -> Path:
        base = Path(self.run.runs_dir)
        if not base.is_absolute():
            base = PROJECT_DIR / base  # same place no matter which directory the command runs from
        return base / self.run.name


def _apply(section: object, values: dict, where: str) -> None:
    known = {f.name for f in fields(section)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"Unknown setting [{where}] {key!r}; known: {sorted(known)}")
        setattr(section, key, value)


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    if path:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        for name, values in data.items():
            if not hasattr(cfg, name) or not isinstance(values, dict):
                raise ValueError(f"Unknown config section [{name}]")
            _apply(getattr(cfg, name), values, name)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.run.mode not in ("paper", "live"):
        raise ValueError("run.mode must be 'paper' or 'live'")
    if cfg.brain.type not in ("claude", "rules"):
        raise ValueError("brain.type must be 'claude' or 'rules'")
    if cfg.run.capital <= 0:
        raise ValueError("run.capital must be positive")
    if not cfg.exchange.symbols:
        raise ValueError("exchange.symbols must not be empty")
    for symbol in cfg.exchange.symbols:
        if not symbol.endswith("/" + cfg.exchange.quote):
            raise ValueError(f"symbol {symbol} does not use quote currency {cfg.exchange.quote}")
        if symbol.split("/")[0] in STABLECOINS:
            # Stablecoins (e-money tokens) fall outside the Czech 100,000 CZK crypto tax exemption.
            raise ValueError(f"{symbol}: trading stablecoins is disabled")
    if cfg.risk.max_positions < 1:
        raise ValueError("risk.max_positions must be >= 1")


def load_dotenv(path: str | Path) -> list[str]:
    """Load the secret variables from a .env file; existing environment variables win.

    Only the allow-listed key names are read, so a .env file cannot redirect traffic
    (e.g. ANTHROPIC_BASE_URL) or change anything else about the process. Returns the
    names of ignored keys.
    """
    p = Path(path)
    ignored: list[str] = []
    if not p.is_file():
        return ignored
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key not in SECRET_KEYS:
            ignored.append(key)
            continue
        if value:
            os.environ.setdefault(key, value)
    return ignored


def exchange_credentials() -> dict:
    creds = {
        "apiKey": os.environ.get("EXCHANGE_API_KEY", ""),
        "secret": os.environ.get("EXCHANGE_API_SECRET", ""),
        "password": os.environ.get("EXCHANGE_API_PASSWORD", ""),
    }
    return {k: v for k, v in creds.items() if v}
