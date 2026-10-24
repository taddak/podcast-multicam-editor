"""Command line: keys, check, run, report, close-all, backtest."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from .config import Config, exchange_credentials, load_config, load_dotenv, validate
from .security import DEFAULT_ENV_FILE, PROJECT_DIR, SECRET_KEYS, RedactingFormatter, audit, harden_process, write_env_file

log = logging.getLogger("trader")


def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    formatter = RedactingFormatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True)
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
    if getattr(a, "name", None):
        cfg.run.name = a.name
    if getattr(a, "exchange", None):
        cfg.exchange.id = a.exchange
    if getattr(a, "symbols", None):
        cfg.exchange.symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if getattr(a, "capital", None) is not None:
        cfg.run.capital = a.capital
    if getattr(a, "hours", None) is not None:
        cfg.run.duration_hours = a.hours
    if getattr(a, "brain", None):
        cfg.brain.type = a.brain
    if getattr(a, "model", None):
        cfg.brain.model = a.model
    if getattr(a, "effort", None):
        cfg.brain.effort = a.effort
    if getattr(a, "budget", None) is not None:
        cfg.brain.llm_budget_usd = a.budget
    if getattr(a, "web_search", False):
        cfg.brain.web_search = True
    validate(cfg)
    return cfg


def _print_audit(env_file: Path) -> bool:
    """Print the security audit; True if nothing critical was found."""
    ok = True
    for level, message in audit(env_file):
        print(f"  [{level}] {message}")
        ok = ok and level != "critical"
    return ok


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


def _make_agent(cfg: Config, journal, live: bool):
    from .agent import TradingAgent
    from .brains import RuleBrain, build_brain
    from .clock import RealClock
    from .exchange import build_exchange

    cfg.run.mode = "live" if live else "paper"
    exchange = build_exchange(cfg, exchange_credentials())
    brain = build_brain(cfg)
    fallback = None
    if cfg.brain.type == "claude" and cfg.brain.fallback_to_rules:
        fallback = RuleBrain(cfg.risk.max_positions, cfg.risk.min_stop_loss_pct, cfg.risk.max_stop_loss_pct)
    return TradingAgent(cfg, exchange, brain, journal, RealClock(), fallback)


def cmd_run(cfg: Config, a: argparse.Namespace) -> int:
    import ccxt

    from .journal import Journal
    from .report import format_report, summarize

    print("Security check:")
    safe = _print_audit(a.env_file)
    if a.live:
        if not safe:
            print("Refusing live trading until the critical problems above are fixed.")
            return 2
        if cfg.run.capital > cfg.run.max_live_capital:
            print(f"capital {cfg.run.capital:.2f} is above run.max_live_capital ({cfg.run.max_live_capital:.2f}); "
                  "raise that limit in config.toml if you really mean it")
            return 2
        if not _confirm_live(cfg, a.yes_real_money):
            return 2
    journal = Journal(cfg.run_dir)
    _setup_logging(a.verbose, journal.dir / "agent.log")
    if cfg.brain.type == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY is not set: Claude cannot decide (run `python -m trader keys`)")
    try:
        agent = _make_agent(cfg, journal, a.live)
        if cfg.brain.type == "claude":
            log.info("Claude brain: %s, effort %s, budget $%.2f; refusal fallbacks are enabled server-side",
                     cfg.brain.model, cfg.brain.effort, cfg.brain.llm_budget_usd)
        agent.run()
    except KeyboardInterrupt:
        return 130
    except ccxt.BaseError as e:
        log.error("exchange %s is not usable: %s", cfg.exchange.id, e)
        return 1
    except (RuntimeError, ValueError) as e:
        log.error("%s", e)
        return 1
    print()
    print(format_report(summarize(journal.dir), cfg.exchange.quote))
    return 0


def cmd_close_all(cfg: Config, a: argparse.Namespace) -> int:
    """Panic button: sell every open position of a run and mark the run finished."""
    import ccxt

    from .journal import Journal
    from .report import format_report, summarize

    journal = Journal(cfg.run_dir)
    state = journal.load_state()
    if not state:
        print(f"no run named {cfg.run.name!r} in {cfg.run.runs_dir}/")
        return 1
    if state.get("mode") == "live" and not a.live:
        print("this is a live run: add --live to sell the real positions")
        return 2
    _setup_logging(a.verbose, journal.dir / "agent.log")
    try:
        agent = _make_agent(cfg, journal, state.get("mode") == "live")
        agent.start()
        agent.stopped_reason = "closed manually"
        agent.finish(force_close=True)
    except ccxt.BaseError as e:
        log.error("exchange %s is not usable: %s", cfg.exchange.id, e)
        return 1
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    print(format_report(summarize(journal.dir), cfg.exchange.quote))
    return 0


def cmd_keys(cfg: Config, a: argparse.Namespace) -> int:
    """Enter API keys without echo; they go only into the private .env file."""
    path = Path(a.env_file)
    current: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in SECRET_KEYS:
                    current[k.strip()] = v.strip()
    print(f"Keys are stored only in {path} (readable by you only). Input is hidden; Enter keeps the current value,")
    print("a single '-' deletes it. Never paste keys into a chat, an issue or a commit.\n")
    labels = {
        "ANTHROPIC_API_KEY": "Claude API key (sk-ant-...)",
        "EXCHANGE_API_KEY": f"{cfg.exchange.id} API key",
        "EXCHANGE_API_SECRET": f"{cfg.exchange.id} API secret",
        "EXCHANGE_API_PASSWORD": f"{cfg.exchange.id} API passphrase (only OKX; Enter to skip)",
    }
    for key in SECRET_KEYS:
        status = "set" if current.get(key) else "not set"
        value = getpass.getpass(f"{labels[key]} [{status}]: ").strip()
        if value == "-":
            current.pop(key, None)
        elif value:
            current[key] = value
    write_env_file(path, current)
    print(f"\nSaved. Set: {', '.join(k for k in SECRET_KEYS if current.get(k)) or 'nothing'}")
    return 0


def cmd_check(cfg: Config, a: argparse.Namespace) -> int:
    from .exchange import CcxtExchange, PaperExchange
    from .risk import RiskManager

    _setup_logging(a.verbose)
    print("Security check:")
    _print_audit(a.env_file)
    ex_cfg = cfg.exchange
    print(f"\nExchange {ex_cfg.id}, quote {ex_cfg.quote}")
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
            print("  Make sure the key has NO withdrawal permission (check it in the exchange's API settings).")
        except Exception as e:
            print(f"  API key check FAILED: {e}")
    else:
        print("  no exchange API key set: paper trading only")

    if cfg.brain.type == "claude":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("\nClaude: ANTHROPIC_API_KEY not set (run `python -m trader keys`)")
        else:
            try:
                import anthropic
                info = anthropic.Anthropic().models.retrieve(cfg.brain.model)
                print(f"\nClaude: {info.id} reachable")
            except Exception as e:
                print(f"\nClaude check FAILED for {cfg.brain.model}: {e}")
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
    harden_process()
    parser = argparse.ArgumentParser(prog="python -m trader",
                                     description="Autonomous trading agent for a small spot crypto account.")
    parser.add_argument("-c", "--config", help="TOML config (default: config.toml next to the code, if it exists)")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE,
                        help=f"file with API keys (default: {DEFAULT_ENV_FILE})")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("keys", help="enter API keys securely (hidden input, private .env file)")

    p = sub.add_parser("check", help="security check, exchange data, minimum order sizes, API keys, Claude access")
    _overrides(p)

    p = sub.add_parser("run", help="run the agent (paper trading unless --live)")
    _overrides(p)
    p.add_argument("--live", action="store_true", help="trade real money")
    p.add_argument("--yes-real-money", action="store_true", help="skip the confirmation prompt for --live")

    p = sub.add_parser("report", help="summarize a run (finished or in progress)")
    _overrides(p)
    p.add_argument("--csv", action="store_true", help="also write equity.csv into the run folder")

    p = sub.add_parser("close-all", help="panic button: sell all positions of a run and end it")
    _overrides(p)
    p.add_argument("--live", action="store_true", help="required for a live run")

    p = sub.add_parser("backtest", help="replay history through the agent (rule brain by default)")
    _overrides(p)
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--timeframe", default="1h", choices=["15m", "1h", "4h"])
    p.add_argument("--data-dir", default="data")
    p.add_argument("--offline", action="store_true", help="use cached CSV files only")
    p.add_argument("--min-order", type=float, default=5.0, help="simulated exchange minimum order (quote)")
    p.add_argument("--yes-llm-cost", action="store_true", help="allow a Claude-driven backtest")

    a = parser.parse_args(argv)
    ignored = load_dotenv(a.env_file)
    if ignored:
        print(f"note: ignored non-key entries in {a.env_file}: {', '.join(ignored)}")
    config_path = a.config
    if config_path is None:
        for candidate in (Path("config.toml"), PROJECT_DIR / "config.toml"):
            if candidate.exists():
                config_path = candidate
                break
    try:
        cfg = _apply_overrides(load_config(config_path), a)
    except (ValueError, OSError) as e:
        print(f"config error: {e}")
        return 2
    handler = {"keys": cmd_keys, "check": cmd_check, "run": cmd_run, "report": cmd_report,
               "close-all": cmd_close_all, "backtest": cmd_backtest}[a.command]
    return handler(cfg, a)
