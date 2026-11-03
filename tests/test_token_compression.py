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


@pytest.mark.parametrize(
    "interval",
    ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h"],
)
def test_is_intraday_interval_true_for_sub_daily(interval: str) -> None:
    assert cfg.is_intraday_interval(interval) is True


@pytest.mark.parametrize(
    "interval",
    ["1d", "3d", "7d", "30d", "1w", "1M"],
)
def test_is_intraday_interval_false_for_daily_and_above(interval: str) -> None:
    assert cfg.is_intraday_interval(interval) is False


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, "Asia Session (00:00-07:00 UTC)"),
        (6, "Asia Session (00:00-07:00 UTC)"),
        (7, "London Open Killzone (07:00-10:00 UTC)"),
        (9, "London Open Killzone (07:00-10:00 UTC)"),
        (10, "London Session / Pre-NY (10:00-12:00 UTC)"),
        (11, "London Session / Pre-NY (10:00-12:00 UTC)"),
        (12, "New York Open Killzone (12:00-15:00 UTC)"),
        (14, "New York Open Killzone (12:00-15:00 UTC)"),
        (15, "London Close / NY Afternoon (15:00-20:00 UTC)"),
        (19, "London Close / NY Afternoon (15:00-20:00 UTC)"),
        (20, "Late NY / Off-Session (20:00-00:00 UTC)"),
        (23, "Late NY / Off-Session (20:00-00:00 UTC)"),
    ],
)
def test_fmt_session_killzone_all_six_windows(hour: int, expected: str) -> None:
    dt = datetime(2026, 11, 3, hour, 45, tzinfo=timezone.utc)
    assert cfg.fmt_session_killzone(dt) == expected


@pytest.mark.parametrize(
    ("current_price", "expected_zone"),
    [
        (Decimal("94.99"), "DISCOUNT"),
        (Decimal("95.00"), "AT EQ"),
        (Decimal("95.01"), "PREMIUM"),
    ],
)
def test_dealing_range_and_equilibrium_zones(
    current_price: Decimal, expected_zone: str
) -> None:
    from smc_prompt.structure_analyzer import (
        build_reference_facts,
        format_dealing_range_summary,
    )

    dt_l = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    dt_h = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
    recent_low = SwingPoint(index=0, open_time=dt_l, price=Decimal("90.00"), type=SwingType.LOW)
    recent_high = SwingPoint(index=1, open_time=dt_h, price=Decimal("100.00"), type=SwingType.HIGH)
    pf = cfg.PriceFormat(tick_decimals=2)

    expected_line = (
        f"- Dealing Range (Recent): 90.00–100.00 | EQ (50%): 95.00 (posisi harga: {expected_zone})"
    )
    assert (
        format_dealing_range_summary(
            current_price, recent_low, recent_high, price_format=pf
        )
        == expected_line
    )

    candles = [
        _make_candle(0, "92", "95", "90", "93", open_time=dt_l),
        _make_candle(1, "93", "100", "91", "98", open_time=dt_h),
    ]
    facts = build_reference_facts(
        current_price,
        htf=True,
        candles=candles,
        swings=(recent_low, recent_high),
        rendered_swings=(recent_low, recent_high),
        recent_high=recent_high,
        recent_low=recent_low,
        price_format=pf,
    )
    assert facts.dealing_range_summary == expected_line


def test_killzone_and_dealing_range_in_payload_and_rendered_prompt() -> None:
    from smc_prompt.models import REQUIRED_PLACEHOLDERS
    from smc_prompt.structure_analyzer import analyze

    for key in (
        "SESSION_KILLZONE_UTC",
        "HTF_DEALING_RANGE",
        "MTF_DEALING_RANGE",
        "LTF_DEALING_RANGE",
    ):
        assert key in REQUIRED_PLACEHOLDERS

    candles = _make_smc_series()
    config = cfg.build_config(
        "BTCUSDT",
        htf_interval="4h",
        mtf_interval="1h",
        ltf_interval="15m",
        price_format=cfg.PriceFormat(tick_decimals=2),
    )
    current_price = Decimal("146.00")

    htf_analysis, htf_emitted, _ = analyze(
        candles,
        timeframe=config.htf_interval,
        requested=config.htf_candles,
        current_price=current_price,
        config=config,
    )
    mtf_analysis, mtf_emitted, _ = analyze(
        candles,
        timeframe=config.mtf_interval,
        requested=config.mtf_candles,
        current_price=current_price,
        config=config,
    )
    ltf_analysis, ltf_emitted, _ = analyze(
        candles,
        timeframe=config.ltf_interval,
        requested=config.ltf_candles,
        current_price=current_price,
        config=config,
    )

    payload = build_payload(
        symbol="BTCUSDT",
        generated_at=datetime(2026, 10, 5, 8, 15, tzinfo=timezone.utc),
        current_price=current_price,
        htf=htf_analysis,
        mtf=mtf_analysis,
        ltf=ltf_analysis,
        htf_candles=htf_emitted,
        mtf_candles=mtf_emitted,
        ltf_candles=ltf_emitted,
        config=config,
    )

    assert payload["SESSION_KILLZONE_UTC"] == "London Open Killzone (07:00-10:00 UTC)"
    for tf_key in ("HTF_DEALING_RANGE", "MTF_DEALING_RANGE", "LTF_DEALING_RANGE"):
        assert payload[tf_key].startswith("- Dealing Range (Recent): ")
        assert " | EQ (50%): " in payload[tf_key]
        assert "(posisi harga: PREMIUM)" in payload[tf_key]

    rendered = render(payload, include_atr=True).text
    assert (
        "- **Waktu generate (UTC):** 2026-10-05T08:15:00Z\n"
        "- **Sesi Pasar (UTC):** London Open Killzone (07:00-10:00 UTC)\n"
    ) in rendered
    assert f"{payload['HTF_REF_LOW_SUMMARY']}\n{payload['HTF_DEALING_RANGE']}\n" in rendered
    assert f"{payload['MTF_REF_LOW_SUMMARY']}\n{payload['MTF_DEALING_RANGE']}\n" in rendered
    assert f"{payload['LTF_REF_LOW_SUMMARY']}\n{payload['LTF_DEALING_RANGE']}\n" in rendered





def _make_smc_series() -> list[Candle]:
    """Susun 20 candle 4h tertutup lengkap dengan fractal swings, FVG unfilled, dan OB aktif."""
    from datetime import timedelta

    base = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
    specs = [
        # (open, high, low, close)
        ("100", "102", "98", "101"),
        ("101", "105", "99", "104"),
        ("104", "120", "103", "110"),  # Swing HIGH @ i=2
        ("110", "106", "100", "102"),
        ("102", "104", "99", "100"),
        ("100", "103", "80", "85"),    # Swing LOW @ i=5
        ("85", "95", "90", "94"),
        ("94", "98", "92", "96"),
        ("96", "130", "95", "125"),    # Swing HIGH @ i=8
        ("125", "122", "110", "112"),
        ("112", "115", "108", "110"),
        ("110", "112", "85", "90"),    # Swing LOW @ i=11
        ("90", "100", "92", "98"),
        ("98", "102", "94", "99"),
        ("99", "103", "95", "100"),
        ("100", "104", "96", "101"),
        ("102", "104", "95", "96"),    # i=16: candle bearish basis untuk OB
        ("96", "135", "96", "134"),    # i=17: candle displacement
        ("134", "145", "120", "142"),  # i=18: candle gap -> Bullish FVG [104, 120]
        ("142", "148", "138", "146"),  # i=19: bertahan di atas FVG & OB
    ]
    candles: list[Candle] = []
    for i, (o, h, l, c) in enumerate(specs):
        open_t = base + timedelta(hours=4 * i)
        candles.append(
            Candle(
                open_time=open_t,
                close_time=open_t + timedelta(hours=4),
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(l),
                close=Decimal(c),
                volume=Decimal("100"),
                is_closed=True,
            )
        )
    return candles


def test_intraday_4h_htf_renders_datetime_stamps_across_all_sections() -> None:
    import re
    from smc_prompt.structure_analyzer import analyze

    candles = _make_smc_series()
    config = cfg.build_config(
        "BTCUSDT",
        htf_interval="4h",
        mtf_interval="1h",
        ltf_interval="15m",
        price_format=cfg.PriceFormat(tick_decimals=2),
    )
    current_price = Decimal("146.00")

    htf_analysis, htf_emitted, _ = analyze(
        candles,
        timeframe=config.htf_interval,
        requested=config.htf_candles,
        current_price=current_price,
        config=config,
    )
    mtf_analysis, mtf_emitted, _ = analyze(
        candles,
        timeframe=config.mtf_interval,
        requested=config.mtf_candles,
        current_price=current_price,
        config=config,
    )
    ltf_analysis, ltf_emitted, _ = analyze(
        candles,
        timeframe=config.ltf_interval,
        requested=config.ltf_candles,
        current_price=current_price,
        config=config,
    )

    payload = build_payload(
        symbol="BTCUSDT",
        generated_at=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        current_price=current_price,
        htf=htf_analysis,
        mtf=mtf_analysis,
        ltf=ltf_analysis,
        htf_candles=htf_emitted,
        mtf_candles=mtf_emitted,
        ltf_candles=ltf_emitted,
        config=config,
    )

    dt_row_pattern = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2},")
    ref_dt_pattern = re.compile(r"\(\d{2}-\d{2} \d{2}:\d{2},")

    # 1. Ringkasan Reference High & Low wajib memuat MM-DD HH:MM
    assert ref_dt_pattern.search(payload["HTF_REF_HIGH_SUMMARY"])
    assert ref_dt_pattern.search(payload["HTF_REF_LOW_SUMMARY"])

    # 2. Baris tabel HTF Swings wajib diawali MM-DD HH:MM
    swing_rows = payload["HTF_SWINGS_TABLE"].splitlines()
    assert len(swing_rows) >= 2
    assert all(dt_row_pattern.match(row) for row in swing_rows)

    # 3. Baris tabel HTF FVG wajib diawali MM-DD HH:MM
    fvg_rows = payload["HTF_FVG_TABLE"].splitlines()
    assert fvg_rows and fvg_rows != ["NONE"]
    assert all(dt_row_pattern.match(row) for row in fvg_rows)

    # 4. Baris tabel HTF OB wajib diawali MM-DD HH:MM
    ob_rows = payload["HTF_OB_TABLE"].splitlines()
    assert ob_rows and ob_rows != ["NONE"]
    assert all(dt_row_pattern.match(row) for row in ob_rows)

    # 5. Baris CSV candle mentah HTF wajib diawali MM-DD HH:MM
    csv_rows = payload["HTF_CANDLE_TABLE_CSV"].splitlines()
    assert len(csv_rows) == 20
    assert all(dt_row_pattern.match(row) for row in csv_rows)


@pytest.mark.parametrize("daily_htf", ["1d", "3d", "1w", "1M"])
def test_daily_and_above_htf_renders_date_only_stamps(daily_htf: str) -> None:
    import re
    from smc_prompt.structure_analyzer import analyze

    candles = _make_smc_series()
    config = cfg.build_config(
        "BTCUSDT",
        htf_interval=daily_htf,
        mtf_interval="4h",
        ltf_interval="1h",
        price_format=cfg.PriceFormat(tick_decimals=2),
    )
    current_price = Decimal("146.00")

    htf_analysis, htf_emitted, _ = analyze(
        candles,
        timeframe=config.htf_interval,
        requested=config.htf_candles,
        current_price=current_price,
        config=config,
    )
    mtf_analysis, mtf_emitted, _ = analyze(
        candles,
        timeframe=config.mtf_interval,
        requested=config.mtf_candles,
        current_price=current_price,
        config=config,
    )
    ltf_analysis, ltf_emitted, _ = analyze(
        candles,
        timeframe=config.ltf_interval,
        requested=config.ltf_candles,
        current_price=current_price,
        config=config,
    )

    payload = build_payload(
        symbol="BTCUSDT",
        generated_at=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        current_price=current_price,
        htf=htf_analysis,
        mtf=mtf_analysis,
        ltf=ltf_analysis,
        htf_candles=htf_emitted,
        mtf_candles=mtf_emitted,
        ltf_candles=ltf_emitted,
        config=config,
    )

    date_row_pattern = re.compile(r"^\d{2}-\d{2},")
    ref_date_pattern = re.compile(r"\(\d{2}-\d{2},")

    assert ref_date_pattern.search(payload["HTF_REF_HIGH_SUMMARY"])
    assert ref_date_pattern.search(payload["HTF_REF_LOW_SUMMARY"])
    assert all(date_row_pattern.match(row) for row in payload["HTF_SWINGS_TABLE"].splitlines())
    assert all(date_row_pattern.match(row) for row in payload["HTF_FVG_TABLE"].splitlines())
    assert all(date_row_pattern.match(row) for row in payload["HTF_OB_TABLE"].splitlines())
    assert all(date_row_pattern.match(row) for row in payload["HTF_CANDLE_TABLE_CSV"].splitlines())




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


def test_smc_v2_prompt_instructions_restructuring(tmp_path: Path) -> None:
    """Pastikan struktur instruksi analisis v2 (Ticket #34) termuat lengkap di prompt."""
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

    # 1. Definisi Tabel wajib memuat MSS / CHoCH
    assert "**MSS / CHoCH (Market Structure Shift):**" in prompt

    # 2. Bagian 1 wajib memuat aturan hierarki dan resolusi konflik HTF > MTF > LTF
    assert "## 1. Multi-Timeframe Alignment (HTF, MTF & LTF)" in prompt
    assert "**Hierarki & Resolusi Konflik:**" in prompt
    assert "`HTF > MTF > LTF`" in prompt

    # 3. Bagian 2 wajib memisahkan Retail Liquidity Proxy (2a) dan Inducement / IDM (2b)
    assert "## 2. Pemetaan Likuiditas Ritel & Inducement (IDM)" in prompt
    assert "2a. Retail Liquidity Proxy (Heuristik Mekanis)" in prompt
    assert "2b. Inducement / Sweep Zone (IDM — Internal Liquidity)" in prompt
    assert "satu-satunya sumber" not in prompt

    # 4. Bagian 3 wajib mendefinisikan Draw on Liquidity (DOL) dengan ranking sumber
    assert "## 3. Draw on Liquidity (DOL) = Target Take Profit" in prompt
    assert "Draw on Liquidity (DOL / External Liquidity)" in prompt
    assert "Equal Highs/Lows" in prompt
    assert "Unfilled FVG" in prompt

    # 5. Bagian 4 wajib membedakan Tipe A (ACTIVE / TRIGGERED) dan Tipe B (PENDING / CONDITIONAL)
    assert "## 4. Logika Kontra-Ritel & Status Konfirmasi Entry" in prompt
    assert "Tipe A — `ACTIVE / TRIGGERED` `[FAKTA]`" in prompt
    assert "Tipe B — `PENDING / CONDITIONAL` `[INFERENSI]`" in prompt

    # 6. Bagian 5 wajib memuat Status Setup, buffer spread/fee SL, Jarak Risiko (1R), dan menghapus RRR aktual
    assert "| Status Setup | ACTIVE (Triggered) / PENDING (Conditional) |" in prompt
    assert "0.05%–0.10%" in prompt
    assert "| Jarak Risiko (1R) |" in prompt
    assert "Pos Size = Risk($) / X.XX%" in prompt
    assert "RRR aktual (TP1)" not in prompt
    assert "RRR aktual (TP2)" not in prompt

    # 7. Bagian 6 wajib memuat Rubrik Skor Konfluensi (0-5), Confidence Level, dan Invalidasi & Skenario Flip
    assert "## 6. Kesimpulan, Rubrik Confidence & Invalidasi" in prompt
    assert "**Skor Konfluensi (0–5):**" in prompt
    assert "High (4–5 poin)" in prompt
    assert "Medium (3 poin)" in prompt
    assert "Low (≤2 poin" in prompt
    assert "**Invalidasi & Skenario Flip (1 Kalimat):**" in prompt


