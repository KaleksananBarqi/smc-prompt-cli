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


def _fmt_atr_ratio(dist: Decimal | None, atr: Decimal | None) -> str:
    """Format jarak harga dalam kelipatan ATR (2 desimal), atau 'n/a' bila tidak tersedia."""
    if dist is None or atr is None or atr <= Decimal(0):
        return "n/a"
    return format((dist / atr).quantize(Decimal("0.01")), "f")


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
    plan = spec.plan

    if atr is None:
        closed_candles = [c for c in candles if c.is_closed]
        if len(closed_candles) >= cfg.DEFAULT_ATR_PERIOD + 1:
            atr = compute_atr(closed_candles, cfg.DEFAULT_ATR_PERIOD)

    # Satu lintasan evaluasi ekskursi & kronologi pendekatan dari modul trade_plan
    excursion = compute_excursion(plan, candles, order_status=order_status)

    closest_price = excursion.closest_approach_price or candles[-1].close
    closest_time = excursion.closest_approach_time or candles[-1].open_time
    closest_dist = excursion.closest_approach_distance or Decimal("0")
    post_extreme_price = excursion.post_approach_extreme_price
    post_extreme_time = excursion.post_approach_time
    travel_ratio = excursion.target_travel_ratio
    target_reached = excursion.tp1_touched
    tp2_reached = excursion.tp2_touched
    sl_breached = excursion.sl_touched
    closest_e2_price = excursion.closest_approach_e2_price
    closest_e2_dist = excursion.closest_approach_e2_distance
    e2_status = ""

    if order_status == "unfilled":
        entry_touched = False
        if target_reached:
            status_hint = "DOL_REACHED (Target TP1/DOL tersapu sebelum order terjemput - INVALID)"
        elif travel_ratio >= cfg.DEFAULT_FRONT_RUN_TRAVEL_THRESHOLD_PCT:
            status_hint = (
                f"FRONT_RUNNED (Harga memantul di {fmt(closest_price)} tanpa menjemput Entry 1 "
                f"dan telah melaju {travel_ratio:.1f}% ke TP1)"
            )
        else:
            status_hint = (
                f"FRESH (Limit order belum terjemput dan pantulan pasca-pendekatan baru "
                f"{travel_ratio:.1f}% ke TP1 - FRESH & VALID)"
            )

        entry_status = (
            f"BELUM TERJEMPUT (Limit Order masih aktif di exchange, "
            f"selisih {fmt(closest_dist)} dari level Entry 1)"
        )
        tp_status = (
            "TERSAPU DULUAN (Target TP1 / DOL sudah tersentuh sebelum order terjemput)"
            if target_reached
            else f"BELUM TERSAPU (Perjalanan baru menempuh {travel_ratio:.1f}% ke arah TP1)"
        )
        sl_status = "BELUM AKTIF (Posisi belum terisi di exchange)"
        if plan.entry2 is not None and closest_e2_dist is not None:
            e2_status = f"BELUM TERJEMPUT (Pending Limit di exchange, selisih {fmt(closest_e2_dist)})"

    elif order_status == "partial":
        entry_touched = True
        if sl_breached and plan.sl is not None:
            status_hint = f"STOPPED_OUT (Posisi E1 aktif sempat terkena level SL di {fmt(plan.sl)})"
        elif tp2_reached and plan.tp2 is not None:
            status_hint = f"TP2_REACHED (Posisi E1 telah mencapai target ekspansi TP2 di {fmt(plan.tp2)})"
        elif target_reached:
            status_hint = (
                f"TP1_REACHED (Posisi E1 telah mencapai target awal TP1 di {fmt(plan.tp1)} "
                f"- amankan BE / batalkan pending E2)"
            )
        else:
            status_hint = "PARTIAL_IN_PLAY (Posisi Entry 1 aktif berjalan; Entry 2 masih pending di exchange)"

        entry_status = "ENTRY 1 TERJEMPUT (Aktif 40% Size); ENTRY 2 MASIH PENDING (60% Size)"
        tp_status = (
            "TERCAPAI (Target TP1 / DOL sudah tersentuh)"
            if target_reached
            else f"BELUM TERCAPAI (Perjalanan telah menempuh {travel_ratio:.1f}% ke arah TP1)"
        )
        if plan.sl is None:
            sl_status = "Tidak Ditetapkan (None)"
        elif sl_breached:
            sl_status = f"TERTEMBUS (Harga sempat melewati SL di {fmt(plan.sl)})"
        else:
            sl_status = f"AMAN (Harga belum menyentuh SL di {fmt(plan.sl)})"

        if plan.entry2 is not None:
            e2_status = f"PENDING LIMIT (Order masih menunggu di exchange pada level {fmt(plan.entry2)})"

    else:  # filled
        entry_touched = True
        if sl_breached and plan.sl is not None:
            status_hint = f"STOPPED_OUT (Posisi aktif sempat terkena level SL di {fmt(plan.sl)})"
        elif tp2_reached and plan.tp2 is not None:
            status_hint = f"TP2_REACHED (Posisi aktif telah mencapai target TP2 di {fmt(plan.tp2)})"
        elif target_reached:
            status_hint = f"TP_REACHED (Posisi aktif telah mencapai target TP1 di {fmt(plan.tp1)})"
        else:
            status_hint = "IN_PLAY (Posisi aktif sedang berjalan menuju target)"

        entry_status = "TERJEMPUT (Posisi trading aktif berjalan di market)"
        tp_status = (
            "TERCAPAI (Target TP1 / DOL sudah tersentuh)"
            if target_reached
            else f"BELUM TERCAPAI (Perjalanan telah menempuh {travel_ratio:.1f}% ke arah TP1)"
        )
        if plan.sl is None:
            sl_status = "Tidak Ditetapkan (None)"
        elif sl_breached:
            sl_status = f"TERTEMBUS (Harga sempat melewati SL di {fmt(plan.sl)})"
        else:
            sl_status = f"AMAN (Harga belum menyentuh SL di {fmt(plan.sl)})"

    ref_entry = excursion.active_entry or plan.entry1
    curr_dist_entry = abs(current_price - ref_entry)
    curr_dist_tp = abs(current_price - plan.tp1)

    return FrontRunAnalysis(
        spec=spec,
        current_price=current_price,
        closest_approach_price=closest_price,
        closest_approach_time=closest_time,
        closest_approach_distance=closest_dist,
        closest_approach_distance_atr=_fmt_atr_ratio(closest_dist, atr),
        entry_touched=entry_touched,
        target_reached=target_reached,
        sl_breached=sl_breached,
        target_travel_ratio=travel_ratio,
        current_dist_to_entry=curr_dist_entry,
        current_dist_to_entry_atr=_fmt_atr_ratio(curr_dist_entry, atr),
        current_dist_to_tp=curr_dist_tp,
        current_dist_to_tp_atr=_fmt_atr_ratio(curr_dist_tp, atr),
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
        closest_approach_e2_distance_atr=_fmt_atr_ratio(closest_e2_dist, atr),
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
