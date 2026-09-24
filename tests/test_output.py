from __future__ import annotations

from unittest.mock import patch

import pytest

from smc_prompt.errors import ClipboardUnavailable
from smc_prompt.output import copy_to_clipboard, deliver


def test_copy_to_clipboard_success() -> None:
    with patch("pyperclip.copy") as mock_copy:
        copy_to_clipboard("some text")
        mock_copy.assert_called_once_with("some text")


def test_copy_to_clipboard_exception() -> None:
    with patch("pyperclip.copy", side_effect=RuntimeError("no display")):
        with pytest.raises(ClipboardUnavailable, match="RuntimeError: no display") as exc_info:
            copy_to_clipboard("some text")

        assert isinstance(exc_info.value.__cause__, RuntimeError)


@patch("smc_prompt.output.write_output_file")
@patch("smc_prompt.output.copy_to_clipboard")
def test_deliver_success(mock_copy, mock_write) -> None:
    result = deliver("test text", symbol="BTCUSD")

    mock_write.assert_called_once()
    mock_copy.assert_called_once_with("test text")

    assert result.copied_to_clipboard is True
    assert result.clipboard_error is None


@patch("smc_prompt.output.write_output_file")
@patch("smc_prompt.output.copy_to_clipboard")
def test_deliver_clipboard_fallback(mock_copy, mock_write) -> None:
    mock_copy.side_effect = ClipboardUnavailable("xclip not found")

    result = deliver("test text", symbol="BTCUSD")

    mock_write.assert_called_once()
    mock_copy.assert_called_once_with("test text")

    assert result.copied_to_clipboard is False
    assert result.clipboard_error == "xclip not found"
