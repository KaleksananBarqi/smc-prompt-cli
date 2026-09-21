"""Immutable defaults, configuration dataclass and byte-stable formatting rules.

Values come from ``docs/DESIGN_SPEC.md`` §11 (defaults) and §8.1 (formatting
primitives). This module is a leaf: it must not import any other smc_prompt
module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Sequence

from .errors import ConfigError

# --------------------------------------------------------------------------
# Intervals / endpoint paths (no API key, read-only public market data only)
# --------------------------------------------------------------------------

HTF_INTERVAL: str = "1d"
MTF_INTERVAL: str = "4h"
LTF_INTERVAL: str = "1h"

#: Every kline interval Binance Spot accepts, in ascending-duration order. Used
#: to validate ``--htf-interval`` / ``--mtf-interval`` / ``--ltf-interval``.
BINANCE_INTERVALS: tuple[str, ...] = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
)

#: Human-readable label rendered in the prompt for each Binance interval. Kept
#: explicit (rather than a blanket ``.upper()``) so that minute intervals stay
#: lowercase and cannot collide with the month interval ``1M``. The defaults
#: reproduce the frozen labels ``Daily`` (for ``1d``) and ``1H`` (for ``1h``).
INTERVAL_LABELS: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6H",
    "8h": "8H",
    "12h": "12H",
    "1d": "Daily",
    "3d": "3D",
    "1w": "1W",
    "1M": "1M",
}


def validate_interval(value: str, *, flag: str = "--htf-interval") -> str:
    """Return ``value`` when it is a valid Binance interval, else raise.

    Raises :class:`~smc_prompt.errors.ConfigError` (exit 2) listing the full
    allowed set so the message is self-documenting.
    """

    if value not in BINANCE_INTERVALS:
        raise ConfigError(
            f"{flag} must be one of: " + ", ".join(BINANCE_INTERVALS) + "."
        )
    return value


def interval_label(interval: str) -> str:
    """Return the prompt label for a Binance interval (falls back to verbatim)."""

    return INTERVAL_LABELS.get(interval, interval)

KLINES_PATH: str = "/api/v3/klines"
TICKER_PRICE_PATH: str = "/api/v3/ticker/price"
EXCHANGE_INFO_PATH: str = "/api/v3/exchangeInfo"
#: Binance server time (``GET /api/v3/time``). Preferring the exchange clock
#: over the host clock keeps ``is_closed`` and ``GENERATED_AT_UTC`` immune to a
#: skewed local clock. See ``data_fetcher.fetch_server_time``.
TIME_PATH: str = "/api/v3/time"

# --------------------------------------------------------------------------
# Base URL fallback list (orchestrator decision, see brief §3 notes)
# --------------------------------------------------------------------------

#: Approved public hosts, tried in order. Fail over on connection errors,
#: HTTP 451 (geo-block) and HTTP 403. Overridable constant.
DEFAULT_BASE_URLS: tuple[str, ...] = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)

# --------------------------------------------------------------------------
# Data providers (Binance / Twelve Data / OANDA)
# --------------------------------------------------------------------------
# The analysis pipeline is provider-agnostic: it consumes ``Sequence[Candle]``.
# A provider is any class exposing the fetcher-parity seam
# (``validate_symbol`` / ``fetch_klines`` / ``fetch_current_price`` /
# ``fetch_server_time`` / ``price_notes`` / ``with_now``). Binance is the
# default; the FX/metal providers make spot XAUUSD analyzable, which Binance
# ST> cannot serve (no fiat/metal instruments listed).

PROVIDER_BINANCE: str = "binance"
PROVIDER_TWELVEDATA: str = "twelvedata"
PROVIDER_OANDA: str = "oanda"
PROVIDERS: tuple[str, ...] = (
    PROVIDER_BINANCE,
    PROVIDER_TWELVEDATA,
    PROVIDER_OANDA,
)

#: Human-readable provider names used in messages / the prompt banner.
PROVIDER_LABELS: dict[str, str] = {
    PROVIDER_BINANCE: "Binance",
    PROVIDER_TWELVEDATA: "Twelve Data",
    PROVIDER_OANDA: "OANDA",
}

#: Canonical (Binance-style) interval -> Twelve Data ``interval`` parameter.
#: Twelve Data natively supports ``4h`` so the default trio maps 1:1.
TWELVEDATA_INTERVAL_MAP: dict[str, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1day",
    "1w": "1week",
    "1M": "1month",
}

#: Canonical interval -> OANDA v20 ``granularity``. OANDA natively supports H4
#: so the default trio maps 1:1.
OANDA_INTERVAL_MAP: dict[str, str] = {
    "1m": "M1",
    "5m": "M5",
    "15m": "M15",
    "30m": "M30",
    "1h": "H1",
    "4h": "H4",
    "1d": "D",
    "1w": "W",
    "1M": "M",
}

#: Default OANDA v20 host. ``practice`` (demo) is the default because a free
#: practice account is enough to read candles; ``live`` needs a funded account.
OANDA_ENV_PRACTICE: str = "practice"
OANDA_ENV_LIVE: str = "live"
OANDA_ENVS: tuple[str, ...] = (OANDA_ENV_PRACTICE, OANDA_ENV_LIVE)
OANDA_HOSTS: dict[str, str] = {
    OANDA_ENV_PRACTICE: "https://api-fxpractice.oanda.com",
    OANDA_ENV_LIVE: "https://api-fxtrade.oanda.com",
}

#: Default Twelve Data host.
TWELVEDATA_HOST: str = "https://api.twelvedata.com"

#: Canonical symbol -> provider spelling, for instruments whose provider code is
#: not derivable by the generic FX heuristic (see :func:`provider_symbol`).
PROVIDER_SYMBOLS: dict[str, dict[str, str]] = {
    PROVIDER_TWELVEDATA: {"XAUUSD": "XAU/USD"},
    PROVIDER_OANDA: {"XAUUSD": "XAU_USD"},
}

#: Price precision used when a provider has no exchange ``tickSize`` metadata.
#: Spot XAUUSD quotes on 2 decimals at every mainstream venue.
FALLBACK_TICK_DECIMALS: int = 2

#: The provider display name reaches the prompt through the ``{{provider}}``
#: render variable (see ``template_renderer.PROVIDER_NAME_VAR``) rather than a
#: placeholder, so one byte-frozen template serves Binance, Twelve Data and
#: OANDA. The mapping itself lives in :data:`PROVIDER_LABELS`.


def provider_label(provider: str) -> str:
    """Human-readable provider name (falls back to the raw slug)."""

    return PROVIDER_LABELS.get(provider, provider)


def provider_symbol(provider: str, symbol: str) -> str:
    """Map a canonical uppercase symbol to the provider's instrument code.

    Binance uses the canonical spelling verbatim. The FX providers accept a
    separated form (``XAU/USD`` for Twelve Data, ``XAU_USD`` for OANDA); the
    explicit :data:`PROVIDER_SYMBOLS` table covers the metallic pairs, and a
    deterministic 6-letter FX heuristic (``EURUSD`` -> ``EUR/USD`` /
    ``EUR_USD``) covers the common currency majors. Anything else is passed
    through verbatim so a caller can supply the provider's own instrument code.
    """

    normalized = (symbol or "").strip().upper()
    if provider == PROVIDER_BINANCE:
        return normalized

    explicit = PROVIDER_SYMBOLS.get(provider, {})
    if normalized in explicit:
        return explicit[normalized]

    if len(normalized) == 6 and normalized.isalpha():
        separator = "/" if provider == PROVIDER_TWELVEDATA else "_"
        return f"{normalized[:3]}{separator}{normalized[3:]}"
    return normalized


def provider_interval(provider: str, interval: str) -> str:
    """Translate a canonical interval into the provider's interval token.

    Raises :class:`~smc_prompt.errors.ConfigError` (exit 2) naming the flag and
    the provider's full allowed set when the interval is unsupported there.
    """

    if provider == PROVIDER_BINANCE:
        return interval

    if provider == PROVIDER_TWELVEDATA:
        table = TWELVEDATA_INTERVAL_MAP
    elif provider == PROVIDER_OANDA:
        table = OANDA_INTERVAL_MAP
    else:  # pragma: no cover - guarded by build_config
        return interval

    try:
        return table[interval]
    except KeyError as exc:
        raise ConfigError(
            f"Provider '{provider_label(provider)}' does not support the "
            f"interval '{interval}'. Allowed: " + ", ".join(table) + "."
        ) from exc

# --------------------------------------------------------------------------
# Numeric defaults
# --------------------------------------------------------------------------

DEFAULT_HTF_CANDLES: int = 60
DEFAULT_MTF_CANDLES: int = 186
DEFAULT_LTF_CANDLES: int = 168
DEFAULT_SWING_LOOKBACK: int = 5
DEFAULT_SWING_MERGE_ATR_MULT: Decimal = Decimal("0.5")
DEFAULT_ATR_PERIOD: int = 14
DEFAULT_STRUCTURE_MIN_SWINGS: int = 4
DEFAULT_STRUCTURE_LAST_SWINGS: int = 6
#: Rows kept in the rendered swing-sequence table(s), oldest -> newest capped to
#: the last N swings that are ACTUALLY rendered.
DEFAULT_SWINGS_TABLE_ROWS: int = 12
#: Tolerance for "equal" swing levels, expressed as a multiple of ATR(14). Used
#: both for the Equal-Highs/Lows liquidity-pool facts and for the HH/HL/LH/LL vs
#: EQH/EQL label derivation, so the two always agree.
DEFAULT_EQUAL_LEVELS_ATR_MULT: Decimal = Decimal("0.1")
#: Minimum Fair Value Gap size, expressed as a multiple of ATR(14). Gaps smaller
#: than ``fvg_atr_mult × ATR`` are treated as sub-noise and discarded. Mirrors
#: the equal-levels tolerance pattern (spec §4.7).
DEFAULT_FVG_ATR_MULT: Decimal = Decimal("0.1")
#: Number of newest Fair Value Gaps rendered per timeframe table (oldest -> newest
#: within the emitted window).
DEFAULT_FVG_TABLE_ROWS: int = 8
DEFAULT_CONTEXT_BUFFER: int = 50
FETCH_LIMIT_MAX: int = 1000
DEFAULT_REQUEST_TIMEOUT: float = 10.0
DEFAULT_RETRY_MAX: int = 3
DEFAULT_RETRY_BACKOFF_BASE: float = 1.0
DEFAULT_RETRY_BACKOFF_FACTOR: float = 2.0
DEFAULT_OUTPUT_DIR: str = "output"
DEFAULT_DELISTED_ZERO_VOLUME_STREAK: int = 3

#: Relative-volume lookback (candles) for the volume-spike fact (Phase 5, #16).
#: ``relative_volume = last / mean(last N volumes)``.
DEFAULT_VOLUME_MEAN_PERIOD: int = 20
#: A candle's volume is flagged as a spike when
#: ``relative_volume >= volume_spike_mult`` (Phase 5, #16).
DEFAULT_VOLUME_SPIKE_MULT: Decimal = Decimal("1.5")

#: Post-render size warning threshold in UTF-8 bytes (Phase 5, #11). Crossing
#: this emits a non-fatal WARN with the byte and approximate token count.
PROMPT_BYTES_WARN: int = 120_000
#: Rough bytes-per-token divisor used only for the informational token estimate
#: in the prompt-size warning; it never affects the rendered bytes.
PROMPT_BYTES_PER_TOKEN: int = 4

MIN_CANDLES: int = 10
MIN_SWING_LOOKBACK: int = 3

#: Review / candles-only mode defaults & bounds.
DEFAULT_REVIEW_CANDLES: int = 30
DEFAULT_REVIEW_INTERVAL: str = "1h"
MIN_REVIEW_CANDLES: int = 5
MAX_REVIEW_CANDLES: int = 500


def validate_review_candles(count: int) -> int:
    """Validate review candle count (must be between MIN_REVIEW_CANDLES and MAX_REVIEW_CANDLES)."""

    if count < MIN_REVIEW_CANDLES:
        raise ConfigError(
            f"--review-candles must be >= {MIN_REVIEW_CANDLES} (got {count})."
        )
    if count > MAX_REVIEW_CANDLES:
        raise ConfigError(
            f"--review-candles must be <= {MAX_REVIEW_CANDLES} (got {count})."
        )
    return count


#: Setup validator mode defaults & bounds.
DEFAULT_VALIDATE_CANDLES: int = 50
DEFAULT_VALIDATE_INTERVAL: str = "15m"
MIN_VALIDATE_CANDLES: int = 5
MAX_VALIDATE_CANDLES: int = 500


def validate_validate_candles(count: int) -> int:
    """Validate setup validation candle count (must be between MIN_VALIDATE_CANDLES and MAX_VALIDATE_CANDLES)."""

    if count < MIN_VALIDATE_CANDLES:
        raise ConfigError(
            f"--validate-candles must be >= {MIN_VALIDATE_CANDLES} (got {count})."
        )
    if count > MAX_VALIDATE_CANDLES:
        raise ConfigError(
            f"--validate-candles must be <= {MAX_VALIDATE_CANDLES} (got {count})."
        )
    return count

#: Valid values for ``--distance-reference``.
DISTANCE_REFERENCE_NEAREST: str = "nearest"
DISTANCE_REFERENCE_MOST_RECENT: str = "most-recent"
DISTANCE_REFERENCES: tuple[str, ...] = (
    DISTANCE_REFERENCE_NEAREST,
    DISTANCE_REFERENCE_MOST_RECENT,
)

# --------------------------------------------------------------------------
# Formatting primitives (byte-stable, spec §8.1)
# --------------------------------------------------------------------------


#: Highest precision Binance ever expresses (tickSize strings carry 8 dp).
MAX_PRICE_DECIMALS: int = 8


def price_decimals(value: Decimal | float | int | str) -> int:
    """Decimals chosen by magnitude: >=1000 -> 2dp, >=1 -> 4dp, else 8dp."""

    magnitude = abs(Decimal(str(value)))
    if magnitude >= Decimal("1000"):
        return 2
    if magnitude >= Decimal("1"):
        return 4
    return 8


def decimals_from_tick_size(
    tick_size: Decimal | float | int | str | None,
) -> int | None:
    """Derive the number of price decimals from a Binance ``tickSize``.

    ``PRICE_FILTER.tickSize`` arrives as a decimal string such as
    ``0.01000000`` (-> 2), ``0.00000100`` (-> 6) or ``1.00000000`` (-> 0). The
    trailing zeros are ignored by normalizing the value first. Returns ``None``
    when the tick is absent/unparseable or non-positive, so callers fall back to
    the documented magnitude-bucketed :func:`price_decimals` rule.
    """

    if tick_size is None:
        return None
    try:
        tick = Decimal(str(tick_size))
    except (InvalidOperation, ValueError):
        return None
    if not tick.is_finite() or tick <= 0:
        return None
    exponent = tick.normalize().as_tuple().exponent
    if not isinstance(exponent, int) or exponent >= 0:
        return 0
    return min(-exponent, MAX_PRICE_DECIMALS)


def fmt_price(value: Decimal | float | int | str) -> str:
    """Render a price using the magnitude-based decimal rule."""

    dec = Decimal(str(value))
    return f"{dec:.{price_decimals(dec)}f}"


def fmt_atr(value: Decimal | float | int | str) -> str:
    """Render ATR as a volatility scale figure (spec §12 oracle: ``145.00``).

    The frozen rendered example shows ``1850.00`` and ``145.00`` — 2 dp for
    unit-scale values — whereas magnitude-based ``fmt_price`` would render 145
    as ``145.0000``. Sub-unit values keep their magnitude-based precision so
    low-priced pairs do not collapse to ``0.00``.
    """

    dec = Decimal(str(value))
    dp = 2 if abs(dec) >= Decimal("1") else price_decimals(dec)
    return f"{dec:.{dp}f}"


def fmt_distance(current: Decimal, reference: Decimal) -> str:
    """Render signed distance as ``{sign}{pct:.2f}% ({sign}{abs})``.

    The absolute part uses the decimal precision implied by the *reference*
    price magnitude (spec §8.2 example: ``-0.52% (-330.00)`` from a 63450.00
    swing high, even though 330 itself is < 1000).
    """

    abs_val = Decimal(current) - Decimal(reference)
    pct = (abs_val / Decimal(reference)) * Decimal("100")
    sign = "+" if abs_val >= 0 else "-"
    dp = price_decimals(reference)
    return f"{sign}{abs(pct):.2f}% ({sign}{abs(abs_val):.{dp}f})"


@dataclass(frozen=True)
class PriceFormat:
    """Price rendering bound to the symbol's tick size (spec §8.1).

    ``tick_decimals`` is derived from Binance ``PRICE_FILTER.tickSize`` via
    :func:`decimals_from_tick_size`. When it is ``None`` (tick unknown, e.g.
    ``exchangeInfo`` unavailable) formatting falls back to the documented
    magnitude-bucketed :func:`price_decimals` rule. Both paths are fully
    deterministic; for the frozen BTCUSDT sample the tick-derived precision
    (2 dp) equals the magnitude rule for every rendered value, so the artifact
    is byte-identical.
    """

    tick_decimals: int | None = None

    def decimals(self, value: Decimal | float | int | str) -> int:
        """Decimals for ``value``: tick-derived when known, else magnitude."""

        if self.tick_decimals is not None:
            return self.tick_decimals
        return price_decimals(value)

    def fmt(self, value: Decimal | float | int | str) -> str:
        """Render ``value`` at this format's precision."""

        dec = Decimal(str(value))
        return f"{dec:.{self.decimals(dec)}f}"

    def fmt_distance(self, current: Decimal, reference: Decimal) -> str:
        """Render signed distance using this format's precision."""

        abs_val = Decimal(current) - Decimal(reference)
        pct = (abs_val / Decimal(reference)) * Decimal("100")
        sign = "+" if abs_val >= 0 else "-"
        dp = self.decimals(reference)
        return f"{sign}{abs(pct):.2f}% ({sign}{abs(abs_val):.{dp}f})"


def fmt_ratio(value: Decimal | float | int | str) -> str:
    """Render a plain multiplier (e.g. ``0.1``) for prose (byte-stable).

    Uses ``format(Decimal(...), "f")`` so ``Decimal("0.1")`` renders as ``0.1``
    rather than the magnitude-based ``fmt_price`` form ``0.10000000``.
    """

    return format(Decimal(str(value)), "f")


def fmt_volume(value: Decimal | float | int | str) -> str:
    """Render a raw-candle volume deterministically (byte-stable).

    Rule: plain ``str(Decimal)`` of the value exactly as carried by the
    :class:`~smc_prompt.models.Candle`. Binance klines deliver volume as a JSON
    string, so ``Decimal(str(value))`` reproduces that literal digit sequence
    without float rounding or magnitude-dependent formatting — the cheapest and
    most reproducible column format. No thousands separators, no trailing
    zeros are added or removed (the source already fixed them).
    """

    return format(Decimal(str(value)), "f")


def fmt_relative_volume(value: Decimal | float | int | str) -> str:
    """Render a relative-volume ratio (Phase 5, #16) as a plain ``xN.NN``.

    Always 2 dp with a lowercase ``x`` prefix, e.g. ``x1.25``. Uses
    ``format(Decimal, "f")`` so no scientific notation can ever appear and the
    output is byte-stable.
    """

    return "x" + format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def fmt_atr_pct(atr: Decimal, price: Decimal) -> str:
    """Render ``ATR`` as a percent of ``price`` (Phase 5, #6), 2 dp.

    Result shape is ``2.87% of price``. Callers must guard against a
    non-positive price before calling (division by zero).
    """

    pct = (Decimal(atr) / Decimal(price)) * Decimal("100")
    return f"{abs(pct):.2f}% of price"


def fmt_atr_distance(
    current: Decimal, reference: Decimal, atr: Decimal
) -> str:
    """Render an ATR-normalized distance (Phase 5, #6), 2 dp, e.g. ``x1.25``.

    ``|current - reference| / atr``. Callers must guard ``atr == 0`` before
    calling (division by zero); :func:`build_payload` emits a ``0.00``-safe
    ``x0.00`` in that case instead.
    """

    ratio = (
        abs(Decimal(current) - Decimal(reference)) / Decimal(atr)
    ).quantize(Decimal("0.01"))
    return "x" + format(ratio, "f")


def fmt_htf_date(moment: datetime) -> str:
    """HTF date format: ``%Y-%m-%d`` (UTC)."""

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def fmt_ltf_datetime(moment: datetime) -> str:
    """LTF datetime format: ``%Y-%m-%d %H:%M`` (UTC)."""

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_generated_at(moment: datetime) -> str:
    """``GENERATED_AT_UTC`` format: ``%Y-%m-%dT%H:%M:%SZ``."""

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_output_stamp(moment: datetime) -> str:
    """Output filename stamp: ``%Y%m%dT%H%M%SZ``."""

    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fmt_output_stamp_hyphen(moment: datetime) -> str:
    """UTC filename stamp: ``YYYY-MM-DD-HH-MM-SS-UTC`` (filesystem-safe).

    Colons (illegal on Windows) and the ISO ``Z`` suffix are replaced by
    hyphens and a literal ``UTC`` token, so the stem is safe on both Windows and
    POSIX. The instant is always normalized to UTC before formatting.
    """

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-UTC")


# --------------------------------------------------------------------------
# Configuration dataclass
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Fully validated, immutable run configuration."""

    symbol: str
    htf_candles: int = DEFAULT_HTF_CANDLES
    mtf_candles: int = DEFAULT_MTF_CANDLES
    ltf_candles: int = DEFAULT_LTF_CANDLES
    swing_lookback: int = DEFAULT_SWING_LOOKBACK

    distance_reference: str = DISTANCE_REFERENCE_NEAREST
    include_atr: bool = True

    htf_interval: str = HTF_INTERVAL
    mtf_interval: str = MTF_INTERVAL
    ltf_interval: str = LTF_INTERVAL
    swing_merge_atr_mult: Decimal = DEFAULT_SWING_MERGE_ATR_MULT
    atr_period: int = DEFAULT_ATR_PERIOD
    structure_min_swings: int = DEFAULT_STRUCTURE_MIN_SWINGS
    structure_last_swings: int = DEFAULT_STRUCTURE_LAST_SWINGS
    swings_table_rows: int = DEFAULT_SWINGS_TABLE_ROWS
    equal_levels_atr_mult: Decimal = DEFAULT_EQUAL_LEVELS_ATR_MULT
    fvg_atr_mult: Decimal = DEFAULT_FVG_ATR_MULT
    fvg_table_rows: int = DEFAULT_FVG_TABLE_ROWS
    context_buffer: int = DEFAULT_CONTEXT_BUFFER
    fetch_limit_max: int = FETCH_LIMIT_MAX
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    retry_max: int = DEFAULT_RETRY_MAX
    retry_backoff_base: float = DEFAULT_RETRY_BACKOFF_BASE
    retry_backoff_factor: float = DEFAULT_RETRY_BACKOFF_FACTOR
    output_dir: str = DEFAULT_OUTPUT_DIR
    delisted_zero_volume_streak: int = DEFAULT_DELISTED_ZERO_VOLUME_STREAK

    #: Relative-volume window / spike threshold (Phase 5, #16).
    volume_mean_period: int = DEFAULT_VOLUME_MEAN_PERIOD
    volume_spike_mult: Decimal = DEFAULT_VOLUME_SPIKE_MULT

    #: Post-render size warning threshold in bytes; ``None`` disables it
    #: (Phase 5, #11).
    prompt_bytes_warn: int | None = PROMPT_BYTES_WARN
    #: Optional hard post-render size limit in bytes; raising against it is a
    #: `ConfigError` (Phase 5, #11). ``None`` disables the hard limit.
    max_prompt_bytes: int | None = None

    base_urls: tuple[str, ...] = field(default=DEFAULT_BASE_URLS)
    #: Tick-size-derived price formatter. Starts as the magnitude fallback and
    #: is re-bound by ``cli`` (via ``dataclasses.replace``) once ``exchangeInfo``
    #: has been parsed.
    price_format: PriceFormat = field(default_factory=PriceFormat)

    #: Data provider slug (:data:`PROVIDER_BINANCE` default). The analysis
    #: pipeline is provider-agnostic; only the data source selected in ``cli``
    #: changes. Kept on the config so the banner/warnings can name the source.
    provider: str = PROVIDER_BINANCE
    #: False for providers whose candles carry no meaningful volume (OANDA's FX
    #: feeds report zero volume). It suppresses BOTH the fabricated relative
    #: volume and the delisted/zero-volume heuristic, which would otherwise fire
    #: on every such run (spec §9 warning matrix).
    volume_available: bool = True

    @property
    def provider_label(self) -> str:
        """Human-readable provider name (e.g. ``Twelve Data``)."""

        return provider_label(self.provider)

    @property
    def htf_fetch_limit(self) -> int:
        """Candles to request for HTF (table size + context buffer, capped)."""

        return min(self.htf_candles + self.context_buffer, self.fetch_limit_max)

    @property
    def mtf_fetch_limit(self) -> int:
        """Candles to request for MTF (table size + context buffer, capped)."""

        return min(self.mtf_candles + self.context_buffer, self.fetch_limit_max)

    @property
    def ltf_fetch_limit(self) -> int:
        """Candles to request for LTF (table size + context buffer, capped)."""

        return min(self.ltf_candles + self.context_buffer, self.fetch_limit_max)

    @property
    def htf_interval_label(self) -> str:
        """Prompt label for :attr:`htf_interval` (e.g. ``1d`` -> ``Daily``)."""

        return interval_label(self.htf_interval)

    @property
    def mtf_interval_label(self) -> str:
        """Prompt label for :attr:`mtf_interval` (e.g. ``4h`` -> ``4H``)."""

        return interval_label(self.mtf_interval)

    @property
    def ltf_interval_label(self) -> str:
        """Prompt label for :attr:`ltf_interval` (e.g. ``1h`` -> ``1H``)."""

        return interval_label(self.ltf_interval)


def build_config(
    symbol: str,
    *,
    htf_candles: int = DEFAULT_HTF_CANDLES,
    mtf_candles: int = DEFAULT_MTF_CANDLES,
    ltf_candles: int = DEFAULT_LTF_CANDLES,
    swing_lookback: int = DEFAULT_SWING_LOOKBACK,
    distance_reference: str = DISTANCE_REFERENCE_NEAREST,
    include_atr: bool = True,
    htf_interval: str = HTF_INTERVAL,
    mtf_interval: str = MTF_INTERVAL,
    ltf_interval: str = LTF_INTERVAL,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    volume_mean_period: int = DEFAULT_VOLUME_MEAN_PERIOD,
    volume_spike_mult: Decimal = DEFAULT_VOLUME_SPIKE_MULT,
    prompt_bytes_warn: int | None = PROMPT_BYTES_WARN,
    max_prompt_bytes: int | None = None,
    base_urls: Sequence[str] | None = None,
    price_format: PriceFormat | None = None,
    provider: str = PROVIDER_BINANCE,
    volume_available: bool | None = None,
) -> Config:
    """Validate raw CLI arguments and build an immutable :class:`Config`.

    Raises :class:`~smc_prompt.errors.ConfigError` on any invalid input
    (spec §9.3, exit code 2).
    """

    normalized_symbol = (symbol or "").strip().upper()
    if not normalized_symbol:
        raise ConfigError("SYMBOL is required. Example: smc-prompt BTCUSDT.")

    if not isinstance(swing_lookback, int) or swing_lookback < MIN_SWING_LOOKBACK:
        raise ConfigError("--swing-lookback must be an odd integer >= 3.")
    if swing_lookback % 2 == 0:
        raise ConfigError("--swing-lookback must be an odd integer >= 3.")

    if not isinstance(htf_candles, int) or htf_candles < MIN_CANDLES:
        raise ConfigError("--htf-candles must be an integer >= 10.")
    if not isinstance(mtf_candles, int) or mtf_candles < MIN_CANDLES:
        raise ConfigError("--mtf-candles must be an integer >= 10.")
    if not isinstance(ltf_candles, int) or ltf_candles < MIN_CANDLES:
        raise ConfigError("--ltf-candles must be an integer >= 10.")

    if distance_reference not in DISTANCE_REFERENCES:
        raise ConfigError(
            "--distance-reference must be one of: "
            + ", ".join(DISTANCE_REFERENCES)
            + "."
        )

    validate_interval(htf_interval, flag="--htf-interval")
    validate_interval(mtf_interval, flag="--mtf-interval")
    validate_interval(ltf_interval, flag="--ltf-interval")

    # The three tiers must map to three DISTINCT series. Enforcing this in
    # ``build_config`` (not only in ``LocalCsvSource``) makes the rule
    # mode-independent: identical tiers are meaningless on both the network
    # and offline paths and would otherwise emit duplicate blocks.
    if len({htf_interval, mtf_interval, ltf_interval}) != 3:
        raise ConfigError(
            "--htf-interval, --mtf-interval and --ltf-interval must be three "
            "DISTINCT intervals so each tier maps to its own series "
            f"(got htf={htf_interval}, mtf={mtf_interval}, ltf={ltf_interval})."
        )

    if not isinstance(volume_mean_period, int) or volume_mean_period < 1:
        raise ConfigError("volume_mean_period must be an integer >= 1.")
    if Decimal(str(volume_spike_mult)) <= 0:
        raise ConfigError("volume_spike_mult must be positive.")

    if prompt_bytes_warn is not None:
        if not isinstance(prompt_bytes_warn, int) or prompt_bytes_warn < 1:
            raise ConfigError("prompt_bytes_warn must be a positive integer.")
    if max_prompt_bytes is not None:
        if not isinstance(max_prompt_bytes, int) or max_prompt_bytes < 1:
            raise ConfigError("--max-prompt-bytes must be an integer >= 1.")

    if provider not in PROVIDERS:
        raise ConfigError(
            "--provider must be one of: " + ", ".join(PROVIDERS) + "."
        )

    hosts = tuple(base_urls) if base_urls else DEFAULT_BASE_URLS
    hosts = tuple(host.rstrip("/") for host in hosts if host and host.strip())
    if not hosts:
        raise ConfigError("At least one Binance base URL must be provided.")

    # Twelve Data and OANDA report no usable volume on their FX/metal feeds, so
    # the derived volume facts (and the zero-volume delisted heuristic) are
    # disabled unless the caller explicitly overrides it.
    resolved_volume_available = (
        provider == PROVIDER_BINANCE
        if volume_available is None
        else bool(volume_available)
    )

    return Config(
        symbol=normalized_symbol,
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
        volume_mean_period=volume_mean_period,
        volume_spike_mult=Decimal(str(volume_spike_mult)),
        prompt_bytes_warn=prompt_bytes_warn,
        max_prompt_bytes=max_prompt_bytes,
        base_urls=hosts,
        price_format=price_format or PriceFormat(),
        provider=provider,
        volume_available=resolved_volume_available,
    )
