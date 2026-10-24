"""Exchange access.

* `CcxtExchange`  - real exchange through ccxt; places real orders (live mode).
* `PaperExchange` - real market data from the exchange, simulated fills (paper mode).
* `SimExchange`   - historical / synthetic candles, simulated fills (backtests and tests).

All of them speak in the quote currency (EUR by default) and report every
executed order as a `Fill` with the net base amount and the net quote amount,
so the portfolio ledger never has to know how a given exchange charges fees.
"""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass

import ccxt

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

# Errors worth retrying on the next tick instead of stopping the agent.
TRANSIENT_ERRORS = (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable)

# ccxt methods that move money off the account or add leverage. The agent never needs them; they are
# replaced on the client so that not even a bug (or a key with too many rights) can call them.
BLOCKED_METHODS = (
    "withdraw", "transfer", "create_deposit_address", "set_leverage", "set_margin_mode",
    "borrow_margin", "borrow_cross_margin", "borrow_isolated_margin", "add_margin", "reduce_margin",
)


class OrderError(Exception):
    pass


class BlockedOperation(Exception):
    pass


def _block_dangerous_methods(client) -> None:
    def blocked(name):
        def _raise(*args, **kwargs):
            raise BlockedOperation(f"{name} is disabled in this agent")
        return _raise

    for name in BLOCKED_METHODS:
        camel = name.split("_")[0] + "".join(part.title() for part in name.split("_")[1:])
        for attr in {name, camel}:
            if hasattr(client, attr):
                setattr(client, attr, blocked(attr))


@dataclass
class MarketInfo:
    symbol: str
    base: str
    quote: str
    min_amount: float = 0.0
    min_cost: float = 0.0
    taker_fee: float = 0.0025

    def min_order_value(self, price: float) -> float:
        """Smallest order (in quote currency) the exchange accepts at this price."""
        return max(self.min_cost, self.min_amount * price)


@dataclass
class Ticker:
    symbol: str
    last: float
    bid: float
    ask: float
    quote_volume: float
    change_24h_pct: float | None = None

    @property
    def spread_pct(self) -> float:
        if not self.bid or not self.ask:
            return 0.0
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100.0


@dataclass
class Fill:
    symbol: str
    side: str            # buy | sell
    amount: float        # net base amount received (buy) or sold (sell)
    price: float         # average execution price
    quote_amount: float  # net quote spent (buy) or received (sell), fees included
    fee: float           # fee in quote-currency terms, for reporting
    order_id: str = ""


def _floor(value: float, decimals: int) -> float:
    factor = 10 ** decimals
    return math.floor(value * factor) / factor


def simulate_buy(ticker: Ticker, market: MarketInfo, cost: float, slippage_pct: float) -> Fill:
    price = ticker.ask * (1 + slippage_pct / 100.0)
    fee = cost * market.taker_fee
    amount = (cost - fee) / price
    return Fill(market.symbol, "buy", amount, price, cost, fee, order_id="sim")


def simulate_sell(ticker: Ticker, market: MarketInfo, amount: float, slippage_pct: float) -> Fill:
    price = ticker.bid * (1 - slippage_pct / 100.0)
    gross = amount * price
    fee = gross * market.taker_fee
    return Fill(market.symbol, "sell", amount, price, gross - fee, fee, order_id="sim")


class Exchange:
    """Interface shared by the live, paper and simulated exchanges."""

    name = "exchange"
    quote = "EUR"
    is_live = False

    def __init__(self) -> None:
        self.markets: dict[str, MarketInfo] = {}

    def load_markets(self, symbols: list[str]) -> dict[str, MarketInfo]:
        raise NotImplementedError

    def tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        raise NotImplementedError

    def ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[list[float]]:
        raise NotImplementedError

    def buy(self, symbol: str, cost: float) -> Fill:
        raise NotImplementedError

    def sell(self, symbol: str, amount: float) -> Fill:
        raise NotImplementedError

    def free_balance(self, currency: str) -> float | None:
        """Free balance on the exchange, or None when not applicable (paper / sim)."""
        return None

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return _floor(amount, 8)


class CcxtExchange(Exchange):
    is_live = True

    def __init__(self, exchange_id: str, quote: str = "EUR", credentials: dict | None = None,
                 options: dict | None = None, taker_fee: float | None = None):
        super().__init__()
        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"ccxt does not know exchange {exchange_id!r}")
        params = {"enableRateLimit": True, **(credentials or {})}
        if options:
            params["options"] = options
        self.client = getattr(ccxt, exchange_id)(params)
        _block_dangerous_methods(self.client)
        self.name = exchange_id
        self.quote = quote
        self.taker_fee_override = taker_fee

    # ---- market data -------------------------------------------------------------
    def load_markets(self, symbols: list[str]) -> dict[str, MarketInfo]:
        self.client.load_markets()
        found: dict[str, MarketInfo] = {}
        for symbol in symbols:
            m = self.client.markets.get(symbol)
            if not m or not m.get("spot", True) or m.get("active") is False:
                continue
            limits = m.get("limits") or {}
            found[symbol] = MarketInfo(
                symbol=symbol,
                base=m["base"],
                quote=m["quote"],
                min_amount=float((limits.get("amount") or {}).get("min") or 0.0),
                min_cost=float((limits.get("cost") or {}).get("min") or 0.0),
                taker_fee=float(self.taker_fee_override if self.taker_fee_override is not None
                                else (m.get("taker") or 0.0025)),
            )
        self.markets = found
        return found

    def tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        if not symbols:
            return {}
        if self.client.has.get("fetchTickers"):
            raw = self.client.fetch_tickers(symbols)
        else:
            raw = {s: self.client.fetch_ticker(s) for s in symbols}
        out = {}
        for symbol, t in raw.items():
            if symbol not in symbols:
                continue
            last = t.get("last") or t.get("close")
            if not last:
                continue
            qv = t.get("quoteVolume")
            if qv is None and t.get("baseVolume") is not None:
                qv = t["baseVolume"] * last
            out[symbol] = Ticker(
                symbol=symbol,
                last=float(last),
                bid=float(t.get("bid") or last),
                ask=float(t.get("ask") or last),
                quote_volume=float(qv or 0.0),
                change_24h_pct=t.get("percentage"),
            )
        return out

    def ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[list[float]]:
        return self.client.fetch_ohlcv(symbol, timeframe, limit=limit)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        try:
            return float(self.client.amount_to_precision(symbol, amount))
        except ccxt.InvalidOrder:
            return 0.0

    # ---- live orders ---------------------------------------------------------------
    def free_balance(self, currency: str) -> float | None:
        balance = self.client.fetch_balance()
        return float((balance.get("free") or {}).get(currency) or 0.0)

    def buy(self, symbol: str, cost: float) -> Fill:
        if symbol not in self.markets:  # only the spot markets loaded from the configured list
            raise OrderError(f"{symbol} is not an allowed market")
        c = self.client
        cost = _floor(cost, 2)
        if c.has.get("createMarketBuyOrderWithCost"):
            order = c.create_market_buy_order_with_cost(symbol, cost)
        elif c.has.get("createMarketOrderWithCost"):
            order = c.create_market_order_with_cost(symbol, "buy", cost)
        else:
            ticker = c.fetch_ticker(symbol)
            price = ticker.get("ask") or ticker["last"]
            amount = self.amount_to_precision(symbol, cost / price)
            order = c.create_order(symbol, "market", "buy", amount, price)
        return self._fill_from_order(symbol, "buy", self._settle(symbol, order))

    def sell(self, symbol: str, amount: float) -> Fill:
        if symbol not in self.markets:
            raise OrderError(f"{symbol} is not an allowed market")
        amount = self.amount_to_precision(symbol, amount)
        if amount <= 0:
            raise OrderError(f"sell amount for {symbol} rounds to zero")
        order = self.client.create_order(symbol, "market", "sell", amount)
        return self._fill_from_order(symbol, "sell", self._settle(symbol, order))

    def _settle(self, symbol: str, order: dict, attempts: int = 15) -> dict:
        """Market orders fill almost instantly, but the create response is often incomplete."""
        order_id = order.get("id")
        for _ in range(attempts):
            if order.get("status") in ("closed", "canceled", "expired", "rejected") and order.get("filled") is not None:
                return order
            if not order_id:
                return order
            time.sleep(1.0)
            try:
                order = self.client.fetch_order(order_id, symbol)
            except ccxt.BaseError:
                break
        return order

    def _fill_from_order(self, symbol: str, side: str, order: dict) -> Fill:
        market = self.markets.get(symbol)
        if market is None:
            raise OrderError(f"unknown market {symbol}")
        filled = float(order.get("filled") or 0.0)
        if filled <= 0:
            raise OrderError(f"{side} order {order.get('id')} on {symbol} not filled (status={order.get('status')})")
        cost = order.get("cost")
        avg = order.get("average") or (cost / filled if cost else None) or order.get("price")
        if not avg:
            raise OrderError(f"{side} order {order.get('id')} on {symbol} has no price")
        avg = float(avg)
        cost = float(cost) if cost else filled * avg

        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        fees = [f for f in fees if f and f.get("cost") is not None]
        fee_quote = sum(float(f["cost"]) for f in fees if f.get("currency") == market.quote)
        fee_base = sum(float(f["cost"]) for f in fees if f.get("currency") == market.base)
        if not fees:  # exchange did not report fees: assume the taker fee, in quote
            fee_quote = cost * market.taker_fee

        if side == "buy":
            return Fill(symbol, side, filled - fee_base, avg, cost + fee_quote,
                        fee_quote + fee_base * avg, str(order.get("id", "")))
        return Fill(symbol, side, filled, avg, cost - fee_quote - fee_base * avg,
                    fee_quote + fee_base * avg, str(order.get("id", "")))


class PaperExchange(CcxtExchange):
    """Live public market data, simulated fills. Needs no API keys."""

    is_live = False

    def __init__(self, exchange_id: str, quote: str = "EUR", options: dict | None = None,
                 taker_fee: float | None = None, slippage_pct: float = 0.05):
        super().__init__(exchange_id, quote, credentials=None, options=options, taker_fee=taker_fee)
        self.name = f"{exchange_id} (paper)"
        self.slippage_pct = slippage_pct

    def free_balance(self, currency: str) -> float | None:
        return None

    def buy(self, symbol: str, cost: float) -> Fill:
        ticker = self.tickers([symbol])[symbol]
        return simulate_buy(ticker, self.markets[symbol], cost, self.slippage_pct)

    def sell(self, symbol: str, amount: float) -> Fill:
        ticker = self.tickers([symbol])[symbol]
        return simulate_sell(ticker, self.markets[symbol], amount, self.slippage_pct)


class SimExchange(Exchange):
    """Replays candles ([ts_ms, open, high, low, close, volume]) against a SimClock."""

    is_live = False

    def __init__(self, candles: dict[str, list[list[float]]], clock, timeframe: str = "1h",
                 quote: str = "EUR", taker_fee: float = 0.0025, min_cost: float = 5.0,
                 spread_pct: float = 0.10, slippage_pct: float = 0.05):
        super().__init__()
        self.name = "simulation"
        self.quote = quote
        self.clock = clock
        self.timeframe = timeframe
        self.tf_ms = TF_SECONDS[timeframe] * 1000
        self.candles = {s: sorted(c, key=lambda row: row[0]) for s, c in candles.items()}
        self._ts = {s: [row[0] for row in c] for s, c in self.candles.items()}
        self.taker_fee = taker_fee
        self.min_cost = min_cost
        self.spread_pct = spread_pct
        self.slippage_pct = slippage_pct

    def load_markets(self, symbols: list[str]) -> dict[str, MarketInfo]:
        self.markets = {
            s: MarketInfo(s, s.split("/")[0], s.split("/")[1], 0.0, self.min_cost, self.taker_fee)
            for s in symbols if s in self.candles
        }
        return self.markets

    def _closed(self, symbol: str) -> list[list[float]]:
        """Candles that have fully closed at the current simulated time."""
        cutoff = self.clock.now() * 1000 - self.tf_ms
        idx = bisect.bisect_right(self._ts[symbol], cutoff)
        return self.candles[symbol][:idx]

    def first_tradable_time(self, warmup_candles: int) -> float:
        """Earliest time (s) at which every symbol has `warmup_candles` closed candles."""
        latest = max(self._ts[s][min(max(warmup_candles - 1, 0), len(self._ts[s]) - 1)] for s in self.candles)
        return (latest + self.tf_ms) / 1000.0

    def last_time(self) -> float:
        return (min(ts[-1] for ts in self._ts.values()) + self.tf_ms) / 1000.0

    def tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        out = {}
        per_day = max(1, 86_400_000 // self.tf_ms)
        for symbol in symbols:
            rows = self._closed(symbol) if symbol in self.candles else []
            if not rows:
                continue
            close = rows[-1][4]
            day = rows[-per_day:]
            half = self.spread_pct / 200.0
            out[symbol] = Ticker(
                symbol=symbol,
                last=close,
                bid=close * (1 - half),
                ask=close * (1 + half),
                quote_volume=sum(r[4] * r[5] for r in day),
                change_24h_pct=(close / day[0][1] - 1) * 100 if day[0][1] else None,
            )
        return out

    def ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[list[float]]:
        rows = self._closed(symbol)
        target_ms = TF_SECONDS.get(timeframe, 0) * 1000
        if target_ms <= self.tf_ms:
            return [list(r) for r in rows[-limit:]]
        groups: dict[int, list[float]] = {}
        for ts, o, h, l, c, v in rows:
            key = int(ts // target_ms * target_ms)
            g = groups.get(key)
            if g is None:
                groups[key] = [key, o, h, l, c, v]
            else:
                g[2] = max(g[2], h)
                g[3] = min(g[3], l)
                g[4] = c
                g[5] += v
        return list(groups.values())[-limit:]

    def buy(self, symbol: str, cost: float) -> Fill:
        ticker = self.tickers([symbol])[symbol]
        return simulate_buy(ticker, self.markets[symbol], cost, self.slippage_pct)

    def sell(self, symbol: str, amount: float) -> Fill:
        ticker = self.tickers([symbol])[symbol]
        return simulate_sell(ticker, self.markets[symbol], amount, self.slippage_pct)


def build_exchange(cfg, credentials: dict | None = None) -> Exchange:
    """Exchange for `run`: live orders only when run.mode == 'live'."""
    ex = cfg.exchange
    if cfg.run.mode == "live":
        if not credentials:
            raise ValueError("Live mode needs EXCHANGE_API_KEY and EXCHANGE_API_SECRET in the environment")
        return CcxtExchange(ex.id, ex.quote, credentials, ex.options, ex.taker_fee)
    return PaperExchange(ex.id, ex.quote, ex.options, ex.taker_fee, ex.paper_slippage_pct)
