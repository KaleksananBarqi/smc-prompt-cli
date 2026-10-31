"""Unit tests for token compression and prompt refactoring features."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from smc_prompt import cli
from smc_prompt import config as cfg
from smc_prompt.models import (
    Candle,
    FVG,
    FVGDirection,
    StructureClass,
    SwingPoint,
    SwingType,
    TimeframeAnalysis,
)
from smc_prompt.structure_analyzer import (
    _fvg_mitigation,
    detect_fvg,
    format_ref_summary,
)
from smc_prompt.template_renderer import (
    _render_fvg_table,
    _render_swings,
    build_payload,
    render,
    render_csv_table,
)
from .conftest import HTF_CSV, LTF_CSV, MTF_CSV


def _make_candle(
    index: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "100.55",
    open_time: datetime | None = None,
) -> Candle:
    t = open_time or datetime(2026, 10, 1 + index, 0, 0, tzinfo=timezone.utc)
    return Candle(
        open_time=t,
        close_time=t,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        is_closed=True,
    )


def test_fmt_volume_rounding_integer() -> None:
    assert cfg.fmt_volume("36657.288") == "36657"
    assert cfg.fmt_volume("100.00") == "100"
    assert cfg.fmt_volume("0.000") == "0"
    assert cfg.fmt_volume("99.5") == "100"
    assert cfg.fmt_volume("99.49") == "99"


def test_compact_timestamp_formatting() -> None:
    dt = datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc)
    assert cfg.fmt_htf_date(dt) == "10-29"
    assert cfg.fmt_ltf_datetime(dt) == "10-29 14:30"


def test_short_labels_in_swings_and_fvg() -> None:
    dt = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
    swings = [
        SwingPoint(index=0, open_time=dt, price=Decimal("100"), type=SwingType.HIGH),
        SwingPoint(index=1, open_time=dt, price=Decimal("90"), type=SwingType.LOW),
    ]
    pf = cfg.PriceFormat(tick_decimals=2)
    rendered_swings = _render_swings(swings, htf=True, limit=5, price_format=pf)
    assert "10-29,SH,100.00,-,untested" in rendered_swings
    assert "10-29,SL,90.00,-,untested" in rendered_swings

    fvgs = [
        FVG(
            direction=FVGDirection.BULLISH,
            lower=Decimal("95"),
            upper=Decimal("98"),
            birth_time=dt,
            filled=False,
            index=2,
            mitigated_index=None,
        ),
        FVG(
            direction=FVGDirection.BEARISH,
            lower=Decimal("80"),
            upper=Decimal("85"),
            birth_time=dt,
            filled=True,
            index=3,
            mitigated_index=4,
        ),
    ]
    # Total candles = 5 -> mitigated_index 4 is in the last 3 candles (4 >= 5 - 3 = 2)
    rendered_fvg = _render_fvg_table(fvgs, htf=True, limit=5, total_candles=5, price_format=pf)
    assert "10-29,BULL,95.00,98.00,unfilled" in rendered_fvg
    assert "10-29,BEAR,80.00,85.00,filled" in rendered_fvg

    # If total candles = 10 -> mitigated_index 4 is old (4 < 10 - 3 = 7), so filled is filtered out
    rendered_filtered = _render_fvg_table(fvgs, htf=True, limit=5, total_candles=10, price_format=pf)
    assert "BULL" in rendered_filtered
    assert "BEAR" not in rendered_filtered


def test_ref_summary_deduplication() -> None:
    dt1 = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
    dt2 = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
    candles = [
        _make_candle(0, "80000", "85000", "75000", "80000", open_time=dt2),
        _make_candle(1, "80000", "82000", "79000", "81000", open_time=dt1),
    ]

    recent_h = SwingPoint(index=1, open_time=dt1, price=Decimal("81953.7"), type=SwingType.HIGH)
    nearest_h = SwingPoint(index=1, open_time=dt1, price=Decimal("81953.7"), type=SwingType.HIGH)
    extreme_h = SwingPoint(index=0, open_time=dt2, price=Decimal("84875.2"), type=SwingType.HIGH)

    pf = cfg.PriceFormat(tick_decimals=1)
    summary_h = format_ref_summary(
        Decimal("77543.0"),
        recent_h,
        nearest_h,
        extreme_h,
        SwingType.HIGH,
        candles,
        htf=True,
        atr=Decimal("1170.0"),
        price_format=pf,
    )
    assert summary_h.startswith("- Ref HIGH: Recent/Nearest=81953.7 (10-29, -5.38%, 3.77xATR, untested) | Extreme=84875.2 (10-21, untested)")

    # All three identical
    summary_all = format_ref_summary(
        Decimal("77543.0"),
        recent_h,
        nearest_h,
        recent_h,
        SwingType.HIGH,
        candles,
        htf=True,
        atr=Decimal("1170.0"),
        price_format=pf,
    )
    assert summary_all.startswith("- Ref HIGH: Recent/Nearest/Extreme=81953.7 (10-29, -5.38%, 3.77xATR, untested)")


def test_cli_full_candles_flag(tmp_path: Path) -> None:
    # Default without --full-candles: tail limit 20 HTF, 30 MTF, 40 LTF
    prompt_tail = cli.run(
        "BTCUSDT",
        htf_candles=60,
        mtf_candles=120,
        ltf_candles=100,
        output_dir=str(tmp_path / "tail"),
        input_csv=str(HTF_CSV),
        htf_file=str(HTF_CSV),
        mtf_file=str(MTF_CSV),
        ltf_file=str(LTF_CSV),
    ).prompt

    htf_rows = prompt_tail.split("### Data Candle Mentah HTF")[1].split("```")[1].strip().splitlines()
    mtf_rows = prompt_tail.split("### Data Candle Mentah MTF")[1].split("```")[1].strip().splitlines()
    ltf_rows = prompt_tail.split("### Data Candle Mentah LTF")[1].split("```")[1].strip().splitlines()

    assert len(htf_rows) == 20
    assert len(mtf_rows) == 30
    assert len(ltf_rows) == 40

    # With full_candles=True: full window (60, 120, 100)
    prompt_full = cli.run(
        "BTCUSDT",
        htf_candles=60,
        mtf_candles=120,
        ltf_candles=100,
        full_candles=True,
        output_dir=str(tmp_path / "full"),
        input_csv=str(HTF_CSV),
        htf_file=str(HTF_CSV),
        mtf_file=str(MTF_CSV),
        ltf_file=str(LTF_CSV),
    ).prompt

    htf_full_rows = prompt_full.split("### Data Candle Mentah HTF")[1].split("```")[1].strip().splitlines()
    mtf_full_rows = prompt_full.split("### Data Candle Mentah MTF")[1].split("```")[1].strip().splitlines()
    ltf_full_rows = prompt_full.split("### Data Candle Mentah LTF")[1].split("```")[1].strip().splitlines()

    assert len(htf_full_rows) == 60
    assert len(mtf_full_rows) == 120
    assert len(ltf_full_rows) == 100


def test_compute_level_status_sweep_vs_bos() -> None:
    from smc_prompt.structure_analyzer import compute_level_status

    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 2, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 3, tzinfo=timezone.utc)
    t3 = datetime(2026, 10, 4, tzinfo=timezone.utc)

    # 1. Untested: level not touched
    candles_untested = [
        _make_candle(0, "100", "105", "95", "102", open_time=t0),
        _make_candle(1, "102", "104", "98", "101", open_time=t1),
    ]
    high_level = SwingPoint(index=0, open_time=t0, price=Decimal("110"), type=SwingType.HIGH)
    assert compute_level_status(candles_untested, high_level, SwingType.HIGH) == "untested"

    # 2. Wick sweep: high breached level but candle closed below
    candles_wick_sweep = [
        _make_candle(0, "100", "110", "95", "105", open_time=t0),
        _make_candle(1, "105", "112", "100", "108", open_time=t1),  # high 112 > 110, close 108 <= 110
    ]
    assert compute_level_status(candles_wick_sweep, high_level, SwingType.HIGH) == "wick_sweep"

    # 3. Reclaim sweep: 1 candle closed outside (> 110), but immediate next closed back inside (<= 110)
    candles_reclaim_sweep = [
        _make_candle(0, "100", "110", "95", "105", open_time=t0),
        _make_candle(1, "105", "115", "104", "112", open_time=t1),  # close 112 > 110 (1 close)
        _make_candle(2, "111", "111", "102", "105", open_time=t2),  # immediately back <= 110
    ]
    assert compute_level_status(candles_reclaim_sweep, high_level, SwingType.HIGH) == "reclaim_sweep"

    # 4. Pending break: only 1 candle closed outside so far, but no reclaim yet
    candles_pending_break = [
        _make_candle(0, "100", "110", "95", "105", open_time=t0),
        _make_candle(1, "105", "115", "104", "112", open_time=t1),  # close 112 > 110
    ]
    assert compute_level_status(candles_pending_break, high_level, SwingType.HIGH) == "pending_break"

    # 5. Broken (BOS): 2 or more consecutive closes outside
    candles_broken = [
        _make_candle(0, "100", "110", "95", "105", open_time=t0),
        _make_candle(1, "105", "115", "104", "112", open_time=t1),  # close 1 > 110
        _make_candle(2, "112", "116", "111", "114", open_time=t2),  # close 2 > 110
    ]
    assert compute_level_status(candles_broken, high_level, SwingType.HIGH) == "broken"


def test_order_block_detection_and_mitigation() -> None:
    from smc_prompt.structure_analyzer import detect_fvg, detect_order_blocks
    from smc_prompt.template_renderer import _render_ob_table

    # Construct candles forming a Bullish FVG:
    # Candle 0: Bearish candle (base) [open=100, high=102, low=95, close=96]
    # Candle 1: Displacement start [open=96, high=108, low=96, close=107]
    # Candle 2: Gap candle [open=107, high=125, low=112, close=124] (low=112 > high_0=102 -> Bullish FVG [102, 112])
    # Candle 3: Retest candle entering zone [open=124, high=124, low=98, close=105] (low enters base zone 95..102, close 105 > 95 -> tapped)
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 2, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 3, tzinfo=timezone.utc)
    t3 = datetime(2026, 10, 4, tzinfo=timezone.utc)

    candles = [
        _make_candle(0, "100", "102", "95", "96", open_time=t0),
        _make_candle(1, "96", "108", "96", "107", open_time=t1),
        _make_candle(2, "107", "125", "112", "124", open_time=t2),
    ]
    # min_gap_atr_mult=0.1, atr_value=Decimal("10") -> min gap size = 1.0 (gap here is 112-102 = 10.0 >= 1.0)
    fvgs = detect_fvg(candles, Decimal("0.1"), atr_value=Decimal("10"))
    assert len(fvgs) == 1
    assert fvgs[0].direction is FVGDirection.BULLISH

    # Detect OB from initial 3 candles
    obs = detect_order_blocks(candles, fvgs)
    assert len(obs) == 1
    ob = obs[0]
    assert ob.direction is FVGDirection.BULLISH
    assert ob.lower == Decimal("95")
    assert ob.upper == Decimal("102")
    assert ob.mt == Decimal("98.5")  # (95 + 102) / 2
    assert ob.status == "unmitigated"

    # Add candle 3 that taps the OB (low 98 <= upper 102, close 105 >= lower 95)
    candles.append(_make_candle(3, "124", "124", "98", "105", open_time=t3))
    obs_tapped = detect_order_blocks(candles, fvgs)
    assert obs_tapped[0].status == "tapped"

    # Add candle 4 that invalidates OB (close 90 < lower 95)
    t4 = datetime(2026, 10, 5, tzinfo=timezone.utc)
    candles.append(_make_candle(4, "105", "105", "89", "90", open_time=t4))
    obs_invalidated = detect_order_blocks(candles, fvgs)
    assert obs_invalidated[0].status == "invalidated"
    assert obs_invalidated[0].invalidated_index == 4

    # Check rendering filter: since candle 4 is at index 4 and total candles=5 (4 >= 5-3),
    # it is still recent and rendered as invalidated
    pf = cfg.PriceFormat(tick_decimals=2)
    rendered = _render_ob_table(obs_invalidated, htf=True, total_candles=5, price_format=pf)
    assert "10-01,BULL_OB,95.00,102.00,98.50,invalidated" in rendered

    # If total candles=10, old invalidated is excluded
    rendered_filtered = _render_ob_table(obs_invalidated, htf=True, total_candles=10, price_format=pf)
    assert rendered_filtered == "NONE"


def test_order_block_in_prompt_template(tmp_path: Path) -> None:
    # Verify that the generated prompt includes the Order Block sections
    prompt = cli.run(
        "BTCUSDT",
        htf_candles=60,
        mtf_candles=120,
        ltf_candles=100,
        output_dir=str(tmp_path),
        input_csv=str(HTF_CSV),
        htf_file=str(HTF_CSV),
        mtf_file=str(MTF_CSV),
        ltf_file=str(LTF_CSV),
    ).prompt

    assert "### Order Block Aktif HTF" in prompt
    assert "### Order Block Aktif MTF" in prompt
    assert "### Order Block Aktif LTF" in prompt
    assert "Order Block Aktif:" in prompt

