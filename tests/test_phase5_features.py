"""Tests for the Phase 5 additions (scope #6, #11, #16).

Covers the ATR-normalized distance / ATR-as-%-of-price facts (#6), the
post-render prompt-size guard (#11), and the usability items: ``--dry-run``,
the volume-derived signals, and the regex-based unresolved-placeholder guard.

All tests are network-free.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from smc_prompt import cli
from smc_prompt import config as cfg
from smc_prompt.errors import ConfigError, NetworkError
from smc_prompt.models import (
    Candle,
    StructureClass,
    SwingPoint,
    SwingType,
    TimeframeAnalysis,
)
from smc_prompt.structure_analyzer import compute_relative_volume
from smc_prompt.template_renderer import (
    _atr_normalized_distance,
    _atr_pct_of_price,
    _volume_relative,
    _volume_spike,
    build_payload,
    check_unresolved_placeholders,
    render,
)

from .conftest import HTF_CSV, LTF_CSV, MTF_CSV, make_candle, make_swing


# --------------------------------------------------------------------------
# #6 ATR-normalized distance + ATR as % of price
# --------------------------------------------------------------------------


def test_fmt_atr_distance_basic() -> None:
    assert cfg.fmt_atr_distance(Decimal("105"), Decimal("100"), Decimal("2")) == "x2.50"


def test_fmt_atr_distance_uses_absolute_value() -> None:
    # Direction is carried by the sibling percent field; this ratio is unsigned.
    assert cfg.fmt_atr_distance(Decimal("95"), Decimal("100"), Decimal("2")) == "x2.50"


def test_atr_normalized_distance_guards_zero_and_none() -> None:
    assert _atr_normalized_distance(Decimal("100"), Decimal("110"), Decimal("0")) == "n/a"
    assert _atr_normalized_distance(Decimal("100"), Decimal("110"), None) == "n/a"


def test_fmt_atr_pct_of_price() -> None:
    assert cfg.fmt_atr_pct(Decimal("1067.24"), Decimal("61791.35")) == "1.73% of price"


def test_atr_pct_of_price_guards_zero_atr_and_price() -> None:
    analysis = _analysis(atr=Decimal("0"))
    assert _atr_pct_of_price(analysis, Decimal("100")) == "n/a"
    analysis = _analysis(atr=Decimal("5"))
    assert _atr_pct_of_price(analysis, Decimal("0")) == "n/a"


# --------------------------------------------------------------------------
# #16 Volume-derived signals
# --------------------------------------------------------------------------


def _volume_series(volumes: list[str]) -> list[Candle]:
    return [
        make_candle(i, str(100 + i), str(90 + i), volume=v)
        for i, v in enumerate(volumes)
    ]


def test_relative_volume_last_over_mean() -> None:
    candles = _volume_series(["10", "10", "10", "10", "20"])

    metrics = compute_relative_volume(candles, period=20)

    assert metrics is not None
    assert metrics.relative == Decimal("1.6667")  # 20 / mean(12) quantized
    assert metrics.window == 5
    assert metrics.last == Decimal("20")


def test_relative_volume_spike_flag() -> None:
    candles = _volume_series(["10", "10", "10", "10", "100"])

    metrics = compute_relative_volume(candles, spike_mult=Decimal("1.5"))

    assert metrics is not None
    assert metrics.is_spike is True


def test_relative_volume_not_spike() -> None:
    candles = _volume_series(["10", "10", "10", "10", "10"])

    metrics = compute_relative_volume(candles, spike_mult=Decimal("1.5"))

    assert metrics is not None
    assert metrics.relative == Decimal("1.0000")
    assert metrics.is_spike is False


def test_relative_volume_zero_mean_is_safe() -> None:
    candles = _volume_series(["0", "0", "0"])

    metrics = compute_relative_volume(candles, period=20)

    assert metrics is not None
    assert metrics.relative == Decimal("0")
    assert metrics.is_spike is False


def test_relative_volume_needs_two_candles() -> None:
    assert compute_relative_volume(_volume_series(["10"])) is None
    assert compute_relative_volume([]) is None


def test_fmt_relative_volume_two_decimals() -> None:
    assert cfg.fmt_relative_volume(Decimal("1.4")) == "x1.40"
    assert cfg.fmt_relative_volume(Decimal("0.5")) == "x0.50"


def test_volume_renderers_handle_missing_metrics() -> None:
    analysis = _analysis()
    assert _volume_relative(analysis) == "n/a"
    assert _volume_spike(analysis) == "unknown"


# --------------------------------------------------------------------------
# #16 Regex unresolved-placeholder guard
# --------------------------------------------------------------------------


def test_check_unresolved_placeholders_flags_tags() -> None:
    assert check_unresolved_placeholders("a {{PAIR}} b") == ["PAIR"]
    assert check_unresolved_placeholders("a {{ PAIR }} b") == ["PAIR"]
    assert check_unresolved_placeholders("no tags here") == []


def test_check_unresolved_placeholders_allows_literal_braces() -> None:
    # A literal ``{{`` / ``}}`` in the body must NOT false-positive; only a tag
    # naming a declared placeholder is reported.
    text = "range {{100}} and literal }} and {{not_a_placeholder"
    assert check_unresolved_placeholders(text, {"PAIR"}) == []


def test_check_unresolved_placeholders_only_declared() -> None:
    # Only a tag naming a DECLARED placeholder is reported; an undeclared tag is
    # already rejected by StrictUndefined during the render itself.
    text = "{{PAIR}} {{UNKNOWN}}"
    assert check_unresolved_placeholders(text, {"PAIR"}) == ["PAIR"]


# --------------------------------------------------------------------------
# #11 Prompt-size guard
# --------------------------------------------------------------------------


def test_prompt_size_notes_warns_when_over_threshold() -> None:
    config = replace(cfg.build_config("TESTUSDT"), prompt_bytes_warn=10)

    notes = cli._prompt_size_notes("x" * 100, config)

    assert len(notes) == 1
    assert "100 bytes" in notes[0]
    assert "~25 tokens" in notes[0]


def test_prompt_size_notes_silent_under_threshold() -> None:
    config = replace(cfg.build_config("TESTUSDT"), prompt_bytes_warn=1000)

    assert cli._prompt_size_notes("x" * 10, config) == []


def test_prompt_size_hard_limit_raises_config_error() -> None:
    config = replace(cfg.build_config("TESTUSDT"), max_prompt_bytes=10)

    with pytest.raises(ConfigError, match="exceeding --max-prompt-bytes"):
        cli._prompt_size_notes("x" * 100, config)


def test_cli_max_prompt_bytes_offline_aborts(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "BTCUSDT",
            "--input-csv",
            str(HTF_CSV),
            "--htf-file",
            str(HTF_CSV),
            "--ltf-file",
            str(LTF_CSV),
            "--output-dir",
            str(tmp_path),
            "--max-prompt-bytes",
            "10",
        ],
    )

    assert result.exit_code == 2
    assert "--max-prompt-bytes" in result.output
    assert not list(tmp_path.glob("BTCUSDT-*.md"))


# --------------------------------------------------------------------------
# #16 --dry-run
# --------------------------------------------------------------------------


def test_dry_run_does_not_fetch_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry-run must not construct a data source")

    monkeypatch.setattr("smc_prompt.cli.DataFetcher", _boom)
    monkeypatch.setattr("smc_prompt.cli.LocalCsvSource", _boom)

    result = cli.run(
        "BTCUSDT",
        htf_candles=60,
        ltf_candles=100,
        swing_lookback=5,
        distance_reference="nearest",
        include_atr=True,
        output_dir=str(tmp_path),
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.prompt == ""
    assert result.output_path == ""
    assert not list(tmp_path.glob("BTCUSDT-*.md"))


def test_dry_run_cli_exits_zero_and_prints_settings(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["BTCUSDT", "--dry-run", "--output-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "symbol=BTCUSDT" in result.output
    assert not list(tmp_path.glob("BTCUSDT-*.md"))


def test_dry_run_still_validates_symbol_argument() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.main, ["", "--dry-run"])

    assert result.exit_code == 2


def test_run_has_no_debug_parameter() -> None:
    import inspect

    assert "debug" not in inspect.signature(cli.run).parameters


# --------------------------------------------------------------------------
# Placeholder contract: every declared required placeholder is populated
# --------------------------------------------------------------------------


def _offline_prompt(tmp_path: Path) -> str:
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


def test_offline_prompt_renders_phase5_facts(tmp_path: Path) -> None:
    prompt = _offline_prompt(tmp_path)

    assert "- ATR ≈ " in prompt
    assert "% of price (ATR(14) HTF)" in prompt
    assert "×ATR(14))" in prompt  # ATR-normalized distance appended per swing
    assert "Volume candle terakhir" in prompt
    assert "spike:" in prompt
    # The ATR-normalized distance is rendered as a single x-prefixed ratio.
    assert "x" in prompt


def test_render_accepts_literal_braces_in_payload() -> None:
    from datetime import datetime, timezone

    from smc_prompt.models import ReferenceFacts

    config = cfg.build_config("TESTUSDT")
    htf = _analysis(atr=Decimal("100"), price=Decimal("1000"))
    mtf = _analysis(atr=Decimal("50"), price=Decimal("1000"))
    ltf = _analysis(atr=Decimal("25"), price=Decimal("1000"))
    facts = ReferenceFacts(
        recent_high="1000.00 pada x (DI ATAS harga), status: untested",
        recent_low="900.00 pada x (DI BAWAH harga), status: untested",
        nearest_high="1000.00 pada x (DI ATAS harga), status: untested",
        nearest_low="900.00 pada x (DI BAWAH harga), status: untested",
        window_high="1000.00 pada x (DI ATAS harga), status: untested",
        window_low="900.00 pada x (DI BAWAH harga), status: untested",
    )
    htf = replace(htf, reference_facts=facts)
    mtf = replace(mtf, reference_facts=facts)
    ltf = replace(ltf, reference_facts=facts)

    payload = build_payload(
        symbol="TESTUSDT",
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        current_price=Decimal("1000"),
        htf=htf,
        mtf=mtf,
        ltf=ltf,
        htf_candles=_volume_series(["10", "10", "20"]),
        mtf_candles=_volume_series(["10", "10", "25"]),
        ltf_candles=_volume_series(["10", "10", "30"]),
        config=config,
    )
    # Injecting a literal ``{{`` into a value must not trip the guard (the guard
    # runs on the rendered text after substitution).
    payload["PAIR"] = "{{LITERAL}}"

    rendered = render(payload)

    assert "{{LITERAL}}" in rendered.text


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _dt():
    from datetime import datetime, timezone

    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _swing(index: int, price: str, swing_type: SwingType) -> SwingPoint:
    from datetime import timedelta

    return SwingPoint(
        index=index,
        open_time=_dt() + timedelta(hours=index),
        price=Decimal(price),
        type=swing_type,
    )


def _analysis(
    *,
    atr: Decimal | None = None,
    price: Decimal = Decimal("100"),
    timeframe: str = "1h",
) -> TimeframeAnalysis:
    high = _swing(0, str(price), SwingType.HIGH)
    low = _swing(1, str(price - Decimal("10")), SwingType.LOW)
    return TimeframeAnalysis(
        timeframe=timeframe,
        structure_class=StructureClass.RANGING,
        swing_high=high,
        swing_low=low,
        dist_to_high=type("D", (), {"reference": high, "text": ""})(),
        dist_to_low=type("D", (), {"reference": low, "text": ""})(),
        atr=atr,
        candle_count=3,
        swings=(high, low),
    )


# --------------------------------------------------------------------------
# 3-timeframe support (MTF tier)
# --------------------------------------------------------------------------


def _three_tier_payload(*, include_facts: bool = True) -> dict[str, str]:
    from datetime import datetime, timezone

    from smc_prompt.models import ReferenceFacts

    config = cfg.build_config("TESTUSDT")
    htf = _analysis(atr=Decimal("100"), price=Decimal("1000"), timeframe="1d")
    mtf = _analysis(atr=Decimal("50"), price=Decimal("1000"), timeframe="4h")
    ltf = _analysis(atr=Decimal("25"), price=Decimal("1000"), timeframe="1h")
    if include_facts:
        facts = ReferenceFacts(
            recent_high="1000.00 pada 2026-01-01 00:00 (DI ATAS harga), status: untested",
            recent_low="900.00 pada 2026-01-01 01:00 (DI BAWAH harga), status: untested",
            nearest_high="1000.00 pada 2026-01-01 00:00 (DI ATAS harga), status: untested",
            nearest_low="900.00 pada 2026-01-01 01:00 (DI BAWAH harga), status: untested",
            window_high="1000.00 pada 2026-01-01 00:00 (DI ATAS harga), status: untested",
            window_low="900.00 pada 2026-01-01 01:00 (DI BAWAH harga), status: untested",
        )
        htf = replace(htf, reference_facts=facts)
        mtf = replace(mtf, reference_facts=facts)
        ltf = replace(ltf, reference_facts=facts)
    candles = _volume_series(["10", "10", "20"])
    return build_payload(
        symbol="TESTUSDT",
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        current_price=Decimal("1000"),
        htf=htf,
        mtf=mtf,
        ltf=ltf,
        htf_candles=candles,
        mtf_candles=candles,
        ltf_candles=candles,
        config=config,
    )


def test_build_config_accepts_three_intervals() -> None:
    config = cfg.build_config(
        "TESTUSDT", htf_interval="1d", mtf_interval="4h", ltf_interval="1h"
    )

    assert config.mtf_interval == "4h"
    assert config.mtf_interval_label == "4H"
    assert config.mtf_candles == cfg.DEFAULT_MTF_CANDLES


def test_build_config_rejects_duplicate_intervals() -> None:
    with pytest.raises(ConfigError, match="DISTINCT intervals"):
        cfg.build_config(
            "TESTUSDT", htf_interval="1d", mtf_interval="4h", ltf_interval="4h"
        )
    with pytest.raises(ConfigError, match="DISTINCT intervals"):
        cfg.build_config(
            "TESTUSDT", htf_interval="1h", mtf_interval="1h", ltf_interval="1h"
        )


def test_default_interval_trio_is_distinct() -> None:
    assert len({cfg.HTF_INTERVAL, cfg.MTF_INTERVAL, cfg.LTF_INTERVAL}) == 3


def test_mtf_candles_below_minimum_rejected() -> None:
    with pytest.raises(ConfigError, match="--mtf-candles"):
        cfg.build_config("TESTUSDT", mtf_candles=9)


def test_mtf_fetch_limit_capped() -> None:
    small = cfg.build_config("TESTUSDT", mtf_candles=50)
    assert small.mtf_fetch_limit == 100  # 50 + context_buffer(50)

    big = cfg.build_config("TESTUSDT", mtf_candles=1000)
    assert big.mtf_fetch_limit == cfg.FETCH_LIMIT_MAX


def test_mtf_uses_datetime_stamp() -> None:
    payload = _three_tier_payload()

    # MTF sits below the HTF tier, so it uses the datetime form, not the date.
    assert payload["MTF_SWING_HIGH_DATE"] == "2026-01-01 00:00"
    assert ":" in payload["MTF_SWING_HIGH_DATE"]
    # The first MTF candle row is datetime-stamped.
    assert payload["MTF_CANDLE_TABLE_CSV"].splitlines()[0].startswith(
        "2026-01-01 00:00,"
    )


def test_required_placeholders_all_resolved_for_three_tiers() -> None:
    from smc_prompt.models import REQUIRED_PLACEHOLDERS

    payload = _three_tier_payload()
    mtf_keys = [key for key in REQUIRED_PLACEHOLDERS if key.startswith("MTF_")]

    assert len(mtf_keys) == 26
    assert all(key in payload for key in mtf_keys)
    assert all(str(payload[key]).strip() for key in mtf_keys)
    assert "MTF_ATR14" in payload


def test_analyze_three_series_renders_three_blocks(tmp_path: Path) -> None:
    prompt = _offline_prompt(tmp_path)

    assert "Ringkasan Data HTF" in prompt
    assert "Ringkasan Data MTF" in prompt
    assert "Ringkasan Data LTF" in prompt
    assert "Multi-Timeframe Alignment (HTF, MTF & LTF)" in prompt
    assert "FVG Mekanis MTF" in prompt


def test_no_atr_removes_all_three_atr_lines(tmp_path: Path) -> None:
    from smc_prompt.template_renderer import render

    payload = _three_tier_payload()
    with_atr = render(payload, include_atr=True).text
    without_atr = render(payload, include_atr=False).text

    assert "ATR(14): " not in ""
    assert with_atr.count("ATR(14): ") == 3
    assert without_atr.count("ATR(14): ") == 0
    # Removing the ATR lines drops exactly the three lines and nothing else.
    lines_on = [
        line for line in with_atr.split("\n") if "ATR(14): " not in line
    ]
    lines_off = without_atr.split("\n")
    assert lines_on == lines_off


def test_prompt_size_three_tier_bytes(tmp_path: Path) -> None:
    prompt = _offline_prompt(tmp_path)
    size = len(prompt.encode("utf-8"))

    # Documentation anchor for DESIGN_SPEC §14: the 3-tier render stays well
    # below the warn threshold.
    assert size < cfg.PROMPT_BYTES_WARN
    assert size > 18_684  # strictly larger than the old dual-tier artifact


def test_non_goal_strings_absent(tmp_path: Path) -> None:
    """Tool-emitted data blocks must not contain forbidden SMC/ICT tokens.

    The static instructional body legitimately names these concepts (it asks
    the downstream LLM about them), so the check is scoped to the tool-emitted
    data block headers (which are never emitted by the analyzer).
    """

    prompt = _offline_prompt(tmp_path)

    for token in ("SSL", "BSL"):
        assert f": {token}" not in prompt
        assert f",{token}" not in prompt
    # ``structure_class`` values are never rendered as "bias".
    for line in prompt.split("\n"):
        if "Klasifikasi struktur (mekanis)" in line or "struktur (mekanis)" in line:
            assert "bias" not in line.lower()


# --------------------------------------------------------------------------
# Model label + reasoned Trade Plan (requirement 1)
# --------------------------------------------------------------------------


def test_trade_plan_setup_row_is_blank_for_llm(tmp_path: Path) -> None:
    prompt = _offline_prompt(tmp_path)

    # The setup/model row lives INSIDE section 5 and is left for the LLM to fill.
    plan = prompt.split("## 5. Trading Plan", 1)[1]
    assert "| Setup / Model | [mis. ICT 2022 Model" in plan
    # The tool injects no model label anywhere in the prompt.
    assert "- **Model:**" not in prompt
    assert "MODEL_NAME" not in prompt


def test_trade_plan_requests_named_reasoning(tmp_path: Path) -> None:
    prompt = _offline_prompt(tmp_path)

    for marker in (
        "Alasan Entry",
        "Alasan SL",
        "TP1",
        "R multiple TP1",
        "Alasan TP1",
        "TP2",
        "R multiple TP2",
        "Alasan TP2",
        "RRR aktual (TP1)",
        "RRR aktual (TP2)",
    ):
        assert marker in prompt
