"""Unit tests for the pure structure analyzer (Phase 4, #15).

All tests are network-free; inputs are synthetic in-memory candles/swings so the
deterministic contracts of prepare_series / detect_swings / classify_structure /
detect_equal_levels / detect_fvg and the tickSize→decimals derivation are pinned.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from smc_prompt import config as cfg
from smc_prompt.errors import InsufficientDataError
from smc_prompt.models import (
    FVGDirection,
    StructureClass,
    SwingType,
)
from smc_prompt.structure_analyzer import (
    classify_structure,
    compute_atr,
    detect_equal_levels,
    detect_fvg,
    enforce_alternation,
    format_dealing_range_summary,
    minimum_required,
    prepare_series,
    select_dealing_range_bounds,
)

from .conftest import make_candle, make_swing


# --------------------------------------------------------------------------
# detect_swings (via raw fractal output + ATR merge)
# --------------------------------------------------------------------------


def _fractal_candles() -> list:
    """9 candles: strict HIGH@2, strict LOW@4, strict HIGH@6 (n=5 windows)."""

    highs = ["90", "95", "100", "95", "52", "95", "100.3", "95", "90"]
    lows = ["80", "85", "88", "85", "50", "85", "88", "85", "80"]
    return [
        make_candle(i, highs[i], lows[i], open_=highs[i], close=lows[i])
        for i in range(len(highs))
    ]


def test_detect_swings_finds_alternating_skeleton() -> None:
    from smc_prompt.structure_analyzer import detect_swings

    swings = detect_swings(_fractal_candles(), n=5, atr_value=None)

    assert [s.type for s in swings] == [
        SwingType.HIGH,
        SwingType.LOW,
        SwingType.HIGH,
    ]
    assert [s.price for s in swings] == [
        Decimal("100"),
        Decimal("50"),
        Decimal("100.3"),
    ]


def test_detect_swings_atr_merge_collapses_near_high() -> None:
    from smc_prompt.structure_analyzer import detect_swings

    # threshold = 0.5 * 2 = 1; |100.3 - 100| = 0.3 < 1, later is more extreme,
    # so the prior high is merged away (leaving LOW, HIGH).
    merged = detect_swings(
        _fractal_candles(),
        n=5,
        atr_value=Decimal("2"),
        merge_mult=Decimal("0.5"),
    )

    assert [s.type for s in merged] == [SwingType.LOW, SwingType.HIGH]
    assert merged[-1].price == Decimal("100.3")


def test_detect_swings_equal_highs_do_not_both_qualify() -> None:
    from smc_prompt.structure_analyzer import detect_swings

    # Three candles share the window maximum -> strict '>' means none is a high.
    highs = ["1", "2", "3", "3", "3", "2", "1"]
    lows = ["0.5", "1", "2", "2", "2", "1", "0.5"]
    candles = [
        make_candle(i, highs[i], lows[i], open_=highs[i], close=lows[i])
        for i in range(len(highs))
    ]

    swings = detect_swings(candles, n=5, atr_value=None)
    assert [s for s in swings if s.type is SwingType.HIGH] == []


@pytest.mark.parametrize("n", [2, 4, 6, 1, 0])
def test_detect_swings_rejects_even_or_small_window(n: int) -> None:
    from smc_prompt.structure_analyzer import detect_swings

    with pytest.raises(ValueError):
        detect_swings(_fractal_candles(), n=n)


# --------------------------------------------------------------------------
# enforce_alternation
# --------------------------------------------------------------------------


def test_enforce_alternation_collapses_runs_to_extreme() -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "105", SwingType.HIGH),
        make_swing(2, "50", SwingType.LOW),
        make_swing(3, "45", SwingType.LOW),
        make_swing(4, "110", SwingType.HIGH),
    ]

    result = enforce_alternation(swings)

    assert [s.type for s in result] == [
        SwingType.HIGH,
        SwingType.LOW,
        SwingType.HIGH,
    ]
    assert [s.price for s in result] == [
        Decimal("105"),
        Decimal("45"),
        Decimal("110"),
    ]


def test_enforce_alternation_keeps_first_on_tie() -> None:
    first = make_swing(0, "100", SwingType.HIGH)
    second = make_swing(5, "100", SwingType.HIGH)

    result = enforce_alternation([first, second])

    assert len(result) == 1
    assert result[0].index == 0  # first-seen wins on exact ties


# --------------------------------------------------------------------------
# classify_structure
# --------------------------------------------------------------------------


def test_classify_bullish_hh_hl(config: cfg.Config) -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "50", SwingType.LOW),
        make_swing(2, "105", SwingType.HIGH),
        make_swing(3, "60", SwingType.LOW),
    ]
    assert classify_structure(swings, config) is StructureClass.BULLISH


def test_classify_bearish_lh_ll(config: cfg.Config) -> None:
    swings = [
        make_swing(0, "105", SwingType.HIGH),
        make_swing(1, "60", SwingType.LOW),
        make_swing(2, "100", SwingType.HIGH),
        make_swing(3, "50", SwingType.LOW),
    ]
    assert classify_structure(swings, config) is StructureClass.BEARISH


def test_classify_mixed_is_ranging(config: cfg.Config) -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "50", SwingType.LOW),
        make_swing(2, "105", SwingType.HIGH),  # HH
        make_swing(3, "40", SwingType.LOW),  # LL -> contradictory
    ]
    assert classify_structure(swings, config) is StructureClass.RANGING


def test_classify_equal_levels(config: cfg.Config) -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "50", SwingType.LOW),
        make_swing(2, "100.5", SwingType.HIGH),
        make_swing(3, "50.2", SwingType.LOW),
    ]
    # tol = 0.1 * 10 = 1.0 -> both pairs within tolerance.
    result = classify_structure(swings, config, atr_value=Decimal("10"))
    assert result is StructureClass.EQUAL_LEVELS


def test_classify_equal_levels_requires_atr(config: cfg.Config) -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "50", SwingType.LOW),
        make_swing(2, "100.5", SwingType.HIGH),
        make_swing(3, "50.2", SwingType.LOW),
    ]
    # Without ATR the equal-levels fact cannot be asserted -> Bullish (HH+HL).
    assert classify_structure(swings, config) is StructureClass.BULLISH


def test_classify_fewer_than_two_highs_is_ranging(config: cfg.Config) -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "50", SwingType.LOW),
        make_swing(2, "45", SwingType.LOW),
        make_swing(3, "40", SwingType.LOW),
    ]
    assert classify_structure(swings, config) is StructureClass.RANGING


def test_classify_below_min_swings_is_ranging(config: cfg.Config) -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "50", SwingType.LOW),
        make_swing(2, "105", SwingType.HIGH),
    ]
    assert classify_structure(swings, config) is StructureClass.RANGING


# --------------------------------------------------------------------------
# fmt_distance (config + PriceFormat)
# --------------------------------------------------------------------------


def test_fmt_distance_positive_sign_and_magnitude() -> None:
    assert cfg.fmt_distance(Decimal("105"), Decimal("100")) == "+5.00% (+5.0000)"


def test_fmt_distance_negative_sign() -> None:
    assert cfg.fmt_distance(Decimal("95"), Decimal("100")) == "-5.00% (-5.0000)"


def test_fmt_distance_large_reference_two_decimals() -> None:
    assert (
        cfg.fmt_distance(Decimal("63120"), Decimal("68400"))
        == "-7.72% (-5280.00)"
    )


def test_fmt_distance_zero_is_positive() -> None:
    assert cfg.fmt_distance(Decimal("100"), Decimal("100")) == "+0.00% (+0.0000)"


def test_price_format_fmt_distance_uses_tick_decimals() -> None:
    pf = cfg.PriceFormat(2)
    assert pf.fmt_distance(Decimal("105"), Decimal("100")) == "+5.00% (+5.00)"


# --------------------------------------------------------------------------
# prepare_series / InsufficientDataError
# --------------------------------------------------------------------------


def _series(count: int, *, zero_tail: int = 0) -> list:
    """Build ``count`` hourly candles; the last ``zero_tail`` have volume 0."""

    return [
        make_candle(
            i,
            str(100 + i),
            str(90 + i),
            volume="0" if i >= count - zero_tail else "5",
        )
        for i in range(count)
    ]


def test_prepare_series_drops_half_open_and_keeps_requested() -> None:
    candles = _series(20) + [
        make_candle(20, "200", "190", is_closed=False, volume="5")
    ]

    closed, stats = prepare_series(
        candles, timeframe="1h", requested=10, swing_lookback=5, atr_period=14
    )

    assert len(closed) == 20  # half-open dropped
    assert stats.closed_count == 20
    assert stats.emitted_count == 10
    assert stats.requested_count == 10
    assert stats.was_reduced is False


def test_prepare_series_reduces_when_history_short() -> None:
    candles = _series(20)

    closed, stats = prepare_series(
        candles, timeframe="1h", requested=25, swing_lookback=5, atr_period=14
    )

    assert len(closed) == 20
    assert stats.emitted_count == 20
    assert stats.was_reduced is True


def test_prepare_series_zero_volume_streak() -> None:
    candles = _series(20, zero_tail=3)

    _, stats = prepare_series(
        candles, timeframe="1h", requested=10, swing_lookback=5, atr_period=14
    )

    assert stats.zero_volume_streak == 3


def test_prepare_series_raises_below_minimum() -> None:
    minimum = minimum_required(5, 14)  # max(10, 15, 5) == 15
    assert minimum == 15

    candles = _series(minimum - 1)
    with pytest.raises(InsufficientDataError):
        prepare_series(
            candles, timeframe="1h", requested=10, swing_lookback=5, atr_period=14
        )


def test_compute_atr_raises_without_enough_history() -> None:
    with pytest.raises(InsufficientDataError):
        compute_atr(_series(14), 14)


# --------------------------------------------------------------------------
# detect_equal_levels
# --------------------------------------------------------------------------


def test_detect_equal_levels_clusters_pool() -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "50", SwingType.LOW),
        make_swing(2, "100.4", SwingType.HIGH),
        make_swing(3, "50.3", SwingType.LOW),
        make_swing(4, "120", SwingType.HIGH),
    ]

    highs, lows = detect_equal_levels(swings, Decimal("1"))

    assert len(highs) == 1
    assert highs[0].count == 2
    assert highs[0].price == Decimal("100.4")  # most extreme representative
    assert len(lows) == 1
    assert lows[0].count == 2
    assert lows[0].price == Decimal("50")  # min representative for a LOW pool


def test_detect_equal_levels_empty_when_far_apart() -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "50", SwingType.LOW),
        make_swing(2, "130", SwingType.HIGH),
        make_swing(3, "20", SwingType.LOW),
    ]

    highs, lows = detect_equal_levels(swings, Decimal("1"))

    assert highs == ()
    assert lows == ()


def test_detect_equal_levels_singletons_not_emitted() -> None:
    swings = [
        make_swing(0, "100", SwingType.HIGH),
        make_swing(1, "50", SwingType.LOW),
    ]

    highs, lows = detect_equal_levels(swings, Decimal("1"))

    assert highs == ()
    assert lows == ()


# --------------------------------------------------------------------------
# detect_fvg
# --------------------------------------------------------------------------


def _gap_candles() -> list:
    return [
        make_candle(0, "100", "95", open_="98", close="96"),
        make_candle(1, "101", "96", open_="96", close="100"),
        make_candle(2, "104", "102", open_="102", close="103"),
    ]


def test_detect_fvg_bullish_bounds_and_birth() -> None:
    candles = _gap_candles()

    fvgs = detect_fvg(candles, Decimal("0.1"), atr_value=None)

    assert len(fvgs) == 1
    fvg = fvgs[0]
    assert fvg.direction is FVGDirection.BULLISH
    assert fvg.lower == Decimal("100")  # high[i-2]
    assert fvg.upper == Decimal("102")  # low[i]
    assert fvg.birth_time == candles[2].open_time
    assert fvg.filled is False
    assert fvg.index == 2


def test_detect_fvg_filled_when_later_candle_trades_through() -> None:
    candles = _gap_candles() + [
        make_candle(3, "103", "99", open_="102", close="100"),
    ]

    fvgs = detect_fvg(candles, Decimal("0.1"), atr_value=None)

    assert fvgs[0].filled is True


def test_detect_fvg_bearish_bounds() -> None:
    candles = [
        make_candle(0, "105", "95", open_="100", close="96"),
        make_candle(1, "100", "94", open_="96", close="97"),
        make_candle(2, "93", "90", open_="92", close="91"),
    ]

    fvgs = detect_fvg(candles, Decimal("0.1"), atr_value=None)

    assert len(fvgs) == 1
    fvg = fvgs[0]
    assert fvg.direction is FVGDirection.BEARISH
    assert fvg.lower == Decimal("93")  # high[i]
    assert fvg.upper == Decimal("95")  # low[i-2]


def test_detect_fvg_sub_noise_filtered() -> None:
    candles = _gap_candles()

    # threshold = 0.1 * 100 = 10 > gap (2) -> discarded as sub-noise.
    fvgs = detect_fvg(candles, Decimal("0.1"), atr_value=Decimal("100"))

    assert fvgs == []


def test_detect_fvg_requires_three_candles() -> None:
    assert detect_fvg(_gap_candles()[:2], Decimal("0.1"), atr_value=None) == []


# --------------------------------------------------------------------------
# tickSize -> decimals derivation (Phase 3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tick", "expected"),
    [
        ("0.01000000", 2),
        ("0.00000100", 6),
        ("1.00000000", 0),
        ("0.1", 1),
        ("0.000000001", 8),  # capped at MAX_PRICE_DECIMALS
        (None, None),
        ("0", None),
        ("0.00000000", None),
        ("not-a-number", None),
    ],
)
def test_decimals_from_tick_size(tick: object, expected: int | None) -> None:
    assert cfg.decimals_from_tick_size(tick) == expected


# --------------------------------------------------------------------------
# Dealing Range & Structure / Equal-Levels Active Filtering (Layer-A Bug Fixes)
# --------------------------------------------------------------------------


def test_classify_structure_downgrades_bullish_when_key_hl_is_broken() -> None:
    from dataclasses import replace

    config = cfg.Config(
        symbol="BTCUSDT",
        htf_candles=60,
        mtf_candles=120,
        ltf_candles=100,
        swing_lookback=5,
        distance_reference="nearest",
        include_atr=True,
        output_dir="output",
    )
    swings = [
        make_swing(0, "76000", SwingType.LOW),
        make_swing(1, "78000", SwingType.HIGH),
        replace(make_swing(2, "77561.9", SwingType.LOW), status="broken"),
        make_swing(3, "78853.5", SwingType.HIGH),
    ]

    assert classify_structure(swings, config, atr_value=Decimal("200")) is StructureClass.RANGING


def test_classify_structure_downgrades_bearish_when_key_lh_is_broken() -> None:
    from dataclasses import replace

    config = cfg.Config(
        symbol="BTCUSDT",
        htf_candles=60,
        mtf_candles=120,
        ltf_candles=100,
        swing_lookback=5,
        distance_reference="nearest",
        include_atr=True,
        output_dir="output",
    )
    swings = [
        make_swing(0, "80000", SwingType.HIGH),
        make_swing(1, "78000", SwingType.LOW),
        replace(make_swing(2, "79000", SwingType.HIGH), status="broken"),
        make_swing(3, "77000", SwingType.LOW),
    ]

    assert classify_structure(swings, config, atr_value=Decimal("200")) is StructureClass.RANGING


def test_select_dealing_range_bounds_skips_broken_low_above_current_price() -> None:
    from dataclasses import replace

    extreme_low = replace(make_swing(0, "76270.0", SwingType.LOW), status="untested")
    mid_high = replace(make_swing(1, "78000.0", SwingType.HIGH), status="broken")
    recent_low = replace(make_swing(2, "77561.9", SwingType.LOW), status="broken")
    recent_high = replace(make_swing(3, "78853.5", SwingType.HIGH), status="untested")
    swings = [extreme_low, mid_high, recent_low, recent_high]

    current_price = Decimal("76800.0")
    range_low, range_high = select_dealing_range_bounds(
        swings, current_price, recent_low, recent_high
    )

    assert range_low.price == Decimal("76270.0")
    assert range_high.price == Decimal("78853.5")

    summary = format_dealing_range_summary(
        current_price, range_low, range_high, price_format=cfg.PriceFormat(tick_decimals=1)
    )
    assert "76270.0–78853.5" in summary
    assert "EQ (50%): 77561.8 (posisi harga: DISCOUNT)" in summary


def test_detect_equal_levels_ignores_broken_swings_and_wrong_side_pools() -> None:
    from dataclasses import replace

    # Two equal lows at 77561.9 that were already broken, with current_price at 76800.0
    swings_broken = [
        replace(make_swing(0, "77561.9", SwingType.LOW), status="broken"),
        make_swing(1, "78500.0", SwingType.HIGH),
        replace(make_swing(2, "77562.0", SwingType.LOW), status="broken"),
    ]
    highs, lows = detect_equal_levels(
        swings_broken, Decimal("5"), current_price=Decimal("76800.0")
    )
    assert highs == ()
    assert lows == ()

    # Even if status were untested, Equal Lows above current_price (76800) must be filtered out
    swings_wrong_side = [
        make_swing(0, "77561.9", SwingType.LOW),
        make_swing(1, "78500.0", SwingType.HIGH),
        make_swing(2, "77562.0", SwingType.LOW),
        make_swing(3, "76500.0", SwingType.HIGH),
        make_swing(4, "76501.0", SwingType.HIGH),
    ]
    highs_ws, lows_ws = detect_equal_levels(
        swings_wrong_side, Decimal("5"), current_price=Decimal("76800.0")
    )
    assert highs_ws == ()
    assert lows_ws == ()

