"""Tests for the offline CSV data source and the offline CLI wiring (#10).

Verifies the CSV schema parsing, close-time derivation, the fetcher-parity
interface, and that the network path is only used when no ``--input-csv`` is
supplied.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from smc_prompt import cli
from smc_prompt import config as cfg
from smc_prompt.csv_source import LocalCsvSource, load_csv_series
from smc_prompt.errors import ConfigError, NetworkError

from .conftest import HTF_CSV, LTF_CSV, MTF_CSV


def _write_csv(path: Path, rows: list[str], header: str | None = None) -> Path:
    header = header or "open_time,open,high,low,close,volume"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _source(tmp_path: Path) -> LocalCsvSource:
    config = cfg.build_config(
        "TESTUSDT",
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
    )
    return LocalCsvSource(
        config,
        htf_file=str(HTF_CSV),
        mtf_file=str(MTF_CSV),
        ltf_file=str(LTF_CSV),
    )


# --------------------------------------------------------------------------
# Schema / parsing
# --------------------------------------------------------------------------


def test_load_csv_parses_iso_with_z(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        ["2026-01-01T00:00:00Z,1,2,0.5,1.5,10"],
    )
    series = load_csv_series(str(path), interval="1d")

    candle = series.candles[0]
    assert candle.open_time == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert candle.open == Decimal("1")
    assert candle.high == Decimal("2")
    assert candle.low == Decimal("0.5")
    assert candle.close == Decimal("1.5")
    assert candle.volume == Decimal("10")
    assert candle.is_closed is True


def test_load_csv_accepts_naive_and_date_forms(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        [
            "2026-01-02 03:00,1,2,0.5,1.5,10",
            "2026-01-02T04:00:00,1,2,0.5,1.5,10",
        ],
    )
    series = load_csv_series(str(path), interval="1h")

    assert series.candles[0].open_time == datetime(
        2026, 1, 2, 3, tzinfo=timezone.utc
    )
    assert series.candles[1].open_time == datetime(
        2026, 1, 2, 4, tzinfo=timezone.utc
    )


def test_load_csv_epoch_millis(tmp_path: Path) -> None:
    ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    path = _write_csv(tmp_path / "a.csv", [f"{ms},1,2,0.5,1.5,10"])

    series = load_csv_series(str(path), interval="1d")
    assert series.candles[0].open_time == datetime(
        2026, 1, 1, tzinfo=timezone.utc
    )


def test_load_csv_derives_close_time_from_spacing(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        [
            "2026-01-01T00:00:00Z,1,2,0.5,1.5,10",
            "2026-01-01T01:00:00Z,1,2,0.5,1.5,10",
        ],
    )
    series = load_csv_series(str(path), interval="1h")

    first = series.candles[0]
    assert first.close_time == datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert series.last_close_time == datetime(2026, 1, 1, 2, tzinfo=timezone.utc)


def test_load_csv_explicit_close_time_wins(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        ["2026-01-01T00:00:00Z,1,2,0.5,1.5,10,2026-01-01T00:07:30Z"],
        header="open_time,open,high,low,close,volume,close_time",
    )
    series = load_csv_series(str(path), interval="1h")

    assert series.candles[0].close_time == datetime(
        2026, 1, 1, 0, 7, 30, tzinfo=timezone.utc
    )
    assert series.last_close_time == datetime(
        2026, 1, 1, 0, 7, 30, tzinfo=timezone.utc
    )


def test_load_csv_rows_are_sorted_chronologically(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        [
            "2026-01-01T02:00:00Z,1,2,0.5,1.5,10",
            "2026-01-01T00:00:00Z,1,2,0.5,1.5,10",
        ],
    )
    series = load_csv_series(str(path), interval="1h")

    assert [c.open_time.hour for c in series.candles] == [0, 2]


def test_load_csv_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_csv_series(str(tmp_path / "nope.csv"), interval="1d")


def test_load_csv_missing_column_raises(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        ["2026-01-01T00:00:00Z,1,2,0.5,1.5"],
        header="open_time,open,high,low,close",
    )
    with pytest.raises(ConfigError, match="missing required column"):
        load_csv_series(str(path), interval="1d")


def test_load_csv_invalid_number_raises(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "a.csv", ["2026-01-01T00:00:00Z,1,x,0.5,1.5,10"])
    with pytest.raises(ConfigError, match="invalid high"):
        load_csv_series(str(path), interval="1d")


def test_load_csv_empty_body_raises(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "a.csv", [])
    with pytest.raises(ConfigError, match="no candle rows"):
        load_csv_series(str(path), interval="1d")


def test_load_csv_invalid_timestamp_raises(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "a.csv", ["not-a-time,1,2,0.5,1.5,10"])
    with pytest.raises(ConfigError, match="invalid timestamp"):
        load_csv_series(str(path), interval="1d")


# --------------------------------------------------------------------------
# Interface parity
# --------------------------------------------------------------------------


def test_local_source_interface_parity() -> None:
    source = _source(Path("."))

    entry = source.validate_symbol()
    assert entry["status"] == "TRADING"
    assert entry["filters"] == []
    assert source.active_base_url is None
    assert source.price_notes == ()
    assert source.with_now(datetime.now(timezone.utc)) is source

    # fetch_server_time is deterministic: max(close_time) + 1s.
    assert source.fetch_server_time() == datetime(
        2026, 7, 20, 0, 0, 1, tzinfo=timezone.utc
    )


def test_local_source_fetch_klines_returns_tail() -> None:
    source = _source(Path("."))

    rows = source.fetch_klines("1d", 10)
    assert len(rows) == 10
    assert all(c.is_closed for c in rows)


def test_local_source_fetch_current_price_is_last_close() -> None:
    source = _source(Path("."))

    price = source.fetch_current_price()
    ltf = source.fetch_klines("1h", 10_000)
    assert price == ltf[-1].close


def test_local_source_unknown_interval_raises() -> None:
    source = _source(Path("."))
    with pytest.raises(ConfigError):
        source.fetch_klines("5m", 10)


def test_local_source_identical_intervals_rejected(tmp_path: Path) -> None:
    # ``build_config`` now enforces the three-way distinctness rule, so the
    # duplicate trio is rejected before ``LocalCsvSource`` is constructed.
    with pytest.raises(ConfigError, match="DISTINCT intervals"):
        cfg.build_config("TESTUSDT", htf_interval="1h", ltf_interval="1h")


def test_local_source_three_way_distinctness_guard(tmp_path: Path) -> None:
    """Direct construction still guards a duplicate trio (defense-in-depth)."""

    config = cfg.build_config("TESTUSDT")  # valid 1d / 4h / 1h trio
    # Bypass ``build_config`` validation by mutating the frozen config to a
    # duplicate trio, then assert the source-level guard fires.
    from dataclasses import replace

    bad = replace(config, ltf_interval=config.htf_interval)
    with pytest.raises(ConfigError, match="distinct --htf-interval"):
        LocalCsvSource(
            bad,
            htf_file=str(HTF_CSV),
            mtf_file=str(MTF_CSV),
            ltf_file=str(LTF_CSV),
        )


# --------------------------------------------------------------------------
# CLI wiring: offline opt-in, network remains default
# --------------------------------------------------------------------------


def test_cli_offline_runs_from_csv(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "BTCUSDT",
            "--input-csv",
            str(HTF_CSV),
            "--htf-file",
            str(HTF_CSV),
            "--mtf-file",
            str(MTF_CSV),
            "--ltf-file",
            str(LTF_CSV),
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert list(tmp_path.glob("BTCUSDT-*.md"))


def test_cli_offline_three_csv_mode(tmp_path: Path) -> None:
    """--input-csv + three explicit --*-file flags renders all three tiers."""

    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "BTCUSDT",
            "--input-csv",
            str(HTF_CSV),
            "--htf-file",
            str(HTF_CSV),
            "--mtf-file",
            str(MTF_CSV),
            "--ltf-file",
            str(LTF_CSV),
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    written = list(tmp_path.glob("BTCUSDT-*.md"))
    assert written
    text = written[0].read_text(encoding="utf-8")
    assert "Ringkasan Data HTF" in text
    assert "Ringkasan Data MTF" in text
    assert "Ringkasan Data LTF" in text


def test_cli_htf_file_without_input_csv_is_config_error() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["BTCUSDT", "--htf-file", str(HTF_CSV)]
    )

    assert result.exit_code == 2
    assert "require --input-csv" in result.output


def test_cli_mtf_file_without_input_csv_is_config_error() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["BTCUSDT", "--mtf-file", str(MTF_CSV)]
    )

    assert result.exit_code == 2
    assert "require --input-csv" in result.output
    assert "--input-csv" in result.output


def test_cli_network_is_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no offline flag, cli.run must construct the network DataFetcher."""

    built: dict[str, bool] = {}

    class _RecordingFetcher:
        def __init__(self, config: object) -> None:
            built["network"] = True

        def validate_symbol(self) -> dict:
            raise NetworkError("stop here: network path selected")

    monkeypatch.setattr("smc_prompt.cli.DataFetcher", _RecordingFetcher)
    monkeypatch.setattr("smc_prompt.cli.LocalCsvSource", lambda *a, **k: built.setdefault("local", True))

    with pytest.raises(NetworkError):
        cli.run(
            "BTCUSDT",
            htf_candles=60,
            ltf_candles=100,
            swing_lookback=5,
            distance_reference="nearest",
            include_atr=True,
            output_dir="output",
        )

    assert built.get("network") is True
    assert "local" not in built
