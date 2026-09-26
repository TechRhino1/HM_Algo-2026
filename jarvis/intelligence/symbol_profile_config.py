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
    fast_cash_r: float = 1.00
    fast_cash_volume_pct: float = 0.50  # Bank 50% or 60%
    be_trigger_r: float = 1.00
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
            "TREND_FOLLOWING": 3.0,
            "RANGE_MEAN_REVERSION": 2.5,
            "BREAKOUT_EXPANSION": 1.0,
            "CHOCH_STRUCTURAL_REVERSAL": 0.0,
            "TREND_PULLBACK": 0.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
        },
        banned_strategies=["TREND_PULLBACK", "LIQUIDITY_SWEEP_REVERSAL", "CHOCH_STRUCTURAL_REVERSAL"],
        sl_atr_multiplier=1.80,
        min_target_rr=0.60,
        asym_rr=2.5,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=999.0,
        fast_cash_volume_pct=0.0,
        be_trigger_r=999.0,
        runner_trail_atr=1.50,
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
            "RANGE_MEAN_REVERSION": 5.0,
            "CHOCH_STRUCTURAL_REVERSAL": 0.0,
            "TREND_PULLBACK": 0.0,
            "TREND_FOLLOWING": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "LIQUIDITY_SWEEP_REVERSAL", "TREND_FOLLOWING", "TREND_PULLBACK", "CHOCH_STRUCTURAL_REVERSAL"],
        sl_atr_multiplier=3.20,
        min_target_rr=2.0,
        asym_rr=4.0,
        anti_wick_buffer_atr=0.45,
        fast_cash_r=1.80,
        fast_cash_volume_pct=0.35,
        be_trigger_r=1.20,
        runner_trail_atr=2.60,
        session_restriction=False,
        contract_size=1.0,
        pip_size=0.01,
        pip_value_per_lot=0.01,
        digits=2,
        typical_spread_pips=120.0,
        max_allowed_spread_pips=300.0,
        commission_per_lot=0.0,
        min_volume=0.01,
        volume_step=0.01,
        margin_pct=0.5
    ),

    "SOLUSD": SymbolProfileConfig(
        symbol="SOLUSD",
        canonical="SOLUSD",
        asset_class="CRYPTO",
        strategy_weights={
            "CHOCH_STRUCTURAL_REVERSAL": 5.0,
            "TREND_PULLBACK": 0.0,
            "TREND_FOLLOWING": 0.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
            "RANGE_MEAN_REVERSION": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
        },
        banned_strategies=["TREND_PULLBACK", "BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL", "TREND_FOLLOWING"],
        sl_atr_multiplier=3.20,
        min_target_rr=2.0,
        asym_rr=4.0,
        anti_wick_buffer_atr=0.45,
        fast_cash_r=1.40,
        fast_cash_volume_pct=0.40,
        be_trigger_r=1.10,
        runner_trail_atr=2.50,
        session_restriction=False,
        contract_size=1.0,
        pip_size=0.01,
        pip_value_per_lot=0.01,
        digits=2,
        typical_spread_pips=15.0,
        max_allowed_spread_pips=50.0,
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
            "TREND_PULLBACK": 3.8,
            "CHOCH_STRUCTURAL_REVERSAL": 3.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
            "TREND_FOLLOWING": 1.0,
            "RANGE_MEAN_REVERSION": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL"],
        sl_atr_multiplier=2.80,
        min_target_rr=1.8,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.30,
        fast_cash_volume_pct=0.55,
        be_trigger_r=1.00,
        runner_trail_atr=2.60,
        session_restriction=True,
        allowed_utc_hours=(14, 19),
        contract_size=1.0,
        pip_size=0.1,
        pip_value_per_lot=1.0,
        digits=1,
        typical_spread_pips=0.6,
        max_allowed_spread_pips=2.5,
        commission_per_lot=0.0,
        min_volume=0.01,
        volume_step=0.01,
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
            "TREND_PULLBACK": 3.6,
            "CHOCH_STRUCTURAL_REVERSAL": 2.8,
            "TREND_FOLLOWING": 1.0,
            "RANGE_MEAN_REVERSION": 0.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL"],
        sl_atr_multiplier=2.80,
        min_target_rr=1.8,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.30,
        fast_cash_volume_pct=0.55,
        be_trigger_r=1.00,
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
        min_volume=0.01,
        volume_step=0.01,
        margin_pct=0.2
    ),

    "UK100": SymbolProfileConfig(
        symbol="UK100",
        canonical="UK100",
        asset_class="INDEX",
        strategy_weights={
            "TREND_PULLBACK": 3.6,
            "CHOCH_STRUCTURAL_REVERSAL": 2.8,
            "TREND_FOLLOWING": 1.0,
            "RANGE_MEAN_REVERSION": 0.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL"],
        sl_atr_multiplier=2.80,
        min_target_rr=1.8,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.30,
        fast_cash_volume_pct=0.55,
        be_trigger_r=1.00,
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
        min_volume=0.01,
        volume_step=0.01,
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
        fast_cash_r=1.80,
        fast_cash_volume_pct=0.35,
        be_trigger_r=1.20,
        runner_trail_atr=2.60,
        session_restriction=True,
        allowed_utc_hours=(14, 19),
        contract_size=1.0,
        pip_size=1.0,
        pip_value_per_lot=1.0,
        digits=1,
        typical_spread_pips=2.0,
        max_allowed_spread_pips=7.0,
        commission_per_lot=0.0,
        min_volume=0.01,
        volume_step=0.01,
        margin_pct=0.2
    ),

    "US30": SymbolProfileConfig(
        symbol="US30",
        canonical="US30",
        asset_class="INDEX",
        strategy_weights={
            "TREND_PULLBACK": 3.8,
            "CHOCH_STRUCTURAL_REVERSAL": 0.0,
            "TREND_FOLLOWING": 1.0,
            "LIQUIDITY_SWEEP_REVERSAL": 0.0,
            "RANGE_MEAN_REVERSION": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL", "CHOCH_STRUCTURAL_REVERSAL"],
        sl_atr_multiplier=2.80,
        min_target_rr=1.8,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.30,
        fast_cash_volume_pct=0.55,
        be_trigger_r=1.00,
        runner_trail_atr=2.60,
        session_restriction=True,
        allowed_utc_hours=(14, 19),
        contract_size=1.0,
        pip_size=1.0,
        pip_value_per_lot=1.0,
        digits=1,
        typical_spread_pips=2.5,
        max_allowed_spread_pips=8.0,
        commission_per_lot=0.0,
        min_volume=0.01,
        volume_step=0.01,
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
            "TREND_PULLBACK": 3.0,
            "CHOCH_STRUCTURAL_REVERSAL": 2.4,
            "TREND_FOLLOWING": 1.2,
            "LIQUIDITY_SWEEP_REVERSAL": 1.5,
            "RANGE_MEAN_REVERSION": 0.0,
            "BREAKOUT_EXPANSION": 0.0,
        },
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION"],
        sl_atr_multiplier=1.80,
        min_target_rr=2.0,
        asym_rr=3.0,
        anti_wick_buffer_atr=0.40,
        fast_cash_r=1.20,
        fast_cash_volume_pct=0.50,
        be_trigger_r=1.00,
        runner_trail_atr=2.20,
        session_restriction=False,
        allowed_utc_hours=None,
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
            "LIQUIDITY_SWEEP_REVERSAL": 3.0,
            "CHOCH_STRUCTURAL_REVERSAL": 2.5,
            "TREND_PULLBACK": 2.2,
            "BREAKOUT_EXPANSION": 1.2,
            "RANGE_MEAN_REVERSION": 1.5,
            "TREND_FOLLOWING": 1.8,
        },
        banned_strategies=[],
        sl_atr_multiplier=2.80,
        min_target_rr=2.0,
        asym_rr=4.2,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.50,
        fast_cash_volume_pct=0.60,
        be_trigger_r=1.80,
        runner_trail_atr=2.60,
        session_restriction=False,
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
            "LIQUIDITY_SWEEP_REVERSAL": 2.4,
            "TREND_PULLBACK": 2.2,
            "RANGE_MEAN_REVERSION": 1.6,
            "TREND_FOLLOWING": 1.2,
            "CHOCH_STRUCTURAL_REVERSAL": 1.5,
            "BREAKOUT_EXPANSION": 0.8,
        },
        banned_strategies=[],
        sl_atr_multiplier=2.80,
        min_target_rr=2.0,
        asym_rr=3.6,
        anti_wick_buffer_atr=0.35,
        fast_cash_r=1.00,
        fast_cash_volume_pct=0.60,
        be_trigger_r=1.00,
        runner_trail_atr=2.60,
        session_restriction=False,
        contract_size=1000.0,
        pip_size=0.01,
        pip_value_per_lot=10.0,
        digits=2,
        typical_spread_pips=3.0,
        max_allowed_spread_pips=8.0,
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
        strategy_weights={"RANGE_MEAN_REVERSION": 3.8, "CHOCH_STRUCTURAL_REVERSAL": 0.0, "LIQUIDITY_SWEEP_REVERSAL": 0.0, "TREND_PULLBACK": 0.0, "TREND_FOLLOWING": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_FOLLOWING", "TREND_PULLBACK", "LIQUIDITY_SWEEP_REVERSAL", "CHOCH_STRUCTURAL_REVERSAL"], sl_atr_multiplier=2.60, min_target_rr=1.7, asym_rr=3.5,
        fast_cash_r=1.30, fast_cash_volume_pct=0.50, be_trigger_r=1.00, runner_trail_atr=2.60,
        session_restriction=False, allowed_utc_hours=None, contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=0.7, max_allowed_spread_pips=2.0, commission_per_lot=0.0, margin_pct=0.1
    ),
    "GBPUSD": SymbolProfileConfig(
        symbol="GBPUSD", canonical="GBPUSD", asset_class="FOREX",
        strategy_weights={"TREND_PULLBACK": 3.5, "CHOCH_STRUCTURAL_REVERSAL": 2.8, "LIQUIDITY_SWEEP_REVERSAL": 2.2, "RANGE_MEAN_REVERSION": 1.8, "TREND_FOLLOWING": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_FOLLOWING"], sl_atr_multiplier=2.60, min_target_rr=1.7, asym_rr=3.5,
        fast_cash_r=1.30, fast_cash_volume_pct=0.50, be_trigger_r=1.00, runner_trail_atr=2.60,
        session_restriction=True, allowed_utc_hours=(7, 18), contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=0.9, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "USDJPY": SymbolProfileConfig(
        symbol="USDJPY", canonical="USDJPY", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.5, "LIQUIDITY_SWEEP_REVERSAL": 2.8, "TREND_FOLLOWING": 2.2, "TREND_PULLBACK": 0.0, "RANGE_MEAN_REVERSION": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "TREND_PULLBACK"], sl_atr_multiplier=2.80, min_target_rr=1.7, asym_rr=3.5,
        fast_cash_r=1.30, fast_cash_volume_pct=0.50, be_trigger_r=1.00, runner_trail_atr=2.60,
        session_restriction=False, contract_size=100_000.0, pip_size=0.01, pip_value_per_lot=6.80, digits=3,
        typical_spread_pips=0.8, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "AUDUSD": SymbolProfileConfig(
        symbol="AUDUSD", canonical="AUDUSD", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.5, "RANGE_MEAN_REVERSION": 3.0, "LIQUIDITY_SWEEP_REVERSAL": 2.8, "TREND_PULLBACK": 0.0, "TREND_FOLLOWING": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_FOLLOWING", "TREND_PULLBACK"], sl_atr_multiplier=2.60, min_target_rr=1.7, asym_rr=3.5,
        fast_cash_r=1.30, fast_cash_volume_pct=0.50, be_trigger_r=1.00, runner_trail_atr=2.60,
        session_restriction=False, contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=0.9, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "USDCHF": SymbolProfileConfig(
        symbol="USDCHF", canonical="USDCHF", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.8, "RANGE_MEAN_REVERSION": 0.0, "LIQUIDITY_SWEEP_REVERSAL": 0.0, "TREND_PULLBACK": 0.0, "TREND_FOLLOWING": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_FOLLOWING", "TREND_PULLBACK", "LIQUIDITY_SWEEP_REVERSAL", "RANGE_MEAN_REVERSION"], sl_atr_multiplier=2.60, min_target_rr=1.7, asym_rr=3.5,
        fast_cash_r=1.30, fast_cash_volume_pct=0.50, be_trigger_r=1.00, runner_trail_atr=2.60,
        session_restriction=True, allowed_utc_hours=(7, 18), contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=1.1, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "USDCAD": SymbolProfileConfig(
        symbol="USDCAD", canonical="USDCAD", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.2, "TREND_PULLBACK": 2.8, "TREND_FOLLOWING": 2.2, "RANGE_MEAN_REVERSION": 1.8, "LIQUIDITY_SWEEP_REVERSAL": 1.5, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION"], sl_atr_multiplier=2.60, min_target_rr=1.7, asym_rr=3.5,
        fast_cash_r=1.30, fast_cash_volume_pct=0.50, be_trigger_r=1.00, runner_trail_atr=2.60,
        session_restriction=True, allowed_utc_hours=(7, 20), contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=7.50, digits=5,
        typical_spread_pips=1.1, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "NZDUSD": SymbolProfileConfig(
        symbol="NZDUSD", canonical="NZDUSD", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.2, "RANGE_MEAN_REVERSION": 2.8, "TREND_PULLBACK": 2.2, "LIQUIDITY_SWEEP_REVERSAL": 1.8, "TREND_FOLLOWING": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "TREND_FOLLOWING"], sl_atr_multiplier=2.60, min_target_rr=1.7, asym_rr=3.5,
        fast_cash_r=1.30, fast_cash_volume_pct=0.50, be_trigger_r=1.00, runner_trail_atr=2.60,
        session_restriction=False, contract_size=100_000.0, pip_size=0.0001, pip_value_per_lot=10.0, digits=5,
        typical_spread_pips=1.2, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "EURJPY": SymbolProfileConfig(
        symbol="EURJPY", canonical="EURJPY", asset_class="FOREX",
        strategy_weights={"CHOCH_STRUCTURAL_REVERSAL": 3.0, "TREND_PULLBACK": 2.8, "TREND_FOLLOWING": 2.4, "LIQUIDITY_SWEEP_REVERSAL": 1.8, "RANGE_MEAN_REVERSION": 0.0, "BREAKOUT_EXPANSION": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION"], sl_atr_multiplier=2.60, min_target_rr=1.7, asym_rr=3.5,
        fast_cash_r=1.30, fast_cash_volume_pct=0.50, be_trigger_r=1.00, runner_trail_atr=2.60,
        session_restriction=False, contract_size=100_000.0, pip_size=0.01, pip_value_per_lot=6.80, digits=3,
        typical_spread_pips=1.0, max_allowed_spread_pips=2.5, commission_per_lot=0.0, margin_pct=0.1
    ),
    "GBPJPY": SymbolProfileConfig(
        symbol="GBPJPY", canonical="GBPJPY", asset_class="FOREX",
        strategy_weights={"TREND_PULLBACK": 3.2, "CHOCH_STRUCTURAL_REVERSAL": 2.8, "TREND_FOLLOWING": 2.4, "BREAKOUT_EXPANSION": 0.0, "RANGE_MEAN_REVERSION": 0.0, "LIQUIDITY_SWEEP_REVERSAL": 0.0},
        banned_strategies=["BREAKOUT_EXPANSION", "RANGE_MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL"], sl_atr_multiplier=2.60, min_target_rr=1.7, asym_rr=3.5,
        fast_cash_r=1.30, fast_cash_volume_pct=0.50, be_trigger_r=1.00, runner_trail_atr=2.60,
        session_restriction=False, contract_size=100_000.0, pip_size=0.01, pip_value_per_lot=6.80, digits=3,
        typical_spread_pips=1.2, max_allowed_spread_pips=3.0, commission_per_lot=0.0, margin_pct=0.1
    ),
}

# Alias resolution mapping
_ALIAS_TO_CANONICAL: Dict[str, str] = {
    "GOLD": "XAUUSD", "GOLD.I#": "XAUUSD", "GOLD.I": "XAUUSD", "XAUUSD#": "XAUUSD", "XAUUSD.I#": "XAUUSD", "XAUUSD.I": "XAUUSD",
    "BTCUSD#": "BTCUSD", "BTCUSD.I#": "BTCUSD", "BTCUSD.I": "BTCUSD", "BITCOIN": "BTCUSD",
    "ETHUSD#": "ETHUSD", "ETHUSD.I#": "ETHUSD", "ETHEREUM": "ETHUSD", "ETH": "ETHUSD",
    "SOLUSD#": "SOLUSD", "SOLUSD.I#": "SOLUSD", "SOLANA": "SOLUSD", "SOL": "SOLUSD",
    "US500#": "US500", "SPX500": "US500", "SP500": "US500", "US500.I#": "US500", "US500CASH#": "US500",
    "NAS100#": "NAS100", "USTECH": "NAS100", "NDX100": "NAS100", "US100": "NAS100", "US100CASH#": "NAS100",
    "US30#": "US30", "DJ30": "US30", "WALLSTREET": "US30", "US30CASH#": "US30",
    "USOIL": "WTI", "OIL": "WTI", "CRUDE": "WTI", "USOIL.I#": "WTI", "OIL.I#": "WTI", "CL": "WTI", "OILCASH#": "WTI",
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
