"""Unit tests for DataFetcher against Binance Futures endpoints (network-free)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from smc_prompt import config as cfg
from smc_prompt.data_fetcher import DataFetcher
from smc_prompt.errors import NetworkError, SymbolNotFoundError

_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
_NOW_MS = int(_NOW.timestamp() * 1000)


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    def __init__(self, routes: list[tuple[str, Any]]) -> None:
        self._routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        for fragment, payload in self._routes:
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[1], int):
                    return _FakeResponse(payload[0], status_code=payload[1])
                return _FakeResponse(payload)
        raise AssertionError(f"No fake route matched {url}")


@pytest.fixture
def base_config() -> cfg.Config:
    return cfg.build_config("BTCUSDT")


def test_binance_futures_endpoint_constants() -> None:
    """Ensure endpoint constants point to Binance USDT-M Futures."""
    assert cfg.KLINES_PATH == "/fapi/v1/klines"
    assert cfg.TICKER_PRICE_PATH == "/fapi/v1/ticker/price"
    assert cfg.EXCHANGE_INFO_PATH == "/fapi/v1/exchangeInfo"
    assert cfg.TIME_PATH == "/fapi/v1/time"
    assert cfg.DEFAULT_BASE_URLS == ("https://fapi.binance.com",)


def test_fetch_server_time_fapi(base_config: cfg.Config) -> None:
    session = _FakeSession([
        (cfg.TIME_PATH, {"serverTime": _NOW_MS}),
    ])
    fetcher = DataFetcher(base_config, session=session, now=lambda: _NOW)
    server_time = fetcher.fetch_server_time()

    assert server_time == _NOW
    assert server_time.tzinfo == timezone.utc
    assert len(session.calls) == 1
    assert cfg.TIME_PATH in session.calls[0][0]


def test_fetch_server_time_malformed(base_config: cfg.Config) -> None:
    session = _FakeSession([
        (cfg.TIME_PATH, {"invalid": 123}),
    ])
    fetcher = DataFetcher(base_config, session=session, now=lambda: _NOW)
    with pytest.raises(NetworkError, match="server time endpoint returned no serverTime field"):
        fetcher.fetch_server_time()


def test_validate_symbol_fapi_matches_in_symbols_list(base_config: cfg.Config) -> None:
    """Binance Futures returns all symbols in /fapi/v1/exchangeInfo."""
    symbols_payload = {
        "symbols": [
            {"symbol": "1000BONKUSDT", "status": "TRADING", "filters": []},
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                ],
            },
            {"symbol": "ETHUSDT", "status": "TRADING", "filters": []},
        ]
    }
    session = _FakeSession([
        (cfg.EXCHANGE_INFO_PATH, symbols_payload),
    ])
    fetcher = DataFetcher(base_config, session=session, now=lambda: _NOW)
    entry = fetcher.validate_symbol()

    assert entry["symbol"] == "BTCUSDT"
    assert entry["status"] == "TRADING"
    price_filter = next(f for f in entry["filters"] if f["filterType"] == "PRICE_FILTER")
    assert price_filter["tickSize"] == "0.10"


def test_validate_symbol_fapi_symbol_not_found(base_config: cfg.Config) -> None:
    symbols_payload = {
        "symbols": [
            {"symbol": "ETHUSDT", "status": "TRADING", "filters": []},
            {"symbol": "SOLUSDT", "status": "TRADING", "filters": []},
        ]
    }
    session = _FakeSession([
        (cfg.EXCHANGE_INFO_PATH, symbols_payload),
    ])
    fetcher = DataFetcher(base_config, session=session, now=lambda: _NOW)
    with pytest.raises(SymbolNotFoundError, match="Binance Futures"):
        fetcher.validate_symbol()


def test_validate_symbol_fapi_empty_symbols_list(base_config: cfg.Config) -> None:
    session = _FakeSession([
        (cfg.EXCHANGE_INFO_PATH, {"symbols": []}),
    ])
    fetcher = DataFetcher(base_config, session=session, now=lambda: _NOW)
    with pytest.raises(SymbolNotFoundError, match="Binance Futures"):
        fetcher.validate_symbol()


def test_fetch_klines_fapi_parsing_and_closure(base_config: cfg.Config) -> None:
    # 2 rows: one closed, one open
    open_ms_1 = _NOW_MS - 7200 * 1000
    close_ms_1 = open_ms_1 + 3600 * 1000 - 1  # in the past -> closed

    open_ms_2 = _NOW_MS - 1800 * 1000
    close_ms_2 = _NOW_MS + 1800 * 1000 - 1  # in the future -> not closed

    raw_klines = [
        [open_ms_1, "90000.0", "90500.0", "89800.0", "90200.0", "150.5", close_ms_1],
        [open_ms_2, "90200.0", "91000.0", "90100.0", "90800.0", "80.2", close_ms_2],
    ]

    session = _FakeSession([
        (cfg.KLINES_PATH, raw_klines),
    ])
    fetcher = DataFetcher(base_config, session=session, now=lambda: _NOW)
    candles = fetcher.fetch_klines("1h", 2)

    assert len(candles) == 2
    assert candles[0].open == Decimal("90000.0")
    assert candles[0].high == Decimal("90500.0")
    assert candles[0].low == Decimal("89800.0")
    assert candles[0].close == Decimal("90200.0")
    assert candles[0].volume == Decimal("150.5")
    assert candles[0].is_closed is True

    assert candles[1].close == Decimal("90800.0")
    assert candles[1].is_closed is False


def test_fetch_current_price_from_ticker(base_config: cfg.Config) -> None:
    session = _FakeSession([
        (cfg.TICKER_PRICE_PATH, {"symbol": "BTCUSDT", "price": "90250.50"}),
    ])
    fetcher = DataFetcher(base_config, session=session, now=lambda: _NOW)
    price = fetcher.fetch_current_price()

    assert price == Decimal("90250.50")
    assert fetcher.price_notes == ()


def test_fetch_current_price_fallback_on_error(base_config: cfg.Config) -> None:
    close_ms = _NOW_MS - 3600 * 1000
    raw_klines = [
        [_NOW_MS - 7200 * 1000, "90000.0", "90500.0", "89800.0", "90100.0", "100.0", close_ms],
    ]
    session = _FakeSession([
        (cfg.TICKER_PRICE_PATH, ({}, 500)),
        (cfg.KLINES_PATH, raw_klines),
    ])
    fetcher = DataFetcher(base_config, session=session, now=lambda: _NOW, sleep=lambda _: None)
    price = fetcher.fetch_current_price()

    assert price == Decimal("90100.0")
    assert len(fetcher.price_notes) == 1
    assert "Ticker price rejected" in fetcher.price_notes[0]


def test_fatal_client_400_raises_symbol_not_found(base_config: cfg.Config) -> None:
    session = _FakeSession([
        (cfg.KLINES_PATH, ({"code": -1121, "msg": "Invalid symbol."}, 400)),
    ])
    fetcher = DataFetcher(base_config, session=session, now=lambda: _NOW)
    with pytest.raises(SymbolNotFoundError, match="Binance Futures"):
        fetcher.fetch_klines("1h", 10)
