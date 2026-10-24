import pytest

from trader.config import Config, RiskConfig, load_config
from trader.exchange import CcxtExchange, Fill, MarketInfo, Ticker
from trader.indicators import atr, ema, rsi
from trader.portfolio import Portfolio
from trader.risk import HALT, KILL, OK, RiskManager

NOW = 1_760_000_000.0


def market(symbol="AAA/EUR", min_cost=5.0, min_amount=0.0, fee=0.0025):
    return MarketInfo(symbol, symbol.split("/")[0], "EUR", min_amount, min_cost, fee)


def ticker(symbol="AAA/EUR", price=100.0, spread=0.1):
    half = price * spread / 200
    return Ticker(symbol, price, price - half, price + half, 1e6)


# ---- indicators ---------------------------------------------------------------------------------
def test_indicators_basic():
    assert ema([5.0] * 30, 10)[-1] == pytest.approx(5.0)
    assert rsi([float(i) for i in range(1, 40)]) == 100.0
    assert rsi([float(40 - i) for i in range(40)]) == pytest.approx(0.0)
    assert rsi([1.0, 2.0]) is None
    highs = [10.0 + (i % 2) for i in range(30)]
    lows = [9.0 + (i % 2) for i in range(30)]
    closes = [9.5 + (i % 2) for i in range(30)]
    assert atr(highs, lows, closes) > 0


# ---- portfolio -----------------------------------------------------------------------------------
def test_portfolio_round_trip_accounting():
    p = Portfolio.new(10.0)
    p.apply_buy(Fill("AAA/EUR", "buy", 0.099, 100.0, 10.0, 0.025), NOW, 3.0, 6.0, True, "test")
    assert p.cash == pytest.approx(0.0)
    pos = p.positions["AAA/EUR"]
    assert pos.stop_price == pytest.approx(97.0)
    assert pos.take_profit_price == pytest.approx(106.0)
    assert p.equity({"AAA/EUR": 110.0}) == pytest.approx(0.099 * 110)

    trade = p.apply_sell(Fill("AAA/EUR", "sell", 0.099, 110.0, 10.86, 0.027), NOW + 3600, "tp")
    assert "AAA/EUR" not in p.positions
    assert trade.pnl == pytest.approx(0.86)
    assert p.cash == pytest.approx(10.86)
    assert p.fees_paid == pytest.approx(0.052)
    assert p.trades_today == 2


def test_portfolio_partial_sell_and_serialization():
    p = Portfolio.new(20.0)
    p.apply_buy(Fill("AAA/EUR", "buy", 0.2, 100.0, 20.0, 0.05), NOW, 3.0, None, True, "")
    p.apply_sell(Fill("AAA/EUR", "sell", 0.1, 100.0, 9.97, 0.03), NOW, "half")
    pos = p.positions["AAA/EUR"]
    assert pos.amount == pytest.approx(0.1)
    assert pos.cost == pytest.approx(10.0)
    restored = Portfolio.from_dict(p.to_dict())
    assert restored.positions["AAA/EUR"].amount == pytest.approx(0.1)
    assert restored.closed[0].exit_reason == "half"


# ---- risk ------------------------------------------------------------------------------------------
def test_buy_checks():
    r = RiskManager(RiskConfig(max_positions=1, min_order_buffer=1.3), max_spread_pct=0.35)
    p = Portfolio.new(10.0)
    m, t = market(), ticker()
    assert not r.check_buy(p, m, t, 5.0, 10.0, 10.0, NOW, OK).ok          # below 5 * 1.3
    v = r.check_buy(p, m, t, 50.0, 10.0, 10.0, NOW, OK)                     # capped at available cash
    assert v.ok and v.cost == pytest.approx(10.0)
    assert not r.check_buy(p, m, t, 8.0, 10.0, 10.0, NOW, HALT).ok
    assert not r.check_buy(p, m, ticker(spread=1.0), 8.0, 10.0, 10.0, NOW, OK).ok
    p.cooldowns["AAA/EUR"] = NOW + 60
    assert not r.check_buy(p, m, t, 8.0, 10.0, 10.0, NOW, OK).ok
    p.cooldowns.clear()
    p.apply_buy(Fill("BBB/EUR", "buy", 1, 8, 8, 0.02), NOW, 3, None, True, "")
    assert "max 1 open" in r.check_buy(p, m, t, 8.0, 10.0, 10.0, NOW, OK).reason


def test_account_state_and_exits():
    r = RiskManager(RiskConfig(daily_loss_limit_pct=8, max_drawdown_pct=25, max_trades_per_day=3), 0.35)
    p = Portfolio.new(10.0)
    p.peak_equity, p.day_start_equity = 12.0, 10.0
    assert r.state(p, 9.5) == OK
    assert r.state(p, 9.1) == HALT
    assert r.state(p, 8.9) == KILL
    p.trades_today = 3
    assert r.state(p, 10.0) == HALT

    p = Portfolio.new(10.0)
    p.apply_buy(Fill("AAA/EUR", "buy", 0.1, 100.0, 10.0, 0.0), NOW, 3.0, 6.0, True, "")
    assert r.exits(p, {"AAA/EUR": 99.0}) == []
    assert r.exits(p, {"AAA/EUR": 96.9}) == [("AAA/EUR", "stop_loss")]
    assert r.exits(p, {"AAA/EUR": 106.5}) == [("AAA/EUR", "take_profit")]
    r.update_trailing(p, {"AAA/EUR": 105.0})
    assert p.positions["AAA/EUR"].stop_price == pytest.approx(105.0 * 0.97)
    assert r.exits(p, {"AAA/EUR": 101.0}) == [("AAA/EUR", "trailing_stop")]


def test_sell_checks_avoid_dust():
    r = RiskManager(RiskConfig(min_order_buffer=1.3), 0.35)
    p = Portfolio.new(10.0)
    pos = p.apply_buy(Fill("AAA/EUR", "buy", 0.1, 100.0, 10.0, 0.0), NOW, 3.0, None, True, "")
    m = market()
    assert r.check_sell(pos, m, 100.0, 1.0).amount == pytest.approx(0.1)
    assert r.check_sell(pos, m, 100.0, 0.5).amount == pytest.approx(0.1)   # half would leave dust
    assert not r.check_sell(pos, m, 40.0, 1.0).ok                          # 4 EUR < 5 EUR minimum


def test_stop_clamps():
    r = RiskManager(RiskConfig(min_stop_loss_pct=1, max_stop_loss_pct=8, default_stop_loss_pct=3), 0.35)
    assert r.clamp_stop(None) == 3
    assert r.clamp_stop(0.2) == 1
    assert r.clamp_stop(20) == 8
    assert r.clamp_take_profit(0) == 6.0


# ---- live order parsing (no network) -------------------------------------------------------------------
@pytest.fixture
def live_exchange():
    ex = CcxtExchange("bitvavo", "EUR", credentials={"apiKey": "x", "secret": "y"})
    ex.markets = {"BTC/EUR": market("BTC/EUR")}
    return ex


def test_fill_from_order_fee_in_quote(live_exchange):
    order = {"id": "1", "status": "closed", "filled": 0.0001, "average": 90000.0, "cost": 9.0,
             "fee": {"currency": "EUR", "cost": 0.0225}}
    f = live_exchange._fill_from_order("BTC/EUR", "buy", order)
    assert f.amount == pytest.approx(0.0001)
    assert f.quote_amount == pytest.approx(9.0225)
    s = live_exchange._fill_from_order("BTC/EUR", "sell", order)
    assert s.quote_amount == pytest.approx(8.9775)


def test_fill_from_order_fee_in_base_and_missing(live_exchange):
    order = {"id": "2", "status": "closed", "filled": 0.0001, "average": 90000.0, "cost": 9.0,
             "fees": [{"currency": "BTC", "cost": 0.0000001}]}
    f = live_exchange._fill_from_order("BTC/EUR", "buy", order)
    assert f.amount == pytest.approx(0.0000999)
    assert f.fee == pytest.approx(0.009)
    order = {"id": "3", "status": "closed", "filled": 0.0001, "average": None, "cost": 9.0, "fee": None}
    f = live_exchange._fill_from_order("BTC/EUR", "buy", order)
    assert f.price == pytest.approx(90000.0)
    assert f.fee == pytest.approx(9.0 * 0.0025)


def test_unfilled_order_raises(live_exchange):
    from trader.exchange import OrderError
    with pytest.raises(OrderError):
        live_exchange._fill_from_order("BTC/EUR", "buy", {"id": "4", "status": "open", "filled": 0})


# ---- config -------------------------------------------------------------------------------------------
def test_example_config_loads():
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
    assert cfg.run.mode == "paper"
    assert cfg.exchange.id == "bitvavo"


def test_unknown_config_key(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[risk]\nmax_postions = 3\n")
    with pytest.raises(ValueError, match="max_postions"):
        load_config(path)
    assert isinstance(Config().risk, RiskConfig)
