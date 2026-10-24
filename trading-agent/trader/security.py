"""Defense in depth for a program that holds exchange and API keys.

* secrets are read only from an allow-list of variables and redacted from every log line and journal entry,
* files the agent writes are private to the user (umask 077 on macOS/Linux),
* model-written text is stripped of control characters before it reaches the terminal,
* `audit()` reports risky setups: a .env readable by others, tracked by git or inside a synced folder.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
# subprocess is used only for a fixed `git ls-files` query
import subprocess  # nosec B404
import sys
from pathlib import Path

SECRET_KEYS = ("ANTHROPIC_API_KEY", "EXCHANGE_API_KEY", "EXCHANGE_API_SECRET", "EXCHANGE_API_PASSWORD")
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"

# C0/C1 control characters (except tab and newline) and the Unicode bidi overrides used in
# terminal-escape and "trojan source" tricks; written as code points, never as literal characters.
_CONTROL_CODES = frozenset([*range(0x00, 0x09), *range(0x0B, 0x20), *range(0x7F, 0xA0),
                            *range(0x202A, 0x202F), *range(0x2066, 0x206A)])
_SYNCED_FOLDERS = ("dropbox", "onedrive", "google drive", "googledrive", "icloud", "mobile documents", "mega")


def _secret_values() -> list[str]:
    values = [os.environ.get(k, "") for k in SECRET_KEYS]
    return sorted((v for v in values if len(v) >= 6), key=len, reverse=True)


def redact(text: str) -> str:
    for value in _secret_values():
        if value in text:
            text = text.replace(value, "***")
    return text


def clean_text(text: str, limit: int | None = None) -> str:
    """Printable text only: no terminal escape sequences or bidi tricks from model output."""
    text = "".join(" " if ord(ch) in _CONTROL_CODES else ch for ch in str(text))
    return text[:limit] if limit else text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def harden_process() -> None:
    """Every file created from now on is readable only by the current user (POSIX)."""
    if os.name == "posix":
        os.umask(0o077)


def secure_file(path: Path) -> None:
    if os.name == "posix" and path.exists():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write KEY=VALUE lines with 0600 permissions from the start (never world-readable, not even briefly)."""
    lines = "".join(f"{k}={v}\n" for k, v in values.items() if v)
    if os.name == "posix":
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(lines)
        secure_file(path)
    else:
        path.write_text(lines, encoding="utf-8")


def _tracked_by_git(path: Path) -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    try:
        # Fixed argument list, no shell: nothing user-controlled reaches a command line.
        result = subprocess.run([git, "ls-files", "--error-unmatch", path.name], cwd=path.parent,  # nosec B603
                                capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def audit(env_file: Path) -> list[tuple[str, str]]:
    """(level, message) pairs; level is 'critical', 'warning' or 'ok'."""
    findings: list[tuple[str, str]] = []
    if sys.version_info < (3, 11):
        findings.append(("critical", "Python 3.11 or newer is required"))
    if env_file.exists():
        if _tracked_by_git(env_file):
            findings.append(("critical", f"{env_file} is tracked by git: remove it (git rm --cached) and rotate the keys"))
        if os.name == "posix" and env_file.stat().st_mode & 0o077:
            secure_file(env_file)
            findings.append(("warning", f"{env_file} was readable by other users; permissions fixed to 600"))
        lowered = str(env_file.resolve()).lower()
        if any(name in lowered for name in _SYNCED_FOLDERS):
            findings.append(("warning", f"{env_file} is inside a cloud-synced folder; keys get copied to the cloud"))
    for var in ("ANTHROPIC_BASE_URL", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        if os.environ.get(var) or os.environ.get(var.lower()):
            findings.append(("warning", f"{var} is set: API traffic (with your keys) goes through {var}"))
    if not findings:
        findings.append(("ok", "no problems found"))
    return findings
