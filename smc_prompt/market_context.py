"""Pengayaan konteks pasar tambahan (Derivatif & Acuan Struktur BTCUSDT) dengan strategi Binance-First.

Mengambil statistik derivatif (Funding Rate & Open Interest) serta acuan struktur
``BTCUSDT`` (untuk deteksi *SMT Divergence* pada altcoin) dari Binance Futures
terlebih dahulu sebagai acuan likuiditas global terdalam, lalu beralih (*fallback*)
secara anggun ke provider kripto aktif (`bitunix`, `mexc`, `gate`) apabila Binance
tidak dapat dihubungi atau tidak mencantumkan simbol tersebut.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from . import config as cfg
from .bitunix_source import BitunixSource
from .data_fetcher import DataFetcher
from .gate_source import GateSource
from .mexc_source import MexcSource
from .models import SwingType
from .structure_analyzer import analyze

_BTC_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT", "BTCUSDC", "BTCUSD", "BTCBUSD"})

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


def format_btc_benchmark_line(
    *,
    source_label: str,
    btc_price: Decimal | float | int | str | None,
    htf_label: str,
    htf_structure: str,
    htf_recent_high: Decimal | float | int | str | None,
    htf_high_status: str,
    htf_recent_low: Decimal | float | int | str | None,
    htf_low_status: str,
    mtf_label: str,
    mtf_structure: str,
    price_format: cfg.PriceFormat | None = None,
) -> str:
    """Rakit baris tunggal ringkasan benchmark struktur BTCUSDT untuk evaluasi SMT Divergence."""

    price_dec = _to_decimal(btc_price)
    high_dec = _to_decimal(htf_recent_high)
    low_dec = _to_decimal(htf_recent_low)
    if price_dec is None or high_dec is None or low_dec is None:
        return ""

    pf = price_format or cfg.PriceFormat(tick_decimals=2)
    fmt_btc_price = pf.fmt(price_dec)
    fmt_high = pf.fmt(high_dec)
    fmt_low = pf.fmt(low_dec)
    return (
        f"- **Benchmark BTCUSDT ({source_label}):** {fmt_btc_price} "
        f"| HTF ({htf_label}): {htf_structure} "
        f"(Ref HIGH: {fmt_high} {htf_high_status}, Ref LOW: {fmt_low} {htf_low_status}) "
        f"| MTF ({mtf_label}): {mtf_structure}"
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


def _resolve_http_session(
    session: requests.Session | None,
    active_fetcher: Any = None,
) -> requests.Session:
    """Ambil instance requests.Session dari argumen eksplisit, active_fetcher, atau buat baru."""

    if session is not None:
        return session
    if active_fetcher is not None:
        extracted = getattr(active_fetcher, "_session", None) or getattr(
            getattr(active_fetcher, "_http", None), "session", None
        )
        if extracted is not None:
            return extracted
    return requests.Session()


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

        http_session = _resolve_http_session(session, active_fetcher)

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


def _is_btc_symbol(symbol: str) -> bool:
    """Kembalikan True jika simbol ternormalisasi sudah merupakan pasangan BTC."""

    normalized = symbol.upper().replace("_", "").replace("/", "").replace("-", "")
    return normalized in _BTC_SYMBOLS


def _build_btc_benchmark_from_fetcher(
    btc_config: cfg.Config,
    btc_fetcher: Any,
    *,
    source_label: str,
) -> str:
    """Ambil candle HTF & MTF serta harga terkini BTCUSDT dari ``btc_fetcher`` dan rakit baris benchmark."""

    htf_raw = btc_fetcher.fetch_klines(
        btc_config.htf_interval, btc_config.htf_fetch_limit
    )
    mtf_raw = btc_fetcher.fetch_klines(
        btc_config.mtf_interval, btc_config.mtf_fetch_limit
    )
    btc_price = btc_fetcher.fetch_current_price()

    htf_analysis, _, _ = analyze(
        htf_raw,
        timeframe=btc_config.htf_interval,
        requested=btc_config.htf_candles,
        current_price=btc_price,
        config=btc_config,
    )
    mtf_analysis, _, _ = analyze(
        mtf_raw,
        timeframe=btc_config.mtf_interval,
        requested=btc_config.mtf_candles,
        current_price=btc_price,
        config=btc_config,
    )

    highs = [s for s in htf_analysis.swings if s.type is SwingType.HIGH]
    lows = [s for s in htf_analysis.swings if s.type is SwingType.LOW]
    recent_high = highs[-1] if highs else htf_analysis.swing_high
    recent_low = lows[-1] if lows else htf_analysis.swing_low
    high_status = recent_high.status if highs else htf_analysis.swing_high_status
    low_status = recent_low.status if lows else htf_analysis.swing_low_status

    return format_btc_benchmark_line(
        source_label=source_label,
        btc_price=btc_price,
        htf_label=btc_config.htf_interval_label,
        htf_structure=htf_analysis.structure_class.value,
        htf_recent_high=recent_high.price,
        htf_high_status=high_status,
        htf_recent_low=recent_low.price,
        htf_low_status=low_status,
        mtf_label=btc_config.mtf_interval_label,
        mtf_structure=mtf_analysis.structure_class.value,
        price_format=btc_config.price_format,
    )


def fetch_btc_benchmark_context(
    config: cfg.Config,
    *,
    active_fetcher: Any = None,
    session: requests.Session | None = None,
    offline_mode: bool = False,
) -> str:
    """Ambil baris acuan struktur BTCUSDT untuk deteksi SMT Divergence pada altcoin."""

    if (
        offline_mode
        or config.provider not in cfg.CRYPTO_FAILOVER_CHAIN
        or _is_btc_symbol(config.symbol)
    ):
        return ""

    try:
        if active_fetcher is not None and hasattr(
            active_fetcher, "fetch_btc_benchmark_info"
        ):
            custom_info = active_fetcher.fetch_btc_benchmark_info()
            if isinstance(custom_info, str) and custom_info.strip():
                return custom_info.strip()
            if isinstance(custom_info, dict):
                line = format_btc_benchmark_line(
                    source_label=str(
                        custom_info.get("source_label") or config.provider_label
                    ),
                    btc_price=custom_info.get("btc_price"),
                    htf_label=str(
                        custom_info.get("htf_label") or config.htf_interval_label
                    ),
                    htf_structure=str(custom_info.get("htf_structure") or ""),
                    htf_recent_high=custom_info.get("htf_recent_high"),
                    htf_high_status=str(
                        custom_info.get("htf_high_status") or "untested"
                    ),
                    htf_recent_low=custom_info.get("htf_recent_low"),
                    htf_low_status=str(
                        custom_info.get("htf_low_status") or "untested"
                    ),
                    mtf_label=str(
                        custom_info.get("mtf_label") or config.mtf_interval_label
                    ),
                    mtf_structure=str(custom_info.get("mtf_structure") or ""),
                )
                if line:
                    return line

        http_session = _resolve_http_session(session, active_fetcher)

        now_fn = (
            getattr(active_fetcher, "_now", None)
            if active_fetcher is not None
            and callable(getattr(active_fetcher, "_now", None))
            else None
        )

        btc_config = replace(
            config,
            symbol="BTCUSDT",
            price_format=cfg.PriceFormat(tick_decimals=2),
            retry_max=1,
        )

        # 1. Coba Binance terlebih dahulu (Binance-First)
        try:
            binance_cfg = replace(btc_config, provider=cfg.PROVIDER_BINANCE)
            binance_fetcher = DataFetcher(
                binance_cfg,
                session=http_session,
                sleep=lambda _: None,
                now=now_fn,
            )
            line = _build_btc_benchmark_from_fetcher(
                binance_cfg,
                binance_fetcher,
                source_label=cfg.provider_label(cfg.PROVIDER_BINANCE),
            )
            if line:
                return line
        except (Exception, AssertionError):
            pass

        # 2. Fallback ke penyedia kripto aktif apabila bukan Binance
        if config.provider == cfg.PROVIDER_BITUNIX:
            fallback_fetcher: Any = BitunixSource(
                btc_config,
                session=http_session,
                sleep=lambda _: None,
                now=now_fn,
            )
        elif config.provider == cfg.PROVIDER_MEXC:
            fallback_fetcher = MexcSource(
                btc_config,
                session=http_session,
                sleep=lambda _: None,
                now=now_fn,
            )
        elif config.provider == cfg.PROVIDER_GATE:
            fallback_fetcher = GateSource(
                btc_config,
                session=http_session,
                sleep=lambda _: None,
                now=now_fn,
            )
        else:
            return ""

        return _build_btc_benchmark_from_fetcher(
            btc_config,
            fallback_fetcher,
            source_label=cfg.provider_label(config.provider),
        )
    except (Exception, AssertionError):
        return ""


