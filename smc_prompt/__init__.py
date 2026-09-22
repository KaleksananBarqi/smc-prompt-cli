"""smc-prompt — mechanical SMC/ICT [FAKTA] payload generator.

This package performs NO reasoning of its own: it fetches read-only public
market data from one configured provider (Binance Futures by default; Twelve Data
or OANDA for FX/metals such as XAUUSD, which Binance Futures does not list),
computes only objective structural facts, and injects them into a fixed prompt
template. See ``docs/DESIGN_SPEC.md`` for the frozen design.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
