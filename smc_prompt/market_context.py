"""Pengayaan konteks pasar derivatif (Funding Rate & Open Interest) dengan strategi Binance-First.

Mengambil statistik derivatif dari Binance Futures terlebih dahulu sebagai acuan
likuiditas global terdalam, lalu beralih (*fallback*) secara anggun ke provider
kripto aktif (`bitunix`, `mexc`, `gate`) apabila Binance tidak dapat dihubungi
atau tidak mencantumkan simbol tersebut.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from . import config as cfg

BINANCE_PREMIUM_INDEX_PATH: str = "/fapi/v1/premiumIndex"
BINANCE_OPEN_INTEREST_PATH: str = "/fapi/v1/openInterest"
BINANCE_OPEN_INTEREST_HIST_PATH: str = "/futures/data/openInterestHist"
BITUNIX_TICKERS_PATH: str = "/api/v1/futures/market/tickers"
MEXC_TICKER_PATH: str = "/api/v1/contract/ticker"
GATE_CONTRACT_PATH_TEMPLATE: str = "/api/v4/futures/usdt/contracts/{contract}"


def _to_decimal(value: Decimal | float | int | str | None) -> Decimal | None:
    """Konversi nilai numerik/string ke Decimal secara aman, atau None jika gagal."""

    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not dec.is_finite():
        return None
    return dec


def format_funding_rate(rate: Decimal | float | int | str) -> str:
    """Format rasio funding rate desimal menjadi persentase bertanda 4 desimal (contoh: ``+0.0100%``)."""

    dec = _to_decimal(rate)
    if dec is None:
        return "n/a"
    pct = dec * Decimal("100")
    sign = "+" if pct >= 0 else "-"
    return f"{sign}{abs(pct):.4f}%"


def format_open_interest(oi: Decimal | float | int | str) -> str:
    """Format nilai Open Interest secara ringkas menggunakan sufiks ``K``, ``M``, atau ``B``."""

    dec = _to_decimal(oi)
    if dec is None:
        return "n/a"
    mag = abs(dec)
    if mag >= Decimal("1000000000"):
        return f"{dec / Decimal('1000000000'):.2f}B"
    if mag >= Decimal("1000000"):
        return f"{dec / Decimal('1000000'):.2f}M"
    if mag >= Decimal("1000"):
        return f"{dec / Decimal('1000'):.2f}K"
    return f"{dec:.2f}"


def format_oi_delta(
    prev_oi: Decimal | float | int | str,
    latest_oi: Decimal | float | int | str,
) -> str:
    """Hitung dan format perubahan persentase Open Interest 4H (contoh: ``(+2.45% 4H)``)."""

    prev = _to_decimal(prev_oi)
    latest = _to_decimal(latest_oi)
    if prev is None or latest is None or prev <= 0:
        return ""
    pct = ((latest - prev) / prev) * Decimal("100")
    sign = "+" if pct >= 0 else "-"
    return f"({sign}{abs(pct):.2f}% 4H)"


def format_derivatives_context_line(
    *,
    source_label: str,
    funding_rate: Decimal | float | int | str | None = None,
    open_interest: Decimal | float | int | str | None = None,
    oi_delta_4h: str | None = None,
) -> str:
    """Rakit baris tunggal ringkasan konteks derivatif untuk header prompt."""

    funding_dec = _to_decimal(funding_rate)
    oi_dec = _to_decimal(open_interest)
    if funding_dec is None and oi_dec is None:
        return ""

    funding_str = format_funding_rate(funding_dec) if funding_dec is not None else "n/a"
    oi_str = format_open_interest(oi_dec) if oi_dec is not None else "n/a"
    delta_suffix = f" {oi_delta_4h.strip()}" if oi_delta_4h and oi_delta_4h.strip() else ""
    return (
        f"- **Konteks Derivatif ({source_label}):** "
        f"Funding: {funding_str} | OI: {oi_str}{delta_suffix}"
    )


def _get_json_once(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    *,
    timeout: float,
) -> Any:
    """Lakukan satu kali permintaan HTTP GET dan kembalikan JSON jika status 2xx."""

    response = session.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "smc-prompt/0.1"},
    )
    if response.status_code >= 400:
        return None
    return response.json()


def _fetch_binance_derivatives(
    config: cfg.Config,
    session: requests.Session,
) -> str:
    """Ambil Funding Rate, Open Interest, dan opsional delta 4H dari Binance Futures."""

    symbol = cfg.provider_symbol(cfg.PROVIDER_BINANCE, config.symbol)
    hosts = config.base_urls or cfg.DEFAULT_BASE_URLS
    timeout = config.request_timeout

    for host in hosts:
        base = host.rstrip("/")
        try:
            prem = _get_json_once(
                session,
                f"{base}{BINANCE_PREMIUM_INDEX_PATH}",
                {"symbol": symbol},
                timeout=timeout,
            )
            if not isinstance(prem, dict) or "lastFundingRate" not in prem:
                continue

            oi_payload = _get_json_once(
                session,
                f"{base}{BINANCE_OPEN_INTEREST_PATH}",
                {"symbol": symbol},
                timeout=timeout,
            )
            if not isinstance(oi_payload, dict) or "openInterest" not in oi_payload:
                continue

            funding_rate = prem.get("lastFundingRate")
            open_interest = oi_payload.get("openInterest")

            oi_delta_str = ""
            try:
                hist = _get_json_once(
                    session,
                    f"{base}{BINANCE_OPEN_INTEREST_HIST_PATH}",
                    {"symbol": symbol, "period": "4h", "limit": 2},
                    timeout=timeout,
                )
                if isinstance(hist, list) and len(hist) >= 2:
                    prev_entry = hist[-2]
                    latest_entry = hist[-1]
                    if isinstance(prev_entry, dict) and isinstance(latest_entry, dict):
                        prev_val = prev_entry.get("sumOpenInterest") or prev_entry.get("openInterest")
                        latest_val = latest_entry.get("sumOpenInterest") or latest_entry.get("openInterest")
                        if prev_val is not None and latest_val is not None:
                            oi_delta_str = format_oi_delta(prev_val, latest_val)
            except (Exception, AssertionError):
                oi_delta_str = ""

            line = format_derivatives_context_line(
                source_label=cfg.provider_label(cfg.PROVIDER_BINANCE),
                funding_rate=funding_rate,
                open_interest=open_interest,
                oi_delta_4h=oi_delta_str,
            )
            if line:
                return line
        except (Exception, AssertionError):
            continue

    return ""


def _fetch_bitunix_derivatives(
    config: cfg.Config,
    session: requests.Session,
) -> str:
    """Ambil Funding Rate & Open Interest dari endpoint tickers Bitunix Futures."""

    symbol = cfg.provider_symbol(cfg.PROVIDER_BITUNIX, config.symbol)
    url = f"{cfg.BITUNIX_HOST.rstrip('/')}{BITUNIX_TICKERS_PATH}"
    try:
        payload = _get_json_once(
            session,
            url,
            {"symbols": symbol},
            timeout=config.request_timeout,
        )
        if not isinstance(payload, dict) or payload.get("code", 0) != 0:
            return ""
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return ""
        matched: dict[str, Any] | None = None
        for item in data:
            if isinstance(item, dict) and item.get("symbol", "").upper() == symbol:
                matched = item
                break
        if matched is None and isinstance(data[0], dict):
            matched = data[0]
        if matched is None:
            return ""

        funding_rate = (
            matched.get("fundingRate")
            if matched.get("fundingRate") is not None
            else matched.get("funding_rate", matched.get("lastFundingRate"))
        )
        open_interest = (
            matched.get("openInterest")
            if matched.get("openInterest") is not None
            else matched.get("open_interest", matched.get("holdVol"))
        )
        return format_derivatives_context_line(
            source_label=cfg.provider_label(cfg.PROVIDER_BITUNIX),
            funding_rate=funding_rate,
            open_interest=open_interest,
        )
    except (Exception, AssertionError):
        return ""


def _fetch_mexc_derivatives(
    config: cfg.Config,
    session: requests.Session,
) -> str:
    """Ambil Funding Rate & Open Interest (holdVol) dari endpoint ticker MEXC Futures."""

    symbol = cfg.provider_symbol(cfg.PROVIDER_MEXC, config.symbol)
    url = f"{cfg.MEXC_HOST.rstrip('/')}{MEXC_TICKER_PATH}"
    try:
        payload = _get_json_once(
            session,
            url,
            {"symbol": symbol},
            timeout=config.request_timeout,
        )
        if not isinstance(payload, dict) or payload.get("code", 0) != 0:
            return ""
        data = payload.get("data")
        if not isinstance(data, dict):
            return ""

        funding_rate = (
            data.get("fundingRate")
            if data.get("fundingRate") is not None
            else data.get("funding_rate")
        )
        open_interest = (
            data.get("holdVol")
            if data.get("holdVol") is not None
            else data.get("openInterest", data.get("open_interest"))
        )
        return format_derivatives_context_line(
            source_label=cfg.provider_label(cfg.PROVIDER_MEXC),
            funding_rate=funding_rate,
            open_interest=open_interest,
        )
    except (Exception, AssertionError):
        return ""


def _fetch_gate_derivatives(
    config: cfg.Config,
    session: requests.Session,
) -> str:
    """Ambil Funding Rate & Open Interest (position_size) dari detail kontrak Gate.io Futures."""

    contract = cfg.provider_symbol(cfg.PROVIDER_GATE, config.symbol)
    path = GATE_CONTRACT_PATH_TEMPLATE.format(contract=contract)
    url = f"{cfg.GATE_HOST.rstrip('/')}{path}"
    try:
        payload = _get_json_once(
            session,
            url,
            {},
            timeout=config.request_timeout,
        )
        if not isinstance(payload, dict) or payload.get("label") is not None:
            return ""

        funding_rate = (
            payload.get("funding_rate")
            if payload.get("funding_rate") is not None
            else payload.get("fundingRate")
        )
        open_interest = (
            payload.get("position_size")
            if payload.get("position_size") is not None
            else payload.get("total_size", payload.get("openInterest"))
        )
        return format_derivatives_context_line(
            source_label=cfg.provider_label(cfg.PROVIDER_GATE),
            funding_rate=funding_rate,
            open_interest=open_interest,
        )
    except (Exception, AssertionError):
        return ""


def fetch_derivatives_context(
    config: cfg.Config,
    *,
    active_fetcher: Any = None,
    session: requests.Session | None = None,
    offline_mode: bool = False,
) -> str:
    """Ambil baris konteks derivatif dengan strategi Binance-First dan fallback anggun."""

    if offline_mode or config.provider not in cfg.CRYPTO_FAILOVER_CHAIN:
        return ""

    try:
        if active_fetcher is not None and hasattr(active_fetcher, "fetch_derivatives_info"):
            custom_info = active_fetcher.fetch_derivatives_info()
            if isinstance(custom_info, str) and custom_info.strip():
                return custom_info.strip()
            if isinstance(custom_info, dict):
                line = format_derivatives_context_line(
                    source_label=str(custom_info.get("source_label") or config.provider_label),
                    funding_rate=custom_info.get("funding_rate"),
                    open_interest=custom_info.get("open_interest"),
                    oi_delta_4h=custom_info.get("oi_delta_4h"),
                )
                if line:
                    return line

        http_session = session
        if http_session is None and active_fetcher is not None:
            http_session = getattr(active_fetcher, "_session", None) or getattr(
                getattr(active_fetcher, "_http", None), "session", None
            )
        if http_session is None:
            http_session = requests.Session()

        binance_line = _fetch_binance_derivatives(config, http_session)
        if binance_line:
            return binance_line

        if config.provider == cfg.PROVIDER_BITUNIX:
            return _fetch_bitunix_derivatives(config, http_session)
        if config.provider == cfg.PROVIDER_MEXC:
            return _fetch_mexc_derivatives(config, http_session)
        if config.provider == cfg.PROVIDER_GATE:
            return _fetch_gate_derivatives(config, http_session)
    except (Exception, AssertionError):
        return ""

    return ""

