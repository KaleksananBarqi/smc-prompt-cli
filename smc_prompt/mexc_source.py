"""Klien REST publik MEXC Futures (USDT-M) untuk data pasar baca-saja.

Endpoints (publik, tanpa perlu API key):
  * ``GET /api/v1/contract/kline/{symbol}``   — data candlestick OHLCV
  * ``GET /api/v1/contract/ticker``           — harga pasar terkini / statistik 24 jam
  * ``GET /api/v1/contract/detail``           — spesifikasi kontrak & presisi harga
  * ``GET /api/v1/contract/ping``             — sinkronisasi jam server

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
    candle_delta,
    closed_ltf_fallback_price,
    ms_to_utc,
    parse_decimal,
    price_sanity_warning,
)

DETAIL_PATH = "/api/v1/contract/detail"
KLINES_PATH_TEMPLATE = "/api/v1/contract/kline/{symbol}"
TICKER_PATH = "/api/v1/contract/ticker"
PING_PATH = "/api/v1/contract/ping"

#: Selisih waktu penutupan cadangan jika token interval tidak dikenali.
_DEFAULT_DELTA = timedelta(hours=1)


class MexcSource:
    """Mengambil dan menormalisasi data pasar MEXC Futures untuk satu simbol aset."""

    def __init__(
        self,
        config: cfg.Config,
        *,
        base_urls: Sequence[str] = (cfg.MEXC_HOST,),
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = None,  # type: ignore[assignment]
        rand: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._hosts = tuple(host.rstrip("/") for host in base_urls) or (
            cfg.MEXC_HOST,
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

    def with_now(self, moment: datetime) -> "MexcSource":
        """Kembalikan instance fetcher ekuivalen yang terikat pada waktu referensi acuan."""

        return MexcSource(
            self._config,
            base_urls=self._hosts,
            session=self._http.session,
            now=lambda: moment,
        )

    def _request(self, path: str, params: dict[str, Any], *, context: str) -> Any:
        """Lakukan HTTP GET ke endpoint MEXC Futures dan buka envelope payload JSON."""

        payload = self._http.get_json(self._hosts, path, params, context=context)

        if isinstance(payload, dict):
            code = payload.get("code")
            msg = payload.get("msg") or "unknown error"
            if code is not None and code != 0:
                # Kode 2 atau 404 menandakan simbol tidak terdaftar di MEXC
                if code in (2, 404) or "not found" in str(msg).lower() or "not exist" in str(msg).lower():
                    raise SymbolNotFoundError(
                        f"MEXC Futures menolak permintaan {context} (code {code}): {msg}"
                    )
                # Kode 500 atau error lainnya adalah kegagalan jaringan/server internal
                raise NetworkError(
                    f"MEXC Futures mengembalikan galat untuk {context} (code {code}): {msg}"
                )
        return payload

    def validate_symbol(self) -> dict[str, Any]:
        """Validasi simbol terhadap detail kontrak MEXC Futures dan peroleh ukuran tick (tickSize)."""

        target = cfg.provider_symbol(cfg.PROVIDER_MEXC, self._config.symbol)
        context = f"{target} detail kontrak"
        payload = self._request(DETAIL_PATH, {"symbol": target}, context=context)

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise SymbolNotFoundError(
                f"Simbol '{self._config.symbol}' ({target}) tidak tersedia di MEXC Futures. "
                f"Periksa ejaan simbol (misal: BTCUSDT, ETHUSDT)."
            )

        # Hitung tickSize dari priceScale
        # Contoh: priceScale 1 -> 0.1, 2 -> 0.01, 4 -> 0.0001
        price_scale = data.get("priceScale")
        if isinstance(price_scale, int) and price_scale >= 0:
            tick_size = Decimal(10) ** -price_scale
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

    def fetch_klines(
        self, interval: str, limit: int, *, symbol: str | None = None
    ) -> list[Candle]:
        """Ambil candle sejumlah limit untuk interval kanonik yang ditentukan."""

        canonical = cfg.validate_interval(interval)
        mexc_interval = cfg.provider_interval(cfg.PROVIDER_MEXC, canonical)
        target = cfg.provider_symbol(cfg.PROVIDER_MEXC, symbol or self._config.symbol)
        context = f"{target} candle {interval}"

        path = KLINES_PATH_TEMPLATE.format(symbol=target)
        payload = self._request(path, {"interval": mexc_interval}, context=context)

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return []

        times = data.get("time") or []
        opens = data.get("open") or []
        highs = data.get("high") or []
        lows = data.get("low") or []
        closes = data.get("close") or []
        vols = data.get("vol") or []

        total = min(len(times), len(opens), len(highs), len(lows), len(closes), len(vols))
        if total == 0:
            return []

        delta = candle_delta(canonical, _DEFAULT_DELTA)
        start_idx = max(0, total - limit)

        candles: list[Candle] = []
        for i in range(start_idx, total):
            # Timestamp kline MEXC dinyatakan dalam detik
            open_time = datetime.fromtimestamp(int(times[i]), tz=timezone.utc)
            close_time = open_time + delta
            candles.append(
                Candle(
                    open_time=open_time,
                    open=parse_decimal(opens[i], field="open", context=context),
                    high=parse_decimal(highs[i], field="high", context=context),
                    low=parse_decimal(lows[i], field="low", context=context),
                    close=parse_decimal(closes[i], field="close", context=context),
                    volume=parse_decimal(vols[i], field="vol", context=context),
                    close_time=close_time,
                    is_closed=self._now() >= close_time,
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
        """Ambil harga pasar terkini dari ticker MEXC Futures."""

        target = cfg.provider_symbol(cfg.PROVIDER_MEXC, symbol or self._config.symbol)
        context = f"{target} harga"

        payload = self._request(TICKER_PATH, {"symbol": target}, context=context)
        data = payload.get("data") if isinstance(payload, dict) else None

        price: Decimal | None = None
        if isinstance(data, dict):
            raw_price = data.get("lastPrice") or data.get("fairPrice")
            if raw_price is not None:
                price = parse_decimal(raw_price, field="lastPrice", context=context)

        if price is None or price <= 0:
            if reference_candle is not None:
                return closed_ltf_fallback_price(
                    [reference_candle], interval=self._config.ltf_interval
                )
            raise NetworkError(
                f"Tidak dapat memperoleh harga terkini untuk {target} dari MEXC Futures."
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
        """Ambil waktu server MEXC dari endpoint ping, fallback ke jam host jika gagal."""

        try:
            payload = self._request(PING_PATH, {}, context="waktu server MEXC")
            if isinstance(payload, dict) and payload.get("data") is not None:
                return ms_to_utc(payload["data"])
        except Exception:
            pass
        return self._now()
