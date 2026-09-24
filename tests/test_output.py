"""Tests for the output delivery module."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from smc_prompt.output import (
    ClipboardUnavailable,
    DeliveryResult,
    OutputError,
    copy_to_clipboard,
    deliver,
    make_output_path,
    write_output_file,
)


def test_delivery_result_clipboard_warning() -> None:
    res1 = DeliveryResult(Path("some/path"), False, "Some error")
    assert (
        res1.clipboard_warning
        == "Clipboard unavailable (Some error). Prompt written to some/path."
    )

    res2 = DeliveryResult(Path("some/path"), True, None)
    assert res2.clipboard_warning is None


def test_copy_to_clipboard_success(mocker) -> None:
    mock_copy = mocker.patch("pyperclip.copy")
    copy_to_clipboard("test text")
    mock_copy.assert_called_once_with("test text")


def test_copy_to_clipboard_failure(mocker) -> None:
    mocker.patch("pyperclip.copy", side_effect=Exception("Test mock exception"))
    with pytest.raises(ClipboardUnavailable, match="Exception: Test mock exception"):
        copy_to_clipboard("test text")


def test_write_output_file_success(tmp_path: Path) -> None:
    out_file = tmp_path / "sub" / "file.txt"
    res = write_output_file(out_file, "hello")
    assert res == out_file
    assert out_file.read_text(encoding="utf-8") == "hello"


def test_write_output_file_failure(mocker) -> None:
    mocker.patch("builtins.open", side_effect=OSError("Permission denied"))
    with pytest.raises(OutputError, match="Permission denied"):
        write_output_file(Path("some/path"), "hello")


def test_deliver_success(mocker, tmp_path: Path) -> None:
    mock_copy = mocker.patch("smc_prompt.output.copy_to_clipboard")

    moment = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    res = deliver("content", symbol="EURUSD", output_dir=str(tmp_path), moment=moment)

    assert res.output_path.parent == tmp_path
    assert res.output_path.name == "EURUSD-2023-01-01-12-00-00-UTC.md"
    assert res.output_path.read_text(encoding="utf-8") == "content"
    assert res.copied_to_clipboard is True
    assert res.clipboard_error is None
    mock_copy.assert_called_once_with("content")


def test_deliver_clipboard_failure(mocker, tmp_path: Path) -> None:
    mocker.patch(
        "smc_prompt.output.copy_to_clipboard",
        side_effect=ClipboardUnavailable("Failed"),
    )

    moment = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    res = deliver("content", symbol="EURUSD", output_dir=str(tmp_path), moment=moment)

    assert res.output_path.exists()
    assert res.copied_to_clipboard is False
    assert res.clipboard_error == "Failed"


def test_make_output_path_is_validation() -> None:
    moment = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    path = make_output_path("EURUSD", "out", moment, is_validation=True)
    assert path.name == "EURUSD-VALIDATION-2023-01-01-12-00-00-UTC.md"


def test_make_output_path_is_review() -> None:
    moment = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    path = make_output_path("EURUSD", "out", moment, is_review=True)
    assert path.name == "EURUSD-REVIEW-2023-01-01-12-00-00-UTC.md"


def test_deliver_uses_current_time_if_moment_none(mocker, tmp_path: Path) -> None:
    mock_now = mocker.patch("smc_prompt.output.datetime")
    mock_now.now.return_value = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mocker.patch("smc_prompt.output.copy_to_clipboard")

    res = deliver("content", symbol="EURUSD", output_dir=str(tmp_path))
    assert res.output_path.name == "EURUSD-2023-01-01-12-00-00-UTC.md"
