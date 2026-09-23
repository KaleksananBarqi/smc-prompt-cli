"""Unit tests for the interactive wizard and interactive CLI activation."""

from unittest.mock import MagicMock, patch

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
