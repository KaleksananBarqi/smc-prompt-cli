"""Unit tests for Crypto Provider Failover Chain."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from smc_prompt import cli, config as cfg
from smc_prompt.csv_source import load_csv_series
from smc_prompt.errors import ConfigError, NetworkError, SymbolNotFoundError
from smc_prompt.models import Candle
from tests.conftest import HTF_CSV, LTF_CSV, MTF_CSV


class _DummySuccessfulFetcher:
    def __init__(self, cfg_obj: cfg.Config) -> None:
        self._cfg = cfg_obj
        self.price_notes = ()
        self._htf_candles = load_csv_series(str(HTF_CSV), interval="1d").candles
        self._mtf_candles = load_csv_series(str(MTF_CSV), interval="4h").candles
        self._ltf_candles = load_csv_series(str(LTF_CSV), interval="1h").candles

    def validate_symbol(self) -> dict:
        return {
            "symbol": self._cfg.symbol,
            "status": "TRADING",
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1"}],
        }

    def fetch_server_time(self) -> datetime:
        return datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    def with_now(self, now_time: datetime) -> "_DummySuccessfulFetcher":
        return self

    def fetch_klines(self, interval: str, limit: int) -> list[Candle]:
        if interval == self._cfg.htf_interval:
            return self._htf_candles[-limit:]
        if interval == self._cfg.mtf_interval:
            return self._mtf_candles[-limit:]
        return self._ltf_candles[-limit:]

    def fetch_current_price(self, *args, **kwargs) -> Decimal:
        return Decimal("80500.0")


class _NetworkFailingFetcher:
    def __init__(self, cfg_obj: cfg.Config) -> None:
        pass

    def validate_symbol(self) -> dict:
        raise NetworkError("connection timed out")


class _SymbolNotFoundFetcher:
    def __init__(self, cfg_obj: cfg.Config) -> None:
        pass

    def validate_symbol(self) -> dict:
        raise SymbolNotFoundError("symbol not found on exchange")


def test_failover_binance_to_bitunix(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Binance failing with NetworkError should failover to Bitunix."""
    attempts = []

    def mock_build_fetcher(provider_name, active_cfg, **kwargs):
        attempts.append(provider_name)
        if provider_name == cfg.PROVIDER_BINANCE:
            return _NetworkFailingFetcher(active_cfg)
        if provider_name == cfg.PROVIDER_BITUNIX:
            return _DummySuccessfulFetcher(active_cfg)
        raise AssertionError(f"Unexpected provider {provider_name}")

    monkeypatch.setattr("smc_prompt.cli._build_fetcher", mock_build_fetcher)

    result = cli.run(
        "BTCUSDT",
        output_dir=str(tmp_path),
        htf_candles=10,
        mtf_candles=10,
        ltf_candles=10,
    )

    assert attempts == ["binance", "bitunix"]
    assert "(Bitunix)" in result.prompt
    assert any("Koneksi jaringan ke Binance gagal" in w for w in result.warnings)


def test_failover_binance_and_bitunix_to_mexc(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Binance and Bitunix failing should failover to MEXC."""
    attempts = []

    def mock_build_fetcher(provider_name, active_cfg, **kwargs):
        attempts.append(provider_name)
        if provider_name in (cfg.PROVIDER_BINANCE, cfg.PROVIDER_BITUNIX):
            return _NetworkFailingFetcher(active_cfg)
        if provider_name == cfg.PROVIDER_MEXC:
            return _DummySuccessfulFetcher(active_cfg)
        raise AssertionError(f"Unexpected provider {provider_name}")

    monkeypatch.setattr("smc_prompt.cli._build_fetcher", mock_build_fetcher)

    result = cli.run(
        "BTCUSDT",
        output_dir=str(tmp_path),
        htf_candles=10,
        mtf_candles=10,
        ltf_candles=10,
    )

    assert attempts == ["binance", "bitunix", "mexc"]
    assert "(MEXC)" in result.prompt


def test_failover_chain_exhausted(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """When all providers in chain fail, NetworkError is raised."""
    attempts = []

    def mock_build_fetcher(provider_name, active_cfg, **kwargs):
        attempts.append(provider_name)
        return _NetworkFailingFetcher(active_cfg)

    monkeypatch.setattr("smc_prompt.cli._build_fetcher", mock_build_fetcher)

    with pytest.raises(NetworkError, match="connection timed out"):
        cli.run(
            "BTCUSDT",
            output_dir=str(tmp_path),
            htf_candles=10,
            mtf_candles=10,
            ltf_candles=10,
        )

    assert attempts == ["binance", "bitunix", "mexc", "gate"]


def test_no_fallback_flag_disables_failover(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """With no_fallback=True, failure on Binance raises immediately."""
    attempts = []

    def mock_build_fetcher(provider_name, active_cfg, **kwargs):
        attempts.append(provider_name)
        return _NetworkFailingFetcher(active_cfg)

    monkeypatch.setattr("smc_prompt.cli._build_fetcher", mock_build_fetcher)

    with pytest.raises(NetworkError, match="connection timed out"):
        cli.run(
            "BTCUSDT",
            output_dir=str(tmp_path),
            htf_candles=10,
            mtf_candles=10,
            ltf_candles=10,
            no_fallback=True,
        )

    assert attempts == ["binance"]


def test_symbol_not_found_does_not_failover(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """SymbolNotFoundError must abort immediately without trying next provider."""
    attempts = []

    def mock_build_fetcher(provider_name, active_cfg, **kwargs):
        attempts.append(provider_name)
        return _SymbolNotFoundFetcher(active_cfg)

    monkeypatch.setattr("smc_prompt.cli._build_fetcher", mock_build_fetcher)

    with pytest.raises(SymbolNotFoundError, match="symbol not found"):
        cli.run(
            "BTCUSDT",
            output_dir=str(tmp_path),
            htf_candles=10,
            mtf_candles=10,
            ltf_candles=10,
        )

    assert attempts == ["binance"]


def test_explicit_provider_fails_down_chain(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Selecting --provider bitunix should failover to mexc and gate."""
    attempts = []

    def mock_build_fetcher(provider_name, active_cfg, **kwargs):
        attempts.append(provider_name)
        if provider_name == cfg.PROVIDER_BITUNIX:
            return _NetworkFailingFetcher(active_cfg)
        if provider_name == cfg.PROVIDER_MEXC:
            return _DummySuccessfulFetcher(active_cfg)
        raise AssertionError(f"Unexpected provider {provider_name}")

    monkeypatch.setattr("smc_prompt.cli._build_fetcher", mock_build_fetcher)

    result = cli.run(
        "BTCUSDT",
        provider=cfg.PROVIDER_BITUNIX,
        output_dir=str(tmp_path),
        htf_candles=10,
        mtf_candles=10,
        ltf_candles=10,
    )

    assert attempts == ["bitunix", "mexc"]
    assert "(MEXC)" in result.prompt


def test_failover_skips_incompatible_interval_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When a candidate in failover chain does not support the interval (e.g. 2h on MEXC),
    it should be skipped with a warning and failover should continue to Gate.io."""
    attempts = []

    def mock_build_fetcher(provider_name, active_cfg, **kwargs):
        attempts.append(provider_name)
        if provider_name in (cfg.PROVIDER_BINANCE, cfg.PROVIDER_BITUNIX):
            return _NetworkFailingFetcher(active_cfg)
        if provider_name == cfg.PROVIDER_GATE:
            return _DummySuccessfulFetcher(active_cfg)
        raise AssertionError(f"Unexpected provider {provider_name}")

    monkeypatch.setattr("smc_prompt.cli._build_fetcher", mock_build_fetcher)

    result = cli.run(
        "BTCUSDT",
        mtf_interval="2h",
        output_dir=str(tmp_path),
        htf_candles=10,
        mtf_candles=10,
        ltf_candles=10,
    )

    assert attempts == ["binance", "bitunix", "gate"]
    assert "(Gate.io)" in result.prompt
    assert any("MEXC tidak mendukung interval '2h'" in w for w in result.warnings)


def test_initial_unsupported_interval_fails_fast(tmp_path) -> None:
    """When user explicitly specifies a provider that does not support the interval,
    it should fail immediately with ConfigError without attempting failover."""
    with pytest.raises(ConfigError, match="does not support the interval '2h'"):
        cli.run(
            "BTCUSDT",
            provider=cfg.PROVIDER_MEXC,
            mtf_interval="2h",
            output_dir=str(tmp_path),
        )


def test_mexc_code_500_triggers_failover_to_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """MEXC returning 500 NetworkError should failover to Gate."""
    attempts = []

    def mock_build_fetcher(provider_name, active_cfg, **kwargs):
        attempts.append(provider_name)
        if provider_name in (cfg.PROVIDER_BINANCE, cfg.PROVIDER_BITUNIX, cfg.PROVIDER_MEXC):
            return _NetworkFailingFetcher(active_cfg)
        if provider_name == cfg.PROVIDER_GATE:
            return _DummySuccessfulFetcher(active_cfg)
        raise AssertionError(f"Unexpected provider {provider_name}")

    monkeypatch.setattr("smc_prompt.cli._build_fetcher", mock_build_fetcher)

    result = cli.run(
        "BTCUSDT",
        output_dir=str(tmp_path),
        htf_candles=10,
        mtf_candles=10,
        ltf_candles=10,
    )

    assert attempts == ["binance", "bitunix", "mexc", "gate"]
    assert "(Gate.io)" in result.prompt
