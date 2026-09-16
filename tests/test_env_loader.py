"""Tests for the optional ``.env`` credential loader (``smc_prompt.env_loader``).

The suite stays network-free and hermetic: every case writes its own ``.env``
into ``tmp_path`` and snapshot/restores ``os.environ`` so a developer's real
``.env`` (or exported key) can never leak into or out of a test.
"""

from __future__ import annotations

import builtins
import os
from pathlib import Path
from unittest import mock

import pytest

from smc_prompt import cli
from smc_prompt import env_loader
from smc_prompt.errors import ConfigError


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _write_env(tmp_path: Path, body: str, name: str = ".env") -> Path:
    """Create a ``.env`` under ``tmp_path`` and return its path."""

    target = tmp_path / name
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture(autouse=True)
def _isolate_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the provider credentials so each test starts from a clean slate.

    ``monkeypatch`` restores the original values on teardown, so an exported
    key in the developer's shell is untouched by the suite.
    """

    for name in (
        cli.TWELVEDATA_KEY_ENV,
        cli.OANDA_TOKEN_ENV,
        cli.OANDA_ACCOUNT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------
# Loading behaviour
# --------------------------------------------------------------------------


def test_load_env_file_populates_missing_variables(tmp_path: Path) -> None:
    """Variables absent from the environment are read out of the file."""

    target = _write_env(
        tmp_path,
        f"{cli.TWELVEDATA_KEY_ENV}=file-key\n{cli.OANDA_TOKEN_ENV}=file-token\n",
    )

    result = env_loader.load_env_file(target)

    assert result.status == env_loader.STATUS_LOADED
    assert result.loaded == 2
    assert os.environ[cli.TWELVEDATA_KEY_ENV] == "file-key"
    assert os.environ[cli.OANDA_TOKEN_ENV] == "file-token"
    assert not result.is_warning


def test_load_env_file_ignores_blank_and_comment_lines(tmp_path: Path) -> None:
    """Comment and empty lines do not produce variables."""

    target = _write_env(
        tmp_path,
        "# a comment\n"
        "\n"
        f"{cli.OANDA_ACCOUNT_ENV}=\n"
        f"{cli.TWELVEDATA_KEY_ENV}=only-real-value\n",
    )

    env_loader.load_env_file(target)

    assert os.environ[cli.TWELVEDATA_KEY_ENV] == "only-real-value"
    # An empty assignment loads as an empty string, which ``_env_value`` in
    # cli.py correctly treats as "not provided".
    assert cli._env_value(cli.OANDA_ACCOUNT_ENV) is None


def test_load_env_file_missing_file_is_silent(tmp_path: Path) -> None:
    """A absent .env is the normal case: no warning, no variables."""

    result = env_loader.load_env_file(tmp_path / "does-not-exist.env")

    assert result.status == env_loader.STATUS_MISSING_FILE
    assert not result.is_warning


# --------------------------------------------------------------------------
# Precedence: shell environment must beat the file
# --------------------------------------------------------------------------


def test_load_env_file_does_not_override_existing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``override=False`` keeps flag > shell-env > .env intact."""

    monkeypatch.setenv(cli.TWELVEDATA_KEY_ENV, "shell-key")
    target = _write_env(tmp_path, f"{cli.TWELVEDATA_KEY_ENV}=file-key\n")

    result = env_loader.load_env_file(target)

    assert result.status == env_loader.STATUS_LOADED
    # Nothing was written because the shell already supplied the value.
    assert result.loaded == 0
    assert os.environ[cli.TWELVEDATA_KEY_ENV] == "shell-key"


def test_cli_flag_still_beats_both_environment_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end precedence check across the whole chain."""

    monkeypatch.setenv(cli.TWELVEDATA_KEY_ENV, "shell-key")
    target = _write_env(tmp_path, f"{cli.TWELVEDATA_KEY_ENV}=file-key\n")
    env_loader.load_env_file(target)

    key, _token, _account = cli._resolve_credentials("flag-key", None, None)

    assert key == "flag-key"


def test_file_supplies_key_when_flag_and_shell_are_absent(tmp_path: Path) -> None:
    """The file is the weakest source but still a working one."""

    target = _write_env(tmp_path, f"{cli.TWELVEDATA_KEY_ENV}=file-key\n")
    env_loader.load_env_file(target)

    key, _token, _account = cli._resolve_credentials(None, None, None)

    assert key == "file-key"


# --------------------------------------------------------------------------
# Opt-outs
# --------------------------------------------------------------------------


def test_no_dotenv_disables_loading(tmp_path: Path) -> None:
    """``--no-dotenv`` short-circuits before touching the filesystem."""

    target = _write_env(tmp_path, f"{cli.TWELVEDATA_KEY_ENV}=file-key\n")

    result = env_loader.load_env_file(target, enabled=False)

    assert result.status == env_loader.STATUS_DISABLED
    assert cli.TWELVEDATA_KEY_ENV not in os.environ


def test_explicit_env_file_path_is_honoured(tmp_path: Path) -> None:
    """``--env-file`` reads a file other than the default ``.env``."""

    target = _write_env(
        tmp_path, f"{cli.OANDA_TOKEN_ENV}=alt-token\n", name="staging.env"
    )

    result = env_loader.load_env_file(target)

    assert result.status == env_loader.STATUS_LOADED
    assert result.path == str(target)
    assert os.environ[cli.OANDA_TOKEN_ENV] == "alt-token"


# --------------------------------------------------------------------------
# Graceful degradation when python-dotenv is unavailable
# --------------------------------------------------------------------------


def test_missing_python_dotenv_warns_without_crashing(tmp_path: Path) -> None:
    """An existing .env plus no python-dotenv => WARN, exit code stays 0."""

    target = _write_env(tmp_path, f"{cli.TWELVEDATA_KEY_ENV}=file-key\n")
    real_import = builtins.__import__

    def _deny_dotenv(name: str, *args: object, **kwargs: object):
        if name == "dotenv" or name.startswith("dotenv."):
            raise ImportError("No module named 'dotenv'")
        return real_import(name, *args, **kwargs)

    with mock.patch.object(builtins, "__import__", side_effect=_deny_dotenv):
        result = env_loader.load_env_file(target)

    assert result.status == env_loader.STATUS_MISSING_DEPENDENCY
    assert result.is_warning
    # The warning names the file so the user knows what was skipped.
    assert str(target) in result.warning_message()
    # Nothing was loaded, but no exception escaped.
    assert cli.TWELVEDATA_KEY_ENV not in os.environ


def test_cli_warn_helper_emits_missing_dependency_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``cli._load_dotenv_or_warn`` surfaces the WARN on stderr."""

    target = _write_env(tmp_path, f"{cli.TWELVEDATA_KEY_ENV}=file-key\n")
    real_import = builtins.__import__

    def _deny_dotenv(name: str, *args: object, **kwargs: object):
        if name == "dotenv" or name.startswith("dotenv."):
            raise ImportError("No module named 'dotenv'")
        return real_import(name, *args, **kwargs)

    with mock.patch.object(builtins, "__import__", side_effect=_deny_dotenv):
        cli._load_dotenv_or_warn(str(target), no_dotenv=False)

    captured = capsys.readouterr()
    assert "python-dotenv is not installed" in captured.err


def test_missing_python_dotenv_is_silent_without_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No .env means no warning even when python-dotenv is absent."""

    cli._load_dotenv_or_warn(str(tmp_path / "nope.env"), no_dotenv=False)

    captured = capsys.readouterr()
    assert captured.err == ""


def test_provider_still_rejects_empty_key_after_loading(
    tmp_path: Path,
) -> None:
    """A .env with no key leaves the fail-fast ConfigError in place."""

    env_loader.load_env_file(_write_env(tmp_path, "# nothing here\n"))

    with pytest.raises(ConfigError):
        cli.run(
            "XAUUSD",
            htf_candles=60,
            mtf_candles=120,
            ltf_candles=100,
            swing_lookback=5,
            distance_reference="nearest",
            include_atr=True,
            output_dir=str(tmp_path),
            provider="twelvedata",
            twelvedata_key=None,
        )
