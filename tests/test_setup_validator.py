"""Tests for setup validation & front-run checking (--validate-setup / --check-setup).

Verifies:
  1. Mathematical analysis for Long and Short setups across statuses
     (FRESH, FRONT_RUNNED, TRIGGERED, DOL_REACHED, STOPPED_OUT).
  2. Automatic trade direction inference and price boundary validation.
  3. Jinja2 template rendering: presence of role persona, setup specs,
     approach metrics, raw klines, and evaluation guidelines.
  4. Integration with cli.run and Click CliRunner using offline CSVs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from smc_prompt import cli, config as cfg
from smc_prompt.errors import ConfigError
from smc_prompt.output import make_output_path
from smc_prompt.setup_validator import (
    FrontRunAnalysis,
    SetupSpec,
    analyze_setup,
    render_validation_prompt,
)
from tests.conftest import HTF_CSV, LTF_CSV, MTF_CSV, make_candle


def test_make_output_path_validation_mode() -> None:
    """Memastikan nama file output memiliki format SYMBOL-VALIDATION-TIMESTAMP.md."""
    moment = datetime(2026, 9, 21, 15, 30, 0, tzinfo=timezone.utc)
    path = make_output_path("BTCUSDT", "output", moment, is_validation=True)
    assert path.name == "BTCUSDT-VALIDATION-2026-09-21-15-30-00-UTC.md"


def test_analyze_setup_empty_candles_raises() -> None:
    """Analisis setup harus error jika tidak ada candle yang diberikan."""
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("100"),
        tp_price=Decimal("120"),
    )
    with pytest.raises(ConfigError, match="no candle data provided"):
        analyze_setup(setup, [])


def test_analyze_setup_long_front_runned() -> None:
    """Long setup: harga mendekati entry tapi berbalik arah dan mencapai >60% target."""
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("100"),
        tp_price=Decimal("120"),
        sl_price=Decimal("90"),
    )
    # Candle 0: mendekat ke entry (low 101, selisih 1.0)
    # Candle 1: memantul naik ke 116 (travel ratio = (116-100)/20 = 80%)
    candles = [
        make_candle(0, open_="105", high="106", low="101", close="103"),
        make_candle(1, open_="103", high="116", low="102", close="115"),
    ]

    analysis = analyze_setup(setup, candles)
    assert analysis.status == "FRONT_RUNNED"
    assert not analysis.entry_touched
    assert not analysis.tp_touched
    assert not analysis.sl_touched
    assert analysis.closest_price == Decimal("101")
    assert analysis.missed_by_distance == Decimal("1")
    assert analysis.missed_by_pct == Decimal("1.0")
    assert analysis.target_travel_ratio == Decimal("80.0")


def test_analyze_setup_long_fresh() -> None:
    """Long setup: harga belum menyentuh entry dan belum lari jauh ke target."""
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("100"),
        tp_price=Decimal("120"),
        sl_price=Decimal("90"),
    )
    candles = [
        make_candle(0, open_="108", high="110", low="103", close="105"),
        make_candle(1, open_="105", high="107", low="104", close="106"),
    ]

    analysis = analyze_setup(setup, candles)
    assert analysis.status == "FRESH"
    assert not analysis.entry_touched
    assert analysis.closest_price == Decimal("103")
    assert analysis.target_travel_ratio == Decimal("50.0")  # (110-100)/20 * 100 = 50%


def test_analyze_setup_long_triggered() -> None:
    """Long setup: posisi sudah terisi (filled), entry tersentuh (low <= entry), setup aktif."""
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("100"),
        tp_price=Decimal("120"),
        sl_price=Decimal("90"),
        order_status="filled",
    )
    candles = [
        make_candle(0, open_="105", high="105", low="99.5", close="101"),
        make_candle(1, open_="101", high="108", low="100.5", close="107"),
    ]

    analysis = analyze_setup(setup, candles)
    assert analysis.status == "TRIGGERED"
    assert analysis.entry_touched
    assert analysis.missed_by_distance == Decimal("0")
    assert analysis.entry_time == candles[0].open_time


def test_analyze_setup_long_dol_reached() -> None:
    """Long setup: target TP tercapai sebelum entry tersentuh (invalidasi setup)."""
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("100"),
        tp_price=Decimal("120"),
        sl_price=Decimal("90"),
        order_status="unfilled",
    )
    candles = [
        make_candle(0, open_="105", high="106", low="101", close="104"),
        make_candle(1, open_="104", high="121", low="103", close="119"),
    ]

    analysis = analyze_setup(setup, candles)
    assert analysis.status == "DOL_REACHED"
    assert not analysis.entry_touched
    assert analysis.tp_touched


def test_analyze_setup_long_stopped_out() -> None:
    """Long setup: posisi terisi (filled), harga menembus SL."""
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("100"),
        tp_price=Decimal("120"),
        sl_price=Decimal("90"),
        order_status="filled",
    )
    candles = [
        make_candle(0, open_="105", high="105", low="99", close="99.5"),
        make_candle(1, open_="99.5", high="100", low="88", close="89"),
    ]

    analysis = analyze_setup(setup, candles)
    assert analysis.status == "STOPPED_OUT"
    assert analysis.entry_touched
    assert analysis.sl_touched


def test_analyze_setup_short_front_runned() -> None:
    """Short setup: harga mendekati entry dari bawah, lalu jatuh >60% menuju target."""
    setup = SetupSpec(
        symbol="ETHUSDT",
        direction="short",
        entry_price=Decimal("2000"),
        tp_price=Decimal("1800"),
        sl_price=Decimal("2100"),
        order_status="unfilled",
    )
    # Candle 0: mendekati entry (high 1990, selisih 10.0)
    # Candle 1: jatuh ke 1850 (travel ratio = (2000-1850)/200 = 75%)
    candles = [
        make_candle(0, open_="1950", high="1990", low="1940", close="1970"),
        make_candle(1, open_="1970", high="1975", low="1850", close="1860"),
    ]

    analysis = analyze_setup(setup, candles)
    assert analysis.status == "FRONT_RUNNED"
    assert not analysis.entry_touched
    assert analysis.closest_price == Decimal("1990")
    assert analysis.missed_by_distance == Decimal("10")
    assert analysis.target_travel_ratio == Decimal("75.0")


def test_analyze_setup_short_stopped_out() -> None:
    """Short setup: posisi terisi (filled), lalu harga naik menembus SL."""
    setup = SetupSpec(
        symbol="ETHUSDT",
        direction="short",
        entry_price=Decimal("2000"),
        tp_price=Decimal("1800"),
        sl_price=Decimal("2100"),
        order_status="filled",
    )
    candles = [
        make_candle(0, open_="1980", high="2005", low="1970", close="1995"),
        make_candle(1, open_="1995", high="2105", low="1990", close="2102"),
    ]

    analysis = analyze_setup(setup, candles)
    assert analysis.status == "STOPPED_OUT"
    assert analysis.entry_touched
    assert analysis.sl_touched


def test_analyze_setup_pdf_scenario_unfilled_not_stopped_out() -> None:
    """Skenario PDF user: Long BTC entry 83700, SL 81700, candle historis pernah 80850.

    Trader menyatakan limit order BELUM TERJEMPUT (unfilled).
    Sistem TIDAK BOLEH menganggap posisi STOPPED_OUT atau TERJEMPUT berdasarkan
    candle historis masa lalu sebelum order dipasang.
    Closest approach harus mengambil pantulan terdekat yang berada di atas entry (84778).
    """
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("83700"),
        tp_price=Decimal("88000"),
        sl_price=Decimal("81700"),
        order_status="unfilled",
    )
    candles = [
        # Candle 0: masa lalu sebelum order (misal 01:45 UTC), low 80850
        make_candle(0, open_="81200", high="81500", low="80850", close="81300"),
        # Candle 1: harga naik lalu pullback menuju entry tapi hanya sampai 84778 lalu lari ke 87000
        make_candle(1, open_="81300", high="85500", low="84778", close="85400"),
        make_candle(2, open_="85400", high="87000", low="85200", close="86800"),
    ]

    analysis = analyze_setup(setup, candles)

    # Validasi fakta bahwa order belum terisi
    assert not analysis.entry_touched
    assert not analysis.sl_touched
    assert analysis.status != "STOPPED_OUT"
    assert analysis.status == "FRONT_RUNNED"
    # Closest approach harus 84778, bukan 80850
    assert analysis.closest_price == Decimal("84778")
    assert analysis.closest_approach_distance == Decimal("1078")  # 84778 - 83700
    assert "BELUM TERJEMPUT" in analysis.entry_status
    assert "BELUM AKTIF" in analysis.sl_status


def test_render_validation_prompt() -> None:
    """Memverifikasi seluruh section dan komponen prompt validasi setup."""
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("60000.00"),
        tp_price=Decimal("62000.00"),
        sl_price=Decimal("59000.00"),
    )
    candles = [
        make_candle(0, open_="60500", high="60600", low="60050", close="60200"),
        make_candle(1, open_="60200", high="61600", low="60100", close="61500"),
    ]
    analysis = analyze_setup(setup, candles)

    prompt = render_validation_prompt(
        setup=setup,
        analysis=analysis,
        candles=candles,
        interval="15m",
        provider_label="Binance",
        generated_at=datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc),
        price_format=cfg.PriceFormat(2),
    )

    # Header & Persona
    assert "## Peran" in prompt
    assert "Senior ICT/SMC Risk & Setup Validator" in prompt
    assert "- **Pair/Aset:** BTCUSDT" in prompt
    assert "- **Arah Setup:** LONG" in prompt
    assert "- **Level Entry yang Direncanakan:** 60000.00" in prompt
    assert "- **Target Take Profit (TP / DOL):** 62000.00" in prompt
    assert "- **Level Stop Loss (SL):** 59000.00" in prompt

    # Status & Metrik
    assert "FRONT_RUNNED" in prompt
    assert "Ringkasan Fakta Kuantitatif Setup" in prompt
    assert "Pendekatan Terdekat ke Entry" in prompt
    assert "Rasio Jelajah Menuju Target" in prompt

    # Prompt instruksi dan klines mentah
    assert "Kerangka Evaluasi Integritas Setup" in prompt
    assert "Data Candle Mentah Sequence Perjalanan Harga" in prompt
    assert "```csv" in prompt
    assert "[STATUS: FRESH & VALID]" in prompt


def test_render_validation_prompt_anti_hallucination_unfilled() -> None:
    """Memverifikasi direktif anti-halusinasi muncul saat order_status unfilled."""
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("83700"),
        tp_price=Decimal("88000"),
        sl_price=Decimal("81700"),
        order_status="unfilled",
    )
    candles = [
        make_candle(0, open_="81200", high="81500", low="80850", close="81300"),
        make_candle(1, open_="81300", high="85500", low="84778", close="85400"),
    ]
    analysis = analyze_setup(setup, candles)
    prompt = render_validation_prompt(
        setup=setup,
        analysis=analysis,
        candles=candles,
        interval="15m",
        provider_label="Binance",
        generated_at=datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc),
        price_format=cfg.PriceFormat(2),
    )

    assert "- **Status Order Trader:** BELUM TERJEMPUT (LIMIT ORDER MASIH PENDING DI EXCHANGE)" in prompt
    assert "DILARANG KERAS" in prompt
    assert "berhalusinasi atau berasumsi bahwa posisi ini sudah aktif" in prompt
    assert "LIMIT ORDER MASIH PENDING DI EXCHANGE DAN BELUM PERNAH TERISI / BELUM TERJEMPUT" in prompt


def test_render_validation_prompt_filled_directives() -> None:
    """Memverifikasi petunjuk manajemen posisi muncul saat order_status filled."""
    setup = SetupSpec(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("83700"),
        tp_price=Decimal("88000"),
        sl_price=Decimal("81700"),
        order_status="filled",
    )
    candles = [
        make_candle(0, open_="84000", high="84500", low="83500", close="83900"),
    ]
    analysis = analyze_setup(setup, candles)
    prompt = render_validation_prompt(
        setup=setup,
        analysis=analysis,
        candles=candles,
        interval="15m",
        provider_label="Binance",
        generated_at=datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc),
        price_format=cfg.PriceFormat(2),
    )

    assert "- **Status Order Trader:** SUDAH TERJEMPUT (POSISI TRADING AKTIF BERJALAN)" in prompt
    assert "SUDAH TERJEMPUT (AKTIF BERJALAN DI PASAR)" in prompt
    assert "Trade Management" in prompt


def test_cli_validate_setup_validations() -> None:
    """Memverifikasi validasi input CLI untuk --validate-setup."""
    # Tanpa order status yang ditentukan wajib error
    with pytest.raises(ConfigError, match="requires specifying order status"):
        cli.run("BTCUSDT", validate_setup=True, entry=100.0, tp=120.0)

    # Konflik opsi --unfilled dan --filled
    with pytest.raises(ConfigError, match="Cannot specify both --unfilled and --filled"):
        cli.run(
            "BTCUSDT",
            validate_setup=True,
            entry=100.0,
            tp=120.0,
            unfilled=True,
            filled=True,
        )

    # Order status tidak valid
    with pytest.raises(ConfigError, match="Invalid order status 'canceled'"):
        cli.run(
            "BTCUSDT",
            validate_setup=True,
            entry=100.0,
            tp=120.0,
            order_status="canceled",
        )

    # Tanpa entry atau tp
    with pytest.raises(ConfigError, match="requires both --entry and --tp"):
        cli.run("BTCUSDT", validate_setup=True, entry=100.0, unfilled=True)

    with pytest.raises(ConfigError, match="requires both --entry and --tp"):
        cli.run("BTCUSDT", validate_setup=True, tp=120.0, unfilled=True)

    # TP == Entry
    with pytest.raises(ConfigError, match="--tp cannot be equal to --entry"):
        cli.run("BTCUSDT", validate_setup=True, entry=100.0, tp=100.0, unfilled=True)

    # Direction tidak valid
    with pytest.raises(ConfigError, match="Invalid direction 'sideways'"):
        cli.run(
            "BTCUSDT",
            validate_setup=True,
            entry=100.0,
            tp=120.0,
            direction="sideways",
            unfilled=True,
        )

    # Long setup dengan SL >= Entry
    with pytest.raises(ConfigError, match="For long setups, --sl must be lower than --entry"):
        cli.run(
            "BTCUSDT",
            validate_setup=True,
            entry=100.0,
            tp=120.0,
            sl=105.0,
            unfilled=True,
        )

    # Short setup dengan SL <= Entry
    with pytest.raises(ConfigError, match="For short setups, --sl must be higher than --entry"):
        cli.run(
            "BTCUSDT",
            validate_setup=True,
            entry=100.0,
            tp=80.0,
            sl=95.0,
            unfilled=True,
        )

    # Konflik mode --candles-only dan --validate-setup
    with pytest.raises(ConfigError, match="Cannot specify both --candles-only and --validate-setup"):
        cli.run(
            "BTCUSDT",
            candles_only=True,
            validate_setup=True,
            entry=100.0,
            tp=120.0,
            unfilled=True,
        )


def test_cli_run_validate_setup_offline(tmp_path: Path) -> None:
    """Memverifikasi end-to-end cli.run dengan --validate-setup dan offline CSV."""
    res = cli.run(
        "BTCUSDT",
        validate_setup=True,
        unfilled=True,
        entry=50000.0,
        tp=55000.0,
        sl=48000.0,
        validate_interval="15m",
        validate_candles=20,
        input_csv=str(HTF_CSV),
        htf_file=str(HTF_CSV),
        mtf_file=str(MTF_CSV),
        ltf_file=str(LTF_CSV),
        output_dir=str(tmp_path),
    )

    assert res.output_path.endswith(".md")
    assert "-VALIDATION-" in res.output_path
    output_file = Path(res.output_path)
    assert output_file.exists()

    content = output_file.read_text(encoding="utf-8")
    assert "## Peran" in content
    assert "Senior ICT/SMC Risk & Setup Validator" in content
    assert "- **Pair/Aset:** BTCUSDT" in content
    assert "- **Level Entry yang Direncanakan:** 50000" in content
    assert "- **Status Order Trader:** BELUM TERJEMPUT" in content
    assert "Data Candle Mentah" in content


def test_cli_run_dry_run_validate_setup(capsys: pytest.CaptureFixture[str]) -> None:
    """Memverifikasi --dry-run dengan --validate-setup tidak menulis file."""
    res = cli.run(
        "BTCUSDT",
        validate_setup=True,
        unfilled=True,
        entry=65000.0,
        tp=70000.0,
        sl=63000.0,
        validate_interval="1h",
        validate_candles=30,
        dry_run=True,
        input_csv=str(HTF_CSV),
    )

    assert res.dry_run is True
    assert res.output_path == ""
    err = capsys.readouterr().err
    assert "DRY RUN — setup validation mode" in err
    assert "direction=long order_status=unfilled entry=65000.0 tp=70000.0 sl=63000.0" in err
    assert "validate_interval=1h" in err
    assert "validate_candles=30" in err


def test_cli_main_click_runner(tmp_path: Path) -> None:
    """Memverifikasi eksekusi via Click CliRunner."""
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "BTCUSDT",
            "--validate-setup",
            "--unfilled",
            "--entry",
            "50000",
            "--tp",
            "52000",
            "--input-csv",
            str(HTF_CSV),
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Setup validation prompt written to" in result.output
