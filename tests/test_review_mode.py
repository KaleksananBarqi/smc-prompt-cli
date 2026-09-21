"""Tests for post-trade review mode (--candles-only / --review).

Verifies:
  1. Statistical aggregation of candle sessions (highest, lowest, net change, range).
  2. Markdown rendering: presence of clean data & journal template, absence of prompt instructions.
  3. Output filename convention: TICKER-REVIEW-YYYY-MM-DD-HH-MM-SS-UTC.md.
  4. Integration through cli.run and Click CLI runner with offline CSV and mock feeds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from smc_prompt import cli, config as cfg
from smc_prompt.errors import ConfigError
from smc_prompt.output import make_output_path
from smc_prompt.review_renderer import compute_session_stats, render_review_markdown
from tests.conftest import HTF_CSV, LTF_CSV, MTF_CSV, make_candle


def test_compute_session_stats_basic() -> None:
    candles = [
        make_candle(0, high="105", low="95", open_="100", close="102"),
        make_candle(1, high="110", low="101", open_="102", close="108"),
        make_candle(2, high="109", low="90", open_="108", close="92"),
    ]

    stats = compute_session_stats(candles)

    assert stats.candle_count == 3
    assert stats.first_open == Decimal("100")
    assert stats.last_close == Decimal("92")
    assert stats.net_change == Decimal("-8")
    assert stats.net_change_pct == Decimal("-8.0")
    assert stats.highest_price == Decimal("110")
    assert stats.highest_time == candles[1].open_time
    assert stats.lowest_price == Decimal("90")
    assert stats.lowest_time == candles[2].open_time
    assert stats.total_range == Decimal("20")


def test_compute_session_stats_empty_raises() -> None:
    with pytest.raises(ConfigError, match="no candles provided"):
        compute_session_stats([])


def test_render_review_markdown_content() -> None:
    candles = [
        make_candle(0, high="51000", low="49000", open_="50000", close="50500"),
        make_candle(1, high="52000", low="50200", open_="50500", close="51800"),
    ]

    md = render_review_markdown(
        symbol="BTCUSDT",
        interval="1h",
        candles=candles,
        provider_label="Binance",
        generated_at=datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc),
    )

    # Must contain review header & session stats
    assert "# Post-Trade Review: BTCUSDT (1H)" in md
    assert "- **Symbol:** BTCUSDT" in md
    assert "- **Provider:** Binance" in md
    assert "- **Interval:** 1h (1H)" in md
    assert "- **Jumlah Candle:** 2 candle (closed)" in md
    assert "## Ringkasan Pergerakan Harga (Session Stats)" in md
    assert "- **Open Pertama:** 50000" in md
    assert "- **Close Terakhir:** 51800" in md
    assert "- **Harga Tertinggi (Highest):** 52000" in md
    assert "- **Harga Terendah (Lowest):** 49000" in md

    # Must contain journal checklist
    assert "## Catatan Jurnal & Evaluasi Trade" in md
    assert "- [ ] **Arah Posisi:** Long / Short" in md
    assert "- [ ] **Level Entry:**" in md

    # Must contain raw CSV table
    assert "## Data Candle Mentah (OHLCV)" in md
    assert "```csv" in md

    # Must NOT contain prompt instructions
    assert "Bertindaklah sebagai Senior ICT/SMC Trading Analyst" not in md
    assert "Liquidity Targeting" not in md
    assert "Trading Plan" not in md
    assert "[FAKTA]" not in md


def test_render_review_markdown_daily() -> None:
    candles = [
        make_candle(
            0,
            high="2500",
            low="2400",
            open_="2450",
            close="2480",
            step=timedelta(days=1),
        ),
    ]

    md = render_review_markdown(
        symbol="ETHUSDT",
        interval="1d",
        candles=candles,
        provider_label="Binance",
    )

    assert "(Daily)" in md
    assert "tanggal(UTC),open,high,low,close,volume" in md


def test_make_output_path_review_mode() -> None:
    moment = datetime(2026, 9, 21, 8, 30, 0, tzinfo=timezone.utc)
    path = make_output_path("BTCUSDT", "output", moment, is_review=True)

    assert path.name == "BTCUSDT-REVIEW-2026-09-21-08-30-00-UTC.md"


def test_cli_run_candles_only_offline(tmp_path: Path) -> None:
    res = cli.run(
        "BTCUSDT",
        candles_only=True,
        review_interval="1h",
        review_candles=15,
        input_csv=str(HTF_CSV),
        htf_file=str(HTF_CSV),
        mtf_file=str(MTF_CSV),
        ltf_file=str(LTF_CSV),
        output_dir=str(tmp_path),
    )

    assert res.output_path.endswith(".md")
    assert "-REVIEW-" in res.output_path
    output_file = Path(res.output_path)
    assert output_file.exists()

    content = output_file.read_text(encoding="utf-8")
    assert "# Post-Trade Review: BTCUSDT" in content
    assert "## Data Candle Mentah (OHLCV)" in content
    assert "Bertindaklah sebagai" not in content


def test_cli_run_dry_run_candles_only(capsys: pytest.CaptureFixture[str]) -> None:
    res = cli.run(
        "BTCUSDT",
        candles_only=True,
        review_interval="15m",
        review_candles=25,
        dry_run=True,
    )

    assert res.dry_run is True
    assert res.prompt == ""
    captured = capsys.readouterr()
    assert "DRY RUN — review mode" in captured.err
    assert "review_interval=15m" in captured.err
    assert "review_candles=25" in captured.err


def test_cli_runner_candles_only(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "BTCUSDT",
            "--candles-only",
            "--review-interval",
            "1h",
            "--review-candles",
            "10",
            "--input-csv",
            str(HTF_CSV),
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    created_files = list(tmp_path.glob("BTCUSDT-REVIEW-*.md"))
    assert len(created_files) == 1
    text = created_files[0].read_text(encoding="utf-8")
    assert "# Post-Trade Review: BTCUSDT (1H)" in text
    assert "Jumlah Candle:** 10 candle (closed)" in text


def test_validate_review_candles_bounds() -> None:
    with pytest.raises(ConfigError, match="must be >= 5"):
        cfg.validate_review_candles(4)

    with pytest.raises(ConfigError, match="must be <= 500"):
        cfg.validate_review_candles(501)

    assert cfg.validate_review_candles(30) == 30
