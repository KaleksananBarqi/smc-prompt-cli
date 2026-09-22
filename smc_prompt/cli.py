"""Click entrypoint and the single orchestration function.

``cli.py`` is the only module that writes to stdout/stderr and the only module
that maps exceptions to process exit codes. It contains no analysis logic.
"""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

import click

from . import __version__, config as cfg
from .csv_source import LocalCsvSource
from .data_fetcher import DataFetcher
from .env_loader import load_env_file
from .errors import (
    ConfigError,
    DelistedWarning,
    NetworkError,
    SmcPromptError,
    SymbolStatusWarning,
    exit_code_for,
)
from .oanda_source import OandaSource
from .output import deliver
from .provider_base import price_sanity_warning
from .review_renderer import render_review_markdown
from .setup_validator import (
    SetupSpec,
    analyze_setup,
    render_validation_prompt,
)
from .twelvedata_source import TwelveDataSource
from .structure_analyzer import (
    analyze,
    compute_atr,
    reference_sanity_warnings,
)
from .template_renderer import build_payload, render

PROG = "smc-prompt"

#: Environment-variable fallbacks for the provider credentials, so a normal
#: shell profile does not have to repeat the key on every invocation.
TWELVEDATA_KEY_ENV = "TWELVEDATA_API_KEY"
OANDA_TOKEN_ENV = "OANDA_API_TOKEN"
OANDA_ACCOUNT_ENV = "OANDA_ACCOUNT_ID"

#: Credential precedence, strongest first. Documented here because it is split
#: across two mechanisms: the flag beats the environment inside
#: :func:`_resolve_credentials`, and the shell environment beats ``.env``
#: because :func:`load_env_file` never overrides existing variables.
CREDENTIAL_PRECEDENCE = "CLI flag > environment variable > .env file"


@dataclass(frozen=True)
class RunResult:
    """Summary of a completed run (used for diagnostics/tests)."""

    prompt: str
    output_path: str
    copied_to_clipboard: bool
    printed_to_stdout: bool
    warnings: tuple[str, ...]
    #: True when :func:`run` was invoked in ``--dry-run`` mode: config/symbol
    #: were validated and resolved settings printed, but nothing was fetched,
    #: rendered, or written.
    dry_run: bool = False


def _ensure_utf8_streams() -> None:
    """Force UTF-8 on stdout/stderr.

    The template contains non-ASCII characters (e.g. ``⚠️``, em dashes). On
    Windows the console defaults to cp1252 and would raise
    ``UnicodeEncodeError`` mid-write, corrupting output. Reconfiguring keeps
    the rendered bytes exactly as specified.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", newline="")
        except (ValueError, OSError):  # pragma: no cover - defensive
            pass


def _warn(message: str) -> None:
    click.echo(f"[{PROG}] WARN: {message}", err=True)


def _error(message: str) -> None:
    click.echo(f"[{PROG}] ERROR: {message}", err=True)


def _env_value(name: str) -> str | None:
    """Return a non-empty environment value, or ``None``."""

    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _load_dotenv_or_warn(env_file: str | None, *, no_dotenv: bool) -> None:
    """Populate ``os.environ`` from ``.env``, warning instead of failing.

    Called from :func:`main` only — never from :func:`run`. The test suite
    drives ``run()`` directly, so keeping the loader at the CLI boundary leaves
    the suite hermetic and stops a developer's real ``.env`` from leaking into
    it. A ``.env`` that exists while ``python-dotenv`` is missing produces a
    WARN and the run continues (exit code stays ``0``).
    """

    result = load_env_file(env_file, enabled=not no_dotenv)
    if result.is_warning:
        _warn(result.warning_message())


def _resolve_credentials(
    twelvedata_key: str | None,
    oanda_token: str | None,
    oanda_account_id: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Apply the environment fallbacks for the provider credentials."""

    return (
        twelvedata_key or _env_value(TWELVEDATA_KEY_ENV),
        oanda_token or _env_value(OANDA_TOKEN_ENV),
        oanda_account_id or _env_value(OANDA_ACCOUNT_ENV),
    )


def _make_provider_source(
    config: cfg.Config,
    *,
    twelvedata_key: str | None,
    oanda_token: str | None,
    oanda_account_id: str | None,
    oanda_env: str,
) -> TwelveDataSource | OandaSource:
    """Build the configured network data source (Binance is built separately).

    Every provider exposes the same public shape, so the analysis pipeline below
    is untouched by the choice.
    """

    if config.provider == cfg.PROVIDER_TWELVEDATA:
        return TwelveDataSource(config, api_key=twelvedata_key or "")
    if config.provider == cfg.PROVIDER_OANDA:
        return OandaSource(
            config,
            token=oanda_token or "",
            environment=oanda_env,
            account_id=oanda_account_id,
        )
    raise ConfigError(f"Unsupported provider '{config.provider}'.")




def _resolve_input_files(
    input_csv: str | None,
    htf_file: str | None,
    mtf_file: str | None,
    ltf_file: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the offline CSV trio (or ``(None, None, None)`` for the network).

    Offline mode is enabled *only* by ``--input-csv`` so the default network path
    is never switched implicitly. ``--htf-file`` / ``--mtf-file`` / ``--ltf-file``
    refine which CSV feeds each timeframe; any may be omitted to fall back to
    ``--input-csv``.
    """

    if input_csv:
        return (
            htf_file or input_csv,
            mtf_file or input_csv,
            ltf_file or input_csv,
        )
    if htf_file or mtf_file or ltf_file:
        raise ConfigError(
            "--htf-file / --mtf-file / --ltf-file require --input-csv as well; "
            "offline mode is enabled explicitly so the default network path is "
            "never changed implicitly."
        )
    return None, None, None


def _extract_tick_size(entry: dict) -> str | None:
    """Pull ``PRICE_FILTER.tickSize`` out of an exchangeInfo symbol entry.

    Binance lists several filters per symbol; only ``PRICE_FILTER`` carries the
    ``tickSize`` that defines the price precision. Returns ``None`` when absent
    so the caller falls back to the magnitude-bucketed formatting rule.
    """

    filters = entry.get("filters") if isinstance(entry, dict) else None
    if not isinstance(filters, list):
        return None
    for item in filters:
        if isinstance(item, dict) and item.get("filterType") == "PRICE_FILTER":
            tick = item.get("tickSize")
            return str(tick) if tick is not None else None
    return None


def _print_resolved_settings(
    config: cfg.Config,
    *,
    offline_mode: bool,
    htf_file: str | None,
    mtf_file: str | None,
    ltf_file: str | None,
    print_stdout: bool,
    max_prompt_bytes: int | None,
    candles_only: bool = False,
    review_interval: str = cfg.DEFAULT_REVIEW_INTERVAL,
    review_candles: int = cfg.DEFAULT_REVIEW_CANDLES,
    validate_setup: bool = False,
    entry: float | None = None,
    tp: float | None = None,
    sl: float | None = None,
    direction: str | None = None,
    order_status: str | None = None,
    validate_interval: str = cfg.DEFAULT_VALIDATE_INTERVAL,
    validate_candles: int = cfg.DEFAULT_VALIDATE_CANDLES,
) -> None:
    """Print the resolved run settings for ``--dry-run`` (Phase 5, #16).

    No network, no klines, and no file write happen on this path: it exists for
    CI/pre-flight validation. All output goes to stderr so stdout stays empty
    unless ``--stdout`` is also given.
    """

    source = (
        "offline CSV"
        if offline_mode
        else f"{config.provider_label} network"
    )
    if offline_mode:
        source = (
            f"{source} (HTF={htf_file}, MTF={mtf_file}, LTF={ltf_file})"
        )

    if candles_only:
        lines = [
            f"[{PROG}] DRY RUN — review mode, no file written, no klines fetched.",
            f"[{PROG}] symbol={config.symbol}",
            f"[{PROG}] provider={config.provider}",
            f"[{PROG}] data source={source}",
            f"[{PROG}] review_interval={review_interval} ({cfg.interval_label(review_interval)})",
            f"[{PROG}] review_candles={review_candles}",
            f"[{PROG}] output_dir={config.output_dir} stdout={print_stdout}",
        ]
        for line in lines:
            click.echo(line, err=True)
        return

    if validate_setup:
        lines = [
            f"[{PROG}] DRY RUN — setup validation mode, no file written, no klines fetched.",
            f"[{PROG}] symbol={config.symbol}",
            f"[{PROG}] provider={config.provider}",
            f"[{PROG}] data source={source}",
            f"[{PROG}] direction={direction} order_status={order_status} entry={entry} tp={tp} sl={sl}",
            f"[{PROG}] validate_interval={validate_interval} ({cfg.interval_label(validate_interval)})",
            f"[{PROG}] validate_candles={validate_candles}",
            f"[{PROG}] output_dir={config.output_dir} stdout={print_stdout}",
        ]
        for line in lines:
            click.echo(line, err=True)
        return

    lines = [
        f"[{PROG}] DRY RUN — no file written, no klines fetched.",
        f"[{PROG}] symbol={config.symbol}",
        f"[{PROG}] provider={config.provider}",
        f"[{PROG}] data source={source}",
        f"[{PROG}] htf_interval={config.htf_interval} "
        f"({config.htf_interval_label}) mtf_interval={config.mtf_interval} "
        f"({config.mtf_interval_label}) ltf_interval={config.ltf_interval} "
        f"({config.ltf_interval_label})",
        f"[{PROG}] htf_candles={config.htf_candles} "
        f"mtf_candles={config.mtf_candles} "
        f"ltf_candles={config.ltf_candles} "
        f"swing_lookback={config.swing_lookback}",
        f"[{PROG}] distance_reference={config.distance_reference} "
        f"include_atr={config.include_atr}",
        f"[{PROG}] htf_fetch_limit={config.htf_fetch_limit} "
        f"mtf_fetch_limit={config.mtf_fetch_limit} "
        f"ltf_fetch_limit={config.ltf_fetch_limit}",
        f"[{PROG}] output_dir={config.output_dir} stdout={print_stdout}",
        f"[{PROG}] volume_mean_period={config.volume_mean_period} "
        f"volume_spike_mult={cfg.fmt_ratio(config.volume_spike_mult)} "
        f"volume_available={config.volume_available}",
        f"[{PROG}] prompt_bytes_warn={config.prompt_bytes_warn} "
        f"max_prompt_bytes={max_prompt_bytes}",
    ]
    for line in lines:
        click.echo(line, err=True)


def _prompt_size_notes(text: str, config: cfg.Config) -> list[str]:
    """Post-render prompt-size guard (Phase 5, #11).

    Raises :class:`ConfigError` when an explicit ``--max-prompt-bytes`` hard
    limit is exceeded; otherwise returns a WARN string (with the byte and
    approximate token count) when the configurable warning threshold is
    crossed, or an empty list.
    """

    size = len(text.encode("utf-8"))
    if config.max_prompt_bytes is not None and size > config.max_prompt_bytes:
        raise ConfigError(
            f"Rendered prompt is {size} bytes, exceeding --max-prompt-bytes "
            f"({config.max_prompt_bytes}). Reduce "
            f"--htf-candles/--mtf-candles/--ltf-candles or raise the limit."
        )

    notes: list[str] = []
    if config.prompt_bytes_warn is not None and size > config.prompt_bytes_warn:
        approx_tokens = size // cfg.PROMPT_BYTES_PER_TOKEN
        notes.append(
            f"Rendered prompt is {size} bytes (~{approx_tokens} tokens), above "
            f"the {config.prompt_bytes_warn}-byte warning threshold."
        )
    return notes


def run(
    symbol: str,
    *,
    htf_candles: int = cfg.DEFAULT_HTF_CANDLES,
    ltf_candles: int = cfg.DEFAULT_LTF_CANDLES,
    swing_lookback: int = cfg.DEFAULT_SWING_LOOKBACK,
    distance_reference: str = cfg.DISTANCE_REFERENCE_NEAREST,
    include_atr: bool = True,
    output_dir: str = cfg.DEFAULT_OUTPUT_DIR,
    htf_interval: str = cfg.HTF_INTERVAL,
    mtf_candles: int = cfg.DEFAULT_MTF_CANDLES,
    mtf_interval: str = cfg.MTF_INTERVAL,
    ltf_interval: str = cfg.LTF_INTERVAL,
    print_stdout: bool = False,
    base_urls: tuple[str, ...] | None = None,
    input_csv: str | None = None,
    htf_file: str | None = None,
    mtf_file: str | None = None,
    ltf_file: str | None = None,
    max_prompt_bytes: int | None = None,
    dry_run: bool = False,
    provider: str = cfg.PROVIDER_BINANCE,
    twelvedata_key: str | None = None,
    oanda_token: str | None = None,
    oanda_account_id: str | None = None,
    oanda_env: str = cfg.OANDA_ENV_PRACTICE,
    candles_only: bool = False,
    review_interval: str = cfg.DEFAULT_REVIEW_INTERVAL,
    review_candles: int = cfg.DEFAULT_REVIEW_CANDLES,
    validate_setup: bool = False,
    entry: float | None = None,
    tp: float | None = None,
    sl: float | None = None,
    direction: str | None = None,
    order_status: str | None = None,
    unfilled: bool = False,
    filled: bool = False,
    validate_interval: str = cfg.DEFAULT_VALIDATE_INTERVAL,
    validate_candles: int = cfg.DEFAULT_VALIDATE_CANDLES,
) -> RunResult:
    """Chain fetch -> analyze -> render -> output. Raises on any failure."""

    if candles_only and validate_setup:
        raise ConfigError("Cannot specify both --candles-only and --validate-setup.")

    if candles_only:
        cfg.validate_interval(review_interval, flag="--review-interval")
        cfg.provider_interval(provider, review_interval)
        cfg.validate_review_candles(review_candles)
        if review_interval not in (htf_interval, mtf_interval, ltf_interval):
            ltf_interval = review_interval
            if ltf_interval == mtf_interval:
                mtf_interval = "4h" if ltf_interval != "4h" else "2h"
            if ltf_interval == htf_interval:
                htf_interval = "1d" if ltf_interval != "1d" else "1w"

    inferred_direction: str | None = None
    resolved_order_status: str | None = None
    if validate_setup:
        if unfilled and filled:
            raise ConfigError("Cannot specify both --unfilled and --filled.")

        if unfilled:
            resolved_order_status = "unfilled"
        elif filled:
            resolved_order_status = "filled"
        elif order_status is not None:
            clean_status = order_status.lower().strip()
            if clean_status in ("unfilled", "pending"):
                resolved_order_status = "unfilled"
            elif clean_status in ("filled", "triggered"):
                resolved_order_status = "filled"
            else:
                raise ConfigError(
                    f"Invalid order status '{order_status}'; must be 'unfilled' or 'filled'."
                )
        else:
            raise ConfigError(
                "--validate-setup requires specifying order status: use --status [unfilled|filled] "
                "(or flag --unfilled / --filled)."
            )

        if entry is None or tp is None:
            raise ConfigError("--validate-setup requires both --entry and --tp.")

        dec_entry = Decimal(str(entry))
        dec_tp = Decimal(str(tp))

        if dec_tp == dec_entry:
            raise ConfigError("--tp cannot be equal to --entry.")

        if direction is not None:
            dir_clean = direction.lower().strip()
            if dir_clean not in ("long", "short"):
                raise ConfigError(f"Invalid direction '{direction}'; must be 'long' or 'short'.")
            inferred_direction = dir_clean
        else:
            inferred_direction = "long" if dec_tp > dec_entry else "short"

        if sl is not None:
            dec_sl = Decimal(str(sl))
            if inferred_direction == "long" and dec_sl >= dec_entry:
                raise ConfigError("For long setups, --sl must be lower than --entry.")
            if inferred_direction == "short" and dec_sl <= dec_entry:
                raise ConfigError("For short setups, --sl must be higher than --entry.")

        cfg.validate_interval(validate_interval, flag="--validate-interval")
        cfg.provider_interval(provider, validate_interval)
        cfg.validate_validate_candles(validate_candles)
        if validate_interval not in (htf_interval, mtf_interval, ltf_interval):
            ltf_interval = validate_interval
            if ltf_interval == mtf_interval:
                mtf_interval = "4h" if ltf_interval != "4h" else "2h"
            if ltf_interval == htf_interval:
                htf_interval = "1d" if ltf_interval != "1d" else "1w"

    key, token, account_id = _resolve_credentials(
        twelvedata_key, oanda_token, oanda_account_id
    )

    config = cfg.build_config(
        symbol,
        htf_candles=htf_candles,
        mtf_candles=mtf_candles,
        ltf_candles=ltf_candles,
        swing_lookback=swing_lookback,
        distance_reference=distance_reference,
        include_atr=include_atr,
        htf_interval=htf_interval,
        mtf_interval=mtf_interval,
        ltf_interval=ltf_interval,
        output_dir=output_dir,
        max_prompt_bytes=max_prompt_bytes,
        base_urls=base_urls,
        provider=provider,
    )

    offline_htf, offline_mtf, offline_ltf = _resolve_input_files(
        input_csv, htf_file, mtf_file, ltf_file
    )
    offline_mode = (
        offline_htf is not None
        and offline_mtf is not None
        and offline_ltf is not None
    )

    # --dry-run (Phase 5, #16): validate config + resolve the data source and
    # print the resolved settings, then exit WITHOUT fetching klines or writing
    # a file. Useful for CI / pre-flight checks.
    if dry_run:
        _print_resolved_settings(
            config,
            offline_mode=offline_mode,
            htf_file=offline_htf,
            mtf_file=offline_mtf,
            ltf_file=offline_ltf,
            print_stdout=print_stdout,
            max_prompt_bytes=max_prompt_bytes,
            candles_only=candles_only,
            review_interval=review_interval,
            review_candles=review_candles,
            validate_setup=validate_setup,
            entry=entry,
            tp=tp,
            sl=sl,
            direction=inferred_direction,
            order_status=resolved_order_status,
            validate_interval=validate_interval,
            validate_candles=validate_candles,
        )
        return RunResult("", "", False, False, (), dry_run=True)

    warnings: list[str] = []
    # Data source selection. Three interchangeable sources share one public
    # shape, so the analysis pipeline below is untouched by the choice:
    #   * offline CSV      — enabled ONLY by --input-csv (network-free)
    #   * Binance          — the default network provider (crypto)
    #   * Twelve Data / OANDA — network providers that can serve XAUUSD, which
    #     Binance Futures cannot (no fiat/forex/metal instruments)
    fetcher: DataFetcher | LocalCsvSource | TwelveDataSource | OandaSource
    if offline_mode:
        fetcher = LocalCsvSource(
            config,
            htf_file=offline_htf,
            mtf_file=offline_mtf,
            ltf_file=offline_ltf,
        )
    elif config.provider == cfg.PROVIDER_BINANCE:
        fetcher = DataFetcher(config)
    else:
        fetcher = _make_provider_source(
            config,
            twelvedata_key=key,
            oanda_token=token,
            oanda_account_id=account_id,
            oanda_env=oanda_env,
        )

    # exchangeInfo is fetched once: it validates the symbol, yields the
    # PRICE_FILTER.tickSize for tick-precise price rendering (#9) and carries
    # the listing status surfaced as a non-TRADING warning (#9/#14).
    symbol_entry = fetcher.validate_symbol()

    tick_size = _extract_tick_size(symbol_entry)
    config = replace(
        config,
        price_format=cfg.PriceFormat(cfg.decimals_from_tick_size(tick_size)),
    )

    status = symbol_entry.get("status")
    if isinstance(status, str) and status and status != "TRADING":
        warnings.append(SymbolStatusWarning(config.symbol, status).message())

    # Prefer the provider's own clock over the host clock for the closure
    # decision so a skewed local clock cannot inject a half-open candle (#8).
    # The fetcher exposes an injectable ``now`` seam; rebind it to the fixed
    # instant. The FX providers expose no clock endpoint and return the host
    # clock directly (by design), so this path is only a WARN for Binance.
    server_time: datetime | None = None
    if offline_mode:
        # Offline mode derives ``now`` from the CSV (max close_time + 1s), so
        # GENERATED_AT_UTC is a deterministic function of the input file and no
        # host clock / network is consulted.
        server_time = fetcher.fetch_server_time()
    else:
        try:
            server_time = fetcher.fetch_server_time()
        except NetworkError as exc:
            _warn(
                f"{config.provider_label} server time unavailable ({exc}); "
                f"falling back to the host clock for candle-closure and "
                f"GENERATED_AT_UTC."
            )
    if server_time is not None:
        fetcher = fetcher.with_now(server_time)

    # Post-trade review / candles-only fast-path
    if candles_only:
        for message in warnings:
            _warn(message)

        fetch_limit = min(review_candles + 20, cfg.FETCH_LIMIT_MAX)
        raw_candles = fetcher.fetch_klines(review_interval, fetch_limit)
        closed_candles = [c for c in raw_candles if c.is_closed]

        if len(closed_candles) < review_candles:
            warn_msg = (
                f"Only {len(closed_candles)} closed {review_interval} candles "
                f"available (requested {review_candles}). "
                f"Reduced table to {len(closed_candles)}."
            )
            warnings.append(warn_msg)
            _warn(warn_msg)

        selected_candles = (
            closed_candles[-review_candles:]
            if len(closed_candles) >= review_candles
            else closed_candles
        )
        if not selected_candles:
            raise ConfigError(
                f"No closed candles available for {config.symbol} at {review_interval}."
            )

        generated_at = server_time or datetime.now(timezone.utc)
        review_text = render_review_markdown(
            symbol=config.symbol,
            interval=review_interval,
            candles=selected_candles,
            provider_label=config.provider_label,
            generated_at=generated_at,
            price_format=config.price_format,
        )

        if print_stdout:
            click.echo(review_text, nl=False)

        result = deliver(
            review_text,
            symbol=config.symbol,
            output_dir=config.output_dir,
            moment=generated_at,
            is_review=True,
        )
        output_path = str(result.output_path)
        click.echo(f"[{PROG}] Candle review written to {output_path}.", err=True)
        if result.clipboard_warning:
            _warn(result.clipboard_warning)
        else:
            click.echo(f"[{PROG}] Candle review copied to clipboard.", err=True)

        return RunResult(
            review_text,
            output_path,
            result.copied_to_clipboard,
            print_stdout,
            tuple(warnings),
        )

    # Setup validation fast-path
    if validate_setup:
        for message in warnings:
            _warn(message)

        fetch_limit = min(validate_candles + 20, cfg.FETCH_LIMIT_MAX)
        raw_candles = fetcher.fetch_klines(validate_interval, fetch_limit)
        closed_candles = [c for c in raw_candles if c.is_closed]

        if len(closed_candles) < validate_candles:
            warn_msg = (
                f"Only {len(closed_candles)} closed {validate_interval} candles "
                f"available (requested {validate_candles}). "
                f"Reduced table to {len(closed_candles)}."
            )
            warnings.append(warn_msg)
            _warn(warn_msg)

        selected_candles = (
            closed_candles[-validate_candles:]
            if len(closed_candles) >= validate_candles
            else closed_candles
        )
        if not selected_candles:
            raise ConfigError(
                f"No closed candles available for {config.symbol} at {validate_interval}."
            )

        assert inferred_direction is not None
        assert resolved_order_status is not None
        assert entry is not None
        assert tp is not None

        setup_spec = SetupSpec(
            symbol=config.symbol,
            direction=inferred_direction,
            entry_price=Decimal(str(entry)),
            tp_price=Decimal(str(tp)),
            sl_price=Decimal(str(sl)) if sl is not None else None,
            order_status=resolved_order_status,
        )

        analysis = analyze_setup(setup_spec, selected_candles)
        generated_at = server_time or datetime.now(timezone.utc)

        validation_text = render_validation_prompt(
            setup=setup_spec,
            analysis=analysis,
            candles=selected_candles,
            interval=validate_interval,
            provider_label=config.provider_label,
            generated_at=generated_at,
            price_format=config.price_format,
        )

        if print_stdout:
            click.echo(validation_text, nl=False)

        result = deliver(
            validation_text,
            symbol=config.symbol,
            output_dir=config.output_dir,
            moment=generated_at,
            is_validation=True,
        )
        output_path = str(result.output_path)
        click.echo(f"[{PROG}] Setup validation prompt written to {output_path}.", err=True)
        if result.clipboard_warning:
            _warn(result.clipboard_warning)
        else:
            click.echo(f"[{PROG}] Setup validation prompt copied to clipboard.", err=True)

        return RunResult(
            validation_text,
            output_path,
            result.copied_to_clipboard,
            print_stdout,
            tuple(warnings),
        )

    htf_raw = fetcher.fetch_klines(config.htf_interval, config.htf_fetch_limit)
    mtf_raw = fetcher.fetch_klines(config.mtf_interval, config.mtf_fetch_limit)
    ltf_raw = fetcher.fetch_klines(config.ltf_interval, config.ltf_fetch_limit)

    # Current-price sanity (#13): cross-check the ticker against the last closed
    # LTF candle's [low, high] ± 1 × LTF ATR. The tolerance uses the same ATR
    # driving the swing filter, so no extra indicator is introduced.
    # NOTE: the price-sanity band is intentionally LTF-only; MTF is excluded so
    # the ticker cross-check and closed-candle fallback stay on the finest tier.
    ltf_closed = [candle for candle in ltf_raw if candle.is_closed]
    ltf_atr: Decimal | None = None
    if len(ltf_closed) >= config.atr_period + 1:
        ltf_atr = compute_atr(ltf_closed, config.atr_period)
    reference_candle = ltf_closed[-1] if ltf_closed else None

    current_price = fetcher.fetch_current_price(
        reference_candle=reference_candle, tolerance=ltf_atr
    )
    warnings.extend(fetcher.price_notes)
    price_warning = price_sanity_warning(
        current_price,
        reference_candle,
        ltf_atr,
        symbol=config.symbol,
        price_format=config.price_format,
    )
    if price_warning is not None:
        warnings.append(price_warning)

    generated_at = server_time or datetime.now(timezone.utc)

    htf_result, htf_table, htf_stats = analyze(
        htf_raw,
        timeframe=config.htf_interval,
        requested=config.htf_candles,
        current_price=current_price,
        config=config,
    )
    mtf_result, mtf_table, mtf_stats = analyze(
        mtf_raw,
        timeframe=config.mtf_interval,
        requested=config.mtf_candles,
        current_price=current_price,
        config=config,
    )
    ltf_result, ltf_table, ltf_stats = analyze(
        ltf_raw,
        timeframe=config.ltf_interval,
        requested=config.ltf_candles,
        current_price=current_price,
        config=config,
    )

    if htf_stats.was_reduced:
        warnings.append(
            f"Only {htf_stats.emitted_count} closed {config.htf_interval} candles "
            f"available (requested {htf_stats.requested_count}). "
            f"Reduced table to {htf_stats.emitted_count}."
        )
    if mtf_stats.was_reduced:
        warnings.append(
            f"Only {mtf_stats.emitted_count} closed {config.mtf_interval} candles "
            f"available (requested {mtf_stats.requested_count}). "
            f"Reduced table to {mtf_stats.emitted_count}."
        )
    if ltf_stats.was_reduced:
        warnings.append(
            f"Only {ltf_stats.emitted_count} closed {config.ltf_interval} candles "
            f"available (requested {ltf_stats.requested_count}). "
            f"Reduced table to {ltf_stats.emitted_count}."
        )

    # The zero-volume delisted heuristic only means something when the feed
    # actually carries volume. OANDA reports a tick count (zeroed by the
    # provider) and Twelve Data reports zero for metals, so gating on
    # ``config.volume_available`` is what stops every FX run from warning that a
    # perfectly tradable pair "may be delisted or halted".
    if config.volume_available:
        for stats in (htf_stats, mtf_stats, ltf_stats):
            if stats.zero_volume_streak >= config.delisted_zero_volume_streak:
                warnings.append(
                    DelistedWarning(
                        config.symbol, stats.zero_volume_streak
                    ).message()
                )

    # Mechanical reference/structure contradictions (deterministic; empty when
    # consistent). Non-fatal: the prompt is still rendered with the raw facts.
    warnings.extend(reference_sanity_warnings(htf_result, config))
    warnings.extend(reference_sanity_warnings(mtf_result, config))
    warnings.extend(reference_sanity_warnings(ltf_result, config))

    for message in warnings:
        _warn(message)

    payload = build_payload(
        symbol=config.symbol,
        generated_at=generated_at,
        current_price=current_price,
        htf=htf_result,
        mtf=mtf_result,
        ltf=ltf_result,
        htf_candles=htf_table,
        mtf_candles=mtf_table,
        ltf_candles=ltf_table,
        config=config,
    )
    # ``provider_name`` is a render variable, not a payload placeholder, so the
    # frozen template stays provider-agnostic while the provenance line always
    # names the source that actually produced the data.
    rendered = render(
        payload,
        include_atr=config.include_atr,
        provider_name=config.provider_label,
    )
    prompt_text = rendered.text

    # Post-render size guard (Phase 5, #11): the hard limit raises ConfigError
    # (exit 2) before anything is written; the soft threshold only WARNs.
    size_notes = _prompt_size_notes(prompt_text, config)
    for note in size_notes:
        _warn(note)

    if print_stdout:
        click.echo(prompt_text, nl=False)

    result = deliver(
        prompt_text,
        symbol=config.symbol,
        output_dir=config.output_dir,
        moment=generated_at,
    )
    output_path = str(result.output_path)
    click.echo(f"[{PROG}] Prompt written to {output_path}.", err=True)
    if result.clipboard_warning:
        _warn(result.clipboard_warning)
    else:
        click.echo(f"[{PROG}] Prompt copied to clipboard.", err=True)

    return RunResult(
        prompt_text,
        output_path,
        result.copied_to_clipboard,
        print_stdout,
        tuple(warnings),
    )


@click.command(
    name=PROG,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Generate a mechanical SMC/ICT [FAKTA] prompt payload from read-only "
        "public market data (Binance / Twelve Data / OANDA). Useful for FX and "
        "metals (e.g. XAUUSD) which Binance Futures does not list. No LLM calls, "
        "no reasoning, no trading."
    ),
)
@click.argument("symbol")
@click.option(
    "--provider",
    type=click.Choice(list(cfg.PROVIDERS)),
    default=cfg.PROVIDER_BINANCE,
    show_default=True,
    help=(
        "Data provider. Binance serves crypto only; Twelve Data and OANDA "
        "serve spot FX/metals (XAUUSD)."
    ),
)
@click.option(
    "--twelvedata-key",
    "twelvedata_key",
    default=None,
    help=(
        f"Twelve Data API key. Falls back to the {TWELVEDATA_KEY_ENV} "
        f"environment variable."
    ),
)
@click.option(
    "--oanda-token",
    "oanda_token",
    default=None,
    help=(
        f"OANDA v20 API token. Falls back to the {OANDA_TOKEN_ENV} "
        f"environment variable."
    ),
)
@click.option(
    "--oanda-account-id",
    "oanda_account_id",
    default=None,
    help=(
        f"OANDA v20 account id (optional; auto-discovered). Falls back to the "
        f"{OANDA_ACCOUNT_ENV} environment variable."
    ),
)
@click.option(
    "--oanda-env",
    "oanda_env",
    type=click.Choice(list(cfg.OANDA_ENVS)),
    default=cfg.OANDA_ENV_PRACTICE,
    show_default=True,
    help="OANDA environment: practice (free demo) or live (funded account).",
)
@click.option(
    "--htf-candles",
    type=int,
    default=cfg.DEFAULT_HTF_CANDLES,
    show_default=True,
    help="Number of CLOSED HTF-interval candles in the HTF raw table (>= 10).",
)
@click.option(
    "--mtf-candles",
    type=int,
    default=cfg.DEFAULT_MTF_CANDLES,
    show_default=True,
    help="Number of CLOSED MTF-interval candles in the MTF raw table (>= 10).",
)
@click.option(
    "--ltf-candles",
    type=int,
    default=cfg.DEFAULT_LTF_CANDLES,
    show_default=True,
    help="Number of CLOSED LTF-interval candles in the LTF raw table (>= 10).",
)
@click.option(
    "--htf-interval",
    type=str,
    default=cfg.HTF_INTERVAL,
    show_default=True,
    help=(
        "Candle interval for the HTF series (e.g. 1d, 4h). Mapped per provider "
        "via --provider. Allowed: " + ", ".join(cfg.BINANCE_INTERVALS) + "."
    ),
)
@click.option(
    "--mtf-interval",
    type=str,
    default=cfg.MTF_INTERVAL,
    show_default=True,
    help=(
        "Candle interval for the MTF series (e.g. 4h, 2h). Mapped per provider "
        "via --provider. Allowed: " + ", ".join(cfg.BINANCE_INTERVALS) + "."
    ),
)
@click.option(
    "--ltf-interval",
    type=str,
    default=cfg.LTF_INTERVAL,
    show_default=True,
    help=(
        "Candle interval for the LTF series (e.g. 1h, 15m). Mapped per provider "
        "via --provider. Allowed: " + ", ".join(cfg.BINANCE_INTERVALS) + "."
    ),
)
@click.option(
    "--swing-lookback",
    type=int,
    default=cfg.DEFAULT_SWING_LOOKBACK,
    show_default=True,
    help="Fractal window size N (odd, >= 3).",
)
@click.option(
    "--distance-reference",
    type=click.Choice(list(cfg.DISTANCE_REFERENCES)),
    default=cfg.DISTANCE_REFERENCE_NEAREST,
    show_default=True,
    help="Reference swing for distance: nearest-by-price or most-recent-by-time.",
)
@click.option(
    "--no-atr",
    "no_atr",
    is_flag=True,
    default=False,
    help="Omit the ATR(14) fields from the prompt.",
)
@click.option(
    "--base-url",
    "base_url",
    default=None,
    help=(
        "Override the Binance REST host (Binance provider only). Falls back to "
        + ", ".join(cfg.DEFAULT_BASE_URLS)
        + " on connection errors / HTTP 451 / 403."
    ),
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False),
    default=cfg.DEFAULT_OUTPUT_DIR,
    show_default=True,
    help="Directory for the generated .md prompt file.",
)
@click.option(
    "--stdout",
    "print_stdout",
    is_flag=True,
    default=False,
    help="Also print the prompt to stdout (off by default).",
)
@click.option(
    "--input-csv",
    "input_csv",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Run fully OFFLINE from a local OHLCV CSV (columns: "
        "open_time,open,high,low,close,volume). Feeds ALL THREE timeframes "
        "unless overridden by --htf-file/--mtf-file/--ltf-file. Network is the "
        "default."
    ),
)
@click.option(
    "--htf-file",
    "htf_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Offline HTF candle CSV; requires --input-csv.",
)
@click.option(
    "--mtf-file",
    "mtf_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Offline MTF candle CSV; requires --input-csv.",
)
@click.option(
    "--ltf-file",
    "ltf_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Offline LTF candle CSV; requires --input-csv.",
)
@click.option(
    "--max-prompt-bytes",
    "max_prompt_bytes",
    type=int,
    default=None,
    help=(
        "Hard post-render size limit in bytes; exceeding it aborts with "
        "ConfigError (exit 2). Off by default."
    ),
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help=(
        "Validate config + symbol and print the resolved settings, then exit "
        "WITHOUT fetching klines or writing a file (CI/pre-flight)."
    ),
)
@click.option(
    "--candles-only",
    "--review",
    "candles_only",
    is_flag=True,
    default=False,
    help=(
        "Export raw closed candles and session price metrics to .md without "
        "the SMC/ICT prompt template (for post-trade review / journaling)."
    ),
)
@click.option(
    "--review-interval",
    "review_interval",
    type=str,
    default=cfg.DEFAULT_REVIEW_INTERVAL,
    show_default=True,
    help="Candle interval for --candles-only review mode (e.g. 1h, 15m, 4h).",
)
@click.option(
    "--review-candles",
    "review_candles",
    type=int,
    default=cfg.DEFAULT_REVIEW_CANDLES,
    show_default=True,
    help="Number of closed candles to export in --candles-only review mode (>= 5).",
)
@click.option(
    "--validate-setup",
    "--check-setup",
    "validate_setup",
    is_flag=True,
    default=False,
    help=(
        "Generate a validation prompt to check if a setup is front-runned, "
        "invalidated, triggered, or still fresh before entry."
    ),
)
@click.option(
    "--entry",
    type=float,
    default=None,
    help="Planned entry price level for --validate-setup.",
)
@click.option(
    "--tp",
    type=float,
    default=None,
    help="Planned take profit / DOL target price level for --validate-setup.",
)
@click.option(
    "--sl",
    type=float,
    default=None,
    help="Planned stop loss price level for --validate-setup (optional).",
)
@click.option(
    "--direction",
    type=click.Choice(["long", "short"], case_sensitive=False),
    default=None,
    help=(
        "Planned trade direction (long or short). If omitted, automatically "
        "inferred from entry and tp."
    ),
)
@click.option(
    "--order-status",
    "--status",
    "order_status",
    type=click.Choice(["unfilled", "filled", "pending", "triggered"], case_sensitive=False),
    default=None,
    help=(
        "Execution status of your order at the exchange: 'unfilled' (limit order pending) "
        "or 'filled' (position active). Required for --validate-setup."
    ),
)
@click.option(
    "--unfilled",
    "unfilled",
    is_flag=True,
    default=False,
    help="Shortcut for --order-status unfilled (limit order pending, belum terjemput).",
)
@click.option(
    "--filled",
    "filled",
    is_flag=True,
    default=False,
    help="Shortcut for --order-status filled (position active, sudah terjemput).",
)
@click.option(
    "--validate-interval",
    type=str,
    default=cfg.DEFAULT_VALIDATE_INTERVAL,
    show_default=True,
    help="Candle interval for --validate-setup (e.g. 15m, 1h, 5m).",
)
@click.option(
    "--validate-candles",
    type=int,
    default=cfg.DEFAULT_VALIDATE_CANDLES,
    show_default=True,
    help="Number of closed candles to analyze for --validate-setup (5..500).",
)
@click.option(
    "--env-file",
    "env_file",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Path to a .env file holding provider credentials. Defaults to '.env' "
        "in the working directory. Never overrides variables already set in "
        "the shell (precedence: flag > environment > .env)."
    ),
)
@click.option(
    "--no-dotenv",
    "no_dotenv",
    is_flag=True,
    default=False,
    help=(
        "Skip loading .env entirely. Useful for CI and for debugging so a "
        "stray local file cannot change the run."
    ),
)
@click.option("--debug", is_flag=True, default=False, help="Print stack traces.")
@click.version_option(version=__version__, prog_name=PROG)
def main(
    symbol: str,
    htf_candles: int,
    mtf_candles: int,
    ltf_candles: int,
    htf_interval: str,
    mtf_interval: str,
    ltf_interval: str,
    swing_lookback: int,
    distance_reference: str,
    no_atr: bool,
    base_url: str | None,
    output_dir: str,
    print_stdout: bool,
    input_csv: str | None,
    htf_file: str | None,
    mtf_file: str | None,
    ltf_file: str | None,
    max_prompt_bytes: int | None,
    dry_run: bool,
    provider: str,
    twelvedata_key: str | None,
    oanda_token: str | None,
    oanda_account_id: str | None,
    oanda_env: str,
    candles_only: bool,
    review_interval: str,
    review_candles: int,
    validate_setup: bool,
    entry: float | None,
    tp: float | None,
    sl: float | None,
    direction: str | None,
    order_status: str | None,
    unfilled: bool,
    filled: bool,
    validate_interval: str,
    validate_candles: int,
    env_file: str | None,
    no_dotenv: bool,
    debug: bool,
) -> None:
    """CLI entrypoint. Parses args, then delegates to :func:`run`."""

    _ensure_utf8_streams()

    # .env is a convenience layer UNDER the shell environment (see
    # CREDENTIAL_PRECEDENCE): loading happens here, before run(), so the
    # programmatic run() path and the test suite stay free of ambient files.
    _load_dotenv_or_warn(env_file, no_dotenv=no_dotenv)

    base_urls = cfg.DEFAULT_BASE_URLS
    if base_url:
        remainder = tuple(
            host for host in cfg.DEFAULT_BASE_URLS if host.rstrip("/") != base_url.rstrip("/")
        )
        base_urls = (base_url,) + remainder

    try:
        run(
            symbol,
            htf_candles=htf_candles,
            mtf_candles=mtf_candles,
            ltf_candles=ltf_candles,
            htf_interval=htf_interval,
            mtf_interval=mtf_interval,
            ltf_interval=ltf_interval,
            swing_lookback=swing_lookback,
            distance_reference=distance_reference,
            include_atr=not no_atr,
            output_dir=output_dir,
            print_stdout=print_stdout,
            base_urls=base_urls,
            input_csv=input_csv,
            htf_file=htf_file,
            mtf_file=mtf_file,
            ltf_file=ltf_file,
            max_prompt_bytes=max_prompt_bytes,
            dry_run=dry_run,
            provider=provider,
            twelvedata_key=twelvedata_key,
            oanda_token=oanda_token,
            oanda_account_id=oanda_account_id,
            oanda_env=oanda_env,
            candles_only=candles_only,
            review_interval=review_interval,
            review_candles=review_candles,
            validate_setup=validate_setup,
            entry=entry,
            tp=tp,
            sl=sl,
            direction=direction,
            order_status=order_status,
            unfilled=unfilled,
            filled=filled,
            validate_interval=validate_interval,
            validate_candles=validate_candles,
        )
    except SmcPromptError as exc:
        _error(str(exc))
        if debug:
            traceback.print_exc()
        sys.exit(exit_code_for(exc))
    except Exception as exc:  # pragma: no cover - defensive
        _error(f"Unexpected failure: {exc}.")
        if debug:
            traceback.print_exc()
        sys.exit(exit_code_for(exc))
