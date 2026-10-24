"""Claude as the decision maker.

Each decision cycle is one short, fresh conversation: the current state goes in,
Claude may call `get_candles` (and optionally web search) a few times, and the
final answer is a JSON decision constrained by a schema (structured outputs).
Memory between cycles is carried by the notes Claude leaves for itself, plus the
journal excerpts rendered into every prompt.
"""

from __future__ import annotations

import json
import logging

import anthropic

from ..clock import fmt_ts
from ..config import BrainConfig
from ..market import fmt_pct, fmt_price, format_candles
from .base import BrainError, Decision, DecisionContext, TradeIntent

log = logging.getLogger(__name__)

# USD per million tokens: (input, output, cache read). Cache writes cost 1.25x input.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
WEB_SEARCH_USD = 0.01  # per search

# Models that accept server-side refusal fallbacks in the "default" form.
SERVER_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class BudgetExceeded(BrainError):
    pass


SYSTEM_PROMPT = """\
You are the decision engine of an autonomous trading agent. The agent runs a fixed-length experiment: it starts with a small amount of capital on a spot crypto exchange and tries to end the run with as much capital as possible. A deterministic program around you executes your decisions, checks stop-losses and take-profits every minute, and enforces hard risk limits.

# How a cycle works
- Each cycle you receive the run status, the account, open positions, a market table built from 1-hour candles, recent closed trades, your recent decisions, and the notes you left for yourself last time.
- You may call get_candles to look closer at a symbol (another timeframe or more history) before deciding. Call it when the answer could change your decision, not by routine: every call costs time and money.
- If a web_search tool is available, you may use it sparingly for market-moving news about a coin you are about to trade or already hold.
- End the cycle with your decision as JSON (the schema is enforced). An empty actions list means hold: keep the current positions and cash.

# What the program enforces
Orders that break these rules are rejected, so do not propose them.
- Spot only, long only, no leverage, no short selling. You can only buy with available cash and only sell what you hold.
- One position per symbol (no averaging in), a maximum number of open positions, a maximum number of trades per UTC day, and a cool-down on a symbol after it was stopped out.
- Every buy gets a protective stop: stop_loss_pct is clamped into the allowed range. When trailing stops are on, the stop follows the highest price since entry at the same distance. take_profit_pct sets a profit target; 0 means the default.
- Exchange minimum order sizes: a new position must be at least the "min position" shown for its symbol, so that it stays sellable after a drop. With a small account this usually allows only one or two positions at a time.
- Circuit breakers: the daily loss limit blocks new entries for the rest of the UTC day; the maximum drawdown from peak equity closes everything and ends the run.
- If the run status says so, all open positions are sold at market when the run ends.

# Economics
- Every round trip pays the taker fee twice plus the spread and some slippage; the constraints section gives the estimate. Enter only when the move you expect is clearly larger than that cost. Churning small positions reliably loses money.
- Holding cash is a legitimate position, and not trading is often the best decision.
- Set stops from volatility: the atr1h% column is the typical 1-hour range, and stops tighter than roughly 1.5 to 2 times that get hit by noise. Size the position from your conviction and the stop distance.
- Prefer liquid markets with tight spreads. Mind the remaining time: a position opened shortly before the end has little time to work.
- Be calibrated. Short-term crypto moves are mostly noise; act on clear setups, cut losers, and let the stops do their job.

# Decision fields
- market_view: one or two sentences on the current market regime.
- actions: orders to execute now (the program runs sells before buys). For a buy: symbol, amount_quote (quote currency to spend), stop_loss_pct, take_profit_pct, a short reason, and sell_fraction 0. For a sell: symbol, sell_fraction (1 sells the whole position), a short reason, and 0 for amount_quote, stop_loss_pct and take_profit_pct.
- notes_for_next_cycle: a short memo for your future self: the thesis behind open positions, levels to watch, what would change your mind. Nothing else carries over between cycles.
- next_check_minutes: when to decide again, inside the allowed range. Sooner when something is developing, later when the market is quiet. Stops and take-profits are watched every minute in between, so you do not need frequent checks just to protect a position. The run status shows your API budget: pace yourself so it lasts until the end of the run (when it runs out, a simple rule-based strategy takes over).

Use only the data in the prompt or fetched with tools; never assume a price you have not seen."""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "market_view": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["buy", "sell"]},
                    "symbol": {"type": "string"},
                    "amount_quote": {"type": "number"},
                    "sell_fraction": {"type": "number"},
                    "stop_loss_pct": {"type": "number"},
                    "take_profit_pct": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["action", "symbol", "amount_quote", "sell_fraction",
                             "stop_loss_pct", "take_profit_pct", "reason"],
                "additionalProperties": False,
            },
        },
        "notes_for_next_cycle": {"type": "string"},
        "next_check_minutes": {"type": "integer"},
    },
    "required": ["market_view", "actions", "notes_for_next_cycle", "next_check_minutes"],
    "additionalProperties": False,
}

GET_CANDLES_TOOL = {
    "name": "get_candles",
    "description": (
        "Fetch recent OHLCV candles for one market from the exchange, with an RSI/EMA/ATR summary. "
        "Use it to inspect a symbol more closely before deciding: 15m for entry timing, 4h or 1d for the "
        "bigger trend, 1h for more history than the market table shows."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Market symbol exactly as in the market table, e.g. BTC/EUR."},
            "timeframe": {"type": "string", "enum": ["15m", "1h", "4h", "1d"]},
            "limit": {"type": "integer", "description": "Number of candles to return, 10 to 200."},
        },
        "required": ["symbol", "timeframe", "limit"],
        "additionalProperties": False,
    },
}

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}


def render_context(ctx: DecisionContext) -> str:
    acc, con = ctx.account, ctx.constraints
    total_h = (ctx.run_end - ctx.run_start) / 3600
    elapsed_h = (ctx.now - ctx.run_start) / 3600
    out = [
        "# Run",
        f"time: {fmt_ts(ctx.now)}; elapsed {elapsed_h:.1f} h of {total_h:.1f} h; remaining {total_h - elapsed_h:.1f} h",
        f"mode: {ctx.mode}; exchange: {ctx.exchange_name}; quote currency: {ctx.quote}",
        f"open positions are sold at market when the run ends: {'yes' if con.get('close_all_at_end') else 'no'}",
    ]
    if con.get("llm_budget_usd"):
        out.append(
            f"API budget: spent ${con['llm_spent_usd']:.2f} of ${con['llm_budget_usd']:.2f}"
            + (f"; about ${con['llm_avg_decision_usd']:.3f} per decision so far" if con.get("llm_avg_decision_usd") else "")
        )
    out += [
        "",
        "# Account",
        f"start capital {acc['start_capital']:.2f}; equity {acc['equity']:.2f} ({acc['return_pct']:+.2f}%); "
        f"available cash {acc['available_cash']:.2f}; peak equity {acc['peak_equity']:.2f}",
        f"realized P&L {acc['realized_pnl']:+.2f}; fees paid {acc['fees_paid']:.2f}; "
        f"trades today {acc['trades_today']}/{con['max_trades_per_day']}; risk state: {acc['risk_state']}",
        "",
        "# Open positions",
    ]
    if not ctx.positions:
        out.append("none")
    for p in ctx.positions:
        tp = fmt_price(p["take_profit_price"]) if p.get("take_profit_price") else "none"
        out.append(
            f"- {p['symbol']}: amount {p['amount']:.8g} bought at {fmt_price(p['entry_price'])} for {p['cost']:.2f}; "
            f"now {fmt_price(p['price'])} worth {p['value']:.2f} ({p['pnl_pct']:+.2f}%); stop {fmt_price(p['stop_price'])}; "
            f"take-profit {tp}; held {p['held_hours']:.1f} h; entry reason: {p['reason']}"
        )
    out += [
        "",
        "# Constraints",
        f"- max open positions {con['max_positions']}; max trades per UTC day {con['max_trades_per_day']}",
        f"- stop_loss_pct range {con['min_stop_loss_pct']}-{con['max_stop_loss_pct']} (default {con['default_stop_loss_pct']}); "
        f"trailing stops {'on' if con['trailing_stop'] else 'off'}",
        f"- take_profit_pct default {con['default_take_profit_pct']}, max {con['max_take_profit_pct']}",
        f"- taker fee {con['taker_fee_pct']:.3f}% per side; estimated round-trip cost incl. spread and slippage "
        f"about {con['round_trip_cost_pct']:.2f}%",
        f"- daily loss limit {con['daily_loss_limit_pct']}% (blocks entries); max drawdown from peak "
        f"{con['max_drawdown_pct']}% (closes everything, ends the run)",
        f"- next_check_minutes range {con['min_check_minutes']}-{con['max_check_minutes']}",
        "",
        "# Market (1h candles; changes in %; min position in quote currency)",
        "symbol | price | 1h | 4h | 24h | 7d | rsi1h | trend1h | atr1h% | 24h low-high | spread% | 24h volume | min position",
    ]
    min_pos = con.get("min_position_by_symbol", {})
    for s in ctx.snapshots:
        out.append(
            f"{s.symbol} | {fmt_price(s.price)} | {fmt_pct(s.change_1h)} | {fmt_pct(s.change_4h)} | "
            f"{fmt_pct(s.change_24h)} | {fmt_pct(s.change_7d)} | {fmt_pct(s.rsi_1h, False)} | {s.trend_1h} | "
            f"{fmt_pct(s.atr_pct_1h, False)} | {fmt_price(s.low_24h)}-{fmt_price(s.high_24h)} | "
            f"{s.spread_pct:.3f} | {s.volume_24h:,.0f} | {min_pos.get(s.symbol, s.min_order):.2f}"
        )
    out += ["", "# Recent closed trades"]
    if not ctx.recent_trades:
        out.append("none yet")
    for t in ctx.recent_trades:
        out.append(f"- {fmt_ts(t['closed_at'])} {t['symbol']} {t['pnl_pct']:+.2f}% ({t['pnl']:+.2f}), "
                   f"held {(t['closed_at'] - t['opened_at']) / 3600:.1f} h, exit: {t['exit_reason']}")
    out += ["", "# Your recent decisions"]
    if not ctx.recent_decisions:
        out.append("none yet")
    for d in ctx.recent_decisions:
        out.append(f"- {d['time']} ({d.get('source', '')}): {d.get('summary', '')}")
    out += ["", "# Your notes from the previous cycle", ctx.previous_notes or "none", "", "Decide for this cycle."]
    return "\n".join(out)


class ClaudeBrain:
    name = "claude"

    def __init__(self, cfg: BrainConfig, client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.client = client or anthropic.Anthropic(max_retries=4)
        self.spent_usd = 0.0
        self.last_usage: dict = {}
        self.tools = [GET_CANDLES_TOOL] + ([WEB_SEARCH_TOOL] if cfg.web_search else [])

    # ---- cost accounting -----------------------------------------------------------
    def _cost(self, usage, model: str) -> float:
        inp, out, cache_read = PRICES.get(model, PRICES.get(self.cfg.model, (4.0, 20.0, 0.2)))
        cost = (
            usage.input_tokens * inp
            + (usage.cache_creation_input_tokens or 0) * inp * 1.25
            + (usage.cache_read_input_tokens or 0) * cache_read
            + usage.output_tokens * out
        ) / 1_000_000
        server = getattr(usage, "server_tool_use", None)
        if server is not None:
            cost += (getattr(server, "web_search_requests", 0) or 0) * WEB_SEARCH_USD
        return cost

    def _account(self, response) -> None:
        u = response.usage
        cost = self._cost(u, getattr(response, "model", self.cfg.model))
        self.spent_usd += cost
        lu = self.last_usage
        lu["requests"] = lu.get("requests", 0) + 1
        lu["input_tokens"] = lu.get("input_tokens", 0) + u.input_tokens
        lu["cache_read_tokens"] = lu.get("cache_read_tokens", 0) + (u.cache_read_input_tokens or 0)
        lu["cache_write_tokens"] = lu.get("cache_write_tokens", 0) + (u.cache_creation_input_tokens or 0)
        lu["output_tokens"] = lu.get("output_tokens", 0) + u.output_tokens
        lu["cost_usd"] = round(lu.get("cost_usd", 0.0) + cost, 5)
        lu["model"] = getattr(response, "model", self.cfg.model)

    # ---- API call ---------------------------------------------------------------------
    def _create(self, messages: list):
        if self.spent_usd >= self.cfg.llm_budget_usd:
            raise BudgetExceeded(f"Claude budget of ${self.cfg.llm_budget_usd:.2f} spent (${self.spent_usd:.2f})")
        params = dict(
            model=self.cfg.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=messages,
            output_config={
                "effort": self.cfg.effort,
                "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
            },
            cache_control={"type": "ephemeral"},
        )
        if self.cfg.model in SERVER_FALLBACK_MODELS:
            # If a safety classifier declines, the API retries on the recommended fallback model.
            params.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            return self.client.beta.messages.create(**params)
        except anthropic.AuthenticationError as e:
            raise BrainError(f"Anthropic authentication failed: {e}") from e
        except anthropic.BadRequestError as e:
            raise BrainError(f"Anthropic rejected the request: {e}") from e
        except anthropic.RateLimitError as e:
            raise BrainError(f"Anthropic rate limit: {e}") from e
        except anthropic.APIStatusError as e:
            raise BrainError(f"Anthropic API error {e.status_code}: {e}") from e
        except anthropic.APIConnectionError as e:
            raise BrainError(f"cannot reach the Anthropic API: {e}") from e
        except TypeError as e:  # raised by the SDK when no API key / auth profile can be resolved
            raise BrainError(f"Anthropic client misconfigured (is ANTHROPIC_API_KEY set?): {e}") from e

    # ---- tools ---------------------------------------------------------------------------
    def _run_tool(self, ctx: DecisionContext, name: str, args: dict) -> tuple[str, bool]:
        if name != "get_candles":
            return f"unknown tool {name}", True
        symbol = str(args.get("symbol", ""))
        timeframe = str(args.get("timeframe", "1h"))
        if symbol not in ctx.exchange.markets:
            return f"unknown symbol {symbol}; use a symbol from the market table", True
        if timeframe not in ("15m", "1h", "4h", "1d"):
            return f"unsupported timeframe {timeframe}", True
        limit = min(max(int(args.get("limit") or 100), 10), 200)
        try:
            rows = ctx.exchange.ohlcv(symbol, timeframe, limit)
        except Exception as e:  # exchange/network problems go back to the model, not up the stack
            return f"could not fetch candles: {e}", True
        return format_candles(rows, timeframe), False

    # ---- decision loop -------------------------------------------------------------------
    def decide(self, ctx: DecisionContext) -> Decision:
        self.last_usage = {}
        messages: list = [{"role": "user", "content": render_context(ctx)}]
        for round_no in range(self.cfg.max_tool_rounds + 1):
            response = self._create(messages)
            self._account(response)
            stop = response.stop_reason

            if stop == "refusal":
                details = getattr(response, "stop_details", None)
                raise BrainError(f"Claude declined the request ({getattr(details, 'category', None)})")
            if stop == "max_tokens":
                raise BrainError("Claude hit max_tokens before finishing the decision")
            if stop == "pause_turn":  # server tool (web search) paused; resend to let it continue
                messages.append({"role": "assistant", "content": response.content})
                continue
            if stop == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                last_round = round_no >= self.cfg.max_tool_rounds - 1
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    text, is_error = self._run_tool(ctx, block.name, block.input)
                    if last_round:
                        text += "\n\nTool budget for this cycle is used up: give your final decision now."
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": text, "is_error": is_error})
                messages.append({"role": "user", "content": results})
                continue
            return self._parse(response, ctx)
        raise BrainError("Claude kept calling tools past the per-cycle limit")

    def _parse(self, response, ctx: DecisionContext) -> Decision:
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise BrainError(f"no decision text in the response (stop_reason={response.stop_reason})")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise BrainError(f"decision is not valid JSON: {e}") from e

        tradable = {s.symbol for s in ctx.snapshots}
        held = {p["symbol"] for p in ctx.positions}
        intents = []
        for a in data.get("actions", []):
            action, symbol = a.get("action"), a.get("symbol", "")
            if action == "buy" and symbol in tradable and float(a.get("amount_quote") or 0) > 0:
                intents.append(TradeIntent(
                    "buy", symbol, amount_quote=float(a["amount_quote"]), sell_fraction=0.0,
                    stop_loss_pct=float(a.get("stop_loss_pct") or 0) or None,
                    take_profit_pct=float(a.get("take_profit_pct") or 0) or None,
                    reason=str(a.get("reason", ""))[:300],
                ))
            elif action == "sell" and symbol in held:
                fraction = float(a.get("sell_fraction") or 1.0)
                intents.append(TradeIntent("sell", symbol, sell_fraction=min(max(fraction, 0.0), 1.0) or 1.0,
                                           reason=str(a.get("reason", ""))[:300]))
            else:
                log.warning("ignoring invalid action from Claude: %s", a)
        return Decision(
            intents=intents,
            market_view=str(data.get("market_view", ""))[:500],
            notes=str(data.get("notes_for_next_cycle", ""))[:1500],
            next_check_minutes=int(data.get("next_check_minutes") or 0) or None,
            source=f"claude:{self.last_usage.get('model', self.cfg.model)}",
        )
