"""Unit tests for derivatives & BTCUSDT benchmark market context enrichment (Tickets #35 & #36)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from smc_prompt import config as cfg
from smc_prompt.market_context import (
    fetch_derivatives_context,
    format_derivatives_context_line,
    format_funding_rate,
    format_oi_delta,
    format_open_interest,
)


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    def __init__(self, routes: list[tuple[str, object]]) -> None:
        self._routes = routes
        self.calls: list[tuple[str, dict]] = []

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


def test_format_funding_rate_signed_four_decimals() -> None:
    assert format_funding_rate("0.00010000") == "+0.0100%"
    assert format_funding_rate("-0.00005000") == "-0.0050%"
    assert format_funding_rate(Decimal("0")) == "+0.0000%"
    assert format_funding_rate(0.00025) == "+0.0250%"


def test_format_open_interest_compact_units() -> None:
    assert format_open_interest("452100") == "452.10K"
    assert format_open_interest("1250000") == "1.25M"
    assert format_open_interest("2500000000") == "2.50B"
    assert format_open_interest("850.5") == "850.50"


def test_format_oi_delta_signed_percentage() -> None:
    assert format_oi_delta("100000", "102450") == "(+2.45% 4H)"
    assert format_oi_delta("100000", "98500") == "(-1.50% 4H)"
    assert format_oi_delta("0", "100000") == ""


def test_format_derivatives_context_line_with_and_without_delta() -> None:
    line_full = format_derivatives_context_line(
        source_label="Binance",
        funding_rate="0.00010000",
        open_interest="452100",
        oi_delta_4h="(+2.45% 4H)",
    )
    assert (
        line_full
        == "- **Konteks Derivatif (Binance):** Funding: +0.0100% | OI: 452.10K (+2.45% 4H)"
    )

    line_no_delta = format_derivatives_context_line(
        source_label="Gate.io",
        funding_rate="-0.00005",
        open_interest="1250000",
    )
    assert (
        line_no_delta
        == "- **Konteks Derivatif (Gate.io):** Funding: -0.0050% | OI: 1.25M"
    )

    assert format_derivatives_context_line(source_label="Binance") == ""


def test_fetch_derivatives_context_binance_full() -> None:
    config = cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_BINANCE)
    session = _FakeSession(
        [
            ("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT", "lastFundingRate": "0.00010000"}),
            ("/fapi/v1/openInterest", {"symbol": "BTCUSDT", "openInterest": "452100"}),
            (
                "/futures/data/openInterestHist",
                [
                    {"symbol": "BTCUSDT", "sumOpenInterest": "441288.43"},
                    {"symbol": "BTCUSDT", "sumOpenInterest": "452100.00"},
                ],
            ),
        ]
    )

    line = fetch_derivatives_context(config, session=session)
    assert (
        line
        == "- **Konteks Derivatif (Binance):** Funding: +0.0100% | OI: 452.10K (+2.45% 4H)"
    )


def test_fetch_derivatives_context_binance_hist_failure_degrades_gracefully() -> None:
    config = cfg.build_config("ETHUSDT", provider=cfg.PROVIDER_BINANCE)
    session = _FakeSession(
        [
            ("/fapi/v1/premiumIndex", {"symbol": "ETHUSDT", "lastFundingRate": "-0.00005000"}),
            ("/fapi/v1/openInterest", {"symbol": "ETHUSDT", "openInterest": "1250000"}),
            ("/futures/data/openInterestHist", ({"msg": "unavailable"}, 500)),
        ]
    )

    line = fetch_derivatives_context(config, session=session)
    assert line == "- **Konteks Derivatif (Binance):** Funding: -0.0050% | OI: 1.25M"


def test_fetch_derivatives_context_fallback_to_gate_when_binance_blocked() -> None:
    import requests

    config = cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_GATE)
    session = _FakeSession(
        [
            ("/fapi/v1/premiumIndex", ({"msg": "restricted"}, 451)),
            (
                "/api/v4/futures/usdt/contracts/BTC_USDT",
                {"name": "BTC_USDT", "funding_rate": "0.0001", "position_size": 452100},
            ),
        ]
    )

    line = fetch_derivatives_context(config, session=session)
    assert line == "- **Konteks Derivatif (Gate.io):** Funding: +0.0100% | OI: 452.10K"


def test_fetch_derivatives_context_fallback_to_mexc_when_binance_blocked() -> None:
    import requests

    config = cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_MEXC)
    session = _FakeSession(
        [
            ("/fapi/v1/premiumIndex", requests.ConnectionError("ISP DNS block")),
            (
                "/api/v1/contract/ticker",
                {
                    "code": 0,
                    "data": {
                        "symbol": "BTC_USDT",
                        "fundingRate": "-0.00005",
                        "holdVol": "1250000",
                    },
                },
            ),
        ]
    )

    line = fetch_derivatives_context(config, session=session)
    assert line == "- **Konteks Derivatif (MEXC):** Funding: -0.0050% | OI: 1.25M"


def test_fetch_derivatives_context_fallback_to_bitunix_when_binance_blocked() -> None:
    config = cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_BITUNIX)
    session = _FakeSession(
        [
            ("/fapi/v1/premiumIndex", ({"msg": "blocked"}, 451)),
            (
                "/api/v1/futures/market/tickers",
                {
                    "code": 0,
                    "data": [
                        {
                            "symbol": "BTCUSDT",
                            "fundingRate": "0.0002",
                            "openInterest": "320000",
                        }
                    ],
                },
            ),
        ]
    )

    line = fetch_derivatives_context(config, session=session)
    assert line == "- **Konteks Derivatif (Bitunix):** Funding: +0.0200% | OI: 320.00K"


def test_fetch_derivatives_context_all_fail_returns_empty_string() -> None:
    import requests

    config = cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_GATE)
    session = _FakeSession(
        [
            ("/fapi/v1/premiumIndex", requests.ConnectionError("blocked")),
            ("/api/v4/futures/usdt/contracts/BTC_USDT", ({"label": "ERROR"}, 500)),
        ]
    )

    assert fetch_derivatives_context(config, session=session) == ""


def test_fetch_derivatives_context_skips_network_for_offline_and_fx_providers() -> None:
    session = _FakeSession([])

    btc_cfg = cfg.build_config("BTCUSDT", provider=cfg.PROVIDER_BINANCE)
    assert fetch_derivatives_context(btc_cfg, session=session, offline_mode=True) == ""
    assert len(session.calls) == 0

    td_cfg = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_TWELVEDATA)
    assert fetch_derivatives_context(td_cfg, session=session) == ""
    assert len(session.calls) == 0

    oanda_cfg = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_OANDA)
    assert fetch_derivatives_context(oanda_cfg, session=session) == ""
    assert len(session.calls) == 0


def test_render_with_and_without_derivatives_context_line(tmp_path) -> None:
    from datetime import datetime, timezone
    from smc_prompt.csv_source import load_csv_series
    from smc_prompt.structure_analyzer import analyze
    from smc_prompt.template_renderer import (
        DERIVATIVES_CONTEXT_VAR,
        build_payload,
        render,
    )
    from tests.conftest import HTF_CSV, LTF_CSV, MTF_CSV

    assert DERIVATIVES_CONTEXT_VAR == "DERIVATIVES_CONTEXT_LINE"

    config = cfg.build_config(
        "BTCUSDT",
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        htf_candles=20,
        mtf_candles=30,
        ltf_candles=40,
    )
    htf_raw = load_csv_series(str(HTF_CSV), interval="1d").candles
    mtf_raw = load_csv_series(str(MTF_CSV), interval="4h").candles
    ltf_raw = load_csv_series(str(LTF_CSV), interval="1h").candles
    current_price = ltf_raw[-1].close
    generated_at = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    htf_res, htf_tbl, _ = analyze(
        htf_raw, timeframe="1d", requested=20, current_price=current_price, config=config
    )
    mtf_res, mtf_tbl, _ = analyze(
        mtf_raw, timeframe="4h", requested=30, current_price=current_price, config=config
    )
    ltf_res, ltf_tbl, _ = analyze(
        ltf_raw, timeframe="1h", requested=40, current_price=current_price, config=config
    )

    payload = build_payload(
        symbol="BTCUSDT",
        generated_at=generated_at,
        current_price=current_price,
        htf=htf_res,
        mtf=mtf_res,
        ltf=ltf_res,
        htf_candles=htf_tbl,
        mtf_candles=mtf_tbl,
        ltf_candles=ltf_tbl,
        config=config,
    )

    rendered_empty = render(payload, include_atr=True, provider_name="Binance")
    assert "Konteks Derivatif" not in rendered_empty.text
    assert (
        f"- **ATR % of price (HTF):** {payload['ATR_PCT_OF_PRICE']}\n\n> Data di bawah"
        in rendered_empty.text
    )

    deriv_line = "- **Konteks Derivatif (Binance):** Funding: +0.0100% | OI: 452.10K (+2.45% 4H)"
    rendered_enriched = render(
        payload,
        include_atr=True,
        provider_name="Binance",
        derivatives_context_line=deriv_line,
    )
    assert (
        f"- **ATR % of price (HTF):** {payload['ATR_PCT_OF_PRICE']}\n"
        f"{deriv_line}\n\n"
        f"> Data di bawah"
    ) in rendered_enriched.text


def test_cli_run_wires_derivatives_context_in_live_crypto_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from datetime import datetime, timezone
    from smc_prompt import cli
    from smc_prompt.csv_source import load_csv_series
    from tests.conftest import HTF_CSV, LTF_CSV, MTF_CSV

    class _EnrichedFetcher:
        def __init__(self, cfg_obj: cfg.Config) -> None:
            self._cfg = cfg_obj
            self.price_notes = ()
            self._htf = load_csv_series(str(HTF_CSV), interval="1d").candles
            self._mtf = load_csv_series(str(MTF_CSV), interval="4h").candles
            self._ltf = load_csv_series(str(LTF_CSV), interval="1h").candles

        def validate_symbol(self) -> dict:
            return {
                "symbol": self._cfg.symbol,
                "status": "TRADING",
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1"}],
            }

        def fetch_server_time(self) -> datetime:
            return datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

        def with_now(self, moment: datetime) -> "_EnrichedFetcher":
            return self

        def fetch_klines(self, interval: str, limit: int):
            if interval == self._cfg.htf_interval:
                return self._htf[-limit:]
            if interval == self._cfg.mtf_interval:
                return self._mtf[-limit:]
            return self._ltf[-limit:]

        def fetch_current_price(self, *args, **kwargs) -> Decimal:
            return Decimal("62800.0")

        def fetch_derivatives_info(self) -> dict:
            return {
                "source_label": "Binance",
                "funding_rate": "0.00010000",
                "open_interest": "452100",
                "oi_delta_4h": "(+2.45% 4H)",
            }

    monkeypatch.setattr(
        "smc_prompt.cli._build_fetcher",
        lambda provider_name, active_cfg, **kw: _EnrichedFetcher(active_cfg),
    )

    result = cli.run(
        "BTCUSDT",
        output_dir=str(tmp_path),
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        htf_candles=20,
        mtf_candles=30,
        ltf_candles=40,
    )
    assert (
        "- **Konteks Derivatif (Binance):** Funding: +0.0100% | OI: 452.10K (+2.45% 4H)"
        in result.prompt
    )


def test_format_btc_benchmark_line_renders_expected_header_format() -> None:
    from smc_prompt.market_context import format_btc_benchmark_line

    line = format_btc_benchmark_line(
        source_label="Binance",
        btc_price="62800",
        htf_label="1D",
        htf_structure="Bullish",
        htf_recent_high="63450.0",
        htf_high_status="untested",
        htf_recent_low="60100.0",
        htf_low_status="wick_sweep",
        mtf_label="4H",
        mtf_structure="Ranging/Mixed",
    )
    assert (
        line
        == "- **Benchmark BTCUSDT (Binance):** 62800.00 | HTF (1D): Bullish "
        "(Ref HIGH: 63450.00 untested, Ref LOW: 60100.00 wick_sweep) | MTF (4H): Ranging/Mixed"
    )

    assert (
        format_btc_benchmark_line(
            source_label="Binance",
            btc_price=None,
            htf_label="1D",
            htf_structure="Bullish",
            htf_recent_high="63450",
            htf_high_status="untested",
            htf_recent_low="60100",
            htf_low_status="untested",
            mtf_label="4H",
            mtf_structure="Bullish",
        )
        == ""
    )


def test_render_with_and_without_btc_benchmark_line() -> None:
    from datetime import datetime, timezone
    from smc_prompt.csv_source import load_csv_series
    from smc_prompt.structure_analyzer import analyze
    from smc_prompt.template_renderer import (
        BTC_BENCHMARK_VAR,
        build_payload,
        render,
    )
    from tests.conftest import HTF_CSV, LTF_CSV, MTF_CSV

    assert BTC_BENCHMARK_VAR == "BTC_BENCHMARK_LINE"

    config = cfg.build_config(
        "ETHUSDT",
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        htf_candles=20,
        mtf_candles=30,
        ltf_candles=40,
    )
    htf_raw = load_csv_series(str(HTF_CSV), interval="1d").candles
    mtf_raw = load_csv_series(str(MTF_CSV), interval="4h").candles
    ltf_raw = load_csv_series(str(LTF_CSV), interval="1h").candles
    current_price = ltf_raw[-1].close
    generated_at = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    htf_res, htf_tbl, _ = analyze(
        htf_raw, timeframe="1d", requested=20, current_price=current_price, config=config
    )
    mtf_res, mtf_tbl, _ = analyze(
        mtf_raw, timeframe="4h", requested=30, current_price=current_price, config=config
    )
    ltf_res, ltf_tbl, _ = analyze(
        ltf_raw, timeframe="1h", requested=40, current_price=current_price, config=config
    )

    payload = build_payload(
        symbol="ETHUSDT",
        generated_at=generated_at,
        current_price=current_price,
        htf=htf_res,
        mtf=mtf_res,
        ltf=ltf_res,
        htf_candles=htf_tbl,
        mtf_candles=mtf_tbl,
        ltf_candles=ltf_tbl,
        config=config,
    )

    rendered_empty = render(payload, include_atr=True, provider_name="Binance")
    assert "Benchmark BTCUSDT" not in rendered_empty.text

    deriv_line = "- **Konteks Derivatif (Binance):** Funding: +0.0100% | OI: 1.25M (+1.20% 4H)"
    btc_line = (
        "- **Benchmark BTCUSDT (Binance):** 62800.00 | HTF (1D): Bullish "
        "(Ref HIGH: 63450.00 untested, Ref LOW: 60100.00 untested) | MTF (4H): Ranging/Mixed"
    )
    rendered_both = render(
        payload,
        include_atr=True,
        provider_name="Binance",
        derivatives_context_line=deriv_line,
        btc_benchmark_line=btc_line,
    )
    assert (
        f"- **ATR % of price (HTF):** {payload['ATR_PCT_OF_PRICE']}\n"
        f"{deriv_line}\n"
        f"{btc_line}\n\n"
        f"> Data di bawah"
    ) in rendered_both.text


def _binance_klines_from_csv(csv_path, interval: str) -> list[list]:
    from smc_prompt.csv_source import load_csv_series

    candles = load_csv_series(str(csv_path), interval=interval).candles
    rows: list[list] = []
    for c in candles:
        open_ms = int(c.open_time.timestamp() * 1000)
        close_ms = int(c.close_time.timestamp() * 1000)
        rows.append(
            [
                open_ms,
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
                close_ms,
            ]
        )
    return rows


def test_fetch_btc_benchmark_context_skips_when_symbol_is_btcusdt() -> None:
    from smc_prompt.market_context import fetch_btc_benchmark_context

    session = _FakeSession([])
    for sym in ("BTCUSDT", "btcusdt", "BTC_USDT", "BTC/USDT", "BTCUSDC", "BTCUSD", "BTCBUSD"):
        config = cfg.build_config(sym, provider=cfg.PROVIDER_BINANCE)
        assert fetch_btc_benchmark_context(config, session=session) == ""
    assert len(session.calls) == 0


def test_fetch_btc_benchmark_context_skips_for_offline_and_fx_providers() -> None:
    from smc_prompt.market_context import fetch_btc_benchmark_context

    session = _FakeSession([])

    eth_cfg = cfg.build_config("ETHUSDT", provider=cfg.PROVIDER_BINANCE)
    assert fetch_btc_benchmark_context(eth_cfg, session=session, offline_mode=True) == ""
    assert len(session.calls) == 0

    td_cfg = cfg.build_config("XAUUSD", provider=cfg.PROVIDER_TWELVEDATA)
    assert fetch_btc_benchmark_context(td_cfg, session=session) == ""
    assert len(session.calls) == 0

    oanda_cfg = cfg.build_config("EURUSD", provider=cfg.PROVIDER_OANDA)
    assert fetch_btc_benchmark_context(oanda_cfg, session=session) == ""
    assert len(session.calls) == 0


def test_fetch_btc_benchmark_context_for_altcoin_renders_btc_structure_and_ref_levels() -> None:
    from smc_prompt.csv_source import load_csv_series
    from smc_prompt.market_context import fetch_btc_benchmark_context, format_btc_benchmark_line
    from smc_prompt.models import SwingType
    from smc_prompt.structure_analyzer import analyze
    from tests.conftest import HTF_CSV, MTF_CSV

    htf_kline_rows = _binance_klines_from_csv(HTF_CSV, "1d")
    mtf_kline_rows = _binance_klines_from_csv(MTF_CSV, "4h")

    class _BtcKlinesSession(_FakeSession):
        def get(self, url: str, params=None, timeout=None, headers=None):
            p = dict(params or {})
            self.calls.append((url, p))
            if "/fapi/v1/klines" in url:
                assert p.get("symbol") == "BTCUSDT"
                if p.get("interval") == "1d":
                    return _FakeResponse(htf_kline_rows)
                if p.get("interval") == "4h":
                    return _FakeResponse(mtf_kline_rows)
            if "/fapi/v1/ticker/price" in url:
                assert p.get("symbol") == "BTCUSDT"
                return _FakeResponse({"symbol": "BTCUSDT", "price": "62800.00"})
            raise AssertionError(f"Unexpected call {url} {p}")

    session = _BtcKlinesSession([])
    config = cfg.build_config(
        "ETHUSDT",
        provider=cfg.PROVIDER_BINANCE,
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        htf_candles=20,
        mtf_candles=30,
        ltf_candles=40,
    )

    line = fetch_btc_benchmark_context(config, session=session)

    # Independently verify against analyze() on the same fixture series
    from dataclasses import replace as dc_replace
    btc_cfg = dc_replace(config, symbol="BTCUSDT", price_format=cfg.PriceFormat(tick_decimals=2))
    htf_candles = load_csv_series(str(HTF_CSV), interval="1d").candles
    mtf_candles = load_csv_series(str(MTF_CSV), interval="4h").candles
    htf_res, _, _ = analyze(
        htf_candles,
        timeframe="1d",
        requested=20,
        current_price=Decimal("62800.00"),
        config=btc_cfg,
    )
    mtf_res, _, _ = analyze(
        mtf_candles,
        timeframe="4h",
        requested=30,
        current_price=Decimal("62800.00"),
        config=btc_cfg,
    )
    recent_high = [s for s in htf_res.swings if s.type is SwingType.HIGH][-1]
    recent_low = [s for s in htf_res.swings if s.type is SwingType.LOW][-1]
    expected = format_btc_benchmark_line(
        source_label="Binance",
        btc_price="62800.00",
        htf_label=btc_cfg.htf_interval_label,
        htf_structure=htf_res.structure_class.value,
        htf_recent_high=recent_high.price,
        htf_high_status=recent_high.status,
        htf_recent_low=recent_low.price,
        htf_low_status=recent_low.status,
        mtf_label=btc_cfg.mtf_interval_label,
        mtf_structure=mtf_res.structure_class.value,
        price_format=btc_cfg.price_format,
    )
    assert line == expected
    assert line.startswith("- **Benchmark BTCUSDT (Binance):** 62800.00 | HTF (Daily): ")


def _gate_candlesticks_from_csv(csv_path, interval: str) -> list[dict]:
    from smc_prompt.csv_source import load_csv_series

    candles = load_csv_series(str(csv_path), interval=interval).candles
    return [
        {
            "t": int(c.open_time.timestamp()),
            "o": str(c.open),
            "h": str(c.high),
            "l": str(c.low),
            "c": str(c.close),
            "v": str(c.volume),
        }
        for c in candles
    ]


def test_fetch_btc_benchmark_context_fallback_to_gate_and_graceful_failure() -> None:
    import requests
    from smc_prompt.market_context import fetch_btc_benchmark_context
    from tests.conftest import HTF_CSV, MTF_CSV

    gate_htf = _gate_candlesticks_from_csv(HTF_CSV, "1d")
    gate_mtf = _gate_candlesticks_from_csv(MTF_CSV, "4h")

    class _FallbackSession(_FakeSession):
        def get(self, url: str, params=None, timeout=None, headers=None):
            p = dict(params or {})
            self.calls.append((url, p))
            if "/fapi/v1/" in url:
                return _FakeResponse({"msg": "restricted"}, status_code=451)
            if "/api/v4/futures/usdt/candlesticks" in url:
                assert p.get("contract") == "BTC_USDT"
                if p.get("interval") == "1d":
                    return _FakeResponse(gate_htf)
                if p.get("interval") == "4h":
                    return _FakeResponse(gate_mtf)
            if "/api/v4/futures/usdt/tickers" in url:
                assert p.get("contract") == "BTC_USDT"
                return _FakeResponse([{"contract": "BTC_USDT", "last": "62800.00"}])
            raise AssertionError(f"Unexpected call {url} {p}")

    config = cfg.build_config(
        "SOLUSDT",
        provider=cfg.PROVIDER_GATE,
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        htf_candles=20,
        mtf_candles=30,
        ltf_candles=40,
    )
    line = fetch_btc_benchmark_context(config, session=_FallbackSession([]))
    assert line.startswith("- **Benchmark BTCUSDT (Gate.io):** 62800.00 | HTF (Daily): ")
    assert "Ref HIGH:" in line and "Ref LOW:" in line and "| MTF (4H):" in line

    # Graceful failure when both Binance and active provider fail
    failing_session = _FakeSession(
        [
            ("/fapi/v1/", requests.ConnectionError("ISP block")),
            ("/api/v4/futures/usdt/", ({"label": "SERVER_ERROR"}, 500)),
        ]
    )
    assert fetch_btc_benchmark_context(config, session=failing_session) == ""


def test_fetch_btc_benchmark_context_fallback_to_bitunix_and_mexc() -> None:
    from smc_prompt.csv_source import load_csv_series
    from smc_prompt.market_context import fetch_btc_benchmark_context
    from tests.conftest import HTF_CSV, MTF_CSV

    htf_candles = load_csv_series(str(HTF_CSV), interval="1d").candles
    mtf_candles = load_csv_series(str(MTF_CSV), interval="4h").candles

    # Bitunix returns newest-first in `data`
    bitunix_htf = [
        {
            "time": int(c.open_time.timestamp() * 1000),
            "open": str(c.open),
            "high": str(c.high),
            "low": str(c.low),
            "close": str(c.close),
            "baseVol": str(c.volume),
        }
        for c in reversed(htf_candles)
    ]
    bitunix_mtf = [
        {
            "time": int(c.open_time.timestamp() * 1000),
            "open": str(c.open),
            "high": str(c.high),
            "low": str(c.low),
            "close": str(c.close),
            "baseVol": str(c.volume),
        }
        for c in reversed(mtf_candles)
    ]

    class _BitunixSession(_FakeSession):
        def get(self, url: str, params=None, timeout=None, headers=None):
            p = dict(params or {})
            self.calls.append((url, p))
            if "/fapi/v1/" in url:
                return _FakeResponse({"msg": "restricted"}, status_code=451)
            if "/api/v1/futures/market/kline" in url:
                assert p.get("symbol") == "BTCUSDT"
                if p.get("interval") == "1d":
                    return _FakeResponse({"code": 0, "data": bitunix_htf})
                if p.get("interval") == "4h":
                    return _FakeResponse({"code": 0, "data": bitunix_mtf})
            if "/api/v1/futures/market/tickers" in url:
                return _FakeResponse(
                    {"code": 0, "data": [{"symbol": "BTCUSDT", "lastPrice": "62800.00"}]}
                )
            raise AssertionError(f"Unexpected call {url} {p}")

    bitunix_cfg = cfg.build_config(
        "NEARUSDT",
        provider=cfg.PROVIDER_BITUNIX,
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        htf_candles=20,
        mtf_candles=30,
        ltf_candles=40,
    )
    bitunix_line = fetch_btc_benchmark_context(bitunix_cfg, session=_BitunixSession([]))
    assert bitunix_line.startswith(
        "- **Benchmark BTCUSDT (Bitunix):** 62800.00 | HTF (Daily): "
    )

    mexc_htf = {
        "time": [int(c.open_time.timestamp()) for c in htf_candles],
        "open": [str(c.open) for c in htf_candles],
        "high": [str(c.high) for c in htf_candles],
        "low": [str(c.low) for c in htf_candles],
        "close": [str(c.close) for c in htf_candles],
        "vol": [str(c.volume) for c in htf_candles],
    }
    mexc_mtf = {
        "time": [int(c.open_time.timestamp()) for c in mtf_candles],
        "open": [str(c.open) for c in mtf_candles],
        "high": [str(c.high) for c in mtf_candles],
        "low": [str(c.low) for c in mtf_candles],
        "close": [str(c.close) for c in mtf_candles],
        "vol": [str(c.volume) for c in mtf_candles],
    }

    class _MexcSession(_FakeSession):
        def get(self, url: str, params=None, timeout=None, headers=None):
            p = dict(params or {})
            self.calls.append((url, p))
            if "/fapi/v1/" in url:
                return _FakeResponse({"msg": "restricted"}, status_code=451)
            if "/api/v1/contract/kline/BTC_USDT" in url:
                if p.get("interval") == "Day1":
                    return _FakeResponse({"code": 0, "data": mexc_htf})
                if p.get("interval") == "Hour4":
                    return _FakeResponse({"code": 0, "data": mexc_mtf})
            if "/api/v1/contract/ticker" in url:
                return _FakeResponse(
                    {"code": 0, "data": {"symbol": "BTC_USDT", "lastPrice": "62800.00"}}
                )
            raise AssertionError(f"Unexpected call {url} {p}")

    mexc_cfg = cfg.build_config(
        "NEARUSDT",
        provider=cfg.PROVIDER_MEXC,
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        htf_candles=20,
        mtf_candles=30,
        ltf_candles=40,
    )
    mexc_line = fetch_btc_benchmark_context(mexc_cfg, session=_MexcSession([]))
    assert mexc_line.startswith(
        "- **Benchmark BTCUSDT (MEXC):** 62800.00 | HTF (Daily): "
    )


def test_cli_run_wires_btc_benchmark_for_altcoin_and_skips_for_btcusdt(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from datetime import datetime, timezone
    from smc_prompt import cli
    from smc_prompt.csv_source import load_csv_series
    from tests.conftest import HTF_CSV, LTF_CSV, MTF_CSV

    class _AltcoinEnrichedFetcher:
        def __init__(self, cfg_obj: cfg.Config) -> None:
            self._cfg = cfg_obj
            self.price_notes = ()
            self._htf = load_csv_series(str(HTF_CSV), interval="1d").candles
            self._mtf = load_csv_series(str(MTF_CSV), interval="4h").candles
            self._ltf = load_csv_series(str(LTF_CSV), interval="1h").candles

        def validate_symbol(self) -> dict:
            return {
                "symbol": self._cfg.symbol,
                "status": "TRADING",
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}],
            }

        def fetch_server_time(self) -> datetime:
            return datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

        def with_now(self, moment: datetime) -> "_AltcoinEnrichedFetcher":
            return self

        def fetch_klines(self, interval: str, limit: int):
            if interval == self._cfg.htf_interval:
                return self._htf[-limit:]
            if interval == self._cfg.mtf_interval:
                return self._mtf[-limit:]
            return self._ltf[-limit:]

        def fetch_current_price(self, *args, **kwargs) -> Decimal:
            return Decimal("3450.25")

        def fetch_derivatives_info(self) -> dict:
            return {
                "source_label": "Binance",
                "funding_rate": "0.00010000",
                "open_interest": "1250000",
                "oi_delta_4h": "(+1.80% 4H)",
            }

        def fetch_btc_benchmark_info(self) -> dict:
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
                "mtf_structure": "Ranging/Mixed",
            }

    monkeypatch.setattr(
        "smc_prompt.cli._build_fetcher",
        lambda provider_name, active_cfg, **kw: _AltcoinEnrichedFetcher(active_cfg),
    )

    eth_result = cli.run(
        "ETHUSDT",
        output_dir=str(tmp_path / "eth"),
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        htf_candles=20,
        mtf_candles=30,
        ltf_candles=40,
    )
    assert (
        "- **Konteks Derivatif (Binance):** Funding: +0.0100% | OI: 1.25M (+1.80% 4H)\n"
        "- **Benchmark BTCUSDT (Binance):** 62800.00 | HTF (Daily): Bullish "
        "(Ref HIGH: 63450.00 untested, Ref LOW: 60100.00 untested) | MTF (4H): Ranging/Mixed"
    ) in eth_result.prompt

    btc_result = cli.run(
        "BTCUSDT",
        output_dir=str(tmp_path / "btc"),
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        htf_candles=20,
        mtf_candles=30,
        ltf_candles=40,
    )
    assert "Benchmark BTCUSDT" not in btc_result.prompt






