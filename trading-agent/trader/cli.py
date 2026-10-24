"""Command line: run, check, backtest, report."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import Config, exchange_credentials, load_config, load_dotenv, validate

log = logging.getLogger("trader")


def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("httpx", "httpx2", "anthropic", "urllib3", "ccxt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _overrides(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", help="run name (folder under runs/); reuse it to resume")
    p.add_argument("--exchange", help="ccxt exchange id, e.g. bitvavo, kraken, myokx, bybiteu")
    p.add_argument("--symbols", help="comma separated, e.g. BTC/EUR,ETH/EUR")
    p.add_argument("--capital", type=float, help="capital the agent may use, in the quote currency")
    p.add_argument("--hours", type=float, help="run length in hours (168 = one week)")
    p.add_argument("--brain", choices=["claude", "rules"])
    p.add_argument("--model", help="Claude model id")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--budget", type=float, help="max Claude API spend in USD for the run")
    p.add_argument("--web-search", action="store_true", help="let Claude search the web for news")


def _apply_overrides(cfg: Config, a: argparse.Namespace) -> Config:
    if a.name:
        cfg.run.name = a.name
    if a.exchange:
        cfg.exchange.id = a.exchange
    if a.symbols:
        cfg.exchange.symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if a.capital is not None:
        cfg.run.capital = a.capital
    if a.hours is not None:
        cfg.run.duration_hours = a.hours
    if a.brain:
        cfg.brain.type = a.brain
    if a.model:
        cfg.brain.model = a.model
    if a.effort:
        cfg.brain.effort = a.effort
    if a.budget is not None:
        cfg.brain.llm_budget_usd = a.budget
    if a.web_search:
        cfg.brain.web_search = True
    validate(cfg)
    return cfg


def _confirm_live(cfg: Config, assume_yes: bool) -> bool:
    print("\n" + "!" * 72)
    print(f"  LIVE TRADING on {cfg.exchange.id} with up to {cfg.run.capital:.2f} {cfg.exchange.quote} of real money.")
    print("  The agent can lose part or all of it. Use API keys WITHOUT withdrawal rights.")
    print("!" * 72 + "\n")
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to start live trading without --yes-real-money in a non-interactive shell.")
        return False
    return input("Type YES to start: ").strip() == "YES"


def cmd_run(cfg: Config, a: argparse.Namespace) -> int:
    import ccxt

    from .agent import TradingAgent
    from .brains import RuleBrain, build_brain
    from .clock import RealClock
    from .exchange import build_exchange
    from .journal import Journal
    from .report import format_report, summarize

    cfg.run.mode = "live" if a.live else "paper"
    if cfg.run.mode == "live" and not _confirm_live(cfg, a.yes_real_money):
        return 2
    journal = Journal(cfg.run_dir)
    _setup_logging(a.verbose, journal.dir / "agent.log")
    if cfg.brain.type == "claude" and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.warning("ANTHROPIC_API_KEY is not set; Claude calls will fail unless an `ant auth login` profile exists")

    exchange = build_exchange(cfg, exchange_credentials())
    brain = build_brain(cfg)
    fallback = None
    if cfg.brain.type == "claude" and cfg.brain.fallback_to_rules:
        fallback = RuleBrain(cfg.risk.max_positions, cfg.risk.min_stop_loss_pct, cfg.risk.max_stop_loss_pct)
    if cfg.brain.type == "claude":
        log.info("Claude brain: %s, effort %s, budget $%.2f; refusal fallbacks are enabled server-side",
                 cfg.brain.model, cfg.brain.effort, cfg.brain.llm_budget_usd)
    agent = TradingAgent(cfg, exchange, brain, journal, RealClock(), fallback)
    try:
        agent.run()
    except KeyboardInterrupt:
        return 130
    except ccxt.BaseError as e:
        log.error("exchange %s is not usable: %s", cfg.exchange.id, e)
        return 1
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    print()
    print(format_report(summarize(journal.dir), cfg.exchange.quote))
    return 0


def cmd_check(cfg: Config, a: argparse.Namespace) -> int:
    from .exchange import CcxtExchange, PaperExchange
    from .risk import RiskManager

    _setup_logging(a.verbose)
    ex_cfg = cfg.exchange
    print(f"Exchange {ex_cfg.id}, quote {ex_cfg.quote}")
    public = PaperExchange(ex_cfg.id, ex_cfg.quote, ex_cfg.options, ex_cfg.taker_fee)
    try:
        markets = public.load_markets(ex_cfg.symbols)
        tickers = public.tickers(list(markets))
    except Exception as e:
        print(f"  public market data FAILED: {e}")
        return 1
    risk = RiskManager(cfg.risk, ex_cfg.max_spread_pct)
    print(f"  {'symbol':<10} {'price':>12} {'spread%':>8} {'24h volume':>14} {'min order':>10} {'min pos.':>9} {'taker%':>7}  ok")
    for symbol, m in markets.items():
        t = tickers.get(symbol)
        if t is None:
            continue
        liquid = t.quote_volume >= ex_cfg.min_quote_volume_24h and t.spread_pct <= ex_cfg.max_spread_pct
        print(f"  {symbol:<10} {t.last:>12.6g} {t.spread_pct:>8.3f} {t.quote_volume:>14,.0f} "
              f"{m.min_order_value(t.last):>10.2f} {risk.min_position(m, t.last):>9.2f} {m.taker_fee * 100:>7.3f}  "
              f"{'yes' if liquid else 'skip'}")
    missing = sorted(set(ex_cfg.symbols) - set(markets))
    if missing:
        print(f"  not listed: {', '.join(missing)}")
    if tickers:
        smallest = min(risk.min_position(markets[s], t.last) for s, t in tickers.items())
        slots = int(cfg.run.capital // smallest) if smallest else 0
        print(f"  with {cfg.run.capital:.2f} {ex_cfg.quote} the agent can hold at most "
              f"{min(slots, cfg.risk.max_positions)} position(s) at once")

    creds = exchange_credentials()
    if creds:
        try:
            live = CcxtExchange(ex_cfg.id, ex_cfg.quote, creds, ex_cfg.options, ex_cfg.taker_fee)
            free = live.free_balance(ex_cfg.quote)
            print(f"  API key OK, free {ex_cfg.quote}: {free:.2f}")
        except Exception as e:
            print(f"  API key check FAILED: {e}")
    else:
        print("  no EXCHANGE_API_KEY set: paper trading only")

    if cfg.brain.type == "claude":
        try:
            import anthropic
            info = anthropic.Anthropic().models.retrieve(cfg.brain.model)
            print(f"Claude: {info.id} reachable")
        except Exception as e:
            print(f"Claude check FAILED for {cfg.brain.model}: {e}")
        print(f"Claude budget cap: ${cfg.brain.llm_budget_usd:.2f} for the run "
              f"(the rule brain takes over when it is spent)")
    return 0


def cmd_backtest(cfg: Config, a: argparse.Namespace) -> int:
    from .backtest import fetch_history, load_history, run_backtest
    from .brains import build_brain
    from .report import format_report, summarize

    _setup_logging(a.verbose)
    data_dir = Path(a.data_dir)
    if a.offline:
        candles = load_history(data_dir, cfg.exchange.id, cfg.exchange.symbols, a.timeframe)
    else:
        candles = fetch_history(cfg.exchange.id, cfg.exchange.symbols, a.timeframe, a.days, data_dir,
                                cfg.exchange.options)
    if not candles:
        print("no candle data")
        return 1
    if a.brain is None:
        cfg.brain.type = "rules"
    if cfg.brain.type == "claude" and not a.yes_llm_cost:
        print("A Claude backtest calls the API for every decision and costs real money; "
              "add --yes-llm-cost to proceed (the budget cap still applies).")
        return 2
    logging.getLogger("trader").setLevel(logging.WARNING if not a.verbose else logging.DEBUG)
    summary = run_backtest(cfg, candles, a.timeframe, build_brain(cfg), a.min_order, a.name)
    print(format_report(summarize(summary["run_dir"]), cfg.exchange.quote))
    print(f"  buy & hold   {summary['buy_and_hold_pct']:+.2f} % (equal-weight basket, same {summary['days']:.1f} days)")
    return 0


def cmd_report(cfg: Config, a: argparse.Namespace) -> int:
    from .report import format_report, summarize, write_equity_csv

    run_dir = cfg.run_dir
    s = summarize(run_dir)
    print(format_report(s, cfg.exchange.quote))
    if a.csv:
        path = run_dir / "equity.csv"
        write_equity_csv(s, path)
        print(f"equity curve written to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader",
                                     description="Autonomous trading agent for a small spot crypto account.")
    parser.add_argument("-c", "--config", help="TOML config (default: config.toml if it exists)")
    parser.add_argument("--env-file", default=".env", help="file with API keys (default: .env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run the agent (paper trading unless --live)")
    _overrides(p)
    p.add_argument("--live", action="store_true", help="trade real money")
    p.add_argument("--yes-real-money", action="store_true", help="skip the confirmation prompt for --live")

    p = sub.add_parser("check", help="check exchange data, minimum order sizes, API keys and Claude access")
    _overrides(p)

    p = sub.add_parser("backtest", help="replay history through the agent (rule brain by default)")
    _overrides(p)
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--timeframe", default="1h", choices=["15m", "1h", "4h"])
    p.add_argument("--data-dir", default="data")
    p.add_argument("--offline", action="store_true", help="use cached CSV files only")
    p.add_argument("--min-order", type=float, default=5.0, help="simulated exchange minimum order (quote)")
    p.add_argument("--yes-llm-cost", action="store_true", help="allow a Claude-driven backtest")

    p = sub.add_parser("report", help="summarize a run (finished or in progress)")
    _overrides(p)
    p.add_argument("--csv", action="store_true", help="also write equity.csv into the run folder")

    a = parser.parse_args(argv)
    load_dotenv(a.env_file)
    config_path = a.config or ("config.toml" if Path("config.toml").exists() else None)
    try:
        cfg = _apply_overrides(load_config(config_path), a)
    except (ValueError, OSError) as e:
        print(f"config error: {e}")
        return 2
    handler = {"run": cmd_run, "check": cmd_check, "backtest": cmd_backtest, "report": cmd_report}[a.command]
    return handler(cfg, a)
