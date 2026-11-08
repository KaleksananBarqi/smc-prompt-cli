"""Unit tests for the interactive wizard and interactive CLI activation."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from smc_prompt import cli, config as cfg, interactive


def test_cli_launches_interactive_when_no_symbol() -> None:
    runner = CliRunner()
    with patch("smc_prompt.interactive.run_interactive_wizard") as mock_wizard:
        result = runner.invoke(cli.main, [])
        assert result.exit_code == 0
        mock_wizard.assert_called_once()


def test_cli_launches_interactive_with_flag() -> None:
    runner = CliRunner()
    with patch("smc_prompt.interactive.run_interactive_wizard") as mock_wizard:
        result = runner.invoke(cli.main, ["-i"])
        assert result.exit_code == 0
        mock_wizard.assert_called_once()


def test_interactive_wizard_default_run() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        # Step 1: Provider choice (1 -> binance)
        # Step 2: Symbol prompt (BTCUSDT)
        # Step 3: Action choice (1 -> prompt)
        mock_prompt.side_effect = [
            "1",         # Provider: Binance
            "BTCUSDT",   # Symbol
            "1",         # Action: SMC/ICT prompt
        ]
        # Step 4: Use default timeframe? (True)
        # Step 5: Print stdout? (False)
        # Final: Execute? (True)
        mock_confirm.side_effect = [
            True,   # default timeframe
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "BTCUSDT"
        assert kwargs["provider"] == "binance"
        assert kwargs["htf_interval"] == cfg.HTF_INTERVAL
        assert kwargs["mtf_interval"] == cfg.MTF_INTERVAL
        assert kwargs["ltf_interval"] == cfg.LTF_INTERVAL


def test_interactive_wizard_review_with_journal() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "1",         # Provider: Binance
            "BTCUSDT",   # Symbol
            "2",         # Action: Review Pasca-Trade
            "1",         # Arah: Long
            "65000",     # Level Entry
            "64500",     # Stop Loss
            "66500",     # Take Profit
            "1",         # Hasil: Hit TP
            "66500",     # Level Exit / Harga Hit (default TP, tekan Enter / konfirmasi)
            "Reaksi FVG mantap",  # Catatan
        ]
        mock_confirm.side_effect = [
            True,   # default review interval & candles
            True,   # fill journal?
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "BTCUSDT"
        assert kwargs["candles_only"] is True
        assert kwargs["direction"] == "long"
        assert kwargs["entry"] == 65000.0
        assert kwargs["sl"] == 64500.0
        assert kwargs["tp"] == 66500.0
        assert kwargs["outcome"] == "Hit TP"
        assert kwargs["exit_price"] == 66500.0
        assert kwargs["notes"] == "Reaksi FVG mantap"


def test_interactive_wizard_review_skip_journal() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "1",         # Provider: Binance
            "BTCUSDT",   # Symbol
            "2",         # Action: Review Pasca-Trade
        ]
        mock_confirm.side_effect = [
            True,   # default review interval & candles
            False,  # fill journal? (Skip)
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "BTCUSDT"
        assert kwargs["candles_only"] is True
        assert kwargs["direction"] is None
        assert kwargs["entry"] is None
        assert kwargs["sl"] is None
        assert kwargs["tp"] is None
        assert kwargs["outcome"] is None
        assert kwargs["notes"] is None


def test_interactive_wizard_bitunix_selection() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "2",         # Provider: Bitunix
            "ETHUSDT",   # Symbol
            "1",         # Action: SMC/ICT prompt
        ]
        mock_confirm.side_effect = [
            True,   # default timeframe
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "ETHUSDT"
        assert kwargs["provider"] == "bitunix"


def test_interactive_wizard_abort_before_execution() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "1",
            "BTCUSDT",
            "1",
        ]
        mock_confirm.side_effect = [
            True,   # default timeframe
            False,  # print stdout
            False,  # Abort execute!
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_not_called()


def test_cli_dry_run_with_bitunix() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.main, ["BTCUSDT", "--provider", "bitunix", "--dry-run"])
    assert result.exit_code == 0
    assert "provider=bitunix" in result.output


def test_cli_loads_dotenv_before_interactive() -> None:
    runner = CliRunner()
    with patch("smc_prompt.interactive.run_interactive_wizard") as mock_wizard, \
         patch("smc_prompt.cli._load_dotenv_or_warn") as mock_dotenv:
        result = runner.invoke(cli.main, [])
        assert result.exit_code == 0
        mock_dotenv.assert_called_once_with(None, no_dotenv=False)
        mock_wizard.assert_called_once()


def test_interactive_wizard_mexc_selection() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "3",         # Provider: MEXC
            "SOLUSDT",   # Symbol
            "1",         # Action: SMC/ICT prompt
        ]
        mock_confirm.side_effect = [
            True,   # default timeframe
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "SOLUSDT"
        assert kwargs["provider"] == "mexc"


def test_interactive_wizard_gate_selection() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "4",         # Provider: Gate.io
            "DOGEUSDT",  # Symbol
            "1",         # Action: SMC/ICT prompt
        ]
        mock_confirm.side_effect = [
            True,   # default timeframe
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "DOGEUSDT"
        assert kwargs["provider"] == "gate"


def test_interactive_wizard_twelvedata_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", "mock-env-key")
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "5",         # Provider: Twelve Data
            "XAUUSD",    # Symbol
            "1",         # Action: SMC/ICT prompt
        ]
        mock_confirm.side_effect = [
            True,   # default timeframe
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "XAUUSD"
        assert kwargs["provider"] == "twelvedata"
        assert kwargs["twelvedata_key"] is None


def test_interactive_wizard_twelvedata_prompts_when_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "5",               # Provider: Twelve Data
            "XAUUSD",          # Symbol
            "manual-td-key",   # Twelve Data API Key prompted manually
            "1",               # Action: SMC/ICT prompt
        ]
        mock_confirm.side_effect = [
            True,   # default timeframe
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "XAUUSD"
        assert kwargs["provider"] == "twelvedata"
        assert kwargs["twelvedata_key"] == "manual-td-key"


def test_interactive_wizard_validate_mode_successful() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "1",         # Provider: Binance
            "BTCUSDT",   # Symbol
            "3",         # Action: Validasi Setup
            75993.8,     # Entry 1
            "76173.5",   # Entry 2
            75158.5,     # TP1
            "74909.4",   # TP2
            "76330.0",   # SL
            "1",         # Status: unfilled
        ]
        mock_confirm.side_effect = [
            True,   # default validate interval (15m, 50)
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "BTCUSDT"
        assert kwargs["validate_setup"] is True
        assert kwargs["entry"] == 75993.8
        assert kwargs["entry2"] == 76173.5
        assert kwargs["tp"] == 75158.5
        assert kwargs["tp2"] == 74909.4
        assert kwargs["sl"] == 76330.0
        assert kwargs["order_status"] == "unfilled"


def test_interactive_wizard_validate_mode_retries_on_invalid_setup() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "1",         # Provider: Binance
            "BTCUSDT",   # Symbol
            "3",         # Action: Validasi Setup
            # Putaran 1 (invalid: SL 76330 < Entry 759938)
            759938.0,    # Entry 1 (typo)
            "",          # Entry 2 (skip)
            751585.0,    # TP1 (typo)
            "",          # TP2 (skip)
            "76330.0",   # SL
            # Putaran 2 (valid)
            75993.8,     # Entry 1
            "",          # Entry 2
            75158.5,     # TP1
            "",          # TP2
            "76330.0",   # SL
            "1",         # Status: unfilled
        ]
        mock_confirm.side_effect = [
            True,   # Masukkan ulang parameter setup? (retry = True)
            True,   # default validate interval (15m, 50)
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert kwargs["entry"] == 75993.8
        assert kwargs["tp"] == 75158.5
        assert kwargs["sl"] == 76330.0


def test_interactive_wizard_handles_smc_prompt_error() -> None:
    from smc_prompt.errors import ConfigError

    with patch("smc_prompt.cli.run", side_effect=ConfigError("Invalid test config")), \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "1",         # Provider: Binance
            "BTCUSDT",   # Symbol
            "1",         # Action: SMC/ICT prompt
        ]
        mock_confirm.side_effect = [
            True,   # default timeframe
            False,  # print stdout
            True,   # execute run
        ]

        with pytest.raises(SystemExit) as exc_info:
            interactive.run_interactive_wizard()
        assert exc_info.value.code == 2


def test_format_cli_command_scenarios() -> None:
    from smc_prompt.interactive import format_cli_command

    # 1. Default prompt mode
    cmd = format_cli_command("btcusdt")
    assert cmd == "smc-prompt BTCUSDT"

    # 2. Prompt mode dengan custom timeframe dan provider
    cmd_custom = format_cli_command(
        "ethusdt",
        provider="bitunix",
        htf_interval="1w",
        mtf_interval="1d",
        ltf_interval="4h",
        swing_lookback=7,
        print_stdout=True,
    )
    assert cmd_custom == (
        "smc-prompt ETHUSDT --provider bitunix --htf-interval 1w "
        "--mtf-interval 1d --ltf-interval 4h --swing-lookback 7 --stdout"
    )

    # 3. CSV provider
    cmd_csv = format_cli_command("solusdt", provider="csv", input_csv="/tmp/sol.csv")
    assert cmd_csv == "smc-prompt SOLUSDT --input-csv /tmp/sol.csv"

    # 4. Review mode dengan dual-entry dan exit
    cmd_review = format_cli_command(
        "btcusdt",
        action="review",
        review_interval="15m",
        review_candles=100,
        review_direction="long",
        review_entry=65000.0,
        review_entry2=64800.0,
        review_sl=64000.0,
        review_tp=67000.0,
        review_tp2=68000.0,
        review_outcome="Hit TP",
        review_exit=67000.0,
        review_notes='Sweep liquidity "Asia low"',
    )
    assert "--candles-only" in cmd_review
    assert "--review-interval 15m" in cmd_review
    assert "--review-candles 100" in cmd_review
    assert "--direction long" in cmd_review
    assert "--entry 65000.0" in cmd_review
    assert "--entry2 64800.0" in cmd_review
    assert "--sl 64000.0" in cmd_review
    assert "--tp 67000.0" in cmd_review
    assert "--tp2 68000.0" in cmd_review
    assert '--outcome "Hit TP"' in cmd_review
    assert "--exit 67000.0" in cmd_review
    assert r'--notes "Sweep liquidity \"Asia low\""' in cmd_review

    # 5. Validate mode dengan dual entry dan status
    cmd_val = format_cli_command(
        "btcusdt",
        action="validate",
        entry_price=65000.0,
        entry2_price=64800.0,
        tp_price=67000.0,
        tp2_price=68000.0,
        sl_price=64000.0,
        order_status="unfilled",
        validate_interval="5m",
        validate_candles=40,
    )
    assert "--validate-setup" in cmd_val
    assert "--entry 65000.0" in cmd_val
    assert "--entry2 64800.0" in cmd_val
    assert "--tp 67000.0" in cmd_val
    assert "--tp2 68000.0" in cmd_val
    assert "--sl 64000.0" in cmd_val
    assert "--unfilled" in cmd_val
    assert "--validate-interval 5m" in cmd_val
    assert "--validate-candles 40" in cmd_val


def test_interactive_wizard_review_dual_entry_support() -> None:
    with patch("smc_prompt.cli.run") as mock_run, \
         patch("click.prompt") as mock_prompt, \
         patch("click.confirm") as mock_confirm:

        mock_prompt.side_effect = [
            "1",                 # Provider: Binance
            "BTCUSDT",           # Symbol
            "2",                 # Action: Review
            "1",                 # Arah: Long
            "65000, 64800",      # Level Entry (dual entry)
            "64000",             # Stop Loss
            "67000, 68000",      # Take Profit (dual TP)
            "1",                 # Hasil: Hit TP
            "67000",             # Exit level
            "Catatan review",    # Notes
        ]
        mock_confirm.side_effect = [
            True,   # default review interval & candles
            True,   # fill journal?
            False,  # print stdout
            True,   # execute run
        ]

        interactive.run_interactive_wizard()
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["entry"] == 65000.0
        assert kwargs["entry2"] == 64800.0
        assert kwargs["tp"] == 67000.0
        assert kwargs["tp2"] == 68000.0
        assert kwargs["sl"] == 64000.0
        assert kwargs["direction"] == "long"



