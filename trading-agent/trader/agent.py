"""The agent loop: every tick it guards open positions (stops, take-profits,
circuit breakers); when a decision is due it builds a market snapshot, asks the
brain, and executes whatever passes the risk manager."""

from __future__ import annotations

import logging
import statistics

import ccxt

from .brains.base import Brain, BrainError, Decision, DecisionContext, TradeIntent
from .clock import fmt_ts
from .config import Config
from .exchange import TRANSIENT_ERRORS, Exchange, OrderError, Ticker
from .journal import Journal
from .market import build_snapshot, select_universe
from .portfolio import Portfolio
from .risk import KILL, RiskManager

log = logging.getLogger("trader")

try:
    from .brains.claude import BudgetExceeded
except ImportError:  # anthropic not installed: only the rule brain is available
    class BudgetExceeded(BrainError):
        pass


class TradingAgent:
    def __init__(self, cfg: Config, exchange: Exchange, brain: Brain, journal: Journal, clock,
                 fallback: Brain | None = None):
        self.cfg = cfg
        self.ex = exchange
        self.brain = brain
        self.fallback = fallback
        self.journal = journal
        self.clock = clock
        self.risk = RiskManager(cfg.risk, cfg.exchange.max_spread_pct)
        self.portfolio: Portfolio = Portfolio.new(cfg.run.capital)
        self.universe: list[str] = []
        self.run_start = 0.0
        self.run_end = 0.0
        self.next_decision = 0.0
        self.next_equity_log = 0.0
        self.notes = ""
        self.recent_decisions: list[dict] = []
        self.brain_disabled = False
        self.llm_decisions = 0
        self.stopped_reason = ""
        self.finished = False
        self.last_tickers: dict[str, Ticker] = {}
        self._throttle: dict[str, float] = {}

    # ---- helpers -----------------------------------------------------------------------
    @property
    def prices(self) -> dict[str, float]:
        return {s: t.last for s, t in self.last_tickers.items()}

    def equity(self) -> float:
        return self.portfolio.equity(self.prices)

    def _quiet(self, key: str, now: float, every: float = 3600) -> bool:
        """True if `key` was reported less than `every` seconds ago (avoids log spam every tick)."""
        if now - self._throttle.get(key, -1e18) < every:
            return True
        self._throttle[key] = now
        return False

    def available_cash(self) -> float:
        cash = self.portfolio.cash
        if self.ex.is_live:
            free = self.ex.free_balance(self.cfg.exchange.quote) or 0.0
            fee = max((m.taker_fee for m in self.ex.markets.values()), default=0.0025)
            cash = min(cash, free) * (1 - fee) - 0.01  # headroom for exchanges that add the fee on top
        return max(cash, 0.0)

    def save_state(self) -> None:
        self.journal.save_state({
            "version": 1,
            "exchange": self.ex.name,
            "mode": self.cfg.run.mode,
            "run_start": self.run_start,
            "run_end": self.run_end,
            "next_decision": self.next_decision,
            "notes": self.notes,
            "recent_decisions": self.recent_decisions,
            "brain_disabled": self.brain_disabled,
            "llm_decisions": self.llm_decisions,
            "last_prices": self.prices,
            "finished": self.finished,
            "stopped_reason": self.stopped_reason,
            "portfolio": self.portfolio.to_dict(),
        })

    # ---- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        now = self.clock.now()
        markets = self.ex.load_markets(self.cfg.exchange.symbols)
        if not markets:
            raise RuntimeError(f"none of {self.cfg.exchange.symbols} is tradable on {self.ex.name}")
        missing = sorted(set(self.cfg.exchange.symbols) - set(markets))
        if missing:
            log.warning("not available on %s, skipped: %s", self.ex.name, ", ".join(missing))
        self.universe = list(markets)

        state = self.journal.load_state()
        if state and state.get("finished"):
            raise RuntimeError(f"run in {self.journal.dir} already finished; pick another --name")
        if state:
            self.portfolio = Portfolio.from_dict(state["portfolio"])
            self.run_start, self.run_end = state["run_start"], state["run_end"]
            self.notes = state.get("notes", "")
            self.recent_decisions = state.get("recent_decisions", [])
            self.brain_disabled = state.get("brain_disabled", False)
            self.llm_decisions = state.get("llm_decisions", 0)
            self.next_decision = now
            self.last_tickers = {s: Ticker(s, p, p, p, 0.0) for s, p in state.get("last_prices", {}).items()}
            if self.ex.is_live:
                self._reconcile_positions(now)
            self.journal.log("resume", now, equity=self.equity())
            log.info("resumed run %s: equity %.2f %s, %d open positions", self.journal.dir.name,
                     self.equity(), self.cfg.exchange.quote, len(self.portfolio.positions))
        else:
            capital = self.cfg.run.capital
            if self.ex.is_live:
                free = self.ex.free_balance(self.cfg.exchange.quote) or 0.0
                if free < capital:
                    log.warning("only %.2f %s free on the exchange; using that instead of %.2f",
                                free, self.cfg.exchange.quote, capital)
                    capital = free
                if capital <= 0:
                    raise RuntimeError(f"no free {self.cfg.exchange.quote} on the exchange")
            self.portfolio = Portfolio.new(capital)
            self.run_start = now
            self.run_end = now + self.cfg.run.duration_hours * 3600
            self.next_decision = now
            self.journal.log("start", now, capital=capital, exchange=self.ex.name, mode=self.cfg.run.mode,
                             brain=self.brain.name, symbols=self.universe, run_end=self.run_end)
            log.info("started %s run on %s with %.2f %s, brain=%s, until %s", self.cfg.run.mode, self.ex.name,
                     capital, self.cfg.exchange.quote, self.brain.name, fmt_ts(self.run_end))
        if hasattr(self.brain, "spent_usd"):
            self.brain.spent_usd = self.portfolio.llm_cost_usd
        self.next_equity_log = now
        self.save_state()

    def _reconcile_positions(self, now: float) -> None:
        for symbol, pos in list(self.portfolio.positions.items()):
            base = self.ex.markets[symbol].base if symbol in self.ex.markets else symbol.split("/")[0]
            free = self.ex.free_balance(base) or 0.0
            if free < pos.amount * 0.98:
                log.warning("%s: exchange holds %.8g but the ledger says %.8g; adjusting", symbol, free, pos.amount)
                self.journal.log("reconcile", now, symbol=symbol, ledger=pos.amount, exchange=free)
                if free <= 0:
                    del self.portfolio.positions[symbol]
                else:
                    pos.cost *= free / pos.amount
                    pos.amount = free

    def run(self) -> dict:
        self.start()
        consecutive_errors = 0
        try:
            while not self.stopped_reason:
                now = self.clock.now()
                if now >= self.run_end:
                    self.stopped_reason = "end of run"
                    break
                try:
                    self.tick(now)
                    consecutive_errors = 0
                except TRANSIENT_ERRORS as e:  # network hiccups: keep going, however long they last
                    if not self._quiet("transient", now, 600):
                        log.warning("transient exchange error: %s", e)
                        self.journal.log("error", now, transient=True, error=str(e)[:500])
                except Exception as e:  # anything else: keep guarding positions, but not forever
                    consecutive_errors += 1
                    log.exception("error in tick (%d in a row)", consecutive_errors)
                    self.journal.log("error", now, transient=False, error=f"{type(e).__name__}: {e}"[:500])
                    if consecutive_errors >= 30:
                        self.stopped_reason = f"too many consecutive errors: {e}"
                        break
                self.clock.sleep(max(1.0, self.cfg.run.tick_seconds - (self.clock.now() - now)))
        except KeyboardInterrupt:
            self.save_state()
            log.warning("interrupted: state saved, positions left open; run the same command to resume")
            raise
        return self.finish()

    def finish(self, force_close: bool = False) -> dict:
        now = self.clock.now()
        if (self.cfg.run.close_all_at_end or force_close) and self.portfolio.positions:
            self.liquidate(now, "closed_manually" if force_close else "end_of_run")
        equity = self.equity()
        p = self.portfolio
        summary = {
            "stopped_reason": self.stopped_reason,
            "start_capital": p.start_capital,
            "final_equity": equity,
            "return_pct": (equity / p.start_capital - 1) * 100 if p.start_capital else 0.0,
            "closed_trades": len(p.closed),
            "fees_paid": p.fees_paid,
            "llm_cost_usd": p.llm_cost_usd,
            "open_positions": list(p.positions),
        }
        self.journal.log("equity", now, **self._equity_fields())
        self.journal.log("end", now, **summary)
        self.finished = True
        self.save_state()
        return summary

    # ---- one tick ------------------------------------------------------------------------------
    def tick(self, now: float) -> None:
        p = self.portfolio
        decision_due = now >= self.next_decision
        symbols = sorted(set(self.universe) | set(p.positions)) if decision_due else list(p.positions)
        if symbols:
            self.last_tickers.update(self.ex.tickers(symbols))
        prices = self.prices

        equity = self.equity()
        p.roll_day(now, equity)
        p.mark(equity)

        self.risk.update_trailing(p, prices)
        for symbol, reason in self.risk.exits(p, prices):
            self.sell(symbol, 1.0, reason, now)

        equity = self.equity()
        state = self.risk.state(p, equity)
        if state == KILL:
            log.error("max drawdown hit: equity %.2f vs peak %.2f; closing everything", equity, p.peak_equity)
            self.liquidate(now, "kill_switch")
            self.stopped_reason = f"max drawdown of {self.cfg.risk.max_drawdown_pct}% from peak hit"
            return

        if decision_due:
            self.decide(now, state)

        if now >= self.next_equity_log:
            self.journal.log("equity", now, **self._equity_fields())
            self.next_equity_log = now + self.cfg.run.equity_log_minutes * 60
        self.save_state()

    def _equity_fields(self) -> dict:
        p = self.portfolio
        equity = self.equity()
        return {
            "equity": round(equity, 6),
            "cash": round(p.cash, 6),
            "return_pct": round((equity / p.start_capital - 1) * 100, 4) if p.start_capital else 0.0,
            "positions": {s: round(pos.value(self.prices.get(s, pos.entry_price)), 6) for s, pos in p.positions.items()},
        }

    # ---- decisions ---------------------------------------------------------------------------------
    def _context(self, now: float, state: str) -> DecisionContext:
        p = self.portfolio
        tickers = self.last_tickers
        universe = select_universe({s: t for s, t in tickers.items() if s in self.ex.markets},
                                   self.cfg.exchange.min_quote_volume_24h, self.cfg.exchange.max_spread_pct,
                                   always=set(p.positions))
        snapshots = []
        for symbol in universe:
            try:
                snap = build_snapshot(self.ex, tickers[symbol], self.ex.markets[symbol])
            except TRANSIENT_ERRORS as e:
                log.warning("no candles for %s: %s", symbol, e)
                continue
            if snap:
                snapshots.append(snap)

        equity = self.equity()
        prices = self.prices
        positions = []
        for s, pos in p.positions.items():
            price = prices.get(s, pos.entry_price)
            positions.append({
                "symbol": s, "amount": pos.amount, "entry_price": pos.entry_price, "cost": pos.cost,
                "price": price, "value": pos.value(price), "pnl_pct": pos.pnl_pct(price),
                "stop_price": pos.stop_price, "take_profit_price": pos.take_profit_price,
                "held_hours": (now - pos.opened_at) / 3600, "reason": pos.reason,
            })
        fee = max((m.taker_fee for m in self.ex.markets.values()), default=0.0025)
        spreads = [s.spread_pct for s in snapshots] or [0.1]
        r, ex = self.cfg.risk, self.cfg.exchange
        constraints = {
            "max_positions": r.max_positions,
            "max_trades_per_day": r.max_trades_per_day,
            "min_stop_loss_pct": r.min_stop_loss_pct,
            "max_stop_loss_pct": r.max_stop_loss_pct,
            "default_stop_loss_pct": r.default_stop_loss_pct,
            "default_take_profit_pct": r.default_take_profit_pct,
            "max_take_profit_pct": r.max_take_profit_pct,
            "trailing_stop": r.trailing_stop,
            "daily_loss_limit_pct": r.daily_loss_limit_pct,
            "max_drawdown_pct": r.max_drawdown_pct,
            "taker_fee_pct": fee * 100,
            "round_trip_cost_pct": 2 * fee * 100 + statistics.median(spreads) + 2 * ex.paper_slippage_pct,
            "min_check_minutes": self.cfg.run.min_decision_interval_minutes,
            "max_check_minutes": self.cfg.run.max_decision_interval_minutes,
            "close_all_at_end": self.cfg.run.close_all_at_end,
            "min_position_by_symbol": {
                s.symbol: self.risk.min_position(self.ex.markets[s.symbol], s.price) for s in snapshots
            },
        }
        if hasattr(self.brain, "spent_usd") and not self.brain_disabled:
            constraints.update(
                llm_budget_usd=self.cfg.brain.llm_budget_usd,
                llm_spent_usd=p.llm_cost_usd,
                llm_avg_decision_usd=p.llm_cost_usd / self.llm_decisions if self.llm_decisions else 0.0,
            )
        account = {
            "start_capital": p.start_capital,
            "equity": equity,
            "return_pct": (equity / p.start_capital - 1) * 100 if p.start_capital else 0.0,
            "available_cash": self.available_cash(),
            "peak_equity": p.peak_equity,
            "realized_pnl": p.realized_pnl,
            "fees_paid": p.fees_paid,
            "trades_today": p.trades_today,
            "risk_state": state,
        }
        recent = [vars(t) for t in p.closed[-8:]]
        return DecisionContext(
            now=now, run_start=self.run_start, run_end=self.run_end, mode=self.cfg.run.mode,
            exchange_name=self.ex.name, quote=self.cfg.exchange.quote, account=account, positions=positions,
            snapshots=snapshots, constraints=constraints, recent_trades=recent,
            recent_decisions=self.recent_decisions[-4:], previous_notes=self.notes, exchange=self.ex,
        )

    def decide(self, now: float, state: str) -> None:
        ctx = self._context(now, state)
        brain = self.fallback if self.brain_disabled else self.brain
        decision: Decision | None = None
        retry_soon = False
        if brain is None:
            decision = Decision(source="none: brain disabled, positions managed by stops only")
        else:
            try:
                decision = brain.decide(ctx)
                if brain is self.brain and hasattr(brain, "spent_usd"):
                    self.llm_decisions += 1
            except BudgetExceeded as e:
                log.warning("%s", e)
                self.journal.log("brain_disabled", now, reason=str(e))
                self.brain_disabled = True
                if self.fallback is not None:
                    log.warning("switching to the %s brain for the rest of the run", self.fallback.name)
                    decision = self.fallback.decide(ctx)
            except BrainError as e:
                log.error("brain error, holding this cycle: %s", e)
                self.journal.log("error", now, brain=True, error=str(e)[:500])
                retry_soon = True
            except Exception as e:  # a bug in a brain must not stop the agent from guarding positions
                log.exception("unexpected brain failure, holding this cycle")
                self.journal.log("error", now, brain=True, error=f"{type(e).__name__}: {e}"[:500])
                retry_soon = True
            finally:
                if hasattr(self.brain, "spent_usd"):
                    self.portfolio.llm_cost_usd = self.brain.spent_usd

        run = self.cfg.run
        minutes = run.min_decision_interval_minutes if retry_soon else (
            (decision.next_check_minutes if decision else None) or run.decision_interval_minutes)
        minutes = min(max(minutes, run.min_decision_interval_minutes), run.max_decision_interval_minutes)
        self.next_decision = now + minutes * 60
        if decision is None:
            return

        if decision.notes:
            self.notes = decision.notes
        usage = getattr(brain, "last_usage", None) if brain is self.brain else None
        actions = "; ".join(
            f"{i.action} {i.symbol}" + (f" {i.amount_quote:.2f}" if i.action == "buy" else f" x{i.sell_fraction:g}")
            for i in decision.intents) or "hold"
        self.journal.log("decision", now, source=decision.source, market_view=decision.market_view,
                         intents=[vars(i) for i in decision.intents], notes=decision.notes,
                         next_check_minutes=minutes, usage=usage, llm_cost_total_usd=self.portfolio.llm_cost_usd)
        self.recent_decisions.append({"time": fmt_ts(now), "source": decision.source,
                                      "summary": f"{decision.market_view} -> {actions}"[:400]})
        self.recent_decisions = self.recent_decisions[-6:]
        log.info("decision (%s): %s | %s | next in %d min%s", decision.source, decision.market_view[:160], actions,
                 minutes, f" | LLM ${self.portfolio.llm_cost_usd:.2f}" if self.portfolio.llm_cost_usd else "")

        for intent in [i for i in decision.intents if i.action == "sell"]:
            self.sell(intent.symbol, intent.sell_fraction, f"brain: {intent.reason}"[:200], now)
        for intent in [i for i in decision.intents if i.action == "buy"]:
            self.buy(intent, now)

    # ---- execution -----------------------------------------------------------------------------------
    def buy(self, intent: TradeIntent, now: float) -> None:
        p = self.portfolio
        market = self.ex.markets.get(intent.symbol)
        ticker = self.last_tickers.get(intent.symbol)
        if market is None or ticker is None:
            self.journal.log("rejected", now, action="buy", symbol=intent.symbol, reason="unknown market")
            return
        equity = self.equity()
        verdict = self.risk.check_buy(p, market, ticker, intent.amount_quote, equity, self.available_cash(),
                                      now, self.risk.state(p, equity))
        if not verdict.ok:
            log.info("buy %s rejected: %s", intent.symbol, verdict.reason)
            self.journal.log("rejected", now, action="buy", symbol=intent.symbol, reason=verdict.reason)
            return
        stop = self.risk.clamp_stop(intent.stop_loss_pct)
        take_profit = self.risk.clamp_take_profit(intent.take_profit_pct)
        try:
            fill = self.ex.buy(intent.symbol, verdict.cost)
        except (OrderError, ccxt.BaseError) as e:
            log.error("buy %s failed: %s", intent.symbol, e)
            self.journal.log("error", now, action="buy", symbol=intent.symbol, error=str(e)[:500])
            return
        pos = p.apply_buy(fill, now, stop, take_profit, self.cfg.risk.trailing_stop, intent.reason)
        self.journal.log("trade", now, side="buy", symbol=fill.symbol, amount=fill.amount, price=fill.price,
                         quote_amount=fill.quote_amount, fee=fill.fee, stop_price=pos.stop_price,
                         take_profit_price=pos.take_profit_price, reason=intent.reason, order_id=fill.order_id)
        log.info("BUY  %s %.8g @ %.6g for %.2f %s (stop %.6g, tp %s) - %s", fill.symbol, fill.amount, fill.price,
                 fill.quote_amount, self.cfg.exchange.quote, pos.stop_price,
                 f"{pos.take_profit_price:.6g}" if pos.take_profit_price else "-", intent.reason)

    def sell(self, symbol: str, fraction: float, reason: str, now: float) -> None:
        p = self.portfolio
        pos = p.positions.get(symbol)
        market = self.ex.markets.get(symbol)
        price = self.prices.get(symbol)
        if pos is None or market is None or price is None:
            return
        verdict = self.risk.check_sell(pos, market, price, fraction)
        if not verdict.ok:
            if not self._quiet(f"sell:{symbol}", now):
                log.warning("cannot sell %s: %s", symbol, verdict.reason)
                self.journal.log("rejected", now, action="sell", symbol=symbol, reason=verdict.reason)
            return
        amount = verdict.amount
        selling_all = amount >= pos.amount
        if self.ex.is_live:
            free = self.ex.free_balance(market.base) or 0.0
            amount = min(amount, free)
            if amount <= 0:
                if not self._quiet(f"balance:{symbol}", now):
                    log.error("no %s balance on the exchange to sell; was it moved manually?", market.base)
                    self.journal.log("error", now, action="sell", symbol=symbol, error="no balance on exchange")
                return
        try:
            fill = self.ex.sell(symbol, amount)
        except (OrderError, ccxt.BaseError) as e:
            if not self._quiet(f"sellerr:{symbol}", now, 600):
                log.error("sell %s failed: %s", symbol, e)
                self.journal.log("error", now, action="sell", symbol=symbol, error=str(e)[:500])
            return
        trade = p.apply_sell(fill, now, reason)
        leftover = p.positions.get(symbol)
        if selling_all and leftover is not None and leftover.value(price) < market.min_order_value(price):
            del p.positions[symbol]  # precision dust that can never be sold
        if reason == "stop_loss":
            p.cooldowns[symbol] = now + self.cfg.risk.symbol_cooldown_minutes * 60
        self.journal.log("trade", now, side="sell", symbol=symbol, amount=fill.amount, price=fill.price,
                         quote_amount=fill.quote_amount, fee=fill.fee, pnl=trade.pnl, pnl_pct=trade.pnl_pct,
                         reason=reason, order_id=fill.order_id)
        log.info("SELL %s %.8g @ %.6g for %.2f %s, P&L %+.2f (%+.2f%%) - %s", symbol, fill.amount, fill.price,
                 fill.quote_amount, self.cfg.exchange.quote, trade.pnl, trade.pnl_pct, reason)

    def liquidate(self, now: float, reason: str) -> None:
        symbols = list(self.portfolio.positions)
        if not symbols:
            return
        try:
            self.last_tickers.update(self.ex.tickers(symbols))
        except TRANSIENT_ERRORS as e:
            log.warning("could not refresh prices before liquidating: %s", e)
        for symbol in symbols:
            self.sell(symbol, 1.0, reason, now)
