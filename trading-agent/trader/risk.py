"""Hard risk limits. The brain proposes, the risk manager disposes: nothing the
brain says can bypass these checks."""

from __future__ import annotations

from dataclasses import dataclass

from .config import RiskConfig
from .exchange import MarketInfo, Ticker
from .portfolio import Portfolio, Position

OK, HALT, KILL = "ok", "halt_entries", "kill"


@dataclass
class Verdict:
    ok: bool
    cost: float = 0.0
    amount: float = 0.0
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig, max_spread_pct: float):
        self.cfg = cfg
        self.max_spread_pct = max_spread_pct

    # ---- parameter clamps ----------------------------------------------------------
    def clamp_stop(self, pct: float | None) -> float:
        if not pct or pct <= 0:
            pct = self.cfg.default_stop_loss_pct
        return min(max(pct, self.cfg.min_stop_loss_pct), self.cfg.max_stop_loss_pct)

    def clamp_take_profit(self, pct: float | None) -> float | None:
        if not pct or pct <= 0:
            pct = self.cfg.default_take_profit_pct
        if not pct or pct <= 0:
            return None
        return min(pct, self.cfg.max_take_profit_pct)

    def min_position(self, market: MarketInfo, price: float) -> float:
        return market.min_order_value(price) * self.cfg.min_order_buffer

    # ---- account-level state -----------------------------------------------------------
    def state(self, p: Portfolio, equity: float) -> str:
        if equity <= p.peak_equity * (1 - self.cfg.max_drawdown_pct / 100.0):
            return KILL
        if equity <= p.day_start_equity * (1 - self.cfg.daily_loss_limit_pct / 100.0):
            return HALT
        if p.trades_today >= self.cfg.max_trades_per_day:
            return HALT
        return OK

    # ---- stops ---------------------------------------------------------------------------
    def update_trailing(self, p: Portfolio, prices: dict[str, float]) -> None:
        for symbol, pos in p.positions.items():
            price = prices.get(symbol)
            if price is None or price <= pos.highest_price:
                continue
            pos.highest_price = price
            if pos.trailing:
                pos.stop_price = max(pos.stop_price, price * (1 - pos.stop_loss_pct / 100.0))

    def exits(self, p: Portfolio, prices: dict[str, float]) -> list[tuple[str, str]]:
        out = []
        for symbol, pos in p.positions.items():
            price = prices.get(symbol)
            if price is None:
                continue
            if price <= pos.stop_price:
                out.append((symbol, "stop_loss" if pos.stop_price < pos.entry_price else "trailing_stop"))
            elif pos.take_profit_price and price >= pos.take_profit_price:
                out.append((symbol, "take_profit"))
        return out

    # ---- order checks ----------------------------------------------------------------------
    def check_buy(self, p: Portfolio, market: MarketInfo, ticker: Ticker, requested: float,
                  equity: float, available_cash: float, now: float, state: str) -> Verdict:
        symbol = market.symbol
        if state != OK:
            return Verdict(False, reason=f"risk state is {state}: no new entries")
        if symbol in p.positions:
            return Verdict(False, reason=f"already holding {symbol} (no averaging in)")
        if len(p.positions) >= self.cfg.max_positions:
            return Verdict(False, reason=f"max {self.cfg.max_positions} open positions")
        if p.cooldowns.get(symbol, 0) > now:
            return Verdict(False, reason=f"{symbol} is cooling down after a stop-loss")
        if ticker.spread_pct > self.max_spread_pct:
            return Verdict(False, reason=f"spread {ticker.spread_pct:.2f}% above {self.max_spread_pct}%")
        if requested <= 0:
            return Verdict(False, reason="non-positive amount")
        min_needed = self.min_position(market, ticker.ask)
        cap = min(available_cash, equity * self.cfg.max_position_pct / 100.0)
        cost = min(requested, cap)
        if cost < min_needed:
            if requested < min_needed:
                return Verdict(False, reason=f"requested {requested:.2f} below minimum position {min_needed:.2f}")
            return Verdict(False, reason=f"only {cap:.2f} available, minimum position is {min_needed:.2f}")
        return Verdict(True, cost=cost)

    def check_sell(self, pos: Position, market: MarketInfo, price: float, fraction: float) -> Verdict:
        fraction = min(max(fraction, 0.0), 1.0) or 1.0
        amount = pos.amount * fraction
        min_value = market.min_order_value(price)
        if fraction < 1.0 and (pos.amount - amount) * price < min_value * self.cfg.min_order_buffer:
            amount = pos.amount  # selling part would leave unsellable dust; sell everything
        if amount * price < min_value:
            if pos.amount * price < min_value:
                return Verdict(False, reason=f"{pos.symbol} position worth {pos.amount * price:.2f} is below the "
                                             f"exchange minimum {min_value:.2f}; cannot sell until price recovers")
            amount = pos.amount
        return Verdict(True, amount=amount)
