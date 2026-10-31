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
) -> str:
    """Render the newest mechanical FVGs (oldest -> newest within the window).

    Row format: ``<stamp>,<BULL|BEAR>,<lower>,<upper>,<status>`` where
    ``<stamp>`` is ``%m-%d`` (HTF) / ``%m-%d %H:%M`` (LTF) and ``<status>`` is
    ``filled`` or ``unfilled``. Only unfilled gaps (or filled within the last
    3 closed candles) are emitted, up to ``limit`` rows.
    """

    relevant = [fvg for fvg in fvgs if _is_relevant_fvg(fvg, total_candles)]
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
        rows.append(
            ",".join(
                [
                    stamp,
                    direction,
                    fmt(fvg.lower),
                    fmt(fvg.upper),
                    status,
                ]
            )
        )
    return "\n".join(rows)


def _is_relevant_ob(ob: OrderBlock, total_candles: int | None) -> bool:
    if ob.status in ("unmitigated", "tapped"):
        return True
    if ob.status == "invalidated" and total_candles is not None and ob.invalidated_index is not None:
        return ob.invalidated_index >= (total_candles - 3)
    return False


def _render_ob_table(
    obs: Sequence[OrderBlock],
    *,
    htf: bool,
    limit: int = 5,
    total_candles: int | None = None,
    price_format: cfg.PriceFormat | None = None,
) -> str:
    """Render active / recently-mitigated Order Blocks (oldest -> newest)."""

    relevant = [ob for ob in obs if _is_relevant_ob(ob, total_candles)]
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

    price_format = config.price_format
    fmt = price_format.fmt

    htf_swings = _render_swings(
        htf.swings if htf.swings else (htf.swing_high, htf.swing_low),
        htf=True,
        limit=config.swings_table_rows,
        atr=htf.atr,
        equal_mult=config.equal_levels_atr_mult,
        price_format=price_format,
    )
    mtf_swings = _render_swings(
        mtf.swings if mtf.swings else (mtf.swing_high, mtf.swing_low),
        # MTF carries a datetime stamp (like LTF), not the HTF date stamp.
        htf=False,
        limit=config.swings_table_rows,
        atr=mtf.atr,
        equal_mult=config.equal_levels_atr_mult,
        price_format=price_format,
    )
    ltf_swings = _render_swings(
        ltf.swings if ltf.swings else (ltf.swing_high, ltf.swing_low),
        htf=False,
        limit=config.swings_table_rows,
        atr=ltf.atr,
        equal_mult=config.equal_levels_atr_mult,
        price_format=price_format,
    )

    htf_facts = htf.reference_facts
    mtf_facts = mtf.reference_facts
    ltf_facts = ltf.reference_facts
    if htf_facts is None or mtf_facts is None or ltf_facts is None:
        raise SmcPromptError(
            "Reference facts missing from analysis; aborting before render."
        )

    htf_render = (
        list(htf_candles)
        if config.full_candles
        else list(htf_candles)[-config.htf_render_candles :]
    )
    mtf_render = (
        list(mtf_candles)
        if config.full_candles
        else list(mtf_candles)[-config.mtf_render_candles :]
    )
    ltf_render = (
        list(ltf_candles)
        if config.full_candles
        else list(ltf_candles)[-config.ltf_render_candles :]
    )

    payload: dict[str, str] = {
        "PAIR": symbol.upper(),
        "GENERATED_AT_UTC": cfg.fmt_generated_at(generated_at),
        "CURRENT_PRICE": fmt(current_price),
        "ATR_PCT_OF_PRICE": _atr_pct_of_price(htf, current_price),
        "HTF_STRUCTURE_CLASS": htf.structure_class.value,
        "HTF_REF_HIGH_SUMMARY": htf_facts.ref_high_summary or f"- Ref HIGH: {htf_facts.recent_high}",
        "HTF_REF_LOW_SUMMARY": htf_facts.ref_low_summary or f"- Ref LOW: {htf_facts.recent_low}",
        "HTF_VOLUME_RELATIVE": _volume_relative(htf),
        "HTF_VOLUME_SPIKE": _volume_spike(htf),
        "HTF_INTERVAL_LABEL": config.htf_interval_label,
        "HTF_CANDLE_COUNT": str(len(htf_render)),
        "HTF_CANDLE_TABLE_CSV": render_csv_table(
            htf_render, htf=True, price_format=price_format
        ),
        "HTF_SWINGS_TABLE": htf_swings,
        "HTF_EQUAL_HIGHS": _render_equal_levels(
            htf.equal_highs, price_format=price_format
        ),
        "HTF_EQUAL_LOWS": _render_equal_levels(
            htf.equal_lows, price_format=price_format
        ),
        "HTF_FVG_TABLE": _render_fvg_table(
            htf.fvgs,
            htf=True,
            limit=config.fvg_table_rows,
            total_candles=(htf.closed_count or htf.candle_count),
            price_format=price_format,
        ),
        "HTF_FVG_COUNT": str(config.fvg_table_rows),
        "HTF_OB_TABLE": _render_ob_table(
            htf.order_blocks,
            htf=True,
            limit=config.ob_table_rows,
            total_candles=(htf.closed_count or htf.candle_count),
            price_format=price_format,
        ),
        "HTF_OB_COUNT": str(config.ob_table_rows),
        "MTF_STRUCTURE_CLASS": mtf.structure_class.value,
        "MTF_REF_HIGH_SUMMARY": mtf_facts.ref_high_summary or f"- Ref HIGH: {mtf_facts.recent_high}",
        "MTF_REF_LOW_SUMMARY": mtf_facts.ref_low_summary or f"- Ref LOW: {mtf_facts.recent_low}",
        "MTF_VOLUME_RELATIVE": _volume_relative(mtf),
        "MTF_VOLUME_SPIKE": _volume_spike(mtf),
        "MTF_INTERVAL_LABEL": config.mtf_interval_label,
        "MTF_CANDLE_COUNT": str(len(mtf_render)),
        "MTF_CANDLE_TABLE_CSV": render_csv_table(
            mtf_render, htf=False, price_format=price_format
        ),
        "MTF_SWINGS_TABLE": mtf_swings,
        "MTF_EQUAL_HIGHS": _render_equal_levels(
            mtf.equal_highs, price_format=price_format
        ),
        "MTF_EQUAL_LOWS": _render_equal_levels(
            mtf.equal_lows, price_format=price_format
        ),
        "MTF_FVG_TABLE": _render_fvg_table(
            mtf.fvgs,
            htf=False,
            limit=config.fvg_table_rows,
            total_candles=(mtf.closed_count or mtf.candle_count),
            price_format=price_format,
        ),
        "MTF_FVG_COUNT": str(config.fvg_table_rows),
        "MTF_OB_TABLE": _render_ob_table(
            mtf.order_blocks,
            htf=False,
            limit=config.ob_table_rows,
            total_candles=(mtf.closed_count or mtf.candle_count),
            price_format=price_format,
        ),
        "MTF_OB_COUNT": str(config.ob_table_rows),
        "LTF_STRUCTURE_CLASS": ltf.structure_class.value,
        "LTF_REF_HIGH_SUMMARY": ltf_facts.ref_high_summary or f"- Ref HIGH: {ltf_facts.recent_high}",
        "LTF_REF_LOW_SUMMARY": ltf_facts.ref_low_summary or f"- Ref LOW: {ltf_facts.recent_low}",
        "LTF_VOLUME_RELATIVE": _volume_relative(ltf),
        "LTF_VOLUME_SPIKE": _volume_spike(ltf),
        "LTF_INTERVAL_LABEL": config.ltf_interval_label,
        "LTF_CANDLE_COUNT": str(len(ltf_render)),
        "LTF_CANDLE_TABLE_CSV": render_csv_table(
            ltf_render, htf=False, price_format=price_format
        ),
        "LTF_SWINGS_TABLE": ltf_swings,
        "LTF_EQUAL_HIGHS": _render_equal_levels(
            ltf.equal_highs, price_format=price_format
        ),
        "LTF_EQUAL_LOWS": _render_equal_levels(
            ltf.equal_lows, price_format=price_format
        ),
        "LTF_FVG_TABLE": _render_fvg_table(
            ltf.fvgs,
            htf=False,
            limit=config.fvg_table_rows,
            total_candles=(ltf.closed_count or ltf.candle_count),
            price_format=price_format,
        ),
        "LTF_FVG_COUNT": str(config.fvg_table_rows),
        "LTF_OB_TABLE": _render_ob_table(
            ltf.order_blocks,
            htf=False,
            limit=config.ob_table_rows,
            total_candles=(ltf.closed_count or ltf.candle_count),
            price_format=price_format,
        ),
        "LTF_OB_COUNT": str(config.ob_table_rows),
    }

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
