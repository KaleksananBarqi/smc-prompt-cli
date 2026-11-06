"""Post-trade review renderer: candle statistics, excursion metrics, and clean markdown export.

Generates a focused post-review artifact containing:
  1. Metadata header (symbol, interval, provider, generation time)
  2. Session price movement statistics (open, close, net change, highest/lowest, range)
  3. Structured journaling checklist & SMC v2 evaluation template (Dual-Entry, MFE/MAE)
  4. Optional Layer-A structural context (Dealing Range, EQ, Swings, FVG ce, OB mt)
  5. Raw OHLCV CSV table
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
from .trade_plan import ExcursionResult, TradePlan, build_trade_plan


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
    """Spesifikasi jurnal trade pasca-eksekusi untuk laporan review SMC v2."""

    direction: str | None = None
    entry_price: Decimal | None = None  # Entry 1 (Aggressive)
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None  # TP1 (Nearest DOL)
    exit_price: Decimal | None = None  # Harga riil saat posisi ditutup / hit
    outcome: str | None = None
    notes: str | None = None
    entry2_price: Decimal | None = None  # Entry 2 (Moderate)
    tp2_price: Decimal | None = None  # TP2 (Main DOL)
    filled_legs: str | None = None  # 'e1', 'e2', 'both'
    excursion: ExcursionResult | None = None

    @property
    def is_empty(self) -> bool:
        """True jika tidak ada satu pun field jurnal yang diisi."""
        return not any(
            [
                self.direction,
                self.entry_price is not None,
                self.entry2_price is not None,
                self.sl_price is not None,
                self.tp_price is not None,
                self.tp2_price is not None,
                self.exit_price is not None,
                self.outcome,
                self.notes,
            ]
        )

    @property
    def is_dual_entry(self) -> bool:
        """True jika jurnal memuat spesifikasi Entry 2."""
        return self.entry2_price is not None

    @property
    def blended_entry(self) -> Decimal | None:
        """Kalkulasi harga rata-rata tertimbang Blended Entry."""
        if self.entry_price is None:
            return None
        if self.entry2_price is None:
            return self.entry_price
        w1, w2 = cfg.DEFAULT_DUAL_ENTRY_WEIGHTS
        return (w1 * self.entry_price) + (w2 * self.entry2_price)

    @property
    def active_entry(self) -> Decimal | None:
        """Harga entry acuan yang terealisasi berdasarkan leg yang terisi."""
        if self.filled_legs == "e1":
            return self.entry_price
        if self.filled_legs == "e2":
            return self.entry2_price
        return self.blended_entry

    @property
    def risk_reward_ratio(self) -> Decimal | None:
        """Kalkulasi rasio R:R terencana ke TP1."""
        ref_entry = self.active_entry
        if ref_entry is None or self.sl_price is None or self.tp_price is None:
            return None
        risk = abs(ref_entry - self.sl_price)
        reward = abs(self.tp_price - ref_entry)
        if risk == Decimal(0):
            return None
        return (reward / risk).quantize(Decimal("0.01"))

    @property
    def risk_reward_ratio_tp2(self) -> Decimal | None:
        """Kalkulasi rasio R:R terencana ke TP2."""
        ref_entry = self.active_entry
        if ref_entry is None or self.sl_price is None or self.tp2_price is None:
            return None
        risk = abs(ref_entry - self.sl_price)
        reward = abs(self.tp2_price - ref_entry)
        if risk == Decimal(0):
            return None
        return (reward / risk).quantize(Decimal("0.01"))

    @property
    def realized_pnl_points(self) -> Decimal | None:
        """Selisih poin harga riil yang terealisasi (exit vs entry acuan)."""
        ref_entry = self.active_entry
        if ref_entry is None or self.exit_price is None:
            return None
        is_short = (self.direction or "").lower() == "short"
        diff = (
            ref_entry - self.exit_price
            if is_short
            else self.exit_price - ref_entry
        )
        return diff

    @property
    def realized_r_multiple(self) -> Decimal | None:
        """Realisasi R-Multiple dari trade (Realized R)."""
        pnl = self.realized_pnl_points
        ref_entry = self.active_entry
        if pnl is None or ref_entry is None or self.sl_price is None:
            return None
        risk = abs(ref_entry - self.sl_price)
        if risk == Decimal(0):
            return None
        return (pnl / risk).quantize(Decimal("0.01"))


def _build_ai_guide(
    j: TradeJournalSpec,
    fmt: object,  # callable PriceFormat.fmt
) -> list[str]:
    """Susun blok panduan evaluasi terstruktur untuk LLM berdasarkan data jurnal SMC v2."""
    _fmt = fmt  # type: ignore[assignment]

    lines: list[str] = []
    lines.append("> 💡 **Panduan Evaluasi untuk AI (SMC/ICT Post-Trade Review):**")
    lines.append(
        "> Evaluasi trade di atas berdasarkan sekuens pergerakan harga pada Data Candle Mentah (OHLCV) dan Fakta Ekskursi Mekanis di bawah."
    )
    lines.append(
        "> Berikan analisis objektif dengan menyertakan label **[FAKTA]** / **[INFERENSI]** / **[SPEKULASI]** mencakup:"
    )

    q_num = 1

    # Pertanyaan kualitas entry & Dealing Range
    if j.active_entry is not None:
        discount_word = (
            "area Discount thd EQ 50% / POI"
            if (j.direction or "").lower() == "long"
            else "area Premium thd EQ 50% / POI"
        )
        lines.append(
            f"> {q_num}. **Kualitas Entry & Dealing Range:** Apakah eksekusi entry di {_fmt(j.active_entry)}"
            f" sudah tepat pada {discount_word}?"
        )
        q_num += 1

    # Pertanyaan efektivitas dual-entry
    if j.is_dual_entry:
        lines.append(
            f"> {q_num}. **Efektivitas Skema Dual-Entry:** Apakah Entry 1 (Aggressive) dengan buffer 0.10x ATR berhasil mengantisipasi front-run? Apakah Entry 2 (Moderate di CE/MT) sempat terisi atau terlewati?"
        )
        q_num += 1

    # Pertanyaan perjalanan harga & trade management
    if j.active_entry is not None and j.exit_price is not None:
        pnl = j.realized_pnl_points
        realized_r = j.realized_r_multiple
        r_sign = "+" if (realized_r or Decimal(0)) >= Decimal(0) else ""
        pnl_sign = "+" if (pnl or Decimal(0)) >= Decimal(0) else ""
        pnl_str = f"{pnl_sign}{_fmt(pnl)} poin" if pnl is not None else "N/A"
        r_str = f"{r_sign}{realized_r}R" if realized_r is not None else "N/A"

        mfe_hint = ""
        if j.excursion and j.excursion.mfe_price is not None:
            mfe_r_str = f" (+{j.excursion.mfe_r}R)" if j.excursion.mfe_r is not None else ""
            mfe_hint = f" (MFE tercapai di {_fmt(j.excursion.mfe_price)}{mfe_r_str})"

        lines.append(
            f"> {q_num}. **Trade Management & Ekskursi Harga{mfe_hint}:** Seberapa jauh harga sempat"
            f" menguntungkan sebelum berbalik arah? Apakah ada sinyal struktural (CHoCH /"
            f" liquidity sweep) yang seharusnya dapat digunakan untuk mengamankan profit / trailing stop lebih awal?"
        )
        q_num += 1

        lines.append(
            f"> {q_num}. **Keputusan Exit & DOL:** Apakah penutupan di {_fmt(j.exit_price)}"
            f" ({r_str} / {pnl_str})"
            f" merupakan keputusan yang optimal berdasarkan struktur pasar dan target likuiditas saat itu?"
        )
        q_num += 1
    elif j.exit_price is not None:
        lines.append(
            f"> {q_num}. **Keputusan Exit:** Apakah penutupan di {_fmt(j.exit_price)}"
            f" merupakan keputusan yang optimal berdasarkan struktur pasar saat itu?"
        )
        q_num += 1

    # Pertanyaan SL placement
    if j.sl_price is not None and j.active_entry is not None:
        lines.append(
            f"> {q_num}. **Penempatan SL:** Apakah SL di {_fmt(j.sl_price)} sudah ditempatkan"
            f" di balik struktur yang valid (below swing low / above swing high) dengan buffer spread/fee yang cukup?"
        )
        q_num += 1

    # Rekomendasi
    lines.append(
        f"> {q_num}. **Rekomendasi Manajemen Risiko:** Apa aturan eksekusi (Mode Continuation vs Mode Sweep) atau trade management yang"
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
    structure_block: str | None = None,
) -> str:
    """Render the post-trade review markdown document."""

    if not candles:
        raise ConfigError("Cannot render review markdown: no candles provided.")

    moment = generated_at or datetime.now(timezone.utc)
    fmt = (price_format or cfg.PriceFormat()).fmt
    is_daily = not cfg.is_intraday_interval(interval)
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

        if j.is_dual_entry:
            e1_str = fmt(j.entry_price) if j.entry_price is not None else "-"
            e2_str = fmt(j.entry2_price) if j.entry2_price is not None else "-"
            blended_str = fmt(j.blended_entry) if j.blended_entry is not None else "-"
            entry_line = (
                f"- [x] **Level Entry (Dual-Entry):** Entry 1 (Aggressive) = {e1_str} | "
                f"Entry 2 (Moderate) = {e2_str} | Blended = {blended_str}"
            )
        else:
            entry_line = (
                f"- [x] **Level Entry:** {fmt(j.entry_price)}"
                if j.entry_price is not None
                else "- [ ] **Level Entry:**"
            )

        if j.sl_price is not None:
            ref_entry = j.active_entry
            risk_pts = (
                abs(ref_entry - j.sl_price) if ref_entry is not None else None
            )
            risk_str = f" (1R Risk: {fmt(risk_pts)} poin)" if risk_pts is not None else ""
            sl_line = f"- [x] **Stop Loss (SL):** {fmt(j.sl_price)}{risk_str}"
        else:
            sl_line = "- [ ] **Stop Loss (SL):**"

        if j.tp_price is not None:
            rr = j.risk_reward_ratio
            rr_str = f" (Planned R:R 1:{rr})" if rr is not None else ""
            tp_label = "Take Profit (TP1)" if j.tp2_price is not None else "Take Profit (TP)"
            tp_line = f"- [x] **{tp_label}:** {fmt(j.tp_price)}{rr_str}"
        else:
            tp_line = "- [ ] **Take Profit (TP):**"

        tp2_lines: list[str] = []
        if j.tp2_price is not None:
            rr2 = j.risk_reward_ratio_tp2
            rr2_str = f" (Planned R:R 1:{rr2})" if rr2 is not None else ""
            tp2_lines.append(f"- [x] **Take Profit (TP2 - Main DOL):** {fmt(j.tp2_price)}{rr2_str}")

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

        # Ekskursi Harga Mekanis (MFE/MAE/First-Touch) jika tersedia
        excursion_lines: list[str] = []
        if j.excursion and j.excursion.mfe_price is not None:
            exc = j.excursion
            mfe_r_str = f" (+{exc.mfe_r}R)" if exc.mfe_r is not None else ""
            mae_r_str = f" (-{exc.mae_r}R)" if exc.mae_r is not None else ""
            excursion_lines.extend(
                [
                    "",
                    "### Fakta Ekskursi Harga Mekanis (Post-Entry MFE / MAE)",
                    f"- **Maximum Favorable Excursion (MFE):** {fmt(exc.mfe_price)}{mfe_r_str} (keuntungan maksimal yang sempat tersentuh)",
                    f"- **Maximum Adverse Excursion (MAE):** {fmt(exc.mae_price)}{mae_r_str} (penurunan terdalam yang sempat ditahan)",
                ]
            )
            if exc.first_touch_sequence:
                excursion_lines.append(
                    f"- **Urutan Sentuh Pertama (First-Touch):** {' ➔ '.join(t.upper() for t in exc.first_touch_sequence)}"
                )
            if exc.first_fill_killzone:
                excursion_lines.append(
                    f"- **Sesi Killzone saat Fill:** {exc.first_fill_killzone}"
                )

        journal_lines = [
            dir_line,
            entry_line,
            sl_line,
            tp_line,
            *tp2_lines,
            exit_line,
            outcome_line,
            *notes_lines,
            *excursion_lines,
        ]

        # Blok panduan evaluasi AI — aktif hanya jika ada data numerik yang presisi
        if j.entry_price is not None or j.exit_price is not None:
            ai_guide_lines = _build_ai_guide(j, fmt)
            journal_lines.extend(["", "---", "", *ai_guide_lines])
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

    structure_section: list[str] = []
    if structure_block:
        structure_section = [
            "---",
            "",
            structure_block,
            "",
        ]

    lines = [
        f"# Post-Trade Review: {symbol.upper()} ({interval_lbl})",
        "",
        f"- **Symbol:** {symbol.upper()}",
        f"- **Provider:** {provider_label}",
        f"- **Interval:** {interval} ({interval_lbl})",
        f"- **Jumlah Candle:** {stats.candle_count} candle (closed)",
        f"- **Rentang Waktu (UTC):** {_fmt_ts(stats.start_time)} s/d {_fmt_ts(stats.end_time)}",
        f"- **Sesi Pasar (UTC):** {cfg.fmt_session_killzone(moment)}",
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
        *structure_section,
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
