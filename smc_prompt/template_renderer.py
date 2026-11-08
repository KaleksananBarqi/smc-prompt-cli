"""Build the placeholder map and render the byte-frozen Jinja2 template.

Byte-stability contract (spec §8.1):
  * UTF-8, LF line endings only, no trailing whitespace, exactly one final ``\\n``.
  * Values are pre-formatted strings; the template uses plain substitution.
  * Missing/empty placeholders are a hard error — a prompt is never emitted
    with an empty field or an ``N/A`` placeholder.
"""

from __future__ import annotations

import html
import re
from decimal import Decimal
from datetime import datetime
from functools import lru_cache
from importlib import resources
from typing import Sequence

from jinja2 import Environment, StrictUndefined, TemplateError

from . import config as cfg
from .errors import SmcPromptError
from .models import (
    Candle,
    EqualLevel,
    FVG,
    FVGDirection,
    OPTIONAL_PLACEHOLDERS,
    OrderBlock,
    REQUIRED_PLACEHOLDERS,
    RenderedPrompt,
    SwingPoint,
    SwingType,
    TimeframeAnalysis,
)
from .structure_analyzer import format_dealing_range_summary

TEMPLATE_PACKAGE = "smc_prompt"
TEMPLATE_DIR = "templates"
TEMPLATE_NAME = "prompt_template.j2"

#: Non-empty token rendered when an ATR-dependent fact cannot be computed
#: (ATR unavailable or zero). Required placeholders must never be empty.
ATR_NA = "n/a"
#: Non-empty token rendered when volume facts cannot be computed.
VOLUME_NA = "n/a"
#: Volume-spike flag tokens.
VOLUME_SPIKE_YES = "yes"
VOLUME_SPIKE_NO = "no"
VOLUME_SPIKE_UNKNOWN = "unknown"

#: Name of the boolean the template uses to toggle the two ATR lines. Exposed
#: as a constant so tests/other modules can reference the exact spelling.
INCLUDE_ATR_VAR = "INCLUDE_ATR"

#: Name of the render variable carrying the data provider's display name, used
#: by the template's provenance line. It is a *render variable* rather than a
#: placeholder because the provider is chosen at runtime, which lets one
#: byte-frozen template serve Binance, Twelve Data and OANDA unchanged. It is
#: deliberately NOT added to ``payload`` / ``REQUIRED_PLACEHOLDERS``: the
#: unresolved-placeholder guard only reports names present in the payload, so a
#: provider value that happens to contain ``{{`` / ``}}`` cannot false-positive.
PROVIDER_NAME_VAR = "provider"

#: Nama variabel render opsional untuk baris konteks derivatif (Funding Rate & OI).
DERIVATIVES_CONTEXT_VAR = "DERIVATIVES_CONTEXT_LINE"

#: Nama variabel render opsional untuk baris acuan struktur BTCUSDT (deteksi SMT Divergence).
BTC_BENCHMARK_VAR = "BTC_BENCHMARK_LINE"

#: Matches a Jinja substitution tag naming a single placeholder, e.g. ``{{PAIR}}``
#: or ``{{ PAIR }}``. Used by :func:`check_unresolved_placeholders` instead of a
#: naive ``"{{" in text`` substring scan, which would false-positive on any
#: literal ``{{`` / ``}}`` that legitimately appears in the rendered body.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@lru_cache(maxsize=1)
def load_template_text() -> str:
    """Read the template asset once (source tree and installed wheel work).

    Cached so the byte-frozen asset is read exactly once per process; the
    content never changes at runtime, so caching cannot affect determinism.
    """

    try:
        asset = resources.files(TEMPLATE_PACKAGE).joinpath(
            TEMPLATE_DIR, TEMPLATE_NAME
        )
        return asset.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:  # pragma: no cover
        raise SmcPromptError(f"Prompt template asset not found ({exc}).")


def _template_env() -> Environment:
    """A fresh Jinja environment with byte-stable settings.

    ``trim_blocks=True`` makes a block tag (``{% ... %}``) consume only the
    trailing newline, so putting the two ATR lines inside a single
    ``{% if %}...{% endif %}`` leaves the surrounding prompt byte-identical in
    both the ATR-on and ATR-off cases. ``keep_trailing_newline=False`` plus the
    final :func:`_tidy` guarantees exactly one trailing LF.
    """

    return Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=False,
        newline_sequence="\n",
    )


def template_source() -> str:
    """Return the (unrendered) template source, centered on the ATR toggle."""

    return load_template_text()


def render_csv_table(
    candles: Sequence[Candle],
    *,
    htf: bool,
    price_format: cfg.PriceFormat | None = None,
) -> str:
    """Render the raw candle CSV block (no header, no index column).

    Column order is ``date,open,high,low,close,volume`` (HTF) /
    ``datetime,open,high,low,close,volume`` (LTF). Prices use the tick-size
    derived ``price_format`` (magnitude fallback when the tick is unknown). The
    ``volume`` column is rendered with :func:`config.fmt_volume` (plain
    ``str(Decimal)`` of the source kline volume) for byte-stability. The column
    order is documented by a legend line in the template prose rather than a
    header row (cheaper, and it keeps every data row uniform).
    """

    fmt = (price_format or cfg.PriceFormat()).fmt
    rows: list[str] = []
    for candle in candles:
        stamp = (
            cfg.fmt_htf_date(candle.open_time)
            if htf
            else cfg.fmt_ltf_datetime(candle.open_time)
        )
        rows.append(
            ",".join(
                [
                    stamp,
                    fmt(candle.open),
                    fmt(candle.high),
                    fmt(candle.low),
                    fmt(candle.close),
                    cfg.fmt_volume(candle.volume),
                ]
            )
        )
    return "\n".join(rows)


def _swing_labels(
    swings: Sequence[SwingPoint], *, tol: Decimal
) -> list[str]:
    """Derive HH/HL/LH/LL (or EQH/EQL) labels per swing (full sequence).

    Each swing is compared to the previous swing of the SAME type. A higher high
    beyond ``tol`` -> ``HH``, a lower high beyond ``tol`` -> ``LH`` and anything
    within ``tol`` -> ``EQH`` (equal highs). Lows mirror this with ``HL`` / ``LL``
    / ``EQL``. The first swing of a given type has no predecessor and gets ``-``
    (single ASCII token). ``tol = equal_levels_atr_mult × ATR`` matches the
    Equal-Highs/Lows fact so labels and facts always agree.
    """

    labels: list[str] = []
    prev_high: Decimal | None = None
    prev_low: Decimal | None = None

    for swing in swings:
        if swing.type is SwingType.HIGH:
            if prev_high is None:
                labels.append("-")
            elif swing.price > prev_high + tol:
                labels.append("HH")
            elif swing.price < prev_high - tol:
                labels.append("LH")
            else:
                labels.append("EQH")
            prev_high = swing.price
        else:
            if prev_low is None:
                labels.append("-")
            elif swing.price > prev_low + tol:
                labels.append("HL")
            elif swing.price < prev_low - tol:
                labels.append("LL")
            else:
                labels.append("EQL")
            prev_low = swing.price

    return labels


def _render_swings(
    swings: Sequence[SwingPoint],
    *,
    htf: bool,
    limit: int,
    atr: Decimal | None = None,
    equal_mult: Decimal = cfg.DEFAULT_EQUAL_LEVELS_ATR_MULT,
    price_format: cfg.PriceFormat | None = None,
) -> str:
    """Render the detected swing sequence (oldest -> newest, capped to ``limit``).

    Row format: ``<stamp>,<SWING_HIGH|SWING_LOW>,<price>,<LABEL>`` where
    ``<stamp>`` is ``%Y-%m-%d`` for HTF and ``%Y-%m-%d %H:%M`` for LTF, and
    ``<LABEL>`` is one of ``HH`` / ``HL`` / ``LH`` / ``LL`` / ``EQH`` / ``EQL``
    / ``-`` (see :func:`_swing_labels`). Labels are computed over the FULL swing
    sequence and only the last ``limit`` rows are emitted, so the leftmost
    rendered row still carries its true relative label.
    """

    seq = list(swings)
    if not seq:
        return ""

    fmt = (price_format or cfg.PriceFormat()).fmt
    tol = equal_mult * atr if atr is not None else Decimal("0")
    labelled = list(zip(seq, _swing_labels(seq, tol=tol)))

    start = max(0, len(labelled) - limit)
    rows: list[str] = []
    for swing, label in labelled[start:]:
        stamp = (
            cfg.fmt_htf_date(swing.open_time)
            if htf
            else cfg.fmt_ltf_datetime(swing.open_time)
        )
        type_token = (
            "SH" if swing.type is SwingType.HIGH else "SL"
        )
        rows.append(
            ",".join([stamp, type_token, fmt(swing.price), label, swing.status])
        )
    return "\n".join(rows)


def _render_equal_levels(
    clusters: Sequence[EqualLevel],
    *,
    price_format: cfg.PriceFormat | None = None,
) -> str:
    """Render equal-level clusters as ``<price> (<n> swings)`` or ``NONE``.

    Multiple clusters are joined with ``"; "`` in chronological order. The
    literal ``NONE`` (non-empty) is emitted when no pool exists, as required by
    the renderer's non-empty-placeholder contract.
    """

    if not clusters:
        return "NONE"
    fmt = (price_format or cfg.PriceFormat()).fmt
    return "; ".join(
        f"{fmt(cluster.price)} ({cluster.count} swings)"
        for cluster in clusters
    )


def _is_within_poi_distance(
    lower: Decimal,
    upper: Decimal,
    current_price: Decimal | None,
    atr: Decimal | None,
    max_atr_mult: Decimal | None,
) -> bool:
    """Periksa apakah rentang zona [lower, upper] berada dalam radius max_atr_mult × ATR dari current_price."""
    if (
        current_price is None
        or atr is None
        or atr <= 0
        or max_atr_mult is None
    ):
        return True
    if lower <= current_price <= upper:
        dist = Decimal("0")
    else:
        dist = min(abs(current_price - lower), abs(current_price - upper))
    return dist <= (max_atr_mult * atr)


def _is_relevant_fvg(fvg: FVG, total_candles: int | None) -> bool:
    if not fvg.filled:
        return True
    if total_candles is not None and fvg.mitigated_index is not None:
        return fvg.mitigated_index >= (total_candles - 3)
    return False


def _render_fvg_table(
    fvgs: Sequence[FVG],
    *,
    htf: bool,
    limit: int,
    total_candles: int | None = None,
    price_format: cfg.PriceFormat | None = None,
    current_price: Decimal | None = None,
    atr: Decimal | None = None,
    max_atr_mult: Decimal | None = None,
) -> str:
    """Render the newest mechanical FVGs (oldest -> newest within the window).

    Row format: ``<stamp>,<BULL|BEAR>,<lower>,<upper>,<ce>,<status>`` where
    ``<stamp>`` is ``%m-%d`` (HTF) / ``%m-%d %H:%M`` (LTF), ``<ce>`` is the 50%
    Consequent Encroachment midpoint, and ``<status>`` is ``filled`` or
    ``unfilled``. Only unfilled gaps (or filled within the last 3 closed
    candles) within the optional ATR distance threshold are emitted, up to
    ``limit`` rows.
    """

    relevant = [
        fvg
        for fvg in fvgs
        if _is_relevant_fvg(fvg, total_candles)
        and _is_within_poi_distance(
            fvg.lower, fvg.upper, current_price, atr, max_atr_mult
        )
    ]
    if not relevant:
        return "NONE"

    fmt = (price_format or cfg.PriceFormat()).fmt
    window = list(relevant)[-limit:]
    rows: list[str] = []
    for fvg in window:
        stamp = (
            cfg.fmt_htf_date(fvg.birth_time)
            if htf
            else cfg.fmt_ltf_datetime(fvg.birth_time)
        )
        direction = (
            "BULL"
            if fvg.direction is FVGDirection.BULLISH
            else "BEAR"
        )
        status = "filled" if fvg.filled else "unfilled"
        ce_val = (
            fvg.ce
            if fvg.ce is not None
            else (fvg.lower + fvg.upper) / Decimal("2")
        )
        rows.append(
            ",".join(
                [
                    stamp,
                    direction,
                    fmt(fvg.lower),
                    fmt(fvg.upper),
                    fmt(ce_val),
                    status,
                ]
            )
        )
    return "\n".join(rows)


def _is_relevant_ob(ob: OrderBlock, total_candles: int | None = None) -> bool:
    """Filter Order Block: hanya kembalikan OB yang masih aktif (unmitigated atau tapped)."""
    return ob.status in ("unmitigated", "tapped")


def _render_ob_table(
    obs: Sequence[OrderBlock],
    *,
    htf: bool,
    limit: int = 5,
    total_candles: int | None = None,
    price_format: cfg.PriceFormat | None = None,
    current_price: Decimal | None = None,
    atr: Decimal | None = None,
    max_atr_mult: Decimal | None = None,
) -> str:
    """Render active / recently-mitigated Order Blocks (oldest -> newest)."""

    relevant = [
        ob
        for ob in obs
        if _is_relevant_ob(ob, total_candles)
        and _is_within_poi_distance(
            ob.lower, ob.upper, current_price, atr, max_atr_mult
        )
    ]
    if not relevant:
        return "NONE"

    fmt = (price_format or cfg.PriceFormat()).fmt
    window = list(relevant)[-limit:]
    rows: list[str] = []
    for ob in window:
        stamp = (
            cfg.fmt_htf_date(ob.birth_time)
            if htf
            else cfg.fmt_ltf_datetime(ob.birth_time)
        )
        type_token = (
            "BULL_OB" if ob.direction is FVGDirection.BULLISH else "BEAR_OB"
        )
        rows.append(
            ",".join(
                [
                    stamp,
                    type_token,
                    fmt(ob.lower),
                    fmt(ob.upper),
                    fmt(ob.mt),
                    ob.status,
                ]
            )
        )
    return "\n".join(rows)


def _atr_normalized_distance(
    current: Decimal, reference: Decimal, atr: Decimal | None
) -> str:
    """Return ``x{abs_distance/atr:.2f}`` guarding ``atr in (None, 0)``.

    Pre-formatted to a string to preserve byte-stability; ``n/a`` (non-empty)
    is emitted when ATR is unavailable or zero, so the placeholder contract
    still holds.
    """

    if atr is None or atr == 0:
        return ATR_NA
    return cfg.fmt_atr_distance(current, reference, atr)


def _atr_pct_of_price(analysis: TimeframeAnalysis, current_price: Decimal) -> str:
    """Render ``ATR`` as a percent of ``current_price`` (Phase 5, #6).

    Guards against a non-positive price or an unavailable/zero ATR by returning
    the non-empty literal ``n/a``.
    """

    atr = analysis.atr
    if atr is None or atr == 0 or current_price <= 0:
        return ATR_NA
    return cfg.fmt_atr_pct(atr, current_price)


def _volume_relative(analysis: TimeframeAnalysis) -> str:
    """Render relative volume (``last / mean``) or ``n/a`` when unavailable."""

    if analysis.volume is None:
        return VOLUME_NA
    return cfg.fmt_relative_volume(analysis.volume.relative)


def _volume_spike(analysis: TimeframeAnalysis) -> str:
    """Render the volume-spike flag token or ``unknown`` when unavailable."""

    if analysis.volume is None:
        return VOLUME_SPIKE_UNKNOWN
    return VOLUME_SPIKE_YES if analysis.volume.is_spike else VOLUME_SPIKE_NO


def _resolve_dealing_range(
    current_price: Decimal,
    analysis: TimeframeAnalysis,
    *,
    price_format: cfg.PriceFormat | None = None,
) -> str:
    """Ambil ringkasan dealing range dari analysis facts atau fallback berbasis swing high/low."""
    if analysis.reference_facts and analysis.reference_facts.dealing_range_summary:
        return analysis.reference_facts.dealing_range_summary
    return format_dealing_range_summary(
        current_price,
        analysis.swing_low,
        analysis.swing_high,
        price_format=price_format,
    )


def _render_timeframe_tables(
    analysis: TimeframeAnalysis,
    *,
    is_daily: bool,
    config: cfg.Config,
    current_price: Decimal | None = None,
    filter_poi_distance: bool = True,
) -> tuple[str, str, str, str, str]:
    """Render tabel swings, equal highs, equal lows, FVG table, dan Order Block table."""
    price_format = config.price_format
    swings_table = _render_swings(
        analysis.swings if analysis.swings else (analysis.swing_high, analysis.swing_low),
        htf=is_daily,
        limit=config.swings_table_rows,
        atr=analysis.atr,
        equal_mult=config.equal_levels_atr_mult,
        price_format=price_format,
    )
    eq_highs = _render_equal_levels(analysis.equal_highs, price_format=price_format)
    eq_lows = _render_equal_levels(analysis.equal_lows, price_format=price_format)

    filter_price = current_price if filter_poi_distance else None
    filter_mult = config.max_poi_atr_mult if filter_poi_distance else None

    fvg_table = _render_fvg_table(
        analysis.fvgs,
        htf=is_daily,
        limit=config.fvg_table_rows,
        total_candles=(analysis.closed_count or analysis.candle_count),
        price_format=price_format,
        current_price=filter_price,
        atr=analysis.atr,
        max_atr_mult=filter_mult,
    )
    ob_table = _render_ob_table(
        analysis.order_blocks,
        htf=is_daily,
        limit=config.ob_table_rows,
        total_candles=(analysis.closed_count or analysis.candle_count),
        price_format=price_format,
        current_price=filter_price,
        atr=analysis.atr,
        max_atr_mult=filter_mult,
    )
    return swings_table, eq_highs, eq_lows, fvg_table, ob_table


def _build_timeframe_payload(
    prefix: str,
    analysis: TimeframeAnalysis,
    candles: Sequence[Candle],
    *,
    interval_label: str,
    is_daily: bool,
    render_candle_count: int,
    current_price: Decimal,
    config: cfg.Config,
    filter_poi_distance: bool = True,
) -> dict[str, str]:
    """Format seluruh 16 placeholder field untuk satu timeframe (HTF, MTF, atau LTF)."""
    price_format = config.price_format
    rendered_candles = (
        list(candles)
        if config.full_candles
        else list(candles)[-render_candle_count:]
    )

    swings_table, eq_highs, eq_lows, fvg_table, ob_table = _render_timeframe_tables(
        analysis,
        is_daily=is_daily,
        config=config,
        current_price=current_price,
        filter_poi_distance=filter_poi_distance,
    )

    facts = analysis.reference_facts
    ref_high = (
        facts.ref_high_summary or f"- Ref HIGH: {facts.recent_high}"
        if facts is not None
        else ""
    )
    ref_low = (
        facts.ref_low_summary or f"- Ref LOW: {facts.recent_low}"
        if facts is not None
        else ""
    )
    dealing_range = (
        facts.dealing_range_summary
        if facts is not None and facts.dealing_range_summary
        else _resolve_dealing_range(current_price, analysis, price_format=price_format)
    )

    return {
        f"{prefix}_STRUCTURE_CLASS": analysis.structure_class.value,
        f"{prefix}_REF_HIGH_SUMMARY": ref_high,
        f"{prefix}_REF_LOW_SUMMARY": ref_low,
        f"{prefix}_DEALING_RANGE": dealing_range,
        f"{prefix}_VOLUME_RELATIVE": _volume_relative(analysis),
        f"{prefix}_VOLUME_SPIKE": _volume_spike(analysis),
        f"{prefix}_INTERVAL_LABEL": interval_label,
        f"{prefix}_CANDLE_COUNT": str(len(rendered_candles)),
        f"{prefix}_CANDLE_TABLE_CSV": render_csv_table(
            rendered_candles, htf=is_daily, price_format=price_format
        ),
        f"{prefix}_SWINGS_TABLE": swings_table,
        f"{prefix}_EQUAL_HIGHS": eq_highs,
        f"{prefix}_EQUAL_LOWS": eq_lows,
        f"{prefix}_FVG_TABLE": fvg_table,
        f"{prefix}_FVG_COUNT": str(config.fvg_table_rows),
        f"{prefix}_OB_TABLE": ob_table,
        f"{prefix}_OB_COUNT": str(config.ob_table_rows),
    }


def render_structure_context_block(
    analysis: TimeframeAnalysis,
    timeframe: str,
    current_price: Decimal,
    config: cfg.Config,
) -> str:
    """Render blok konteks struktur pasar SMC v2 (Dealing Range, Swings, FVG ce, OB mt)."""
    is_daily = not cfg.is_intraday_interval(timeframe)
    interval_lbl = cfg.interval_label(timeframe)

    dealing_range = _resolve_dealing_range(
        current_price, analysis, price_format=config.price_format
    )

    swings_table, eq_highs, eq_lows, fvg_table, ob_table = _render_timeframe_tables(
        analysis,
        is_daily=is_daily,
        config=config,
        current_price=current_price,
        filter_poi_distance=True,
    )

    lines = [
        f"### Konteks Struktur Pasar ({interval_lbl})",
        f"- Klasifikasi Struktur (Mekanis): {analysis.structure_class.value}",
        dealing_range,
        f"- Equal Highs (Liquidity Pool): {eq_highs}",
        f"- Equal Lows (Liquidity Pool): {eq_lows}",
        "",
        f"#### Sequence Swing Terdeteksi ({interval_lbl}, oldest→newest)",
        "```",
        swings_table,
        "```",
        "",
        f"#### FVG Mekanis Unfilled ({interval_lbl}, oldest→newest, kolom: stamp,tipe,lower,upper,ce,status)",
        "```",
        fvg_table,
        "```",
        "",
        f"#### Order Block Aktif ({interval_lbl}, oldest→newest, kolom: stamp,tipe,lower,upper,mt,status)",
        "```",
        ob_table,
        "```",
    ]
    return "\n".join(lines)


def build_payload(
    *,
    symbol: str,
    generated_at: datetime,
    current_price: Decimal,
    htf: TimeframeAnalysis,
    mtf: TimeframeAnalysis,
    ltf: TimeframeAnalysis,
    htf_candles: Sequence[Candle],
    mtf_candles: Sequence[Candle],
    ltf_candles: Sequence[Candle],
    config: cfg.Config,
) -> dict[str, str]:
    """Build the complete placeholder map (spec §10).

    All price values are rendered through ``config.price_format`` — the
    tick-size derived precision (magnitude fallback when the tick is unknown).
    For the frozen BTCUSDT sample both agree, so the artifact is unchanged.
    """
    if (
        htf.reference_facts is None
        or mtf.reference_facts is None
        or ltf.reference_facts is None
    ):
        raise SmcPromptError(
            "Reference facts missing from analysis; aborting before render."
        )

    fmt = config.price_format.fmt
    payload: dict[str, str] = {
        "PAIR": symbol.upper(),
        "GENERATED_AT_UTC": cfg.fmt_generated_at(generated_at),
        "SESSION_KILLZONE_UTC": cfg.fmt_session_killzone(generated_at),
        "CURRENT_PRICE": fmt(current_price),
        "ATR_PCT_OF_PRICE": _atr_pct_of_price(htf, current_price),
    }

    payload.update(
        _build_timeframe_payload(
            "HTF",
            htf,
            htf_candles,
            interval_label=config.htf_interval_label,
            is_daily=not cfg.is_intraday_interval(config.htf_interval),
            render_candle_count=config.htf_render_candles,
            current_price=current_price,
            config=config,
            filter_poi_distance=False,
        )
    )
    payload.update(
        _build_timeframe_payload(
            "MTF",
            mtf,
            mtf_candles,
            interval_label=config.mtf_interval_label,
            is_daily=not cfg.is_intraday_interval(config.mtf_interval),
            render_candle_count=config.mtf_render_candles,
            current_price=current_price,
            config=config,
            filter_poi_distance=True,
        )
    )
    payload.update(
        _build_timeframe_payload(
            "LTF",
            ltf,
            ltf_candles,
            interval_label=config.ltf_interval_label,
            is_daily=not cfg.is_intraday_interval(config.ltf_interval),
            render_candle_count=config.ltf_render_candles,
            current_price=current_price,
            config=config,
            filter_poi_distance=True,
        )
    )

    if config.include_atr:
        if htf.atr is None or mtf.atr is None or ltf.atr is None:
            raise SmcPromptError(
                "ATR requested but not computable; aborting before render."
            )
        payload["HTF_ATR14"] = cfg.fmt_atr(htf.atr)
        payload["MTF_ATR14"] = cfg.fmt_atr(mtf.atr)
        payload["LTF_ATR14"] = cfg.fmt_atr(ltf.atr)

    _validate_payload(payload, include_atr=config.include_atr)
    return payload


def _validate_payload(payload: dict[str, str], *, include_atr: bool) -> None:
    required = list(REQUIRED_PLACEHOLDERS)
    if include_atr:
        required.extend(OPTIONAL_PLACEHOLDERS)

    missing = [key for key in required if key not in payload]
    if missing:
        raise SmcPromptError(
            "Missing placeholder values: " + ", ".join(missing) + "."
        )

    empty = [key for key in required if not str(payload[key]).strip()]
    if empty:
        raise SmcPromptError(
            "Empty placeholder values: " + ", ".join(empty) + "."
        )


def _tidy(text: str) -> str:
    """Normalize to byte-stable output: LF only, no trailing ws, one final LF."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def check_unresolved_placeholders(
    text: str, declared: set[str] | None = None
) -> list[str]:
    """Return placeholder names left unresolved in ``text`` (Phase 5, #16).

    Uses a targeted ``\\{\\{\\s*\\w+\\s*\\}\\}`` regex instead of a naive
    ``"{{" in text`` substring scan, so a literal ``{{`` / ``}}`` that
    legitimately belongs to the rendered body cannot false-positive. When
    ``declared`` is given, only tags naming a **declared** placeholder are
    reported — a literal ``{{100}}`` in the body is ignored because ``100`` is
    not a declared placeholder, while a declared key that was somehow left
    un-substituted is reported. Undeclared tags are already rejected earlier by
    ``StrictUndefined`` during the render itself.
    """

    found = _PLACEHOLDER_RE.findall(text)
    if declared is None:
        return sorted(set(found))
    return sorted({name for name in found if name in declared})


def render(
    payload: dict[str, str],
    *,
    include_atr: bool = True,
    provider_name: str = "Binance",
    derivatives_context_line: str = "",
    btc_benchmark_line: str = "",
) -> RenderedPrompt:
    """Render the template with strict undefined handling.

    The two ATR lines are wrapped in ``{% if INCLUDE_ATR %}`` in the template;
    ``include_atr`` is passed as a render variable so ``--no-atr`` is driven by
    Jinja control flow rather than brittle line-string filtering. The output is
    byte-stable in both cases.

    ``provider_name`` is the data source named on the prompt's provenance line
    (e.g. ``Twelve Data``). It is a render variable rather than a payload
    placeholder so the frozen template stays provider-agnostic; the default
    reproduces the original Binance wording byte for byte.
    """

    env = _template_env()
    try:
        template = env.from_string(template_source())
        text = template.render(
            **{
                **payload,
                INCLUDE_ATR_VAR: include_atr,
                # A provider name is a literal string, never markup, and the
                # template is autoescape=False; escaping here is what keeps a
                # provider label from ever being read as a Jinja tag.
                PROVIDER_NAME_VAR: html.escape(provider_name, quote=False),
                DERIVATIVES_CONTEXT_VAR: derivatives_context_line,
                BTC_BENCHMARK_VAR: btc_benchmark_line,
            }
        )
    except TemplateError as exc:
        raise SmcPromptError(f"Template rendering failed: {exc}.")

    unresolved = check_unresolved_placeholders(text, set(payload))
    if unresolved:
        raise SmcPromptError(
            "Template rendering left unresolved placeholder(s): "
            + ", ".join(unresolved)
            + "; aborting."
        )

    return RenderedPrompt(text=_tidy(text), placeholders=dict(payload))
