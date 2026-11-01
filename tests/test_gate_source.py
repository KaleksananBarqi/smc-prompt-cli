"""Unit tests for GateSource (network-free with mocks)."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import requests

from smc_prompt import config as cfg
from smc_prompt.gate_source import GateSource
from smc_prompt.errors import NetworkError, SymbolNotFoundError
from smc_prompt.models import Candle


@pytest.fixture
def base_config() -> cfg.Config:
    return cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_GATE)


def test_gate_validate_symbol_success(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "name": "BTC_USDT",
        "order_price_round": "0.1",
        "in_delisting": False,
    }
    session.get.return_value = response

    source = GateSource(base_config, session=session)
    entry = source.validate_symbol()

    assert entry["symbol"] == "BTC_USDT"
    assert entry["status"] == "TRADING"
    assert entry["filters"][0]["tickSize"] == "0.1"


def test_gate_validate_symbol_not_found(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "label": "CONTRACT_NOT_FOUND",
        "message": "Contract not found",
    }
    session.get.return_value = response

    source = GateSource(base_config, session=session)
    with pytest.raises(SymbolNotFoundError, match="CONTRACT_NOT_FOUND"):
        source.validate_symbol()


def test_gate_fetch_klines_success(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = [
        {
            "t": 1700000000,
            "o": "85000.0",
            "h": "85500.0",
            "l": "84900.0",
            "c": "85400.0",
            "v": 1102,
        },
        {
            "t": 1700003600,
            "o": "85400.0",
            "h": "85600.0",
            "l": "85300.0",
            "c": "85500.0",
            "v": 1205,
        },
    ]
    session.get.return_value = response

    source = GateSource(
        base_config,
        session=session,
        now=lambda: datetime.fromtimestamp(1700010000, tz=timezone.utc),
    )
    candles = source.fetch_klines("1h", 10)

    assert len(candles) == 2
    assert candles[0].open == Decimal("85000.0")
    assert candles[0].close == Decimal("85400.0")
    assert candles[0].volume == Decimal("1102")
    assert candles[1].open == Decimal("85400.0")
    assert candles[1].close == Decimal("85500.0")
    assert candles[0].is_closed is True
    assert candles[1].is_closed is True


def test_gate_fetch_current_price_success(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = [
        {
            "contract": "BTC_USDT",
            "last": "85550.5",
        }
    ]
    session.get.return_value = response

    source = GateSource(base_config, session=session)
    price = source.fetch_current_price()
    assert price == Decimal("85550.5")


def test_gate_fetch_current_price_fallback(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = []
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
    source = GateSource(base_config, session=session)
    price = source.fetch_current_price(reference_candle=ref_candle)
    assert price == Decimal("85200")


def test_gate_fetch_server_time(base_config: cfg.Config) -> None:
    session = MagicMock(spec=requests.Session)
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.json.return_value = {
        "server_time": 1700000000000,
    }
    session.get.return_value = response

    source = GateSource(base_config, session=session)
    st = source.fetch_server_time()
    assert st == datetime.fromtimestamp(1700000000, tz=timezone.utc)
