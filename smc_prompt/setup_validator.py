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

    @property
    def status(self) -> str:
        if self.sl_breached:
            return "STOPPED_OUT"
        if self.target_reached and not self.entry_touched:
            return "DOL_REACHED"
        if not self.entry_touched and self.target_travel_ratio >= Decimal("60.0"):
            return "FRONT_RUNNED"
        if not self.entry_touched:
            return "FRESH"
        return "TRIGGERED"

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

    if direction == "long":
        # For LONG limit order, closest approach is lowest low
        closest_candle = min(candles, key=lambda c: c.low)
        closest_price = closest_candle.low
        closest_time = closest_candle.open_time
        entry_touched = closest_price <= entry

        highest_candle = max(candles, key=lambda c: c.high)
        target_reached = highest_candle.high >= tp
        sl_breached = (closest_price <= sl) if sl is not None else False

        total_target_span = tp - entry
        peak = highest_candle.high
        if peak <= entry:
            travel_ratio = Decimal("0.0")
        else:
            travel_ratio = min(
                Decimal("100.0"),
                ((peak - entry) / total_target_span) * Decimal("100"),
            )

    else:  # short
        # For SHORT limit order, closest approach is highest high
        closest_candle = max(candles, key=lambda c: c.high)
        closest_price = closest_candle.high
        closest_time = closest_candle.open_time
        entry_touched = closest_price >= entry

        lowest_candle = min(candles, key=lambda c: c.low)
        target_reached = lowest_candle.low <= tp
        sl_breached = (closest_price >= sl) if sl is not None else False

        total_target_span = entry - tp
        trough = lowest_candle.low
        if trough >= entry:
            travel_ratio = Decimal("0.0")
        else:
            travel_ratio = min(
                Decimal("100.0"),
                ((entry - trough) / total_target_span) * Decimal("100"),
            )

    closest_dist = abs(closest_price - entry)
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

    # Determine status_hint
    if sl_breached:
        status_hint = "STOPPED_OUT (Level SL tertembus)"
    elif target_reached and not entry_touched:
        status_hint = "DOL_REACHED (Target TP tersapu duluan sebelum Entry terjemput - INVALID)"
    elif not entry_touched and travel_ratio >= Decimal("60.0"):
        status_hint = f"FRONT_RUNNED (Harga berbalik sebelum Entry dan menempuh {travel_ratio:.1f}% ke TP)"
    elif not entry_touched:
        status_hint = "FRESH (Setup belum terjemput dan belum menempuh >=60% ke TP)"
    else:
        status_hint = "TRIGGERED (Level Entry sudah tersentuh / posisi aktif)"

    entry_status = (
        "TERJEMPUT (Wick/Body sempat menyentuh/menembus level entry)"
        if entry_touched
        else f"BELUM TERJEMPUT (Selisih {fmt(closest_dist)} dari level entry)"
    )

    tp_status = (
        "TERSAPU (Target TP / DOL sudah tersentuh/terlewati)"
        if target_reached
        else f"BELUM TERSAPU (Perjalanan baru menempuh {travel_ratio:.1f}% ke arah TP)"
    )

    if sl is None:
        sl_status = "Tidak Ditetapkan (None)"
    elif sl_breached:
        sl_status = f"TERTEMBUS (Harga sempat melewati SL di {fmt(sl)})"
    else:
        sl_status = f"AMAN (Harga belum menyentuh SL di {fmt(sl)})"

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

    context = {
        "PAIR": spec.symbol.upper(),
        "DIRECTION": spec.direction.upper(),
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
