"""Modul akuisisi data pasar dan rantai failover otomatis (Crypto Failover Chain).

Mengenkapsulasi seluruh siklus penarikan data dari exchange atau file CSV:
  1. Resolusi urutan kandidat provider dalam rantai failover kripto dan
     validasi kompatibilitas interval per kandidat.
  2. Pembuatan instance sumber data melalui seam adapter ``SourceFactory``.
  3. Validasi simbol, ekstraksi ``tickSize`` ke ``PriceFormat``, dan
     sinkronisasi jam server (dengan fallback ke jam host bila endpoint waktu gagal).
  4. Penarikan klines multi-timeframe (``prompt``) maupun single-timeframe
     (``validate_setup`` dan ``candles_only``), termasuk pemotongan closed
     candles dan pembangunan blok konteks struktur Layer-A otomatis.
  5. Pengayaan konteks pasar tambahan (funding rate, open interest, dan
     benchmark BTCUSDT) dengan strategi Binance-First yang fail-open.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Literal, Protocol

from . import config as cfg
from .bitunix_source import BitunixSource
from .csv_source import LocalCsvSource
from .data_fetcher import DataFetcher
from .errors import ConfigError, NetworkError, SymbolStatusWarning
from .gate_source import GateSource
from .market_context import fetch_btc_benchmark_context, fetch_derivatives_context
from .mexc_source import MexcSource
from .models import Candle
from .oanda_source import OandaSource
from .provider_base import price_sanity_warning
from .structure_analyzer import analyze, compute_atr
from .template_renderer import render_structure_context_block
from .twelvedata_source import TwelveDataSource

AcquisitionMode = Literal["prompt", "validate_setup", "candles_only"]


class SourceFactory(Protocol):
    """Kontrak seam adapter untuk membangun instance sumber data provider."""

    def __call__(
        self,
        provider_name: str,
        active_config: cfg.Config,
        *,
        offline_mode: bool,
        offline_htf: str | None,
        offline_mtf: str | None,
        offline_ltf: str | None,
        key: str | None,
        token: str | None,
        account_id: str | None,
        oanda_env: str,
    ) -> Any: ...


@dataclass(frozen=True)
class MarketSnapshot:
    """Snapshot lengkap data pasar yang telah divalidasi, dipotong, dan diperkaya."""

    mode: AcquisitionMode
    fetcher: Any
    active_config: cfg.Config
    server_time: datetime | None
    generated_at: datetime
    warnings: tuple[str, ...]

    # Field untuk mode single-timeframe ('candles_only' & 'validate_setup')
    raw_candles: tuple[Candle, ...] = ()
    closed_candles: tuple[Candle, ...] = ()
    selected_candles: tuple[Candle, ...] = ()
    structure_block: str | None = None

    # Field untuk mode multi-timeframe ('prompt')
    htf_raw: tuple[Candle, ...] = ()
    mtf_raw: tuple[Candle, ...] = ()
    ltf_raw: tuple[Candle, ...] = ()
    ltf_atr: Decimal | None = None
    reference_candle: Candle | None = None
    current_price: Decimal | None = None

    # Konteks pasar tambahan (Binance-First) untuk 'prompt' & 'validate_setup'
    derivatives_context_line: str = ""
    btc_benchmark_line: str = ""


def resolve_mode_intervals(
    htf_interval: str,
    mtf_interval: str,
    ltf_interval: str,
    *,
    target_interval: str | None = None,
) -> tuple[str, str, str]:
    """Menyesuaikan trio interval agar mencakup ``target_interval`` tanpa duplikasi.

    Digunakan pada mode single-timeframe (``--validate-setup`` dan ``--candles-only``)
    ketika ``target_interval`` berada di luar trio ``(htf_interval, mtf_interval, ltf_interval)``
    agar ``cfg.build_config()`` dan ``LocalCsvSource`` dapat melayani interval tersebut.
    """

    if target_interval is None or target_interval in (
        htf_interval,
        mtf_interval,
        ltf_interval,
    ):
        return htf_interval, mtf_interval, ltf_interval

    new_ltf = target_interval
    new_mtf = mtf_interval
    new_htf = htf_interval
    if new_ltf == new_mtf:
        new_mtf = "4h" if new_ltf != "4h" else "2h"
    if new_ltf == new_htf:
        new_htf = "1d" if new_ltf != "1d" else "1w"
    return new_htf, new_mtf, new_ltf


def extract_tick_size(entry: dict) -> str | None:
    """Mengekstrak ``PRICE_FILTER.tickSize`` dari respons metadata simbol exchange."""

    filters = entry.get("filters") if isinstance(entry, dict) else None
    if not isinstance(filters, list):
        return None
    for item in filters:
        if isinstance(item, dict) and item.get("filterType") == "PRICE_FILTER":
            tick = item.get("tickSize")
            return str(tick) if tick is not None else None
    return None


def make_provider_source(
    config: cfg.Config,
    *,
    twelvedata_key: str | None,
    oanda_token: str | None,
    oanda_account_id: str | None,
    oanda_env: str,
) -> TwelveDataSource | OandaSource | BitunixSource | MexcSource | GateSource:
    """Membangun sumber data jaringan non-Binance berdasarkan ``config.provider``."""

    if config.provider == cfg.PROVIDER_BITUNIX:
        return BitunixSource(config)
    if config.provider == cfg.PROVIDER_MEXC:
        return MexcSource(config)
    if config.provider == cfg.PROVIDER_GATE:
        return GateSource(config)
    if config.provider == cfg.PROVIDER_TWELVEDATA:
        return TwelveDataSource(config, api_key=twelvedata_key or "")
    if config.provider == cfg.PROVIDER_OANDA:
        return OandaSource(
            config,
            token=oanda_token or "",
            environment=oanda_env,
            account_id=oanda_account_id,
        )
    raise ConfigError(f"Unsupported provider '{config.provider}'.")


def build_provider_fetcher(
    provider_name: str,
    active_config: cfg.Config,
    *,
    offline_mode: bool,
    offline_htf: str | None,
    offline_mtf: str | None,
    offline_ltf: str | None,
    key: str | None,
    token: str | None,
    account_id: str | None,
    oanda_env: str,
) -> (
    DataFetcher
    | LocalCsvSource
    | TwelveDataSource
    | OandaSource
    | BitunixSource
    | MexcSource
    | GateSource
):
    """Implementasi default ``SourceFactory`` untuk membuat instance fetcher."""

    if offline_mode:
        return LocalCsvSource(
            active_config,
            htf_file=offline_htf,
            mtf_file=offline_mtf,
            ltf_file=offline_ltf,
        )
    if provider_name == cfg.PROVIDER_BINANCE:
        return DataFetcher(active_config)
    return make_provider_source(
        active_config,
        twelvedata_key=key,
        oanda_token=token,
        oanda_account_id=account_id,
        oanda_env=oanda_env,
    )


def _supports_auxiliary_context(
    fetcher: Any,
    config: cfg.Config,
    *,
    offline_mode: bool,
) -> bool:
    """Memeriksa apakah fetcher dan konfigurasi mendukung pengayaan konteks derivatif/BTC."""

    if offline_mode or config.provider not in cfg.CRYPTO_FAILOVER_CHAIN:
        return False
    return (
        isinstance(fetcher, (DataFetcher, BitunixSource, MexcSource, GateSource))
        or hasattr(fetcher, "fetch_derivatives_info")
        or hasattr(fetcher, "fetch_btc_benchmark_info")
    )


def _enrich_crypto_context(
    config: cfg.Config,
    fetcher: Any,
    *,
    offline_mode: bool,
) -> tuple[str, str]:
    """Mengambil baris konteks derivatif dan benchmark BTCUSDT secara fail-open."""

    if not _supports_auxiliary_context(fetcher, config, offline_mode=offline_mode):
        return "", ""

    deriv_line = ""
    btc_line = ""
    try:
        deriv_line = fetch_derivatives_context(
            config,
            active_fetcher=fetcher,
            offline_mode=offline_mode,
        )
    except (Exception, AssertionError):
        deriv_line = ""

    try:
        btc_line = fetch_btc_benchmark_context(
            config,
            active_fetcher=fetcher,
            offline_mode=offline_mode,
        )
    except (Exception, AssertionError):
        btc_line = ""

    return deriv_line, btc_line


def _build_single_tf_structure_block(
    closed_candles: list[Candle],
    *,
    interval: str,
    config: cfg.Config,
    no_structure: bool,
) -> str | None:
    """Membangun blok konteks struktur Layer-A untuk mode single-timeframe bila memungkinkan."""

    if no_structure or len(closed_candles) < 25:
        return None
    try:
        curr_p = closed_candles[-1].close
        struct_res, _, _ = analyze(
            closed_candles,
            timeframe=interval,
            requested=min(len(closed_candles), 40),
            current_price=curr_p,
            config=config,
        )
        return render_structure_context_block(
            struct_res,
            timeframe=interval,
            current_price=curr_p,
            config=config,
        )
    except Exception:
        return None


def acquire_market_snapshot(
    config: cfg.Config,
    *,
    mode: AcquisitionMode = "prompt",
    offline_mode: bool = False,
    offline_htf: str | None = None,
    offline_mtf: str | None = None,
    offline_ltf: str | None = None,
    no_fallback: bool = False,
    no_structure: bool = False,
    single_interval: str | None = None,
    single_candles: int | None = None,
    key: str | None = None,
    token: str | None = None,
    account_id: str | None = None,
    oanda_env: str = cfg.OANDA_ENV_PRACTICE,
    source_factory: SourceFactory | None = None,
    warn_callback: Callable[[str], None] | None = None,
) -> MarketSnapshot:
    """Menjalankan akuisisi data pasar lengkap beserta failover, pemotongan candle, dan pengayaan."""

    factory = source_factory or build_provider_fetcher
    warnings: list[str] = []

    def _emit_realtime_warn(msg: str) -> None:
        if warn_callback is not None:
            warn_callback(msg)

    # 1. Tentukan kandidat provider dalam rantai failover
    if offline_mode or no_fallback or config.provider not in cfg.CRYPTO_FAILOVER_CHAIN:
        candidates = [config.provider]
    else:
        start_idx = cfg.CRYPTO_FAILOVER_CHAIN.index(config.provider)
        candidates = list(cfg.CRYPTO_FAILOVER_CHAIN[start_idx:])

    last_network_exc: NetworkError | None = None
    selected_fetcher: Any = None
    resolved_config: cfg.Config | None = None
    resolved_server_time: datetime | None = None
    raw_single_candles: list[Candle] = []
    htf_raw: list[Candle] = []
    mtf_raw: list[Candle] = []
    ltf_raw: list[Candle] = []
    ltf_atr: Decimal | None = None
    reference_candle: Candle | None = None
    current_price: Decimal | None = None

    for candidate_idx, candidate_provider in enumerate(candidates):
        is_fallback = candidate_provider != config.provider
        active_config = replace(config, provider=candidate_provider)

        # Validasi kompatibilitas interval pada kandidat exchange
        if mode in ("candles_only", "validate_setup"):
            assert single_interval is not None
            needed_intervals = [single_interval]
        else:
            needed_intervals = [
                active_config.htf_interval,
                active_config.mtf_interval,
                active_config.ltf_interval,
            ]

        unsupported_interval: str | None = None
        for intv in needed_intervals:
            try:
                cfg.provider_interval(candidate_provider, intv)
            except ConfigError:
                unsupported_interval = intv
                break

        if unsupported_interval is not None:
            if is_fallback and candidate_idx + 1 < len(candidates):
                next_p = candidates[candidate_idx + 1]
                skip_msg = (
                    f"{cfg.provider_label(candidate_provider)} tidak mendukung interval '{unsupported_interval}'. "
                    f"Melewati dan beralih ke {cfg.provider_label(next_p)}..."
                )
                warnings.append(skip_msg)
                _emit_realtime_warn(skip_msg)
                continue
            raise ConfigError(
                f"Provider '{cfg.provider_label(candidate_provider)}' does not support the "
                f"interval '{unsupported_interval}'."
            )

        fetcher = factory(
            candidate_provider,
            active_config,
            offline_mode=offline_mode,
            offline_htf=offline_htf,
            offline_mtf=offline_mtf,
            offline_ltf=offline_ltf,
            key=key,
            token=token,
            account_id=account_id,
            oanda_env=oanda_env,
        )

        try:
            # Validasi simbol & ekstraksi tickSize ke PriceFormat
            symbol_entry = fetcher.validate_symbol()
            tick_size = extract_tick_size(symbol_entry)
            active_config = replace(
                active_config,
                price_format=cfg.PriceFormat(cfg.decimals_from_tick_size(tick_size)),
            )

            status = symbol_entry.get("status") if isinstance(symbol_entry, dict) else None
            candidate_warnings: list[str] = []
            if isinstance(status, str) and status and status != "TRADING":
                candidate_warnings.append(
                    SymbolStatusWarning(active_config.symbol, status).message()
                )

            # Sinkronisasi jam server
            server_time: datetime | None = None
            if offline_mode:
                server_time = fetcher.fetch_server_time()
            else:
                try:
                    server_time = fetcher.fetch_server_time()
                except NetworkError as exc:
                    _emit_realtime_warn(
                        f"{active_config.provider_label} server time unavailable ({exc}); "
                        f"falling back to the host clock for candle-closure and "
                        f"GENERATED_AT_UTC."
                    )
            if server_time is not None:
                fetcher = fetcher.with_now(server_time)

            # Penarikan candle & harga sesuai mode
            if mode in ("candles_only", "validate_setup"):
                assert single_interval is not None
                assert single_candles is not None
                fetch_limit = min(
                    max(single_candles + 20, 100)
                    if not no_structure
                    else single_candles + 20,
                    cfg.FETCH_LIMIT_MAX,
                )
                raw_single_candles = fetcher.fetch_klines(single_interval, fetch_limit)
            else:
                htf_raw = fetcher.fetch_klines(
                    active_config.htf_interval, active_config.htf_fetch_limit
                )
                mtf_raw = fetcher.fetch_klines(
                    active_config.mtf_interval, active_config.mtf_fetch_limit
                )
                ltf_raw = fetcher.fetch_klines(
                    active_config.ltf_interval, active_config.ltf_fetch_limit
                )

                ltf_closed = [c for c in ltf_raw if c.is_closed]
                ltf_atr = None
                if len(ltf_closed) >= active_config.atr_period + 1:
                    ltf_atr = compute_atr(ltf_closed, active_config.atr_period)
                reference_candle = ltf_closed[-1] if ltf_closed else None

                current_price = fetcher.fetch_current_price(
                    reference_candle=reference_candle, tolerance=ltf_atr
                )

            if is_fallback:
                warn_msg = (
                    f"Koneksi jaringan ke {cfg.provider_label(config.provider)} gagal. "
                    f"Berhasil beralih otomatis ke {cfg.provider_label(candidate_provider)}."
                )
                warnings.append(warn_msg)

            warnings.extend(candidate_warnings)
            selected_fetcher = fetcher
            resolved_config = active_config
            resolved_server_time = server_time
            break

        except NetworkError as exc:
            last_network_exc = exc
            if candidate_idx + 1 < len(candidates):
                next_p = candidates[candidate_idx + 1]
                _emit_realtime_warn(
                    f"Koneksi jaringan ke {cfg.provider_label(candidate_provider)} gagal ({exc}). "
                    f"Mengalihkan otomatis ke {cfg.provider_label(next_p)}..."
                )
                continue
            raise

    if resolved_config is None or selected_fetcher is None:
        if last_network_exc is not None:
            raise last_network_exc
        raise NetworkError("Semua exchange dalam rantai failover tidak dapat dihubungi.")

    generated_at = resolved_server_time or datetime.now(timezone.utc)

    # Proses pasca-penarikan untuk mode single-timeframe ('candles_only' & 'validate_setup')
    if mode in ("candles_only", "validate_setup"):
        assert single_interval is not None
        assert single_candles is not None

        for message in warnings:
            _emit_realtime_warn(message)

        closed_candles = [c for c in raw_single_candles if c.is_closed]
        if len(closed_candles) < single_candles:
            warn_msg = (
                f"Only {len(closed_candles)} closed {single_interval} candles "
                f"available (requested {single_candles}). "
                f"Reduced table to {len(closed_candles)}."
            )
            warnings.append(warn_msg)
            _emit_realtime_warn(warn_msg)

        selected_candles = (
            closed_candles[-single_candles:]
            if len(closed_candles) >= single_candles
            else closed_candles
        )
        if not selected_candles:
            raise ConfigError(
                f"No closed candles available for {resolved_config.symbol} at {single_interval}."
            )

        structure_block = _build_single_tf_structure_block(
            closed_candles,
            interval=single_interval,
            config=resolved_config,
            no_structure=no_structure,
        )

        deriv_line = ""
        btc_line = ""
        if mode == "validate_setup":
            deriv_line, btc_line = _enrich_crypto_context(
                resolved_config,
                selected_fetcher,
                offline_mode=offline_mode,
            )

        return MarketSnapshot(
            mode=mode,
            fetcher=selected_fetcher,
            active_config=resolved_config,
            server_time=resolved_server_time,
            generated_at=generated_at,
            warnings=tuple(warnings),
            raw_candles=tuple(raw_single_candles),
            closed_candles=tuple(closed_candles),
            selected_candles=tuple(selected_candles),
            structure_block=structure_block,
            derivatives_context_line=deriv_line,
            btc_benchmark_line=btc_line,
        )

    # Proses pasca-penarikan untuk mode multi-timeframe ('prompt')
    price_notes = getattr(selected_fetcher, "price_notes", ())
    if price_notes:
        warnings.extend(price_notes)

    assert current_price is not None
    price_warning = price_sanity_warning(
        current_price,
        reference_candle,
        ltf_atr,
        symbol=resolved_config.symbol,
        price_format=resolved_config.price_format,
    )
    if price_warning is not None:
        warnings.append(price_warning)

    deriv_line, btc_line = _enrich_crypto_context(
        resolved_config,
        selected_fetcher,
        offline_mode=offline_mode,
    )

    return MarketSnapshot(
        mode="prompt",
        fetcher=selected_fetcher,
        active_config=resolved_config,
        server_time=resolved_server_time,
        generated_at=generated_at,
        warnings=tuple(warnings),
        htf_raw=tuple(htf_raw),
        mtf_raw=tuple(mtf_raw),
        ltf_raw=tuple(ltf_raw),
        ltf_atr=ltf_atr,
        reference_candle=reference_candle,
        current_price=current_price,
        derivatives_context_line=deriv_line,
        btc_benchmark_line=btc_line,
    )
