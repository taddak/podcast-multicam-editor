"""Replay historical candles through the real agent loop (same risk manager,
same execution code) with a simulated clock."""

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
from .clock import SimClock
from .config import Config
from .exchange import TF_SECONDS, SimExchange
from .journal import Journal

log = logging.getLogger("trader")

WARMUP_CANDLES = 200


def _csv_path(data_dir: Path, exchange_id: str, symbol: str, timeframe: str) -> Path:
    return data_dir / f"{exchange_id}_{symbol.replace('/', '-')}_{timeframe}.csv"


def fetch_history(exchange_id: str, symbols: list[str], timeframe: str, days: float, data_dir: Path,
                  options: dict | None = None) -> dict[str, list[list[float]]]:
    """Download candles from the exchange's public API and cache them as CSV."""
    params = {"enableRateLimit": True}
    if options:
        params["options"] = options
    ex = getattr(ccxt, exchange_id)(params)
    ex.load_markets()
    tf_ms = TF_SECONDS[timeframe] * 1000
    now_ms = ex.milliseconds()
    since = now_ms - int((days * 86400 + WARMUP_CANDLES * TF_SECONDS[timeframe]) * 1000)
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
        if len(candles) < WARMUP_CANDLES + 24:
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


def run_backtest(cfg: Config, candles: dict[str, list[list[float]]], timeframe: str = "1h",
                 brain: Brain | None = None, min_order: float = 5.0, name: str | None = None,
                 spread_pct: float = 0.10) -> dict:
    cfg = copy.deepcopy(cfg)
    clock = SimClock(0)
    sim = SimExchange(candles, clock, timeframe, cfg.exchange.quote,
                      taker_fee=cfg.exchange.taker_fee if cfg.exchange.taker_fee is not None else 0.0025,
                      min_cost=min_order, spread_pct=spread_pct, slippage_pct=cfg.exchange.paper_slippage_pct)
    start, end = sim.first_tradable_time(WARMUP_CANDLES), sim.last_time()
    if end - start < 24 * 3600:
        raise ValueError("not enough history for a backtest (need warm-up + at least one day)")
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

    # Buy-and-hold of an equal-weight basket over the same window, for comparison.
    rets = []
    for rows in candles.values():
        start_px = next((r[4] for r in rows if (r[0] + sim.tf_ms) / 1000 >= start), None)
        end_px = rows[-1][4]
        if start_px:
            rets.append(end_px / start_px - 1)
    summary["buy_and_hold_pct"] = sum(rets) / len(rets) * 100 if rets else 0.0
    summary["run_dir"] = str(journal.dir)
    summary["days"] = (end - start) / 86400
    return summary
