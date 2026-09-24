"""Output filename convention: ``TICKER-YYYY-MM-DD-HH-MM-SS-UTC.md``.

Pins the hyphenated UTC filename introduced for requirement 2 and proves the
stamp is UTC-normalized and filesystem-safe on both Windows and POSIX.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from smc_prompt import config as cfg
from smc_prompt.output import make_output_path

#: ``TICKER-YYYY-MM-DD-HH-MM-SS-UTC.md`` (ticker upper-case alphanumeric).
_FILENAME_RE = re.compile(
    r"^[A-Z0-9]+-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-UTC\.md$"
)

#: Characters illegal on Windows plus the POSIX path separator.
_ILLEGAL = set('<>:"/\\|?*')


def test_make_output_path_exact_name() -> None:
    moment = datetime(2026, 9, 14, 6, 6, 32, tzinfo=timezone.utc)

    path = make_output_path("EURUSD", "out", moment)

    assert path.name == "EURUSD-2026-09-14-06-06-32-UTC.md"


def test_make_output_path_symbol_is_upper_cased() -> None:
    moment = datetime(2026, 9, 14, 6, 6, 32, tzinfo=timezone.utc)

    path = make_output_path("eurusd", "out", moment)

    assert path.name == "EURUSD-2026-09-14-06-06-32-UTC.md"


def test_make_output_path_sanitizes_symbol_path_traversal() -> None:
    moment = datetime(2026, 9, 14, 6, 6, 32, tzinfo=timezone.utc)

    path_fwd = make_output_path("../../etc/passwd", "out", moment)
    assert path_fwd.name == ".._.._ETC_PASSWD-2026-09-14-06-06-32-UTC.md"
    assert ".." not in path_fwd.parent.parts  # Ensure it doesn't traverse up

    path_bwd = make_output_path("..\\..\\windows\\system32", "out", moment)
    assert path_bwd.name == ".._.._WINDOWS_SYSTEM32-2026-09-14-06-06-32-UTC.md"


def test_make_output_path_normalizes_non_utc_offset() -> None:
    """A ``+07:00`` instant stamps the SAME UTC wall clock as its UTC twin."""

    utc_moment = datetime(2026, 9, 14, 6, 6, 32, tzinfo=timezone.utc)
    jakarta = timezone(timedelta(hours=7))
    local_moment = datetime(2026, 9, 14, 13, 6, 32, tzinfo=jakarta)

    assert (
        make_output_path("EURUSD", "out", local_moment).name
        == make_output_path("EURUSD", "out", utc_moment).name
    )


def test_make_output_path_is_filesystem_safe() -> None:
    moment = datetime(2026, 9, 14, 6, 6, 32, tzinfo=timezone.utc)
    name = make_output_path("BTCUSDT", "out", moment).name

    assert _FILENAME_RE.match(name)
    assert not (_ILLEGAL & set(name))
    assert ":" not in name  # colons are illegal on Windows
    assert not name.endswith(".")


def test_fmt_output_stamp_hyphen_is_utc_and_hyphenated() -> None:
    jakarta = timezone(timedelta(hours=7))
    local_moment = datetime(2026, 9, 14, 13, 6, 32, tzinfo=jakarta)

    assert cfg.fmt_output_stamp_hyphen(local_moment) == "2026-09-14-06-06-32-UTC"
