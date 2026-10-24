import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader.config import Config  # noqa: E402

START_MS = 1_760_000_400_000  # an exact hour, 2025-10-09 09:00 UTC
HOUR_MS = 3_600_000


def make_candles(n: int, start_price: float, drift: float, vol: float, seed: int,
                 start_ms: int = START_MS, tf_ms: int = HOUR_MS) -> list[list[float]]:
    """Geometric random walk OHLCV with roughly 100k quote volume per candle."""
    rng = random.Random(seed)
    rows, price = [], start_price
    for i in range(n):
        o = price
        c = o * math.exp(rng.gauss(drift, vol))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        low = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        v = rng.uniform(50_000, 150_000) / c
        rows.append([start_ms + i * tf_ms, o, h, low, c, v])
        price = c
    return rows


@pytest.fixture
def candles():
    return {
        "AAA/EUR": make_candles(600, 100.0, 0.0015, 0.008, seed=1),   # trending up
        "BBB/EUR": make_candles(600, 2.0, -0.0010, 0.010, seed=2),    # trending down
        "CCC/EUR": make_candles(600, 0.5, 0.0, 0.012, seed=3),        # sideways
    }


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.run.runs_dir = str(tmp_path / "runs")
    c.run.name = "test"
    c.exchange.symbols = ["AAA/EUR", "BBB/EUR", "CCC/EUR"]
    c.exchange.min_quote_volume_24h = 10_000
    c.brain.type = "rules"
    return c
