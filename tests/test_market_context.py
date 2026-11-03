"""Unit tests for derivatives market context enrichment (Ticket #35)."""

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



