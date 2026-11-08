"""Offline / local CSV data source (Phase 4, scope #10).

Network-free sibling of :class:`smc_prompt.data_fetcher.DataFetcher`. It exposes
the **same public shape** (``validate_symbol`` / ``fetch_klines`` /
``fetch_current_price`` / ``fetch_server_time`` / ``price_notes`` / ``with_now``)
so ``cli.run`` can drive either source without branching through the analysis
pipeline. The pipeline itself is untouched: it already consumes
``Sequence[Candle]``.

CSV schema (one file per timeframe, header row required):

    open_time,open,high,low,close,volume[,close_time]

* ``open_time``     — ISO-8601 UTC (``2026-07-15T00:00:00Z`` / ``... +00:00`` /
                      ``2026-07-15 00:00`` / ``2026-07-15``); a pure-integer
                      value is interpreted as epoch milliseconds.
* ``open/high/low/close/volume`` — decimal strings (parsed as :class:`Decimal`).
* ``close_time``    — optional; when absent it is derived as
                      ``open_time + delta`` where ``delta`` is the first positive
                      spacing between consecutive ``open_time`` values.

Rows are treated as **already closed** (an offline snapshot *is* closed history),
which keeps the offline render fully deterministic and independent of the host
clock. ``fetch_server_time`` returns ``max(close_time) + 1s`` so the derived
``GENERATED_AT_UTC`` is a deterministic function of the input file alone.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

from . import config as cfg
from .errors import ConfigError
from .models import Candle
from .provider_base import SymbolMetadata

#: Required CSV column names (case-insensitive, matched after stripping).
_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {"open_time", "open", "high", "low", "close", "volume"}
)

#: Optional CSV column names.
_OPTIONAL_COLUMNS: frozenset[str] = frozenset({"close_time"})


@dataclass(frozen=True)
class CsvSeries:
    """A parsed CSV candle series plus derived metadata."""

    path: str
    candles: tuple[Candle, ...]
    last_close_time: datetime


def _parse_time(raw: str, *, path: str, line_no: int) -> datetime:
    """Parse one timestamp cell into an aware UTC datetime.

    Accepts ISO-8601 with an explicit offset/``Z``, a naive ISO-8601 value
    (assumed UTC), a bare ``YYYY-MM-DD`` date, or epoch milliseconds.
    """

    value = (raw or "").strip()
    if not value:
        raise ConfigError(
            f"{path}: line {line_no} has an empty open_time."
        )

    if value.lstrip("-").isdigit():
        try:
            return datetime.fromtimestamp(
                int(value) / 1000.0, tz=timezone.utc
            )
        except (OverflowError, OSError, ValueError) as exc:
            raise ConfigError(
                f"{path}: line {line_no} has an out-of-range epoch "
                f"timestamp '{value}' ({exc})."
            ) from exc

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ConfigError(
            f"{path}: line {line_no} has an invalid timestamp '{value}' "
            f"({exc}). Expected ISO-8601 UTC."
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_decimal(raw: str, *, path: str, line_no: int, column: str) -> Decimal:
    """Parse one numeric cell into a :class:`Decimal`."""

    value = (raw or "").strip()
    if not value:
        raise ConfigError(
            f"{path}: line {line_no} has an empty {column} value."
        )
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ConfigError(
            f"{path}: line {line_no} has an invalid {column} value "
            f"'{value}' ({exc})."
        ) from exc


def _sniff_delta(open_times: Sequence[datetime]) -> timedelta:
    """Infer the candle spacing from consecutive open times.

    Returns the first positive spacing (records are chronological and uniform in
    practice); falls back to one hour when no positive spacing exists (a single
    row, or all rows sharing a timestamp).
    """

    for earlier, later in zip(open_times, open_times[1:]):
        delta = later - earlier
        if delta > timedelta(0):
            return delta
    return timedelta(hours=1)


def load_csv_series(path: str, *, interval: str) -> CsvSeries:
    """Parse an OHLCV CSV file into a :class:`CsvSeries`.

    Raises :class:`ConfigError` (exit 2) when the file is missing, the header
    lacks a required column, or a row is malformed.
    """

    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(
            f"Offline CSV file not found: {path}. Provide --input-csv (used "
            f"for all three series) or explicit --htf-file / --mtf-file / "
            f"--ltf-file paths."
        )

    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"Could not read offline CSV {path} ({exc}).") from exc

    reader = csv.reader(text.splitlines())
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    if not rows:
        raise ConfigError(f"Offline CSV {path} is empty.")

    header = [cell.strip().lower() for cell in rows[0]]
    missing = _REQUIRED_COLUMNS - set(header)
    if missing:
        raise ConfigError(
            f"Offline CSV {path} is missing required column(s): "
            f"{', '.join(sorted(missing))}. Expected header: "
            f"open_time,open,high,low,close,volume[,close_time]."
        )

    index = {name: header.index(name) for name in header}
    has_close = "close_time" in index

    parsed: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal,
                       datetime | None]] = []
    for offset, row in enumerate(rows[1:], start=2):
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        open_time = _parse_time(
            row[index["open_time"]], path=path, line_no=offset
        )
        close_time = (
            _parse_time(row[index["close_time"]], path=path, line_no=offset)
            if has_close
            else None
        )
        parsed.append(
            (
                open_time,
                _parse_decimal(
                    row[index["open"]], path=path, line_no=offset, column="open"
                ),
                _parse_decimal(
                    row[index["high"]], path=path, line_no=offset, column="high"
                ),
                _parse_decimal(
                    row[index["low"]], path=path, line_no=offset, column="low"
                ),
                _parse_decimal(
                    row[index["close"]], path=path, line_no=offset, column="close"
                ),
                _parse_decimal(
                    row[index["volume"]],
                    path=path,
                    line_no=offset,
                    column="volume",
                ),
                close_time,
            )
        )

    if not parsed:
        raise ConfigError(
            f"Offline CSV {path} contains a header but no candle rows."
        )

    parsed.sort(key=lambda item: item[0])
    open_times = [item[0] for item in parsed]
    delta = _sniff_delta(open_times)

    last_close_time = parsed[-1][6] or (parsed[-1][0] + delta)

    candles = tuple(
        Candle(
            open_time=open_time,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            close_time=close_time or (open_time + delta),
            # Offline history is a closed snapshot: every row is closed.
            is_closed=True,
        )
        for (
            open_time,
            open_,
            high,
            low,
            close,
            volume,
            close_time,
        ) in parsed
    )

    return CsvSeries(path=path, candles=candles, last_close_time=last_close_time)


class LocalCsvSource:
    """Network-free data source reading local OHLCV CSV files.

    Mirrors the :class:`~smc_prompt.data_fetcher.DataFetcher` public interface so
    ``cli.run`` treats both interchangeably. Only the fetcher/source touches
    I/O; the analysis pipeline stays pure.
    """

    def __init__(
        self,
        config: cfg.Config,
        *,
        htf_file: str,
        mtf_file: str,
        ltf_file: str,
    ) -> None:
        self._config = config
        self._htf_file = htf_file
        self._mtf_file = mtf_file
        self._ltf_file = ltf_file

        # Defense-in-depth: ``build_config`` already enforces three DISTINCT
        # intervals, but a directly-constructed source must not silently map
        # two tiers to one series either.
        if len(
            {config.htf_interval, config.mtf_interval, config.ltf_interval}
        ) != 3:
            raise ConfigError(
                "Offline mode requires distinct --htf-interval, --mtf-interval "
                "and --ltf-interval values so each CSV maps to one timeframe "
                f"(got htf={config.htf_interval}, mtf={config.mtf_interval}, "
                f"ltf={config.ltf_interval})."
            )

        self._series: dict[str, CsvSeries] = {
            config.htf_interval: load_csv_series(
                htf_file, interval=config.htf_interval
            ),
            config.mtf_interval: load_csv_series(
                mtf_file, interval=config.mtf_interval
            ),
            config.ltf_interval: load_csv_series(
                ltf_file, interval=config.ltf_interval
            ),
        }
        self._price_notes: list[str] = []
        self._now = max(
            series.last_close_time for series in self._series.values()
        ) + timedelta(seconds=1)

    # ------------------------------------------------------------------
    # Interface parity with DataFetcher
    # ------------------------------------------------------------------

    @property
    def price_notes(self) -> tuple[str, ...]:
        """Non-fatal notes recorded by the last :meth:`fetch_current_price`."""

        return tuple(self._price_notes)

    @property
    def active_base_url(self) -> str | None:
        """Always ``None``: the local source has no host."""

        return None

    def with_now(self, moment: datetime) -> "LocalCsvSource":
        """Return this source unchanged (offline rows are already closed).

        Provided for interface parity with :meth:`DataFetcher.with_now`; the
        offline ``now`` is derived from the CSV so it never depends on the host
        clock.
        """

        return self

    def validate_symbol(self) -> SymbolMetadata:
        """Return a structured :class:`SymbolMetadata` for offline CSV mode."""

        return SymbolMetadata(symbol=self._config.symbol, status="TRADING", tick_size=None)

    def fetch_server_time(self) -> datetime:
        """Return a deterministic instant derived from the CSV files.

        ``max(close_time) + 1s`` ensures every offline row counts as closed and
        makes ``GENERATED_AT_UTC`` a pure function of the input data.
        """

        return self._now

    def fetch_klines(
        self, interval: str, limit: int, *, symbol: str | None = None
    ) -> list[Candle]:
        """Return the last ``limit`` offline candles for ``interval``.

        Mirrors the network fetcher's "most recent ``limit`` rows" behaviour.
        """

        series = self._series.get(interval)
        if series is None:
            raise ConfigError(
                f"Offline CSV source has no data for interval '{interval}'. "
                f"Available: {', '.join(sorted(self._series))}."
            )
        if limit <= 0:
            return list(series.candles)
        return list(series.candles[-limit:])

    def fetch_current_price(
        self,
        *,
        reference_candle: Candle | None = None,
        tolerance: Decimal | None = None,
    ) -> Decimal:
        """Return the last *closed* LTF candle close (deterministic, no network).

        Offline mode has no ticker endpoint; the last closed candle close is the
        canonical, reproducible "current price" for a historical snapshot.
        """

        self._price_notes = []
        ltf = self._series.get(self._config.ltf_interval)
        if ltf is None or not ltf.candles:  # pragma: no cover - guarded earlier
            raise ConfigError(
                "Offline CSV source has no LTF candles to derive a current "
                "price from."
            )
        return ltf.candles[-1].close
