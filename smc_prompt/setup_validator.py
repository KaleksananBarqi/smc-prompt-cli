"""Setup validator & front-run detection engine for SMC/ICT trades.

Evaluates whether a planned trade setup:
  1. Has been front-runned (price reversed near the POI without filling,
     and already travelled substantially towards TP/DOL).
  2. Is invalidated (target TP/DOL was swept before the entry was triggered).
  3. Is fresh (entry untouched, target untouched, safe to keep limit order).
  4. Is triggered (entry price already touched).
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

VALIDATE_TEMPLATE_NAME = "validate_setup_template.j2"


@dataclass(frozen=True)
class SetupSpec:
    """Specification of the planned trade to validate."""

    symbol: str
    direction: str  # 'long' or 'short'
    entry_price: Decimal
    tp_price: Decimal
    sl_price: Decimal | None = None
    interval: str = cfg.DEFAULT_VALIDATE_INTERVAL
    provider_label: str = "Binance"
    order_status: str = "unfilled"  # 'unfilled' or 'filled'


@dataclass(frozen=True)
class FrontRunAnalysis:
    """Quantitative outcome of setup evaluation."""

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

    @property
    def status(self) -> str:
        if self.spec.order_status == "unfilled":
            if self.target_reached:
                return "DOL_REACHED"
            if self.target_travel_ratio >= Decimal("60.0"):
                return "FRONT_RUNNED"
            return "FRESH"
        else:
            if self.sl_breached:
                return "STOPPED_OUT"
            if self.target_reached:
                return "TP_REACHED"
            return "TRIGGERED"

    @property
    def is_unfilled(self) -> bool:
        return self.spec.order_status == "unfilled"

    @property
    def is_filled(self) -> bool:
        return self.spec.order_status == "filled"

    @property
    def missed_by_distance(self) -> Decimal:
        return Decimal("0") if self.entry_touched else self.closest_approach_distance

    @property
    def missed_by_pct(self) -> Decimal:
        if self.entry_touched or self.spec.entry_price == Decimal(0):
            return Decimal(0)
        return ((self.closest_approach_distance / self.spec.entry_price) * Decimal("100")).quantize(Decimal("0.1"))

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



@dataclass(frozen=True)
class _AnalysisIntermediate:
    entry_touched: bool
    sl_breached: bool
    target_reached: bool
    travel_ratio: Decimal
    closest_price: Decimal
    closest_time: datetime
    closest_dist: Decimal
    post_extreme_price: Decimal | None
    post_extreme_time: datetime | None
    status_hint: str
    entry_status: str
    tp_status: str
    sl_status: str


def _analyze_unfilled(
    candles: Sequence[Candle],
    direction: str,
    entry: Decimal,
    tp: Decimal,
    fmt: callable,
) -> _AnalysisIntermediate:
    entry_touched = False
    sl_breached = False

    if direction == "long":
        unfilled_candidates = [c for c in candles if c.low > entry]
        if unfilled_candidates:
            closest_candle = min(unfilled_candidates, key=lambda c: c.low)
        else:
            closest_candle = min(candles, key=lambda c: abs(c.low - entry))
        closest_price = closest_candle.low
        closest_time = closest_candle.open_time

        closest_idx = candles.index(closest_candle)
        post_approach_candles = candles[closest_idx:]

        highest_post_candle = max(post_approach_candles, key=lambda c: c.high)
        post_extreme_price = highest_post_candle.high
        post_extreme_time = highest_post_candle.open_time

        target_reached = post_extreme_price >= tp

        total_target_span = tp - entry
        if post_extreme_price <= entry:
            travel_ratio = Decimal("0.0")
        else:
            travel_ratio = min(
                Decimal("100.0"),
                ((post_extreme_price - entry) / total_target_span) * Decimal("100"),
            )
    else:
        unfilled_candidates = [c for c in candles if c.high < entry]
        if unfilled_candidates:
            closest_candle = max(unfilled_candidates, key=lambda c: c.high)
        else:
            closest_candle = min(candles, key=lambda c: abs(c.high - entry))
        closest_price = closest_candle.high
        closest_time = closest_candle.open_time

        closest_idx = candles.index(closest_candle)
        post_approach_candles = candles[closest_idx:]

        lowest_post_candle = min(post_approach_candles, key=lambda c: c.low)
        post_extreme_price = lowest_post_candle.low
        post_extreme_time = lowest_post_candle.open_time

        target_reached = post_extreme_price <= tp

        total_target_span = entry - tp
        if post_extreme_price >= entry:
            travel_ratio = Decimal("0.0")
        else:
            travel_ratio = min(
                Decimal("100.0"),
                ((entry - post_extreme_price) / total_target_span) * Decimal("100"),
            )

    if target_reached:
        status_hint = "DOL_REACHED (Target TP/DOL tersapu setelah memantul sebelum limit order terjemput - INVALID)"
    elif travel_ratio >= Decimal("60.0"):
        status_hint = f"FRONT_RUNNED (Harga memantul di {fmt(closest_price)} tanpa menjemput Entry dan telah melaju {travel_ratio:.1f}% ke TP)"
    else:
        status_hint = f"FRESH (Limit order belum terjemput dan pantulan pasca-pendekatan baru {travel_ratio:.1f}% ke TP - FRESH & VALID)"

    closest_dist = abs(closest_price - entry)
    entry_status = f"BELUM TERJEMPUT (Limit Order masih aktif di exchange, selisih {fmt(closest_dist)} dari level entry)"
    tp_status = (
        "TERSAPU DULUAN (Target TP / DOL sudah tersentuh sebelum order terjemput)"
        if target_reached
        else f"BELUM TERSAPU (Perjalanan baru menempuh {travel_ratio:.1f}% ke arah TP)"
    )
    sl_status = "BELUM AKTIF (Posisi belum terisi di exchange)"

    return _AnalysisIntermediate(
        entry_touched=entry_touched,
        sl_breached=sl_breached,
        target_reached=target_reached,
        travel_ratio=travel_ratio,
        closest_price=closest_price,
        closest_time=closest_time,
        closest_dist=closest_dist,
        post_extreme_price=post_extreme_price,
        post_extreme_time=post_extreme_time,
        status_hint=status_hint,
        entry_status=entry_status,
        tp_status=tp_status,
        sl_status=sl_status,
    )


def _analyze_filled(
    candles: Sequence[Candle],
    direction: str,
    entry: Decimal,
    tp: Decimal,
    sl: Decimal | None,
    fmt: callable,
) -> _AnalysisIntermediate:
    entry_touched = True

    if direction == "long":
        fill_candidates = [c for c in candles if c.low <= entry <= c.high]
        if fill_candidates:
            fill_candle = fill_candidates[0]
            fill_idx = candles.index(fill_candle)
            active_candles = candles[fill_idx:]
        else:
            active_candles = candles

        lowest_active_candle = min(active_candles, key=lambda c: c.low)
        closest_price = lowest_active_candle.low
        closest_time = lowest_active_candle.open_time
        sl_breached = (closest_price <= sl) if sl is not None else False

        highest_active_candle = max(active_candles, key=lambda c: c.high)
        post_extreme_price = highest_active_candle.high
        post_extreme_time = highest_active_candle.open_time
        target_reached = post_extreme_price >= tp

        total_target_span = tp - entry
        if post_extreme_price <= entry:
            travel_ratio = Decimal("0.0")
        else:
            travel_ratio = min(
                Decimal("100.0"),
                ((post_extreme_price - entry) / total_target_span) * Decimal("100"),
            )
    else:
        fill_candidates = [c for c in candles if c.low <= entry <= c.high]
        if fill_candidates:
            fill_candle = fill_candidates[0]
            fill_idx = candles.index(fill_candle)
            active_candles = candles[fill_idx:]
        else:
            active_candles = candles

        highest_active_candle = max(active_candles, key=lambda c: c.high)
        closest_price = highest_active_candle.high
        closest_time = highest_active_candle.open_time
        sl_breached = (closest_price >= sl) if sl is not None else False

        lowest_active_candle = min(active_candles, key=lambda c: c.low)
        post_extreme_price = lowest_active_candle.low
        post_extreme_time = lowest_active_candle.open_time
        target_reached = post_extreme_price <= tp

        total_target_span = entry - tp
        if post_extreme_price >= entry:
            travel_ratio = Decimal("0.0")
        else:
            travel_ratio = min(
                Decimal("100.0"),
                ((entry - post_extreme_price) / total_target_span) * Decimal("100"),
            )

    closest_dist = abs(closest_price - entry)

    if sl_breached:
        status_hint = "STOPPED_OUT (Posisi aktif sempat terkena level SL)"
    elif target_reached:
        status_hint = "TP_REACHED (Posisi aktif telah mencapai target Take Profit)"
    else:
        status_hint = "IN_PLAY (Posisi aktif sedang berjalan menuju target)"

    entry_status = "TERJEMPUT (Posisi trading aktif berjalan di market)"
    tp_status = (
        "TERCAPAI (Target TP / DOL sudah tersentuh)"
        if target_reached
        else f"BELUM TERCAPAI (Perjalanan telah menempuh {travel_ratio:.1f}% ke arah TP)"
    )
    if sl is None:
        sl_status = "Tidak Ditetapkan (None)"
    elif sl_breached:
        sl_status = f"TERTEMBUS (Harga sempat melewati SL di {fmt(sl)})"
    else:
        sl_status = f"AMAN (Harga belum menyentuh SL di {fmt(sl)})"

    return _AnalysisIntermediate(
        entry_touched=entry_touched,
        sl_breached=sl_breached,
        target_reached=target_reached,
        travel_ratio=travel_ratio,
        closest_price=closest_price,
        closest_time=closest_time,
        closest_dist=closest_dist,
        post_extreme_price=post_extreme_price,
        post_extreme_time=post_extreme_time,
        status_hint=status_hint,
        entry_status=entry_status,
        tp_status=tp_status,
        sl_status=sl_status,
    )

def analyze_setup(
    spec: SetupSpec,
    candles: Sequence[Candle],
    current_price: Decimal | None = None,
    *,
    atr: Decimal | None = None,
    price_format: cfg.PriceFormat | None = None,
) -> FrontRunAnalysis:
    """Analyze a candle sequence to determine if a setup is front-runned, valid, or invalidated."""

    if not candles:
        raise ConfigError("Cannot validate setup: no candle data provided.")

    if current_price is None:
        current_price = candles[-1].close

    fmt = (price_format or cfg.PriceFormat()).fmt
    direction = spec.direction.strip().lower()
    if direction not in ("long", "short"):
        raise ConfigError(f"Direction must be 'long' or 'short' (got '{spec.direction}').")

    order_status = (spec.order_status or "unfilled").strip().lower()
    if order_status not in ("unfilled", "filled"):
        raise ConfigError(f"Order status must be 'unfilled' or 'filled' (got '{spec.order_status}').")

    entry = spec.entry_price
    tp = spec.tp_price
    sl = spec.sl_price

    if direction == "long" and tp <= entry:
        raise ConfigError(
            f"Invalid LONG setup: TP ({tp}) must be strictly greater than Entry ({entry})."
        )
    if direction == "short" and tp >= entry:
        raise ConfigError(
            f"Invalid SHORT setup: TP ({tp}) must be strictly less than Entry ({entry})."
        )

    if atr is None:
        closed_candles = [c for c in candles if c.is_closed]
        if len(closed_candles) >= cfg.DEFAULT_ATR_PERIOD + 1:
            atr = compute_atr(closed_candles, cfg.DEFAULT_ATR_PERIOD)

    if order_status == "unfilled":
        interm = _analyze_unfilled(candles, direction, entry, tp, fmt)
    else:  # filled
        interm = _analyze_filled(candles, direction, entry, tp, sl, fmt)

    entry_touched = interm.entry_touched
    sl_breached = interm.sl_breached
    target_reached = interm.target_reached
    travel_ratio = interm.travel_ratio
    closest_price = interm.closest_price
    closest_time = interm.closest_time
    closest_dist = interm.closest_dist
    post_extreme_price = interm.post_extreme_price
    post_extreme_time = interm.post_extreme_time
    status_hint = interm.status_hint
    entry_status = interm.entry_status
    tp_status = interm.tp_status
    sl_status = interm.sl_status

    if atr and atr > Decimal(0):
        closest_dist_atr = format((closest_dist / atr).quantize(Decimal("0.01")), "f")
    else:
        closest_dist_atr = "n/a"

    curr_dist_entry = abs(current_price - entry)
    curr_dist_entry_atr = (
        format((curr_dist_entry / atr).quantize(Decimal("0.01")), "f")
        if atr and atr > Decimal(0)
        else "n/a"
    )

    curr_dist_tp = abs(current_price - tp)
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
) -> str:
    """Render the Jinja2 validation prompt."""

    spec = setup or analysis.spec
    interval_str = interval or spec.interval
    provider = provider_label or spec.provider_label
    moment = generated_at or datetime.now(timezone.utc)
    fmt = (price_format or cfg.PriceFormat()).fmt

    is_daily = interval_str == "1d"
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

    order_status_label = (
        "BELUM TERJEMPUT (LIMIT ORDER MASIH PENDING DI EXCHANGE)"
        if spec.order_status == "unfilled"
        else "SUDAH TERJEMPUT (POSISI TRADING AKTIF BERJALAN)"
    )

    context = {
        "PAIR": spec.symbol.upper(),
        "DIRECTION": spec.direction.upper(),
        "ORDER_STATUS_LABEL": order_status_label,
        "IS_UNFILLED": spec.order_status == "unfilled",
        "IS_FILLED": spec.order_status == "filled",
        "ENTRY_PRICE": fmt(spec.entry_price),
        "TP_PRICE": fmt(spec.tp_price),
        "SL_PRICE": fmt(spec.sl_price) if spec.sl_price is not None else "Tidak Ditetapkan",
        "CURRENT_PRICE": fmt(analysis.current_price),
        "GENERATED_AT_UTC": _fmt_ts(moment),
        "provider": provider,
        "INTERVAL_LABEL": cfg.interval_label(interval_str),
        "CANDLE_COUNT": str(len(candles)),
        "CLOSEST_PRICE": fmt(analysis.closest_approach_price),
        "CLOSEST_TIME": _fmt_ts(analysis.closest_approach_time),
        "CLOSEST_DIST": fmt(analysis.closest_approach_distance),
        "CLOSEST_DIST_ATR": analysis.closest_approach_distance_atr,
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
        "TRAVEL_RATIO": f"{analysis.target_travel_ratio:.1f}",
        "SL_STATUS": analysis.sl_status,
        "CURRENT_DIST_TO_ENTRY": fmt(analysis.current_dist_to_entry),
        "CURRENT_DIST_TO_ENTRY_ATR": analysis.current_dist_to_entry_atr,
        "CURRENT_DIST_TO_TP": fmt(analysis.current_dist_to_tp),
        "CURRENT_DIST_TO_TP_ATR": analysis.current_dist_to_tp_atr,
        "STATUS_HINT": analysis.status_hint,
        "CANDLE_TABLE_CSV": csv_table,
    }

    rendered = template.render(**context).strip()
    return rendered + "\n"
