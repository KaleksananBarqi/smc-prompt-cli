"""Binance Futures (USDT-M) public REST client (read-only market data only).

Endpoints (no API key, no signed/private routes, spec §4.1):
  * ``GET /fapi/v1/klines``        — HTF daily / LTF hourly OHLCV
  * ``GET /fapi/v1/ticker/price``  — current price
  * ``GET /fapi/v1/exchangeInfo``  — symbol validation (+ tickSize, status)
  * ``GET /fapi/v1/time``          — server time (clock-skew-safe closure)

Includes retry-with-backoff (spec §9.4) and base-URL failover on connection
errors / HTTP 451 / HTTP 403.
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

#: HTTP statuses that mean "this host is blocked for us" -> try the next host.
_HOST_BLOCK_STATUSES: frozenset[int] = frozenset({403, 451})

#: HTTP statuses that mean the request itself is invalid -> no retry.
_FATAL_CLIENT_STATUSES: frozenset[int] = frozenset({400, 404})

#: HTTP statuses worth retrying on the same host.
_RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _ms_to_utc(ms: int | float | str) -> datetime:
    """Convert a Binance millisecond timestamp to an aware UTC datetime."""

    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)


def price_within_band(
    price: Decimal,
    reference_candle: Candle | None,
    tolerance: Decimal | None,
) -> bool:
    """True when ``price`` lies within ``reference.low - tol .. reference.high + tol``.

    A missing reference candle or tolerance degrades to "always plausible"
    (no basis for a cross-check), keeping the callers branch-free.
    """

    if reference_candle is None or tolerance is None:
        return True
    return (
        reference_candle.low - tolerance
        <= price
        <= reference_candle.high + tolerance
    )


def price_sanity_warning(
    price: Decimal,
    reference_candle: Candle | None,
    tolerance: Decimal | None,
    *,
    symbol: str,
) -> str | None:
    """Non-fatal warning when ``price`` is outside the last-candle band ± ATR.

    Returns ``None`` when the price is plausible (or cannot be cross-checked).
    The message is deterministic; it is emitted on stderr by the CLI and never
    alters the rendered prompt.
    """

    if price_within_band(price, reference_candle, tolerance):
        return None
    band = tolerance if tolerance is not None else Decimal("0")
    return (
        f"Current price {cfg.fmt_price(price)} is outside the last closed "
        f"candle range [{cfg.fmt_price(reference_candle.low)}, "
        f"{cfg.fmt_price(reference_candle.high)}] ± {cfg.fmt_price(band)}; "
        f"possible stale {symbol} ticker data. Prompt generated with caution."
    )


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
    # Internals
    # ------------------------------------------------------------------

    def _jitter(self) -> float:
        """Small +/-250ms jitter; affects network timing only, not output."""

        return (self._rand() - 0.5) * 0.5

    def _backoff(self, attempt: int) -> float:
        return self._config.retry_backoff_base * (
            self._config.retry_backoff_factor ** attempt
        ) + self._jitter()

    def _request_json(self, path: str, params: dict[str, Any]) -> Any:
        """GET ``path`` with retry/backoff and base-URL failover.

        Raises :class:`SymbolNotFoundError` for invalid-symbol responses and
        :class:`NetworkError` once every host/attempt pair is exhausted.
        """

        hosts: Sequence[str] = self._config.base_urls
        last_reason = "unknown error"
        attempts_total = 0

        for host in hosts:
            url = f"{host}{path}"
            for attempt in range(self._config.retry_max):
                attempts_total += 1
                try:
                    response = self._session.get(
                        url,
                        params=params,
                        timeout=self._config.request_timeout,
                        headers={"User-Agent": "smc-prompt/0.1"},
                    )
                except (requests.ConnectionError, requests.Timeout) as exc:
                    last_reason = f"{type(exc).__name__}: {exc}"
                    if attempt < self._config.retry_max - 1:
                        self._sleep(self._backoff(attempt))
                        continue
                    break

                status = response.status_code

                if status in _HOST_BLOCK_STATUSES:
                    # Geo-block / forbidden for this host: stop retrying it and
                    # fail over to the next approved base URL.
                    last_reason = f"HTTP {status} from {host}"
                    break

                if status in _FATAL_CLIENT_STATUSES:
                    # 400 commonly means "symbol does not exist" for klines.
                    raise SymbolNotFoundError(
                        f"Symbol '{self._config.symbol}' is not listed on "
                        f"Binance Futures. Check the spelling (e.g. BTCUSDT)."
                    )

                if status in _RETRY_STATUSES:
                    last_reason = f"HTTP {status} from {host}"
                    if attempt < self._config.retry_max - 1:
                        self._sleep(self._backoff(attempt))
                        continue
                    break

                if status >= 400:
                    last_reason = f"HTTP {status} from {host}"
                    break

                try:
                    payload = response.json()
                except ValueError as exc:
                    last_reason = f"invalid JSON from {host}: {exc}"
                    if attempt < self._config.retry_max - 1:
                        self._sleep(self._backoff(attempt))
                        continue
                    break

                self._active_base_url = host
                return payload

        raise NetworkError(
            f"Binance API unreachable after {attempts_total} attempts "
            f"({last_reason}). No prompt generated."
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_base_url(self) -> str | None:
        """Host that served the last successful request."""

        return self._active_base_url

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
        return _ms_to_utc(server_ms)

    def validate_symbol(self) -> dict[str, Any]:
        """Validate the symbol against ``exchangeInfo`` (spec §9.3).

        Returns the raw ``exchangeInfo`` entry so the caller can derive
        ``PRICE_FILTER.tickSize`` precision and surface a non-TRADING
        ``status`` warning (the entry is *not* discarded).
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
        return matched[0]

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
            close_time = _ms_to_utc(row[6])
            is_closed = now >= close_time + timedelta(seconds=1)
            candles.append(
                Candle(
                    open_time=_ms_to_utc(row[0]),
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
        """Fetch the current price via ticker/price, falling back to kline close.

        Uses the last *closed* LTF candle close as fallback; the half-open
        candle is never used for anything else (spec §4.3).

        Sanity checks (§ robustness cluster):

        * ``price > 0`` — a non-positive ticker value is rejected.
        * plausibility cross-check — when ``reference_candle`` (the last closed
          LTF candle) and ``tolerance`` (``1 × ATR(14)``) are supplied, a ticker
          price outside the candle's ``[low, high]`` band ± tolerance is
          rejected as implausible.

        A rejected ticker is downgraded to the closed-candle fallback and the
        reason is recorded in :attr:`price_notes` (the CLI emits it as a
        non-fatal WARN). A :class:`NetworkError` is raised only when *no* valid
        price can be determined at all.
        """

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
