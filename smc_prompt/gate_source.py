"""Klien REST publik Gate.io Futures (USDT-M) untuk data pasar baca-saja.

Endpoints (publik, tanpa perlu API key):
  * ``GET /api/v4/futures/usdt/candlesticks``       — data candlestick OHLCV
  * ``GET /api/v4/futures/usdt/tickers``            — harga pasar terkini / statistik 24 jam
  * ``GET /api/v4/futures/usdt/contracts/{contract}``— spesifikasi kontrak & presisi harga
  * ``GET /api/v4/spot/time``                       — sinkronisasi jam server

Kontrak: paritas fetcher dengan :class:`smc_prompt.data_fetcher.DataFetcher`
(``validate_symbol`` / ``fetch_klines`` / ``fetch_current_price`` /
``fetch_server_time`` / ``price_notes`` / ``with_now``).
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
    SymbolMetadata,
    candle_delta,
    closed_ltf_fallback_price,
    get_json_once,
    ms_to_utc,
    parse_decimal,
    price_sanity_warning,
)

CONTRACT_PATH_TEMPLATE = "/api/v4/futures/usdt/contracts/{contract}"
CANDLESTICKS_PATH = "/api/v4/futures/usdt/candlesticks"
TICKERS_PATH = "/api/v4/futures/usdt/tickers"
TIME_PATH = "/api/v4/spot/time"

#: Selisih waktu penutupan cadangan jika token interval tidak dikenali.
_DEFAULT_DELTA = timedelta(hours=1)


class GateSource:
    """Mengambil dan menormalisasi data pasar Gate.io Futures untuk satu simbol aset."""

    def __init__(
        self,
        config: cfg.Config,
        *,
        base_urls: Sequence[str] = (cfg.GATE_HOST,),
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = None,  # type: ignore[assignment]
        rand: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._hosts = tuple(host.rstrip("/") for host in base_urls) or (
            cfg.GATE_HOST,
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._price_notes: list[str] = []

        http_kwargs: dict[str, Any] = {"session": session}
        if sleep is not None:
            http_kwargs["sleep"] = sleep
        if rand is not None:
            http_kwargs["rand"] = rand
        self._http = ProviderHttp(config, **http_kwargs)

    @property
    def price_notes(self) -> tuple[str, ...]:
        """Catatan non-fatal yang direkam saat pemanggilan :meth:`fetch_current_price` terakhir."""

        return tuple(self._price_notes)

    def with_now(self, moment: datetime) -> "GateSource":
        """Kembalikan instance fetcher ekuivalen yang terikat pada waktu referensi acuan."""

        return GateSource(
            self._config,
            base_urls=self._hosts,
            session=self._http.session,
            now=lambda: moment,
        )

    def _request(self, path: str, params: dict[str, Any], *, context: str) -> Any:
        """Lakukan HTTP GET ke endpoint Gate.io Futures dan buka envelope payload JSON."""

        payload = self._http.get_json(self._hosts, path, params, context=context)

        if isinstance(payload, dict):
            label = payload.get("label")
            msg = payload.get("message") or "unknown error"
            if label is not None:
                if "NOT_FOUND" in str(label).upper() or "INVALID" in str(label).upper():
                    raise SymbolNotFoundError(
                        f"Gate.io Futures menolak permintaan {context} ({label}): {msg}"
                    )
                raise NetworkError(
                    f"Gate.io Futures mengembalikan galat untuk {context} ({label}): {msg}"
                )
        return payload

    def validate_symbol(self) -> SymbolMetadata:
        """Validasi kontrak terhadap daftar kontrak Gate.io Futures dan peroleh ukuran tick (tickSize)."""

        target = cfg.provider_symbol(cfg.PROVIDER_GATE, self._config.symbol)
        context = f"{target} detail kontrak"
        path = CONTRACT_PATH_TEMPLATE.format(contract=target)

        payload = self._request(path, {}, context=context)
        if not isinstance(payload, dict) or not payload.get("name"):
            raise SymbolNotFoundError(
                f"Simbol '{self._config.symbol}' ({target}) tidak tersedia di Gate.io Futures. "
                f"Periksa ejaan simbol (misal: BTCUSDT, ETHUSDT)."
            )

        order_price_round = payload.get("order_price_round")
        if order_price_round is not None:
            tick_size = parse_decimal(order_price_round, field="order_price_round", context=context)
        else:
            tick_size = Decimal("0.01")

        return SymbolMetadata(
            symbol=target,
            status="TRADING",
            tick_size=str(tick_size),
        )

    def fetch_klines(
        self, interval: str, limit: int, *, symbol: str | None = None
    ) -> list[Candle]:
        """Ambil candle sejumlah limit untuk interval kanonik yang ditentukan."""

        canonical = cfg.validate_interval(interval)
        gate_interval = cfg.provider_interval(cfg.PROVIDER_GATE, canonical)
        target = cfg.provider_symbol(cfg.PROVIDER_GATE, symbol or self._config.symbol)
        context = f"{target} candle {interval}"

        params: dict[str, Any] = {
            "contract": target,
            "interval": gate_interval,
            "limit": limit,
        }
        payload = self._request(CANDLESTICKS_PATH, params, context=context)

        if not isinstance(payload, list):
            return []

        delta = candle_delta(canonical, _DEFAULT_DELTA)
        candles: list[Candle] = []

        for item in payload:
            if not isinstance(item, dict):
                continue
            raw_t = item.get("t")
            if raw_t is None:
                continue
            open_time = datetime.fromtimestamp(int(raw_t), tz=timezone.utc)
            close_time = open_time + delta
            candles.append(
                Candle(
                    open_time=open_time,
                    open=parse_decimal(item.get("o"), field="open", context=context),
                    high=parse_decimal(item.get("h"), field="high", context=context),
                    low=parse_decimal(item.get("l"), field="low", context=context),
                    close=parse_decimal(item.get("c"), field="close", context=context),
                    volume=parse_decimal(item.get("v", "0"), field="volume", context=context),
                    close_time=close_time,
                    is_closed=self._now() >= close_time,
                )
            )

        # Gate.io mengembalikan data candlestick dalam urutan kronologis (terlama -> terbaru)
        return candles

    def fetch_current_price(
        self,
        symbol: str | None = None,
        *,
        reference_candle: Candle | None = None,
        tolerance: Decimal | None = None,
    ) -> Decimal:
        """Ambil harga pasar terkini dari ticker Gate.io Futures."""

        target = cfg.provider_symbol(cfg.PROVIDER_GATE, symbol or self._config.symbol)
        context = f"{target} harga"

        payload = self._request(TICKERS_PATH, {"contract": target}, context=context)
        price: Decimal | None = None

        if isinstance(payload, list) and payload:
            for item in payload:
                if isinstance(item, dict) and item.get("contract", "").upper() == target:
                    raw_price = item.get("last") or item.get("mark_price")
                    if raw_price is not None:
                        price = parse_decimal(raw_price, field="last", context=context)
                        break

        if price is None or price <= 0:
            if reference_candle is not None:
                return closed_ltf_fallback_price(
                    [reference_candle], interval=self._config.ltf_interval
                )
            raise NetworkError(
                f"Tidak dapat memperoleh harga terkini untuk {target} dari Gate.io Futures."
            )

        warning = price_sanity_warning(
            price,
            reference_candle,
            tolerance,
            symbol=target,
        )
        if warning:
            self._price_notes.append(warning)

        return price

    def fetch_server_time(self) -> datetime:
        """Ambil waktu server Gate.io dari endpoint spot time, fallback ke jam host jika gagal."""

        try:
            payload = self._request(TIME_PATH, {}, context="waktu server Gate.io")
            if isinstance(payload, dict) and payload.get("server_time") is not None:
                return ms_to_utc(payload["server_time"])
        except Exception:
            pass
        return self._now()

    def fetch_derivatives_info(self) -> dict[str, Any]:
        """Ambil Funding Rate & Open Interest (position_size) dari detail kontrak Gate.io Futures."""

        contract = cfg.provider_symbol(cfg.PROVIDER_GATE, self._config.symbol)
        path = CONTRACT_PATH_TEMPLATE.format(contract=contract)
        for host in self._hosts:
            url = f"{host.rstrip('/')}{path}"
            try:
                payload = get_json_once(
                    self._http.session,
                    url,
                    {},
                    timeout=self._config.request_timeout,
                )
                if not isinstance(payload, dict) or payload.get("label") is not None:
                    continue

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
                return {
                    "source_label": cfg.provider_label(cfg.PROVIDER_GATE),
                    "funding_rate": funding_rate,
                    "open_interest": open_interest,
                }
            except (Exception, AssertionError):
                continue
        return {}
