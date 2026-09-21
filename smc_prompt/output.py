"""Deliver the rendered prompt: write the Markdown file, then best-effort clipboard.

The ``.md`` file is the primary artifact and is **always** written to
``<output_dir>/<SYMBOL>-<YYYY-MM-DD-HH-MM-SS-UTC>.md``; failing to write it raises
:class:`OutputError` (exit 6). Copying to the clipboard happens afterwards and is
best-effort: when ``pyperclip`` fails (e.g. a headless system without
xclip/xsel) the failure is surfaced as a warning while the exit code stays 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyperclip

from . import config as cfg
from .errors import ClipboardUnavailable, OutputError


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a delivery attempt."""

    output_path: Path
    copied_to_clipboard: bool
    clipboard_error: str | None

    @property
    def clipboard_warning(self) -> str | None:
        """Warning text when the clipboard copy failed, else ``None``."""

        if self.clipboard_error is None:
            return None
        return (
            f"Clipboard unavailable ({self.clipboard_error}). "
            f"Prompt written to {self.output_path}."
        )


def make_output_path(
    symbol: str,
    output_dir: str,
    moment: datetime,
    *,
    is_review: bool = False,
    is_validation: bool = False,
) -> Path:
    """``<output_dir>/<SYMBOL>-[REVIEW-|VALIDATION-]<YYYY-MM-DD-HH-MM-SS-UTC>.md``.

    The stamp is always normalized to UTC and uses hyphen separators (no
    colons), so the filename is filesystem-safe on Windows and POSIX. Example:
    ``EURUSD-2026-09-14-06-06-32-UTC.md`` or
    ``EURUSD-REVIEW-2026-09-14-06-06-32-UTC.md`` or
    ``EURUSD-VALIDATION-2026-09-14-06-06-32-UTC.md``.
    """

    stamp = cfg.fmt_output_stamp_hyphen(moment)
    if is_validation:
        prefix = f"{symbol.upper()}-VALIDATION"
    elif is_review:
        prefix = f"{symbol.upper()}-REVIEW"
    else:
        prefix = symbol.upper()
    return Path(output_dir) / f"{prefix}-{stamp}.md"


def copy_to_clipboard(text: str) -> None:
    """Copy ``text`` to the system clipboard.

    Raises :class:`ClipboardUnavailable` when ``pyperclip`` reports failure.
    """

    try:
        pyperclip.copy(text)
    except Exception as exc:  # pyperclip raises various platform exceptions
        raise ClipboardUnavailable(f"{type(exc).__name__}: {exc}") from exc


def write_output_file(path: Path, text: str) -> Path:
    """Write the prompt to ``path``, creating parent directories as needed.

    Raises :class:`OutputError` (exit 6) when the file cannot be written.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        raise OutputError(
            f"Could not write prompt to {path} ({exc})."
        ) from exc
    return path


def deliver(
    text: str,
    *,
    symbol: str,
    output_dir: str = cfg.DEFAULT_OUTPUT_DIR,
    moment: datetime | None = None,
    is_review: bool = False,
    is_validation: bool = False,
) -> DeliveryResult:
    """Write the text to a ``.md`` file, then best-effort copy to clipboard."""

    path = make_output_path(
        symbol,
        output_dir,
        moment or datetime.now(timezone.utc),
        is_review=is_review,
        is_validation=is_validation,
    )
    write_output_file(path, text)

    clipboard_error: str | None = None
    try:
        copy_to_clipboard(text)
    except ClipboardUnavailable as exc:
        clipboard_error = str(exc)

    return DeliveryResult(path, clipboard_error is None, clipboard_error)


