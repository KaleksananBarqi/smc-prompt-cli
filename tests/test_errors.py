from __future__ import annotations

from smc_prompt.errors import (
    EXIT_CODE_MAP,
    ClipboardUnavailable,
    ConfigError,
    DelistedWarning,
    InsufficientDataError,
    NetworkError,
    OutputError,
    SmcPromptError,
    SymbolNotFoundError,
    SymbolStatusWarning,
    exit_code_for,
)


def test_exception_exit_codes() -> None:
    """Exception classes should have the correct exit code constants."""
    assert SmcPromptError.exit_code == 1
    assert ConfigError.exit_code == 2
    assert SymbolNotFoundError.exit_code == 3
    assert NetworkError.exit_code == 4
    assert InsufficientDataError.exit_code == 5
    assert OutputError.exit_code == 6
    assert ClipboardUnavailable.exit_code == 6


def test_delisted_warning_message() -> None:
    """DelistedWarning should format the message string correctly."""
    warning = DelistedWarning(symbol="TESTUSDT", count=15)
    msg = warning.message()
    assert "15" in msg
    assert "TESTUSDT" in msg
    assert "zero volume" in msg


def test_symbol_status_warning_message() -> None:
    """SymbolStatusWarning should format the message string correctly."""
    warning = SymbolStatusWarning(symbol="BREAKUSDT", status="BREAK")
    msg = warning.message()
    assert "BREAKUSDT" in msg
    assert "'BREAK'" in msg
    assert "exchange status" in msg


def test_exit_code_for_known_errors() -> None:
    """exit_code_for should extract the mapped exit code for our exceptions."""
    assert exit_code_for(SmcPromptError()) == 1
    assert exit_code_for(ConfigError()) == 2
    assert exit_code_for(NetworkError()) == 4
    assert exit_code_for(ClipboardUnavailable()) == 6


def test_exit_code_for_unknown_errors() -> None:
    """exit_code_for should fallback to the base code (1) for other exceptions."""
    assert exit_code_for(ValueError("Oops")) == 1
    assert exit_code_for(Exception("Generic failure")) == 1
    assert exit_code_for(KeyboardInterrupt()) == 1


def test_exit_code_map_contains_all_fatal_errors() -> None:
    """EXIT_CODE_MAP should cover all the main fatal CLI exceptions."""
    assert EXIT_CODE_MAP[SmcPromptError] == 1
    assert EXIT_CODE_MAP[ConfigError] == 2
    assert EXIT_CODE_MAP[SymbolNotFoundError] == 3
    assert EXIT_CODE_MAP[NetworkError] == 4
    assert EXIT_CODE_MAP[InsufficientDataError] == 5
    assert EXIT_CODE_MAP[OutputError] == 6

    # ClipboardUnavailable is non-fatal and thus usually missing from the CLI
    # top-level map, but we verify it's correctly excluded if that's by design.
    assert ClipboardUnavailable not in EXIT_CODE_MAP
