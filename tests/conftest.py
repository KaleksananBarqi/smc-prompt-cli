"""Shared fixtures/helpers for the smc-prompt test suite.

The suite is network-free: analysis tests use in-memory candles and the render
tests feed the offline CSV source (#10).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from smc_prompt import config as cfg
from smc_prompt.models import Candle, SwingPoint, SwingType

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
HTF_CSV = FIXTURE_DIR / "htf_daily.csv"
MTF_CSV = FIXTURE_DIR / "mtf_4h.csv"
LTF_CSV = FIXTURE_DIR / "ltf_hourly.csv"

#: Byte-frozen hash of the offline render produced from the committed fixtures.
#: Regenerate with ``python tests/fixtures/generate_fixtures.py`` (deterministic
#: fixtures) and the hash with::
#:
#:     python -c "import hashlib; from smc_prompt import cli; \
#:       from tests.conftest import HTF_CSV, MTF_CSV, LTF_CSV; \
#:       t=cli.run('BTCUSDT', htf_candles=60, ltf_candles=100, \
#:       swing_lookback=5, distance_reference='nearest', include_atr=True, \
#:       output_dir='output', htf_interval='1d', ltf_interval='1h', \
#:       input_csv=str(HTF_CSV), htf_file=str(HTF_CSV), \
#:       mtf_file=str(MTF_CSV), ltf_file=str(LTF_CSV)).prompt; \
#:       print(hashlib.sha256(t.encode('utf-8')).hexdigest(), len(t.encode('utf-8')))"
#:
#: The 3-tier extension added the MTF data block, so the hash and byte count
#: were regenerated from the 3-fixture offline render (HTF + MTF + LTF).
GOLDEN_SHA256 = "1ca8eed0d35cdea9a999cdd0536c8f18c6c42d0633a44d8d0eb40c296fabfd09"
GOLDEN_BYTES = 30796

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_candle(
    index: int,
    high: str | float,
    low: str | float,
    *,
    open_: str | float | None = None,
    close: str | float | None = None,
    volume: str | float = "1",
    is_closed: bool = True,
    base: datetime = BASE_TIME,
    step: timedelta = timedelta(hours=1),
) -> Candle:
    """Build a deterministic :class:`Candle` at ``base + index * step``."""

    open_time = base + index * step
    open_value = Decimal(str(open_ if open_ is not None else high))
    close_value = Decimal(str(close if close is not None else low))
    return Candle(
        open_time=open_time,
        open=open_value,
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=close_value,
        volume=Decimal(str(volume)),
        close_time=open_time + step,
        is_closed=is_closed,
    )


def make_swing(
    index: int,
    price: str | float,
    swing_type: SwingType,
    *,
    base: datetime = BASE_TIME,
) -> SwingPoint:
    """Build a deterministic :class:`SwingPoint`."""

    return SwingPoint(
        index=index,
        open_time=base + timedelta(hours=index),
        price=Decimal(str(price)),
        type=swing_type,
    )


@pytest.fixture()
def config() -> cfg.Config:
    """Default config for a generic test symbol."""

    return cfg.build_config("TESTUSDT")


@pytest.fixture(autouse=True)
def _no_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize clipboard access so tests stay headless and deterministic."""

    def _noop(text: str) -> None:
        return None

    monkeypatch.setattr("smc_prompt.output.copy_to_clipboard", _noop)
