"""
HM Algo 2.0 — Autonomous LLM Master Controller & Market Governor.

Provides dynamic, macro-aware governance over the algorithmic trading system:
1. Gathers real-time account state, symbol spreads, market regimes, and recent trade outcomes.
2. Uses an advanced LLM (via CopilotProvider) or an institutional Bayesian heuristic fallback
   to determine:
   - Dynamic Risk Posture: DEFENSIVE (0.50x-0.75x), NORMAL (1.00x), or AGGRESSIVE (1.15x-1.25x).
   - Dynamic Symbol Directives: ENABLED, THROTTLED, or PAUSED per symbol.
   - Dynamic Strategy Prioritization: symbol-specific preferred strategies based on session and regime.
   - Max Concurrent Position limits for low-balance account protection.
3. Thread-safe caching with TTL prevents latency impact on the high-frequency execution hot path.
4. Hard institutional bounds guarantee safety: risk multipliers never exceed [0.35, 1.25]
   and micro-account single-trade loss caps are never compromised.
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict

from jarvis.api.copilot_provider import CopilotProvider
from jarvis.application.state_manager import StateManager, GLOBAL_STATE
from jarvis.data.schemas import MarketRegime

logger = logging.getLogger("JARVIS_LLMController")


@dataclass
class SymbolDirective:
    symbol: str
    status: str  # "ENABLED", "THROTTLED", "PAUSED"
    preferred_strategy: str
    reason: str


@dataclass
class GovernorDirectives:
    risk_posture: str  # "DEFENSIVE", "NORMAL", "AGGRESSIVE"
    risk_multiplier: float  # [0.35, 1.25]
    max_concurrent_trades: int  # 1 to 3
    symbol_directives: Dict[str, SymbolDirective] = field(default_factory=dict)
    macro_reasoning: str = ""
    confidence: float = 0.80
    source: str = "OFFLINE_HEURISTIC"  # "LLM" or "OFFLINE_HEURISTIC"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert SymbolDirective dataclass to plain dict
        d["symbol_directives"] = {k: asdict(v) for k, v in self.symbol_directives.items()}
        return d


class AutonomousLLMSystemController:
    """
    Master Algorithmic Governor using LLM reasoning (with robust offline fallback)
    to dynamically adapt risk dials, symbol routing, and strategy preferences.
    """

    def __init__(
        self,
        state_manager: StateManager = GLOBAL_STATE,
        provider: Optional[CopilotProvider] = None,
        cache_ttl_sec: float = 180.0,  # 3 minutes default evaluation cache
    ):
        self.state_manager = state_manager or GLOBAL_STATE
        self.provider = provider or CopilotProvider.from_env()
        self.cache_ttl_sec = cache_ttl_sec

        self._lock = threading.Lock()
        self._last_eval_time: float = 0.0
        self._cached_directives: Optional[GovernorDirectives] = None

    def get_latest_directives(self, force_refresh: bool = False) -> GovernorDirectives:
        """
        Returns the latest active governor directives.
        Uses cached evaluation unless expired or forced.
        """
        now = time.time()
        with self._lock:
            if (
                not force_refresh
                and self._cached_directives is not None
                and (now - self._last_eval_time) < self.cache_ttl_sec
            ):
                return self._cached_directives

        # Compute fresh directives
        directives = self._evaluate_system_and_market()

        with self._lock:
            self._cached_directives = directives
            self._last_eval_time = now

        # Publish to state manager for UI visibility
        try:
            if hasattr(self.state_manager, "update_llm_governor_state"):
                self.state_manager.update_llm_governor_state(directives.to_dict())
        except Exception as e:
            logger.debug(f"Could not push governor state to state_manager: {e}")

        return directives

    def _gather_system_context(self) -> Dict[str, Any]:
        """Gathers real-time telemetry from state_manager, open positions, and account."""
        acc = getattr(self.state_manager, "account", None)
        equity = float(getattr(acc, "equity", 500.0) or 500.0)
        balance = float(getattr(acc, "balance", 500.0) or 500.0)
        drawdown_pct = max(0.0, ((balance - equity) / max(1.0, balance)) * 100.0)

        open_positions = getattr(self.state_manager, "positions", []) or []
        open_count = len(open_positions)
        floating_pnl = sum(float(getattr(p, "profit", 0.0) or 0.0) for p in open_positions)

        active_symbols = getattr(self.state_manager, "active_symbols", [])
        if not active_symbols:
            try:
                from jarvis.config.settings import SETTINGS
                active_symbols = list(SETTINGS.trading.symbols)
            except Exception:
                active_symbols = ["XAUUSD", "BTCUSD", "ETHUSD", "EURUSD", "GBPUSD"]

        # Gather per-symbol context
        now_dt = datetime.now(timezone.utc)
        current_hour_utc = now_dt.hour
        is_weekend = now_dt.weekday() >= 5

        symbol_contexts = {}
        for sym in active_symbols:
            ctx = self.state_manager.get_market_context(sym) if hasattr(self.state_manager, "get_market_context") else getattr(self.state_manager, "market_contexts", {}).get(sym)
            dec = getattr(self.state_manager, "latest_decisions", {}).get(sym)
            regime = getattr(dec, "regime", None)
            regime_str = (
                getattr(regime, "primary_regime", MarketRegime.RANGE).value
                if hasattr(getattr(regime, "primary_regime", None), "value")
                else str(getattr(regime, "primary_regime", "RANGE") if regime else "RANGE")
            )
            spread_pips = float(getattr(ctx, "live_spread_pips", 0.0) or 0.0)
            typical_spread = 2.0
            try:
                # The old import targeted jarvis.market.broker_symbols, a module
                # that does not exist. Every symbol silently fell back to 2.0
                # pips, inflating BTC (1500), ETH (345), SOL (35) and XAG (4.0)
                # spreads into permanent "spread spike" pauses.
                from jarvis.data.symbol_registry import resolve

                typical_spread = float(resolve(sym).typical_spread_pips)
            except Exception as exc:
                logger.debug("Governor spread baseline fallback for %s: %s", sym, exc)

            spread_ratio = spread_pips / max(0.1, typical_spread) if spread_pips > 0 else 1.0

            symbol_contexts[sym] = {
                "regime": regime_str,
                "spread_pips": round(spread_pips, 2),
                "spread_ratio": round(spread_ratio, 2),
                "has_open_position": any(getattr(p, "symbol", "") == sym for p in open_positions),
            }

        return {
            "equity": round(equity, 2),
            "balance": round(balance, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "open_position_count": open_count,
            "floating_pnl": round(floating_pnl, 2),
            "current_hour_utc": current_hour_utc,
            "is_weekend": is_weekend,
            "symbols": symbol_contexts,
        }

    def _evaluate_system_and_market(self) -> GovernorDirectives:
        """
        Executes governance evaluation:
        Tries LLM reasoning if available; otherwise seamlessly falls back to institutional heuristic.
        """
        ctx = self._gather_system_context()

        if self.provider and self.provider.available:
            try:
                llm_directives = self._call_llm_governor(ctx)
                if llm_directives is not None:
                    return llm_directives
            except Exception as e:
                logger.warning(f"LLM Governor API call failed, falling back to heuristic: {e}")

        # Institutional Bayesian / Rule-Based Offline Governor
        return self._heuristic_governor(ctx)

    def _build_llm_prompt(self, ctx: Dict[str, Any]) -> str:
        return f"""You are the Chief Risk Officer and Master Algorithmic Governor for HM Algo 2.0.
Current Live System State:
- Equity: ${ctx['equity']}, Balance: ${ctx['balance']}, Drawdown: {ctx['drawdown_pct']}%
- Open Positions: {ctx['open_position_count']} (Floating PnL: ${ctx['floating_pnl']})
- Time UTC: {ctx['current_hour_utc']}:00 (Weekend: {ctx['is_weekend']})
- Active Symbols & Market Context:
{json.dumps(ctx['symbols'], indent=2)}

Institutional Rules:
1. Micro accounts ($400-$500) MUST preserve capital: Max 1-2 concurrent positions.
2. If Drawdown > 3.0% or multiple open losses, enforce DEFENSIVE posture (risk_multiplier 0.50x to 0.75x).
3. Asian session (00:00-06:00 UTC) & Rollover (21:00-23:00 UTC): PAUSE or THROTTLE Gold and Forex majors.
4. Weekends: PAUSE all FX and Gold; set Crypto to THROTTLED with preferred_strategy="RANGE_MEAN_REVERSION".
5. Prime London/NY sessions with normal spreads: Set NORMAL or AGGRESSIVE (risk_multiplier 1.0x to 1.25x).
6. Crypto high momentum: Prefer "TREND_FOLLOWING" or "BREAKOUT_EXPANSION" on weekdays.
7. Gold prime hours: Prefer "LIQUIDITY_SWEEP_REVERSAL" or "CHOCH_STRUCTURAL_REVERSAL".

Respond ONLY with a valid JSON object matching this schema:
{{
  "risk_posture": "DEFENSIVE" | "NORMAL" | "AGGRESSIVE",
  "risk_multiplier": float between 0.50 and 1.25,
  "max_concurrent_trades": int between 1 and 2,
  "macro_reasoning": "Brief explanation of current market conditions and risk dial adjustments",
  "confidence": float between 0.60 and 0.95,
  "symbol_directives": {{
    "<SYMBOL>": {{
      "status": "ENABLED" | "THROTTLED" | "PAUSED",
      "preferred_strategy": "LIQUIDITY_SWEEP_REVERSAL" | "TREND_FOLLOWING" | "TREND_PULLBACK" | "RANGE_MEAN_REVERSION" | "CHOCH_STRUCTURAL_REVERSAL",
      "reason": "short explanation"
    }}
  }}
}}"""

    def _call_llm_governor(self, ctx: Dict[str, Any]) -> Optional[GovernorDirectives]:
        """Calls the LLM provider and parses structured JSON directives."""
        prompt = self._build_llm_prompt(ctx)
        messages = [
            {"role": "system", "content": "You are an institutional algorithmic risk governor. Output valid JSON only."},
            {"role": "user", "content": prompt},
        ]
        response_text = self.provider.chat(messages)
        if not response_text:
            return None

        # Parse JSON from response
        clean_json = response_text.strip()
        if "```json" in clean_json:
            clean_json = clean_json.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_json:
            clean_json = clean_json.split("```")[1].split("```")[0].strip()

        data = json.loads(clean_json)

        posture = str(data.get("risk_posture", "NORMAL")).upper()
        if posture not in ("DEFENSIVE", "NORMAL", "AGGRESSIVE"):
            posture = "NORMAL"

        raw_mult = float(data.get("risk_multiplier", 1.0))
        risk_mult = max(0.35, min(1.25, raw_mult))

        max_conc = int(data.get("max_concurrent_trades", 2))
        max_conc = max(1, min(2 if ctx["equity"] < 500.0 else 3, max_conc))

        sym_dirs = {}
        for sym, d in data.get("symbol_directives", {}).items():
            sym_dirs[sym] = SymbolDirective(
                symbol=sym,
                status=str(d.get("status", "ENABLED")).upper(),
                preferred_strategy=str(d.get("preferred_strategy", "TREND_PULLBACK")),
                reason=str(d.get("reason", "LLM directive")),
            )

        return GovernorDirectives(
            risk_posture=posture,
            risk_multiplier=round(risk_mult, 2),
            max_concurrent_trades=max_conc,
            symbol_directives=sym_dirs,
            macro_reasoning=str(data.get("macro_reasoning", "Autonomous LLM reasoning")),
            confidence=float(data.get("confidence", 0.85)),
            source="LLM",
        )

    def _heuristic_governor(self, ctx: Dict[str, Any]) -> GovernorDirectives:
        """
        Robust, institutional-grade Bayesian heuristic fallback when no LLM key is configured.
        Adheres to ICT/SMC principles, session killzones, and micro-account protection.
        """
        equity = ctx["equity"]
        dd_pct = ctx["drawdown_pct"]
        hour = ctx["current_hour_utc"]
        is_weekend = ctx["is_weekend"]
        open_count = ctx["open_position_count"]
        symbols = ctx["symbols"]

        # 1. Base Posture Determination
        if dd_pct >= 3.0 or (open_count >= 2 and ctx["floating_pnl"] < -5.0):
            posture = "DEFENSIVE"
            risk_mult = 0.65
            max_conc = 1
            reason = f"Defensive posture engaged: drawdown at {dd_pct:.1f}% on small account (${equity:.2f})."
        elif dd_pct >= 1.5:
            posture = "DEFENSIVE"
            risk_mult = 0.80
            max_conc = 1
            reason = f"Moderate risk damping: drawdown at {dd_pct:.1f}%."
        elif not is_weekend and (7 <= hour <= 16):
            # Prime London / NY killzone window
            posture = "NORMAL"
            risk_mult = 1.00
            max_conc = 2 if equity >= 350.0 else 1
            reason = f"Prime institutional session ({hour:02d}:00 UTC). Normal risk allocation active."
        else:
            posture = "NORMAL"
            risk_mult = 0.85
            max_conc = 1
            reason = f"Off-peak liquidity hours ({hour:02d}:00 UTC). Micro risk damped to 0.85x."

        # 2. Per-Symbol Directives
        sym_directives: Dict[str, SymbolDirective] = {}
        for sym, s_data in symbols.items():
            canonical = sym.upper()
            regime = s_data.get("regime", "RANGE")
            spread_ratio = s_data.get("spread_ratio", 1.0)
            is_crypto = any(c in canonical for c in ("BTC", "ETH", "SOL", "CRYPTO"))
            is_gold = any(g in canonical for g in ("XAU", "GOLD"))

            # Spread blowout protection
            if spread_ratio > 1.80:
                sym_directives[sym] = SymbolDirective(
                    symbol=sym,
                    status="PAUSED",
                    preferred_strategy="RANGE_MEAN_REVERSION",
                    reason=f"Spread spike detected ({spread_ratio:.1f}x typical spread). Entries halted.",
                )
                continue

            # Weekend governance
            if is_weekend:
                if is_crypto:
                    sym_directives[sym] = SymbolDirective(
                        symbol=sym,
                        status="THROTTLED",
                        preferred_strategy="RANGE_MEAN_REVERSION",
                        reason="Weekend crypto regime: lower volume and mean-reverting chop. Breakouts banned.",
                    )
                else:
                    sym_directives[sym] = SymbolDirective(
                        symbol=sym,
                        status="PAUSED",
                        preferred_strategy="NONE",
                        reason="Traditional market closed on weekend.",
                    )
                continue

            # Weekday Session Governance
            if is_gold:
                # Prime Gold hours: 07:00 to 19:00 UTC
                if 7 <= hour <= 18:
                    sym_directives[sym] = SymbolDirective(
                        symbol=sym,
                        status="ENABLED",
                        preferred_strategy="LIQUIDITY_SWEEP_REVERSAL" if "RANGE" in regime else "CHOCH_STRUCTURAL_REVERSAL",
                        reason="Prime London/NY liquidity session for Gold. Clean institutional displacement.",
                    )
                else:
                    sym_directives[sym] = SymbolDirective(
                        symbol=sym,
                        status="THROTTLED",
                        preferred_strategy="RANGE_MEAN_REVERSION",
                        reason="Off-hours Asian session or Rollover window. Gold entries throttled with reduced risk.",
                    )
            elif is_crypto:
                if 12 <= hour <= 20:
                    sym_directives[sym] = SymbolDirective(
                        symbol=sym,
                        status="ENABLED",
                        preferred_strategy="TREND_FOLLOWING" if "TREND" in regime else "TREND_PULLBACK",
                        reason="US cash equity open: maximum crypto trend persistence and CVD volume.",
                    )
                else:
                    sym_directives[sym] = SymbolDirective(
                        symbol=sym,
                        status="ENABLED",
                        preferred_strategy="TREND_PULLBACK",
                        reason="Normal crypto session. Requiring discount/premium pullback before entry.",
                    )
            else:
                # Forex Majors, Indices, Commodities
                is_asian_native = any(c in canonical for c in ("JPY", "AUD", "NZD"))
                is_index = any(c in canonical for c in ("US30", "NAS100", "US500", "GER40", "UK100"))
                if is_asian_native and (0 <= hour <= 9):
                    sym_directives[sym] = SymbolDirective(
                        symbol=sym,
                        status="ENABLED",
                        preferred_strategy="LIQUIDITY_SWEEP_REVERSAL" if hour <= 4 else "TREND_PULLBACK",
                        reason="Tokyo/Sydney session open for Asia-Pacific currency. Active institutional order flow.",
                    )
                elif 7 <= hour <= 21:
                    sym_directives[sym] = SymbolDirective(
                        symbol=sym,
                        status="ENABLED",
                        preferred_strategy="LIQUIDITY_SWEEP_REVERSAL" if hour <= 9 else ("BREAKOUT_EXPANSION" if is_index and 13 <= hour <= 17 else "TREND_PULLBACK"),
                        reason="London/NY overlap killzone. High volume and institutional expansion active.",
                    )
                else:
                    sym_directives[sym] = SymbolDirective(
                        symbol=sym,
                        status="THROTTLED",
                        preferred_strategy="RANGE_MEAN_REVERSION",
                        reason="Off-peak session. Throttled risk to avoid low-liquidity chop.",
                    )

        # Micro-account hard constraint
        if equity < 500.0:
            max_conc = min(2, max_conc)
            risk_mult = min(1.10, risk_mult)

        return GovernorDirectives(
            risk_posture=posture,
            risk_multiplier=round(risk_mult, 2),
            max_concurrent_trades=max_conc,
            symbol_directives=sym_directives,
            macro_reasoning=reason,
            confidence=0.88,
            source="OFFLINE_HEURISTIC",
        )
