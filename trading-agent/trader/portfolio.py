"""The agent's own ledger. It tracks only the capital assigned to the agent,
never the whole exchange account."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .clock import day_key
from .exchange import Fill


@dataclass
class Position:
    symbol: str
    amount: float
    entry_price: float       # average execution price
    cost: float              # quote spent including fees
    opened_at: float
    stop_loss_pct: float
    stop_price: float
    take_profit_price: float | None
    highest_price: float
    trailing: bool = True
    reason: str = ""

    def value(self, price: float) -> float:
        return self.amount * price

    def pnl_pct(self, price: float) -> float:
        return (self.value(price) / self.cost - 1.0) * 100.0 if self.cost else 0.0


@dataclass
class ClosedTrade:
    symbol: str
    amount: float
    entry_price: float
    exit_price: float
    cost: float
    proceeds: float
    pnl: float
    pnl_pct: float
    opened_at: float
    closed_at: float
    exit_reason: str


@dataclass
class Portfolio:
    start_capital: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    closed: list[ClosedTrade] = field(default_factory=list)
    fees_paid: float = 0.0
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    trades_today: int = 0
    cooldowns: dict[str, float] = field(default_factory=dict)
    llm_cost_usd: float = 0.0

    @classmethod
    def new(cls, capital: float) -> "Portfolio":
        return cls(start_capital=capital, cash=capital, peak_equity=capital, day_start_equity=capital)

    # ---- valuation -------------------------------------------------------------
    def equity(self, prices: dict[str, float]) -> float:
        total = self.cash
        for symbol, pos in self.positions.items():
            total += pos.value(prices.get(symbol, pos.entry_price))
        return total

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.closed)

    def roll_day(self, now: float, equity: float) -> None:
        key = day_key(now)
        if key != self.day:
            self.day = key
            self.day_start_equity = equity
            self.trades_today = 0

    def mark(self, equity: float) -> None:
        self.peak_equity = max(self.peak_equity, equity)

    # ---- bookkeeping -----------------------------------------------------------
    def apply_buy(self, fill: Fill, now: float, stop_loss_pct: float, take_profit_pct: float | None,
                  trailing: bool, reason: str) -> Position:
        if fill.symbol in self.positions:
            raise ValueError(f"already holding {fill.symbol}")
        self.cash -= fill.quote_amount
        self.fees_paid += fill.fee
        self.trades_today += 1
        pos = Position(
            symbol=fill.symbol,
            amount=fill.amount,
            entry_price=fill.price,
            cost=fill.quote_amount,
            opened_at=now,
            stop_loss_pct=stop_loss_pct,
            stop_price=fill.price * (1 - stop_loss_pct / 100.0),
            take_profit_price=fill.price * (1 + take_profit_pct / 100.0) if take_profit_pct else None,
            highest_price=fill.price,
            trailing=trailing,
            reason=reason,
        )
        self.positions[fill.symbol] = pos
        return pos

    def apply_sell(self, fill: Fill, now: float, reason: str) -> ClosedTrade:
        pos = self.positions[fill.symbol]
        amount = min(fill.amount, pos.amount)
        share = amount / pos.amount if pos.amount else 1.0
        cost_part = pos.cost * share
        self.cash += fill.quote_amount
        self.fees_paid += fill.fee
        self.trades_today += 1
        trade = ClosedTrade(
            symbol=fill.symbol,
            amount=amount,
            entry_price=pos.entry_price,
            exit_price=fill.price,
            cost=cost_part,
            proceeds=fill.quote_amount,
            pnl=fill.quote_amount - cost_part,
            pnl_pct=(fill.quote_amount / cost_part - 1.0) * 100.0 if cost_part else 0.0,
            opened_at=pos.opened_at,
            closed_at=now,
            exit_reason=reason,
        )
        self.closed.append(trade)
        remaining = pos.amount - amount
        if share >= 0.999999 or remaining <= 0:
            del self.positions[fill.symbol]
        else:
            pos.amount = remaining
            pos.cost -= cost_part
        return trade

    # ---- persistence -------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Portfolio":
        data = dict(data)
        data["positions"] = {s: Position(**p) for s, p in data.get("positions", {}).items()}
        data["closed"] = [ClosedTrade(**t) for t in data.get("closed", [])]
        return cls(**data)
