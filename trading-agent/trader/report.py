"""Summaries of a run directory: works on a finished run and on one still in progress."""

from __future__ import annotations

import csv
from pathlib import Path

from .clock import fmt_ts
from .journal import Journal
from .portfolio import Portfolio


def max_drawdown(values: list[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak * 100)
    return worst


def summarize(run_dir: str | Path) -> dict:
    journal = Journal(run_dir)
    state = journal.load_state()
    if not state:
        raise FileNotFoundError(f"no state.json in {run_dir}")
    p = Portfolio.from_dict(state["portfolio"])
    prices = state.get("last_prices", {})
    curve = [(e["ts"], e["equity"]) for e in journal.events({"equity"})]
    equity = p.equity(prices)
    wins = [t for t in p.closed if t.pnl > 0]
    decisions = journal.events({"decision"})
    errors = journal.events({"error"})
    rejected = journal.events({"rejected"})
    return {
        "run": Path(run_dir).name,
        "exchange": state.get("exchange"),
        "mode": state.get("mode"),
        "finished": state.get("finished"),
        "stopped_reason": state.get("stopped_reason"),
        "start": fmt_ts(state["run_start"]),
        "end": fmt_ts(state["run_end"]),
        "start_capital": p.start_capital,
        "equity": equity,
        "return_pct": (equity / p.start_capital - 1) * 100 if p.start_capital else 0.0,
        "max_drawdown_pct": max_drawdown([v for _, v in curve] + [equity]),
        "closed_trades": len(p.closed),
        "win_rate_pct": len(wins) / len(p.closed) * 100 if p.closed else 0.0,
        "best_trade_pct": max((t.pnl_pct for t in p.closed), default=0.0),
        "worst_trade_pct": min((t.pnl_pct for t in p.closed), default=0.0),
        "realized_pnl": p.realized_pnl,
        "fees_paid": p.fees_paid,
        "llm_cost_usd": p.llm_cost_usd,
        "decisions": len(decisions),
        "rejected_orders": len(rejected),
        "errors": len(errors),
        "open_positions": {s: pos.value(prices.get(s, pos.entry_price)) for s, pos in p.positions.items()},
        "trades": [vars(t) for t in p.closed],
        "curve": curve,
    }


def format_report(s: dict, quote: str = "EUR") -> str:
    lines = [
        f"Run {s['run']} on {s['exchange']} ({s['mode']}), {s['start']} -> {s['end']}"
        + ("" if s["finished"] else "  [still running]"),
        f"  capital      {s['start_capital']:.2f} {quote} -> {s['equity']:.2f} {quote}  ({s['return_pct']:+.2f} %)",
        f"  max drawdown {s['max_drawdown_pct']:.2f} %",
        f"  trades       {s['closed_trades']} closed, win rate {s['win_rate_pct']:.0f} %, "
        f"best {s['best_trade_pct']:+.2f} %, worst {s['worst_trade_pct']:+.2f} %",
        f"  realized P&L {s['realized_pnl']:+.2f} {quote}, fees {s['fees_paid']:.2f} {quote}",
        f"  decisions    {s['decisions']}, rejected orders {s['rejected_orders']}, errors {s['errors']}",
    ]
    if s["llm_cost_usd"]:
        lines.append(f"  Claude API   ${s['llm_cost_usd']:.2f}")
    if s["open_positions"]:
        lines.append("  open         " + ", ".join(f"{k} {v:.2f}" for k, v in s["open_positions"].items()))
    if s.get("stopped_reason"):
        lines.append(f"  stopped      {s['stopped_reason']}")
    if s["trades"]:
        lines.append("  last trades:")
        for t in s["trades"][-10:]:
            lines.append(f"    {fmt_ts(t['closed_at'])}  {t['symbol']:<10} {t['pnl_pct']:+6.2f} %  "
                         f"{t['pnl']:+.2f}  {t['exit_reason'][:60]}")
    return "\n".join(lines)


def write_equity_csv(s: dict, path: str | Path) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "equity"])
        for ts, eq in s["curve"]:
            w.writerow([fmt_ts(ts), f"{eq:.4f}"])
