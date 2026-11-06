"""Trade plan representation, dual-entry schemas, and excursion tracking.

Provides:
  1. TradePlan: Immutable data contract for SMC trade setups (Entry 1 Aggressive,
     Entry 2 Moderate, Blended Entry, Stop Loss, TP1 Nearest DOL, TP2 Main DOL).
  2. build_trade_plan: Single validation & normalization builder for CLI and tests.
  3. compute_excursion: Mechanical MFE/MAE, first-touch, and fill tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from . import config as cfg
from .errors import ConfigError
from .models import Candle


@dataclass(frozen=True)
class TradePlan:
    """Spesifikasi rencana trading SMC dengan dukungan Dual-Entry & Multi-TP."""

    direction: str  # 'long' atau 'short'
    entry1: Decimal  # Entry 1 (Aggressive / Proximal Edge + Front-Run Buffer)
    tp1: Decimal  # TP1 (Nearest DOL / Target Utama Awal)
    entry2: Decimal | None = None  # Entry 2 (Moderate / 50% CE/MT / Post-Sweep)
    sl: Decimal | None = None  # Stop Loss terpadu (1 SL yang sama untuk kedua entry)
    tp2: Decimal | None = None  # TP2 (Main DOL / Target Ekspansi)

    @property
    def is_dual_entry(self) -> bool:
        """True bila trade plan menyertakan Entry 2 (Moderate)."""
        return self.entry2 is not None

    @property
    def has_tp2(self) -> bool:
        """True bila trade plan menyertakan TP2 (Main DOL)."""
        return self.tp2 is not None

    @property
    def blended_entry(self) -> Decimal:
        """Harga rata-rata tertimbang Blended Entry (0.4×E1 + 0.6×E2) atau E1 jika single."""
        if self.entry2 is None:
            return self.entry1
        w1, w2 = cfg.DEFAULT_DUAL_ENTRY_WEIGHTS
        return (w1 * self.entry1) + (w2 * self.entry2)

    @property
    def risk_1r_blended(self) -> Decimal | None:
        """Jarak risiko 1R acuan dari Blended Entry ke Stop Loss."""
        if self.sl is None:
            return None
        return abs(self.blended_entry - self.sl)

    @property
    def risk_1r_e1(self) -> Decimal | None:
        """Jarak risiko 1R mandiri dari Entry 1 ke Stop Loss."""
        if self.sl is None:
            return None
        return abs(self.entry1 - self.sl)

    @property
    def risk_1r_e2(self) -> Decimal | None:
        """Jarak risiko 1R mandiri dari Entry 2 ke Stop Loss."""
        if self.sl is None or self.entry2 is None:
            return None
        return abs(self.entry2 - self.sl)

    def rr_tp1(self, from_entry: Decimal | None = None) -> Decimal | None:
        """Kalkulasi RRR ke TP1 (default menggunakan Blended Entry)."""
        basis = from_entry if from_entry is not None else self.blended_entry
        if self.sl is None:
            return None
        risk = abs(basis - self.sl)
        if risk == Decimal(0):
            return None
        reward = abs(self.tp1 - basis)
        return (reward / risk).quantize(Decimal("0.01"))

    def rr_tp2(self, from_entry: Decimal | None = None) -> Decimal | None:
        """Kalkulasi RRR ke TP2 (default menggunakan Blended Entry)."""
        if self.tp2 is None or self.sl is None:
            return None
        basis = from_entry if from_entry is not None else self.blended_entry
        risk = abs(basis - self.sl)
        if risk == Decimal(0):
            return None
        reward = abs(self.tp2 - basis)
        return (reward / risk).quantize(Decimal("0.01"))


def _to_decimal(val: Decimal | float | int | str | None, name: str) -> Decimal | None:
    """Konversi nilai harga ke Decimal secara aman."""
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except Exception as exc:
        raise ConfigError(f"Nilai '{val}' untuk {name} tidak valid sebagai angka numerik ({exc}).")


def build_trade_plan(
    *,
    entry: Decimal | float | int | str | None = None,
    tp: Decimal | float | int | str | None = None,
    direction: str | None = None,
    sl: Decimal | float | int | str | None = None,
    entry1: Decimal | float | int | str | None = None,
    entry2: Decimal | float | int | str | None = None,
    tp1: Decimal | float | int | str | None = None,
    tp2: Decimal | float | int | str | None = None,
) -> TradePlan:
    """Bangun dan validasi instance TradePlan secara deterministik."""

    e1_raw = entry1 if entry1 is not None else entry
    tp1_raw = tp1 if tp1 is not None else tp

    if e1_raw is None or tp1_raw is None:
        raise ConfigError("Trade setup memerlukan minimal Entry 1 dan TP1.")

    e1 = _to_decimal(e1_raw, "Entry 1")
    t1 = _to_decimal(tp1_raw, "TP1")
    assert e1 is not None and t1 is not None

    if e1 == t1:
        raise ConfigError("--tp cannot be equal to --entry.")

    e2 = _to_decimal(entry2, "Entry 2")
    t2 = _to_decimal(tp2, "TP2")
    dec_sl = _to_decimal(sl, "Stop Loss")

    # Inferensi arah otomatis bila tidak diberikan
    if direction is not None:
        dir_clean = direction.strip().lower()
        if dir_clean not in ("long", "short"):
            raise ConfigError(f"Invalid direction '{direction}'; must be 'long' or 'short'.")
        dir_val = dir_clean
    else:
        dir_val = "long" if t1 > e1 else "short"

    if dir_val == "long":
        if t1 <= e1:
            raise ConfigError(f"Invalid LONG setup: TP ({t1}) must be strictly greater than Entry ({e1}).")
        if dec_sl is not None and dec_sl >= e1:
            raise ConfigError("For long setups, --sl must be lower than --entry.")

        if e2 is not None:
            if e2 > e1:
                raise ConfigError(
                    f"Setup LONG tidak valid: Entry 2 / Moderate ({e2}) harus lebih rendah atau sama dengan Entry 1 / Aggressive ({e1})."
                )
            if dec_sl is not None and dec_sl >= e2:
                raise ConfigError(
                    f"Setup LONG tidak valid: SL ({dec_sl}) harus lebih rendah dari Entry 2 ({e2})."
                )
            if t1 <= e2:
                raise ConfigError(
                    f"Setup LONG tidak valid: TP1 ({t1}) harus lebih tinggi dari Entry 2 ({e2})."
                )

        if t2 is not None:
            if t2 <= t1:
                raise ConfigError(
                    f"Setup LONG tidak valid: TP2 / Main DOL ({t2}) harus lebih tinggi dari TP1 ({t1})."
                )

    else:  # short
        if t1 >= e1:
            raise ConfigError(f"Invalid SHORT setup: TP ({t1}) must be strictly less than Entry ({e1}).")
        if dec_sl is not None and dec_sl <= e1:
            raise ConfigError("For short setups, --sl must be higher than --entry.")

        if e2 is not None:
            if e2 < e1:
                raise ConfigError(
                    f"Setup SHORT tidak valid: Entry 2 / Moderate ({e2}) harus lebih tinggi atau sama dengan Entry 1 / Aggressive ({e1})."
                )
            if dec_sl is not None and dec_sl <= e2:
                raise ConfigError(
                    f"Setup SHORT tidak valid: SL ({dec_sl}) harus lebih tinggi dari Entry 2 ({e2})."
                )
            if t1 >= e2:
                raise ConfigError(
                    f"Setup SHORT tidak valid: TP1 ({t1}) harus lebih rendah dari Entry 2 ({e2})."
                )

        if t2 is not None:
            if t2 >= t1:
                raise ConfigError(
                    f"Setup SHORT tidak valid: TP2 / Main DOL ({t2}) harus lebih rendah dari TP1 ({t1})."
                )

    return TradePlan(
        direction=dir_val,
        entry1=e1,
        entry2=e2,
        sl=dec_sl,
        tp1=t1,
        tp2=t2,
    )


@dataclass(frozen=True)
class ExcursionResult:
    """Hasil kalkulasi mekanis pergerakan harga pasca-entry (MFE / MAE / First-Touch)."""

    mfe_price: Decimal | None
    mfe_points: Decimal | None
    mfe_r: Decimal | None
    mae_price: Decimal | None
    mae_points: Decimal | None
    mae_r: Decimal | None
    e1_filled: bool
    e2_filled: bool
    e1_fill_time: datetime | None
    e2_fill_time: datetime | None
    first_fill_time: datetime | None
    first_fill_killzone: str | None
    first_touch_sequence: tuple[str, ...]
    sl_touched: bool
    tp1_touched: bool
    tp2_touched: bool


def compute_excursion(
    plan: TradePlan,
    candles: Sequence[Candle],
    *,
    order_status: str = "unfilled",
    active_entry_price: Decimal | None = None,
) -> ExcursionResult:
    """Kalkulasi mekanis MFE, MAE, dan urutan sentuh harga berdasarkan data candle."""

    if not candles:
        return ExcursionResult(
            mfe_price=None,
            mfe_points=None,
            mfe_r=None,
            mae_price=None,
            mae_points=None,
            mae_r=None,
            e1_filled=False,
            e2_filled=False,
            e1_fill_time=None,
            e2_fill_time=None,
            first_fill_time=None,
            first_fill_killzone=None,
            first_touch_sequence=(),
            sl_touched=False,
            tp1_touched=False,
            tp2_touched=False,
        )

    norm_status = order_status.lower().strip()
    is_long = plan.direction == "long"

    e1_candidates = [c for c in candles if c.low <= plan.entry1 <= c.high]
    e1_filled = bool(e1_candidates) or norm_status in ("partial", "filled")
    e1_fill_candle = e1_candidates[0] if e1_candidates else None
    e1_fill_time = e1_fill_candle.open_time if e1_fill_candle else None

    e2_candidates = (
        [c for c in candles if c.low <= plan.entry2 <= c.high]
        if plan.entry2 is not None
        else []
    )
    e2_filled = bool(e2_candidates) or (norm_status == "filled" and plan.is_dual_entry)
    e2_fill_candle = e2_candidates[0] if e2_candidates else None
    e2_fill_time = e2_fill_candle.open_time if e2_fill_candle else None

    # Tentukan waktu fill pertama
    first_fill_time: datetime | None = None
    if e1_fill_time and e2_fill_time:
        first_fill_time = min(e1_fill_time, e2_fill_time)
    elif e1_fill_time:
        first_fill_time = e1_fill_time
    elif e2_fill_time:
        first_fill_time = e2_fill_time

    first_fill_kz = (
        cfg.fmt_session_killzone(first_fill_time) if first_fill_time else None
    )

    # Tentukan candle aktif yang dievaluasi
    if norm_status == "unfilled":
        active_candles: Sequence[Candle] = candles
        active_entry = active_entry_price or plan.entry1
        active_risk = plan.risk_1r_e1
    elif norm_status == "partial":
        # E1 terisi, E2 belum
        if e1_fill_candle:
            idx = candles.index(e1_fill_candle)
            active_candles = candles[idx:]
        else:
            active_candles = candles
        active_entry = plan.entry1
        active_risk = plan.risk_1r_e1
    else:  # filled
        first_c = e1_fill_candle or e2_fill_candle
        if first_c:
            idx = candles.index(first_c)
            active_candles = candles[idx:]
        else:
            active_candles = candles
        active_entry = active_entry_price or plan.blended_entry
        active_risk = plan.risk_1r_blended

    highest_c = max(active_candles, key=lambda c: c.high)
    lowest_c = min(active_candles, key=lambda c: c.low)

    if is_long:
        mfe_price = highest_c.high
        mfe_points = mfe_price - active_entry
        mae_price = lowest_c.low
        mae_points = active_entry - mae_price
        sl_touched = (mae_price <= plan.sl) if plan.sl is not None else False
        tp1_touched = mfe_price >= plan.tp1
        tp2_touched = (mfe_price >= plan.tp2) if plan.tp2 is not None else False
    else:  # short
        mfe_price = lowest_c.low
        mfe_points = active_entry - mfe_price
        mae_price = highest_c.high
        mae_points = mae_price - active_entry
        sl_touched = (mae_price >= plan.sl) if plan.sl is not None else False
        tp1_touched = mfe_price <= plan.tp1
        tp2_touched = (mfe_price <= plan.tp2) if plan.tp2 is not None else False

    mfe_r = (
        (mfe_points / active_risk).quantize(Decimal("0.01"))
        if active_risk and active_risk > Decimal(0)
        else None
    )
    mae_r = (
        (mae_points / active_risk).quantize(Decimal("0.01"))
        if active_risk and active_risk > Decimal(0)
        else None
    )

    # Deteksi urutan sentuh pertama (first-touch chronology)
    touches: list[tuple[datetime, str]] = []
    seen_events: set[str] = set()

    for c in active_candles:
        if is_long:
            if plan.sl is not None and c.low <= plan.sl and "sl" not in seen_events:
                touches.append((c.open_time, "sl"))
                seen_events.add("sl")
            if c.high >= plan.tp1 and "tp1" not in seen_events:
                touches.append((c.open_time, "tp1"))
                seen_events.add("tp1")
            if plan.tp2 is not None and c.high >= plan.tp2 and "tp2" not in seen_events:
                touches.append((c.open_time, "tp2"))
                seen_events.add("tp2")
        else:  # short
            if plan.sl is not None and c.high >= plan.sl and "sl" not in seen_events:
                touches.append((c.open_time, "sl"))
                seen_events.add("sl")
            if c.low <= plan.tp1 and "tp1" not in seen_events:
                touches.append((c.open_time, "tp1"))
                seen_events.add("tp1")
            if plan.tp2 is not None and c.low <= plan.tp2 and "tp2" not in seen_events:
                touches.append((c.open_time, "tp2"))
                seen_events.add("tp2")

    touches.sort(key=lambda item: item[0])
    first_touch_seq = tuple(evt for _, evt in touches)

    return ExcursionResult(
        mfe_price=mfe_price,
        mfe_points=mfe_points,
        mfe_r=mfe_r,
        mae_price=mae_price,
        mae_points=mae_points,
        mae_r=mae_r,
        e1_filled=e1_filled,
        e2_filled=e2_filled,
        e1_fill_time=e1_fill_time,
        e2_fill_time=e2_fill_time,
        first_fill_time=first_fill_time,
        first_fill_killzone=first_fill_kz,
        first_touch_sequence=first_touch_seq,
        sl_touched=sl_touched,
        tp1_touched=tp1_touched,
        tp2_touched=tp2_touched,
    )
