"""Market snapshots: what the brain sees about each tradable symbol."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .exchange import Exchange, MarketInfo, Ticker
from .indicators import atr, ema, pct_change, rsi


@dataclass
class SymbolSnapshot:
    symbol: str
    price: float
    spread_pct: float
    volume_24h: float
    change_1h: float | None
    change_4h: float | None
    change_24h: float | None
    change_7d: float | None
    rsi_1h: float | None
    ema20_1h: float
    ema50_1h: float
    trend_1h: str            # up | down | flat
    atr_pct_1h: float | None
    high_24h: float
    low_24h: float
    min_order: float         # exchange minimum order value in quote currency at this price

    def to_dict(self) -> dict:
        return asdict(self)


def fmt_price(p: float | None) -> str:
    if p is None:
        return "-"
    return f"{p:.6g}"


def fmt_pct(p: float | None, signed: bool = True) -> str:
    if p is None:
        return "-"
    return f"{p:+.2f}" if signed else f"{p:.2f}"


def build_snapshot(exchange: Exchange, ticker: Ticker, market: MarketInfo, candles: int = 200) -> SymbolSnapshot | None:
    rows = exchange.ohlcv(ticker.symbol, "1h", candles)
    if len(rows) < 60:
        return None
    closes = [float(r[4]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    price = ticker.last

    def ago(n: int) -> float | None:
        return closes[-1 - n] if len(closes) > n else None

    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    if e20 > e50 * 1.002 and price > e20:
        trend = "up"
    elif e20 < e50 * 0.998 and price < e20:
        trend = "down"
    else:
        trend = "flat"
    a = atr(highs, lows, closes, 14)
    day = rows[-24:]
    return SymbolSnapshot(
        symbol=ticker.symbol,
        price=price,
        spread_pct=ticker.spread_pct,
        volume_24h=ticker.quote_volume,
        change_1h=pct_change(ago(1), price),
        change_4h=pct_change(ago(4), price),
        change_24h=pct_change(ago(24), price),
        change_7d=pct_change(ago(168), price),
        rsi_1h=rsi(closes, 14),
        ema20_1h=e20,
        ema50_1h=e50,
        trend_1h=trend,
        atr_pct_1h=(a / price * 100) if a else None,
        high_24h=max(float(r[2]) for r in day),
        low_24h=min(float(r[3]) for r in day),
        min_order=market.min_order_value(price),
    )


def select_universe(tickers: dict[str, Ticker], min_quote_volume: float, max_spread_pct: float,
                    always: set[str] | None = None) -> list[str]:
    """Liquid symbols with a tight spread; symbols in `always` (open positions) are kept regardless."""
    always = always or set()
    chosen = [
        s for s, t in tickers.items()
        if s in always or (t.quote_volume >= min_quote_volume and t.spread_pct <= max_spread_pct)
    ]
    return sorted(chosen, key=lambda s: -tickers[s].quote_volume)


def format_candles(rows: list[list[float]], timeframe: str) -> str:
    """Compact CSV of candles plus an indicator summary, for the get_candles tool."""
    if not rows:
        return "no data"
    closes = [float(r[4]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    lines = [f"timeframe={timeframe} candles={len(rows)} (oldest first)", "time_utc,open,high,low,close,volume"]
    for ts, o, h, l, c, v in rows:
        t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        lines.append(f"{t},{fmt_price(o)},{fmt_price(h)},{fmt_price(l)},{fmt_price(c)},{v:.4g}")
    r = rsi(closes, 14)
    a = atr(highs, lows, closes, 14)
    summary = [f"last_close={fmt_price(closes[-1])}"]
    if r is not None:
        summary.append(f"rsi14={r:.1f}")
    if len(closes) >= 20:
        summary.append(f"ema20={fmt_price(ema(closes, 20)[-1])}")
    if len(closes) >= 50:
        summary.append(f"ema50={fmt_price(ema(closes, 50)[-1])}")
    if a:
        summary.append(f"atr14_pct={a / closes[-1] * 100:.2f}")
    lines.append("summary: " + " ".join(summary))
    return "\n".join(lines)
