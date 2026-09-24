"""Bitunix Futures (USDT-M) public REST client (read-only market data only).

Endpoints (public, no API key required):
  * ``GET /api/v1/futures/market/kline``        — OHLCV candlestick data
  * ``GET /api/v1/futures/market/tickers``      — current market price / 24h stats
  * ``GET /api/v1/futures/market/trading_pairs``— symbol validation & precision

Contract: fetcher-parity with :class:`smc_prompt.data_fetcher.DataFetcher`
(``validate_symbol`` / ``fetch_klines`` / ``fetch_current_price`` /
``fetch_server_time`` / ``price_notes`` / ``with_now``), so ``cli.run`` drives
it through the unchanged analysis pipeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Sequence

import requests

from . import config as cfg
from .errors import NetworkError, SymbolNotFoundError
from .models import Candle
from .provider_base import (
    ProviderHttp,
    candle_delta,
    closed_ltf_fallback_price,
    ms_to_utc,
    parse_decimal,
    price_sanity_warning,
)

KLINES_PATH = "/api/v1/futures/market/kline"
TICKERS_PATH = "/api/v1/futures/market/tickers"
TRADING_PAIRS_PATH = "/api/v1/futures/market/trading_pairs"

#: Maximum candles per request supported by Bitunix Futures REST API.
_BITUNIX_PAGE_LIMIT = 200

#: Fallback close-time gap when the interval token is unrecognised.
_DEFAULT_DELTA = timedelta(hours=1)


class BitunixSource:
    """Fetches and normalizes Bitunix Futures market data for one symbol."""

    def __init__(
        self,
        config: cfg.Config,
        *,
        base_urls: Sequence[str] = (cfg.BITUNIX_HOST,),
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = None,  # type: ignore[assignment]
        rand: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._hosts = tuple(host.rstrip("/") for host in base_urls) or (
            cfg.BITUNIX_HOST,
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._price_notes: list[str] = []
        self._server_time_from_header: datetime | None = None

        http_kwargs: dict[str, Any] = {"session": session}
        if sleep is not None:
            http_kwargs["sleep"] = sleep
        if rand is not None:
            http_kwargs["rand"] = rand
        self._http = ProviderHttp(config, **http_kwargs)

    @property
    def price_notes(self) -> tuple[str, ...]:
        """Non-fatal notes recorded by the last :meth:`fetch_current_price`."""

        return tuple(self._price_notes)

    def with_now(self, moment: datetime) -> "BitunixSource":
        """Return an equivalent source bound to a fixed ``now`` instant."""

        return BitunixSource(
            self._config,
            base_urls=self._hosts,
            session=self._http.session,
            now=lambda: moment,
        )

    def _request(self, path: str, params: dict[str, Any], *, context: str) -> Any:
        """GET one Bitunix Futures endpoint and unwrap its envelope."""

        payload = self._http.get_json(self._hosts, path, params, context=context)

        if isinstance(payload, dict):
            code = payload.get("code")
            msg = payload.get("msg") or "unknown error"
            if code is not None and code != 0:
                if code in (2, 404) or "not found" in msg.lower() or "not exist" in msg.lower():
                    raise SymbolNotFoundError(
                        f"Bitunix Futures rejected the {context} request (code {code}): {msg}"
                    )
                raise NetworkError(
                    f"Bitunix Futures returned an error for {context} (code {code}): {msg}"
                )
        return payload

    def validate_symbol(self) -> dict[str, Any]:
        """Validate the symbol against Bitunix Futures pairs and derive tick size."""

        context = f"{self._config.symbol} trading pairs"
        payload = self._request(TRADING_PAIRS_PATH, {}, context=context)

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise NetworkError(
                f"Bitunix Futures returned invalid trading pairs data for {context}."
            )

        target = self._config.symbol.upper()
        matched = None
        for item in data:
            if isinstance(item, dict) and item.get("symbol", "").upper() == target:
                matched = item
                break

        if matched is None:
            raise SymbolNotFoundError(
                f"Symbol '{self._config.symbol}' is not available on Bitunix Futures. "
                f"Check the spelling (e.g. BTCUSDT, ETHUSDT)."
            )

        # Derive tickSize from quotePrecision
        # e.g. quotePrecision 1 -> 0.1, 2 -> 0.01, 4 -> 0.0001
        quote_precision = matched.get("quotePrecision")
        if isinstance(quote_precision, int) and quote_precision >= 0:
            tick_size = Decimal(10) ** -quote_precision
        else:
            tick_size = Decimal("0.01")

        return {
            "symbol": target,
            "status": "TRADING",
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "tickSize": str(tick_size),
                }
            ],
        }

    def _candle_from_row(
        self,
        row: dict[str, Any],
        *,
        canonical_interval: str,
        context: str,
    ) -> Candle:
        """Normalize one Bitunix kline entry into a :class:`Candle`."""

        raw_time = row.get("time")
        if raw_time is None:
            raise NetworkError(
                f"{context} returned a row without timestamp. No prompt generated."
            )

        open_time = ms_to_utc(raw_time)
        delta = candle_delta(canonical_interval, _DEFAULT_DELTA)
        close_time = open_time + delta

        return Candle(
            open_time=open_time,
            open=parse_decimal(row.get("open"), field="open", context=context),
            high=parse_decimal(row.get("high"), field="high", context=context),
            low=parse_decimal(row.get("low"), field="low", context=context),
            close=parse_decimal(row.get("close"), field="close", context=context),
            volume=parse_decimal(
                row.get("baseVol", row.get("quoteVol", "0")),
                field="volume",
                context=context,
            ),
            close_time=close_time,
            is_closed=self._now() >= close_time,
        )

    def fetch_klines(
        self, interval: str, limit: int, *, symbol: str | None = None
    ) -> list[Candle]:
        """Fetch ``limit`` candles for a canonical ``interval``.

        Paginates if ``limit > _BITUNIX_PAGE_LIMIT``. Reverses the newest-first
        Bitunix response into chronological order.
        """

        canonical = cfg.validate_interval(interval)
        bitunix_interval = cfg.provider_interval(cfg.PROVIDER_BITUNIX, canonical)
        target_symbol = (symbol or self._config.symbol).upper()
        context = f"{target_symbol} {interval} candles"

        all_rows: list[dict[str, Any]] = []
        needed = limit
        end_time: int | None = None

        while needed > 0:
            page_size = min(needed, _BITUNIX_PAGE_LIMIT)
            params: dict[str, Any] = {
                "symbol": target_symbol,
                "interval": bitunix_interval,
                "limit": page_size,
            }
            if end_time is not None:
                params["endTime"] = end_time

            payload = self._request(KLINES_PATH, params, context=context)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or not data:
                break

            all_rows.extend(data)
            needed -= len(data)

            if len(data) < page_size:
                # No more older data available
                break

            # Bitunix returns newest-first, so data[-1] is the oldest candle in this page
            try:
                oldest_time = int(data[-1].get("time", 0))
                if oldest_time <= 0:
                    break
                end_time = oldest_time - 1
            except (ValueError, TypeError):
                break

        if not all_rows:
            return []

        # Bitunix data is newest-first; reverse to chronological (oldest-first)
        chronological_rows = list(reversed(all_rows[-limit:] if len(all_rows) > limit else all_rows))

        candles: list[Candle] = []
        for row in chronological_rows:
            if isinstance(row, dict):
                candles.append(
                    self._candle_from_row(
                        row,
                        canonical_interval=canonical,
                        context=context,
                    )
                )

        return candles

    def fetch_current_price(
        self,
        symbol: str | None = None,
        *,
        reference_candle: Candle | None = None,
        tolerance: Decimal | None = None,
    ) -> Decimal:
        """Fetch the current market price from Bitunix Futures tickers."""

        target_symbol = (symbol or self._config.symbol).upper()
        context = f"{target_symbol} price"

        payload = self._request(
            TICKERS_PATH,
            {"symbols": target_symbol},
            context=context,
        )

        data = payload.get("data") if isinstance(payload, dict) else None
        price: Decimal | None = None

        if isinstance(data, list) and data:
            for item in data:
                if isinstance(item, dict) and item.get("symbol", "").upper() == target_symbol:
                    raw_price = item.get("lastPrice") or item.get("last") or item.get("markPrice")
                    if raw_price is not None:
                        price = parse_decimal(raw_price, field="lastPrice", context=context)
                        break

        if price is None or price <= 0:
            if reference_candle is not None:
                return closed_ltf_fallback_price(
                    [reference_candle], interval=self._config.ltf_interval
                )
            raise NetworkError(
                f"Could not retrieve current price for {target_symbol} from Bitunix Futures."
            )

        warning = price_sanity_warning(
            price,
            reference_candle,
            tolerance,
            symbol=target_symbol,
        )
        if warning:
            self._price_notes.append(warning)

        return price

    def fetch_server_time(self) -> datetime:
        """Return the current time.

        Uses host clock (consistently with TwelveData/OANDA).
        """

        return self._now()
