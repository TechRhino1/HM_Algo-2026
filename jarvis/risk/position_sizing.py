import logging
from typing import Dict, Any

logger = logging.getLogger("JARVIS_PositionSizer")

class PositionSizer:
    """Calculates risk-controlled lot sizes adjusted for symbol contract specifications and account balance."""
    
    @staticmethod
    def calculate_lot_size(
        account_balance: float,
        entry_price: float,
        sl_price: float,
        risk_pct: float,
        symbol_info: Dict[str, Any],
        invalidation_risk_coefficient: float = 1.0,
        atr_ratio: float = 1.0,
        current_drawdown_pct: float = 0.0,
        model_confidence: float = 0.60,
        pattern_sample_size: int = 0,
        portfolio_heat_multiplier: float = 1.0,
        is_second_trade: bool = False,
        target_rr: float = 2.0
    ) -> float:
        risk_distance = abs(entry_price - sl_price)
        if risk_distance <= 0 or account_balance <= 0:
            return 0.0

        # Adjust risk_pct based on volatility and drawdown — softened to preserve profitability
        sym_name = str(symbol_info.get("name", "") if symbol_info else "").upper()
        is_high_vol = any(x in sym_name for x in ["XAU", "GOLD", "BTC", "OIL", "US30", "NAS100"])
        if is_high_vol:
            risk_pct *= 0.92  # Was 0.85 — high-vol assets penalized too harshly

        # --- Volatility targeting (P1-1) -------------------------------------
        # Scale the risk budget so a trade contributes roughly constant volatility
        # whatever the regime. atr_ratio is realised ATR / the symbol's own typical
        # ATR, so it is already normalised per symbol — no fixed pip or percentage
        # thresholds. Replaces the old step rule (`atr_ratio > 1.5 -> *0.85`), which
        # was flat everywhere except above an arbitrary cut-off and in practice never
        # fired at all: no caller passed atr_ratio, so it defaulted to 1.0.
        atr_ratio_safe = min(3.0, max(0.33, float(atr_ratio))) if atr_ratio and atr_ratio > 0 else 1.0
        vol_scalar = max(0.40, min(1.25, 1.0 / atr_ratio_safe))
        risk_pct *= vol_scalar

        # NOTE: Drawdown-based sizing reduction is handled by DrawdownGuard's
        # tier multiplier, which flows in through ``portfolio_heat_multiplier``
        # (combined with portfolio heat).  Do NOT apply a second drawdown penalty
        # here — that caused triple-counting (guard × sizer × heat) and reduced
        # effective risk by ~97%, making all trades impossible on micro accounts.

        # Adaptive Second-Trade position discount: scale to 75% to prevent overconcentration
        if is_second_trade:
            risk_pct *= 0.75

        # Portfolio heat scaling (e.g. 1.0x Normal, 0.75x Moderate, 0.50x High)
        risk_pct *= max(0.25, min(1.0, portfolio_heat_multiplier))

        # Fractional Kelly is computed for reporting but deliberately NOT used for
        # sizing. Quarter-Kelly saturates at its 1.50 cap for every plausible (p, R) —
        # (0.60, 2.0) already yields 10.0 — so it contributed a constant +0.75pp
        # rather than information, and `model_confidence` is not yet calibrated
        # (see P0-1). Sizing off an uncalibrated probability is not defensible.
        p = max(0.20, min(0.95, model_confidence))
        R = max(1.20, min(5.0, float(target_rr)))  # Regime-adaptive payoff ratio
        full_kelly = (p * R - (1.0 - p)) / R
        kelly_pct = (full_kelly / 4.0) * 100.0 if full_kelly > 0 else 0.0

        conviction_factor = max(0.70, min(1.35, (model_confidence / 0.60)))

        # P3: Evidence strength scaling — more rewarding for proven patterns, less punitive for small samples
        if pattern_sample_size >= 30:
            evidence_factor = 1.15  # Was 1.10
        elif pattern_sample_size >= 15:
            evidence_factor = 1.08  # Was 1.05
        elif pattern_sample_size >= 5:
            evidence_factor = 1.02  # Was 1.00
        elif pattern_sample_size >= 3:
            evidence_factor = 0.97  # Was 0.90 — harsh penalty suppressed early learning
        else:
            evidence_factor = 1.00

        combined_scaler = max(0.65, min(1.40, conviction_factor * evidence_factor))

        # The configured ceiling is a real ceiling.
        #
        # This clamped to a HARDCODED literal 1.50 while `max_risk_per_trade_pct`
        # (config/settings.json, default 0.5) is the documented limit -- and it
        # applied `invalidation_risk_coefficient` and `combined_scaler` OUTSIDE
        # the clamp, so a high-conviction trade scaled the 0.5 base by up to
        # ~1.55x and the setting was never actually enforced. Measured on the
        # live book: 27 real trades breached it, the worst risking 39% of equity
        # (78x the limit).
        #
        # The multipliers may now only REDUCE risk below the caller's ceiling,
        # never inflate past it -- which is what "max risk per trade" means.
        # ``RiskEngine`` passes ``max_risk_per_trade_pct`` here, so in production
        # the ceiling IS the configured limit; clamping to the caller's own value
        # keeps this function honest about its contract rather than silently
        # overriding an explicit argument with a hardcoded global.
        #
        # Consequence worth knowing: the micro-account floor further down allows
        # the broker minimum lot whenever the risk it forces is <= 2x the target,
        # so tightening the target also tightens that floor. On a $762 account
        # XAUUSD at a 0.01 minimum lot risks 1.31%, which is under 2x the old
        # inflated 0.776% but over 2x the honest 0.575% -- so such a trade is now
        # refused. That is the correct answer to "you cannot risk 0.5% here",
        # but it does mean small accounts stop trading wide-stop symbols.
        ceiling = float(risk_pct) if risk_pct and risk_pct > 0 else 0.0
        scaled = risk_pct * invalidation_risk_coefficient * combined_scaler
        effective_risk_pct = min(ceiling, max(0.10, scaled)) if ceiling > 0 else max(0.10, scaled)
        if ceiling > 0 and scaled > ceiling + 1e-9:
            logger.debug(
                "[%s] risk clamped: multipliers wanted %.2f%%, ceiling is %.2f%% "
                "(max_risk_per_trade_pct)", sym_name, scaled, ceiling,
            )
        logger.debug(
            f"[{sym_name}] risk target {effective_risk_pct:.2f}% "
            f"(base {risk_pct:.2f}% after vol_scalar {vol_scalar:.2f}; "
            f"kelly {kelly_pct:.2f}% computed but unused)"
        )
        risk_amount_dollars = account_balance * (effective_risk_pct / 100.0)

        from jarvis.data.symbol_registry import get_dollar_risk_per_price_unit
        
        sym_key = sym_name if sym_name else "XAUUSD"
        dollar_risk_per_unit = get_dollar_risk_per_price_unit(sym_key, symbol_info)
        dollar_risk_per_lot = risk_distance * dollar_risk_per_unit

        min_vol = symbol_info.get("volume_min", 0.01) if symbol_info else 0.01
        max_vol = symbol_info.get("volume_max", 100.0) if symbol_info else 100.0
        vol_step = symbol_info.get("volume_step", 0.01) if symbol_info else 0.01

        if dollar_risk_per_lot <= 0:
            return 0.0

        # Precise lot sizing formula across all asset classes & currency quote conventions
        raw_lots = risk_amount_dollars / dollar_risk_per_lot

        # ── Micro Account / Small Balance Handling ───────────────────────────
        # On micro accounts the broker minimum lot (0.01) forces risk above the
        # base 0.5%.  The tiered caps below reflect industry-standard micro-
        # account risk management:
        #   $0   – $250  → up to 5.0% per trade (unavoidable on Gold/BTC at 0.01)
        #   $250 – $1000 → up to 4.0% per trade
        #   $1000– $2500 → up to 3.5% per trade
        #   $2500+       → strict 2× target (typically ≤ 1.0%)
        if raw_lots < min_vol:
            actual_risk_dollars = min_vol * dollar_risk_per_lot
            actual_risk_pct = (actual_risk_dollars / (account_balance + 1e-9)) * 100.0

            # Adaptive micro cap by account size tier
            if account_balance < 250.0:
                tier_cap = 5.0   # $100 account: 0.01 Gold = $10 = 10% → tight SL needed
            elif account_balance < 1000.0:
                tier_cap = 4.0
            elif account_balance < 2500.0:
                tier_cap = 3.5
            else:
                tier_cap = 2.0 * effective_risk_pct  # large accounts: strict

            # For very small accounts, also allow by dollar amount:
            # If the actual dollar risk is small in absolute terms (≤ $15),
            # it's acceptable even if the percentage looks high.
            dollar_floor_ok = (actual_risk_dollars <= 15.0 and account_balance < 500.0)

            max_acceptable_risk_pct = max(tier_cap, 2.0 * effective_risk_pct)
            if actual_risk_pct > max_acceptable_risk_pct and not dollar_floor_ok:
                logger.error(
                    f"REJECTED [{sym_key}]: Minimum lot size ({min_vol}) would force "
                    f"{actual_risk_pct:.2f}% risk (target was {effective_risk_pct:.2f}%, cap was {max_acceptable_risk_pct:.2f}%). "
                    f"Account balance too small for this symbol/stop-distance combination."
                )
                return 0.0
            logger.info(
                f"MICRO ACCOUNT [{sym_key}]: Using minimum lot {min_vol} "
                f"(risk: {actual_risk_pct:.1f}% / ${actual_risk_dollars:.2f} on "
                f"${account_balance:.0f} equity; target was {effective_risk_pct:.2f}%)."
            )
            final_lots = min_vol
        else:
            final_lots = min(raw_lots, max_vol)

        if final_lots <= 0.0:
            return 0.0

        # Volume step rounding (e.g. step = 0.01)
        final_lots = max(min_vol, round(final_lots / vol_step) * vol_step)
        return round(final_lots, 2)

