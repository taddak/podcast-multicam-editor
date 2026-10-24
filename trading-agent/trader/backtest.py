"""Replay historical candles through the real agent loop (same risk manager,
same execution code) with a simulated clock, and compare the result with what
the market actually did over the same window."""

from __future__ import annotations

import copy
import csv
import logging
import time
from pathlib import Path

import ccxt

from .agent import TradingAgent
from .brains.base import Brain
from .brains.rules import RuleBrain
from .clock import SimClock, fmt_ts
from .config import Config
from .exchange import TF_SECONDS, SimExchange
from .journal import Journal

log = logging.getLogger("trader")

WARMUP_HOURS = 200  # history the agent sees before the window starts (indicators, 7-day change)


def _csv_path(data_dir: Path, exchange_id: str, symbol: str, timeframe: str) -> Path:
    return data_dir / f"{exchange_id}_{symbol.replace('/', '-')}_{timeframe}.csv"


def warmup_candles(timeframe: str) -> int:
    return WARMUP_HOURS * 3600 // TF_SECONDS[timeframe]


def fetch_history(exchange_id: str, symbols: list[str], timeframe: str, hours: float, data_dir: Path,
                  options: dict | None = None) -> dict[str, list[list[float]]]:
    """Download candles for the window plus warm-up from the public API and cache them as CSV."""
    params = {"enableRateLimit": True}
    if options:
        params["options"] = options
    ex = getattr(ccxt, exchange_id)(params)
    ex.load_markets()
    tf_ms = TF_SECONDS[timeframe] * 1000
    now_ms = ex.milliseconds()
    since = now_ms - int((hours + WARMUP_HOURS) * 3600 * 1000) - tf_ms
    data_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for symbol in symbols:
        if symbol not in ex.markets:
            log.warning("%s not on %s, skipped", symbol, exchange_id)
            continue
        rows: dict[int, list[float]] = {}
        cursor = since
        while cursor < now_ms:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            for r in batch:
                rows[int(r[0])] = [float(x) for x in r[:6]]
            nxt = int(batch[-1][0]) + tf_ms
            if nxt <= cursor:
                break
            cursor = nxt
            time.sleep(ex.rateLimit / 1000)
        candles = [rows[k] for k in sorted(rows) if k + tf_ms <= now_ms]  # drop the still-forming candle
        if len(candles) < warmup_candles(timeframe) + 4:
            log.warning("%s: only %d candles, skipped", symbol, len(candles))
            continue
        with _csv_path(data_dir, exchange_id, symbol, timeframe).open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            w.writerows(candles)
        out[symbol] = candles
        log.info("%s: %d candles", symbol, len(candles))
    return out


def load_history(data_dir: Path, exchange_id: str, symbols: list[str], timeframe: str) -> dict[str, list[list[float]]]:
    out = {}
    for symbol in symbols:
        path = _csv_path(data_dir, exchange_id, symbol, timeframe)
        if not path.exists():
            continue
        with path.open() as fh:
            reader = csv.reader(fh)
            next(reader)
            out[symbol] = [[float(x) for x in row] for row in reader]
    return out


def market_reality(candles: dict[str, list[list[float]]], tf_ms: int, start: float, end: float) -> dict[str, dict]:
    """What each market actually did between start and end: change, high/low and the best possible move."""
    out = {}
    for symbol, rows in candles.items():
        window = [r for r in rows if start * 1000 < r[0] + tf_ms <= end * 1000]
        before = [r for r in rows if r[0] + tf_ms <= start * 1000]
        if not window or not before:
            continue
        first = before[-1][4]  # price the agent saw when the window opened
        last = window[-1][4]
        low = min(r[3] for r in window)
        high = max(r[2] for r in window)
        # best single long trade with perfect hindsight: buy at a low, sell at a later high
        best, run_low = 0.0, first
        for r in window:
            best = max(best, (r[2] / run_low - 1) * 100)
            run_low = min(run_low, r[3])
        out[symbol] = {"start": first, "end": last, "change_pct": (last / first - 1) * 100,
                       "low": low, "high": high, "best_trade_pct": best}
    return out


def run_backtest(cfg: Config, candles: dict[str, list[list[float]]], timeframe: str = "1h",
                 brain: Brain | None = None, min_order: float = 5.0, name: str | None = None,
                 spread_pct: float = 0.10, hours: float | None = None) -> dict:
    """Replay `candles`; with `hours`, only the last `hours` are traded (the rest is warm-up)."""
    cfg = copy.deepcopy(cfg)
    clock = SimClock(0)
    sim = SimExchange(candles, clock, timeframe, cfg.exchange.quote,
                      taker_fee=cfg.exchange.taker_fee if cfg.exchange.taker_fee is not None else 0.0025,
                      min_cost=min_order, spread_pct=spread_pct, slippage_pct=cfg.exchange.paper_slippage_pct)
    start, end = sim.first_tradable_time(warmup_candles(timeframe)), sim.last_time()
    if hours:
        start = max(start, end - hours * 3600)
    if end - start < 2 * TF_SECONDS[timeframe]:
        raise ValueError("not enough history for a backtest (need the warm-up plus the window)")
    clock.t = start

    tf_minutes = TF_SECONDS[timeframe] // 60
    cfg.run.mode = "paper"
    cfg.run.name = name or f"backtest-{int(time.time())}"
    cfg.run.tick_seconds = TF_SECONDS[timeframe]
    cfg.run.duration_hours = (end - start) / 3600
    cfg.run.min_decision_interval_minutes = max(cfg.run.min_decision_interval_minutes, tf_minutes)
    cfg.run.decision_interval_minutes = max(cfg.run.decision_interval_minutes, tf_minutes)
    cfg.run.max_decision_interval_minutes = max(cfg.run.max_decision_interval_minutes, tf_minutes)
    cfg.exchange.symbols = [s for s in cfg.exchange.symbols if s in candles] or sorted(candles)

    journal = Journal(cfg.run_dir)
    brain = brain or RuleBrain(cfg.risk.max_positions, cfg.risk.min_stop_loss_pct, cfg.risk.max_stop_loss_pct)
    agent = TradingAgent(cfg, sim, brain, journal, clock)
    summary = agent.run()

    reality = market_reality(candles, sim.tf_ms, start, end)
    changes = [r["change_pct"] for r in reality.values()]
    summary["reality"] = reality
    summary["buy_and_hold_pct"] = sum(changes) / len(changes) if changes else 0.0
    summary["run_dir"] = str(journal.dir)
    summary["window_start"] = start
    summary["window_end"] = end
    summary["days"] = (end - start) / 86400
    return summary


def format_comparison(summary: dict, quote: str = "EUR") -> str:
    """Agent result next to what the market really did over the same window."""
    reality = summary["reality"]
    lines = [
        f"Window {fmt_ts(summary['window_start'])} -> {fmt_ts(summary['window_end'])} "
        f"({(summary['window_end'] - summary['window_start']) / 3600:.0f} h)",
        "",
        "What the market really did:",
        f"  {'symbol':<10} {'start':>12} {'end':>12} {'change':>9} {'range low-high':>27} {'best possible':>14}",
    ]
    for symbol, r in sorted(reality.items(), key=lambda kv: -kv[1]["change_pct"]):
        lines.append(f"  {symbol:<10} {r['start']:>12.6g} {r['end']:>12.6g} {r['change_pct']:>+8.2f}% "
                     f"{r['low']:>13.6g}-{r['high']:<13.6g} {r['best_trade_pct']:>+13.2f}%")
    if reality:
        best = max(reality.items(), key=lambda kv: kv[1]["change_pct"])
        worst = min(reality.items(), key=lambda kv: kv[1]["change_pct"])
        lines += [
            "",
            f"  equal-weight basket (buy & hold) {summary['buy_and_hold_pct']:+.2f} %",
            f"  best coin to hold                {best[0]} {best[1]['change_pct']:+.2f} %",
            f"  worst coin to hold               {worst[0]} {worst[1]['change_pct']:+.2f} %",
        ]
    lines += [
        "",
        f"Agent: {summary['start_capital']:.2f} -> {summary['final_equity']:.2f} {quote} "
        f"({summary['return_pct']:+.2f} %), {summary['closed_trades']} closed trades, "
        f"fees {summary['fees_paid']:.2f} {quote}"
        + (f", Claude ${summary['llm_cost_usd']:.2f}" if summary.get("llm_cost_usd") else ""),
        f"  vs. basket: {summary['return_pct'] - summary['buy_and_hold_pct']:+.2f} percentage points",
    ]
    return "\n".join(lines)
