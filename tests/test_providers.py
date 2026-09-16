"""Provider-layer tests: mapping, volume gating, parsing (network-free).

The suite drives the Twelve Data and OANDA sources through a fake HTTP session,
so no live credential or network access is required. Binance behaviour is
covered by the golden render test, which also proves the provider change left
its output byte-identical.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from smc_prompt import cli
from smc_prompt import config as cfg
from smc_prompt.errors import ConfigError
from smc_prompt.oanda_source import OandaSource
from smc_prompt.provider_base import (
    aggregate_candles,
    iso_to_utc,
    price_sanity_warning,
    price_within_band,
)
from smc_prompt.structure_analyzer import compute_relative_volume
from smc_prompt.twelvedata_source import TwelveDataSource

from .conftest import HTF_CSV, LTF_CSV, MTF_CSV, make_candle

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fake transport
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload


class _FakeSession:
    """Routes a request to the first payload whose path substring matches.

    Routes are ordered, so a more specific path must be registered before a
    generic prefix (``/v3/accounts/1/pricing`` before ``/v3/accounts``).
    """

    def __init__(self, routes: list[tuple[str, object]]) -> None:
        self._routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        for fragment, payload in self._routes:
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResponse(payload)
        raise AssertionError(f"no fake route matched {url}")


def _twelvedata_routes(rows: list[dict], *, quote: object = None) -> list[tuple[str, object]]:
    quote_payload = (
        quote if quote is not None else {"symbol": "XAU/USD", "close": "2405.50"}
    )
    return [
        ("/time_series", {"values": rows}),
        ("/quote", quote_payload),
    ]


def _oanda_routes(rows: list[dict], *, precision: int = 3) -> list[tuple[str, object]]:
    # Order matters: the candle URL also contains "/instruments", so the
    # specific fragments must precede the generic ones.
    return [
        ("/candles", {"candles": rows}),
        (
            "/pricing",
            {"prices": [{"bids": [{"price": "2404.00"}], "asks": [{"price": "2406.00"}]}]},
        ),
        (
            "/instruments",
            {"instruments": [{"name": "XAU_USD", "displayPrecision": precision}]},
        ),
        ("/v3/accounts", {"accounts": [{"id": "101-001-1234567-001"}]}),
    ]


def _td_row(stamp: str, close: str = "2405.50") -> dict:
    return {
        "datetime": stamp,
        "open": "2400.10",
        "high": "2410.00",
        "low": "2395.00",
        "close": close,
        "volume": "0",
    }


def _oanda_row(stamp: str, *, complete: bool = True) -> dict:
    return {
        "time": stamp,
        "volume": 4321,
        "complete": complete,
        "mid": {"o": "2400.100", "h": "2410.000", "l": "2395.000", "c": "2405.500"},
    }


@pytest.fixture
def xau_config() -> cfg.Config:
    return cfg.build_config(
        "XAUUSD", provider=cfg.PROVIDER_TWELVEDATA, htf_interval="1d"
    )


# --------------------------------------------------------------------------
# Symbol / interval mapping
# --------------------------------------------------------------------------


def test_provider_symbol_metals_use_explicit_table() -> None:
    assert cfg.provider_symbol(cfg.PROVIDER_TWELVEDATA, "xauusd") == "XAU/USD"
    assert cfg.provider_symbol(cfg.PROVIDER_OANDA, "XAUUSD") == "XAU_USD"
    assert cfg.provider_symbol(cfg.PROVIDER_BINANCE, "xauusd") == "XAUUSD"


def test_provider_symbol_currency_pair_heuristic() -> None:
    assert cfg.provider_symbol(cfg.PROVIDER_TWELVEDATA, "EURUSD") == "EUR/USD"
    assert cfg.provider_symbol(cfg.PROVIDER_OANDA, "USDJPY") == "USD_JPY"


def test_provider_symbol_non_fx_passes_through() -> None:
    assert cfg.provider_symbol(cfg.PROVIDER_OANDA, "WTICO_USD") == "WTICO_USD"


def test_provider_interval_maps_native_4h() -> None:
    # Both FX providers serve 4h natively, so the default MTF tier needs no
    # aggregation.
    assert cfg.provider_interval(cfg.PROVIDER_TWELVEDATA, "4h") == "4h"
    assert cfg.provider_interval(cfg.PROVIDER_OANDA, "4h") == "H4"
    assert cfg.provider_interval(cfg.PROVIDER_TWELVEDATA, "1d") == "1day"
    assert cfg.provider_interval(cfg.PROVIDER_OANDA, "1d") == "D"


def test_provider_interval_unsupported_raises_config_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        cfg.provider_interval(cfg.PROVIDER_OANDA, "12h")
    assert "--provider" not in str(excinfo.value)  # message names the provider
    assert "OANDA" in str(excinfo.value)
    assert "12h" in str(excinfo.value)


def test_build_config_rejects_unknown_provider() -> None:
    with pytest.raises(ConfigError):
        cfg.build_config("XAUUSD", provider="metatrader")


def test_build_config_sets_volume_availability_per_provider() -> None:
    assert cfg.build_config("BTCUSDT").volume_available is True
    for provider in (cfg.PROVIDER_TWELVEDATA, cfg.PROVIDER_OANDA):
        config = cfg.build_config("XAUUSD", provider=provider)
        assert config.volume_available is False
        assert config.provider_label in {"Twelve Data", "OANDA"}


# --------------------------------------------------------------------------
# Volume gating
# --------------------------------------------------------------------------


def test_relative_volume_disabled_returns_none() -> None:
    candles = [make_candle(i, high=10 + i, low=9 + i, volume="5") for i in range(5)]
    assert compute_relative_volume(candles, enabled=False) is None
    assert compute_relative_volume(candles, enabled=True) is not None


# --------------------------------------------------------------------------
# Timestamp / aggregation helpers
# --------------------------------------------------------------------------


def test_iso_to_utc_truncates_nanoseconds() -> None:
    parsed = iso_to_utc("2026-07-15T00:00:00.123456789Z")
    assert parsed == datetime(
        2026, 7, 15, 0, 0, 0, 123456, tzinfo=timezone.utc
    )


def test_iso_to_utc_accepts_space_separator_and_z() -> None:
    assert iso_to_utc("2026-07-15 01:30:00") == datetime(
        2026, 7, 15, 1, 30, tzinfo=timezone.utc
    )


def test_aggregate_candles_rolls_two_into_one() -> None:
    candles = [
        make_candle(0, high=10, low=8, open_="9", close="9.5", volume="2"),
        make_candle(1, high=12, low=9, open_="9.5", close="11", volume="3"),
    ]
    rolled = aggregate_candles(candles, 2)
    assert len(rolled) == 1
    assert rolled[0].open == Decimal("9")
    assert rolled[0].high == Decimal("12")
    assert rolled[0].low == Decimal("8")
    assert rolled[0].close == Decimal("11")
    assert rolled[0].volume == Decimal("5")


def test_aggregate_candles_drops_leading_partial_bucket() -> None:
    candles = [make_candle(i, high=10, low=8) for i in range(5)]
    rolled = aggregate_candles(candles, 2)
    # 5 candles -> 1 dropped lead + 2 aligned buckets.
    assert len(rolled) == 2
    assert rolled[0].open_time == candles[1].open_time


def test_aggregate_candles_is_closed_requires_all_constituents() -> None:
    candles = [
        make_candle(0, high=10, low=8, is_closed=True),
        make_candle(1, high=11, low=9, is_closed=False),
    ]
    assert aggregate_candles(candles, 2)[0].is_closed is False


def test_price_band_helpers_are_shared_with_binance() -> None:
    candle = make_candle(0, high=100, low=90)
    assert price_within_band(Decimal("95"), candle, Decimal("1")) is True
    assert price_within_band(Decimal("500"), candle, Decimal("1")) is False
    # No reference/tolerance degrades to "always plausible".
    assert price_within_band(Decimal("500"), None, None) is True


def test_price_sanity_warning_omits_binance_wording() -> None:
    candle = make_candle(0, high=100, low=90)
    message = price_sanity_warning(
        Decimal("500"), candle, Decimal("1"), symbol="XAUUSD"
    )
    assert message is not None
    assert "possible stale XAUUSD ticker data" in message
    assert "Binance" not in message


# --------------------------------------------------------------------------
# Twelve Data
# --------------------------------------------------------------------------


def test_twelvedata_fetch_klines_orders_chronologically(
    xau_config: cfg.Config,
) -> None:
    # The API is documented newest-first; the source must normalize that.
    rows = [
        _td_row("2026-07-17 00:00:00", close="2402"),
        _td_row("2026-07-16 00:00:00", close="2401"),
        _td_row("2026-07-15 00:00:00", close="2400"),
    ]
    source = TwelveDataSource(
        xau_config,
        api_key="test-key",
        session=_FakeSession(_twelvedata_routes(rows)),
        now=lambda: _NOW,
    )
    candles = source.fetch_klines("1d", 3)
    assert [c.close for c in candles] == [
        Decimal("2400"),
        Decimal("2401"),
        Decimal("2402"),
    ]
    assert candles[0].open_time == datetime(2026, 7, 15, tzinfo=timezone.utc)


def test_twelvedata_derives_close_time_and_closure(xau_config: cfg.Config) -> None:
    rows = [_td_row("2026-07-15 00:00:00")]
    source = TwelveDataSource(
        xau_config,
        api_key="test-key",
        session=_FakeSession(_twelvedata_routes(rows)),
        now=lambda: _NOW,
    )
    candle = source.fetch_klines("1d", 1)[0]
    # 1d interval -> close_time is the next midnight UTC.
    assert candle.close_time == datetime(2026, 7, 16, tzinfo=timezone.utc)
    assert candle.is_closed is True


def test_twelvedata_validate_symbol_yields_two_dp_tick(
    xau_config: cfg.Config,
) -> None:
    source = TwelveDataSource(
        xau_config,
        api_key="test-key",
        session=_FakeSession(_twelvedata_routes([])),
        now=lambda: _NOW,
    )
    entry = source.validate_symbol()
    tick = cli._extract_tick_size(entry)
    assert tick is not None
    assert cfg.decimals_from_tick_size(tick) == 2
    assert entry["status"] == "TRADING"


def test_twelvedata_current_price_uses_quote_mid(xau_config: cfg.Config) -> None:
    source = TwelveDataSource(
        xau_config,
        api_key="test-key",
        session=_FakeSession(_twelvedata_routes([], quote={"close": "2412.34"})),
        now=lambda: _NOW,
    )
    assert source.fetch_current_price() == Decimal("2412.34")


def test_twelvedata_requires_api_key(xau_config: cfg.Config) -> None:
    with pytest.raises(ConfigError):
        TwelveDataSource(xau_config, api_key="")


def test_twelvedata_error_envelope_maps_to_symbol_not_found(
    xau_config: cfg.Config,
) -> None:
    routes = [
        ("/time_series", {"status": "error", "code": 404, "message": "not found"}),
    ]
    source = TwelveDataSource(
        xau_config,
        api_key="test-key",
        session=_FakeSession(routes),
        now=lambda: _NOW,
    )
    from smc_prompt.errors import SymbolNotFoundError

    with pytest.raises(SymbolNotFoundError):
        source.fetch_klines("1d", 10)


# --------------------------------------------------------------------------
# OANDA
# --------------------------------------------------------------------------


def _oanda_source(config: cfg.Config, rows: list[dict], *, precision: int = 3):
    return OandaSource(
        config,
        token="test-token",
        session=_FakeSession(_oanda_routes(rows, precision=precision)),
        now=lambda: _NOW,
    )


def test_oanda_fetch_klines_parses_mid_block_and_complete_flag() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    rows = [
        _oanda_row("2026-07-15T00:00:00.000000000Z", complete=True),
        _oanda_row("2026-07-16T00:00:00.000000000Z", complete=False),
    ]
    candles = _oanda_source(config, rows).fetch_klines("1d", 2)

    assert candles[0].open == Decimal("2400.100")
    assert candles[0].high == Decimal("2410.000")
    assert candles[0].close == Decimal("2405.500")
    # 9-digit nanoseconds parsed via truncation.
    assert candles[0].open_time == datetime(2026, 7, 15, tzinfo=timezone.utc)
    # ``complete`` is authoritative over the host clock.
    assert candles[0].is_closed is True
    assert candles[1].is_closed is False


def test_oanda_volume_is_zeroed_not_mislabelled() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    rows = [_oanda_row("2026-07-15T00:00:00.000000000Z")]
    candle = _oanda_source(config, rows).fetch_klines("1d", 1)[0]
    # OANDA reports a tick count; emitting it as "volume" would mislabel it as
    # [FAKTA], so the provider zeroes it.
    assert candle.volume == Decimal("0")
    assert config.volume_available is False


def test_oanda_validate_symbol_uses_venue_display_precision() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    rows = [_oanda_row("2026-07-15T00:00:00.000000000Z")]
    entry = _oanda_source(config, rows, precision=3).validate_symbol()
    tick = cli._extract_tick_size(entry)
    assert tick is not None
    assert cfg.decimals_from_tick_size(tick) == 3


def test_oanda_current_price_is_bid_ask_midpoint() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    rows = [_oanda_row("2026-07-15T00:00:00.000000000Z")]
    price = _oanda_source(config, rows).fetch_current_price()
    # (2404.00 + 2406.00) / 2
    assert price == Decimal("2405")


def test_oanda_requires_token() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    with pytest.raises(ConfigError):
        OandaSource(config, token="")


def test_oanda_rejects_unknown_environment() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    with pytest.raises(ConfigError):
        OandaSource(config, token="t", environment="staging")


# --------------------------------------------------------------------------
# CLI integration
# --------------------------------------------------------------------------


def test_cli_requires_key_for_twelvedata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(cli.TWELVEDATA_KEY_ENV, raising=False)
    with pytest.raises(ConfigError):
        cli.run(
            "XAUUSD",
            htf_candles=60,
            mtf_candles=120,
            ltf_candles=100,
            swing_lookback=5,
            distance_reference="nearest",
            include_atr=True,
            output_dir=str(tmp_path),
            provider=cfg.PROVIDER_TWELVEDATA,
            twelvedata_key=None,
        )


def test_cli_reads_key_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env fallback supplies the credential when no flag is passed."""

    monkeypatch.setenv(cli.TWELVEDATA_KEY_ENV, "env-key")
    monkeypatch.delenv(cli.OANDA_TOKEN_ENV, raising=False)
    key, token, account = cli._resolve_credentials(None, None, None)
    assert key == "env-key"
    assert token is None and account is None

    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_TWELVEDATA)
    source = cli._make_provider_source(
        config,
        twelvedata_key=key,
        oanda_token=token,
        oanda_account_id=account,
        oanda_env=cfg.OANDA_ENV_PRACTICE,
    )
    assert isinstance(source, TwelveDataSource)
    assert source._api_key == "env-key"


def test_offline_run_names_the_selected_provider(tmp_path: Path) -> None:
    """The prompt's provenance line follows ``--provider`` without new bytes.

    Offline mode keeps the run network-free while still exercising the provider
    label, so this proves the frozen template can name any provider.
    """

    result = cli.run(
        "XAUUSD",
        htf_candles=60,
        mtf_candles=120,
        ltf_candles=100,
        swing_lookback=5,
        distance_reference="nearest",
        include_atr=True,
        output_dir=str(tmp_path),
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        input_csv=str(HTF_CSV),
        htf_file=str(HTF_CSV),
        mtf_file=str(MTF_CSV),
        ltf_file=str(LTF_CSV),
        provider=cfg.PROVIDER_TWELVEDATA,
    )

    assert "live market data API (Twelve Data)" in result.prompt
    assert "Binance" not in result.prompt
    # Volume facts are suppressed for a volume-less provider.
    assert "spike: unknown" in result.prompt
    assert "{{" not in result.prompt and "}}" not in result.prompt


def test_offline_run_hides_delisted_warning_when_volume_unavailable(
    tmp_path: Path,
) -> None:
    result = cli.run(
        "XAUUSD",
        htf_candles=60,
        mtf_candles=120,
        ltf_candles=100,
        swing_lookback=5,
        distance_reference="nearest",
        include_atr=True,
        output_dir=str(tmp_path),
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        input_csv=str(HTF_CSV),
        htf_file=str(HTF_CSV),
        mtf_file=str(MTF_CSV),
        ltf_file=str(LTF_CSV),
        provider=cfg.PROVIDER_OANDA,
    )
    assert not any("delisted" in warning for warning in result.warnings)
