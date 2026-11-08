"""Click entrypoint and the single orchestration function.

``cli.py`` is the only module that writes to stdout/stderr and the only module
that maps exceptions to process exit codes. It contains no analysis logic.
"""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import click

from . import __version__, config as cfg
from .bitunix_source import BitunixSource
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
from .gate_source import GateSource
from .market_acquisition import (
    AcquisitionMode,
    MarketSnapshot,
    acquire_market_snapshot,
    extract_tick_size as _extract_tick_size_impl,
    resolve_mode_intervals,
)
from .market_context import fetch_btc_benchmark_context, fetch_derivatives_context
from .mexc_source import MexcSource
from .models import Candle
from .oanda_source import OandaSource
from .output import deliver
from .provider_base import price_sanity_warning
from .review_renderer import TradeJournalSpec, render_review_markdown
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
from .template_renderer import build_payload, render, render_structure_context_block
from .trade_plan import ExcursionResult, TradePlan, build_trade_plan, compute_excursion

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
) -> TwelveDataSource | OandaSource | BitunixSource | MexcSource | GateSource:
    """Build the configured network data source (Binance is built separately).

    Every provider exposes the same public shape, so the analysis pipeline below
    is untouched by the choice.
    """

    if config.provider == cfg.PROVIDER_BITUNIX:
        return BitunixSource(config)
    if config.provider == cfg.PROVIDER_MEXC:
        return MexcSource(config)
    if config.provider == cfg.PROVIDER_GATE:
        return GateSource(config)
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


def _build_fetcher(
    provider_name: str,
    active_config: cfg.Config,
    *,
    offline_mode: bool,
    offline_htf: str | None,
    offline_mtf: str | None,
    offline_ltf: str | None,
    key: str | None,
    token: str | None,
    account_id: str | None,
    oanda_env: str,
) -> (
    DataFetcher
    | LocalCsvSource
    | TwelveDataSource
    | OandaSource
    | BitunixSource
    | MexcSource
    | GateSource
):
    """Membuat instance objek fetcher yang sesuai berdasarkan provider yang dipilih."""
    if offline_mode:
        return LocalCsvSource(
            active_config,
            htf_file=offline_htf,
            mtf_file=offline_mtf,
            ltf_file=offline_ltf,
        )
    if provider_name == cfg.PROVIDER_BINANCE:
        return DataFetcher(active_config)
    return _make_provider_source(
        active_config,
        twelvedata_key=key,
        oanda_token=token,
        oanda_account_id=account_id,
        oanda_env=oanda_env,
    )




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

    return _extract_tick_size_impl(entry)


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
    outcome: str | None = None,
    notes: str | None = None,
    exit_price: float | None = None,
    validate_interval: str = cfg.DEFAULT_VALIDATE_INTERVAL,
    validate_candles: int = cfg.DEFAULT_VALIDATE_CANDLES,
    no_fallback: bool = False,
    entry1: float | None = None,
    entry2: float | None = None,
    tp1: float | None = None,
    tp2: float | None = None,
    no_structure: bool = False,
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
        journal_info: list[str] = []
        if direction:
            journal_info.append(f"direction={direction}")
        if entry is not None:
            journal_info.append(f"entry={entry}")
        if sl is not None:
            journal_info.append(f"sl={sl}")
        if tp is not None:
            journal_info.append(f"tp={tp}")
        if outcome:
            journal_info.append(f"outcome={outcome}")
        if exit_price is not None:
            journal_info.append(f"exit={exit_price}")
        if notes:
            journal_info.append(f"notes={notes}")

        lines = [
            f"[{PROG}] DRY RUN — review mode, no file written, no klines fetched.",
            f"[{PROG}] symbol={config.symbol}",
            f"[{PROG}] provider={config.provider}",
            f"[{PROG}] data source={source}",
            f"[{PROG}] review_interval={review_interval} ({cfg.interval_label(review_interval)})",
            f"[{PROG}] review_candles={review_candles}",
        ]
        if journal_info:
            lines.append(f"[{PROG}] journal={' '.join(journal_info)}")
        lines.append(f"[{PROG}] output_dir={config.output_dir} stdout={print_stdout}")
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
    full_candles: bool = False,
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
    outcome: str | None = None,
    notes: str | None = None,
    exit_price: float | None = None,
    validate_interval: str = cfg.DEFAULT_VALIDATE_INTERVAL,
    validate_candles: int = cfg.DEFAULT_VALIDATE_CANDLES,
    no_fallback: bool = False,
    entry1: float | None = None,
    entry2: float | None = None,
    tp1: float | None = None,
    tp2: float | None = None,
    partial: bool = False,
    no_structure: bool = False,
) -> RunResult:
    """Chain fetch -> analyze -> render -> output. Raises on any failure."""

    if candles_only and validate_setup:
        raise ConfigError("Cannot specify both --candles-only and --validate-setup.")

    resolved_entry1 = entry1 if entry1 is not None else entry
    resolved_tp1 = tp1 if tp1 is not None else tp
    inferred_direction: str | None = None
    resolved_order_status: str | None = None

    if validate_setup:
        if unfilled and filled:
            raise ConfigError("Cannot specify both --unfilled and --filled.")
        if partial and (unfilled or filled):
            raise ConfigError("Cannot specify both --partial and other status flags.")

        if unfilled:
            resolved_order_status = "unfilled"
        elif filled:
            resolved_order_status = "filled"
        elif partial:
            resolved_order_status = "partial"
        elif order_status is not None:
            resolved_order_status = cfg.normalize_order_status(order_status)
        else:
            raise ConfigError(
                "--validate-setup requires specifying order status: use --status [unfilled|partial|filled] "
                "(or flag --unfilled / --partial / --filled)."
            )

        if resolved_entry1 is None or resolved_tp1 is None:
            raise ConfigError("--validate-setup requires both --entry and --tp.")

        plan = build_trade_plan(
            direction=direction,
            entry1=resolved_entry1,
            entry2=entry2,
            sl=sl,
            tp1=resolved_tp1,
            tp2=tp2,
        )
        inferred_direction = plan.direction

        cfg.validate_interval(validate_interval, flag="--validate-interval")
        cfg.provider_interval(provider, validate_interval)
        cfg.validate_validate_candles(validate_candles)
        htf_interval, mtf_interval, ltf_interval = resolve_mode_intervals(
            htf_interval,
            mtf_interval,
            ltf_interval,
            target_interval=validate_interval,
        )

    elif candles_only:
        cfg.validate_interval(review_interval, flag="--review-interval")
        cfg.provider_interval(provider, review_interval)
        cfg.validate_review_candles(review_candles)
        htf_interval, mtf_interval, ltf_interval = resolve_mode_intervals(
            htf_interval,
            mtf_interval,
            ltf_interval,
            target_interval=review_interval,
        )

        if resolved_entry1 is not None and resolved_tp1 is not None:
            plan = build_trade_plan(
                direction=direction,
                entry1=resolved_entry1,
                entry2=entry2,
                sl=sl,
                tp1=resolved_tp1,
                tp2=tp2,
            )
            inferred_direction = plan.direction
        elif direction is not None:
            dir_clean = direction.lower().strip()
            if dir_clean not in ("long", "short"):
                raise ConfigError(f"Invalid direction '{direction}'; must be 'long' or 'short'.")
            inferred_direction = dir_clean
        elif resolved_entry1 is not None and resolved_tp1 is not None:
            inferred_direction = "long" if Decimal(str(resolved_tp1)) > Decimal(str(resolved_entry1)) else "short"

    key, token, account_id = _resolve_credentials(
        twelvedata_key, oanda_token, oanda_account_id
    )

    config = cfg.build_config(
        symbol,
        htf_candles=htf_candles,
        mtf_candles=mtf_candles,
        ltf_candles=ltf_candles,
        full_candles=full_candles,
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
            entry=resolved_entry1,
            tp=resolved_tp1,
            sl=sl,
            direction=inferred_direction,
            order_status=resolved_order_status,
            outcome=outcome,
            notes=notes,
            exit_price=exit_price,
            validate_interval=validate_interval,
            validate_candles=validate_candles,
            no_fallback=no_fallback,
            entry1=resolved_entry1,
            entry2=entry2,
            tp1=resolved_tp1,
            tp2=tp2,
            no_structure=no_structure,
        )
        return RunResult("", "", False, False, (), dry_run=True)

    acq_mode: AcquisitionMode = (
        "candles_only"
        if candles_only
        else ("validate_setup" if validate_setup else "prompt")
    )
    single_interval = (
        review_interval
        if candles_only
        else (validate_interval if validate_setup else None)
    )
    single_candles = (
        review_candles
        if candles_only
        else (validate_candles if validate_setup else None)
    )

    snapshot = acquire_market_snapshot(
        config,
        mode=acq_mode,
        offline_mode=offline_mode,
        offline_htf=offline_htf,
        offline_mtf=offline_mtf,
        offline_ltf=offline_ltf,
        no_fallback=no_fallback,
        no_structure=no_structure,
        single_interval=single_interval,
        single_candles=single_candles,
        key=key,
        token=token,
        account_id=account_id,
        oanda_env=oanda_env,
        source_factory=_build_fetcher,
        warn_callback=_warn,
    )
    config = snapshot.active_config
    generated_at = snapshot.generated_at
    warnings = list(snapshot.warnings)

    # Post-trade review / candles-only fast-path
    if snapshot.mode == "candles_only":
        selected_candles = list(snapshot.selected_candles)

        e1_dec = Decimal(str(resolved_entry1)) if resolved_entry1 is not None else None
        e2_dec = Decimal(str(entry2)) if entry2 is not None else None
        tp1_dec = Decimal(str(resolved_tp1)) if resolved_tp1 is not None else None
        tp2_dec = Decimal(str(tp2)) if tp2 is not None else None
        sl_dec = Decimal(str(sl)) if sl is not None else None
        exit_dec = Decimal(str(exit_price)) if exit_price is not None else None

        excursion_res: ExcursionResult | None = None
        if e1_dec is not None and tp1_dec is not None:
            try:
                rev_plan = build_trade_plan(
                    direction=inferred_direction or direction,
                    entry1=e1_dec,
                    entry2=e2_dec,
                    sl=sl_dec,
                    tp1=tp1_dec,
                    tp2=tp2_dec,
                )
                excursion_res = compute_excursion(
                    rev_plan,
                    selected_candles,
                    order_status="filled",
                )
            except Exception:
                excursion_res = None

        journal = TradeJournalSpec(
            direction=inferred_direction or direction,
            entry_price=e1_dec,
            entry2_price=e2_dec,
            sl_price=sl_dec,
            tp_price=tp1_dec,
            tp2_price=tp2_dec,
            exit_price=exit_dec,
            outcome=outcome,
            notes=notes,
            excursion=excursion_res,
        )

        review_text = render_review_markdown(
            symbol=config.symbol,
            interval=review_interval,
            candles=selected_candles,
            provider_label=config.provider_label,
            generated_at=generated_at,
            price_format=config.price_format,
            journal=journal,
            structure_block=snapshot.structure_block,
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
    if snapshot.mode == "validate_setup":
        selected_candles = list(snapshot.selected_candles)

        assert inferred_direction is not None
        assert resolved_order_status is not None
        assert resolved_entry1 is not None
        assert resolved_tp1 is not None

        setup_spec = SetupSpec(
            symbol=config.symbol,
            direction=inferred_direction,
            entry_price=Decimal(str(resolved_entry1)),
            entry2_price=Decimal(str(entry2)) if entry2 is not None else None,
            tp_price=Decimal(str(resolved_tp1)),
            tp2_price=Decimal(str(tp2)) if tp2 is not None else None,
            sl_price=Decimal(str(sl)) if sl is not None else None,
            order_status=resolved_order_status,
        )

        analysis = analyze_setup(setup_spec, selected_candles)

        validation_text = render_validation_prompt(
            setup=setup_spec,
            analysis=analysis,
            candles=selected_candles,
            interval=validate_interval,
            provider_label=config.provider_label,
            generated_at=generated_at,
            price_format=config.price_format,
            structure_block=snapshot.structure_block,
            derivatives_context_line=snapshot.derivatives_context_line,
            btc_benchmark_line=snapshot.btc_benchmark_line,
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

    # Prompt Mode
    htf_raw = list(snapshot.htf_raw)
    mtf_raw = list(snapshot.mtf_raw)
    ltf_raw = list(snapshot.ltf_raw)
    current_price = snapshot.current_price
    assert current_price is not None

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
        derivatives_context_line=snapshot.derivatives_context_line,
        btc_benchmark_line=snapshot.btc_benchmark_line,
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
        "public market data (Binance / Bitunix / MEXC / Gate.io / Twelve Data / OANDA). Useful for "
        "crypto and FX/metals. Run without arguments or with -i for Interactive Mode. "
        "No LLM calls, no reasoning, no trading."
    ),
)
@click.argument("symbol", required=False, default=None)
@click.option(
    "-i",
    "--interactive",
    "interactive",
    is_flag=True,
    default=False,
    help="Luncurkan Terminal Interactive Wizard (mode panduan interaktif ramah pengguna).",
)
@click.option(
    "--provider",
    type=click.Choice(list(cfg.PROVIDERS)),
    default=cfg.PROVIDER_BINANCE,
    show_default=True,
    help=(
        "Data provider. Binance, Bitunix, MEXC, and Gate.io serve crypto futures; "
        "Twelve Data and OANDA serve spot FX/metals (XAUUSD)."
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
    "--full-candles",
    is_flag=True,
    default=False,
    help="Render the full analysis window of closed candles in the raw candle tables.",
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
    "--entry1",
    "entry",
    type=float,
    default=None,
    help="Planned entry (Entry 1 / Aggressive) price level for --validate-setup or --review.",
)
@click.option(
    "--entry2",
    "entry2",
    type=float,
    default=None,
    help="Planned Entry 2 (Moderate / 50% CE/MT) price level for --validate-setup or --review.",
)
@click.option(
    "--tp",
    "--tp1",
    "tp",
    type=float,
    default=None,
    help="Planned take profit (TP1 / Nearest DOL) target price level for --validate-setup or --review.",
)
@click.option(
    "--tp2",
    "tp2",
    type=float,
    default=None,
    help="Planned Take Profit 2 (TP2 / Main DOL) target price level for --validate-setup or --review.",
)
@click.option(
    "--sl",
    type=float,
    default=None,
    help="Planned stop loss price level for --validate-setup or --review (optional).",
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
    "--outcome",
    "--trade-result",
    "outcome",
    type=click.Choice(["Hit TP", "Hit SL", "BE", "Cut Manual"], case_sensitive=False),
    default=None,
    help="Trade outcome for post-trade review journal: 'Hit TP', 'Hit SL', 'BE', 'Cut Manual'.",
)
@click.option(
    "--notes",
    "--review-notes",
    "notes",
    type=str,
    default=None,
    help="Evaluation / journal notes for post-trade review.",
)
@click.option(
    "--exit-price",
    "--exit",
    "--hit-price",
    "exit_price",
    type=float,
    default=None,
    help=(
        "Actual exit / hit price level for post-trade review journal. "
        "Used to calculate Realized PnL points and R-Multiple. "
        "Defaults are suggested by wizard based on trade outcome (Hit TP/SL/BE)."
    ),
)
@click.option(
    "--order-status",
    "--status",
    "order_status",
    type=click.Choice(["unfilled", "filled", "partial", "pending", "triggered"], case_sensitive=False),
    default=None,
    help=(
        "Execution status of your order at the exchange: 'unfilled' (limit order pending), "
        "'partial' (E1 filled, E2 pending), or 'filled' (position active). Required for --validate-setup."
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
    "--partial",
    "partial",
    is_flag=True,
    default=False,
    help="Shortcut for --order-status partial (E1 terisi, E2 masih pending).",
)
@click.option(
    "--filled",
    "filled",
    is_flag=True,
    default=False,
    help="Shortcut for --order-status filled (position active, sudah terjemput).",
)
@click.option(
    "--no-structure",
    "no_structure",
    is_flag=True,
    default=False,
    help="Nonaktifkan penyertaan ringkasan struktur Layer-A pada mode review dan validasi setup.",
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
@click.option(
    "--no-fallback",
    "no_fallback",
    is_flag=True,
    default=False,
    help="Nonaktifkan pengalihan otomatis antar-exchange (Strict Provider Mode).",
)
@click.option("--debug", is_flag=True, default=False, help="Print stack traces.")
@click.version_option(version=__version__, prog_name=PROG)
def main(
    symbol: str | None,
    interactive: bool,
    htf_candles: int,
    mtf_candles: int,
    ltf_candles: int,
    full_candles: bool,
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
    outcome: str | None,
    notes: str | None,
    exit_price: float | None,
    order_status: str | None,
    unfilled: bool,
    filled: bool,
    validate_interval: str,
    validate_candles: int,
    env_file: str | None,
    no_dotenv: bool,
    no_fallback: bool,
    debug: bool,
    entry2: float | None = None,
    tp2: float | None = None,
    partial: bool = False,
    no_structure: bool = False,
) -> None:
    """CLI entrypoint. Parses args, then delegates to :func:`run` or :func:`run_interactive_wizard`."""

    _ensure_utf8_streams()

    # .env adalah convenience layer di bawah environment shell (lihat
    # CREDENTIAL_PRECEDENCE): pemuatan dilakukan di sini sebelum run() dan wizard,
    # agar kredensial selalu siap digunakan pada seluruh jalur eksekusi CLI.
    _load_dotenv_or_warn(env_file, no_dotenv=no_dotenv)

    # If --interactive is requested or no symbol is provided (and not in dry-run mode),
    # launch the interactive wizard.
    if interactive or (symbol is None and not dry_run):
        from .interactive import run_interactive_wizard
        try:
            run_interactive_wizard()
        except SmcPromptError as exc:
            _error(str(exc))
            if debug:
                traceback.print_exc()
            sys.exit(exit_code_for(exc))
        return

    resolved_symbol = "" if symbol is None else symbol

    base_urls = cfg.DEFAULT_BASE_URLS
    if base_url:
        remainder = tuple(
            host for host in cfg.DEFAULT_BASE_URLS if host.rstrip("/") != base_url.rstrip("/")
        )
        base_urls = (base_url,) + remainder

    try:
        run(
            resolved_symbol,
            htf_candles=htf_candles,
            mtf_candles=mtf_candles,
            ltf_candles=ltf_candles,
            full_candles=full_candles,
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
            outcome=outcome,
            notes=notes,
            exit_price=exit_price,
            order_status=order_status,
            unfilled=unfilled,
            filled=filled,
            validate_interval=validate_interval,
            validate_candles=validate_candles,
            no_fallback=no_fallback,
            entry2=entry2,
            tp2=tp2,
            partial=partial,
            no_structure=no_structure,
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
