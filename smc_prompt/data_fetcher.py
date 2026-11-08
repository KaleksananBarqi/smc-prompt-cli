"""Binance Futures (USDT-M) public REST client (read-only market data only).

Endpoints (no API key, no signed/private routes, spec §4.1):
  * ``GET /fapi/v1/klines``                 — HTF daily / LTF hourly OHLCV
  * ``GET /fapi/v1/ticker/price``           — current price
  * ``GET /fapi/v1/exchangeInfo``           — symbol validation (+ tickSize, status)
  * ``GET /fapi/v1/time``                   — server time (clock-skew-safe closure)
  * ``GET /fapi/v1/premiumIndex``           — funding rate
  * ``GET /fapi/v1/openInterest``           — current open interest
  * ``GET /futures/data/openInterestHist``  — 4H open interest delta

Includes retry-with-backoff (spec §9.4) and base-URL failover via :class:`ProviderHttp`.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Sequence

import requests

from . import config as cfg
from .errors import NetworkError, SymbolNotFoundError
from .models import Candle
from .provider_base import (
    ProviderHttp,
    SymbolMetadata,
    extract_tick_size,
    get_json_once,
    ms_to_utc,
    price_sanity_warning,
    price_within_band,
)

PREMIUM_INDEX_PATH: str = "/fapi/v1/premiumIndex"
OPEN_INTEREST_PATH: str = "/fapi/v1/openInterest"
OPEN_INTEREST_HIST_PATH: str = "/futures/data/openInterestHist"

# Alias fungsi privat untuk kompatibilitas mundur apabila diimpor oleh kode/test lama
_ms_to_utc = ms_to_utc


class DataFetcher:
    """Fetches and normalizes Binance market data for one symbol."""

    def __init__(
        self,
        config: cfg.Config,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._sleep = sleep
        self._rand = rand
        #: ``now`` reference for the half-open-candle decision. The caller
        #: injects the Binance server time here (host-clock fallback) so a
        #: skewed local clock cannot leak a half-formed candle into analysis.
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._http = ProviderHttp(
            config,
            session=self._session,
            sleep=self._sleep,
            rand=self._rand,
        )
        self._active_base_url: str | None = None
        self._price_notes: list[str] = []

    @property
    def price_notes(self) -> tuple[str, ...]:
        """Non-fatal notes recorded by the last :meth:`fetch_current_price`."""

        return tuple(self._price_notes)

    def with_now(self, moment: datetime) -> "DataFetcher":
        """Return an equivalent fetcher bound to a fixed ``now`` instant.

        Rebuilds the fetcher with the same injectable seams (``session`` /
        ``sleep`` / ``rand``) and the supplied ``now`` so the caller can rebind
        the half-open-candle reference to the Binance server time without
        reaching into private state. Network behaviour is otherwise identical.
        """

        return DataFetcher(
            self._config,
            session=self._session,
            sleep=self._sleep,
            rand=self._rand,
            now=lambda: moment,
        )

    # ------------------------------------------------------------------
    # Internals (delegated to ProviderHttp)
    # ------------------------------------------------------------------

    def _request_json(self, path: str, params: dict[str, Any]) -> Any:
        """GET ``path`` with retry/backoff and base-URL failover via :class:`ProviderHttp`.

        Raises :class:`SymbolNotFoundError` for invalid-symbol responses and
        :class:`NetworkError` once every host/attempt pair is exhausted.
        """

        hosts: Sequence[str] = self._config.base_urls
        payload = self._http.get_json(
            hosts,
            path,
            params,
            context=f"{self._config.symbol} {path}",
            not_found_message=(
                f"Symbol '{self._config.symbol}' is not listed on "
                f"Binance Futures. Check the spelling (e.g. BTCUSDT)."
            ),
            unreachable_prefix="Binance API unreachable",
        )
        self._active_base_url = self._http.active_host
        return payload

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_base_url(self) -> str | None:
        """Host that served the last successful request."""

        return self._active_base_url or self._http.active_host

    def fetch_server_time(self) -> datetime:
        """Fetch the Binance server time (``GET /fapi/v1/time``).

        Using the exchange clock as the ``now`` reference keeps the half-open
        candle decision (and ``GENERATED_AT_UTC``) immune to a skewed host
        clock. Raises :class:`NetworkError` when the endpoint is unavailable or
        its payload is malformed; the caller falls back to the host clock.
        """

        payload = self._request_json(cfg.TIME_PATH, {})
        try:
            server_ms = payload["serverTime"]
        except (KeyError, TypeError) as exc:
            raise NetworkError(
                f"server time endpoint returned no serverTime field ({exc})."
            ) from exc
        return ms_to_utc(server_ms)

    def validate_symbol(self) -> SymbolMetadata:
        """Validate the symbol against ``exchangeInfo`` (spec §9.3).

        Returns a structured :class:`SymbolMetadata` (which also supports
        dict-style subscripting) so the caller can derive ``tickSize``
        precision and surface a non-TRADING ``status`` warning.
        """

        payload = self._request_json(
            cfg.EXCHANGE_INFO_PATH, {"symbol": self._config.symbol}
        )
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not symbols:
            raise SymbolNotFoundError(
                f"Symbol '{self._config.symbol}' is not listed on Binance "
                f"Futures. Check the spelling (e.g. BTCUSDT)."
            )
        matched = [s for s in symbols if s.get("symbol") == self._config.symbol]
        if not matched:
            raise SymbolNotFoundError(
                f"Symbol '{self._config.symbol}' is not listed on Binance "
                f"Futures. Check the spelling (e.g. BTCUSDT)."
            )
        entry = matched[0]
        raw_filters_list = entry.get("filters") if isinstance(entry, dict) else None
        raw_filters = (
            tuple(f for f in raw_filters_list if isinstance(f, dict))
            if isinstance(raw_filters_list, list)
            else ()
        )
        return SymbolMetadata(
            symbol=str(entry.get("symbol") or self._config.symbol),
            status=str(entry.get("status") or "TRADING"),
            tick_size=extract_tick_size(entry),
            raw_filters=raw_filters,
        )

    def fetch_klines(
        self, interval: str, limit: int, *, symbol: str | None = None
    ) -> list[Candle]:
        """Fetch ``limit`` klines for ``interval`` and normalize to Candle."""

        active_symbol = symbol or self._config.symbol
        raw = self._request_json(
            cfg.KLINES_PATH,
            {"symbol": active_symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(raw, list):
            raise NetworkError(
                f"Unexpected klines payload for {active_symbol} "
                f"{interval}. No prompt generated."
            )

        now = self._now()
        candles: list[Candle] = []
        for row in raw:
            close_time = ms_to_utc(row[6])
            is_closed = now >= close_time + timedelta(seconds=1)
            candles.append(
                Candle(
                    open_time=ms_to_utc(row[0]),
                    open=Decimal(str(row[1])),
                    high=Decimal(str(row[2])),
                    low=Decimal(str(row[3])),
                    close=Decimal(str(row[4])),
                    volume=Decimal(str(row[5])),
                    close_time=close_time,
                    is_closed=is_closed,
                )
            )
        return candles

    def _kline_fallback_price(self) -> Decimal:
        """Last *closed* LTF candle close (spec §4.3 fallback source)."""

        candles = self.fetch_klines(self._config.ltf_interval, cfg.MIN_CANDLES)
        closed = [c for c in candles if c.is_closed]
        if not closed:
            raise NetworkError(
                "Current price unavailable: ticker endpoint failed and no "
                "closed candle exists to derive a fallback price."
            )
        fallback = closed[-1].close
        if fallback <= 0:
            raise NetworkError(
                f"Fallback kline close is non-positive ({fallback}); "
                f"cannot determine a valid current price."
            )
        return fallback

    def fetch_current_price(
        self,
        *,
        reference_candle: Candle | None = None,
        tolerance: Decimal | None = None,
    ) -> Decimal:
        """Fetch the current price via ticker/price, falling back to kline close."""

        self._price_notes = []
        ticker_error: str | None = None
        try:
            payload = self._request_json(
                cfg.TICKER_PRICE_PATH, {"symbol": self._config.symbol}
            )
            raw_price = payload.get("price") if isinstance(payload, dict) else None
            if raw_price is None:
                raise NetworkError("ticker/price returned no price field.")
            value = Decimal(str(raw_price))
            if value <= 0:
                raise NetworkError(
                    f"ticker/price returned an invalid non-positive price "
                    f"({value})."
                )
            if not price_within_band(value, reference_candle, tolerance):
                raise NetworkError(
                    f"ticker/price {value} is outside the last closed candle "
                    f"range ± ATR."
                )
            return value
        except (NetworkError, SymbolNotFoundError) as exc:
            ticker_error = str(exc)

        fallback = self._kline_fallback_price()
        if ticker_error is not None:
            self._price_notes.append(
                f"Ticker price rejected ({ticker_error}) Using last closed "
                f"{self._config.ltf_interval} candle close "
                f"{cfg.fmt_price(fallback)} instead."
            )
        return fallback

    def fetch_derivatives_info(self) -> dict[str, Any]:
        """Ambil statistik derivatif (Funding Rate, Open Interest, dan Hist 4H) dari Binance Futures."""

        symbol = cfg.provider_symbol(cfg.PROVIDER_BINANCE, self._config.symbol)
        hosts = self._config.base_urls or cfg.DEFAULT_BASE_URLS
        timeout = self._config.request_timeout

        for host in hosts:
            base = host.rstrip("/")
            try:
                prem = get_json_once(
                    self._session,
                    f"{base}{PREMIUM_INDEX_PATH}",
                    {"symbol": symbol},
                    timeout=timeout,
                )
                if not isinstance(prem, dict) or "lastFundingRate" not in prem:
                    continue

                oi_payload = get_json_once(
                    self._session,
                    f"{base}{OPEN_INTEREST_PATH}",
                    {"symbol": symbol},
                    timeout=timeout,
                )
                if not isinstance(oi_payload, dict) or "openInterest" not in oi_payload:
                    continue

                prev_oi = None
                latest_oi = None
                try:
                    hist = get_json_once(
                        self._session,
                        f"{base}{OPEN_INTEREST_HIST_PATH}",
                        {"symbol": symbol, "period": "4h", "limit": 2},
                        timeout=timeout,
                    )
                    if isinstance(hist, list) and len(hist) >= 2:
                        prev_entry = hist[-2]
                        latest_entry = hist[-1]
                        if isinstance(prev_entry, dict) and isinstance(latest_entry, dict):
                            prev_oi = prev_entry.get("sumOpenInterest") or prev_entry.get("openInterest")
                            latest_oi = latest_entry.get("sumOpenInterest") or latest_entry.get("openInterest")
                except (Exception, AssertionError):
                    prev_oi = None
                    latest_oi = None

                return {
                    "source_label": cfg.provider_label(cfg.PROVIDER_BINANCE),
                    "funding_rate": prem.get("lastFundingRate"),
                    "open_interest": oi_payload.get("openInterest"),
                    "prev_open_interest": prev_oi,
                    "latest_open_interest": latest_oi,
                }
            except (Exception, AssertionError):
                continue

        return {}
