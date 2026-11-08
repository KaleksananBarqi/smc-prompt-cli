"""Trade plan representation, dual-entry schemas, and excursion tracking.

Provides:
  1. TradePlan: Immutable data contract for SMC trade setups (Entry 1 Aggressive,
     Entry 2 Moderate, Blended Entry, Stop Loss, TP1 Nearest DOL, TP2 Main DOL,
     plus realized PnL / R-multiple calculations).
  2. build_trade_plan: Single validation & normalization builder for CLI and tests.
  3. compute_excursion: Single-pass mechanical evaluation of unfilled closest
     approach, post-approach travel ratio, MFE/MAE, first-touch chronology, and
     fill tracking.
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

    def resolve_active_entry(self, filled_legs: str | None = None) -> Decimal:
        """Tentukan harga entry acuan berdasarkan leg yang terisi ('e1', 'e2', 'both', atau None)."""
        if not self.is_dual_entry or self.entry2 is None:
            return self.entry1
        leg_norm = (filled_legs or "").strip().lower()
        if leg_norm == "e1":
            return self.entry1
        if leg_norm == "e2":
            return self.entry2
        return self.blended_entry

    def risk_1r(self, from_entry: Decimal | None = None) -> Decimal | None:
        """Jarak risiko 1R dari harga entry acuan (default Blended Entry) ke Stop Loss."""
        if self.sl is None:
            return None
        basis = from_entry if from_entry is not None else self.blended_entry
        return abs(basis - self.sl)

    @property
    def risk_1r_blended(self) -> Decimal | None:
        """Jarak risiko 1R acuan dari Blended Entry ke Stop Loss."""
        return self.risk_1r(self.blended_entry)

    @property
    def risk_1r_e1(self) -> Decimal | None:
        """Jarak risiko 1R mandiri dari Entry 1 ke Stop Loss."""
        return self.risk_1r(self.entry1)

    @property
    def risk_1r_e2(self) -> Decimal | None:
        """Jarak risiko 1R mandiri dari Entry 2 ke Stop Loss."""
        if self.entry2 is None:
            return None
        return self.risk_1r(self.entry2)

    def rr_tp1(self, from_entry: Decimal | None = None) -> Decimal | None:
        """Kalkulasi RRR ke TP1 (default menggunakan Blended Entry)."""
        basis = from_entry if from_entry is not None else self.blended_entry
        risk = self.risk_1r(basis)
        if risk is None or risk == Decimal(0):
            return None
        reward = abs(self.tp1 - basis)
        return (reward / risk).quantize(Decimal("0.01"))

    def rr_tp2(self, from_entry: Decimal | None = None) -> Decimal | None:
        """Kalkulasi RRR ke TP2 (default menggunakan Blended Entry)."""
        if self.tp2 is None:
            return None
        basis = from_entry if from_entry is not None else self.blended_entry
        risk = self.risk_1r(basis)
        if risk is None or risk == Decimal(0):
            return None
        reward = abs(self.tp2 - basis)
        return (reward / risk).quantize(Decimal("0.01"))

    def realized_pnl_points(
        self,
        exit_price: Decimal | None,
        *,
        from_entry: Decimal | None = None,
    ) -> Decimal | None:
        """Selisih poin harga riil yang terealisasi antara exit_price dan entry acuan."""
        if exit_price is None:
            return None
        basis = from_entry if from_entry is not None else self.blended_entry
        if self.direction == "short":
            return basis - exit_price
        return exit_price - basis

    def realized_r_multiple(
        self,
        exit_price: Decimal | None,
        *,
        from_entry: Decimal | None = None,
    ) -> Decimal | None:
        """Realisasi R-Multiple (Realized R) berdasarkan exit_price dan risiko 1R."""
        basis = from_entry if from_entry is not None else self.blended_entry
        pnl = self.realized_pnl_points(exit_price, from_entry=basis)
        risk = self.risk_1r(basis)
        if pnl is None or risk is None or risk == Decimal(0):
            return None
        return (pnl / risk).quantize(Decimal("0.01"))


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
            raise ConfigError(f"For long setups, --sl must be lower than --entry (diberikan SL={dec_sl}, Entry={e1}).")

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
            raise ConfigError(f"For short setups, --sl must be higher than --entry (diberikan SL={dec_sl}, Entry={e1}).")

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
    """Hasil kalkulasi mekanis pendekatan dan pergerakan harga pasca-entry (MFE / MAE / First-Touch)."""

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
    active_entry: Decimal | None = None
    filled_legs: str = "none"
    closest_approach_price: Decimal | None = None
    closest_approach_time: datetime | None = None
    closest_approach_distance: Decimal | None = None
    closest_approach_e2_price: Decimal | None = None
    closest_approach_e2_distance: Decimal | None = None
    post_approach_extreme_price: Decimal | None = None
    post_approach_time: datetime | None = None
    target_travel_ratio: Decimal = Decimal("0.0")


def _compute_travel_ratio(
    direction: str,
    ref_entry: Decimal,
    tp1: Decimal,
    extreme_price: Decimal,
) -> Decimal:
    """Hitung persentase rasio jelajah harga (0.0 - 100.0) dari ref_entry menuju tp1."""
    if direction == "long":
        total_span = tp1 - ref_entry
        if total_span <= Decimal(0) or extreme_price <= ref_entry:
            return Decimal("0.0")
        raw = ((extreme_price - ref_entry) / total_span) * Decimal("100")
    else:
        total_span = ref_entry - tp1
        if total_span <= Decimal(0) or extreme_price >= ref_entry:
            return Decimal("0.0")
        raw = ((ref_entry - extreme_price) / total_span) * Decimal("100")

    return min(Decimal("100.0"), raw).quantize(Decimal("0.1"))


def compute_excursion(
    plan: TradePlan,
    candles: Sequence[Candle],
    *,
    order_status: str = "unfilled",
    filled_legs: str | None = None,
    active_entry_price: Decimal | None = None,
) -> ExcursionResult:
    """Kalkulasi satu-lintasan untuk pendekatan unfilled, MFE, MAE, dan urutan sentuh harga."""

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
            active_entry=active_entry_price or plan.entry1,
            filled_legs="none",
        )

    norm_status = cfg.normalize_order_status(order_status)
    is_long = plan.direction == "long"

    e1_candidates = [c for c in candles if c.low <= plan.entry1 <= c.high]
    e1_candle_hit = bool(e1_candidates)
    e1_fill_candle = e1_candidates[0] if e1_candidates else None
    e1_fill_time = e1_fill_candle.open_time if e1_fill_candle else None

    e2_candidates = (
        [c for c in candles if c.low <= plan.entry2 <= c.high]
        if plan.entry2 is not None
        else []
    )
    e2_candle_hit = bool(e2_candidates)
    e2_fill_candle = e2_candidates[0] if e2_candidates else None
    e2_fill_time = e2_fill_candle.open_time if e2_fill_candle else None

    # 1. Mode UNFILLED: limit order masih pending di bursa
    if norm_status == "unfilled":
        active_entry = active_entry_price or plan.entry1
        active_risk = plan.risk_1r(active_entry)

        if is_long:
            # Order Long menunggu pullback dari atas ke entry1; abaikan candle masa lalu yang low <= entry1
            unfilled_candidates = [c for c in candles if c.low > plan.entry1]
            if unfilled_candidates:
                closest_candle = min(unfilled_candidates, key=lambda c: c.low)
            else:
                closest_candle = min(candles, key=lambda c: abs(c.low - plan.entry1))
            closest_price = closest_candle.low
            closest_time = closest_candle.open_time

            closest_idx = candles.index(closest_candle)
            post_candles = candles[closest_idx:]
            favorable_candle = max(post_candles, key=lambda c: c.high)
            post_extreme_price = favorable_candle.high
            post_extreme_time = favorable_candle.open_time

            tp1_reached = post_extreme_price >= plan.tp1
            tp2_reached = (post_extreme_price >= plan.tp2) if plan.tp2 is not None else False

            closest_e2_price: Decimal | None = None
            closest_e2_dist: Decimal | None = None
            if plan.entry2 is not None:
                unfilled_e2 = [c for c in candles if c.low > plan.entry2]
                cand_e2 = (
                    min(unfilled_e2, key=lambda c: c.low)
                    if unfilled_e2
                    else min(candles, key=lambda c: abs(c.low - plan.entry2))
                )
                closest_e2_price = cand_e2.low
                closest_e2_dist = abs(closest_e2_price - plan.entry2)
        else:
            # Order Short menunggu pullback dari bawah ke entry1; abaikan candle masa lalu yang high >= entry1
            unfilled_candidates = [c for c in candles if c.high < plan.entry1]
            if unfilled_candidates:
                closest_candle = max(unfilled_candidates, key=lambda c: c.high)
            else:
                closest_candle = min(candles, key=lambda c: abs(c.high - plan.entry1))
            closest_price = closest_candle.high
            closest_time = closest_candle.open_time

            closest_idx = candles.index(closest_candle)
            post_candles = candles[closest_idx:]
            favorable_candle = min(post_candles, key=lambda c: c.low)
            post_extreme_price = favorable_candle.low
            post_extreme_time = favorable_candle.open_time

            tp1_reached = post_extreme_price <= plan.tp1
            tp2_reached = (post_extreme_price <= plan.tp2) if plan.tp2 is not None else False

            closest_e2_price = None
            closest_e2_dist = None
            if plan.entry2 is not None:
                unfilled_e2 = [c for c in candles if c.high < plan.entry2]
                cand_e2 = (
                    max(unfilled_e2, key=lambda c: c.high)
                    if unfilled_e2
                    else min(candles, key=lambda c: abs(c.high - plan.entry2))
                )
                closest_e2_price = cand_e2.high
                closest_e2_dist = abs(closest_e2_price - plan.entry2)

        closest_dist = abs(closest_price - plan.entry1)
        travel_ratio = _compute_travel_ratio(
            plan.direction, plan.entry1, plan.tp1, post_extreme_price
        )

        # Hitung juga ekskursi teoretis pada seluruh jendela candle untuk referensi diagnostik
        highest_all = max(candles, key=lambda c: c.high)
        lowest_all = min(candles, key=lambda c: c.low)
        if is_long:
            mfe_price = highest_all.high
            mfe_points = mfe_price - active_entry
            mae_price = lowest_all.low
            mae_points = active_entry - mae_price
        else:
            mfe_price = lowest_all.low
            mfe_points = active_entry - mfe_price
            mae_price = highest_all.high
            mae_points = mae_price - active_entry

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

        return ExcursionResult(
            mfe_price=mfe_price,
            mfe_points=mfe_points,
            mfe_r=mfe_r,
            mae_price=mae_price,
            mae_points=mae_points,
            mae_r=mae_r,
            e1_filled=False,
            e2_filled=False,
            e1_fill_time=None,
            e2_fill_time=None,
            first_fill_time=None,
            first_fill_killzone=None,
            first_touch_sequence=(),
            sl_touched=False,
            tp1_touched=tp1_reached,
            tp2_touched=tp2_reached,
            active_entry=active_entry,
            filled_legs="none",
            closest_approach_price=closest_price,
            closest_approach_time=closest_time,
            closest_approach_distance=closest_dist,
            closest_approach_e2_price=closest_e2_price,
            closest_approach_e2_distance=closest_e2_dist,
            post_approach_extreme_price=post_extreme_price,
            post_approach_time=post_extreme_time,
            target_travel_ratio=travel_ratio,
        )

    # 2. Mode PARTIAL atau FILLED: posisi sudah terjemput dan aktif di pasar
    e1_filled = e1_candle_hit or norm_status in ("partial", "filled")
    if norm_status == "partial":
        e2_filled = False
        resolved_legs = "e1"
    else:
        if filled_legs is not None:
            resolved_legs = filled_legs.strip().lower()
            e2_filled = resolved_legs in ("e2", "both")
        elif not plan.is_dual_entry:
            resolved_legs = "e1"
            e2_filled = False
        elif e1_candle_hit and not e2_candle_hit:
            # Auto-detect dari fakta candle: hanya E1 yang tersentuh, E2 tidak tersentuh
            resolved_legs = "e1"
            e2_filled = False
        else:
            resolved_legs = "both"
            e2_filled = True

    active_entry = (
        active_entry_price
        if active_entry_price is not None
        else plan.resolve_active_entry(resolved_legs)
    )
    active_risk = plan.risk_1r(active_entry)

    # Tentukan waktu fill pertama dan potong deret candle aktif sejak candle fill pertama
    first_fill_time: datetime | None = None
    if e1_fill_time and e2_fill_time and e2_filled:
        first_fill_time = min(e1_fill_time, e2_fill_time)
    elif e1_fill_time:
        first_fill_time = e1_fill_time
    elif e2_fill_time and e2_filled:
        first_fill_time = e2_fill_time

    first_fill_kz = (
        cfg.fmt_session_killzone(first_fill_time) if first_fill_time else None
    )

    first_c = e1_fill_candle if norm_status == "partial" else (e1_fill_candle or e2_fill_candle)
    if first_c is not None:
        idx = candles.index(first_c)
        active_candles: Sequence[Candle] = candles[idx:]
    else:
        active_candles = candles

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

        closest_price = lowest_c.low
        closest_time = lowest_c.open_time
        post_extreme_price = highest_c.high
        post_extreme_time = highest_c.open_time
    else:
        mfe_price = lowest_c.low
        mfe_points = active_entry - mfe_price
        mae_price = highest_c.high
        mae_points = mae_price - active_entry
        sl_touched = (mae_price >= plan.sl) if plan.sl is not None else False
        tp1_touched = mfe_price <= plan.tp1
        tp2_touched = (mfe_price <= plan.tp2) if plan.tp2 is not None else False

        closest_price = highest_c.high
        closest_time = highest_c.open_time
        post_extreme_price = lowest_c.low
        post_extreme_time = lowest_c.open_time

    closest_dist = abs(closest_price - active_entry)
    travel_ratio = _compute_travel_ratio(
        plan.direction, active_entry, plan.tp1, post_extreme_price
    )

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

    # Deteksi urutan sentuh pertama (First-Touch Sequence) pada active_candles
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
        else:
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
        active_entry=active_entry,
        filled_legs=resolved_legs,
        closest_approach_price=closest_price,
        closest_approach_time=closest_time,
        closest_approach_distance=closest_dist,
        closest_approach_e2_price=None,
        closest_approach_e2_distance=None,
        post_approach_extreme_price=post_extreme_price,
        post_approach_time=post_extreme_time,
        target_travel_ratio=travel_ratio,
    )
