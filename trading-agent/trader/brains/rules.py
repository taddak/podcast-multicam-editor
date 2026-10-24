"""Deterministic baseline: 1h trend-following with volatility-based stops.

Free to run, used for backtests and as the fallback when Claude is unavailable
or its budget is spent."""

from __future__ import annotations

from .base import Decision, DecisionContext, TradeIntent


class RuleBrain:
    name = "rules"

    def __init__(self, max_positions: int, min_stop_pct: float = 1.0, max_stop_pct: float = 8.0,
                 reward_risk: float = 2.5):
        self.max_positions = max_positions
        self.min_stop_pct = min_stop_pct
        self.max_stop_pct = max_stop_pct
        self.reward_risk = reward_risk

    def decide(self, ctx: DecisionContext) -> Decision:
        snaps = {s.symbol: s for s in ctx.snapshots}
        intents: list[TradeIntent] = []
        held = {p["symbol"] for p in ctx.positions}

        for pos in ctx.positions:
            s = snaps.get(pos["symbol"])
            if s is None:
                continue
            if s.trend_1h == "down":
                intents.append(TradeIntent("sell", s.symbol, reason="1h trend turned down"))
            elif s.rsi_1h is not None and s.rsi_1h > 80:
                intents.append(TradeIntent("sell", s.symbol, reason=f"overbought, RSI {s.rsi_1h:.0f}"))

        free_slots = self.max_positions - len(held) + len(intents)
        cash = float(ctx.account["available_cash"])
        if ctx.account.get("risk_state") == "ok" and free_slots > 0:
            candidates = []
            for s in ctx.snapshots:
                if s.symbol in held or s.trend_1h != "up" or s.rsi_1h is None or not s.atr_pct_1h:
                    continue
                if not 52 <= s.rsi_1h <= 70 or (s.change_24h or 0) <= 0:
                    continue
                if not 0.25 <= s.atr_pct_1h <= 3.0:
                    continue
                if s.price > s.ema20_1h * (1 + 1.5 * s.atr_pct_1h / 100):
                    continue  # too extended above the average, don't chase
                score = ((s.change_24h or 0) + (s.change_4h or 0)) / s.atr_pct_1h
                candidates.append((score, s))
            candidates.sort(key=lambda c: c[0], reverse=True)

            min_pos = ctx.constraints.get("min_position_by_symbol", {})
            slots = free_slots
            for _, s in candidates:
                if slots <= 0:
                    break
                needed = float(min_pos.get(s.symbol, 0.0))
                if cash < needed or cash <= 0:
                    break
                amount = cash / slots
                if amount < needed:
                    amount, slots = cash, 1
                stop = min(max(2.0 * s.atr_pct_1h, self.min_stop_pct), self.max_stop_pct)
                intents.append(TradeIntent(
                    "buy", s.symbol, amount_quote=round(amount, 2), sell_fraction=0.0,
                    stop_loss_pct=round(stop, 2), take_profit_pct=round(stop * self.reward_risk, 2),
                    reason=f"1h uptrend, RSI {s.rsi_1h:.0f}, 24h {s.change_24h:+.1f}%",
                ))
                cash -= amount
                slots -= 1

        return Decision(intents=intents, market_view="rule-based trend following",
                        next_check_minutes=60, source=self.name)
