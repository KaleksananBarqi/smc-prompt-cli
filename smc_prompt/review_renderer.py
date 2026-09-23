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


@dataclass(frozen=True)
class TradeJournalSpec:
    """Spesifikasi jurnal trade pasca-eksekusi untuk laporan review."""

    direction: str | None = None
    entry_price: Decimal | None = None
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None
    exit_price: Decimal | None = None       # Harga riil saat posisi ditutup / hit
    outcome: str | None = None
    notes: str | None = None

    @property
    def is_empty(self) -> bool:
        """True jika tidak ada satu pun field jurnal yang diisi."""
        return not any([
            self.direction,
            self.entry_price is not None,
            self.sl_price is not None,
            self.tp_price is not None,
            self.exit_price is not None,
            self.outcome,
            self.notes,
        ])

    @property
    def risk_reward_ratio(self) -> Decimal | None:
        """Kalkulasi rasio R:R terencana (planned Risk to Reward)."""
        if self.entry_price is None or self.sl_price is None or self.tp_price is None:
            return None
        risk = abs(self.entry_price - self.sl_price)
        reward = abs(self.tp_price - self.entry_price)
        if risk == Decimal(0):
            return None
        return (reward / risk).quantize(Decimal("0.01"))

    @property
    def realized_pnl_points(self) -> Decimal | None:
        """Selisih poin harga riil yang terealisasi (exit vs entry)."""
        if self.entry_price is None or self.exit_price is None:
            return None
        is_short = (self.direction or "").lower() == "short"
        diff = (
            self.entry_price - self.exit_price
            if is_short
            else self.exit_price - self.entry_price
        )
        return diff

    @property
    def realized_r_multiple(self) -> Decimal | None:
        """Realisasi R-Multiple dari trade (Realized R)."""
        pnl = self.realized_pnl_points
        if pnl is None or self.entry_price is None or self.sl_price is None:
            return None
        risk = abs(self.entry_price - self.sl_price)
        if risk == Decimal(0):
            return None
        return (pnl / risk).quantize(Decimal("0.01"))


def _build_ai_guide(
    j: "TradeJournalSpec",
    fmt: "object",  # callable PriceFormat.fmt
) -> list[str]:
    """Susun blok panduan evaluasi terstruktur untuk LLM berdasarkan data jurnal.

    Blok ini memastikan model AI tidak perlu menebak harga entry/exit, sehingga
    analisis yang dihasilkan lebih akurat dan tidak berhalusinasi.
    """
    # Callable fmt diterima sebagai Any dari closure render_review_markdown
    _fmt = fmt  # type: ignore[assignment]

    lines: list[str] = []
    lines.append("> 💡 **Panduan Evaluasi untuk AI (SMC/ICT Post-Trade Review):**")
    lines.append(
        "> Evaluasi trade di atas berdasarkan sekuens pergerakan harga pada Data Candle Mentah (OHLCV) di bawah."
    )
    lines.append("> Berikan analisis objektif mencakup:")

    q_num = 1

    # Pertanyaan kualitas entry
    if j.entry_price is not None:
        discount_word = "area Discount/POI" if (j.direction or "").lower() == "long" else "area Premium/POI"
        lines.append(
            f"> {q_num}. **Kualitas Entry:** Apakah eksekusi entry di {_fmt(j.entry_price)}"
            f" sudah tepat pada {discount_word}?"
        )
        q_num += 1

    # Pertanyaan perjalanan harga & trade management
    if j.entry_price is not None and j.exit_price is not None:
        pnl = j.realized_pnl_points
        realized_r = j.realized_r_multiple
        r_sign = "+" if (realized_r or Decimal(0)) >= Decimal(0) else ""
        pnl_sign = "+" if (pnl or Decimal(0)) >= Decimal(0) else ""
        pnl_str = f"{pnl_sign}{_fmt(pnl)} poin" if pnl is not None else "N/A"
        r_str = f"{r_sign}{realized_r}R" if realized_r is not None else "N/A"
        lines.append(
            f"> {q_num}. **Trade Management \u0026 Perjalanan Harga:** Seberapa jauh harga sempat"
            f" menguntungkan sebelum berbalik arah? Apakah ada sinyal struktural (CHoCH /"
            f" liquidity sweep) yang dapat digunakan untuk mengamankan profit lebih awal?"
        )
        q_num += 1

        lines.append(
            f"> {q_num}. **Keputusan Exit:** Apakah penutupan di {_fmt(j.exit_price)}"
            f" ({r_str} / {pnl_str})"
            f" merupakan keputusan yang optimal berdasarkan struktur pasar saat itu?"
        )
        q_num += 1
    elif j.exit_price is not None:
        lines.append(
            f"> {q_num}. **Keputusan Exit:** Apakah penutupan di {_fmt(j.exit_price)}"
            f" merupakan keputusan yang optimal berdasarkan struktur pasar saat itu?"
        )
        q_num += 1

    # Pertanyaan SL placement
    if j.sl_price is not None and j.entry_price is not None:
        lines.append(
            f"> {q_num}. **Penempatan SL:** Apakah SL di {_fmt(j.sl_price)} sudah ditempatkan"
            f" di balik struktur yang valid (below swing low / above swing high)?"
        )
        q_num += 1

    # Rekomendasi
    lines.append(
        f"> {q_num}. **Rekomendasi:** Apa aturan manajemen risiko atau trade management yang"
        f" perlu diperbaiki untuk setup serupa berikutnya?"
    )

    return lines


def render_review_markdown(
    symbol: str,
    interval: str,
    candles: Sequence[Candle],
    *,
    provider_label: str = "Binance",
    generated_at: datetime | None = None,
    price_format: cfg.PriceFormat | None = None,
    journal: TradeJournalSpec | None = None,
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

    # Render checklist jurnal trade
    j = journal
    if j and not j.is_empty:
        dir_line = (
            f"- [x] **Arah Posisi:** {j.direction.capitalize()}"
            if j.direction
            else "- [ ] **Arah Posisi:** Long / Short"
        )
        entry_line = (
            f"- [x] **Level Entry:** {fmt(j.entry_price)}"
            if j.entry_price is not None
            else "- [ ] **Level Entry:**"
        )

        if j.sl_price is not None:
            risk_pts = (
                abs(j.entry_price - j.sl_price) if j.entry_price is not None else None
            )
            risk_str = f" (Risk: {fmt(risk_pts)} poin)" if risk_pts is not None else ""
            sl_line = f"- [x] **Stop Loss (SL):** {fmt(j.sl_price)}{risk_str}"
        else:
            sl_line = "- [ ] **Stop Loss (SL):**"

        if j.tp_price is not None:
            rr = j.risk_reward_ratio
            rr_str = f" (Planned R:R 1:{rr})" if rr is not None else ""
            tp_line = f"- [x] **Take Profit (TP):** {fmt(j.tp_price)}{rr_str}"
        else:
            tp_line = "- [ ] **Take Profit (TP):**"

        # Baris Level Exit (Harga Hit) - harga riil saat posisi ditutup
        if j.exit_price is not None:
            exit_line = f"- [x] **Level Exit (Harga Hit):** {fmt(j.exit_price)}"
        else:
            exit_line = "- [ ] **Level Exit (Harga Hit):**"

        # Baris Hasil Akhir + Realized R-Multiple jika data cukup
        if j.outcome:
            realized_r = j.realized_r_multiple
            pnl = j.realized_pnl_points
            if realized_r is not None and pnl is not None:
                r_sign = "+" if realized_r >= Decimal(0) else ""
                pnl_sign = "+" if pnl >= Decimal(0) else ""
                realized_str = (
                    f" (Realized: {r_sign}{realized_r}R | PnL: {pnl_sign}{fmt(pnl)} poin)"
                )
            else:
                realized_str = ""
            outcome_line = f"- [x] **Hasil Akhir:** {j.outcome}{realized_str}"
        else:
            outcome_line = "- [ ] **Hasil Akhir:** Hit TP / Hit SL / BE / Cut Manual"

        if j.notes:
            notes_lines = [
                "- [x] **Evaluasi / Catatan:**",
                f"  > {j.notes}",
            ]
        else:
            notes_lines = [
                "- [ ] **Evaluasi / Catatan:**",
                "  > *Tulis evaluasi di sini (misal: reaksi harga di FVG, liquidity sweep, eksekusi, dll.)...*",
            ]
        journal_lines = [
            dir_line,
            entry_line,
            sl_line,
            tp_line,
            exit_line,
            outcome_line,
            *notes_lines,
        ]

        # Blok panduan evaluasi AI — aktif hanya jika ada data numerik yang presisi
        if j.entry_price is not None or j.exit_price is not None:
            ai_guide_lines = _build_ai_guide(j, fmt)
            journal_lines += ["", "---", ""] + ai_guide_lines
    else:
        journal_lines = [
            "- [ ] **Arah Posisi:** Long / Short",
            "- [ ] **Level Entry:**",
            "- [ ] **Stop Loss (SL):**",
            "- [ ] **Take Profit (TP):**",
            "- [ ] **Level Exit (Harga Hit):**",
            "- [ ] **Hasil Akhir:** Hit TP / Hit SL / BE / Cut Manual",
            "- [ ] **Evaluasi / Catatan:**",
            "  > *Tulis evaluasi di sini (misal: reaksi harga di FVG, liquidity sweep, eksekusi, dll.)...*",
        ]

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
        *journal_lines,
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


