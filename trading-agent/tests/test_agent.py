import json
from types import SimpleNamespace

import pytest

from trader.agent import TradingAgent
from trader.backtest import run_backtest
from trader.brains.base import Decision, TradeIntent
from trader.brains.claude import FALLBACK_BETA, BudgetExceeded, ClaudeBrain, render_context
from trader.brains.rules import RuleBrain
from trader.clock import SimClock
from trader.exchange import SimExchange
from trader.journal import Journal
from trader.report import format_report, summarize


def sim_setup(cfg, candles, hours=72.0):
    clock = SimClock(0)
    sim = SimExchange(candles, clock, "1h", min_cost=5.0)
    clock.t = sim.first_tradable_time(200)
    cfg.run.tick_seconds = 3600
    cfg.run.duration_hours = hours
    cfg.run.min_decision_interval_minutes = 60
    cfg.run.decision_interval_minutes = 60
    return clock, sim


class ScriptedBrain:
    """Returns pre-programmed decisions, one per call."""
    name = "scripted"

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, ctx):
        self.contexts.append(ctx)
        return self.decisions.pop(0) if self.decisions else Decision(source="scripted")


def test_agent_runs_rule_brain_end_to_end(cfg, candles):
    clock, sim = sim_setup(cfg, candles, hours=300)
    journal = Journal(cfg.run_dir)
    agent = TradingAgent(cfg, sim, RuleBrain(cfg.risk.max_positions), journal, clock)
    summary = agent.run()

    p = agent.portfolio
    assert summary["stopped_reason"] in ("end of run",) or "drawdown" in summary["stopped_reason"]
    assert not p.positions, "positions are closed at the end of the run"
    assert p.cash > 0
    assert summary["final_equity"] == pytest.approx(p.cash)
    assert len(p.closed) > 0, "the uptrending symbol should have produced trades"
    kinds = {e["kind"] for e in journal.events()}
    assert {"start", "decision", "trade", "equity", "end"} <= kinds
    # every closed trade is consistent with the cash ledger
    assert p.cash == pytest.approx(p.start_capital + sum(t.pnl for t in p.closed), abs=1e-6)
    report = format_report(summarize(cfg.run_dir))
    assert "capital" in report


def test_agent_enforces_risk_on_brain_orders(cfg, candles):
    clock, sim = sim_setup(cfg, candles, hours=5)
    bad = Decision(intents=[
        TradeIntent("buy", "AAA/EUR", amount_quote=3.0, stop_loss_pct=50),      # below min position
        TradeIntent("buy", "BBB/EUR", amount_quote=100.0, stop_loss_pct=50),    # capped to cash, stop clamped
        TradeIntent("buy", "CCC/EUR", amount_quote=5.0),                        # no cash left
    ], source="scripted", next_check_minutes=60)
    brain = ScriptedBrain([bad])
    journal = Journal(cfg.run_dir)
    agent = TradingAgent(cfg, sim, brain, journal, clock)
    agent.start()
    agent.tick(clock.now())

    p = agent.portfolio
    assert list(p.positions) == ["BBB/EUR"]
    pos = p.positions["BBB/EUR"]
    assert pos.cost == pytest.approx(10.0)
    assert pos.stop_loss_pct == cfg.risk.max_stop_loss_pct
    assert p.cash == pytest.approx(0.0)
    rejected = journal.events({"rejected"})
    assert {e["symbol"] for e in rejected} == {"AAA/EUR", "CCC/EUR"}


def test_agent_resumes_from_state(cfg, candles):
    clock, sim = sim_setup(cfg, candles, hours=48)
    buy = Decision(intents=[TradeIntent("buy", "AAA/EUR", amount_quote=10.0, stop_loss_pct=8)],
                   notes="watch 110", next_check_minutes=60, source="scripted")
    agent = TradingAgent(cfg, sim, ScriptedBrain([buy]), Journal(cfg.run_dir), clock)
    agent.start()
    agent.tick(clock.now())
    assert "AAA/EUR" in agent.portfolio.positions

    clock.sleep(3600)
    brain2 = ScriptedBrain([])
    agent2 = TradingAgent(cfg, sim, brain2, Journal(cfg.run_dir), clock)
    agent2.start()
    assert "AAA/EUR" in agent2.portfolio.positions
    assert agent2.run_end == agent.run_end
    agent2.tick(clock.now())
    assert brain2.contexts[0].previous_notes == "watch 110"
    assert brain2.contexts[0].positions[0]["symbol"] == "AAA/EUR"


def test_kill_switch_liquidates(cfg, candles):
    clock, sim = sim_setup(cfg, candles, hours=48)
    cfg.risk.max_drawdown_pct = 0.5  # trip almost immediately
    cfg.risk.trailing_stop = False
    buy = Decision(intents=[TradeIntent("buy", "BBB/EUR", amount_quote=10.0, stop_loss_pct=8)], source="s")
    agent = TradingAgent(cfg, sim, ScriptedBrain([buy]), Journal(cfg.run_dir), clock)
    summary = agent.run()
    assert "drawdown" in summary["stopped_reason"]
    assert not agent.portfolio.positions


def test_close_all_sells_and_finishes(cfg, candles):
    clock, sim = sim_setup(cfg, candles, hours=48)
    cfg.run.close_all_at_end = False
    buy = Decision(intents=[TradeIntent("buy", "AAA/EUR", amount_quote=10.0, stop_loss_pct=8)], source="s")
    agent = TradingAgent(cfg, sim, ScriptedBrain([buy]), Journal(cfg.run_dir), clock)
    agent.start()
    agent.tick(clock.now())
    assert agent.portfolio.positions

    panic = TradingAgent(cfg, sim, ScriptedBrain([]), Journal(cfg.run_dir), clock)
    panic.start()
    panic.stopped_reason = "closed manually"
    summary = panic.finish(force_close=True)
    assert not panic.portfolio.positions
    assert panic.portfolio.closed[-1].exit_reason == "closed_manually"
    assert summary["stopped_reason"] == "closed manually"
    with pytest.raises(RuntimeError, match="already finished"):
        TradingAgent(cfg, sim, ScriptedBrain([]), Journal(cfg.run_dir), clock).start()


def test_backtest_summary(cfg, candles):
    summary = run_backtest(cfg, candles, "1h", name="bt")
    assert summary["days"] > 10
    assert "buy_and_hold_pct" in summary
    assert summary["final_equity"] > 0


# ---- Claude brain with a fake API client ----------------------------------------------------------------
def usage(inp=3000, out=800):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, server_tool_use=None)


def response(stop_reason, content, model="claude-opus-5-5"):
    return SimpleNamespace(stop_reason=stop_reason, content=content, usage=usage(), model=model,
                           stop_details=None)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def claude_context(cfg, candles):
    clock, sim = sim_setup(cfg, candles)
    agent = TradingAgent(cfg, sim, RuleBrain(2), Journal(cfg.run_dir), clock)
    agent.start()
    agent.last_tickers.update(sim.tickers(agent.universe))
    return agent._context(clock.now(), "ok")


def test_claude_brain_tool_loop_and_parse(cfg, candles):
    ctx = claude_context(cfg, candles)
    decision_json = json.dumps({
        "market_view": "AAA trending up",
        "actions": [
            {"action": "buy", "symbol": "AAA/EUR", "amount_quote": 9.5, "sell_fraction": 0,
             "stop_loss_pct": 2.5, "take_profit_pct": 7, "reason": "breakout"},
            {"action": "buy", "symbol": "NOPE/EUR", "amount_quote": 5, "sell_fraction": 0,
             "stop_loss_pct": 2, "take_profit_pct": 0, "reason": "invalid symbol"},
            {"action": "sell", "symbol": "BBB/EUR", "amount_quote": 0, "sell_fraction": 1,
             "stop_loss_pct": 0, "take_profit_pct": 0, "reason": "not held"},
        ],
        "notes_for_next_cycle": "trail AAA",
        "next_check_minutes": 90,
    })
    tool_call = SimpleNamespace(type="tool_use", id="tu_1", name="get_candles",
                                input={"symbol": "AAA/EUR", "timeframe": "4h", "limit": 50})
    client = FakeClient([
        response("tool_use", [SimpleNamespace(type="thinking", thinking=""), tool_call]),
        response("end_turn", [SimpleNamespace(type="text", text=decision_json)]),
    ])
    brain = ClaudeBrain(cfg.brain, client=client)
    decision = brain.decide(ctx)

    assert [i.symbol for i in decision.intents] == ["AAA/EUR"]
    assert decision.intents[0].stop_loss_pct == 2.5
    assert decision.notes == "trail AAA"
    assert decision.next_check_minutes == 90
    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default" and first["betas"] == [FALLBACK_BETA]
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in first
    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "tu_1" and not tool_result["is_error"]
    assert "timeframe=4h" in tool_result["content"]
    assert brain.spent_usd == pytest.approx(2 * (3000 * 4 + 800 * 20) / 1e6)


def test_claude_brain_budget_and_refusal(cfg, candles):
    ctx = claude_context(cfg, candles)
    cfg.brain.llm_budget_usd = 0.01
    brain = ClaudeBrain(cfg.brain, client=FakeClient([]))
    brain.spent_usd = 0.02
    with pytest.raises(BudgetExceeded):
        brain.decide(ctx)

    from trader.brains.base import BrainError
    cfg.brain.llm_budget_usd = 5
    brain = ClaudeBrain(cfg.brain, client=FakeClient([response("refusal", [])]))
    with pytest.raises(BrainError, match="declined"):
        brain.decide(ctx)


def test_render_context_mentions_constraints(cfg, candles):
    ctx = claude_context(cfg, candles)
    text = render_context(ctx)
    assert "# Market" in text and "AAA/EUR" in text
    assert "min position" in text
    assert "round-trip cost" in text


def test_failing_brain_does_not_stop_the_agent(cfg, candles, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    import anthropic

    class Broken:
        name = "broken"

        def decide(self, ctx):
            raise RuntimeError("bug")

    for brain in (Broken(), ClaudeBrain(cfg.brain, client=anthropic.Anthropic(max_retries=0))):
        cfg.run.name = f"fail-{brain.name}"
        clock, sim = sim_setup(cfg, candles, hours=12)
        agent = TradingAgent(cfg, sim, brain, Journal(cfg.run_dir), clock)
        summary = agent.run()
        assert summary["stopped_reason"] == "end of run"
        assert len(agent.journal.events({"error"})) >= 6  # retried every hour (sim tick), never gave up


def test_budget_exhaustion_switches_to_rules(cfg, candles):
    clock, sim = sim_setup(cfg, candles, hours=6)
    cfg.brain.llm_budget_usd = 0.0
    claude = ClaudeBrain(cfg.brain, client=FakeClient([]))
    agent = TradingAgent(cfg, sim, claude, Journal(cfg.run_dir), clock, fallback=RuleBrain(2))
    agent.run()
    assert agent.brain_disabled
    assert agent.journal.events({"brain_disabled"})
    sources = {e["source"] for e in agent.journal.events({"decision"})}
    assert sources == {"rules"}
