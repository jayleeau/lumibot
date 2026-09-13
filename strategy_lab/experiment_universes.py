"""Frozen universe definitions for the HTS research catalog.

The catalog never stores a loose symbol list inline.  A candidate names a
universe keyword and this module resolves it, so a universe change is a
reviewable edit in one place rather than an accidental parameter value.

``U0`` is owned by :mod:`strategy_lab.hts_backtest`, which already defines the
57-symbol research universe.  It is imported lazily so the catalog stays light
to import and so the two definitions cannot silently drift apart.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Iterable

U0 = "U0"
U0_UNLEVERAGED = "U0_UNLEVERAGED"
U0_EX_CRYPTO = "U0_EX_CRYPTO"
SECTOR_ONLY = "SECTOR_ONLY"
DIVERSIFIED_CORE = "DIVERSIFIED_CORE"

UNIVERSE_KEYWORDS: tuple[str, ...] = (
    U0, U0_UNLEVERAGED, U0_EX_CRYPTO, SECTOR_ONLY, DIVERSIFIED_CORE,
)

# Plan section 5, H091/H095: verify issuer metadata before using these.
LEVERAGED_PRODUCTS: tuple[str, ...] = (
    "TQQQ", "UPRO", "SSO", "UDOW", "TNA", "SOXL", "TECL", "FAS", "ERX",
    "LABU", "NUGT", "BITX", "BOIL",
)
CRYPTO_LINKED: tuple[str, ...] = ("MSTR", "COIN", "BITX", "IBIT")

# Plan section 5, H092.
DIVERSIFIED_CORE_SYMBOLS: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "IEF", "TLT", "DBC", "VNQ", "BIL",
)

# Plan section 5, H093.
SECTOR_ONLY_SYMBOLS: tuple[str, ...] = (
    "XLK", "XLE", "XLF", "XLY", "XLV", "XLI", "XLP", "XLU", "XLRE", "XLB", "XLC",
)

# Plan section 5, H077-H079 breadth basket.  Leveraged clones are excluded.
BREADTH_BASKET: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "XLK", "XLE", "XLF", "XLY", "XLV",
    "XLI", "XLP", "XLU", "XLRE", "XLB", "XLC",
)

# Plan section 5, H060.  Deliberately broad economic-exposure groups.
ECONOMIC_EXPOSURE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nasdaq-100", ("QQQ", "TQQQ")),
    ("sp-500", ("SPY", "SSO", "UPRO")),
    ("semiconductors", ("SMH", "SOXX", "SOXL")),
    ("technology", ("XLK", "TECL")),
    ("russell-2000", ("IWM", "TNA")),
    ("financials", ("XLF", "FAS")),
    ("energy", ("XLE", "ERX")),
    ("biotech", ("XBI", "LABU")),
    ("gold-miners", ("GDX", "NUGT")),
    ("natural-gas", ("UNG", "BOIL")),
    ("crypto-linked", ("MSTR", "COIN", "IBIT", "BITX")),
)


@lru_cache(maxsize=1)
def _default_universe() -> tuple[str, ...]:
    from strategy_lab.hts_backtest import DEFAULT_UNIVERSE  # lazy: avoids duckdb at import

    return tuple(DEFAULT_UNIVERSE)


def _without(symbols: Iterable[str], removed: Iterable[str]) -> tuple[str, ...]:
    drop = set(removed)
    return tuple(symbol for symbol in symbols if symbol not in drop)


@lru_cache(maxsize=None)
def resolve_universe(keyword: str) -> tuple[str, ...]:
    """Resolve a universe keyword to its ordered symbol tuple."""
    if keyword == U0:
        return _default_universe()
    if keyword == U0_UNLEVERAGED:
        return _without(_default_universe(), (*LEVERAGED_PRODUCTS, "MSTR", "COIN"))
    if keyword == U0_EX_CRYPTO:
        return _without(_default_universe(), CRYPTO_LINKED)
    if keyword == SECTOR_ONLY:
        return SECTOR_ONLY_SYMBOLS
    if keyword == DIVERSIFIED_CORE:
        return DIVERSIFIED_CORE_SYMBOLS
    raise KeyError(f"unknown universe keyword: {keyword!r}")


def exposure_group(symbol: str) -> str:
    """Return the economic-exposure group of ``symbol``, else the symbol itself."""
    for name, members in ECONOMIC_EXPOSURE_GROUPS:
        if symbol in members:
            return name
    return symbol
