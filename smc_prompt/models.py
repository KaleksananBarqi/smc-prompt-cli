"""Typed data contracts for smc-prompt (spec §5.1).

Every structure crossing a module boundary is an explicit dataclass so the
two-layer payload contract is independently testable. Prices are ``Decimal``
to keep rendering deterministic and byte-stable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


class SwingType(str, enum.Enum):
    """Type of a detected swing point."""

    HIGH = "HIGH"
    LOW = "LOW"


class StructureClass(str, enum.Enum):
    """Mechanical structure classification (never called "bias")."""

    BULLISH = "Bullish"
    BEARISH = "Bearish"
    RANGING = "Ranging/Mixed"
    #: Distinct fact: the last two same-type swings sit within the equal-levels
    #: tolerance of one another (a liquidity pool), which is NOT the same as a
    #: mixed/contradictory sequence. Kept as its own member so downstream docs
    #: can compare against the literal ``"Equal Highs/Lows"`` string.
    EQUAL_LEVELS = "Equal Highs/Lows"


@dataclass(frozen=True)
class Candle:
    """A single OHLCV candle.

    ``open_time`` / ``close_time`` are timezone-aware UTC datetimes.
    ``is_closed`` is advisory metadata computed by the fetcher; the analyzer
    operates on series from which the half-open candle has already been
    removed.
    """

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: datetime
    is_closed: bool = True


@dataclass(frozen=True)
class SwingPoint:
    """A detected fractal swing high or low."""

    index: int
    open_time: datetime
    price: Decimal
    type: SwingType
    status: str = "untested"

    def is_more_extreme_than(self, other: "SwingPoint") -> bool:
        """Return True if this swing is more extreme than ``other``."""

        if self.type is SwingType.HIGH:
            return self.price > other.price
        return self.price < other.price


@dataclass(frozen=True)
class DistanceMetrics:
    """Signed distance from current price to a reference swing."""

    reference: SwingPoint
    text: str


@dataclass(frozen=True)
class VolumeMetrics:
    """Volume-derived confirmation facts for one timeframe (Phase 5, #16).

    ``relative`` is ``last_volume / mean(last N volumes)`` and ``is_spike`` is
    True when ``relative >= volume_spike_mult``. ``mean`` is the raw mean volume
    over the same window (``None`` when the series is shorter than the window).
    These are cheap, purely mechanical facts that give the LLM factual
    confirmation context for entry logic — no interpretation.
    """

    relative: Decimal
    is_spike: bool
    mean: Decimal | None = None
    last: Decimal | None = None
    window: int = 0


@dataclass(frozen=True)
class EqualLevel:
    """A cluster of same-type swings whose prices sit within tolerance.

    ``price`` is the representative level (the most extreme price of the
    cluster: max for a HIGH cluster, min for a LOW cluster) and ``count`` is the
    number of swings in the cluster. Clusters of size 1 are never emitted.
    """

    price: Decimal
    count: int


class FVGDirection(str, enum.Enum):
    """Direction of a mechanical 3-candle Fair Value Gap."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


@dataclass(frozen=True)
class FVG:
    """A mechanical 3-candle Fair Value Gap (spec §4.7).

    Purely derivable from OHLC — no interpretation. A bullish gap exists when
    ``low[i] > high[i-2]`` (range ``[high[i-2], low[i]]``); a bearish gap when
    ``high[i] < low[i-2]`` (range ``[high[i], low[i-2]]``). ``birth_time`` is the
    open time of the 3rd candle (the gap-closing candle). ``filled`` is True when
    a later closed candle trades FULLY through the range: for a bullish gap when
    a later ``low <= lower``, for a bearish gap when a later ``high >= upper``.
    ``ce`` is the 50% Consequent Encroachment (midpoint) of the gap.
    """

    direction: FVGDirection
    lower: Decimal
    upper: Decimal
    birth_time: datetime
    filled: bool
    index: int = 0
    mitigated_index: int | None = None
    ce: Decimal | None = None

    def __post_init__(self) -> None:
        """Hitung Consequent Encroachment (50% midpoint) otomatis bila belum diisi."""
        if self.ce is None:
            object.__setattr__(
                self, "ce", (self.lower + self.upper) / Decimal("2")
            )


@dataclass(frozen=True)
class OrderBlock:
    """An institutional Order Block (OB) linked to an FVG displacement.

    Bullish OB is the last bearish candle prior to a BULL FVG; Bearish OB is
    the last bullish candle prior to a BEAR FVG. ``mt`` is the 50% Mean
    Threshold (midpoint). ``status`` is one of ``"unmitigated"``, ``"tapped"``,
    or ``"invalidated"``.
    """

    direction: FVGDirection
    lower: Decimal
    upper: Decimal
    mt: Decimal
    birth_time: datetime
    index: int = 0
    status: str = "unmitigated"
    invalidated_index: int | None = None


@dataclass(frozen=True)
class ReferenceFacts:
    """The explicitly-named reference levels for one timeframe.

    Each string already carries the formatted price, the date/datetime, the
    direction word relative to current price and the ``swept``/``untested``
    status token, so the renderer substitutes it verbatim (byte-stability).
    The reported (mode-selected) reference is the *recent* one under
    ``--distance-reference most-recent`` and the *nearest* one under the
    ``nearest`` default, so its status is emitted through those fields.
    """

    recent_high: str
    recent_low: str
    nearest_high: str
    nearest_low: str
    window_high: str
    window_low: str
    ref_high_summary: str = ""
    ref_low_summary: str = ""
    dealing_range_summary: str = ""


@dataclass(frozen=True)
class TimeframeAnalysis:
    """Complete Layer-A analysis result for one timeframe."""

    timeframe: str
    structure_class: StructureClass
    swing_high: SwingPoint
    swing_low: SwingPoint
    dist_to_high: DistanceMetrics
    dist_to_low: DistanceMetrics
    atr: Decimal | None
    candle_count: int
    closed_count: int = 0
    swings: tuple[SwingPoint, ...] = field(default_factory=tuple)
    #: :func:`structure_analyzer.compute_level_status` result for the reported
    #: reference high (``"swept"`` / ``"untested"``).
    swing_high_status: str = "untested"
    #: Same, for the reported reference low.
    swing_low_status: str = "untested"
    #: Equal-high clusters (size >= 2), chronological; empty when none.
    equal_highs: tuple[EqualLevel, ...] = field(default_factory=tuple)
    #: Equal-low clusters (size >= 2), chronological; empty when none.
    equal_lows: tuple[EqualLevel, ...] = field(default_factory=tuple)
    #: Detected mechanical 3-candle Fair Value Gaps, chronological; empty when none.
    fvgs: tuple[FVG, ...] = field(default_factory=tuple)
    order_blocks: tuple[OrderBlock, ...] = field(default_factory=tuple)
    #: The last ``config.swings_table_rows`` swings actually rendered.
    rendered_swings: tuple[SwingPoint, ...] = field(default_factory=tuple)
    #: Formatted recent/nearest/window-extreme reference strings (payload-ready).
    reference_facts: ReferenceFacts | None = None
    #: Current price the references were measured against (sanity re-checks).
    current_price: Decimal | None = None
    #: Nearest-by-price swing high/low (always computed for sanity checks and
    #: for the ``*_REF_*_NEAREST`` payload strings).
    nearest_high: SwingPoint | None = None
    nearest_low: SwingPoint | None = None
    #: Level-breach status of the nearest references (``swept``/``untested``).
    nearest_high_status: str = "untested"
    nearest_low_status: str = "untested"
    #: Volume-derived confirmation facts (Phase 5, #16); ``None`` when the
    #: series is too short to compute a relative volume.
    volume: VolumeMetrics | None = None


@dataclass(frozen=True)
class PayloadSection:
    """A rendered section of the prompt (used for diagnostics/tests)."""

    name: str
    text: str


@dataclass(frozen=True)
class RenderedPrompt:
    """The final prompt plus the placeholder map that produced it."""

    text: str
    placeholders: dict[str, str]


#: The complete set of required template placeholders (spec §10). A rendered
#: prompt is only produced when every one of these is present and non-empty.
REQUIRED_PLACEHOLDERS: tuple[str, ...] = (
    "PAIR",
    "GENERATED_AT_UTC",
    "SESSION_KILLZONE_UTC",
    "CURRENT_PRICE",
    #: ATR(14) as a percentage of current price (Phase 5, #6).
    "ATR_PCT_OF_PRICE",
    "HTF_STRUCTURE_CLASS",
    "HTF_REF_HIGH_SUMMARY",
    "HTF_REF_LOW_SUMMARY",
    "HTF_DEALING_RANGE",
    "HTF_EQUAL_HIGHS",
    "HTF_EQUAL_LOWS",
    "HTF_VOLUME_RELATIVE",
    "HTF_VOLUME_SPIKE",
    "HTF_INTERVAL_LABEL",
    "HTF_CANDLE_COUNT",
    "HTF_CANDLE_TABLE_CSV",
    "HTF_SWINGS_TABLE",
    "HTF_FVG_TABLE",
    "HTF_FVG_COUNT",
    "HTF_OB_TABLE",
    "HTF_OB_COUNT",
    "MTF_STRUCTURE_CLASS",
    "MTF_REF_HIGH_SUMMARY",
    "MTF_REF_LOW_SUMMARY",
    "MTF_DEALING_RANGE",
    "MTF_EQUAL_HIGHS",
    "MTF_EQUAL_LOWS",
    "MTF_VOLUME_RELATIVE",
    "MTF_VOLUME_SPIKE",
    "MTF_INTERVAL_LABEL",
    "MTF_CANDLE_COUNT",
    "MTF_CANDLE_TABLE_CSV",
    "MTF_SWINGS_TABLE",
    "MTF_FVG_TABLE",
    "MTF_FVG_COUNT",
    "MTF_OB_TABLE",
    "MTF_OB_COUNT",
    "LTF_STRUCTURE_CLASS",
    "LTF_REF_HIGH_SUMMARY",
    "LTF_REF_LOW_SUMMARY",
    "LTF_DEALING_RANGE",
    "LTF_EQUAL_HIGHS",
    "LTF_EQUAL_LOWS",
    "LTF_VOLUME_RELATIVE",
    "LTF_VOLUME_SPIKE",
    "LTF_INTERVAL_LABEL",
    "LTF_CANDLE_COUNT",
    "LTF_CANDLE_TABLE_CSV",
    "LTF_SWINGS_TABLE",
    "LTF_FVG_TABLE",
    "LTF_FVG_COUNT",
    "LTF_OB_TABLE",
    "LTF_OB_COUNT",
)

#: Placeholders that are dropped entirely when ATR is disabled.
OPTIONAL_PLACEHOLDERS: tuple[str, ...] = ("HTF_ATR14", "MTF_ATR14", "LTF_ATR14")
