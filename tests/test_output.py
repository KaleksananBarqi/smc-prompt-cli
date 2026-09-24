from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from smc_prompt.errors import ClipboardUnavailable, OutputError
from smc_prompt.output import copy_to_clipboard, deliver, write_output_file


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


def test_write_output_file_success(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "out.md"
    text = "Hello world\n"

    returned_path = write_output_file(path, text)

    assert returned_path == path
    assert path.read_text(encoding="utf-8") == "Hello world\n"


def test_write_output_file_mkdir_raises_oserror(tmp_path: Path) -> None:
    path = tmp_path / "out.md"

    with patch("pathlib.Path.mkdir", side_effect=OSError("mkdir failed")):
        with pytest.raises(OutputError) as exc_info:
            write_output_file(path, "text")

    assert f"Could not write prompt to {path} (mkdir failed)." in str(exc_info.value)


def test_write_output_file_open_raises_oserror(tmp_path: Path) -> None:
    path = tmp_path / "out.md"

    with patch("builtins.open", side_effect=OSError("open failed")):
        with pytest.raises(OutputError) as exc_info:
            write_output_file(path, "text")

    assert f"Could not write prompt to {path} (open failed)." in str(exc_info.value)
