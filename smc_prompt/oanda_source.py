"""OANDA v20 provider (spot metals/FX — highest-fidelity XAU_USD).

Why this exists: Binance Spot cannot serve ``XAUUSD`` at all, and the tokenized
gold proxies (``XAUTUSDT``) are a different market with a peg premium and a
crypto order book. OANDA serves the genuine spot ``XAU_USD`` instrument with
``H4``/``H1``/``D`` granularity natively, so the default ``1d`` / ``4h`` / ``1h``
trio maps 1:1 and no aggregation is required.

Contract: fetcher-parity with :class:`smc_prompt.data_fetcher.DataFetcher`, so
``cli.run`` drives it through the unchanged analysis pipeline.

OANDA v20 API notes
-------------------
* Authentication is ``Authorization: Bearer <token>``; the token is per
  environment, hence the ``--oanda-env practice|live`` switch (practice is the
  default because a free demo account can read candles).
* ``GET /v3/accounts`` is used to auto-discover the account id when the caller
  does not supply one, since ``/pricing`` requires it. When no account can be
  resolved the current price falls back to the last closed candle.
* ``GET /v3/accounts/{id}/instruments`` exposes ``displayPrecision``, which is
  the venue's own price precision. It is mapped onto a synthetic
  ``PRICE_FILTER.tickSize`` so the CLI's existing tick-size path derives the
  correct decimals instead of guessing.
* Candle ``time`` is the candle **start**; the close time is derived from the
  granularity. ``complete`` is the venue's own "this candle is finished" flag and
  is used directly (authoritative, unlike a host-clock comparison).
* **``volume`` on OANDA candles is a tick count, not trade volume.** Rendering it
  in a column labelled ``volume`` would mislabel it as [FAKTA], so it is emitted
  as ``0`` and the provider declares ``volume_available=False``.
* Timestamps carry 9-digit nanosecond fractions; :func:`provider_base.iso_to_utc`
  truncates them to microseconds for Python 3.10 compatibility.
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

ACCOUNTS_PATH = "/v3/accounts"
INSTRUMENTS_SEGMENT = "instruments"
CANDLES_SEGMENT = "candles"
PRICING_SEGMENT = "pricing"

#: Fallback close-time gap when the canonical interval is unrecognised.
_DEFAULT_DELTA = timedelta(hours=1)

#: Precision used when the venue's ``displayPrecision`` cannot be read. OANDA
#: quotes ``XAU_USD`` on 3 decimals, but the shared conservative default matches
#: the wider market convention.
_FALLBACK_DISPLAY_PRECISION = cfg.FALLBACK_TICK_DECIMALS


class OandaSource:
    """Fetches and normalizes OANDA v20 candles for one instrument."""

    def __init__(
        self,
        config: cfg.Config,
        *,
        token: str,
        environment: str = cfg.OANDA_ENV_PRACTICE,
        account_id: str | None = None,
        base_urls: Sequence[str] | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = None,  # type: ignore[assignment]
        rand: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not token or not token.strip():
            raise ConfigError(
                "OANDA requires a v20 API token. Pass --oanda-token or set the "
                "OANDA_API_TOKEN environment variable."
            )
        if environment not in cfg.OANDA_ENVS:
            raise ConfigError(
                "--oanda-env must be one of: " + ", ".join(cfg.OANDA_ENVS) + "."
            )

        self._config = config
        self._token = token.strip()
        self._environment = environment
        self._account_id = account_id.strip() if account_id else None
        resolved_hosts = base_urls or (cfg.OANDA_HOSTS[environment],)
        self._hosts = tuple(host.rstrip("/") for host in resolved_hosts) or (
            cfg.OANDA_HOSTS[environment],
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._price_notes: list[str] = []
        self._display_precision: int | None = None

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

    @property
    def instrument(self) -> str:
        """The provider-side instrument code (e.g. ``XAU_USD``)."""

        return cfg.provider_symbol(cfg.PROVIDER_OANDA, self._config.symbol)

    def with_now(self, moment: datetime) -> "OandaSource":
        """Return an equivalent source bound to a fixed ``now`` instant."""

        clone = OandaSource(
            self._config,
            token=self._token,
            environment=self._environment,
            account_id=self._account_id,
            base_urls=self._hosts,
            session=self._http.session,
            now=lambda: moment,
        )
        clone._display_precision = self._display_precision
        return clone

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, path: str, params: dict[str, Any], *, context: str) -> Any:
        """GET one v20 endpoint with the bearer credential attached."""

        return self._http.get_json(
            self._hosts, path, params, headers=self._headers(), context=context
        )

    def _resolve_account_id(self) -> str | None:
        """Return the configured account id, else the first one on the token.

        ``/pricing`` needs an account id while ``/candles`` does not, so the id
        is discovered lazily and cached. A missing account is not fatal: the
        caller degrades to the closed-candle price fallback.
        """

        if self._account_id:
            return self._account_id
        try:
            payload = self._request(
                ACCOUNTS_PATH, {}, context="OANDA account list"
            )
        except NetworkError:
            return None
        accounts = (
            payload.get("accounts") if isinstance(payload, dict) else None
        )
        if not isinstance(accounts, list) or not accounts:
            return None
        first = accounts[0]
        if not isinstance(first, dict):
            return None
        account_id = first.get("id")
        if isinstance(account_id, str) and account_id:
            self._account_id = account_id
            return account_id
        return None

    def _read_display_precision(self) -> int | None:
        """Read ``displayPrecision`` for the instrument from the venue metadata."""

        if self._display_precision is not None:
            return self._display_precision
        account_id = self._resolve_account_id()
        if account_id is None:
            return None
        try:
            payload = self._request(
                f"{ACCOUNTS_PATH}/{account_id}/{INSTRUMENTS_SEGMENT}",
                {},
                context="OANDA instrument metadata",
            )
        except NetworkError:
            return None
        instruments = (
            payload.get("instruments") if isinstance(payload, dict) else None
        )
        if not isinstance(instruments, list):
            return None
        for item in instruments:
            if not isinstance(item, dict):
                continue
            if str(item.get("name", "")).upper() != self.instrument.upper():
                continue
            raw_precision = item.get("displayPrecision")
            try:
                precision = int(raw_precision)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
            if 0 <= precision <= cfg.MAX_PRICE_DECIMALS:
                self._display_precision = precision
                return precision
            return None
        return None

    @staticmethod
    def _tick_size(precision: int) -> str:
        """Render a ``tickSize`` string from a decimal count (2 -> ``0.01``)."""

        return str(Decimal(1).scaleb(-precision))

    def _candle_from_row(
        self,
        row: dict[str, Any],
        *,
        canonical_interval: str,
        context: str,
    ) -> Candle:
        """Normalize one ``candles`` entry into a :class:`Candle`."""

        # ``price=M`` is requested, so ``mid`` is always present; accepting
        # ``bid``/``ask`` as a fallback keeps a partially-honoured request from
        # hard-failing.
        prices = row.get("mid") or row.get("bid") or row.get("ask")
        if not isinstance(prices, dict):
            raise NetworkError(
                f"{context} returned a candle without price data. "
                f"No prompt generated."
            )

        raw_stamp = row.get("time")
        if not raw_stamp:
            raise NetworkError(
                f"{context} returned a candle without a time. "
                f"No prompt generated."
            )
        try:
            open_time = iso_to_utc(str(raw_stamp))
        except ValueError as exc:
            raise NetworkError(
                f"{context} returned an unparseable time '{raw_stamp}' "
                f"({exc}). No prompt generated."
            ) from exc

        delta = candle_delta(canonical_interval, _DEFAULT_DELTA)
        close_time = open_time + delta

        # ``complete`` is the venue's own closure flag and is authoritative;
        # the clock comparison is only a fallback when the field is absent.
        complete = row.get("complete")
        if isinstance(complete, bool):
            is_closed = complete
        else:
            is_closed = self._now() >= close_time + timedelta(seconds=1)

        return Candle(
            open_time=open_time,
            open=parse_decimal(prices.get("o"), field="mid.o", context=context),
            high=parse_decimal(prices.get("h"), field="mid.h", context=context),
            low=parse_decimal(prices.get("l"), field="mid.l", context=context),
            close=parse_decimal(prices.get("c"), field="mid.c", context=context),
            # OANDA's candle "volume" is a tick count, not trade volume, so it
            # is deliberately zeroed rather than mislabelled as volume.
            volume=Decimal("0"),
            close_time=close_time,
            is_closed=is_closed,
        )

    # ------------------------------------------------------------------
    # Public API (fetcher parity)
    # ------------------------------------------------------------------

    def validate_symbol(self) -> dict[str, Any]:
        """Validate the instrument and return a Binance-shaped synthetic entry.

        A one-candle probe confirms the instrument is tradable on the token,
        and the venue's ``displayPrecision`` (when readable) becomes the
        synthetic ``PRICE_FILTER.tickSize`` so price rendering uses the real
        venue precision.
        """

        instrument = self.instrument
        payload = self._request(
            f"/v3/{INSTRUMENTS_SEGMENT}/{instrument}/{CANDLES_SEGMENT}",
            {"granularity": "D", "count": 1, "price": "M"},
            context=f"{instrument} probe candle",
        )
        candles = payload.get("candles") if isinstance(payload, dict) else None
        if not isinstance(candles, list) or not candles:
            raise SymbolNotFoundError(
                f"Symbol '{self._config.symbol}' (as '{instrument}') is not "
                f"available on OANDA {self._environment}. Check the spelling "
                f"(for gold use XAUUSD; it maps to XAU_USD)."
            )

        precision = self._read_display_precision()
        tick_size = self._tick_size(
            precision if precision is not None else _FALLBACK_DISPLAY_PRECISION
        )
        return {
            "symbol": instrument,
            "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": tick_size}
            ],
        }

    def fetch_klines(
        self, interval: str, limit: int, *, symbol: str | None = None
    ) -> list[Candle]:
        """Fetch ``limit`` candles for a canonical ``interval``.

        The canonical token is translated to a v20 ``granularity`` (``4h`` ->
        ``H4``). v20 returns candles oldest-first, which is the order the
        analyzer expects.
        """

        canonical = cfg.validate_interval(interval)
        granularity = cfg.provider_interval(cfg.PROVIDER_OANDA, canonical)
        instrument = (
            cfg.provider_symbol(cfg.PROVIDER_OANDA, symbol)
            if symbol
            else self.instrument
        )
        context = f"{instrument} {canonical} candles"

        payload = self._request(
            f"/v3/{INSTRUMENTS_SEGMENT}/{instrument}/{CANDLES_SEGMENT}",
            {"granularity": granularity, "count": limit, "price": "M"},
            context=context,
        )
        if not isinstance(payload, dict):
            raise NetworkError(
                f"Unexpected {context} payload. No prompt generated."
            )
        rows = payload.get("candles")
        if not isinstance(rows, list) or not rows:
            raise NetworkError(
                f"OANDA returned no {context}. No prompt generated."
            )

        candles = [
            self._candle_from_row(
                row, canonical_interval=canonical, context=context
            )
            for row in rows
            if isinstance(row, dict)
        ]
        candles.sort(key=lambda candle: candle.open_time)
        return candles

    def fetch_current_price(
        self,
        *,
        reference_candle: Candle | None = None,
        tolerance: Decimal | None = None,
    ) -> Decimal:
        """Fetch the live mid price via ``/pricing``, else the last close.

        OANDA quotes a bid/ask pair, so the current price is the midpoint of the
        two. Sanity rules mirror the Binance fetcher: a non-positive price, or
        one outside the last closed candle's ``[low, high] ± ATR`` band, is
        rejected in favour of the fallback.
        """

        self._price_notes = []
        ticker_error: str | None = None
        try:
            account_id = self._resolve_account_id()
            if account_id is None:
                raise NetworkError(
                    "OANDA account id unavailable; cannot query /pricing."
                )
            payload = self._request(
                f"{ACCOUNTS_PATH}/{account_id}/{PRICING_SEGMENT}",
                {"instruments": self.instrument},
                context=f"{self.instrument} pricing",
            )
            prices = (
                payload.get("prices") if isinstance(payload, dict) else None
            )
            if not isinstance(prices, list) or not prices:
                raise NetworkError("OANDA pricing returned no price entry.")
            entry = prices[0] if isinstance(prices[0], dict) else {}

            bid = self._first_price(entry.get("bids"))
            ask = self._first_price(entry.get("asks"))
            if bid is None and ask is None:
                closeout_bid = entry.get("closeoutBid")
                closeout_ask = entry.get("closeoutAsk")
                if closeout_bid is None and closeout_ask is None:
                    raise NetworkError(
                        "OANDA pricing returned neither bid nor ask."
                    )
                value = self._midpoint(closeout_bid, closeout_ask)
            else:
                value = self._midpoint(bid, ask)

            if value <= 0:
                raise NetworkError(
                    f"OANDA pricing returned an invalid non-positive price "
                    f"({value})."
                )
            if not price_within_band(value, reference_candle, tolerance):
                raise NetworkError(
                    f"OANDA pricing {value} is outside the last closed candle "
                    f"range ± ATR."
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

    @staticmethod
    def _first_price(side: Any) -> Decimal | None:
        """Pull the first price out of a v20 ``bids``/``asks`` array."""

        if not isinstance(side, list):
            return None
        for item in side:
            if not isinstance(item, dict):
                continue
            raw = item.get("price")
            if raw is None:
                continue
            try:
                return Decimal(str(raw))
            except Exception:  # pragma: no cover - defensive parse guard
                continue
        return None

    @staticmethod
    def _midpoint(bid: Any, ask: Any) -> Decimal:
        """Midpoint of the quoted sides, tolerating a missing side."""

        if bid is None and ask is None:
            raise NetworkError("OANDA pricing returned no usable side.")
        if bid is None:
            return Decimal(str(ask))
        if ask is None:
            return Decimal(str(bid))
        return (Decimal(str(bid)) + Decimal(str(ask))) / Decimal("2")

    def fetch_server_time(self) -> datetime:
        """Return the host clock.

        OANDA exposes no public clock endpoint, so the host clock drives the
        candle-closure decision and ``GENERATED_AT_UTC``. Candle closure itself
        still relies on the venue's authoritative ``complete`` flag.
        """

        return datetime.now(timezone.utc)
