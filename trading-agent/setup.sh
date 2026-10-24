#!/usr/bin/env bash
# One-time setup on macOS / Linux: private virtualenv, hash-verified dependencies, config, tests.
set -euo pipefail
cd "$(dirname "$0")"
umask 077

PY=""
for candidate in python3.13 python3.12 python3.14 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11-3.14 not found. Install Python 3.13 from https://www.python.org/downloads/ and run this again."
  exit 1
fi
echo "Using $("$PY" --version)"

[ -d .venv ] || "$PY" -m venv .venv
VPY=.venv/bin/python

echo "Installing dependencies (every file checked against its SHA-256 hash)..."
if ! "$VPY" -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary=:all: -r requirements.lock; then
  echo "No prebuilt package for this platform; building the hash-verified source packages instead."
  "$VPY" -m pip install --quiet --disable-pip-version-check --require-hashes -r requirements.lock
fi
"$VPY" -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary=:all: -r requirements-dev.lock

[ -f config.toml ] || cp config.example.toml config.toml
[ -f .env ] || : > .env
chmod 600 .env
mkdir -p runs && chmod 700 runs

echo "Running tests..."
"$VPY" -m pytest -q

cat <<'EOF'

Setup complete. Next steps:
  source .venv/bin/activate
  python -m trader keys      # enter API keys (hidden input, stored only in .env)
  python -m trader check     # security check, exchange data, keys
  python -m trader run --name paper-week     # one week of paper trading
EOF
