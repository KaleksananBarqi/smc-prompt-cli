"""Unit tests for BitunixSource (network-free with mocks)."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import requests

from smc_prompt import config as cfg
from smc_prompt.bitunix_source import BitunixSource
from smc_prompt.errors import ConfigError, NetworkError, SymbolNotFoundError
from smc_prompt.models import Candle


@pytest.fixture
def base_config() -> cfg.Config:
    return cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_BITUNIX)


def test_bitunix_validate_symbol_success(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "code": 0,
        "msg": "Success",
        "data": [
            {
                "symbol": "BTCUSDT",
                "base": "BTC",
                "quote": "USDT",
                "quotePrecision": 1,
                "basePrecision": 4,
            },
            {
                "symbol": "ETHUSDT",
                "base": "ETH",
                "quote": "USDT",
                "quotePrecision": 2,
                "basePrecision": 3,
            },
        ],
    }
    session.get.return_value = response

    source = BitunixSource(base_config, session=session)
    entry = source.validate_symbol()

    assert entry["symbol"] == "BTCUSDT"
    assert entry["status"] == "TRADING"
    assert entry["filters"][0]["tickSize"] == "0.1"


def test_bitunix_validate_symbol_not_found(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "code": 0,
        "msg": "Success",
        "data": [
            {"symbol": "ETHUSDT", "quotePrecision": 2},
        ],
    }
    session.get.return_value = response

    source = BitunixSource(base_config, session=session)
    with pytest.raises(SymbolNotFoundError, match="is not available on Bitunix Futures"):
        source.validate_symbol()


def test_bitunix_fetch_klines_parses_and_reverses(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    # Bitunix returns newest-first
    response.json.return_value = {
        "code": 0,
        "msg": "Success",
        "data": [
            {
                "open": "85400.0",
                "high": "85600.0",
                "low": "85300.0",
                "close": "85500.0",
                "baseVol": "120.5",
                "quoteVol": "10000000.0",
                "time": "1700003600000",  # Newer
            },
            {
                "open": "85000.0",
                "high": "85500.0",
                "low": "84900.0",
                "close": "85400.0",
                "baseVol": "110.2",
                "quoteVol": "9000000.0",
                "time": "1700000000000",  # Older
            },
        ],
    }
    session.get.return_value = response

    # Fixed now well after both candles
    now_moment = datetime.fromtimestamp(1700010000, tz=timezone.utc)
    source = BitunixSource(base_config, session=session, now=lambda: now_moment)
    candles = source.fetch_klines("1h", 2)

    assert len(candles) == 2
    # Oldest first after reversal
    assert candles[0].open == Decimal("85000.0")
    assert candles[0].close == Decimal("85400.0")
    assert candles[0].volume == Decimal("110.2")
    assert candles[0].is_closed is True

    assert candles[1].open == Decimal("85400.0")
    assert candles[1].close == Decimal("85500.0")
    assert candles[1].volume == Decimal("120.5")


def test_bitunix_fetch_klines_pagination(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)

    page1_response = MagicMock(spec=requests.Response)
    page1_response.status_code = 200
    page1_data = [
        {
            "open": f"85000.{i}",
            "high": "85100.0",
            "low": "84900.0",
            "close": "85050.0",
            "baseVol": "10.0",
            "time": str(1700000000000 + (200 - i) * 3600000),
        }
        for i in range(200)
    ]
    page1_response.json.return_value = {"code": 0, "data": page1_data}

    page2_response = MagicMock(spec=requests.Response)
    page2_response.status_code = 200
    page2_data = [
        {
            "open": "84000.0",
            "high": "84100.0",
            "low": "83900.0",
            "close": "84050.0",
            "baseVol": "10.0",
            "time": "1699000000000",
        }
    ]
    page2_response.json.return_value = {"code": 0, "data": page2_data}

    session.get.side_effect = [page1_response, page2_response]

    source = BitunixSource(base_config, session=session)
    candles = source.fetch_klines("1h", 201)

    assert len(candles) == 201
    assert session.get.call_count == 2


def test_bitunix_fetch_current_price_success(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "code": 0,
        "msg": "Success",
        "data": [
            {
                "symbol": "BTCUSDT",
                "lastPrice": "85250.5",
                "markPrice": "85250.0",
            }
        ],
    }
    session.get.return_value = response

    source = BitunixSource(base_config, session=session)
    price = source.fetch_current_price()

    assert price == Decimal("85250.5")


def test_bitunix_fetch_current_price_fallback(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {"code": 0, "data": []}
    session.get.return_value = response

    source = BitunixSource(base_config, session=session)
    ref_candle = Candle(
        open_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=Decimal("85000"),
        high=Decimal("85500"),
        low=Decimal("84900"),
        close=Decimal("85400"),
        volume=Decimal("100"),
        close_time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        is_closed=True,
    )

    fallback = source.fetch_current_price(reference_candle=ref_candle)
    assert fallback == Decimal("85400")


def test_bitunix_with_now(base_config: cfg.Config) -> None:
    source = BitunixSource(base_config)
    now_point = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    new_source = source.with_now(now_point)

    assert new_source.fetch_server_time() == now_point
