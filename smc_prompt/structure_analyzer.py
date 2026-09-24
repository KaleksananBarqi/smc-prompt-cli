"""Pure, deterministic structure analysis (spec §7).

Implements, in order:
  1. :func:`prepare_series`       — drop the half-open candle, validate history
  2. :func:`compute_atr`          — ATR(14) with simple-mean smoothing
  3. :func:`detect_swings`        — N-bar Williams fractal + ATR separation
                                    filter + alternating-skeleton post-pass
  4. :func:`classify_structure`   — mechanical Bullish / Bearish / Equal
                                    Highs/Lows / Ranging
  5. :func:`detect_equal_levels`  — clustered equal highs/lows (liquidity pool)
  6. :func:`detect_fvg`           — mechanical 3-candle Fair Value Gaps + fill
  7. :func:`compute_level_status` — swept/untested flag per level
  8. :func:`compute_distance_metrics` — signed distance to reference swings
  9. :func:`reference_sanity_warnings` — contradictions vs ``structure_class``

No randomness; no recursion; no lookahead beyond the fractal window. All
functions are network-free and unit-testable in isolation.

Important: analysis runs over the *full* closed series (which includes the
``context_buffer`` fetched beyond the emitted table), so swings near the left
edge of the emitted table remain detectable. Only the emitted table is
truncated to the requested row count.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

import pandas as pd

from . import config as cfg
from .errors import InsufficientDataError
from .models import (
    Candle,
    DistanceMetrics,
    EqualLevel,
    FVG,
    FVGDirection,
    ReferenceFacts,
    StructureClass,
    SwingPoint,
    SwingType,
    TimeframeAnalysis,
    VolumeMetrics,
)


@dataclass(frozen=True)
class SeriesStats:
    """Diagnostics returned alongside the prepared series."""

    closed_count: int
    emitted_count: int
    requested_count: int
    zero_volume_streak: int

    @property
    def was_reduced(self) -> bool:
        """True when fewer closed candles exist than were requested."""

        return self.emitted_count < self.requested_count


# --------------------------------------------------------------------------
# 1. Series preparation
# --------------------------------------------------------------------------


def minimum_required(swing_lookback: int, atr_period: int) -> int:
    """Minimum closed candles needed for ATR + at least one fractal window."""

    return max(cfg.MIN_CANDLES, atr_period + 1, swing_lookback)


def prepare_series(
    candles: Sequence[Candle],
    *,
    timeframe: str,
    requested: int,
    swing_lookback: int = cfg.DEFAULT_SWING_LOOKBACK,
    atr_period: int = cfg.DEFAULT_ATR_PERIOD,
) -> tuple[list[Candle], SeriesStats]:
    """Drop the half-open candle and enforce minimum history.

    Auto-reduces the emitted table length when fewer closed candles exist than
    requested (non-fatal; the caller emits a WARNING). Raises
    :class:`InsufficientDataError` when history is below the minimum needed
    for the fractal window + ATR.
    """

    closed = [candle for candle in candles if candle.is_closed]

    required_min = minimum_required(swing_lookback, atr_period)
    if len(closed) < required_min:
        raise InsufficientDataError(
            f"Not enough closed {timeframe} history to compute structure "
            f"(need >= {required_min}, got {len(closed)})."
        )

    streak = 0
    for candle in reversed(closed):
        if candle.volume == 0:
            streak += 1
        else:
            break

    emitted_count = min(requested, len(closed))
    stats = SeriesStats(
        closed_count=len(closed),
        emitted_count=emitted_count,
        requested_count=requested,
        zero_volume_streak=streak,
    )
    return closed, stats


def emitted_table(closed: Sequence[Candle], emitted_count: int) -> list[Candle]:
    """Return the last ``emitted_count`` closed candles (oldest -> newest)."""

    return list(closed)[-emitted_count:]


# --------------------------------------------------------------------------
# 2. ATR(14)
# --------------------------------------------------------------------------


def compute_atr(
    candles: Sequence[Candle], period: int = cfg.DEFAULT_ATR_PERIOD
) -> Decimal:
    """Classic True Range with simple-mean smoothing over the last ``period``."""

    if len(candles) < period + 1:
        raise InsufficientDataError(
            f"Not enough closed candles to compute ATR({period}) "
            f"(need >= {period + 1}, got {len(candles)})."
        )

    highs = pd.Series([float(c.high) for c in candles], dtype="float64")
    lows = pd.Series([float(c.low) for c in candles], dtype="float64")
    closes = pd.Series([float(c.close) for c in candles], dtype="float64")

    prev_close = closes.shift(1)
    tr = pd.concat(
        [
            (highs - lows),
            (highs - prev_close).abs(),
            (lows - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.iloc[1:].tail(period).mean()
    return Decimal(str(round(float(atr), 10)))


# --------------------------------------------------------------------------
# 2b. Volume-derived confirmation facts (Phase 5, #16)
# --------------------------------------------------------------------------


def compute_relative_volume(
    candles: Sequence[Candle],
    *,
    period: int = cfg.DEFAULT_VOLUME_MEAN_PERIOD,
    spike_mult: Decimal = cfg.DEFAULT_VOLUME_SPIKE_MULT,
    enabled: bool = True,
) -> VolumeMetrics | None:
    """Compute ``last / mean(last ``period`` volumes)`` and the spike flag.

    Purely mechanical: the window is the last ``period`` closed candles
    (including the last one). Returns ``None`` when fewer than two candles are
    available so no fabricated ratio is emitted, and the ratio is ``0``-safe
    (a zero mean volume yields a ``Decimal("0")`` relative volume rather than a
    division error). ``is_spike`` is ``relative >= spike_mult``.

    ``enabled=False`` (``config.volume_available`` is False for providers whose
    feed carries no real volume, e.g. OANDA's tick counts) returns ``None`` so
    the facts render ``n/a`` instead of a fabricated ``x0.00``/``spike: no``.
    """

    if not enabled:
        return None
    if len(candles) < 2:
        return None

    window = list(candles)[-period:] if period > 0 else list(candles)
    if not window:
        return None

    total = sum((candle.volume for candle in window), Decimal("0"))
    mean = total / Decimal(len(window))
    last = window[-1].volume

    if mean == 0:
        relative = Decimal("0")
    else:
        # Quantize to a fixed scale so the ratio (and its rendering) is
        # byte-stable across inputs.
        relative = (last / mean).quantize(Decimal("0.0001"))

    return VolumeMetrics(
        relative=relative,
        is_spike=relative >= Decimal(str(spike_mult)),
        mean=mean,
        last=last,
        window=len(window),
    )


# --------------------------------------------------------------------------
# 3. Swing detection (N-bar Williams fractal)
# --------------------------------------------------------------------------


def enforce_alternation(swings: Sequence[SwingPoint]) -> list[SwingPoint]:
    """Collapse runs of consecutive same-type swings to the extreme one.

    The ATR-merge pass only compares a candidate against the last *same-type*
    swing and may ``pop()``+``append()``, which can leave consecutive same-type
    swings (H,H,L,L). This deterministic O(n) post-pass walks the sequence once
    and keeps a genuine alternating H/L/H/L skeleton: within a run of same-type
    swings it retains the single most extreme one (max price for HIGH, min price
    for LOW). For equal extremes the first-seen is kept (deterministic tie-break).
    No recursion, no lookahead.
    """

    result: list[SwingPoint] = []
    for swing in swings:
        if result and result[-1].type is swing.type:
            if swing.is_more_extreme_than(result[-1]):
                result[-1] = swing
            # else: keep the existing representative of this run
        else:
            result.append(swing)
    return result


def _extract_raw_swings(candles: Sequence[Candle], half: int) -> list[SwingPoint]:
    raw: list[SwingPoint] = []
    for i in range(half, len(candles) - half):
        center = candles[i]
        window = candles[i - half : i + half + 1]

        others = [c for j, c in enumerate(window) if j != half]
        if not others:
            continue

        is_high = center.high > max(c.high for c in others)
        is_low = center.low < min(c.low for c in others)

        if is_high:
            raw.append(
                SwingPoint(i, center.open_time, center.high, SwingType.HIGH)
            )
        elif is_low:
            raw.append(
                SwingPoint(i, center.open_time, center.low, SwingType.LOW)
            )
    return raw


def _filter_by_atr(raw: list[SwingPoint], threshold: Decimal) -> list[SwingPoint]:
    filtered: list[SwingPoint] = []
    for swing in raw:
        last_same_index: int | None = None
        for idx in range(len(filtered) - 1, -1, -1):
            if filtered[idx].type is swing.type:
                last_same_index = idx
                break

        if last_same_index is None:
            filtered.append(swing)
            continue

        last_same = filtered[last_same_index]
        if abs(swing.price - last_same.price) < threshold:
            if swing.is_more_extreme_than(last_same):
                filtered.pop(last_same_index)
                filtered.append(swing)
            # else: keep the prior, more significant swing
        else:
            filtered.append(swing)
    return filtered


def detect_swings(
    candles: Sequence[Candle],
    *,
    n: int = cfg.DEFAULT_SWING_LOOKBACK,
    atr_value: Decimal | None = None,
    merge_mult: Decimal = cfg.DEFAULT_SWING_MERGE_ATR_MULT,
) -> list[SwingPoint]:
    """Detect fractal swings and apply the deterministic ATR separation filter.

    The ATR filter is followed by :func:`enforce_alternation`, so the returned
    sequence is always a genuine alternating H/L/H/L skeleton. That skeleton is
    what classification, reference selection and the rendered swing table consume.
    """

    if n % 2 == 0 or n < 3:
        raise ValueError("fractal window n must be an odd integer >= 3")

    half = (n - 1) // 2
    raw = _extract_raw_swings(candles, half)

    if atr_value is None:
        return raw

    threshold = merge_mult * atr_value
    filtered = _filter_by_atr(raw, threshold)

    # Final deterministic cleanup: never leak consecutive same-type swings.
    return enforce_alternation(filtered)


# --------------------------------------------------------------------------
# 4. Structure classification (mechanical — never "bias")
# --------------------------------------------------------------------------


def classify_structure(
    swings: Sequence[SwingPoint],
    config: cfg.Config,
    *,
    atr_value: Decimal | None = None,
) -> StructureClass:
    """Classify structure from the most recent swings (spec §4.4/§7.2).

    Strict ``>`` / ``<`` comparisons alone would collapse two near-equal highs
    (the textbook liquidity pool) into ``Ranging/Mixed``. When the equal-levels
    tolerance (``equal_levels_atr_mult × ATR``) is available and BOTH the last
    high and the last low sit within it of their predecessor, the distinct
    :data:`StructureClass.EQUAL_LEVELS` fact is returned instead.
    """

    if len(swings) < config.structure_min_swings:
        return StructureClass.RANGING

    recent = list(swings)[-config.structure_last_swings :]
    highs = [s for s in recent if s.type is SwingType.HIGH]
    lows = [s for s in recent if s.type is SwingType.LOW]

    if len(highs) < 2 or len(lows) < 2:
        return StructureClass.RANGING

    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price

    if atr_value is not None:
        tol = config.equal_levels_atr_mult * atr_value
        high_equal = abs(highs[-1].price - highs[-2].price) <= tol
        low_equal = abs(lows[-1].price - lows[-2].price) <= tol
        if high_equal and low_equal:
            return StructureClass.EQUAL_LEVELS

    if hh and hl:
        return StructureClass.BULLISH
    if lh and ll:
        return StructureClass.BEARISH
    return StructureClass.RANGING


# --------------------------------------------------------------------------
# 4b. Equal highs / equal lows (liquidity pool facts)
# --------------------------------------------------------------------------


def _equal_clusters(
    swings: Sequence[SwingPoint], swing_type: SwingType, tol: Decimal
) -> list[EqualLevel]:
    """Cluster same-type swings whose prices are within ``tol`` of each other.

    Scans swings in chronological order. A swing joins the open cluster when its
    price is within ``tol`` of the cluster's representative (the running most
    extreme price); otherwise the cluster closes and a new one opens. Only
    clusters of size >= 2 are emitted, in chronological order. Deterministic.
    """

    clusters: list[EqualLevel] = []
    price: Decimal | None = None
    count = 0

    def close() -> None:
        nonlocal price, count
        if price is not None and count >= 2:
            clusters.append(EqualLevel(price=price, count=count))
        price = None
        count = 0

    for swing in swings:
        if swing.type is not swing_type:
            continue
        if price is None:
            price, count = swing.price, 1
            continue
        if abs(swing.price - price) <= tol:
            count += 1
            if swing_type is SwingType.HIGH:
                if swing.price > price:
                    price = swing.price
            else:
                if swing.price < price:
                    price = swing.price
        else:
            close()
            price, count = swing.price, 1

    close()
    return clusters


def detect_equal_levels(
    swings: Sequence[SwingPoint], tol: Decimal
) -> tuple[tuple[EqualLevel, ...], tuple[EqualLevel, ...]]:
    """Return ``(equal_highs, equal_lows)`` as tuples of :class:`EqualLevel`.

    ``tol`` is the absolute price tolerance, normally
    ``config.equal_levels_atr_mult × ATR``. Empty tuples when no pool exists.
    """

    highs = tuple(_equal_clusters(swings, SwingType.HIGH, tol))
    lows = tuple(_equal_clusters(swings, SwingType.LOW, tol))
    return highs, lows


# --------------------------------------------------------------------------
# 4c. Mechanical Fair Value Gaps (3-candle price gaps)
# --------------------------------------------------------------------------


def _fvg_filled(
    candles: Sequence[Candle],
    index: int,
    direction: FVGDirection,
    lower: Decimal,
    upper: Decimal,
) -> bool:
    """True when a candle strictly after the birth candle fully closes the gap.

    A bullish gap is filled when any later closed candle's low trades at or
    below its lower bound; a bearish gap when any later closed candle's high
    trades at or above its upper bound. Deterministic, closed-series only.
    """

    for candle in candles[index + 1 :]:
        if direction is FVGDirection.BULLISH:
            if candle.low <= lower:
                return True
        else:
            if candle.high >= upper:
                return True
    return False


def detect_fvg(
    candles: Sequence[Candle],
    min_gap_atr_mult: Decimal,
    *,
    atr_value: Decimal | None = None,
) -> list[FVG]:
    """Detect mechanical 3-candle Fair Value Gaps (spec §4.7).

    A bullish FVG exists at candle ``i`` when ``low[i] > high[i-2]`` (range
    ``[high[i-2], low[i]]``); a bearish FVG when ``high[i] < low[i-2]`` (range
    ``[high[i], low[i-2]]``). Purely mechanical — derivable from OHLC alone.

    Gaps narrower than ``min_gap_atr_mult × ATR`` (when ``atr_value`` is given)
    are discarded as sub-noise, mirroring the equal-levels tolerance pattern.
    The returned list is chronological (oldest -> newest); the 3rd candle's
    open time is the gap's birth timestamp. No lookahead beyond the 3-candle
    window; no I/O; deterministic.
    """

    fvgs: list[FVG] = []
    if len(candles) < 3:
        return fvgs

    threshold = (
        min_gap_atr_mult * atr_value if atr_value is not None else Decimal("0")
    )

    for i in range(2, len(candles)):
        first = candles[i - 2]
        third = candles[i]

        if third.low > first.high:
            direction = FVGDirection.BULLISH
            lower, upper = first.high, third.low
        elif third.high < first.low:
            direction = FVGDirection.BEARISH
            lower, upper = third.high, first.low
        else:
            continue

        if (upper - lower) < threshold:
            continue

        fvgs.append(
            FVG(
                direction=direction,
                lower=lower,
                upper=upper,
                birth_time=third.open_time,
                filled=_fvg_filled(candles, i, direction, lower, upper),
                index=i,
            )
        )

    return fvgs


# --------------------------------------------------------------------------
# 4d. Level breach status (swept / untested) over the CLOSED series
# --------------------------------------------------------------------------


def compute_level_status(
    candles: Sequence[Candle], level: SwingPoint, swing_type: SwingType
) -> str:
    """Return ``"swept"`` or ``"untested"`` for ``level`` over closed candles.

    A high is *swept* when any closed candle **strictly after** the swing's own
    candle prints a high strictly above the level; a low is *swept* when any
    later candle prints a low strictly below the level. Otherwise ``untested``.
    Mechanical and deterministic; operates on the closed series only.
    """

    for candle in candles:
        if candle.open_time <= level.open_time:
            continue
        if swing_type is SwingType.HIGH:
            if candle.high > level.price:
                return "swept"
        else:
            if candle.low < level.price:
                return "swept"
    return "untested"


# --------------------------------------------------------------------------
# 5. Distance metrics
# --------------------------------------------------------------------------


def select_reference(
    swings: Sequence[SwingPoint],
    swing_type: SwingType,
    current_price: Decimal,
    mode: str,
) -> SwingPoint:
    """Select the reference swing of ``swing_type``.

    ``nearest``      -> closest by absolute price distance to current price.
    ``most-recent``  -> latest by timestamp (spec §4.5 default reading).
    """

    candidates = [s for s in swings if s.type is swing_type]
    if not candidates:
        raise InsufficientDataError(
            f"No {swing_type.value} swing detected; cannot compute distance."
        )
    if mode == cfg.DISTANCE_REFERENCE_MOST_RECENT:
        return candidates[-1]
    return min(candidates, key=lambda s: (abs(current_price - s.price), -s.index))


def compute_distance_metrics(
    current_price: Decimal,
    swings: Sequence[SwingPoint],
    *,
    mode: str = cfg.DISTANCE_REFERENCE_NEAREST,
    price_format: cfg.PriceFormat | None = None,
) -> tuple[SwingPoint, SwingPoint, DistanceMetrics, DistanceMetrics]:
    """Return (ref_high, ref_low, dist_to_high, dist_to_low)."""

    pf = price_format or cfg.PriceFormat()
    ref_high = select_reference(swings, SwingType.HIGH, current_price, mode)
    ref_low = select_reference(swings, SwingType.LOW, current_price, mode)
    dist_high = DistanceMetrics(
        ref_high, pf.fmt_distance(current_price, ref_high.price)
    )
    dist_low = DistanceMetrics(
        ref_low, pf.fmt_distance(current_price, ref_low.price)
    )
    return ref_high, ref_low, dist_high, dist_low


# --------------------------------------------------------------------------
# 6. Reference facts + sanity warnings
# --------------------------------------------------------------------------


def _direction_word(current_price: Decimal, price: Decimal) -> str:
    """Direction word of a level relative to current price (byte-stable)."""

    if price > current_price:
        return "DI ATAS harga"
    if price < current_price:
        return "DI BAWAH harga"
    return "DI HARGA SAAT INI"


def _format_reference(
    current_price: Decimal,
    swing: SwingPoint,
    *,
    htf: bool,
    status: str,
    price_format: cfg.PriceFormat | None = None,
) -> str:
    """Render one reference level string, payload-ready and byte-stable.

    Format: ``<price> pada <stamp> (<direction word>), status: <swept|untested>``
    where ``<stamp>`` is ``%Y-%m-%d`` for HTF and ``%Y-%m-%d %H:%M`` for LTF.
    """

    pf = price_format or cfg.PriceFormat()
    stamp = (
        cfg.fmt_htf_date(swing.open_time)
        if htf
        else cfg.fmt_ltf_datetime(swing.open_time)
    )
    direction = _direction_word(current_price, swing.price)
    return (
        f"{pf.fmt(swing.price)} pada {stamp} ({direction}), "
        f"status: {status}"
    )


def _window_extreme(
    swings: Sequence[SwingPoint], swing_type: SwingType
) -> SwingPoint | None:
    """Most extreme high/low within the rendered swing window (or None)."""

    candidates = [s for s in swings if s.type is swing_type]
    if not candidates:
        return None
    if swing_type is SwingType.HIGH:
        return max(candidates, key=lambda s: s.price)
    return min(candidates, key=lambda s: s.price)


def build_reference_facts(
    current_price: Decimal,
    *,
    htf: bool,
    candles: Sequence[Candle],
    swings: Sequence[SwingPoint],
    rendered_swings: Sequence[SwingPoint],
    recent_high: SwingPoint,
    recent_low: SwingPoint,
    nearest_high: SwingPoint | None = None,
    nearest_low: SwingPoint | None = None,
    price_format: cfg.PriceFormat | None = None,
) -> ReferenceFacts:
    """Build the recent/nearest/window-extreme reference strings.

    The reported (mode-selected) reference is the *recent* one for
    ``most-recent`` mode and the *nearest* one for the default ``nearest``
    mode; each level carries its own ``swept``/``untested`` status derived from
    the closed candle series so the two always agree.
    """

    if nearest_high is None:
        nearest_high = select_reference(
            swings, SwingType.HIGH, current_price, cfg.DISTANCE_REFERENCE_NEAREST
        )
    if nearest_low is None:
        nearest_low = select_reference(
            swings, SwingType.LOW, current_price, cfg.DISTANCE_REFERENCE_NEAREST
        )

    window_high = _window_extreme(rendered_swings, SwingType.HIGH)
    window_low = _window_extreme(rendered_swings, SwingType.LOW)

    return ReferenceFacts(
        recent_high=_format_reference(
            current_price,
            recent_high,
            htf=htf,
            status=compute_level_status(candles, recent_high, SwingType.HIGH),
            price_format=price_format,
        ),
        recent_low=_format_reference(
            current_price,
            recent_low,
            htf=htf,
            status=compute_level_status(candles, recent_low, SwingType.LOW),
            price_format=price_format,
        ),
        nearest_high=_format_reference(
            current_price,
            nearest_high,
            htf=htf,
            status=compute_level_status(candles, nearest_high, SwingType.HIGH),
            price_format=price_format,
        ),
        nearest_low=_format_reference(
            current_price,
            nearest_low,
            htf=htf,
            status=compute_level_status(candles, nearest_low, SwingType.LOW),
            price_format=price_format,
        ),
        window_high=_format_reference(
            current_price,
            window_high if window_high is not None else nearest_high,
            htf=htf,
            status=compute_level_status(
                candles,
                window_high if window_high is not None else nearest_high,
                SwingType.HIGH,
            ),
            price_format=price_format,
        ),
        window_low=_format_reference(
            current_price,
            window_low if window_low is not None else nearest_low,
            htf=htf,
            status=compute_level_status(
                candles,
                window_low if window_low is not None else nearest_low,
                SwingType.LOW,
            ),
            price_format=price_format,
        ),
    )


def reference_sanity_warnings(
    analysis: TimeframeAnalysis, config: cfg.Config
) -> list[str]:
    """Mechanical contradiction checks between references and structure_class.

    Deterministic; returns an empty list when consistent. Current checks:

    * ``Bullish`` but the nearest low is already broken *above* current price
      (a lower-low structure the reports still call bullish on the near side).
    * ``Bearish`` but the nearest high is already broken *below* current price.
    """

    warnings: list[str] = []
    current = analysis.current_price
    if current is None:
        return warnings

    fmt = config.price_format.fmt

    if (
        analysis.structure_class is StructureClass.BULLISH
        and analysis.nearest_low is not None
        and analysis.nearest_low.price > current
    ):
        warnings.append(
            f"{analysis.timeframe}: structure_class Bullish but nearest low "
            f"{fmt(analysis.nearest_low.price)} is already above "
            f"current price {fmt(current)} (broken level)."
        )

    if (
        analysis.structure_class is StructureClass.BEARISH
        and analysis.nearest_high is not None
        and analysis.nearest_high.price < current
    ):
        warnings.append(
            f"{analysis.timeframe}: structure_class Bearish but nearest high "
            f"{fmt(analysis.nearest_high.price)} is already below "
            f"current price {fmt(current)} (broken level)."
        )

    return warnings


# --------------------------------------------------------------------------
# Orchestration for a single timeframe
# --------------------------------------------------------------------------


def analyze(
    candles: Sequence[Candle],
    *,
    timeframe: str,
    requested: int,
    current_price: Decimal,
    config: cfg.Config,
) -> tuple[TimeframeAnalysis, list[Candle], SeriesStats]:
    """Run the full Layer-A analysis pipeline for one timeframe.

    Returns the analysis, the emitted table candles, and the series stats.
    """

    closed, stats = prepare_series(
        candles,
        timeframe=timeframe,
        requested=requested,
        swing_lookback=config.swing_lookback,
        atr_period=config.atr_period,
    )

    # ATR is ALWAYS computed when enough history exists: it drives the
    # deterministic swing-separation filter (spec §4.2 / §7.1) and must not
    # depend on whether the user wants the ATR lines *displayed*.
    # ``--no-atr`` only suppresses the rendered ATR placeholders, which is
    # handled in ``template_renderer.build_payload``. Coupling it to the
    # computation here would silently change the reported swings/structure.
    atr: Decimal | None = None
    if len(closed) >= config.atr_period + 1:
        atr = compute_atr(closed, config.atr_period)

    swings = detect_swings(
        closed,
        n=config.swing_lookback,
        atr_value=atr,
        merge_mult=config.swing_merge_atr_mult,
    )
    if not swings:
        raise InsufficientDataError(
            f"No swings detected on {timeframe} series "
            f"({len(closed)} closed candles). Prompt not generated."
        )

    structure = classify_structure(swings, config, atr_value=atr)
    ref_high, ref_low, dist_high, dist_low = compute_distance_metrics(
        current_price,
        swings,
        mode=config.distance_reference,
        price_format=config.price_format,
    )

    # Both explicit references are ALWAYS computed: the nearest-by-price pair
    # feeds the *_REF_*_NEAREST payload strings and the structure sanity check
    # even when the reported reference uses ``most-recent`` mode (and vice
    # versa for the recent-by-time pair).
    recent_high = select_reference(
        swings, SwingType.HIGH, current_price, cfg.DISTANCE_REFERENCE_MOST_RECENT
    )
    recent_low = select_reference(
        swings, SwingType.LOW, current_price, cfg.DISTANCE_REFERENCE_MOST_RECENT
    )
    nearest_high = select_reference(
        swings, SwingType.HIGH, current_price, cfg.DISTANCE_REFERENCE_NEAREST
    )
    nearest_low = select_reference(
        swings, SwingType.LOW, current_price, cfg.DISTANCE_REFERENCE_NEAREST
    )

    rendered_swings = tuple(swings[-config.swings_table_rows :])

    tol = (
        config.equal_levels_atr_mult * atr if atr is not None else Decimal("0")
    )
    equal_highs, equal_lows = detect_equal_levels(swings, tol)

    fvgs = tuple(detect_fvg(closed, config.fvg_atr_mult, atr_value=atr))

    volume = compute_relative_volume(
        closed,
        period=config.volume_mean_period,
        spike_mult=config.volume_spike_mult,
        enabled=config.volume_available,
    )

    reference_facts = build_reference_facts(
        current_price,
        htf=(timeframe == config.htf_interval),
        candles=closed,
        swings=swings,
        rendered_swings=rendered_swings,
        recent_high=recent_high,
        recent_low=recent_low,
        nearest_high=nearest_high,
        nearest_low=nearest_low,
        price_format=config.price_format,
    )

    analysis = TimeframeAnalysis(
        timeframe=timeframe,
        structure_class=structure,
        swing_high=ref_high,
        swing_low=ref_low,
        dist_to_high=dist_high,
        dist_to_low=dist_low,
        atr=atr,
        candle_count=stats.emitted_count,
        swings=tuple(swings),
        swing_high_status=compute_level_status(closed, ref_high, SwingType.HIGH),
        swing_low_status=compute_level_status(closed, ref_low, SwingType.LOW),
        equal_highs=equal_highs,
        equal_lows=equal_lows,
        fvgs=fvgs,
        rendered_swings=rendered_swings,
        reference_facts=reference_facts,
        current_price=current_price,
        nearest_high=nearest_high,
        nearest_low=nearest_low,
        nearest_high_status=compute_level_status(
            closed, nearest_high, SwingType.HIGH
        ),
        nearest_low_status=compute_level_status(
            closed, nearest_low, SwingType.LOW
        ),
        volume=volume,
    )
    return analysis, emitted_table(closed, stats.emitted_count), stats
