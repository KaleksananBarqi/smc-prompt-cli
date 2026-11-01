"""Unit tests for MexcSource (network-free with mocks)."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import requests

from smc_prompt import config as cfg
from smc_prompt.mexc_source import MexcSource
from smc_prompt.errors import NetworkError, SymbolNotFoundError
from smc_prompt.models import Candle


@pytest.fixture
def base_config() -> cfg.Config:
    return cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_MEXC)


def test_mexc_validate_symbol_success(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "code": 0,
        "data": {
            "symbol": "BTC_USDT",
            "priceScale": 1,
            "state": 0,
        },
    }
    session.get.return_value = response

    source = MexcSource(base_config, session=session)
    entry = source.validate_symbol()

    assert entry["symbol"] == "BTC_USDT"
    assert entry["status"] == "TRADING"
    assert entry["filters"][0]["tickSize"] == "0.1"


def test_mexc_validate_symbol_not_found(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "success": False,
        "code": 2,
        "msg": "contract not found",
        "data": None,
    }
    session.get.return_value = response

    source = MexcSource(base_config, session=session)
    with pytest.raises(SymbolNotFoundError, match="contract not found"):
        source.validate_symbol()


def test_mexc_fetch_klines_success(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    # MEXC returns time, open, close, high, low, vol lists (oldest to newest)
    response.json.return_value = {
        "success": True,
        "code": 0,
        "data": {
            "time": [1700000000, 1700003600],
            "open": ["85000.0", "85400.0"],
            "high": ["85500.0", "85600.0"],
            "low": ["84900.0", "85300.0"],
            "close": ["85400.0", "85500.0"],
            "vol": ["110.2", "120.5"],
        },
    }
    session.get.return_value = response

    source = MexcSource(
        base_config,
        session=session,
        now=lambda: datetime.fromtimestamp(1700010000, tz=timezone.utc),
    )
    candles = source.fetch_klines("1h", 10)

    assert len(candles) == 2
    assert candles[0].open == Decimal("85000.0")
    assert candles[0].close == Decimal("85400.0")
    assert candles[0].volume == Decimal("110.2")
    assert candles[1].open == Decimal("85400.0")
    assert candles[1].close == Decimal("85500.0")
    assert candles[0].is_closed is True
    assert candles[1].is_closed is True


def test_mexc_fetch_current_price_success(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "code": 0,
        "data": {
            "symbol": "BTC_USDT",
            "lastPrice": "85550.5",
        },
    }
    session.get.return_value = response

    source = MexcSource(base_config, session=session)
    price = source.fetch_current_price()
    assert price == Decimal("85550.5")


def test_mexc_fetch_current_price_fallback(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "code": 0,
        "data": {
            "symbol": "BTC_USDT",
            "lastPrice": None,
        },
    }
    session.get.return_value = response

    ref_candle = Candle(
        open_time=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        open=Decimal("85000"),
        high=Decimal("85500"),
        low=Decimal("84900"),
        close=Decimal("85200"),
        volume=Decimal("100"),
        close_time=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        is_closed=True,
    )
    source = MexcSource(base_config, session=session)
    price = source.fetch_current_price(reference_candle=ref_candle)
    assert price == Decimal("85200")


def test_mexc_fetch_server_time(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "code": 0,
        "data": 1700000000000,
    }
    session.get.return_value = response

    source = MexcSource(base_config, session=session)
    st = source.fetch_server_time()
    assert st == datetime.fromtimestamp(1700000000, tz=timezone.utc)


def test_mexc_server_error_500_raises_network_error(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "success": False,
        "code": 500,
        "msg": "Internal system error",
        "data": None,
    }
    session.get.return_value = response

    source = MexcSource(base_config, session=session)
    with pytest.raises(NetworkError, match="Internal system error"):
        source.validate_symbol()
