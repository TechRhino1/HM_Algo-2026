"""
JARVIS AI 4.0 — Centralized Symbol Metadata Registry.
Eliminates all hardcoded "XAU", "GOLD", "JPY", "BTC" string checks scattered across 6+ files.
Provides contract_size, pip_size, pip_value, spread multiplier, asset class, and margin info per symbol.
"""
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

logger = logging.getLogger("JARVIS_SymbolRegistry")

@dataclass(frozen=True)
class SymbolSpec:
    """Immutable specification for a tradeable instrument."""
    canonical: str              # Canonical name (e.g. "XAUUSD")
    asset_class: str            # "COMMODITY", "FOREX", "CRYPTO", "INDEX"
    contract_size: float        # Lots → units multiplier
    pip_size: float             # Minimum price increment for 1 pip
    pip_value_per_lot: float    # Dollar value of 1 pip move per 1.0 standard lot
    typical_spread_pips: float  # Expected average spread in pips
    max_spread_pips: float      # Reject trade if spread exceeds this
    typical_atr_pct: float      # Typical daily ATR as % of price
    margin_pct: float           # Margin requirement as % of notional (1:1000 = 0.1%)
    digits: int = 5             # Price precision digits
    is_crypto: bool = False     # 24/7 market — exempt from session blackouts
    is_jpy_quote: bool = False  # JPY-quoted pair (affects pip calculation)

# ─── Master Registry ─────────────────────────────────────────────────────────
_REGISTRY: Dict[str, SymbolSpec] = {
    "XAUUSD": SymbolSpec(
        canonical="XAUUSD", asset_class="COMMODITY",
        contract_size=100.0, pip_size=0.1, pip_value_per_lot=10.0,
        typical_spread_pips=2.0, max_spread_pips=5.0,
        typical_atr_pct=0.8, margin_pct=0.1, digits=2
    ),
    "EURUSD": SymbolSpec(
        canonical="EURUSD", asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0,
        typical_spread_pips=0.7, max_spread_pips=2.0,
        typical_atr_pct=0.4, margin_pct=0.1, digits=5
    ),
    "GBPUSD": SymbolSpec(
        canonical="GBPUSD", asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0,
        typical_spread_pips=0.9, max_spread_pips=2.5,
        typical_atr_pct=0.5, margin_pct=0.1, digits=5
    ),
    "USDJPY": SymbolSpec(
        canonical="USDJPY", asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.01, pip_value_per_lot=6.80,
        typical_spread_pips=0.8, max_spread_pips=2.5,
        typical_atr_pct=0.4, margin_pct=0.1, digits=3, is_jpy_quote=True
    ),
    "AUDUSD": SymbolSpec(
        canonical="AUDUSD", asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0,
        typical_spread_pips=0.9, max_spread_pips=2.5,
        typical_atr_pct=0.5, margin_pct=0.1, digits=5
    ),
    "USDCAD": SymbolSpec(
        canonical="USDCAD", asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=7.50,
        typical_spread_pips=2.7, max_spread_pips=8.5,
        typical_atr_pct=0.4, margin_pct=0.1, digits=5
    ),
    "BTCUSD": SymbolSpec(
        canonical="BTCUSD", asset_class="CRYPTO",
        contract_size=1.0, pip_size=0.01, pip_value_per_lot=0.01,
        typical_spread_pips=1500.0, max_spread_pips=3000.0,
        typical_atr_pct=2.5, margin_pct=0.5, digits=2, is_crypto=True
    ),
    "US30": SymbolSpec(
        canonical="US30", asset_class="INDEX",
        contract_size=1.0, pip_size=1.0, pip_value_per_lot=1.0,
        typical_spread_pips=3.9, max_spread_pips=12.0,
        typical_atr_pct=0.9, margin_pct=0.2, digits=2
    ),
    "NAS100": SymbolSpec(
        canonical="NAS100", asset_class="INDEX",
        contract_size=1.0, pip_size=1.0, pip_value_per_lot=1.0,
        typical_spread_pips=2.0, max_spread_pips=6.0,
        typical_atr_pct=1.2, margin_pct=0.2, digits=2
    ),
    "GER40": SymbolSpec(
        canonical="GER40", asset_class="INDEX",
        contract_size=1.0, pip_size=1.0, pip_value_per_lot=1.0,
        typical_spread_pips=2.0, max_spread_pips=6.0,
        typical_atr_pct=0.9, margin_pct=0.2, digits=2
    ),
    "UK100": SymbolSpec(
        canonical="UK100", asset_class="INDEX",
        contract_size=1.0, pip_size=1.0, pip_value_per_lot=1.0,
        typical_spread_pips=1.6, max_spread_pips=8.5,
        typical_atr_pct=0.7, margin_pct=0.2, digits=2
    ),
    "XAGUSD": SymbolSpec(
        canonical="XAGUSD", asset_class="COMMODITY",
        contract_size=5000.0, pip_size=0.01, pip_value_per_lot=50.0,
        typical_spread_pips=4.0, max_spread_pips=12.0,
        typical_atr_pct=2.0, margin_pct=0.2, digits=3
    ),
    "WTI": SymbolSpec(
        canonical="WTI", asset_class="COMMODITY",
        contract_size=100.0, pip_size=0.01, pip_value_per_lot=10.0,
        typical_spread_pips=13.0, max_spread_pips=25.0,
        typical_atr_pct=1.5, margin_pct=0.2, digits=2
    ),
    "ETHUSD": SymbolSpec(
        canonical="ETHUSD", asset_class="CRYPTO",
        contract_size=1.0, pip_size=0.01, pip_value_per_lot=0.01,
        typical_spread_pips=345.0, max_spread_pips=450.0,
        typical_atr_pct=3.0, margin_pct=0.5, digits=2, is_crypto=True
    ),
    "SOLUSD": SymbolSpec(
        canonical="SOLUSD", asset_class="CRYPTO",
        contract_size=10.0, pip_size=0.01, pip_value_per_lot=0.1,
        typical_spread_pips=35.0, max_spread_pips=105.0,
        typical_atr_pct=4.0, margin_pct=0.5, digits=2, is_crypto=True
    ),
    "ETHBTC": SymbolSpec(
        canonical="ETHBTC", asset_class="CRYPTO",
        contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0,
        typical_spread_pips=2.0, max_spread_pips=8.0,
        typical_atr_pct=3.0, margin_pct=0.5, digits=5, is_crypto=True
    ),
    "US500": SymbolSpec(
        canonical="US500", asset_class="INDEX",
        contract_size=1.0, pip_size=1.0, pip_value_per_lot=1.0,
        typical_spread_pips=0.6, max_spread_pips=6.0,
        typical_atr_pct=0.8, margin_pct=0.2, digits=2
    ),
    "USDCHF": SymbolSpec(
        canonical="USDCHF", asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0,
        typical_spread_pips=2.4, max_spread_pips=4.0,
        typical_atr_pct=0.4, margin_pct=0.1, digits=5
    ),
    "NZDUSD": SymbolSpec(
        canonical="NZDUSD", asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0,
        typical_spread_pips=2.8, max_spread_pips=8.5,
        typical_atr_pct=0.5, margin_pct=0.1, digits=5
    ),
    "EURJPY": SymbolSpec(
        canonical="EURJPY", asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.01, pip_value_per_lot=6.80,
        typical_spread_pips=1.0, max_spread_pips=2.5,
        typical_atr_pct=0.5, margin_pct=0.1, digits=3, is_jpy_quote=True
    ),
    "GBPJPY": SymbolSpec(
        canonical="GBPJPY", asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.01, pip_value_per_lot=6.80,
        typical_spread_pips=1.2, max_spread_pips=3.0,
        typical_atr_pct=0.6, margin_pct=0.1, digits=3, is_jpy_quote=True
    ),
}

# ─── Broker Alias Resolution ─────────────────────────────────────────────────
_ALIAS_MAP: Dict[str, str] = {
    "GOLD.I#": "XAUUSD", "GOLD": "XAUUSD", "GOLD#": "XAUUSD", "GOLD.I": "XAUUSD", "XAUUSD#": "XAUUSD", "XAUUSD.I#": "XAUUSD", "XAUUSD.I": "XAUUSD",
    "EURUSD#": "EURUSD", "EURUSD.I#": "EURUSD", "EURUSD.I": "EURUSD",
    "GBPUSD#": "GBPUSD", "GBPUSD.I#": "GBPUSD", "GBPUSD.I": "GBPUSD",
    "USDJPY#": "USDJPY", "USDJPY.I#": "USDJPY", "USDJPY.I": "USDJPY",
    "AUDUSD#": "AUDUSD", "AUDUSD.I#": "AUDUSD", "AUDUSD.I": "AUDUSD",
    "USDCAD#": "USDCAD", "USDCAD.I#": "USDCAD", "USDCAD.I": "USDCAD",
    "BTCUSD#": "BTCUSD", "BTCUSD.I#": "BTCUSD", "BTCUSD.I": "BTCUSD", "BITCOIN": "BTCUSD",
    "ETHUSD#": "ETHUSD", "ETHUSD.I#": "ETHUSD", "ETHEREUM": "ETHUSD", "ETH": "ETHUSD",
    "SOLUSD#": "SOLUSD", "SOLUSD.I#": "SOLUSD", "SOLANA": "SOLUSD", "SOL": "SOLUSD",
    "ETHBTC#": "ETHBTC", "ETHBTC.I#": "ETHBTC", "ETHBTC": "ETHBTC",
    "US500#": "US500", "SPX500": "US500", "SP500": "US500", "US500.I#": "US500", "US500Cash#": "US500",
    "US30#": "US30", "DJ30": "US30", "WALLSTREET": "US30", "US30.I#": "US30", "US30Cash#": "US30",
    "NAS100#": "NAS100", "USTECH": "NAS100", "NDX100": "NAS100", "US100": "NAS100",
    "US100Cash#": "NAS100", "NAS100.I#": "NAS100", "NAS100Cash#": "NAS100",
    "GER40": "GER40", "GER40#": "GER40", "GER40Cash#": "GER40", "GER40.I#": "GER40",
    "DE40": "GER40", "DAX40": "GER40", "GER30": "GER40", "DE30": "GER40", "DAX": "GER40",
    "UK100#": "UK100", "UK100Cash#": "UK100", "UK100.I#": "UK100",
    "FTSE100": "UK100", "FTSE": "UK100", "UK100.I": "UK100",
    "XAGUSD#": "XAGUSD", "XAGUSD.I#": "XAGUSD", "SILVER": "XAGUSD", "SILVER.i#": "XAGUSD",
    "SILVER.I#": "XAGUSD", "XAG": "XAGUSD",
    # Every other cash CFD carries its broker "Cash#" alias (US500Cash#,
    # US30Cash#, US100Cash#, NAS100Cash#, GER40Cash#, UK100Cash#). Oil was the
    # one omission, and because "OILCASH#" contains none of the canonical names
    # the fuzzy pass could not rescue it either: the broker symbol for WTI fell
    # all the way through to the generic FX spec, giving oil a contract size of
    # 100,000 instead of 100 and a pip of 0.0001 instead of 0.01. Position
    # sizing was therefore out by a factor of 1000 and spread gating by 100.
    "USOIL": "WTI", "OIL": "WTI", "CRUDE": "WTI", "USOIL.I#": "WTI", "OIL.I#": "WTI", "CL": "WTI",
    "OILCash#": "WTI", "USOILCash#": "WTI",
    "USDCHF#": "USDCHF", "USDCHF.I#": "USDCHF",
    "NZDUSD#": "NZDUSD", "NZDUSD.I#": "NZDUSD",
    "EURJPY#": "EURJPY", "EURJPY.I#": "EURJPY",
    "GBPJPY#": "GBPJPY", "GBPJPY.I#": "GBPJPY",
}

# Broker aliases are written in the broker's own case ("GER40Cash#", "SILVER.i#",
# "US100Cash#") but ``resolve()`` looks up the upper-cased symbol. Without this
# normalisation every mixed-case alias silently misses the map and falls through
# to the fuzzy/generic fallback — so a correctly written alias still produced the
# wrong spec. Normalise once at import instead of at every lookup.
_ALIAS_MAP = {k.upper(): v for k, v in _ALIAS_MAP.items()}


def resolve(symbol: str) -> SymbolSpec:
    """Resolves any broker alias to its canonical SymbolSpec.

    Falls back to a generic FX spec for anything unknown. That fallback is
    *dangerous* and is logged loudly: GER40, UK100 and XAGUSD were once absent
    from this registry, so they silently resolved to ``contract_size=100_000``,
    ``pip_size=0.0001``, ``max_spread_pips=5.0``. The FX-sized spread cap then
    rejected **100 % of bars** for those instruments and the FX contract size
    corrupted position sizing — eight of sixteen symbols produced zero trades
    for a whole quarter without a single error being raised.
    """
    key = symbol.upper().strip()
    if key in _REGISTRY:
        return _REGISTRY[key]
    canonical = _ALIAS_MAP.get(key)
    if canonical and canonical in _REGISTRY:
        return _REGISTRY[canonical]
    # Fuzzy fallback: check if any known canonical is a substring
    for canon, spec in _REGISTRY.items():
        if canon in key or key in canon:
            logger.warning(
                "symbol_registry: %r resolved by fuzzy match to %r - add an explicit "
                "entry if this instrument is traded", symbol, canon,
            )
            return spec
    # Ultimate fallback — generic forex. Almost certainly wrong for a non-FX
    # instrument, so never let it pass silently.
    logger.error(
        "symbol_registry: %r is NOT registered; falling back to a generic FX spec "
        "(contract_size=100000, pip_size=0.0001). Spread gating and position sizing "
        "will be wrong for anything that is not an FX major. Register it.",
        symbol,
    )
    return SymbolSpec(
        canonical=key, asset_class="FOREX",
        contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0,
        typical_spread_pips=2.0, max_spread_pips=5.0,
        typical_atr_pct=0.5, margin_pct=0.1
    )


def all_symbols() -> List[SymbolSpec]:
    """Every registered spec, ordered by canonical name.

    The authoritative "all available symbols" list for backtests and universe
    sweeps — unlike ``DEFAULT_UNIVERSE`` in ``mt5_history``, which is a curated
    subset and therefore under-reports coverage.
    """
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def is_registered(symbol: str) -> bool:
    """True only for an exact registry or alias hit — never a fuzzy/fallback match.

    ``resolve()`` always returns *something*; this tells you whether that
    something is real. Use it before trusting a spec for anything consequential.
    """
    key = symbol.upper().strip()
    if key in _REGISTRY:
        return True
    canonical = _ALIAS_MAP.get(key)
    return bool(canonical and canonical in _REGISTRY)


def asset_class_of(symbol: str) -> Optional[str]:
    """Registry asset class ("FOREX"/"COMMODITY"/"INDEX"/"CRYPTO"), or None.

    Returns None rather than guessing when the symbol is not registered, so
    callers can distinguish "I know this is crypto" from "I have no idea".
    """
    if not is_registered(symbol):
        return None
    return resolve(symbol).asset_class


def registry_mismatches(broker_meta: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    """Compare the registry spec against real broker metadata from a data manifest.

    Returns a dict of ``{field: (registry_value, broker_value)}`` for every field
    that disagrees. Empty means the registry matches the broker.

    This exists because the registry and the fetch manifests are two sources of
    truth for the same facts, and when they drift nothing complains — the wrong
    spec is simply used, and the symptom (no trades) is attributed to the
    strategy. Run it after every fetch; ``tests/test_symbol_registry.py`` pins it.
    """
    spec = resolve(symbol)
    out: Dict[str, Any] = {}
    for field_name, reg_val, key_names in (
        ("digits", spec.digits, ("digits",)),
        ("contract_size", spec.contract_size, ("contract_size",)),
        ("asset_class", spec.asset_class, ("category",)),
    ):
        broker_val = next(
            (broker_meta.get(k) for k in key_names if broker_meta.get(k) is not None), None
        )
        if broker_val is None:
            continue
        if field_name in ("contract_size",):
            if abs(float(reg_val) - float(broker_val)) > 1e-9:
                out[field_name] = (reg_val, broker_val)
        elif field_name == "asset_class":
            # The manifest uses MT5 categories (INDEX/METAL/FX_MAJOR/CRYPTO);
            # the registry uses its own vocabulary. Compare loosely.
            #
            # ENERGY maps to COMMODITY because the registry's taxonomy is
            # deliberately coarser: it has no ENERGY class, and ``_REGISTRY_CATEGORY``
            # would not know what to do with one. WTI/UKOIL are energy instruments
            # and are bucketed as commodities here — ``mt5_history._CATEGORY``
            # keeps the finer ENERGY label for session/risk purposes.
            mapping = {
                "INDEX": "INDEX", "METAL": "COMMODITY", "COMMODITY": "COMMODITY",
                "ENERGY": "COMMODITY",
                "FX_MAJOR": "FOREX", "FX_CROSS": "FOREX", "CRYPTO": "CRYPTO",
            }
            expected = mapping.get(str(broker_val).upper(), str(broker_val).upper())
            if expected != str(reg_val).upper():
                out[field_name] = (reg_val, expected)
        else:
            if int(reg_val) != int(broker_val):
                out[field_name] = (reg_val, broker_val)
    return out


def is_crypto(symbol: str) -> bool:
    return resolve(symbol).is_crypto

def is_jpy_quote(symbol: str) -> bool:
    return resolve(symbol).is_jpy_quote

def get_contract_size(symbol: str) -> float:
    return resolve(symbol).contract_size

def get_pip_size(symbol: str) -> float:
    return resolve(symbol).pip_size

def get_max_spread(symbol: str) -> float:
    return resolve(symbol).max_spread_pips

def get_dollar_risk_per_price_unit(symbol: str, symbol_info: Optional[Dict[str, Any]] = None) -> float:
    """
    Returns the dollar risk per price unit move for 1.0 standard lot.
    Uses MT5 symbol info if available, otherwise falls back to SymbolSpec.
    """
    if symbol_info:
        tick_val = symbol_info.get("trade_tick_value") or symbol_info.get("tick_value")
        tick_sz = symbol_info.get("trade_tick_size") or symbol_info.get("tick_size") or symbol_info.get("point")
        if tick_val is not None and tick_sz is not None and float(tick_sz) > 0:
            return float(tick_val) / float(tick_sz)

    spec = resolve(symbol)
    if spec.pip_size > 0:
        return spec.pip_value_per_lot / spec.pip_size
    return spec.contract_size

