"""Pengayaan konteks pasar tambahan (Derivatif & Acuan Struktur BTCUSDT) dengan strategi Binance-First.

Mengambil statistik derivatif (Funding Rate & Open Interest) serta acuan struktur
``BTCUSDT`` (untuk deteksi *SMT Divergence* pada altcoin) dari Binance Futures
terlebih dahulu sebagai acuan likuiditas global terdalam, lalu beralih (*fallback*)
secara anggun ke adapter provider kripto aktif (`bitunix`, `mexc`, `gate`) apabila Binance
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
_STANDARD_CRYPTO_ADAPTERS = (DataFetcher, BitunixSource, MexcSource, GateSource)


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


def _format_adapter_derivatives_info(
    info: Any,
    *,
    default_label: str,
) -> str:
    """Format payload hasil ``fetch_derivatives_info()`` dari adapter bursa menjadi satu baris."""

    if isinstance(info, str) and info.strip():
        return info.strip()
    if not isinstance(info, dict) or not info:
        return ""

    oi_delta = info.get("oi_delta_4h")
    if not oi_delta:
        prev_oi = info.get("prev_open_interest")
        latest_oi = info.get("latest_open_interest")
        if prev_oi is not None and latest_oi is not None:
            oi_delta = format_oi_delta(prev_oi, latest_oi)

    return format_derivatives_context_line(
        source_label=str(info.get("source_label") or default_label),
        funding_rate=info.get("funding_rate"),
        open_interest=info.get("open_interest"),
        oi_delta_4h=oi_delta,
    )


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
    """Ambil baris konteks derivatif dengan strategi Binance-First melalui adapter bursa."""

    if offline_mode or config.provider not in cfg.CRYPTO_FAILOVER_CHAIN:
        return ""

    try:
        # Jika active_fetcher adalah test double kustom yang menyediakan fetch_derivatives_info()
        if (
            active_fetcher is not None
            and not isinstance(active_fetcher, _STANDARD_CRYPTO_ADAPTERS)
            and hasattr(active_fetcher, "fetch_derivatives_info")
        ):
            line = _format_adapter_derivatives_info(
                active_fetcher.fetch_derivatives_info(),
                default_label=config.provider_label,
            )
            if line:
                return line

        http_session = _resolve_http_session(session, active_fetcher)

        # 1. Coba Binance terlebih dahulu (Binance-First) via DataFetcher adapter
        binance_fetcher = (
            active_fetcher
            if isinstance(active_fetcher, DataFetcher) and session is None
            else DataFetcher(
                replace(config, provider=cfg.PROVIDER_BINANCE),
                session=http_session,
                sleep=lambda _: None,
            )
        )
        binance_line = _format_adapter_derivatives_info(
            binance_fetcher.fetch_derivatives_info(),
            default_label=cfg.provider_label(cfg.PROVIDER_BINANCE),
        )
        if binance_line:
            return binance_line

        # 2. Fallback ke adapter bursa kripto aktif apabila bukan Binance
        fallback_adapter: Any = None
        if config.provider == cfg.PROVIDER_BITUNIX:
            fallback_adapter = (
                active_fetcher
                if isinstance(active_fetcher, BitunixSource) and session is None
                else BitunixSource(config, session=http_session, sleep=lambda _: None)
            )
        elif config.provider == cfg.PROVIDER_MEXC:
            fallback_adapter = (
                active_fetcher
                if isinstance(active_fetcher, MexcSource) and session is None
                else MexcSource(config, session=http_session, sleep=lambda _: None)
            )
        elif config.provider == cfg.PROVIDER_GATE:
            fallback_adapter = (
                active_fetcher
                if isinstance(active_fetcher, GateSource) and session is None
                else GateSource(config, session=http_session, sleep=lambda _: None)
            )

        if fallback_adapter is not None:
            return _format_adapter_derivatives_info(
                fallback_adapter.fetch_derivatives_info(),
                default_label=config.provider_label,
            )
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
