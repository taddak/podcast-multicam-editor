"""Live-mode order flow against a fake ccxt client (no network, no real money)."""

import pytest

import trader.exchange as exchange_mod
from trader.agent import TradingAgent
from trader.brains.base import Decision, TradeIntent
from trader.clock import SimClock
from trader.exchange import CcxtExchange
from trader.journal import Journal


class FakeCcxt:
    """Mimics the parts of a ccxt exchange the agent uses, Bitvavo-style (fees in EUR)."""

    has = {"fetchTickers": True, "createMarketBuyOrderWithCost": None, "createMarketOrderWithCost": True}

    def __init__(self, prices, eur=10.0, fee=0.0025):
        self.prices = dict(prices)
        self.fee = fee
        self.balance = {"EUR": eur}
        self.orders = {}
        self.markets = {
            s: {"symbol": s, "base": s.split("/")[0], "quote": "EUR", "spot": True, "active": True,
                "taker": fee, "limits": {"amount": {"min": 0.0}, "cost": {"min": 5.0}}}
            for s in prices
        }

    def load_markets(self):
        return self.markets

    def fetch_tickers(self, symbols):
        return {s: {"last": p, "bid": p * 0.9995, "ask": p * 1.0005, "quoteVolume": 5e6, "percentage": 1.0}
                for s, p in self.prices.items() if s in symbols}

    def fetch_ohlcv(self, symbol, timeframe, limit=200):
        p = self.prices[symbol]
        return [[i * 3_600_000, p, p * 1.01, p * 0.99, p * (1 + 0.0005 * i), 1000.0] for i in range(limit)]

    def amount_to_precision(self, symbol, amount):
        return f"{int(amount * 1e8) / 1e8:.8f}"

    def fetch_balance(self):
        return {"free": dict(self.balance)}

    def create_market_order_with_cost(self, symbol, side, cost):
        assert side == "buy"
        price = self.prices[symbol] * 1.0005
        fee = cost * self.fee
        amount = (cost - fee) / price
        self.balance["EUR"] -= cost
        base = symbol.split("/")[0]
        self.balance[base] = self.balance.get(base, 0.0) + amount
        oid = f"o{len(self.orders) + 1}"
        self.orders[oid] = {"id": oid, "status": "closed", "filled": amount, "average": price,
                            "cost": cost - fee, "fee": {"currency": "EUR", "cost": fee}}
        return {"id": oid, "status": "open", "filled": None}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        assert (type_, side) == ("market", "sell")
        base = symbol.split("/")[0]
        assert amount <= self.balance.get(base, 0.0) + 1e-12, "selling more than the free balance"
        px = self.prices[symbol] * 0.9995
        gross = amount * px
        fee = gross * self.fee
        self.balance[base] -= amount
        self.balance["EUR"] += gross - fee
        oid = f"o{len(self.orders) + 1}"
        self.orders[oid] = {"id": oid, "status": "closed", "filled": amount, "average": px,
                            "cost": gross, "fee": {"currency": "EUR", "cost": fee}}
        return {"id": oid, "status": "open"}

    def fetch_order(self, oid, symbol):
        return self.orders[oid]


class Scripted:
    name = "scripted"

    def __init__(self, decisions):
        self.decisions = list(decisions)

    def decide(self, ctx):
        return self.decisions.pop(0) if self.decisions else Decision(source="scripted")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(exchange_mod.time, "sleep", lambda s: None)


def live_agent(cfg, fake, decisions):
    cfg.run.mode = "live"
    cfg.exchange.symbols = list(fake.prices)
    ex = CcxtExchange("bitvavo", "EUR", credentials={"apiKey": "k", "secret": "s"})
    ex.client = fake
    clock = SimClock(1_760_000_000)
    return TradingAgent(cfg, ex, Scripted(decisions), Journal(cfg.run_dir), clock), clock


def test_live_buy_then_sell_matches_exchange_balances(cfg):
    fake = FakeCcxt({"BTC/EUR": 90_000.0, "ETH/EUR": 3_500.0})
    buy = Decision(intents=[TradeIntent("buy", "BTC/EUR", amount_quote=100.0, stop_loss_pct=3)],
                   next_check_minutes=30, source="s")
    sell = Decision(intents=[TradeIntent("sell", "BTC/EUR", sell_fraction=1.0, reason="done")], source="s")
    agent, clock = live_agent(cfg, fake, [buy, sell])
    agent.start()
    agent.tick(clock.now())

    p = agent.portfolio
    pos = p.positions["BTC/EUR"]
    assert pos.cost <= 10.0
    assert fake.balance["BTC"] == pytest.approx(pos.amount)
    assert fake.balance["EUR"] >= 0
    assert p.cash == pytest.approx(fake.balance["EUR"], abs=0.02)

    clock.sleep(1800)
    agent.tick(clock.now())
    assert not p.positions
    assert fake.balance["BTC"] == pytest.approx(0.0, abs=1e-8)
    assert p.cash == pytest.approx(fake.balance["EUR"], abs=0.02)
    assert len(p.closed) == 1 and p.closed[0].pnl < 0  # only fees and spread were paid


def test_live_capital_is_capped_by_free_balance(cfg):
    fake = FakeCcxt({"BTC/EUR": 90_000.0}, eur=7.0)
    agent, clock = live_agent(cfg, fake, [])
    agent.start()
    assert agent.portfolio.start_capital == pytest.approx(7.0)


def test_live_agent_never_spends_more_than_its_capital(cfg):
    fake = FakeCcxt({"BTC/EUR": 90_000.0, "ETH/EUR": 3_500.0}, eur=500.0)
    buys = Decision(intents=[TradeIntent("buy", "BTC/EUR", amount_quote=300.0),
                             TradeIntent("buy", "ETH/EUR", amount_quote=300.0)], source="s")
    agent, clock = live_agent(cfg, fake, [buys])
    agent.start()
    agent.tick(clock.now())
    spent = 500.0 - fake.balance["EUR"]
    assert spent <= cfg.run.capital + 1e-9
    assert len(agent.portfolio.positions) == 1  # 10 EUR only fits one position above the minimum


def test_resume_reconciles_with_exchange(cfg):
    fake = FakeCcxt({"BTC/EUR": 90_000.0})
    buy = Decision(intents=[TradeIntent("buy", "BTC/EUR", amount_quote=10.0)], source="s")
    agent, clock = live_agent(cfg, fake, [buy])
    agent.start()
    agent.tick(clock.now())
    fake.balance["BTC"] /= 2  # someone sold half manually
    agent2, _ = live_agent(cfg, fake, [])
    agent2.start()
    assert agent2.portfolio.positions["BTC/EUR"].amount == pytest.approx(fake.balance["BTC"])
