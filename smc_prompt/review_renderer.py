"""Post-trade review renderer: candle statistics and clean markdown export.

Generates a focused post-review artifact containing:
  1. Metadata header (symbol, interval, provider, generation time)
  2. Session price movement statistics (open, close, net change, highest/lowest, range)
  3. Structured journaling checklist & notes template
  4. Raw OHLCV CSV table
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from . import config as cfg
from .errors import ConfigError
from .models import Candle
from .template_renderer import render_csv_table


@dataclass(frozen=True)
class SessionStats:
    """Summary of price action across the reviewed candle window."""

    start_time: datetime
    end_time: datetime
    first_open: Decimal
    last_close: Decimal
    net_change: Decimal
    net_change_pct: Decimal
    highest_price: Decimal
    highest_time: datetime
    lowest_price: Decimal
    lowest_time: datetime
    total_range: Decimal
    candle_count: int


def compute_session_stats(candles: Sequence[Candle]) -> SessionStats:
    """Compute aggregate price movement metrics for the candle series."""

    if not candles:
        raise ConfigError("Cannot compute session stats: no candles provided.")

    first_candle = candles[0]
    last_candle = candles[-1]
    start_time = first_candle.open_time
    end_time = last_candle.close_time
    first_open = first_candle.open
    last_close = last_candle.close

    net_change = last_close - first_open
    if first_open != Decimal(0):
        net_change_pct = (net_change / first_open) * Decimal(100)
    else:
        net_change_pct = Decimal(0)

    highest_candle = max(candles, key=lambda c: c.high)
    lowest_candle = min(candles, key=lambda c: c.low)

    highest_price = highest_candle.high
    highest_time = highest_candle.open_time
    lowest_price = lowest_candle.low
    lowest_time = lowest_candle.open_time
    total_range = highest_price - lowest_price

    return SessionStats(
        start_time=start_time,
        end_time=end_time,
        first_open=first_open,
        last_close=last_close,
        net_change=net_change,
        net_change_pct=net_change_pct,
        highest_price=highest_price,
        highest_time=highest_time,
        lowest_price=lowest_price,
        lowest_time=lowest_time,
        total_range=total_range,
        candle_count=len(candles),
    )


def render_review_markdown(
    symbol: str,
    interval: str,
    candles: Sequence[Candle],
    *,
    provider_label: str = "Binance",
    generated_at: datetime | None = None,
    price_format: cfg.PriceFormat | None = None,
) -> str:
    """Render the post-trade review markdown document."""

    if not candles:
        raise ConfigError("Cannot render review markdown: no candles provided.")

    moment = generated_at or datetime.now(timezone.utc)
    fmt = (price_format or cfg.PriceFormat()).fmt
    is_daily = interval == "1d"
    interval_lbl = cfg.interval_label(interval)

    def _fmt_ts(dt: datetime) -> str:
        return cfg.fmt_htf_date(dt) if is_daily else cfg.fmt_ltf_datetime(dt)

    stats = compute_session_stats(candles)
    csv_table = render_csv_table(candles, htf=is_daily, price_format=price_format)

    sign = "+" if stats.net_change >= 0 else "-"
    abs_change = abs(stats.net_change)
    abs_pct = abs(stats.net_change_pct)

    legend = (
        "tanggal(UTC),open,high,low,close,volume"
        if is_daily
        else "datetime(UTC),open,high,low,close,volume"
    )

    lines = [
        f"# Post-Trade Review: {symbol.upper()} ({interval_lbl})",
        "",
        f"- **Symbol:** {symbol.upper()}",
        f"- **Provider:** {provider_label}",
        f"- **Interval:** {interval} ({interval_lbl})",
        f"- **Jumlah Candle:** {stats.candle_count} candle (closed)",
        f"- **Rentang Waktu (UTC):** {_fmt_ts(stats.start_time)} s/d {_fmt_ts(stats.end_time)}",
        f"- **Waktu Generate (UTC):** {_fmt_ts(moment)}",
        "",
        "---",
        "",
        "## Ringkasan Pergerakan Harga (Session Stats)",
        "",
        f"- **Open Pertama:** {fmt(stats.first_open)}",
        f"- **Close Terakhir:** {fmt(stats.last_close)}",
        f"- **Perubahan Harga:** {sign}{fmt(abs_change)} ({sign}{abs_pct:.2f}%)",
        f"- **Harga Tertinggi (Highest):** {fmt(stats.highest_price)} pada {_fmt_ts(stats.highest_time)}",
        f"- **Harga Terendah (Lowest):** {fmt(stats.lowest_price)} pada {_fmt_ts(stats.lowest_time)}",
        f"- **Total Range:** {fmt(stats.total_range)}",
        "",
        "---",
        "",
        "## Catatan Jurnal & Evaluasi Trade",
        "",
        "- [ ] **Arah Posisi:** Long / Short",
        "- [ ] **Level Entry:**",
        "- [ ] **Stop Loss (SL):**",
        "- [ ] **Take Profit (TP):**",
        "- [ ] **Hasil Akhir:** Hit TP / Hit SL / BE / Cut Manual",
        "- [ ] **Evaluasi / Catatan:**",
        "  > *Tulis evaluasi di sini (misal: reaksi harga di FVG, liquidity sweep, eksekusi, dll.)...*",
        "",
        "---",
        "",
        "## Data Candle Mentah (OHLCV)",
        "",
        f"Format kolom: {legend}",
        "```csv",
        csv_table,
        "```",
        "",
    ]

    return "\n".join(lines)
