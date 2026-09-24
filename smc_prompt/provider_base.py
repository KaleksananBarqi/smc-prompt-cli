"""Shared scaffolding for the network data providers.

The analysis pipeline is provider-agnostic (it consumes ``Sequence[Candle]``);
what changes per provider is only *how* candles are fetched. This module holds
the pieces that would otherwise be copy-pasted into every provider:

  * :func:`ms_to_utc` / :func:`iso_to_utc`   — timestamp normalization to aware UTC
  * :func:`parse_decimal`                    — strict decimal parsing with context
  * :func:`aggregate_candles`                — deterministic N->1 timeframe rollup
  * :class:`ProviderHttp`                    — retry/backoff + host failover JSON GET

Byte-stability contract: every value that reaches the renderer is a ``Decimal``
built from the provider's literal string, so no float rounding is introduced
(see ``config.fmt_volume`` / ``PriceFormat.fmt``). Aggregation only ever takes
``max`` / ``min`` / ``sum`` of existing values or copies a constituent value
verbatim, so an aggregated candle is exactly as reproducible as its inputs.

No randomness affects output: the only stochastic input is the small retry
jitter, which changes network timing and never a rendered value.
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Sequence

import requests

from . import config as cfg
from .errors import ConfigError, NetworkError, SymbolNotFoundError
from .models import Candle

#: HTTP statuses that mean "this host is blocked for us" -> try the next host.
# Constants are defined as frozenset for O(1) membership test performance while preserving immutability.
HOST_BLOCK_STATUSES: frozenset[int] = frozenset({403, 451})

#: HTTP statuses that mean the request itself is invalid -> no retry.
FATAL_CLIENT_STATUSES: frozenset[int] = frozenset({400, 404})

#: HTTP statuses worth retrying on the same host.
RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: HTTP status meaning the supplied credential was rejected -> a config problem.
AUTH_STATUSES: frozenset[int] = frozenset({401})

_MS_PER_SECOND = 1000.0

#: Matches fractional seconds longer than 6 digits. Python 3.10's
#: ``datetime.fromisoformat`` rejects more than 6, while OANDA emits 9, so the
#: surplus digits are truncated (nanosecond -> microsecond; sub-microsecond
#: precision is irrelevant to a candle timestamp).
_LONG_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def ms_to_utc(ms: int | float | str) -> datetime:
    """Convert an epoch-millisecond timestamp to an aware UTC datetime."""

    return datetime.fromtimestamp(int(ms) / _MS_PER_SECOND, tz=timezone.utc)


def iso_to_utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Accepts a trailing ``Z``, an explicit ``+HH:MM`` offset, a naive value
    (assumed UTC), a space separator and the 9-digit nanosecond fraction OANDA
    emits (truncated to microseconds for Python 3.10 compatibility). Raises
    :class:`ValueError` on anything else so the caller can add context.
    """

    normalized = (text or "").strip().replace("Z", "+00:00")
    normalized = _LONG_FRACTION_RE.sub(r"\1", normalized)
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_decimal(raw: Any, *, field: str, context: str) -> Decimal:
    """Parse one provider value into a :class:`Decimal` (never via ``float``).

    Raises :class:`~smc_prompt.errors.NetworkError` carrying the field name and
    the offending payload context so a malformed response is diagnosable.
    """

    text = "" if raw is None else str(raw).strip()
    if not text:
        raise NetworkError(
            f"{context} returned an empty {field} value. No prompt generated."
        )
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise NetworkError(
            f"{context} returned an unparseable {field} value '{text}' ({exc}). "
            f"No prompt generated."
        ) from exc


def aggregate_candles(
    candles: Sequence[Candle], factor: int
) -> list[Candle]:
    """Roll ``factor`` consecutive same-interval candles into one (N->1).

    Used for providers that do not serve a tier natively (e.g. Yahoo has no
    ``4h``): the tier is built by aggregating the next-finer series instead of
    silently analysing the wrong timeframe.

    Semantics (purely mechanical, matching a standard chart aggregation):

    * ``open``  = first constituent's open
    * ``high``  = max of constituent highs
    * ``low``   = min of constituent lows
    * ``close`` = last constituent's close
    * ``volume``= sum of constituent volumes
    * ``open_time``  = first constituent's open time
    * ``close_time`` = last constituent's close time
    * ``is_closed``  = True only when *every* constituent is closed

    A leading partial group is dropped so that every emitted bucket is
    boundary-aligned with the newest bucket (the series is chronological and
    uniformly spaced, and the newest candle close sits on the tier boundary).
    The result is therefore a deterministic function of the input series.
    """

    if factor <= 1:
        return list(candles)
    ordered = list(candles)
    if not ordered:
        return []

    start = len(ordered) % factor
    window = ordered[start:]
    aggregated: list[Candle] = []
    for index in range(0, len(window), factor):
        group = window[index : index + factor]
        if not group:
            continue
        aggregated.append(
            Candle(
                open_time=group[0].open_time,
                open=group[0].open,
                high=max(candle.high for candle in group),
                low=min(candle.low for candle in group),
                close=group[-1].close,
                volume=sum((candle.volume for candle in group), Decimal("0")),
                close_time=group[-1].close_time,
                is_closed=all(candle.is_closed for candle in group),
            )
        )
    return aggregated


def closed_ltf_fallback_price(
    candles: Sequence[Candle], *, interval: str
) -> Decimal:
    """Last *closed* candle close, used when a provider has no ticker route.

    Mirrors the Binance fetcher's documented current-price fallback so the
    providers stay contract-compatible. Raises
    :class:`~smc_prompt.errors.NetworkError` when no closed candle exists.
    """

    closed = [candle for candle in candles if candle.is_closed]
    if not closed:
        raise NetworkError(
            "Current price unavailable: no closed candle exists to derive a "
            "fallback price."
        )
    fallback = closed[-1].close
    if fallback <= 0:
        raise NetworkError(
            f"Fallback {interval} candle close is non-positive ({fallback}); "
            f"cannot determine a valid current price."
        )
    return fallback


class ProviderHttp:
    """Retry/backoff + host-failover JSON GET shared by the providers.

    Behaviour intentionally mirrors ``data_fetcher.DataFetcher._request_json``
    so the two network paths degrade identically:

    * connection errors / read timeouts  -> retry the same host, then fail over
    * HTTP 403 / 451                     -> fail over immediately (geo-block)
    * HTTP 400 / 404                     -> :class:`SymbolNotFoundError`, no retry
    * HTTP 429 / 5xx                     -> retry the same host, then fail over
    * other 4xx/5xx                      -> fail over
    * invalid JSON body                  -> retry the same host, then fail over

    ``context`` labels the failure message (e.g. ``"XAU/USD 4h candles"``) so an
    error names what was being fetched rather than guessing at the cause.
    """

    def __init__(
        self,
        config: cfg.Config,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._sleep = sleep
        self._rand = rand
        self._active_host: str | None = None

    @property
    def active_host(self) -> str | None:
        """Host that served the last successful request."""

        return self._active_host

    @property
    def session(self) -> requests.Session:
        """The underlying session, so ``with_now`` can clone a provider.

        Reusing one session keeps connection pooling across the cloned
        instances without forcing each provider to keep its own reference.
        """

        return self._session

    def _jitter(self) -> float:
        """Small +/-250ms jitter; affects network timing only, not output."""

        return (self._rand() - 0.5) * 0.5

    def _backoff(self, attempt: int) -> float:
        return self._config.retry_backoff_base * (
            self._config.retry_backoff_factor ** attempt
        ) + self._jitter()

    def get_json(
        self,
        hosts: Sequence[str],
        path: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        context: str,
    ) -> Any:
        """GET ``path`` across ``hosts`` with retry/backoff and failover."""

        request_headers = {"User-Agent": "smc-prompt/0.1"}
        if headers:
            request_headers.update(headers)

        last_reason = "unknown error"
        attempts_total = 0

        for host in hosts:
            base = host.rstrip("/")
            url = f"{base}{path}"
            for attempt in range(self._config.retry_max):
                attempts_total += 1
                try:
                    response = self._session.get(
                        url,
                        params=params,
                        timeout=self._config.request_timeout,
                        headers=request_headers,
                    )
                except (requests.ConnectionError, requests.Timeout) as exc:
                    last_reason = f"{type(exc).__name__}: {exc}"
                    if attempt < self._config.retry_max - 1:
                        self._sleep(self._backoff(attempt))
                        continue
                    break

                status = response.status_code

                if status in HOST_BLOCK_STATUSES:
                    last_reason = f"HTTP {status} from {base}"
                    break

                if status in AUTH_STATUSES:
                    # A rejected credential is a configuration problem, not a
                    # transient network one: fail fast with remediation advice
                    # instead of burning the retry budget.
                    raise ConfigError(
                        f"The provider credential was rejected while fetching "
                        f"{context} (HTTP {status} from {base}). Check the API "
                        f"key/token and that your plan covers this instrument."
                    )

                if status in FATAL_CLIENT_STATUSES:
                    raise SymbolNotFoundError(
                        f"{context} is not available from this provider "
                        f"(HTTP {status} from {base}). Check the symbol "
                        f"spelling and that your plan covers this instrument."
                    )

                if status in RETRY_STATUSES:
                    last_reason = f"HTTP {status} from {base}"
                    if attempt < self._config.retry_max - 1:
                        self._sleep(self._backoff(attempt))
                        continue
                    break

                if status >= 400:
                    last_reason = f"HTTP {status} from {base}"
                    break

                try:
                    payload = response.json()
                except ValueError as exc:
                    last_reason = f"invalid JSON from {base}: {exc}"
                    if attempt < self._config.retry_max - 1:
                        self._sleep(self._backoff(attempt))
                        continue
                    break

                self._active_host = base
                return payload

        raise NetworkError(
            f"Provider unreachable while fetching {context} after "
            f"{attempts_total} attempts ({last_reason}). No prompt generated."
        )


# --------------------------------------------------------------------------
# Current-price sanity cross-check (shared by every network provider)
# --------------------------------------------------------------------------


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
    price_format: "cfg.PriceFormat | None" = None,
) -> str | None:
    """Non-fatal warning when ``price`` is outside the last-candle band ± ATR.

    Returns ``None`` when the price is plausible (or cannot be cross-checked).
    The message is deterministic; it is emitted on stderr by the CLI and never
    alters the rendered prompt. The wording matches the historical Binance
    version byte for byte when ``price_format`` is omitted; providers pass their
    (possibly tick-derived) format so the band renders at the same precision as
    the rest of their prompt.
    """

    if price_within_band(price, reference_candle, tolerance):
        return None
    fmt = price_format.fmt if price_format is not None else cfg.fmt_price
    band = tolerance if tolerance is not None else Decimal("0")
    return (
        f"Current price {cfg.fmt_price(price)} is outside the last closed "
        f"candle range [{cfg.fmt_price(reference_candle.low)}, "
        f"{cfg.fmt_price(reference_candle.high)}] ± {fmt(band)}; "
        f"possible stale {symbol} ticker data. Prompt generated with caution."
    )


def candle_delta(interval: str, fallback: timedelta) -> timedelta:
    """Best-effort interval duration for deriving a missing ``close_time``.

    Only used by providers whose payload omits the close timestamp; the
    canonical Binance interval token is authoritative when recognised.
    """

    durations = {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "2h": timedelta(hours=2),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
        "1w": timedelta(weeks=1),
        "1M": timedelta(days=30),
    }
    return durations.get(interval, fallback)
