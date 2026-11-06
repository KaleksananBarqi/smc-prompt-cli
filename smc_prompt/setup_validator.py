"""Setup validator & front-run detection engine for SMC/ICT trades.

Evaluates whether a planned trade setup:
  1. Has been front-runned (price reversed near POI without filling,
     and already travelled substantially >= 60% towards TP1/DOL).
  2. Is invalidated (target TP1/DOL was swept before the entry was triggered).
  3. Is fresh (entry untouched, target untouched, safe to keep limit order).
  4. Is partially filled (Entry 1 Aggressive filled, Entry 2 Moderate pending).
  5. Is fully triggered / filled (position active, evaluates MFE/MAE and TP/SL).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from importlib import resources
from typing import Sequence

from jinja2 import Environment, StrictUndefined

from . import config as cfg
from .errors import ConfigError, SmcPromptError
from .models import Candle
from .structure_analyzer import compute_atr
from .template_renderer import render_csv_table
from .trade_plan import ExcursionResult, TradePlan, build_trade_plan, compute_excursion

VALIDATE_TEMPLATE_NAME = "validate_setup_template.j2"


@dataclass(frozen=True)
class SetupSpec:
    """Spesifikasi setup trading yang dievaluasi integritasnya."""

    symbol: str
    direction: str  # 'long' atau 'short'
    entry_price: Decimal  # Entry 1 (Aggressive)
    tp_price: Decimal  # TP1 (Nearest DOL)
    sl_price: Decimal | None = None
    interval: str = cfg.DEFAULT_VALIDATE_INTERVAL
    provider_label: str = "Binance"
    order_status: str = "unfilled"  # 'unfilled', 'filled', atau 'partial'
    entry2_price: Decimal | None = None  # Entry 2 (Moderate)
    tp2_price: Decimal | None = None  # TP2 (Main DOL)

    @property
    def plan(self) -> TradePlan:
        """Bangun representasi TradePlan terpadu."""
        return build_trade_plan(
            direction=self.direction,
            entry1=self.entry_price,
            entry2=self.entry2_price,
            sl=self.sl_price,
            tp1=self.tp_price,
            tp2=self.tp2_price,
        )

    @property
    def blended_entry(self) -> Decimal:
        """Harga rata-rata tertimbang Blended Entry."""
        return self.plan.blended_entry


@dataclass(frozen=True)
class FrontRunAnalysis:
    """Hasil kuantitatif evaluasi setup dan deteksi front-run."""

    spec: SetupSpec
    current_price: Decimal
    closest_approach_price: Decimal
    closest_approach_time: datetime
    closest_approach_distance: Decimal
    closest_approach_distance_atr: str
    entry_touched: bool
    target_reached: bool
    sl_breached: bool
    target_travel_ratio: Decimal
    current_dist_to_entry: Decimal
    current_dist_to_entry_atr: str
    current_dist_to_tp: Decimal
    current_dist_to_tp_atr: str
    status_hint: str
    entry_status: str
    tp_status: str
    sl_status: str
    atr: Decimal | None
    post_approach_extreme_price: Decimal | None = None
    post_approach_time: datetime | None = None
    excursion: ExcursionResult | None = None
    tp2_reached: bool = False
    closest_approach_e2_price: Decimal | None = None
    closest_approach_e2_distance: Decimal | None = None
    closest_approach_e2_distance_atr: str = "n/a"
    e2_status: str = ""

    @property
    def status(self) -> str:
        norm_status = cfg.normalize_order_status(self.spec.order_status)
        if norm_status == "unfilled":
            if self.target_reached:
                return "DOL_REACHED"
            if self.target_travel_ratio >= cfg.DEFAULT_FRONT_RUN_TRAVEL_THRESHOLD_PCT:
                return "FRONT_RUNNED"
            return "FRESH"
        elif norm_status == "partial":
            if self.sl_breached:
                return "STOPPED_OUT"
            if self.tp2_reached:
                return "TP2_REACHED"
            if self.target_reached:
                return "TP1_REACHED"
            return "PARTIAL_IN_PLAY"
        else:  # filled
            if self.sl_breached:
                return "STOPPED_OUT"
            if self.tp2_reached:
                return "TP2_REACHED"
            if self.target_reached:
                return "TP_REACHED"
            return "TRIGGERED"

    @property
    def is_unfilled(self) -> bool:
        return cfg.normalize_order_status(self.spec.order_status) == "unfilled"

    @property
    def is_partial(self) -> bool:
        return cfg.normalize_order_status(self.spec.order_status) == "partial"

    @property
    def is_filled(self) -> bool:
        return cfg.normalize_order_status(self.spec.order_status) == "filled"

    @property
    def missed_by_distance(self) -> Decimal:
        return Decimal("0") if self.entry_touched else self.closest_approach_distance

    @property
    def missed_by_pct(self) -> Decimal:
        if self.entry_touched or self.spec.entry_price == Decimal(0):
            return Decimal(0)
        return (
            (self.closest_approach_distance / self.spec.entry_price) * Decimal("100")
        ).quantize(Decimal("0.1"))

    @property
    def closest_price(self) -> Decimal:
        return self.closest_approach_price

    @property
    def tp_touched(self) -> bool:
        return self.target_reached

    @property
    def sl_touched(self) -> bool:
        return self.sl_breached

    @property
    def entry_time(self) -> datetime | None:
        return self.closest_approach_time if self.entry_touched else None


def analyze_setup(
    spec: SetupSpec,
    candles: Sequence[Candle],
    current_price: Decimal | None = None,
    *,
    atr: Decimal | None = None,
    price_format: cfg.PriceFormat | None = None,
) -> FrontRunAnalysis:
    """Analisis sekuens candle untuk mengevaluasi setup SMC (unfilled, partial, filled)."""

    if not candles:
        raise ConfigError("Cannot validate setup: no candle data provided.")

    if current_price is None:
        current_price = candles[-1].close

    fmt = (price_format or cfg.PriceFormat()).fmt
    order_status = cfg.normalize_order_status(spec.order_status)

    # Validasi dan bangun TradePlan terpadu
    plan = build_trade_plan(
        direction=spec.direction,
        entry1=spec.entry_price,
        entry2=spec.entry2_price,
        sl=spec.sl_price,
        tp1=spec.tp_price,
        tp2=spec.tp2_price,
    )

    direction = plan.direction
    entry1 = plan.entry1
    entry2 = plan.entry2
    tp1 = plan.tp1
    tp2 = plan.tp2
    sl = plan.sl

    if atr is None:
        closed_candles = [c for c in candles if c.is_closed]
        if len(closed_candles) >= cfg.DEFAULT_ATR_PERIOD + 1:
            atr = compute_atr(closed_candles, cfg.DEFAULT_ATR_PERIOD)

    # Jalankan kalkulasi ekskursi mekanis (MFE/MAE/First-Touch)
    excursion = compute_excursion(plan, candles, order_status=order_status)

    post_extreme_price: Decimal | None = None
    post_extreme_time: datetime | None = None
    tp2_reached: bool = False
    closest_e2_price: Decimal | None = None
    closest_e2_dist: Decimal | None = None
    closest_e2_dist_atr = "n/a"
    e2_status = ""

    if order_status == "unfilled":
        # Fakta mutlak trader: limit order masih pending di exchange, belum pernah terisi
        entry_touched = False
        sl_breached = False

        if direction == "long":
            # Order Long menunggu harga turun (pullback) dari atas ke entry1.
            # Abaikan candle historis masa lalu sebelum order dipasang (candle yang low <= entry1).
            unfilled_candidates = [c for c in candles if c.low > entry1]
            if unfilled_candidates:
                closest_candle = min(unfilled_candidates, key=lambda c: c.low)
            else:
                closest_candle = min(candles, key=lambda c: abs(c.low - entry1))
            closest_price = closest_candle.low
            closest_time = closest_candle.open_time

            # KRONOLOGI TEMPORAL:
            # Pantulan harga dan rasio perjalanan ke target dihitung sejak titik terdekat ke E1
            closest_idx = candles.index(closest_candle)
            post_approach_candles = candles[closest_idx:]

            highest_post_candle = max(post_approach_candles, key=lambda c: c.high)
            post_extreme_price = highest_post_candle.high
            post_extreme_time = highest_post_candle.open_time

            target_reached = post_extreme_price >= tp1
            if tp2 is not None:
                tp2_reached = post_extreme_price >= tp2

            # Basis rasio jelajah (Target Travel Ratio) dihitung dari E1 ke TP1
            total_target_span = tp1 - entry1
            if post_extreme_price <= entry1:
                travel_ratio = Decimal("0.0")
            else:
                travel_ratio = min(
                    Decimal("100.0"),
                    ((post_extreme_price - entry1) / total_target_span) * Decimal("100"),
                )

            # Jika dual-entry, periksa juga pendekatan ke Entry 2
            if entry2 is not None:
                unfilled_e2 = [c for c in candles if c.low > entry2]
                if unfilled_e2:
                    cand_e2 = min(unfilled_e2, key=lambda c: c.low)
                else:
                    cand_e2 = min(candles, key=lambda c: abs(c.low - entry2))
                closest_e2_price = cand_e2.low
                closest_e2_dist = abs(closest_e2_price - entry2)
                e2_status = f"BELUM TERJEMPUT (Pending Limit di exchange, selisih {fmt(closest_e2_dist)})"

        else:  # short unfilled
            # Order Short menunggu harga naik (pullback) dari bawah ke entry1.
            unfilled_candidates = [c for c in candles if c.high < entry1]
            if unfilled_candidates:
                closest_candle = max(unfilled_candidates, key=lambda c: c.high)
            else:
                closest_candle = min(candles, key=lambda c: abs(c.high - entry1))
            closest_price = closest_candle.high
            closest_time = closest_candle.open_time

            closest_idx = candles.index(closest_candle)
            post_approach_candles = candles[closest_idx:]

            lowest_post_candle = min(post_approach_candles, key=lambda c: c.low)
            post_extreme_price = lowest_post_candle.low
            post_extreme_time = lowest_post_candle.open_time

            target_reached = post_extreme_price <= tp1
            if tp2 is not None:
                tp2_reached = post_extreme_price <= tp2

            total_target_span = entry1 - tp1
            if post_extreme_price >= entry1:
                travel_ratio = Decimal("0.0")
            else:
                travel_ratio = min(
                    Decimal("100.0"),
                    ((entry1 - post_extreme_price) / total_target_span) * Decimal("100"),
                )

            if entry2 is not None:
                unfilled_e2 = [c for c in candles if c.high < entry2]
                if unfilled_e2:
                    cand_e2 = max(unfilled_e2, key=lambda c: c.high)
                else:
                    cand_e2 = min(candles, key=lambda c: abs(c.high - entry2))
                closest_e2_price = cand_e2.high
                closest_e2_dist = abs(closest_e2_price - entry2)
                e2_status = f"BELUM TERJEMPUT (Pending Limit di exchange, selisih {fmt(closest_e2_dist)})"

        # Status mekanis untuk unfilled order
        if target_reached:
            status_hint = "DOL_REACHED (Target TP1/DOL tersapu sebelum order terjemput - INVALID)"
        elif travel_ratio >= cfg.DEFAULT_FRONT_RUN_TRAVEL_THRESHOLD_PCT:
            status_hint = f"FRONT_RUNNED (Harga memantul di {fmt(closest_price)} tanpa menjemput Entry 1 dan telah melaju {travel_ratio:.1f}% ke TP1)"
        else:
            status_hint = f"FRESH (Limit order belum terjemput dan pantulan pasca-pendekatan baru {travel_ratio:.1f}% ke TP1 - FRESH & VALID)"

        closest_dist = abs(closest_price - entry1)
        entry_status = f"BELUM TERJEMPUT (Limit Order masih aktif di exchange, selisih {fmt(closest_dist)} dari level Entry 1)"
        tp_status = (
            "TERSAPU DULUAN (Target TP1 / DOL sudah tersentuh sebelum order terjemput)"
            if target_reached
            else f"BELUM TERSAPU (Perjalanan baru menempuh {travel_ratio:.1f}% ke arah TP1)"
        )
        sl_status = "BELUM AKTIF (Posisi belum terisi di exchange)"

    elif order_status == "partial":
        # Fakta mutlak trader: Entry 1 sudah terisi, Entry 2 masih pending
        entry_touched = True

        # Evaluasi pergerakan sejak first fill candle Entry 1
        fill_candidates = [c for c in candles if c.low <= entry1 <= c.high]
        if fill_candidates:
            fill_candle = fill_candidates[0]
            fill_idx = candles.index(fill_candle)
            active_candles = candles[fill_idx:]
        else:
            active_candles = candles

        if direction == "long":
            lowest_active_candle = min(active_candles, key=lambda c: c.low)
            closest_price = lowest_active_candle.low
            closest_time = lowest_active_candle.open_time
            sl_breached = (closest_price <= sl) if sl is not None else False

            highest_active_candle = max(active_candles, key=lambda c: c.high)
            post_extreme_price = highest_active_candle.high
            post_extreme_time = highest_active_candle.open_time
            target_reached = post_extreme_price >= tp1
            if tp2 is not None:
                tp2_reached = post_extreme_price >= tp2

            total_target_span = tp1 - entry1
            if post_extreme_price <= entry1:
                travel_ratio = Decimal("0.0")
            else:
                travel_ratio = min(
                    Decimal("100.0"),
                    ((post_extreme_price - entry1) / total_target_span) * Decimal("100"),
                )
        else:  # short partial
            highest_active_candle = max(active_candles, key=lambda c: c.high)
            closest_price = highest_active_candle.high
            closest_time = highest_active_candle.open_time
            sl_breached = (closest_price >= sl) if sl is not None else False

            lowest_active_candle = min(active_candles, key=lambda c: c.low)
            post_extreme_price = lowest_active_candle.low
            post_extreme_time = lowest_active_candle.open_time
            target_reached = post_extreme_price <= tp1
            if tp2 is not None:
                tp2_reached = post_extreme_price <= tp2

            total_target_span = entry1 - tp1
            if post_extreme_price >= entry1:
                travel_ratio = Decimal("0.0")
            else:
                travel_ratio = min(
                    Decimal("100.0"),
                    ((entry1 - post_extreme_price) / total_target_span) * Decimal("100"),
                )

        closest_dist = abs(closest_price - entry1)

        if sl_breached:
            status_hint = f"STOPPED_OUT (Posisi E1 aktif sempat terkena level SL di {fmt(sl)})"
        elif tp2_reached:
            status_hint = f"TP2_REACHED (Posisi E1 telah mencapai target ekspansi TP2 di {fmt(tp2)})"
        elif target_reached:
            status_hint = f"TP1_REACHED (Posisi E1 telah mencapai target awal TP1 di {fmt(tp1)} - amankan BE / batalkan pending E2)"
        else:
            status_hint = "PARTIAL_IN_PLAY (Posisi Entry 1 aktif berjalan; Entry 2 masih pending di exchange)"

        entry_status = "ENTRY 1 TERJEMPUT (Aktif 40% Size); ENTRY 2 MASIH PENDING (60% Size)"
        tp_status = (
            "TERCAPAI (Target TP1 / DOL sudah tersentuh)"
            if target_reached
            else f"BELUM TERCAPAI (Perjalanan telah menempuh {travel_ratio:.1f}% ke arah TP1)"
        )
        if sl is None:
            sl_status = "Tidak Ditetapkan (None)"
        elif sl_breached:
            sl_status = f"TERTEMBUS (Harga sempat melewati SL di {fmt(sl)})"
        else:
            sl_status = f"AMAN (Harga belum menyentuh SL di {fmt(sl)})"

        if entry2 is not None:
            e2_status = f"PENDING LIMIT (Order masih menunggu di exchange pada level {fmt(entry2)})"

    else:  # filled
        # Fakta mutlak trader: posisi sudah terisi dan sedang aktif berjalan
        entry_touched = True
        ref_entry = plan.blended_entry

        fill_candidates = [c for c in candles if c.low <= entry1 <= c.high]
        if fill_candidates:
            fill_candle = fill_candidates[0]
            fill_idx = candles.index(fill_candle)
            active_candles = candles[fill_idx:]
        else:
            active_candles = candles

        if direction == "long":
            lowest_active_candle = min(active_candles, key=lambda c: c.low)
            closest_price = lowest_active_candle.low
            closest_time = lowest_active_candle.open_time
            sl_breached = (closest_price <= sl) if sl is not None else False

            highest_active_candle = max(active_candles, key=lambda c: c.high)
            post_extreme_price = highest_active_candle.high
            post_extreme_time = highest_active_candle.open_time
            target_reached = post_extreme_price >= tp1
            if tp2 is not None:
                tp2_reached = post_extreme_price >= tp2

            total_target_span = tp1 - ref_entry
            if post_extreme_price <= ref_entry:
                travel_ratio = Decimal("0.0")
            else:
                travel_ratio = min(
                    Decimal("100.0"),
                    ((post_extreme_price - ref_entry) / total_target_span) * Decimal("100"),
                )
        else:  # short filled
            highest_active_candle = max(active_candles, key=lambda c: c.high)
            closest_price = highest_active_candle.high
            closest_time = highest_active_candle.open_time
            sl_breached = (closest_price >= sl) if sl is not None else False

            lowest_active_candle = min(active_candles, key=lambda c: c.low)
            post_extreme_price = lowest_active_candle.low
            post_extreme_time = lowest_active_candle.open_time
            target_reached = post_extreme_price <= tp1
            if tp2 is not None:
                tp2_reached = post_extreme_price <= tp2

            total_target_span = ref_entry - tp1
            if post_extreme_price >= ref_entry:
                travel_ratio = Decimal("0.0")
            else:
                travel_ratio = min(
                    Decimal("100.0"),
                    ((ref_entry - post_extreme_price) / total_target_span) * Decimal("100"),
                )

        closest_dist = abs(closest_price - ref_entry)

        if sl_breached:
            status_hint = f"STOPPED_OUT (Posisi aktif sempat terkena level SL di {fmt(sl)})"
        elif tp2_reached:
            status_hint = f"TP2_REACHED (Posisi aktif telah mencapai target TP2 di {fmt(tp2)})"
        elif target_reached:
            status_hint = f"TP_REACHED (Posisi aktif telah mencapai target TP1 di {fmt(tp1)})"
        else:
            status_hint = "IN_PLAY (Posisi aktif sedang berjalan menuju target)"

        entry_status = "TERJEMPUT (Posisi trading aktif berjalan di market)"
        tp_status = (
            "TERCAPAI (Target TP1 / DOL sudah tersentuh)"
            if target_reached
            else f"BELUM TERCAPAI (Perjalanan telah menempuh {travel_ratio:.1f}% ke arah TP1)"
        )
        if sl is None:
            sl_status = "Tidak Ditetapkan (None)"
        elif sl_breached:
            sl_status = f"TERTEMBUS (Harga sempat melewati SL di {fmt(sl)})"
        else:
            sl_status = f"AMAN (Harga belum menyentuh SL di {fmt(sl)})"

    if atr and atr > Decimal(0):
        closest_dist_atr = format((closest_dist / atr).quantize(Decimal("0.01")), "f")
    else:
        closest_dist_atr = "n/a"

    if closest_e2_dist is not None and atr and atr > Decimal(0):
        closest_e2_dist_atr = format((closest_e2_dist / atr).quantize(Decimal("0.01")), "f")

    curr_dist_entry = abs(current_price - entry1)
    curr_dist_entry_atr = (
        format((curr_dist_entry / atr).quantize(Decimal("0.01")), "f")
        if atr and atr > Decimal(0)
        else "n/a"
    )

    curr_dist_tp = abs(current_price - tp1)
    curr_dist_tp_atr = (
        format((curr_dist_tp / atr).quantize(Decimal("0.01")), "f")
        if atr and atr > Decimal(0)
        else "n/a"
    )

    return FrontRunAnalysis(
        spec=spec,
        current_price=current_price,
        closest_approach_price=closest_price,
        closest_approach_time=closest_time,
        closest_approach_distance=closest_dist,
        closest_approach_distance_atr=closest_dist_atr,
        entry_touched=entry_touched,
        target_reached=target_reached,
        sl_breached=sl_breached,
        target_travel_ratio=travel_ratio.quantize(Decimal("0.1")),
        current_dist_to_entry=curr_dist_entry,
        current_dist_to_entry_atr=curr_dist_entry_atr,
        current_dist_to_tp=curr_dist_tp,
        current_dist_to_tp_atr=curr_dist_tp_atr,
        status_hint=status_hint,
        entry_status=entry_status,
        tp_status=tp_status,
        sl_status=sl_status,
        atr=atr,
        post_approach_extreme_price=post_extreme_price,
        post_approach_time=post_extreme_time,
        excursion=excursion,
        tp2_reached=tp2_reached,
        closest_approach_e2_price=closest_e2_price,
        closest_approach_e2_distance=closest_e2_dist,
        closest_approach_e2_distance_atr=closest_e2_dist_atr,
        e2_status=e2_status,
    )


def load_validate_template_text() -> str:
    """Read the validation Jinja template."""

    try:
        asset = resources.files("smc_prompt").joinpath(
            "templates", VALIDATE_TEMPLATE_NAME
        )
        return asset.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:  # pragma: no cover
        raise SmcPromptError(f"Validation prompt template asset not found ({exc}).")


def render_validation_prompt(
    analysis: FrontRunAnalysis,
    candles: Sequence[Candle],
    *,
    setup: SetupSpec | None = None,
    interval: str | None = None,
    provider_label: str | None = None,
    generated_at: datetime | None = None,
    price_format: cfg.PriceFormat | None = None,
    structure_block: str | None = None,
    derivatives_context_line: str = "",
    btc_benchmark_line: str = "",
) -> str:
    """Render Jinja2 template validasi setup SMC v2."""

    spec = setup or analysis.spec
    interval_str = interval or spec.interval
    provider = provider_label or spec.provider_label
    moment = generated_at or datetime.now(timezone.utc)
    fmt = (price_format or cfg.PriceFormat()).fmt
    norm_status = cfg.normalize_order_status(spec.order_status)

    is_daily = not cfg.is_intraday_interval(interval_str)
    csv_table = render_csv_table(candles, htf=is_daily, price_format=price_format)

    def _fmt_ts(dt: datetime) -> str:
        return cfg.fmt_htf_date(dt) if is_daily else cfg.fmt_ltf_datetime(dt)

    template_str = load_validate_template_text()
    env = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        autoescape=False,
        trim_blocks=True,
        newline_sequence="\n",
    )
    template = env.from_string(template_str)

    if norm_status == "unfilled":
        order_status_label = "BELUM TERJEMPUT (LIMIT ORDER MASIH PENDING DI EXCHANGE)"
    elif norm_status == "partial":
        order_status_label = "SEBAGIAN TERJEMPUT (ENTRY 1 AKTIF, ENTRY 2 PENDING DI EXCHANGE)"
    else:
        order_status_label = "SUDAH TERJEMPUT (POSISI TRADING AKTIF BERJALAN)"

    plan = spec.plan
    risk_1r_str = fmt(plan.risk_1r_blended) if plan.risk_1r_blended is not None else None
    rr_tp1_val = plan.rr_tp1()
    rr_tp1_str = str(rr_tp1_val) if rr_tp1_val is not None else None
    rr_tp2_val = plan.rr_tp2()
    rr_tp2_str = str(rr_tp2_val) if rr_tp2_val is not None else None

    # Ringkasan ekskursi jika ada
    excursion_summary: str | None = None
    if analysis.excursion and analysis.excursion.mfe_price is not None:
        exc = analysis.excursion
        mfe_r_str = f" (+{exc.mfe_r}R)" if exc.mfe_r is not None else ""
        mae_r_str = f" (-{exc.mae_r}R)" if exc.mae_r is not None else ""
        excursion_summary = (
            f"MFE: {fmt(exc.mfe_price)}{mfe_r_str} | "
            f"MAE: {fmt(exc.mae_price)}{mae_r_str}"
        )
        if exc.first_touch_sequence:
            excursion_summary += f" | First Touch: {' ➔ '.join(t.upper() for t in exc.first_touch_sequence)}"

    csv_legend = (
        "tanggal(UTC),open,high,low,close,volume"
        if is_daily
        else "datetime(UTC),open,high,low,close,volume"
    )

    tp2_status = (
        "TERCAPAI (Target TP2 / Main DOL sudah tersentuh)"
        if analysis.tp2_reached
        else "BELUM TERCAPAI"
    )

    context = {
        "PAIR": spec.symbol.upper(),
        "DIRECTION": spec.direction.upper(),
        "SESSION_KILLZONE_UTC": cfg.fmt_session_killzone(moment),
        "ORDER_STATUS_LABEL": order_status_label,
        "IS_UNFILLED": norm_status == "unfilled",
        "IS_PARTIAL": norm_status == "partial",
        "IS_FILLED": norm_status == "filled",
        "IS_DUAL_ENTRY": plan.is_dual_entry,
        "HAS_TP2": plan.has_tp2,
        "ENTRY_PRICE": fmt(spec.entry_price),
        "ENTRY2_PRICE": fmt(spec.entry2_price) if spec.entry2_price is not None else "",
        "BLENDED_ENTRY_PRICE": fmt(plan.blended_entry),
        "TP_PRICE": fmt(spec.tp_price),
        "TP2_PRICE": fmt(spec.tp2_price) if spec.tp2_price is not None else "",
        "SL_PRICE": fmt(spec.sl_price) if spec.sl_price is not None else "Tidak Ditetapkan",
        "RISK_1R": risk_1r_str,
        "RR_TP1": rr_tp1_str,
        "RR_TP2": rr_tp2_str,
        "CURRENT_PRICE": fmt(analysis.current_price),
        "GENERATED_AT_UTC": _fmt_ts(moment),
        "provider": provider,
        "INTERVAL_LABEL": cfg.interval_label(interval_str),
        "CANDLE_COUNT": str(len(candles)),
        "CLOSEST_PRICE": fmt(analysis.closest_approach_price),
        "CLOSEST_TIME": _fmt_ts(analysis.closest_approach_time),
        "CLOSEST_DIST": fmt(analysis.closest_approach_distance),
        "CLOSEST_DIST_ATR": analysis.closest_approach_distance_atr,
        "CLOSEST_DIST_E2": fmt(analysis.closest_approach_e2_distance) if analysis.closest_approach_e2_distance is not None else "",
        "CLOSEST_DIST_E2_ATR": analysis.closest_approach_e2_distance_atr,
        "E2_STATUS": analysis.e2_status,
        "POST_APPROACH_EXTREME": (
            fmt(analysis.post_approach_extreme_price)
            if analysis.post_approach_extreme_price is not None
            else "n/a"
        ),
        "POST_APPROACH_TIME": (
            _fmt_ts(analysis.post_approach_time)
            if analysis.post_approach_time is not None
            else "n/a"
        ),
        "ENTRY_STATUS": analysis.entry_status,
        "TP_STATUS": analysis.tp_status,
        "TP2_STATUS": tp2_status,
        "TRAVEL_RATIO": f"{analysis.target_travel_ratio:.1f}",
        "SL_STATUS": analysis.sl_status,
        "CURRENT_DIST_TO_ENTRY": fmt(analysis.current_dist_to_entry),
        "CURRENT_DIST_TO_ENTRY_ATR": analysis.current_dist_to_entry_atr,
        "CURRENT_DIST_TO_TP": fmt(analysis.current_dist_to_tp),
        "CURRENT_DIST_TO_TP_ATR": analysis.current_dist_to_tp_atr,
        "EXCURSION_SUMMARY": excursion_summary,
        "STATUS_HINT": analysis.status_hint,
        "STRUCTURE_BLOCK": structure_block or "",
        "DERIVATIVES_CONTEXT_LINE": derivatives_context_line,
        "BTC_BENCHMARK_LINE": btc_benchmark_line,
        "CSV_LEGEND": csv_legend,
        "CANDLE_TABLE_CSV": csv_table,
    }

    rendered = template.render(**context).strip()
    return rendered + "\n"
