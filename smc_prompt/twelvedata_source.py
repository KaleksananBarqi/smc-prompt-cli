"""Twelve Data provider (spot FX / metals — makes XAUUSD analyzable).

Why this exists: Binance Spot lists no fiat/forex/metal instruments, so
``exchangeInfo``/``klines`` can never serve ``XAUUSD``. The tokenized-gold
proxies on Binance (``XAUTUSDT``, ``PAXGUSDT``) are **not** spot gold: they
carry a persistent peg premium/discount, a crypto order-book microstructure and
a 24/7 session with no weekend gap, all of which fabricate swings and FVGs that
do not exist on the gold market. Twelve Data serves the real ``XAU/USD`` pair,
natively at ``1d`` / ``4h`` / ``1h``, which is why the default trio maps 1:1 and
no aggregation is needed here.

Contract: fetcher-parity with :class:`smc_prompt.data_fetcher.DataFetcher`
(``validate_symbol`` / ``fetch_klines`` / ``fetch_current_price`` /
``fetch_server_time`` / ``price_notes`` / ``with_now``), so ``cli.run`` drives
it through the unchanged analysis pipeline.

Twelve Data API notes
---------------------
* ``GET /time_series`` returns ``values`` **newest-first** — reversed here to
  the chronological order the analyzer requires.
* The FX/metal feed reports ``volume`` as ``"0"``. Rather than emit a
  meaningless relative volume (and trip the zero-volume delisted heuristic on
  every run), the provider declares ``volume_available=False`` through
  ``config.volume_available``, so the volume facts render ``n/a``.
* ``datetime`` is ``YYYY-MM-DD`` for ``1day`` and ``YYYY-MM-DD HH:MM:SS`` for
  intraday; ``timezone=UTC`` is requested explicitly so no local offset leaks
  into the swing timestamps.
* There is no exchange clock endpoint, so ``fetch_server_time`` returns the
  host clock (documented, and not a warning condition).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Sequence

import requests

from . import config as cfg
from .errors import ConfigError, NetworkError, SymbolNotFoundError
from .models import Candle
from .provider_base import (
    ProviderHttp,
    candle_delta,
    closed_ltf_fallback_price,
    iso_to_utc,
    parse_decimal,
    price_within_band,
)

TIME_SERIES_PATH = "/time_series"
QUOTE_PATH = "/quote"

#: Fallback close-time gap when the interval token is unrecognised.
_DEFAULT_DELTA = timedelta(hours=1)

#: Binance-shaped synthetic ``PRICE_FILTER`` entry. Providers have no
#: ``exchangeInfo``, so they synthesise the shape ``cli._extract_tick_size``
#: already understands. This is what lets the CLI stay provider-agnostic.
_SYNTHETIC_TICK = Decimal("0.01")


class TwelveDataSource:
    """Fetches and normalizes Twelve Data candles for one symbol."""

    def __init__(
        self,
        config: cfg.Config,
        *,
        api_key: str,
        base_urls: Sequence[str] = (cfg.TWELVEDATA_HOST,),
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = None,  # type: ignore[assignment]
        rand: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ConfigError(
                "Twelve Data requires an API key. Pass --twelvedata-key or set "
                "the TWELVEDATA_API_KEY environment variable."
            )
        self._config = config
        self._api_key = api_key.strip()
        self._hosts = tuple(host.rstrip("/") for host in base_urls) or (
            cfg.TWELVEDATA_HOST,
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._price_notes: list[str] = []

        http_kwargs: dict[str, Any] = {"session": session}
        if sleep is not None:
            http_kwargs["sleep"] = sleep
        if rand is not None:
            http_kwargs["rand"] = rand
        self._http = ProviderHttp(config, **http_kwargs)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @property
    def price_notes(self) -> tuple[str, ...]:
        """Non-fatal notes recorded by the last :meth:`fetch_current_price`."""

        return tuple(self._price_notes)

    def with_now(self, moment: datetime) -> "TwelveDataSource":
        """Return an equivalent source bound to a fixed ``now`` instant."""

        return TwelveDataSource(
            self._config,
            api_key=self._api_key,
            base_urls=self._hosts,
            session=self._http.session,
            now=lambda: moment,
        )

    def _request(self, path: str, params: dict[str, Any], *, context: str) -> Any:
        """GET one Twelve Data endpoint and unwrap its error envelope.

        Twelve Data reports application-level failures with HTTP 200 and a
        ``status: "error"`` body, so the envelope is inspected explicitly and
        mapped onto the friendly :class:`SymbolNotFoundError`.
        """

        query = dict(params)
        query["apikey"] = self._api_key
        query["format"] = "JSON"
        payload = self._http.get_json(self._hosts, path, query, context=context)

        if isinstance(payload, dict):
            status = payload.get("status")
            code = payload.get("code")
            if status == "error" or (code is not None and code != 200):
                message = payload.get("message") or "unknown error"
                raise SymbolNotFoundError(
                    f"Twelve Data rejected the {context} request "
                    f"(code {code}): {message}"
                )
        return payload

    def _candle_from_row(
        self,
        row: dict[str, Any],
        *,
        canonical_interval: str,
        context: str,
    ) -> Candle:
        """Normalize one ``values`` entry into a :class:`Candle`."""

        raw_stamp = row.get("datetime")
        if not raw_stamp:
            raise NetworkError(
                f"{context} returned a row without a datetime. "
                f"No prompt generated."
            )
        try:
            open_time = iso_to_utc(str(raw_stamp))
        except ValueError as exc:
            raise NetworkError(
                f"{context} returned an unparseable datetime "
                f"'{raw_stamp}' ({exc}). No prompt generated."
            ) from exc

        delta = candle_delta(canonical_interval, _DEFAULT_DELTA)
        close_time = open_time + delta

        return Candle(
            open_time=open_time,
            open=parse_decimal(row.get("open"), field="open", context=context),
            high=parse_decimal(row.get("high"), field="high", context=context),
            low=parse_decimal(row.get("low"), field="low", context=context),
            close=parse_decimal(row.get("close"), field="close", context=context),
            volume=parse_decimal(
                row.get("volume", "0"), field="volume", context=context
            ),
            close_time=close_time,
            is_closed=self._now() >= close_time + timedelta(seconds=1),
        )

    # ------------------------------------------------------------------
    # Public API (fetcher parity)
    # ------------------------------------------------------------------

    def validate_symbol(self) -> dict[str, Any]:
        """Validate the pair and return a Binance-shaped synthetic entry.

        The entry carries a ``PRICE_FILTER.tickSize`` so
        ``cli._extract_tick_size`` derives the price precision through the same
        path Binance uses (``XAU/USD`` -> ``0.01`` -> 2 dp), rather than falling
        back to the magnitude rule.
        """

        provider_symbol = cfg.provider_symbol(
            cfg.PROVIDER_TWELVEDATA, self._config.symbol
        )
        payload = self._request(
            QUOTE_PATH,
            {"symbol": provider_symbol},
            context=f"{provider_symbol} quote",
        )
        if not isinstance(payload, dict) or payload.get("symbol") is None:
            raise SymbolNotFoundError(
                f"Symbol '{self._config.symbol}' (as '{provider_symbol}') is "
                f"not available on Twelve Data. Check the spelling (for metals "
                f"use XAUUSD; it maps to XAU/USD)."
            )
        return {
            "symbol": provider_symbol,
            "status": "TRADING",
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "tickSize": str(_SYNTHETIC_TICK),
                }
            ],
        }

    def fetch_klines(
        self, interval: str, limit: int, *, symbol: str | None = None
    ) -> list[Candle]:
        """Fetch ``limit`` candles for a canonical ``interval``.

        The canonical token is translated to Twelve Data's ``interval``
        parameter (``4h`` -> ``4h``, ``1d`` -> ``1day``) and the newest-first
        payload is reversed into chronological order.
        """

        canonical = cfg.validate_interval(interval)
        provider_interval = cfg.provider_interval(
            cfg.PROVIDER_TWELVEDATA, canonical
        )
        provider_symbol = cfg.provider_symbol(
            cfg.PROVIDER_TWELVEDATA, symbol or self._config.symbol
        )
        context = f"{provider_symbol} {canonical} candles"

        payload = self._request(
            TIME_SERIES_PATH,
            {
                "symbol": provider_symbol,
                "interval": provider_interval,
                "outputsize": limit,
                "timezone": "UTC",
                "order": "ASC",
            },
            context=context,
        )
        if not isinstance(payload, dict):
            raise NetworkError(
                f"Unexpected {context} payload. No prompt generated."
            )
        values = payload.get("values")
        if not isinstance(values, list) or not values:
            raise NetworkError(
                f"Twelve Data returned no {context}. No prompt generated."
            )

        rows = [row for row in values if isinstance(row, dict)]
        # ``order=ASC`` is requested, but the API silently returns newest-first
        # when it ignores the parameter, so the order is verified and normalized
        # rather than trusted.
        candles = [
            self._candle_from_row(
                row, canonical_interval=canonical, context=context
            )
            for row in rows
        ]
        candles.sort(key=lambda candle: candle.open_time)
        return candles

    def fetch_current_price(
        self,
        *,
        reference_candle: Candle | None = None,
        tolerance: Decimal | None = None,
    ) -> Decimal:
        """Fetch the live price via ``/quote`` with a closed-candle fallback.

        Sanity rules mirror the Binance fetcher: a non-positive value, or one
        outside the last closed candle's ``[low, high] ± ATR`` band, is rejected
        in favour of the fallback, with the reason recorded in
        :attr:`price_notes` (emitted as a non-fatal WARN).
        """

        self._price_notes = []
        provider_symbol = cfg.provider_symbol(
            cfg.PROVIDER_TWELVEDATA, self._config.symbol
        )
        ticker_error: str | None = None
        try:
            payload = self._request(
                QUOTE_PATH,
                {"symbol": provider_symbol},
                context=f"{provider_symbol} quote",
            )
            raw_price = (
                payload.get("close") if isinstance(payload, dict) else None
            )
            if raw_price is None:
                raise NetworkError("Twelve Data quote returned no close field.")
            value = parse_decimal(
                raw_price, field="close", context=f"{provider_symbol} quote"
            )
            if value <= 0:
                raise NetworkError(
                    f"Twelve Data quote returned an invalid non-positive price "
                    f"({value})."
                )
            if not price_within_band(value, reference_candle, tolerance):
                raise NetworkError(
                    f"Twelve Data quote {value} is outside the last closed "
                    f"candle range ± ATR."
                )
            return value
        except (NetworkError, SymbolNotFoundError) as exc:
            ticker_error = str(exc)

        fallback = closed_ltf_fallback_price(
            self.fetch_klines(self._config.ltf_interval, cfg.MIN_CANDLES),
            interval=self._config.ltf_interval,
        )
        if ticker_error is not None:
            self._price_notes.append(
                f"Ticker price rejected ({ticker_error}) Using last closed "
                f"{self._config.ltf_interval} candle close "
                f"{self._config.price_format.fmt(fallback)} instead."
            )
        return fallback

    def fetch_server_time(self) -> datetime:
        """Return the host clock.

        Twelve Data exposes no exchange-clock endpoint, so the host clock is
        used for the candle-closure decision and ``GENERATED_AT_UTC``. This is
        documented rather than warned about, since it is the expected behaviour
        for this provider (unlike a Binance server-time *failure*).
        """

        return datetime.now(timezone.utc)
