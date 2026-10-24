import json
import logging
import os
import shutil
import stat
import subprocess

import pytest

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

from trader import cli
from trader.config import Config, load_dotenv, validate
from trader.exchange import BlockedOperation, CcxtExchange, OrderError
from trader.journal import Journal
from trader.security import RedactingFormatter, audit, clean_text, redact, write_env_file

SECRET = "sk-ant-api03-SUPERSECRETVALUE"


@pytest.fixture
def secret_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("EXCHANGE_API_SECRET", "exchange-secret-123456")
    return SECRET


def test_redact_and_log_formatter(secret_env):
    assert redact(f"auth failed for key {SECRET}") == "auth failed for key ***"
    formatter = RedactingFormatter("%(message)s")
    try:
        raise ValueError("bad key " + os.environ["EXCHANGE_API_SECRET"])
    except ValueError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "error with %s", (SECRET,), None)
        import sys
        record.exc_info = sys.exc_info()
    text = formatter.format(record)
    assert SECRET not in text and "exchange-secret-123456" not in text
    assert text.startswith("error with ***") and "ValueError: bad key ***" in text


def test_journal_never_stores_secrets(secret_env, tmp_path):
    j = Journal(tmp_path)
    j.log("error", 0.0, error=f"401 Unauthorized: {SECRET}")
    raw = (tmp_path / "journal.jsonl").read_text()
    assert SECRET not in raw
    assert json.loads(raw)["error"] == "401 Unauthorized: ***"


def test_dotenv_loads_only_secret_keys(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "EXCHANGE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=abc123456\nANTHROPIC_BASE_URL=https://evil.example\n"
                   "export EXCHANGE_API_KEY='k-123456'\n# comment\n")
    ignored = load_dotenv(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "abc123456"
    assert os.environ["EXCHANGE_API_KEY"] == "k-123456"
    assert "ANTHROPIC_BASE_URL" not in os.environ
    assert ignored == ["ANTHROPIC_BASE_URL"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_env_file_permissions(tmp_path):
    env = tmp_path / ".env"
    write_env_file(env, {"ANTHROPIC_API_KEY": "abc", "EXCHANGE_API_KEY": ""})
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert env.read_text() == "ANTHROPIC_API_KEY=abc\n"

    os.chmod(env, 0o644)
    findings = audit(env)
    assert any("permissions fixed" in m for _, m in findings)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


@needs_git
def test_audit_flags_env_tracked_by_git(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    env = tmp_path / ".env"
    write_env_file(env, {"ANTHROPIC_API_KEY": "abc"})
    subprocess.run(["git", "add", "-f", ".env"], cwd=tmp_path, check=True)
    assert ("critical" in [level for level, _ in audit(env)])


def test_clean_text_strips_terminal_escapes():
    assert clean_text("buy \x1b[2J\x1b]0;pwned\x07 now" + chr(0x202E)) == "buy  [2J ]0;pwned  now "
    assert clean_text("a" * 50, 10) == "a" * 10


def test_dangerous_exchange_methods_are_blocked():
    ex = CcxtExchange("bitvavo", "EUR", credentials={"apiKey": "k", "secret": "s"})
    for name in ("withdraw", "transfer", "create_deposit_address", "createDepositAddress", "set_leverage"):
        if hasattr(ex.client, name):
            with pytest.raises(BlockedOperation):
                getattr(ex.client, name)("BTC", 1, "address")
    with pytest.raises(OrderError):
        ex.buy("BTC/EUR", 5.0)  # markets not loaded -> nothing is allowed


def test_stablecoins_rejected():
    cfg = Config()
    cfg.exchange.symbols = ["BTC/EUR", "USDC/EUR"]
    with pytest.raises(ValueError, match="stablecoins"):
        validate(cfg)


def test_live_refuses_large_capital(tmp_path, capsys):
    rc = cli.main(["--env-file", str(tmp_path / ".env"), "run", "--live", "--capital", "500",
                   "--brain", "rules", "--name", "x"])
    assert rc == 2
    assert "max_live_capital" in capsys.readouterr().out


@needs_git
def test_live_refuses_when_env_is_in_git(tmp_path, capsys):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    env = tmp_path / ".env"
    write_env_file(env, {"EXCHANGE_API_KEY": "k-123456"})
    subprocess.run(["git", "add", "-f", ".env"], cwd=tmp_path, check=True)
    rc = cli.main(["--env-file", str(env), "run", "--live", "--brain", "rules", "--name", "x"])
    assert rc == 2
    assert "Refusing live trading" in capsys.readouterr().out


def test_keys_command_writes_private_env(tmp_path, monkeypatch, capsys):
    answers = iter(["sk-ant-test-123456", "exkey-123456", "exsecret-123456", ""])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    env = tmp_path / ".env"
    assert cli.main(["--env-file", str(env), "keys"]) == 0
    text = env.read_text()
    assert "EXCHANGE_API_SECRET=exsecret-123456" in text and "PASSWORD" not in text
    if os.name == "posix":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "exsecret-123456" not in out
