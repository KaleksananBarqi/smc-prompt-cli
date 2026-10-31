"""UTC guarantee: candles are parsed and rendered in UTC (requirement 3).

The parser/analysis paths already normalize to aware UTC; these tests pin that
contract as a regression guard and assert the rendered candle legends state the
timezone explicitly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smc_prompt import config as cfg
from smc_prompt.csv_source import _parse_time
from smc_prompt.data_fetcher import _ms_to_utc

from .conftest import HTF_CSV, LTF_CSV, MTF_CSV


def _parse(raw: str):
    return _parse_time(raw, path="test.csv", line_no=1)


def test_parse_time_naive_is_assumed_utc() -> None:
    parsed = _parse("2026-01-01 00:00:00")

    assert parsed.tzinfo is timezone.utc
    assert parsed == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_parse_time_z_suffix_is_utc() -> None:
    parsed = _parse("2026-01-01T00:00:00Z")

    assert parsed.tzinfo is timezone.utc


def test_parse_time_epoch_millis_is_utc() -> None:
    parsed = _parse("1767225600000")

    assert parsed.tzinfo is timezone.utc
    assert parsed == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_parse_time_non_utc_offset_is_converted() -> None:
    """A ``+07:00`` input is converted to the equivalent UTC instant."""

    parsed = _parse("2026-01-01T07:00:00+07:00")

    assert parsed.tzinfo is timezone.utc
    assert parsed == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_ms_to_utc_bounds() -> None:
    assert _ms_to_utc(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert _ms_to_utc(1767225600000) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def _offline_prompt(tmp_path: Path) -> str:
    from smc_prompt import cli

    return cli.run(
        "BTCUSDT",
        htf_candles=60,
        mtf_candles=120,
        ltf_candles=100,
        swing_lookback=5,
        distance_reference="nearest",
        include_atr=True,
        output_dir=str(tmp_path),
        htf_interval="1d",
        mtf_interval="4h",
        ltf_interval="1h",
        input_csv=str(HTF_CSV),
        htf_file=str(HTF_CSV),
        mtf_file=str(MTF_CSV),
        ltf_file=str(LTF_CSV),
    ).prompt


def test_rendered_candle_legends_state_utc(tmp_path: Path) -> None:
    prompt = _offline_prompt(tmp_path)

    assert "Candle Mentah" in prompt
    assert prompt.count("semua timestamp dalam UTC") == 3


def test_generated_at_and_output_stamp_agree_on_utc(tmp_path: Path) -> None:
    from smc_prompt.output import make_output_path

    moment = datetime(2026, 9, 14, 6, 6, 32, tzinfo=timezone.utc)

    assert cfg.fmt_generated_at(moment) == "2026-09-14T06:06:32Z"
    # Both formatters normalize a non-UTC offset to the same UTC instant.
    offset = moment.astimezone(timezone(timedelta(hours=7)))
    assert cfg.fmt_generated_at(offset) == "2026-09-14T06:06:32Z"
    assert make_output_path("BTCUSDT", "o", offset).name.endswith(
        "-2026-09-14-06-06-32-UTC.md"
    )
