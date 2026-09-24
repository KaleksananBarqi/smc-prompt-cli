from __future__ import annotations

from unittest.mock import patch

import pytest

from smc_prompt.errors import ClipboardUnavailable
from smc_prompt.output import copy_to_clipboard


def test_copy_to_clipboard_success() -> None:
    with patch("pyperclip.copy") as mock_copy:
        copy_to_clipboard("some text")
        mock_copy.assert_called_once_with("some text")


def test_copy_to_clipboard_exception() -> None:
    with patch("pyperclip.copy", side_effect=RuntimeError("no display")):
        with pytest.raises(ClipboardUnavailable, match="RuntimeError: no display") as exc_info:
            copy_to_clipboard("some text")

        assert isinstance(exc_info.value.__cause__, RuntimeError)
