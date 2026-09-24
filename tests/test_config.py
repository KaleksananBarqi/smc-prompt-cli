"""Tests for the configuration module and defaults."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from smc_prompt.config import (
    Config,
    PriceFormat,
    build_config,
    decimals_from_tick_size,
    fmt_atr,
    fmt_atr_distance,
    fmt_atr_pct,
    fmt_distance,
    fmt_generated_at,
    fmt_htf_date,
    fmt_ltf_datetime,
    fmt_output_stamp,
    fmt_output_stamp_hyphen,
    fmt_price,
    fmt_ratio,
    fmt_relative_volume,
    fmt_volume,
    interval_label,
    price_decimals,
    provider_interval,
    provider_label,
    provider_symbol,
    validate_interval,
    validate_review_candles,
    validate_validate_candles,
)
from smc_prompt.errors import ConfigError


def test_validate_interval():
    assert validate_interval("1h") == "1h"
    assert validate_interval("1d") == "1d"
    with pytest.raises(ConfigError, match="must be one of"):
        validate_interval("2m")

def test_validate_review_candles():
    assert validate_review_candles(30) == 30
    assert validate_review_candles(5) == 5
    assert validate_review_candles(500) == 500
    with pytest.raises(ConfigError, match="must be >="):
        validate_review_candles(4)
    with pytest.raises(ConfigError, match="must be <="):
        validate_review_candles(501)

def test_validate_validate_candles():
    assert validate_validate_candles(50) == 50
    assert validate_validate_candles(5) == 5
    assert validate_validate_candles(500) == 500
    with pytest.raises(ConfigError, match="must be >="):
        validate_validate_candles(4)
    with pytest.raises(ConfigError, match="must be <="):
        validate_validate_candles(501)

def test_provider_label():
    assert provider_label("binance") == "Binance"
    assert provider_label("twelvedata") == "Twelve Data"
    assert provider_label("oanda") == "OANDA"
    assert provider_label("bitunix") == "Bitunix"
    assert provider_label("unknown") == "unknown"

def test_provider_symbol():
    # Binance/Bitunix use canonical verbatim
    assert provider_symbol("binance", "BTCUSDT") == "BTCUSDT"
    assert provider_symbol("bitunix", "ETHUSDT") == "ETHUSDT"

    # Explicit table lookup
    assert provider_symbol("twelvedata", "XAUUSD") == "XAU/USD"
    assert provider_symbol("oanda", "XAUUSD") == "XAU_USD"

    # 6-letter FX heuristic
    assert provider_symbol("twelvedata", "EURUSD") == "EUR/USD"
    assert provider_symbol("oanda", "EURUSD") == "EUR_USD"

    # Fallback to verbatim
    assert provider_symbol("twelvedata", "UNKNOWN") == "UNKNOWN"

def test_provider_interval():
    assert provider_interval("binance", "15m") == "15m"
    assert provider_interval("bitunix", "15m") == "15m"
    assert provider_interval("twelvedata", "15m") == "15min"
    assert provider_interval("oanda", "15m") == "M15"

    with pytest.raises(ConfigError, match="does not support the interval"):
        provider_interval("twelvedata", "3m")

def test_price_decimals():
    assert price_decimals(Decimal("1500.5")) == 2
    assert price_decimals(1000) == 2
    assert price_decimals("1.5") == 4
    assert price_decimals(0.5) == 8

def test_decimals_from_tick_size():
    assert decimals_from_tick_size("0.01000000") == 2
    assert decimals_from_tick_size("0.00000100") == 6
    assert decimals_from_tick_size("1.00000000") == 0
    assert decimals_from_tick_size("invalid") is None
    assert decimals_from_tick_size(None) is None
    assert decimals_from_tick_size("-0.01") is None
    assert decimals_from_tick_size("0") is None

def test_fmt_price():
    assert fmt_price(1500) == "1500.00"
    assert fmt_price("1.5") == "1.5000"
    assert fmt_price(Decimal("0.5")) == "0.50000000"

def test_fmt_atr():
    assert fmt_atr(145.5) == "145.50"
    assert fmt_atr(1.5) == "1.50"
    assert fmt_atr(0.5) == "0.50000000"

def test_fmt_distance():
    # +0.52% (+330.00) from 63450.00 base (uses 2dp for absolute value due to reference magnitude >= 1000)
    assert fmt_distance(Decimal("63780"), Decimal("63450")) == "+0.52% (+330.00)"
    assert fmt_distance(Decimal("63120"), Decimal("63450")) == "-0.52% (-330.00)"

def test_fmt_ratio():
    assert fmt_ratio("0.1") == "0.1"
    assert fmt_ratio(Decimal("0.100").normalize()) == "0.1"

def test_fmt_volume():
    assert fmt_volume("123.456") == "123.456"

def test_fmt_relative_volume():
    assert fmt_relative_volume("1.234") == "x1.23"
    assert fmt_relative_volume(1.235) == "x1.24" # checking round half even logic

def test_fmt_atr_pct():
    assert fmt_atr_pct(Decimal("1.5"), Decimal("100")) == "1.50% of price"

def test_fmt_atr_distance():
    assert fmt_atr_distance(Decimal("105"), Decimal("100"), Decimal("2.5")) == "x2.00"
def test_fmt_ratio_fixed():
    assert fmt_ratio("0.1") == "0.1"
    assert fmt_ratio("0.100") == "0.100" # since it just uses string length essentially

def test_date_formatting():
    dt = datetime(2026, 1, 1, 15, 30, 45, tzinfo=timezone.utc)

    assert fmt_htf_date(dt) == "2026-01-01"
    assert fmt_ltf_datetime(dt) == "2026-01-01 15:30"
    assert fmt_generated_at(dt) == "2026-01-01T15:30:45Z"
    assert fmt_output_stamp(dt) == "20260101T153045Z"
    assert fmt_output_stamp_hyphen(dt) == "2026-01-01-15-30-45-UTC"

def test_price_format_class():
    pf_magnitude = PriceFormat()
    assert pf_magnitude.decimals(1500) == 2
    assert pf_magnitude.fmt(1500) == "1500.00"
    assert pf_magnitude.fmt_distance(Decimal("1515"), Decimal("1500")) == "+1.00% (+15.00)"

    pf_tick = PriceFormat(tick_decimals=4)
    assert pf_tick.decimals(1500) == 4
    assert pf_tick.fmt(1500) == "1500.0000"
    assert pf_tick.fmt_distance(Decimal("1515"), Decimal("1500")) == "+1.00% (+15.0000)"

def test_config_class_properties():
    cfg = Config(
        symbol="BTCUSDT",
        htf_candles=100,
        mtf_candles=200,
        ltf_candles=300,
        context_buffer=50,
        fetch_limit_max=200,
        provider="twelvedata",
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h"
    )

    assert cfg.provider_label == "Twelve Data"

    # Check max capping logic: 100+50=150 (<= 200) -> 150
    assert cfg.htf_fetch_limit == 150
    # 200+50=250 (> 200 limit) -> 200
    assert cfg.mtf_fetch_limit == 200
    # 300+50=350 (> 200 limit) -> 200
    assert cfg.ltf_fetch_limit == 200

    assert cfg.htf_interval_label == "Daily"
    assert cfg.mtf_interval_label == "4H"
    assert cfg.ltf_interval_label == "1H"

def test_build_config_happy_path():
    cfg = build_config("BTCUSDT")
    assert cfg.symbol == "BTCUSDT"
    assert cfg.htf_interval == "4h"
    assert cfg.mtf_interval == "1h"
    assert cfg.ltf_interval == "15m"
    assert cfg.provider == "binance"
    assert cfg.volume_available is True

    cfg_oanda = build_config("EURUSD", provider="oanda")
    assert cfg_oanda.symbol == "EURUSD"
    assert cfg_oanda.provider == "oanda"
    assert cfg_oanda.volume_available is False

def test_build_config_validations():
    # Missing symbol
    with pytest.raises(ConfigError, match="SYMBOL is required"):
        build_config("")

    # Invalid lookback (even or < 3)
    with pytest.raises(ConfigError, match="odd integer >= 3"):
        build_config("BTC", swing_lookback=2)
    with pytest.raises(ConfigError, match="odd integer >= 3"):
        build_config("BTC", swing_lookback=4)

    # Invalid candle counts (< 10)
    with pytest.raises(ConfigError, match=">= 10"):
        build_config("BTC", htf_candles=9)
    with pytest.raises(ConfigError, match=">= 10"):
        build_config("BTC", mtf_candles=9)
    with pytest.raises(ConfigError, match=">= 10"):
        build_config("BTC", ltf_candles=9)

    # Invalid distance reference
    with pytest.raises(ConfigError, match="must be one of"):
        build_config("BTC", distance_reference="invalid")

    # Invalid intervals or duplicates
    with pytest.raises(ConfigError, match="must be one of"):
        build_config("BTC", htf_interval="2m")

    with pytest.raises(ConfigError, match="must be three DISTINCT intervals"):
        build_config("BTC", htf_interval="1h", mtf_interval="1h", ltf_interval="15m")

    # Invalid volume mean period
    with pytest.raises(ConfigError, match=">= 1"):
        build_config("BTC", volume_mean_period=0)

    # Invalid volume spike mult
    with pytest.raises(ConfigError, match="must be positive"):
        build_config("BTC", volume_spike_mult=-1)

    # Prompt bytes invalid
    with pytest.raises(ConfigError, match="positive integer"):
        build_config("BTC", prompt_bytes_warn=0)
    with pytest.raises(ConfigError, match=">= 1"):
        build_config("BTC", max_prompt_bytes=0)

    # Invalid provider
    with pytest.raises(ConfigError, match="must be one of"):
        build_config("BTC", provider="invalid")

    # Empty base urls
    with pytest.raises(ConfigError, match="At least one Binance base URL"):
        build_config("BTC", base_urls=["", "  "])

def test_interval_label():
    assert interval_label("1h") == "1H"
    assert interval_label("1d") == "Daily"
    assert interval_label("unknown") == "unknown"
