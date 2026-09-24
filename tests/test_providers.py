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
from smc_prompt.errors import ConfigError, NetworkError
from smc_prompt.oanda_source import OandaSource
from smc_prompt.provider_base import (
    aggregate_candles,
    iso_to_utc,
    price_sanity_warning,
    price_within_band,
)
from smc_prompt.structure_analyzer import compute_relative_volume
from smc_prompt.twelvedata_source import TwelveDataSource
from smc_prompt.models import Candle
from smc_prompt.errors import NetworkError, SymbolNotFoundError

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


def test_oanda_missing_time_key_raises_network_error() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)

    row = _oanda_row("2026-07-15T00:00:00.000000000Z")
    del row["time"]

    with pytest.raises(NetworkError, match="returned a candle without a time"):
        _oanda_source(config, [row]).fetch_klines("1d", 1)


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

def test_oanda_init_kwargs() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    sleep_called = False
    rand_called = False
    def my_sleep(s): nonlocal sleep_called; sleep_called = True
    def my_rand(): nonlocal rand_called; rand_called = True; return 0.5

    source = OandaSource(config, token="test", sleep=my_sleep, rand=my_rand)
    # The HTTP client should use the provided functions
    source._http._sleep(0.1)
    source._http._rand()
    assert sleep_called
    assert rand_called

def test_oanda_price_notes() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    source = _oanda_source(config, [])
    assert source.price_notes == ()
    source._price_notes.append("test note")
    assert source.price_notes == ("test note",)

def test_oanda_with_now() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    source = _oanda_source(config, [])
    source._display_precision = 4
    moment = datetime(2027, 1, 1, tzinfo=timezone.utc)
    clone = source.with_now(moment)
    assert clone._now() == moment
    assert clone._display_precision == 4

def test_oanda_server_time() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    source = OandaSource(config, token="test")
    # Returns datetime.now(timezone.utc)
    t = source.fetch_server_time()
    assert t.tzinfo == timezone.utc

def test_oanda_resolve_account_id_errors() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)

    # NetworkError
    source1 = OandaSource(
        config, token="test",
        session=_FakeSession([("/v3/accounts", NetworkError("fail"))])
    )
    assert source1._resolve_account_id() is None

    # Missing/invalid accounts
    source2 = OandaSource(
        config, token="test",
        session=_FakeSession([("/v3/accounts", {})])
    )
    assert source2._resolve_account_id() is None

    # Invalid accounts list
    source3 = OandaSource(
        config, token="test",
        session=_FakeSession([("/v3/accounts", {"accounts": [123]})])
    )
    assert source3._resolve_account_id() is None

    # Account missing ID
    source4 = OandaSource(
        config, token="test",
        session=_FakeSession([("/v3/accounts", {"accounts": [{"no_id": True}]})])
    )
    assert source4._resolve_account_id() is None

    # Valid but cached later
    source5 = OandaSource(
        config, token="test",
        session=_FakeSession([("/v3/accounts", {"accounts": [{"id": "acc-1"}]})])
    )
    assert source5._resolve_account_id() == "acc-1"
    assert source5._resolve_account_id() == "acc-1"  # Hit cache

def test_oanda_read_display_precision_errors() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)

    # account_id is None -> None
    source1 = OandaSource(
        config, token="test",
        session=_FakeSession([("/v3/accounts", NetworkError("fail"))])
    )
    assert source1._read_display_precision() is None

    # Network error on instruments
    source2 = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/accounts/acc-1/instruments", NetworkError("fail")),
            ("/v3/accounts", {"accounts": [{"id": "acc-1"}]})
        ])
    )
    assert source2._read_display_precision() is None

    # Missing instruments
    source3 = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/accounts/acc-1/instruments", {}),
            ("/v3/accounts", {"accounts": [{"id": "acc-1"}]})
        ])
    )
    assert source3._read_display_precision() is None

    # Invalid instrument entry, bad name, invalid precision
    source4 = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/accounts/acc-1/instruments", {"instruments": [
                123,
                {"name": "OTHER"},
                {"name": "XAU_USD", "displayPrecision": "invalid"},
            ]}),
            ("/v3/accounts", {"accounts": [{"id": "acc-1"}]})
        ])
    )
    assert source4._read_display_precision() is None

    # Invalid precision type that raises ValueError/TypeError inside int()
    source5 = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/accounts/acc-1/instruments", {"instruments": [
                {"name": "XAU_USD", "displayPrecision": None},
            ]}),
            ("/v3/accounts", {"accounts": [{"id": "acc-1"}]})
        ])
    )
    assert source5._read_display_precision() is None

    # Out of bounds precision
    source6 = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/accounts/acc-1/instruments", {"instruments": [
                {"name": "XAU_USD", "displayPrecision": 99},
            ]}),
            ("/v3/accounts", {"accounts": [{"id": "acc-1"}]})
        ])
    )
    assert source6._read_display_precision() is None

    # Success, verify caching
    source7 = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/accounts/acc-1/instruments", {"instruments": [
                {"name": "XAU_USD", "displayPrecision": 4},
            ]}),
            ("/v3/accounts", {"accounts": [{"id": "acc-1"}]})
        ])
    )
    assert source7._read_display_precision() == 4
    assert source7._read_display_precision() == 4  # Hits cache


def test_oanda_candle_from_row_errors() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    source = _oanda_source(config, [])

    # Missing price data
    with pytest.raises(NetworkError, match="without price data"):
        source._candle_from_row({}, canonical_interval="1d", context="test")

    # Missing time
    with pytest.raises(NetworkError, match="without a time"):
        source._candle_from_row({"mid": {"o": "1", "h": "1", "l": "1", "c": "1"}}, canonical_interval="1d", context="test")

    # Unparseable time
    with pytest.raises(NetworkError, match="unparseable time"):
        source._candle_from_row({
            "mid": {"o": "1", "h": "1", "l": "1", "c": "1"},
            "time": "invalid_time"
        }, canonical_interval="1d", context="test")

def test_oanda_candle_from_row_fallback_closure() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    now_moment = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)
    source = OandaSource(config, token="test", now=lambda: now_moment)

    # Not using 'complete' field boolean, fall back to comparing with host clock
    row = {
        "mid": {"o": "1", "h": "1", "l": "1", "c": "1"},
        "time": "2026-07-15T00:00:00.000000000Z",
        "complete": "not_a_bool"
    }
    # For "1d", close_time is 2026-07-16 00:00:00
    # Host clock is exactly 2026-07-16 00:00:00.
    # The condition is is_closed = self._now() >= close_time + timedelta(seconds=1)
    # 2026-07-16 00:00:00 is not >= 2026-07-16 00:00:01, so is_closed is False
    candle = source._candle_from_row(row, canonical_interval="1d", context="test")
    assert candle.is_closed is False

    # Move time slightly past close_time + 1s
    source2 = OandaSource(
        config, token="test",
        now=lambda: datetime(2026, 7, 16, 0, 0, 1, tzinfo=timezone.utc)
    )
    candle2 = source2._candle_from_row(row, canonical_interval="1d", context="test")
    assert candle2.is_closed is True


def test_oanda_validate_symbol_not_found() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    source = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/instruments/XAU_USD/candles", {"candles": []})
        ])
    )
    with pytest.raises(SymbolNotFoundError, match="is not available on OANDA"):
        source.validate_symbol()

def test_oanda_fetch_klines_errors() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)

    # Unexpected payload (not a dict)
    source1 = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/instruments/XAU_USD/candles", ["not_a_dict"])
        ])
    )
    with pytest.raises(NetworkError, match="Unexpected"):
        source1.fetch_klines("1d", 10)

    # Missing/empty candles array
    source2 = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/instruments/XAU_USD/candles", {"candles": []})
        ])
    )
    with pytest.raises(NetworkError, match="returned no"):
        source2.fetch_klines("1d", 10)

def test_oanda_first_price_and_midpoint_edge_cases() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    source = _oanda_source(config, [])

    # _first_price logic
    assert source._first_price(None) is None
    assert source._first_price([123]) is None  # item not dict
    assert source._first_price([{"no_price": True}]) is None
    assert source._first_price([{"price": "123.45"}]) == Decimal("123.45")

    # _midpoint logic
    with pytest.raises(NetworkError, match="returned no usable side"):
        source._midpoint(None, None)

    assert source._midpoint(Decimal("10"), None) == Decimal("10")
    assert source._midpoint(None, Decimal("20")) == Decimal("20")
    assert source._midpoint(Decimal("10"), Decimal("20")) == Decimal("15")

def test_oanda_fetch_current_price_errors_and_fallback() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)

    # We need a fallback candles route to test fallback properly.
    def make_fallback_routes(pricing_payload, acc_id_payload=None):
        if acc_id_payload is None:
            acc_id_payload = {"accounts": [{"id": "acc-1"}]}
        return [
            ("/v3/instruments/XAU_USD/candles", {"candles": [
                _oanda_row("2026-07-15T00:00:00Z")
            ]}),
            ("/pricing", pricing_payload),
            ("/v3/accounts", acc_id_payload)
        ]

    # No account id available
    source1 = OandaSource(
        config, token="test",
        session=_FakeSession(make_fallback_routes({"prices": []}, NetworkError("fail")))
    )
    assert source1.fetch_current_price() == Decimal("2405.500")
    assert "cannot query /pricing" in source1.price_notes[0]

    # Empty prices array
    source2 = OandaSource(
        config, token="test",
        session=_FakeSession(make_fallback_routes({"prices": []}))
    )
    assert source2.fetch_current_price() == Decimal("2405.500")
    assert "returned no price entry" in source2.price_notes[0]

    # Using closeoutBid / closeoutAsk when bids/asks missing
    source3 = OandaSource(
        config, token="test",
        session=_FakeSession(make_fallback_routes({
            "prices": [{
                "closeoutBid": "2000",
                "closeoutAsk": "2010"
            }]
        }))
    )
    # closeout midpoint is 2005
    # Band check: ref candle not passed, so tolerance is None -> returns True
    assert source3.fetch_current_price() == Decimal("2005")

    # Both sets missing
    source4 = OandaSource(
        config, token="test",
        session=_FakeSession(make_fallback_routes({
            "prices": [{}]
        }))
    )
    assert source4.fetch_current_price() == Decimal("2405.500")
    assert "returned neither bid nor ask" in source4.price_notes[0]

    # Negative price logic
    source5 = OandaSource(
        config, token="test",
        session=_FakeSession(make_fallback_routes({
            "prices": [{
                "closeoutBid": "-10",
                "closeoutAsk": "0"
            }]
        }))
    )
    assert source5.fetch_current_price() == Decimal("2405.500")
    assert "invalid non-positive price (-5)" in source5.price_notes[0]

    # Out of band logic
    source6 = OandaSource(
        config, token="test",
        session=_FakeSession(make_fallback_routes({
            "prices": [{"bids": [{"price": "5000"}], "asks": [{"price": "5000"}]}]
        }))
    )
    ref_candle = Candle(
        open_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=Decimal("100"), high=Decimal("110"), low=Decimal("90"), close=Decimal("100"),
        volume=Decimal("1"), close_time=datetime(2026, 1, 2, tzinfo=timezone.utc), is_closed=True
    )
    assert source6.fetch_current_price(reference_candle=ref_candle, tolerance=Decimal("10")) == Decimal("2405.500")
    assert "outside the last closed candle range" in source6.price_notes[0]


def test_oanda_read_display_precision_empty_instruments_loop() -> None:
    config = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    # Instruments list has items, but none of them match the requested instrument
    source = OandaSource(
        config, token="test",
        session=_FakeSession([
            ("/v3/accounts/acc-1/instruments", {"instruments": [
                {"name": "NOT_XAU_USD", "displayPrecision": 4},
                {"name": "ALSO_NOT", "displayPrecision": 3},
            ]}),
            ("/v3/accounts", {"accounts": [{"id": "acc-1"}]})
        ])
    )
    assert source._read_display_precision() is None
