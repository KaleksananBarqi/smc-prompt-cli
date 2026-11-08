"""Unit tests untuk modul akuisisi data pasar & rantai failover (market_acquisition.py)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from smc_prompt import config as cfg
from smc_prompt.csv_source import load_csv_series
from smc_prompt.errors import ConfigError, NetworkError, SymbolNotFoundError
from smc_prompt.market_acquisition import (
    MarketSnapshot,
    acquire_market_snapshot,
    extract_tick_size,
    resolve_mode_intervals,
)
from smc_prompt.models import Candle
from tests.conftest import HTF_CSV, LTF_CSV, MTF_CSV, make_candle


class _FakeAdapterSource:
    """Adapter sumber data tiruan yang diinjeksi melalui seam source_factory."""

    def __init__(
        self,
        active_cfg: cfg.Config,
        *,
        status: str = "TRADING",
        tick_size: str = "0.01",
        server_time_exc: Exception | None = None,
        validate_exc: Exception | None = None,
        single_candles: list[Candle] | None = None,
        deriv_info: dict | None = None,
        btc_info: dict | None = None,
    ) -> None:
        self._cfg = active_cfg
        self._status = status
        self._tick_size = tick_size
        self._server_time_exc = server_time_exc
        self._validate_exc = validate_exc
        self._single_candles = single_candles
        self._deriv_info = deriv_info
        self._btc_info = btc_info
        self.price_notes = ()
        self.bound_now: datetime | None = None
        self._htf = load_csv_series(str(HTF_CSV), interval="1d").candles
        self._mtf = load_csv_series(str(MTF_CSV), interval="4h").candles
        self._ltf = load_csv_series(str(LTF_CSV), interval="1h").candles

    def validate_symbol(self) -> dict:
        if self._validate_exc is not None:
            raise self._validate_exc
        return {
            "symbol": self._cfg.symbol,
            "status": self._status,
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": self._tick_size}],
        }

    def fetch_server_time(self) -> datetime:
        if self._server_time_exc is not None:
            raise self._server_time_exc
        return datetime(2026, 11, 8, 7, 0, tzinfo=timezone.utc)

    def with_now(self, moment: datetime) -> "_FakeAdapterSource":
        self.bound_now = moment
        return self

    def fetch_klines(self, interval: str, limit: int) -> list[Candle]:
        if self._single_candles is not None:
            return self._single_candles[-limit:]
        if interval == self._cfg.htf_interval:
            return self._htf[-limit:]
        if interval == self._cfg.mtf_interval:
            return self._mtf[-limit:]
        return self._ltf[-limit:]

    def fetch_current_price(self, *args, **kwargs) -> Decimal:
        return Decimal("3450.25")

    def fetch_derivatives_info(self) -> dict:
        if self._deriv_info is not None:
            return self._deriv_info
        return {
            "source_label": "Binance",
            "funding_rate": "0.00010000",
            "open_interest": "100000",
            "oi_delta_4h": "",
        }

    def fetch_btc_benchmark_info(self) -> dict:
        if self._btc_info is not None:
            return self._btc_info
        return {
            "source_label": "Binance",
            "btc_price": "62800.00",
            "htf_label": "Daily",
            "htf_structure": "Bullish",
            "htf_recent_high": "63450.00",
            "htf_high_status": "untested",
            "htf_recent_low": "60100.00",
            "htf_low_status": "untested",
            "mtf_label": "4H",
            "mtf_structure": "Bullish",
        }


def test_resolve_mode_intervals_preserves_or_adjusts_trio() -> None:
    """Memastikan resolve_mode_intervals menjaga keunikan 3 timeframe saat target di luar trio."""
    # Target sudah ada di dalam trio -> tidak berubah
    assert resolve_mode_intervals("1d", "4h", "1h", target_interval="4h") == (
        "1d",
        "4h",
        "1h",
    )
    # Target baru di luar trio -> ltf diganti ke target
    assert resolve_mode_intervals("1d", "4h", "1h", target_interval="15m") == (
        "1d",
        "4h",
        "15m",
    )
    # Jika target bertabrakan dengan mtf -> mtf digeser otomatis agar tidak duplikat
    htf, mtf, ltf = resolve_mode_intervals("1d", "4h", "1h", target_interval="4h")
    assert len({htf, mtf, ltf}) == 3


def test_extract_tick_size_handles_valid_and_missing_filters() -> None:
    """Memastikan extract_tick_size mengambil tickSize dari PRICE_FILTER dengan aman."""
    assert (
        extract_tick_size(
            {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"}]}
        )
        == "0.001"
    )
    assert extract_tick_size({"filters": []}) is None
    assert extract_tick_size({}) is None


def test_acquire_market_snapshot_injectable_seam_failover_and_enrichment() -> None:
    """Menguji rantai failover, resolusi tickSize, dan pengayaan konteks via source_factory tanpa monkeypatch."""
    config = cfg.build_config("ETHUSDT", provider=cfg.PROVIDER_BINANCE)
    attempts: list[str] = []
    emitted_warns: list[str] = []

    def injected_factory(provider_name: str, active_cfg: cfg.Config, **kwargs):
        attempts.append(provider_name)
        if provider_name == cfg.PROVIDER_BINANCE:
            return _FakeAdapterSource(
                active_cfg,
                validate_exc=NetworkError("Binance timeout"),
            )
        return _FakeAdapterSource(
            active_cfg,
            tick_size="0.01",
            deriv_info={
                "source_label": "Bitunix",
                "funding_rate": "0.00010000",
                "open_interest": "950000",
                "oi_delta_4h": "",
            },
            btc_info={
                "source_label": "Bitunix",
                "btc_price": "62800.00",
                "htf_label": "Daily",
                "htf_structure": "Bullish",
                "htf_recent_high": "63450.00",
                "htf_high_status": "untested",
                "htf_recent_low": "60100.00",
                "htf_low_status": "untested",
                "mtf_label": "4H",
                "mtf_structure": "Bullish",
            },
        )

    snapshot = acquire_market_snapshot(
        config,
        mode="prompt",
        source_factory=injected_factory,
        warn_callback=emitted_warns.append,
    )

    assert isinstance(snapshot, MarketSnapshot)
    assert attempts == ["binance", "bitunix"]
    assert snapshot.active_config.provider == cfg.PROVIDER_BITUNIX
    assert snapshot.active_config.price_format.tick_decimals == 2
    assert any("Berhasil beralih otomatis ke Bitunix" in w for w in snapshot.warnings)
    assert any("Mengalihkan otomatis ke Bitunix" in w for w in emitted_warns)
    assert "Konteks Derivatif (Bitunix)" in snapshot.derivatives_context_line
    assert "Benchmark BTCUSDT (Bitunix)" in snapshot.btc_benchmark_line


def test_acquire_market_snapshot_server_time_fallback_and_single_tf_slicing() -> None:
    """Menguji fallback jam server saat fetch_server_time gagal serta pemotongan closed candles."""
    config = cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_BINANCE)
    emitted_warns: list[str] = []

    # Gunakan 30 closed candle dari LTF_CSV (memiliki variasi swing) + 1 unclosed candle di ujung
    csv_closed = load_csv_series(str(LTF_CSV), interval="1h").candles[:30]
    candles = list(csv_closed) + [
        make_candle(30, open_="102", high="106", low="101", close="104", is_closed=False)
    ]

    def injected_factory(provider_name: str, active_cfg: cfg.Config, **kwargs):
        return _FakeAdapterSource(
            active_cfg,
            status="BREAK",
            server_time_exc=NetworkError("clock endpoint unreachable"),
            single_candles=candles,
        )

    snapshot = acquire_market_snapshot(
        config,
        mode="validate_setup",
        single_interval="15m",
        single_candles=35,  # minta 35 tapi hanya 30 closed yang tersedia
        source_factory=injected_factory,
        warn_callback=emitted_warns.append,
    )

    assert snapshot.server_time is None
    assert len(snapshot.closed_candles) == 30
    assert len(snapshot.selected_candles) == 30
    assert snapshot.structure_block is not None
    assert "### Konteks Struktur Pasar (15m)" in snapshot.structure_block
    assert any("server time unavailable" in w for w in emitted_warns)
    assert any("Reduced table to 30" in w for w in snapshot.warnings)
    assert any("BREAK" in w for w in snapshot.warnings)


def test_acquire_market_snapshot_raises_when_no_closed_candles() -> None:
    """Memastikan ConfigError dilempar jika tidak ada candle yang sudah closed."""
    config = cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_BINANCE)
    unclosed_only = [
        make_candle(0, open_="100", high="105", low="95", close="102", is_closed=False)
    ]

    with pytest.raises(ConfigError, match="No closed candles available"):
        acquire_market_snapshot(
            config,
            mode="candles_only",
            single_interval="1h",
            single_candles=10,
            source_factory=lambda p, c, **kw: _FakeAdapterSource(
                c, single_candles=unclosed_only
            ),
        )


def test_acquire_market_snapshot_symbol_not_found_does_not_failover() -> None:
    """Memastikan SymbolNotFoundError langsung dihentikan tanpa mencoba provider berikutnya."""
    config = cfg.build_config("INVALIDUSDT", provider=cfg.PROVIDER_BINANCE)
    attempts: list[str] = []

    def injected_factory(provider_name: str, active_cfg: cfg.Config, **kwargs):
        attempts.append(provider_name)
        return _FakeAdapterSource(
            active_cfg,
            validate_exc=SymbolNotFoundError("not listed"),
        )

    with pytest.raises(SymbolNotFoundError, match="not listed"):
        acquire_market_snapshot(
            config,
            mode="prompt",
            source_factory=injected_factory,
        )

    assert attempts == ["binance"]
