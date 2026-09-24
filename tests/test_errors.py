import pytest
from smc_prompt.errors import (
    DelistedWarning,
    SymbolStatusWarning,
    SmcPromptError,
    ConfigError,
    SymbolNotFoundError,
    NetworkError,
    InsufficientDataError,
    OutputError,
    ClipboardUnavailable,
    exit_code_for,
)

def test_exit_code_for_smc_prompt_error():
    assert exit_code_for(SmcPromptError()) == 1
    assert exit_code_for(ConfigError()) == 2
    assert exit_code_for(SymbolNotFoundError()) == 3
    assert exit_code_for(NetworkError()) == 4
    assert exit_code_for(InsufficientDataError()) == 5
    assert exit_code_for(OutputError()) == 6
    assert exit_code_for(ClipboardUnavailable()) == 6

def test_exit_code_for_fallback():
    # Test with standard exceptions
    assert exit_code_for(ValueError()) == 1
    assert exit_code_for(RuntimeError()) == 1
    assert exit_code_for(Exception()) == 1
    assert exit_code_for(BaseException()) == 1

def test_delisted_warning_message():
    warning = DelistedWarning(symbol="BTCUSDT", count=10)
    assert warning.message() == (
        "Latest 10 candles have zero volume; BTCUSDT "
        "may be delisted or halted. Prompt generated with caution."
    )

def test_symbol_status_warning_message():
    warning = SymbolStatusWarning(symbol="ETHUSDT", status="BREAK")
    assert warning.message() == (
        "Symbol ETHUSDT has exchange status 'BREAK' "
        "(not TRADING); candles may be stale or the market halted. "
        "Prompt generated with caution."
    )
