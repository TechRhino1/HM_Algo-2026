"""Avellaneda-Stoikov Microstructure & Limit Order Pricing Engine.
Computes the reservation price and half-spread based on inventory, volatility, and arrival rate.
Enables HM Algo 2.0 to take limit order entries that fade toxic flow and minimize slippage.

Scale-Invariant Implementation:
Operates in dimensionless relative return space (ATR / mid) so quotes scale
uniformly across Forex (EURUSD), Metals (XAUUSD), Indices (NAS100), and Crypto (BTCUSD).
"""
from __future__ import annotations

import math
from typing import Any, Tuple


class AvellanedaStoikovPricer:
    """
    Scale-Invariant Avellaneda-Stoikov Limit Order Microstructure Pricer.
    Operates in dimensionless relative return space (ATR / mid) so quotes scale
    uniformly across Forex (EURUSD), Metals (XAUUSD), Indices (NAS100), and Crypto (BTCUSD).
    """

    def __init__(self, gamma: float = 0.15, base_kappa: float = 1.5):
        self.gamma = max(gamma, 1e-4)
        self.base_kappa = max(base_kappa, 1e-4)

    def dynamic_kappa(self, current_spread: float, median_spread: float) -> float:
        """Higher kappa = denser order book = tighter limit offset."""
        if current_spread <= 0 or median_spread <= 0:
            return self.base_kappa
        spread_ratio = median_spread / max(current_spread, 1e-9)
        return max(0.35, min(5.0, self.base_kappa * spread_ratio))

    def optimal_quotes(
        self,
        mid_price: float,
        atr: float,
        inventory_q: float = 0.0,
        horizon_fraction: float = 0.25,
        current_spread: float = 0.0,
        median_spread: float = 0.0,
    ) -> Tuple[float, float, float]:
        """
        Returns (reservation_price, optimal_buy_limit, optimal_sell_limit).
        `inventory_q` in [-1.0, +1.0] represents normalized directional exposure.
        """
        if mid_price <= 0 or atr <= 0:
            return mid_price, mid_price, mid_price

        # Dimensionless volatility normalized to mid_price
        sigma_rel = atr / mid_price
        var_rel = sigma_rel * sigma_rel
        tau = max(0.01, min(1.0, horizon_fraction))

        kappa = self.dynamic_kappa(current_spread, median_spread)

        # Reservation shift in relative terms, scaled back by mid_price
        r_shift = inventory_q * self.gamma * var_rel * tau * mid_price
        reservation_px = mid_price - r_shift

        # Optimal half-spread in ATR-scaled units
        risk_term = 0.5 * self.gamma * sigma_rel * tau * atr
        liquidity_term = (1.0 / (self.gamma * 10.0)) * math.log(1.0 + (self.gamma / kappa)) * atr
        half_spread_px = max(atr * 0.15, min(atr * 0.85, risk_term + liquidity_term))

        optimal_buy_limit = reservation_px - half_spread_px
        optimal_sell_limit = reservation_px + half_spread_px
        return reservation_px, optimal_buy_limit, optimal_sell_limit


def reservation_price(
    mid: float,
    inventory: float = 0.0,
    gamma: float = 0.10,
    sigma: float = 0.001,
    time_left_s: float = 60.0,
) -> float:
    """Computes Avellaneda-Stoikov reservation price skewed by inventory."""
    if mid <= 0:
        return 0.0
    tau = max(0.01, min(1.0, time_left_s / 300.0))
    return mid - (inventory * gamma * (sigma ** 2) * tau * mid)


def half_spread(
    gamma: float = 0.10,
    sigma: float = 0.001,
    time_left_s: float = 60.0,
    kappa: float = 1.5,
    atr: float = 0.0,
    mid: float = 1.0,
) -> float:
    """Half the total quoted spread around reservation price in price units."""
    base_atr = atr if atr > 0 else (mid * sigma)
    tau = max(0.01, min(1.0, time_left_s / 300.0))
    risk_term = 0.5 * gamma * (sigma ** 2) * tau * base_atr
    liq_term = (1.0 / max(gamma * 10.0, 1e-4)) * math.log1p(gamma / max(kappa, 1e-4)) * base_atr
    return max(base_atr * 0.10, min(base_atr * 0.85, risk_term + liq_term))


def compute_as_limit_entry(
    mid: float,
    side: str,
    inventory: float = 0.0,
    sigma: float = 0.001,
    spread_pips: float = 1.0,
    pip_size: float = 0.0001,
    gamma: float = 0.10,
    kappa: float = 1.5,
    time_left_s: float = 60.0,
    atr: float = 0.0,
) -> dict[str, Any]:
    """Calculates optimal scale-invariant limit entry price for a BUY or SELL order.
    Returns optimal entry price, reservation price, and expected price improvement vs market.
    """
    is_buy = side.upper() in ("BUY", "LONG")
    pricer = AvellanedaStoikovPricer(gamma=gamma, base_kappa=kappa)
    effective_atr = atr if atr > 0 else (mid * sigma if sigma > 0 else pip_size * 20.0)
    current_spread = spread_pips * pip_size
    median_spread = pip_size * 2.0

    r_px, buy_px, sell_px = pricer.optimal_quotes(
        mid_price=mid,
        atr=effective_atr,
        inventory_q=inventory,
        horizon_fraction=max(0.05, min(1.0, time_left_s / 300.0)),
        current_spread=current_spread,
        median_spread=median_spread,
    )

    half_mkt = current_spread / 2.0
    if is_buy:
        limit_price = min(mid, max(buy_px, mid - (effective_atr * 0.50)))
        expected_savings = max(0.0, (mid + half_mkt) - limit_price)
    else:
        limit_price = max(mid, min(sell_px, mid + (effective_atr * 0.50)))
        expected_savings = max(0.0, limit_price - (mid - half_mkt))

    return {
        "mid": mid,
        "side": "BUY" if is_buy else "SELL",
        "reservation_price": round(r_px, 6),
        "half_spread": round(abs(r_px - (buy_px if is_buy else sell_px)), 6),
        "optimal_limit_price": round(limit_price, 6),
        "expected_savings": round(expected_savings, 6),
    }
