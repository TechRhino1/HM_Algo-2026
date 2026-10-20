"""
HM Algo 2.0 — Institutional Symbol-Specific Configuration Matrix.
Defines separate, specialized trading logic, Bayesian strategy priors,
dynamic SL/TP envelopes, fast-cash profit banking, trailing ratchets,
session hours, and XM Ultra Low Standard account specifications.

CRITICAL INVARIANCE: Everything related to XAUUSD / Gold is strictly preserved
and 100% untouched.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SymbolProfileConfig:
    symbol: str
    canonical: str
    asset_class: str  # "CRYPTO", "INDEX", "COMMODITY", "FOREX"
    
    # Strategy Priorities & Bayesian Likelihood Weights
    strategy_weights: Dict[str, float]
    banned_strategies: List[str] = field(default_factory=list)
    
    # Dynamic Structural Levels & Stop-Loss Breathing Room
    sl_atr_multiplier: float = 1.80
    min_target_rr: float = 2.0
    asym_rr: float = 3.5
    anti_wick_buffer_atr: float = 0.35
    
    # Profit Protection & Execution Ratchet
    fast_cash_r: float = 1.50
    fast_cash_volume_pct: float = 0.33  # Bank 33% at 1.5R, leave 67% runner
    be_trigger_r: float = 1.80
    be_buffer_pct: float = 0.08
    runner_trail_atr: float = 2.0
    
    # Session & Timing Filters
    session_restriction: bool = False
    allowed_utc_hours: Optional[Tuple[int, int]] = None  # (start_hour, end_hour) inclusive
    
    # XM Ultra Low Standard Account Specifications
    contract_size: float = 1.0
    pip_size: float = 0.01
    pip_value_per_lot: float = 0.01
    digits: int = 2
    typical_spread_pips: float = 1.0
    max_allowed_spread_pips: float = 3.0
    commission_per_lot: float = 0.0  # XM Ultra Low Standard has $0.00 commission
    min_volume: float = 0.01
    volume_step: float = 0.01
    margin_pct: float = 0.5


# ─── SYMBOL-SPECIFIC INSTITUTIONAL CONFIGURATION MATRIX ──────────────────────────

SYMBOL_PROFILES: Dict[str, SymbolProfileConfig] = {
    # =========================================================================
    # 1. CRYPTO ASSETS (XM Ultra Low Standard Specs: $0 Commission, 24/7)
    # =========================================================================
    "BTCUSD": SymbolProfileConfig(
        symbol="BTCUSD",
        canonical="BTCUSD",
        asset_class="CRYPTO",
        strategy_weights={
            "TREND_PULLBACK": 3.5,             # Deep pullbacks into discount/premium OTE
            "CHOCH_STRUCTURAL_REVERSAL": 3.0,  # Confirmed structural shifts only
            "TREND_FOLLOWING": 2.5,            # Institutional trend continuation
            "RANGE_MEAN_REVERSION": 1.5,       # Demote range fades on macro trending crypto
            "LIQUIDITY_SWEEP_REVERSAL": 0.5,   # Avoid counter-trend sweep fades in crypto
            "BREAKOUT_EXPANSION": 0.0,         # Ban false breakout traps in crypto
        },
        banned_strategies=["BREAKOUT_EXPANSION"],
        sl_atr_multiplier=1.50,                # Calibrated for local structural execution
        min_target_rr=2.0,
        asym_rr=3.5,
        anti_wick_buffer_atr=0.40,             # Calibrated to absorb crypto wicks
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=1.80,                 # Calibrated runner trail
        session_restriction=False,
        contract_size=1.0,
        pip_size=0.01,
        pip_value_per_lot=0.01,
        digits=2,
        typical_spread_pips=1500.0,
        max_allowed_spread_pips=3000.0,
        commission_per_lot=0.0,
        min_volume=0.01,
        volume_step=0.01,
        margin_pct=0.5
    ),

    "ETHUSD": SymbolProfileConfig(
        symbol="ETHUSD",
        canonical="ETHUSD",
        asset_class="CRYPTO",
        strategy_weights={
            "TREND_FOLLOWING": 2.8,
            "TREND_PULLBACK": 2.5,
            "RANGE_MEAN_REVERSION": 2.5,
            "CHOCH_STRUCTURAL_REVERSAL": 2.0,
            "LIQUIDITY_SWEEP_REVERSAL": 1.8,
            "BREAKOUT_EXPANSION": 1.0,
        },
        banned_strategies=[],
        sl_atr_multiplier=1.60,                # Calibrated local swing stop (was 3.20 macro stop)
        min_target_rr=2.0,
        asym_rr=3.5,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=1.80,
        session_restriction=False,
        contract_size=1.0,
        pip_size=0.01,
        pip_value_per_lot=0.01,
        digits=2,
        typical_spread_pips=120.0,
        max_allowed_spread_pips=300.0,
        commission_per_lot=0.0,
        min_volume=0.02,
        volume_step=0.01,
        margin_pct=0.5
    ),

    "SOLUSD": SymbolProfileConfig(
        symbol="SOLUSD",
        canonical="SOLUSD",
        asset_class="CRYPTO",
        strategy_weights={
            "CHOCH_STRUCTURAL_REVERSAL": 4.0,
            "TREND_PULLBACK": 3.0,
            "TREND_FOLLOWING": 2.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
            "RANGE_MEAN_REVERSION": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL"],
        sl_atr_multiplier=3.20,
        min_target_rr=2.0,
        asym_rr=4.0,
        anti_wick_buffer_atr=0.45,
        fast_cash_r=1.00,
        fast_cash_volume_pct=0.40,
        be_trigger_r=1.80,
        runner_trail_atr=2.50,
        session_restriction=False,
        contract_size=10.0,
        pip_size=0.01,
        pip_value_per_lot=0.10,
        digits=2,
        typical_spread_pips=45.0,
        max_allowed_spread_pips=105.0,
        commission_per_lot=0.0,
        min_volume=0.05,
        volume_step=0.01,
        margin_pct=0.5
    ),

    "ETHBTC": SymbolProfileConfig(
        symbol="ETHBTC",
        canonical="ETHBTC",
        asset_class="CRYPTO",
        strategy_weights={
            "RANGE_MEAN_REVERSION": 3.5,       # Crypto cross-pair mean reversion
            "CHOCH_STRUCTURAL_REVERSAL": 2.5,
            "TREND_FOLLOWING": 0.5,            # Demote trend-following due to excessive spread friction
            "BREAKOUT_EXPANSION": 0.5,
            "TREND_PULLBACK": 0.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
        },
        banned_strategies=["LIQUIDITY_SWEEP_REVERSAL", "BREAKOUT_EXPANSION"],
        sl_atr_multiplier=2.50,
        min_target_rr=2.0,
        asym_rr=3.5,
        anti_wick_buffer_atr=0.50,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=2.20,
        session_restriction=True,              # Restrict to prime hours to avoid spread blowouts
        allowed_utc_hours=(8, 20),
        contract_size=100000.0,
        pip_size=0.00001,
        pip_value_per_lot=1.0,
        digits=5,
        typical_spread_pips=11.0,
        max_allowed_spread_pips=25.0,
        commission_per_lot=0.0,
        min_volume=0.01,
        volume_step=0.01,
        margin_pct=0.5
    ),

    # =========================================================================
    # 2. EQUITY INDICES (XM Ultra Low Standard Specs: $0 Commission, 14:00-19:00 UTC)
    # =========================================================================
    "US500": SymbolProfileConfig(
        symbol="US500",
        canonical="US500",
        asset_class="INDEX",
        strategy_weights={
            "LIQUIDITY_SWEEP_REVERSAL": 3.5,
            "CHOCH_STRUCTURAL_REVERSAL": 3.0,
            "RANGE_MEAN_REVERSION": 2.5,
            "TREND_PULLBACK": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
            "TREND_FOLLOWING": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_FOLLOWING", "TREND_PULLBACK"],
        sl_atr_multiplier=2.80,
        min_target_rr=2.0,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=2.60,
        session_restriction=True,
        allowed_utc_hours=(13, 21),
        contract_size=1.0,
        pip_size=1.0,
        pip_value_per_lot=1.0,
        digits=2,
        typical_spread_pips=0.6,
        max_allowed_spread_pips=2.5,
        commission_per_lot=0.0,
        min_volume=0.1,
        volume_step=0.1,
        margin_pct=0.2
    ),

    # Registered 2026-09-15: GER40 and UK100 were absent from this table and so
    # fell through to the generic FOREX template below, which gave them a
    # contract size of 100,000 instead of 1.0 - a 100,000x sizing error.
    "GER40": SymbolProfileConfig(
        symbol="GER40",
        canonical="GER40",
        asset_class="INDEX",
        strategy_weights={
            "LIQUIDITY_SWEEP_REVERSAL": 3.5,
            "CHOCH_STRUCTURAL_REVERSAL": 3.0,
            "RANGE_MEAN_REVERSION": 2.5,
            "TREND_PULLBACK": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
            "TREND_FOLLOWING": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_FOLLOWING", "TREND_PULLBACK"],
        sl_atr_multiplier=2.80,
        min_target_rr=2.0,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=2.60,
        session_restriction=True,
        allowed_utc_hours=(8, 17),
        contract_size=1.0,
        pip_size=1.0,
        pip_value_per_lot=1.0,
        digits=2,
        typical_spread_pips=2.0,
        max_allowed_spread_pips=6.0,
        commission_per_lot=0.0,
        min_volume=0.1,
        volume_step=0.1,
        margin_pct=0.2
    ),

    "UK100": SymbolProfileConfig(
        symbol="UK100",
        canonical="UK100",
        asset_class="INDEX",
        strategy_weights={
            "LIQUIDITY_SWEEP_REVERSAL": 3.5,
            "CHOCH_STRUCTURAL_REVERSAL": 3.0,
            "RANGE_MEAN_REVERSION": 2.5,
            "TREND_PULLBACK": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
            "TREND_FOLLOWING": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_FOLLOWING", "TREND_PULLBACK"],
        sl_atr_multiplier=2.80,
        min_target_rr=2.0,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=2.60,
        session_restriction=True,
        allowed_utc_hours=(8, 17),
        contract_size=1.0,
        pip_size=1.0,
        pip_value_per_lot=1.0,
        digits=2,
        typical_spread_pips=1.6,
        max_allowed_spread_pips=8.5,
        commission_per_lot=0.0,
        min_volume=0.1,
        volume_step=0.1,
        margin_pct=0.2
    ),

    "NAS100": SymbolProfileConfig(
        symbol="NAS100",
        canonical="NAS100",
        asset_class="INDEX",
        strategy_weights={
            "TREND_PULLBACK": 3.8,
            "TREND_FOLLOWING": 2.5,
            "CHOCH_STRUCTURAL_REVERSAL": 2.8,
            "BREAKOUT_EXPANSION": 0.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
            "RANGE_MEAN_REVERSION": 0.0,
        },
        banned_strategies=["LIQUIDITY_SWEEP_REVERSAL", "RANGE_MEAN_REVERSION", "BREAKOUT_EXPANSION"],
        sl_atr_multiplier=2.80,
        min_target_rr=2.0,
        asym_rr=4.0,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=2.60,
        session_restriction=True,
        allowed_utc_hours=(13, 21),
        contract_size=1.0,
        pip_size=1.0,
        pip_value_per_lot=1.0,
        digits=2,
        typical_spread_pips=2.0,
        max_allowed_spread_pips=7.0,
        commission_per_lot=0.0,
        min_volume=0.1,
        volume_step=0.1,
        margin_pct=0.2
    ),

    "US30": SymbolProfileConfig(
        symbol="US30",
        canonical="US30",
        asset_class="INDEX",
        strategy_weights={
            "TREND_PULLBACK": 3.8,
            "CHOCH_STRUCTURAL_REVERSAL": 2.5,
            "TREND_FOLLOWING": 1.5,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
            "RANGE_MEAN_REVERSION": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL"],
        sl_atr_multiplier=2.80,
        min_target_rr=2.0,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=2.60,
        session_restriction=True,
        allowed_utc_hours=(13, 21),
        contract_size=1.0,
        pip_size=1.0,
        pip_value_per_lot=1.0,
        digits=2,
        typical_spread_pips=2.5,
        max_allowed_spread_pips=8.0,
        commission_per_lot=0.0,
        min_volume=0.1,
        volume_step=0.1,
        margin_pct=0.2
    ),

    # =========================================================================
    # 3. COMMODITIES (XM Ultra Low Standard Specs: $0 Commission)
    # =========================================================================
    # Registered 2026-09-15: XAGUSD was absent from this table and so fell
    # through to the generic FOREX template, giving it a 100,000 contract size
    # and 5 digits instead of 5,000 and 3 - a 20x sizing error.
    "XAGUSD": SymbolProfileConfig(
        symbol="XAGUSD",
        canonical="XAGUSD",
        asset_class="COMMODITY",
        strategy_weights={
            "LIQUIDITY_SWEEP_REVERSAL": 3.5,
            "CHOCH_STRUCTURAL_REVERSAL": 3.0,
            "RANGE_MEAN_REVERSION": 2.5,
            "TREND_PULLBACK": 0.0,
            "TREND_FOLLOWING": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_PULLBACK", "TREND_FOLLOWING"],
        sl_atr_multiplier=1.40,
        min_target_rr=2.0,
        asym_rr=3.5,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=2.20,
        session_restriction=True,
        allowed_utc_hours=(12, 19),
        contract_size=5000.0,
        pip_size=0.01,
        pip_value_per_lot=50.0,
        digits=3,
        typical_spread_pips=4.0,
        max_allowed_spread_pips=12.0,
        commission_per_lot=0.0,
        min_volume=0.01,
        volume_step=0.01,
        margin_pct=0.2
    ),

    "XAUUSD": SymbolProfileConfig(
        symbol="XAUUSD",
        canonical="XAUUSD",
        asset_class="COMMODITY",
        strategy_weights={
            "TREND_PULLBACK": 3.5,
            "LIQUIDITY_SWEEP_REVERSAL": 3.5,
            "CHOCH_STRUCTURAL_REVERSAL": 3.0,
            "BREAKOUT_EXPANSION": 1.5,
            "RANGE_MEAN_REVERSION": 0.0,
            "TREND_FOLLOWING": 0.0,
        },
        banned_strategies=["RANGE_MEAN_REVERSION", "TREND_FOLLOWING"],
        sl_atr_multiplier=1.50,                # XAUUSD structural stops beyond the swept wick + 1.5x ATR cushion (published XAu standard 0.3-0.5 ATR buffer; sub-1x ATR stops sit inside one candle's range and get swept)
        min_target_rr=2.0,
        asym_rr=3.8,
        anti_wick_buffer_atr=0.45,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=2.00,
        session_restriction=True,
        # NY data-driven hours: XAU volatility/liquidity peak sits 12:00-15:30 UTC
        # (13:00 UTC is the widest hour at ~$24 range; 21:00 UTC rollover dies to
        # ~$2.80). Our own live ledger agrees: the 12-14h block is the only
        # consistently positive gold segment, while 15-17h and 06-08h were the
        # two biggest bleeder clusters. Asia sessions are mostly uninformed
        # flow (Tokyo dominance documented for metals) and are excluded.
        allowed_utc_hours=(12, 16),
        contract_size=100.0,
        pip_size=0.1,
        pip_value_per_lot=10.0,
        digits=2,
        typical_spread_pips=2.0,
        max_allowed_spread_pips=5.0,
        commission_per_lot=0.0,
        min_volume=0.01,
        volume_step=0.01,
        margin_pct=0.1
    ),

    "WTI": SymbolProfileConfig(
        symbol="WTI",
        canonical="WTI",
        asset_class="COMMODITY",
        strategy_weights={
            "LIQUIDITY_SWEEP_REVERSAL": 3.5,
            "RANGE_MEAN_REVERSION": 3.5,
            "CHOCH_STRUCTURAL_REVERSAL": 2.5,
            "BREAKOUT_EXPANSION": 0.0,
            "TREND_FOLLOWING": 0.0,
            "TREND_PULLBACK": 0.0,
        },
        banned_strategies=["TREND_PULLBACK", "TREND_FOLLOWING", "BREAKOUT_EXPANSION"],
        sl_atr_multiplier=2.80,
        min_target_rr=2.0,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.33,
        be_trigger_r=1.80,
        runner_trail_atr=2.60,
        session_restriction=True,
        allowed_utc_hours=(8, 19),
        contract_size=100.0,
        pip_size=0.01,
        pip_value_per_lot=1.0,
        digits=2,
        typical_spread_pips=13.0,
        max_allowed_spread_pips=25.0,
        commission_per_lot=0.0,
        min_volume=0.01,
        volume_step=0.01,
        margin_pct=0.2
    ),

    # =========================================================================
    # 4. FOREX MAJORS & CROSSES (XM Ultra Low Standard Specs: $0 Commission)
    # =========================================================================
    "EURUSD": SymbolProfileConfig(
        symbol="EURUSD", canonical="EURUSD", asset_class="FOREX",
        strategy_weights={"RANGE_MEAN_REVERSION": 2.8, "TREND_PULLBACK": 2.5, "CHOCH_STRUCTURAL_REVERSAL": 2.2, "LIQUIDITY_SWEEP_REVERSAL": 2.0, "TREND_FOLLOWING": 2.0, "BREAKOUT_EXPANSION": 0.5},
        banned_strategies=[], sl_atr_multiplier=1.40, min_target_rr=2.0, asym_rr=3.5,
        fast_cash_r=1.50, fast_cash_volume_pct=0.33, be_trigger_r=1.80, runner_trail_atr=2.00,
        session_restriction=True, allowed_utc_hours=(7, 17), contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=1.6, max_allowed_spread_pips=3.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "GBPUSD": SymbolProfileConfig(
        symbol="GBPUSD", canonical="GBPUSD", asset_class="FOREX",
        strategy_weights={"TREND_PULLBACK": 3.0, "CHOCH_STRUCTURAL_REVERSAL": 2.6, "LIQUIDITY_SWEEP_REVERSAL": 2.2, "TREND_FOLLOWING": 2.2, "RANGE_MEAN_REVERSION": 1.8, "BREAKOUT_EXPANSION": 0.5},
        banned_strategies=[], sl_atr_multiplier=1.60, min_target_rr=2.0, asym_rr=3.5,
        fast_cash_r=1.50, fast_cash_volume_pct=0.33, be_trigger_r=1.80, runner_trail_atr=2.20,
        session_restriction=True, allowed_utc_hours=(7, 17), contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=1.8, max_allowed_spread_pips=3.8, commission_per_lot=0.0, margin_pct=0.1
    ),
    "USDJPY": SymbolProfileConfig(
        symbol="USDJPY", canonical="USDJPY", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.5, "RANGE_MEAN_REVERSION": 3.0, "LIQUIDITY_SWEEP_REVERSAL": 2.0, "TREND_PULLBACK": 0.0, "TREND_FOLLOWING": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_PULLBACK", "TREND_FOLLOWING"], sl_atr_multiplier=2.60, min_target_rr=2.0, asym_rr=3.5,
        fast_cash_r=1.50, fast_cash_volume_pct=0.33, be_trigger_r=1.80, runner_trail_atr=2.60,
        session_restriction=True, allowed_utc_hours=(0, 16), contract_size=100_000.0, pip_size=0.01, pip_value_per_lot=6.80, digits=3,
        typical_spread_pips=1.8, max_allowed_spread_pips=3.8, commission_per_lot=0.0, margin_pct=0.1
    ),
    "AUDUSD": SymbolProfileConfig(
        symbol="AUDUSD", canonical="AUDUSD", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.0, "RANGE_MEAN_REVERSION": 2.8, "LIQUIDITY_SWEEP_REVERSAL": 2.5, "TREND_PULLBACK": 2.2, "TREND_FOLLOWING": 2.0, "BREAKOUT_EXPANSION": 0.5},
        banned_strategies=[], sl_atr_multiplier=2.60, min_target_rr=2.0, asym_rr=3.5,
        fast_cash_r=1.50, fast_cash_volume_pct=0.33, be_trigger_r=1.80, runner_trail_atr=2.60,
        session_restriction=False, contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=1.8, max_allowed_spread_pips=3.8, commission_per_lot=0.0, margin_pct=0.1
    ),
    "USDCHF": SymbolProfileConfig(
        symbol="USDCHF", canonical="USDCHF", asset_class="FOREX",
        strategy_weights={"RANGE_MEAN_REVERSION": 3.2, "CHOCH_STRUCTURAL_REVERSAL": 2.8, "LIQUIDITY_SWEEP_REVERSAL": 1.8, "TREND_PULLBACK": 0.0, "TREND_FOLLOWING": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_PULLBACK", "TREND_FOLLOWING"], sl_atr_multiplier=2.60, min_target_rr=2.0, asym_rr=3.5,
        fast_cash_r=1.50, fast_cash_volume_pct=0.33, be_trigger_r=1.80, runner_trail_atr=2.60,
        session_restriction=False, allowed_utc_hours=None, contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=1.1, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "USDCAD": SymbolProfileConfig(
        symbol="USDCAD", canonical="USDCAD", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.0, "TREND_PULLBACK": 2.8, "TREND_FOLLOWING": 2.4, "RANGE_MEAN_REVERSION": 2.0, "LIQUIDITY_SWEEP_REVERSAL": 1.8, "BREAKOUT_EXPANSION": 0.5},
        banned_strategies=[], sl_atr_multiplier=2.60, min_target_rr=2.0, asym_rr=3.5,
        fast_cash_r=1.50, fast_cash_volume_pct=0.33, be_trigger_r=1.80, runner_trail_atr=2.60,
        session_restriction=False, allowed_utc_hours=None, contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=7.50, digits=5,
        typical_spread_pips=1.1, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "NZDUSD": SymbolProfileConfig(
        symbol="NZDUSD", canonical="NZDUSD", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.0, "RANGE_MEAN_REVERSION": 2.6, "TREND_PULLBACK": 2.4, "LIQUIDITY_SWEEP_REVERSAL": 2.0, "TREND_FOLLOWING": 2.0, "BREAKOUT_EXPANSION": 0.5},
        banned_strategies=[], sl_atr_multiplier=2.60, min_target_rr=2.0, asym_rr=3.5,
        fast_cash_r=1.50, fast_cash_volume_pct=0.33, be_trigger_r=1.80, runner_trail_atr=2.60,
        session_restriction=False, contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=1.2, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "EURJPY": SymbolProfileConfig(
        symbol="EURJPY", canonical="EURJPY", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.0, "TREND_PULLBACK": 2.8, "TREND_FOLLOWING": 2.4, "LIQUIDITY_SWEEP_REVERSAL": 1.8, "RANGE_MEAN_REVERSION": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION"], sl_atr_multiplier=2.60, min_target_rr=2.0, asym_rr=3.5,
        fast_cash_r=1.50, fast_cash_volume_pct=0.33, be_trigger_r=1.80, runner_trail_atr=2.60,
        session_restriction=False, contract_size=100_000.0, pip_size=0.01, pip_value_per_lot=6.80, digits=3,
        typical_spread_pips=1.0, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "GBPJPY": SymbolProfileConfig(
        symbol="GBPJPY", canonical="GBPJPY", asset_class="FOREX",
        strategy_weights={"TREND_PULLBACK": 3.2, "CHOCH_STRUCTURAL_REVERSAL": 2.8, "TREND_FOLLOWING": 2.4, "BREAKOUT_EXPANSION": 0.0, "RANGE_MEAN_REVERSION": 0.0, "LIQUIDITY_SWEEP_REVERSAL": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL"], sl_atr_multiplier=2.60, min_target_rr=2.0, asym_rr=3.5,
        fast_cash_r=1.50, fast_cash_volume_pct=0.33, be_trigger_r=1.80, runner_trail_atr=2.60,
        session_restriction=False, contract_size=100_000.0, pip_size=0.01, pip_value_per_lot=6.80, digits=3,
        typical_spread_pips=1.2, max_allowed_spread_pips=3.0, commission_per_lot=0.0, margin_pct=0.1
    ),
}

# Alias resolution mapping
_ALIAS_TO_CANONICAL: Dict[str, str] = {
    "GOLD": "XAUUSD", "GOLD#": "XAUUSD", "GOLD.I#": "XAUUSD", "GOLD.I": "XAUUSD", "XAUUSD#": "XAUUSD", "XAUUSD.I#": "XAUUSD", "XAUUSD.I": "XAUUSD",
    "BTCUSD#": "BTCUSD", "BTCUSD.I#": "BTCUSD", "BTCUSD.I": "BTCUSD", "BITCOIN": "BTCUSD",
    "ETHUSD#": "ETHUSD", "ETHUSD.I#": "ETHUSD", "ETHEREUM": "ETHUSD", "ETH": "ETHUSD",
    "SOLUSD#": "SOLUSD", "SOLUSD.I#": "SOLUSD", "SOLANA": "SOLUSD", "SOL": "SOLUSD",
    "US500#": "US500", "SPX500": "US500", "SP500": "US500", "US500.I#": "US500", "US500CASH#": "US500",
    "NAS100#": "NAS100", "USTECH": "NAS100", "NDX100": "NAS100", "US100": "NAS100", "US100CASH#": "NAS100",
    "US30#": "US30", "DJ30": "US30", "WALLSTREET": "US30", "US30CASH#": "US30",
    "USOIL": "WTI", "OIL": "WTI", "CRUDE": "WTI", "USOIL.I#": "WTI", "OIL.I#": "WTI", "CL": "WTI", "OILCASH#": "WTI",
    "GER40#": "GER40", "GER40.I#": "GER40", "GER40.I": "GER40", "GER40CASH#": "GER40", "GER30": "GER40", "GER30#": "GER40", "DAX": "GER40", "DAX40": "GER40",
    "UK100#": "UK100", "UK100.I#": "UK100", "UK100.I": "UK100", "UK100CASH#": "UK100", "FTSE": "UK100", "FTSE100": "UK100",
    "SILVER": "XAGUSD", "SILVER#": "XAGUSD", "SILVER.I#": "XAGUSD", "SILVER.I": "XAGUSD", "XAGUSD#": "XAGUSD", "XAGUSD.I#": "XAGUSD", "XAGUSD.I": "XAGUSD",
    "WTI#": "WTI", "WTI.I#": "WTI", "OILCASH#": "WTI", "USOILCASH#": "WTI",
    "ETHBTC#": "ETHBTC", "ETHBTC.I#": "ETHBTC", "ETHBTC.I": "ETHBTC",
    "GOLD24-7.I#": "XAUUSD", "GOLD24-7#": "XAUUSD",
    "EURUSD#": "EURUSD", "EURUSD.I#": "EURUSD",
    "GBPUSD#": "GBPUSD", "GBPUSD.I#": "GBPUSD",
    "USDJPY#": "USDJPY", "USDJPY.I#": "USDJPY",
    "AUDUSD#": "AUDUSD", "AUDUSD.I#": "AUDUSD",
    "USDCHF#": "USDCHF", "USDCHF.I#": "USDCHF",
    "USDCAD#": "USDCAD", "USDCAD.I#": "USDCAD",
    "NZDUSD#": "NZDUSD", "NZDUSD.I#": "NZDUSD",
    "EURJPY#": "EURJPY", "EURJPY.I#": "EURJPY",
    "GBPJPY#": "GBPJPY", "GBPJPY.I#": "GBPJPY",
}


def get_symbol_profile_config(symbol: str) -> SymbolProfileConfig:
    """Returns the dedicated, symbol-specific configuration profile."""
    key = str(symbol or "XAUUSD").upper().strip()
    canonical = _ALIAS_TO_CANONICAL.get(key, key)
    if canonical in SYMBOL_PROFILES:
        return SYMBOL_PROFILES[canonical]

    # Try resolving via global SymbolRegistry before falling back
    try:
        from jarvis.data.symbol_registry import resolve as resolve_symbol
        reg_canon = resolve_symbol(symbol).canonical
        if reg_canon in SYMBOL_PROFILES:
            return SYMBOL_PROFILES[reg_canon]
    except Exception:
        pass

    # Fallback to general Forex template if symbol is unknown. This used to be
    # completely silent, which is how GER40/UK100 ended up sized 100,000x wrong
    # for months. Make it loud so an unregistered symbol cannot slip through.
    logger.error(
        "symbol_profile_config: %r is not registered; falling back to the "
        "generic FOREX template (contract_size=100_000, pip_size=0.0001, "
        "digits=5). Position sizing for this symbol will be wrong unless it is "
        "an actual FX major.", symbol,
    )
    return SymbolProfileConfig(
        symbol=symbol,
        canonical=canonical,
        asset_class="FOREX",
        strategy_weights={"RANGE_MEAN_REVERSION": 2.0, "LIQUIDITY_SWEEP_REVERSAL": 2.0, "TREND_PULLBACK": 1.5, "CHOCH_STRUCTURAL_REVERSAL": 1.5},
        contract_size=100_000.0,
        pip_size=0.0001,
        pip_value_per_lot=10.0,
        digits=5
    )
