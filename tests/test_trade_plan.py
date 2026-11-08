"""Unit tests for TradePlan, build_trade_plan, and compute_excursion."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from smc_prompt.errors import ConfigError
from smc_prompt.models import Candle
from smc_prompt.trade_plan import TradePlan, build_trade_plan, compute_excursion


def _candle(open_t: str, o: str, h: str, l: str, c: str) -> Candle:
    dt = datetime.fromisoformat(open_t).replace(tzinfo=timezone.utc)
    return Candle(
        open_time=dt,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        volume=Decimal("100"),
        close_time=dt,
        is_closed=True,
    )


def test_build_trade_plan_single_entry_auto_direction():
    plan_long = build_trade_plan(entry="100", tp="120", sl="90")
    assert plan_long.direction == "long"
    assert plan_long.entry1 == Decimal("100")
    assert plan_long.entry2 is None
    assert plan_long.blended_entry == Decimal("100")
    assert plan_long.risk_1r_blended == Decimal("10")
    assert plan_long.rr_tp1() == Decimal("2.00")

    plan_short = build_trade_plan(entry="100", tp="80", sl="110")
    assert plan_short.direction == "short"
    assert plan_short.entry1 == Decimal("100")
    assert plan_short.blended_entry == Decimal("100")
    assert plan_short.risk_1r_blended == Decimal("10")
    assert plan_short.rr_tp1() == Decimal("2.00")


def test_build_trade_plan_dual_entry_long():
    plan = build_trade_plan(
        direction="long",
        entry1="100",
        entry2="95",
        sl="90",
        tp1="110",
        tp2="120",
    )
    assert plan.is_dual_entry
    assert plan.has_tp2
    # 0.4*100 + 0.6*95 = 40 + 57 = 97
    assert plan.blended_entry == Decimal("97.0")
    assert plan.risk_1r_blended == Decimal("7.0")
    assert plan.risk_1r_e1 == Decimal("10.0")
    # rr_tp1: reward = 110 - 97 = 13; risk = 7; 13/7 = 1.86
    assert plan.rr_tp1() == Decimal("1.86")
    # rr_tp2: reward = 120 - 97 = 23; risk = 7; 23/7 = 3.29
    assert plan.rr_tp2() == Decimal("3.29")
    # rr_tp1 from entry1: reward = 110-100 = 10; risk = 100-90 = 10; 1.00
    assert plan.rr_tp1(from_entry=plan.entry1) == Decimal("1.00")


def test_build_trade_plan_dual_entry_short():
    plan = build_trade_plan(
        direction="short",
        entry1="100",
        entry2="105",
        sl="110",
        tp1="90",
        tp2="80",
    )
    assert plan.is_dual_entry
    assert plan.has_tp2
    # 0.4*100 + 0.6*105 = 40 + 63 = 103
    assert plan.blended_entry == Decimal("103.0")
    assert plan.risk_1r_blended == Decimal("7.0")
    assert plan.risk_1r_e1 == Decimal("10.0")


def test_build_trade_plan_validation_guards():
    with pytest.raises(ConfigError, match="minimal Entry 1 dan TP1"):
        build_trade_plan(entry="100")

    with pytest.raises(ConfigError, match="--tp cannot be equal to --entry"):
        build_trade_plan(entry="100", tp="100")

    with pytest.raises(ConfigError, match="Invalid LONG setup: TP"):
        build_trade_plan(direction="long", entry="100", tp="90")

    with pytest.raises(ConfigError, match="For long setups, --sl must be lower than --entry"):
        build_trade_plan(direction="long", entry="100", tp="120", sl="105")

    with pytest.raises(ConfigError, match="Entry 2.*harus lebih rendah atau sama"):
        build_trade_plan(direction="long", entry1="100", entry2="105", tp1="120")

    with pytest.raises(ConfigError, match="TP2.*harus lebih tinggi"):
        build_trade_plan(direction="long", entry1="100", tp1="120", tp2="115")


def test_compute_excursion_long_filled():
    plan = build_trade_plan(
        direction="long",
        entry1="100",
        entry2="95",
        sl="90",
        tp1="110",
        tp2="120",
    )
    candles = [
        _candle("2026-11-06T08:00:00", "102", "103", "99", "100"),  # E1 hit (low 99 <= 100)
        _candle("2026-11-06T09:00:00", "100", "101", "94", "96"),   # E2 hit (low 94 <= 95)
        _candle("2026-11-06T10:00:00", "96", "112", "95", "111"),   # TP1 hit (high 112 >= 110)
    ]

    res = compute_excursion(plan, candles, order_status="filled")
    assert res.e1_filled is True
    assert res.e2_filled is True
    assert res.mfe_price == Decimal("112")
    assert res.mae_price == Decimal("94")
    assert res.tp1_touched is True
    assert res.tp2_touched is False
    assert res.sl_touched is False
    assert res.first_touch_sequence == ("tp1",)
    assert res.first_fill_killzone is not None
    assert res.active_entry == Decimal("97.0")
    assert res.filled_legs == "both"


def test_compute_excursion_dual_entry_auto_detects_single_leg_e1():
    """Saat order_status filled pada Dual-Entry namun hanya E1 yang tersentuh candle,
    active_entry otomatis menggunakan E1 (bukan Blended Entry)."""
    plan = build_trade_plan(
        direction="long",
        entry1="100",
        entry2="95",
        sl="90",
        tp1="110",
        tp2="120",
    )
    candles = [
        _candle("2026-11-06T08:00:00", "102", "103", "99", "101"),  # E1 hit (low 99 <= 100), E2 (95) untouched
        _candle("2026-11-06T09:00:00", "101", "112", "100", "111"),  # TP1 hit (high 112 >= 110)
    ]

    res = compute_excursion(plan, candles, order_status="filled")
    assert res.e1_filled is True
    assert res.e2_filled is False
    assert res.filled_legs == "e1"
    assert res.active_entry == Decimal("100")
    # MFE points dari E1 (100) ke 112 = 12; risk E1 = 100 - 90 = 10 -> +1.20R
    assert res.mfe_points == Decimal("12")
    assert res.mfe_r == Decimal("1.20")
    # MAE points dari E1 (100) ke 99 = 1; risk E1 = 10 -> -0.10R
    assert res.mae_points == Decimal("1")
    assert res.mae_r == Decimal("0.10")


def test_trade_plan_realized_pnl_and_r_multiple():
    plan_long = build_trade_plan(
        direction="long",
        entry1="100",
        entry2="95",
        sl="90",
        tp1="110",
    )
    # Blended entry = 97, risk = 7
    assert plan_long.realized_pnl_points(Decimal("111")) == Decimal("14.0")
    assert plan_long.realized_r_multiple(Decimal("111")) == Decimal("2.00")
    # Dari E1 saja (100), risk = 10
    assert plan_long.realized_pnl_points(Decimal("115"), from_entry=plan_long.entry1) == Decimal("15")
    assert plan_long.realized_r_multiple(Decimal("115"), from_entry=plan_long.entry1) == Decimal("1.50")

    plan_short = build_trade_plan(
        direction="short",
        entry1="100",
        sl="110",
        tp1="80",
    )
    assert plan_short.realized_pnl_points(Decimal("85")) == Decimal("15")
    assert plan_short.realized_r_multiple(Decimal("85")) == Decimal("1.50")
    assert plan_short.realized_pnl_points(Decimal("110")) == Decimal("-10")
    assert plan_short.realized_r_multiple(Decimal("110")) == Decimal("-1.00")
