"""Avellaneda-Stoikov Microstructure & Limit Order Pricing Engine.
Computes the reservation price and half-spread based on inventory, volatility, and arrival rate.
Enables HM Algo 2.0 to take limit order entries that fade toxic flow and minimize slippage.

Formulas:
  Reservation price: r = mid - q * gamma * sigma^2 * (T - t)
  Half spread:       delta = gamma * sigma^2 * (T - t) + (2 / gamma) * ln(1 + gamma / kappa)
"""
from __future__ import annotations

import math
from typing import Any


def reservation_price(
    mid: float,
    inventory: float,
    gamma: float = 0.10,
    sigma: float = 0.001,
    time_left_s: float = 60.0,
) -> float:
    """Computes Avellaneda-Stoikov reservation price skewed by inventory.
    A positive inventory (long) pulls reservation price down to incentivize selling.
    A negative inventory (short) pulls reservation price up to incentivize buying.
    """
    if mid <= 0:
        return 0.0
    return mid - inventory * gamma * (sigma**2) * time_left_s


def half_spread(
    gamma: float = 0.10,
    sigma: float = 0.001,
    time_left_s: float = 60.0,
    kappa: float = 1.5,
) -> float:
    """Half the total quoted spread around reservation price."""
    inventory_term = gamma * (sigma**2) * time_left_s
    liquidity_term = (2.0 / gamma) * math.log1p(gamma / kappa) if kappa > 0 else 0.0
    return inventory_term + liquidity_term


def optimal_quote_prices(
    mid: float,
    inventory: float = 0.0,
    sigma: float = 0.001,
    gamma: float = 0.10,
    kappa: float = 1.5,
    time_left_s: float = 60.0,
    tick_size: float = 0.01,
) -> tuple[float, float]:
    """Returns (optimal_bid, optimal_ask) aligned to tick_size."""
    r = reservation_price(mid, inventory, gamma, sigma, time_left_s)
    h = half_spread(gamma, sigma, time_left_s, kappa)
    bid = r - h
    ask = r + h
    if tick_size > 0:
        bid = round(math.floor(bid / tick_size) * tick_size, 6)
        ask = round(math.ceil(ask / tick_size) * tick_size, 6)
    return bid, ask


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
) -> dict[str, Any]:
    """Calculates optimal limit entry price for a BUY or SELL order.
    Returns optimal entry price, reservation price, and expected price improvement vs market.
    """
    is_buy = side.upper() in ("BUY", "LONG")
    r = reservation_price(mid, inventory, gamma, sigma, time_left_s)
    h = half_spread(gamma, sigma, time_left_s, kappa)

    # Peg to reservation price boundary inside the spread
    half_market_spread = (spread_pips * pip_size) / 2.0
    if is_buy:
        # Limit buy: placed at or slightly above r - h, but capped below mid + half_market_spread
        limit_price = min(mid, max(r - h, mid - half_market_spread))
        expected_savings = max(0.0, (mid + half_market_spread) - limit_price)
    else:
        # Limit sell: placed at or slightly below r + h, but above mid - half_market_spread
        limit_price = max(mid, min(r + h, mid + half_market_spread))
        expected_savings = max(0.0, limit_price - (mid - half_market_spread))

    return {
        "mid": mid,
        "side": "BUY" if is_buy else "SELL",
        "reservation_price": round(r, 6),
        "half_spread": round(h, 6),
        "optimal_limit_price": round(limit_price, 6),
        "expected_savings": round(expected_savings, 6),
    }
