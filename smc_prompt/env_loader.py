"""Optional ``.env`` credential loading for the CLI entrypoint only.

Why this module exists
----------------------
``smc-prompt`` reads provider secrets from the environment
(``TWELVEDATA_API_KEY`` / ``OANDA_API_TOKEN`` / ``OANDA_ACCOUNT_ID`` — see
``cli._resolve_credentials``). Exporting them in every shell is tedious, so a
local ``.env`` file is supported as a *convenience surface* underneath the
existing precedence chain::

    CLI flag  >  shell environment  >  .env file

That ordering is guaranteed by calling ``load_dotenv(..., override=False)``:
a variable already present in ``os.environ`` is never rewritten, so a real
shell export always wins over the file, and the flag keeps winning over both
inside :func:`smc_prompt.cli._resolve_credentials`.

Scope: CLI only
---------------
The loader is invoked from :func:`smc_prompt.cli.main`, deliberately **not**
from :func:`smc_prompt.cli.run`. The test suite drives ``run()`` directly (see
``tests/test_providers.py``), so loading a developer's real ``.env`` inside
``run()`` would leak secrets into the suite and break its network-free,
hermetic guarantees.

Graceful degradation
--------------------
``python-dotenv`` is declared as a dependency, but the import is guarded
anyway: when it is unavailable *and* a ``.env`` file is actually present, the
caller receives a ``missing_dependency`` status and emits a WARN (exit code
stays ``0``). A missing library must never silently swallow a file the user
believes is being read, and it must never abort a run either.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Default ``.env`` location: the process working directory. Chosen (rather
#: than the package directory) so a cloned checkout keeps its secrets next to
#: the code the user actually runs.
DEFAULT_ENV_FILE = ".env"

#: Status codes returned by :func:`load_env_file`.
STATUS_LOADED = "loaded"
STATUS_MISSING_FILE = "missing_file"
STATUS_MISSING_DEPENDENCY = "missing_dependency"
STATUS_DISABLED = "disabled"


@dataclass(frozen=True)
class EnvLoadResult:
    """Outcome of one :func:`load_env_file` call.

    ``status`` is one of the ``STATUS_*`` constants. ``path`` is the resolved
    file path the call looked at (always populated, even on failure, so the
    WARN message can name it). ``loaded`` is the number of variables actually
    written into the environment — variables already exported by the shell are
    skipped, so a ``loaded`` of ``0`` on an existing file is normal and not an
    error.
    """

    status: str
    path: str
    loaded: int = 0
    detail: str | None = None

    @property
    def is_warning(self) -> bool:
        """True when the caller should surface a non-fatal WARN.

        Only a genuinely present-but-unreadable file warns; an absent ``.env``
        is the overwhelmingly common case and stays silent.
        """

        return self.status == STATUS_MISSING_DEPENDENCY

    def warning_message(self) -> str:
        """Human-readable WARN text for :attr:`is_warning` outcomes."""

        return (
            f"Found '{self.path}' but python-dotenv is not installed, so the "
            f"file was ignored. Install it (pip install python-dotenv) or "
            f"export the variables manually. Continuing."
        )


def load_env_file(
    path: str | os.PathLike[str] | None = None,
    *,
    enabled: bool = True,
) -> EnvLoadResult:
    """Load ``.env`` variables into ``os.environ`` without overriding them.

    Parameters
    ----------
    path:
        Explicit ``.env`` location (``--env-file``). When ``None`` the
        :data:`DEFAULT_ENV_FILE` relative path is used, which resolves against
        the current working directory.
    enabled:
        ``False`` short-circuits the whole operation (``--no-dotenv``), which
        keeps CI and manual debugging hermetically free of a stray ``.env``.

    Returns
    -------
    EnvLoadResult
        Never raises: every failure mode is reported as a status instead, so
        the CLI can decide between silence and a WARN.
    """

    target = Path(path) if path is not None else Path(DEFAULT_ENV_FILE)
    resolved = str(target)

    if not enabled:
        return EnvLoadResult(status=STATUS_DISABLED, path=resolved)

    if not target.is_file():
        # The common case: no .env at all. Silent, not a warning.
        return EnvLoadResult(status=STATUS_MISSING_FILE, path=resolved)

    try:
        from dotenv import load_dotenv
    except ImportError as exc:  # pragma: no cover - depends on install state
        return EnvLoadResult(
            status=STATUS_MISSING_DEPENDENCY,
            path=resolved,
            detail=str(exc),
        )

    # ``override=False`` is the load-bearing argument: it preserves the
    # flag > shell-env > .env precedence documented in the module docstring.
    before = set(os.environ)
    load_dotenv(dotenv_path=target, override=False)
    loaded = len(set(os.environ) - before)

    return EnvLoadResult(status=STATUS_LOADED, path=resolved, loaded=loaded)
