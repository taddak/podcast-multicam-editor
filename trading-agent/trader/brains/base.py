"""What every brain receives and returns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

from ..exchange import Exchange
from ..market import SymbolSnapshot


@dataclass
class TradeIntent:
    action: str                      # buy | sell
    symbol: str
    amount_quote: float = 0.0        # buy: how much quote currency to spend
    sell_fraction: float = 1.0       # sell: share of the position to sell
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    reason: str = ""


@dataclass
class Decision:
    intents: list[TradeIntent] = field(default_factory=list)
    market_view: str = ""
    notes: str = ""
    next_check_minutes: int | None = None
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DecisionContext:
    now: float
    run_start: float
    run_end: float
    mode: str
    exchange_name: str
    quote: str
    account: dict                    # cash, equity, start capital, P&L, fees, risk state, ...
    positions: list[dict]            # open positions with live P&L and stop levels
    snapshots: list[SymbolSnapshot]
    constraints: dict                # risk limits the brain must respect
    recent_trades: list[dict]
    recent_decisions: list[dict]
    previous_notes: str
    exchange: Exchange               # for tools that need more data (get_candles)


class BrainError(Exception):
    pass


class Brain(Protocol):
    name: str

    def decide(self, ctx: DecisionContext) -> Decision: ...
