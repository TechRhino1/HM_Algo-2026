"""The deterministic state snapshot.
Everything computable stays strictly in Python. This module never calls AI models.
It turns book quotes, recent trades/bars, and internal bookkeeping into one
compact dict under ~400 tokens with strict timestamp discipline.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any


def _pct_return(
    prices: list[tuple[float, float]], now: float, lookback_s: float
) -> float | None:
    """Return over the last lookback_s seconds using only points before `now`."""
    past = [p for ts, p in prices if ts <= now - lookback_s]
    current = [p for ts, p in prices if ts <= now]
    if not past or not current:
        return None
    base = past[-1]
    latest = current[-1]
    if base == 0:
        return None
    return round((latest - base) / base, 6)


def _realised_vol(
    prices: list[tuple[float, float]],
    now: float,
    window_s: float,
    step_s: float = 60.0,
) -> float | None:
    """Realised volatility: stdev of log returns on a fixed `step_s` grid inside the window."""
    pts = sorted((ts, p) for ts, p in prices if ts <= now and p > 0)
    if not pts:
        return None
    n_steps = int(window_s // step_s)
    grid = [now - k * step_s for k in range(n_steps, -1, -1)]
    sampled = []
    j = 0
    last = None
    for g in grid:
        while j < len(pts) and pts[j][0] <= g:
            last = pts[j][1]
            j += 1
        if last is not None:
            sampled.append(last)
    if len(sampled) < 3:
        return None
    rets = [math.log(b / a) for a, b in zip(sampled, sampled[1:])]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(max(var, 0.0)), 6)


@dataclass
class InventoryState:
    """Internal inventory and execution bookkeeping, carried tick to tick."""
    inventory: float = 0.0
    entry_price: float = 0.0
    position_opened_at: float | None = None
    realised_pnl_usd: float = 0.0
    high_water_mark_usd: float = 0.0
    equity_usd: float = 0.0
    fills: int = 0
    orders_submitted: int = 0
    orders_rejected: int = 0
    api_error_streak: int = 0
    recent_latencies_ms: list[float] = field(default_factory=list)
    recent_slippage_bps: list[float] = field(default_factory=list)
    # Session VWAP accumulators
    vwap_cum_pv: float = 0.0
    vwap_cum_vol: float = 0.0
    vwap_last_trade_ts: float = 0.0

    def update_vwap(self, trades: list[tuple[float, float, float]]) -> None:
        """Fold newly seen trades (ts, price, size) into running VWAP."""
        newest_ts = self.vwap_last_trade_ts
        for ts, price, size in trades:
            if ts <= self.vwap_last_trade_ts:
                continue
            if price <= 0 or size <= 0:
                continue
            self.vwap_cum_pv += price * size
            self.vwap_cum_vol += size
            newest_ts = max(newest_ts, ts)
        self.vwap_last_trade_ts = newest_ts

    def record_slippage(self, expected_price: float, fill_price: float, side: str) -> None:
        """Signed slippage in basis points: positive means worse execution."""
        if expected_price <= 0:
            return
        sign = 1.0 if side.lower() in ("buy", "long") else -1.0
        bps = sign * (fill_price - expected_price) / expected_price * 10_000
        self.recent_slippage_bps.append(round(bps, 2))
        self.recent_slippage_bps = self.recent_slippage_bps[-10:]

    def apply_fill(self, side: str, qty: float, price: float, ts: float) -> None:
        """Update inventory, average entry price, and realized P&L upon a fill."""
        if qty <= 0 or price <= 0:
            return
        signed = qty if side.lower() in ("buy", "long") else -qty
        old = self.inventory
        new = old + signed
        if old == 0 or (old > 0) == (signed > 0):
            self.entry_price = (
                (self.entry_price * abs(old) + price * qty) / abs(new) if new else 0.0
            )
            if old == 0:
                self.position_opened_at = ts
        else:
            closed = min(abs(old), qty)
            direction = 1.0 if old > 0 else -1.0
            self.realised_pnl_usd += (price - self.entry_price) * closed * direction
            if abs(new) < 1e-12:
                new = 0.0
                self.entry_price = 0.0
                self.position_opened_at = None
            elif (new > 0) != (old > 0):
                self.entry_price = price
                self.position_opened_at = ts
        self.inventory = new
        self.fills += 1


def build_state_snapshot(
    *,
    as_of: float,
    mid: float,
    microprice: float,
    spread_bps: float,
    bid_depth: list[tuple[float, float]],
    ask_depth: list[tuple[float, float]],
    trade_prices: list[tuple[float, float]],
    trade_sides: list[tuple[float, str]],
    inv: InventoryState,
    data_timestamp: float,
    has_depth: bool = True,
    point_value: float = 1.0,
) -> dict[str, Any]:
    """Assemble the deterministic snapshot under ~400 tokens."""
    bid_sz = sum(sz for _, sz in bid_depth[:3])
    ask_sz = sum(sz for _, sz in ask_depth[:3])
    if bid_sz + ask_sz > 0:
        imbalance = round((bid_sz - ask_sz) / (bid_sz + ask_sz), 4)
    else:
        imbalance = None

    recent_trades = [(ts, side) for ts, side in trade_sides if ts <= as_of]
    window_trades = [s for ts, s in recent_trades if ts >= as_of - 30.0]
    buys = sum(1 for s in window_trades if s.lower() in ("buy", "long"))
    aggressive_buy_ratio = buys / len(window_trades) if window_trades else 0.5
    trade_intensity = len(window_trades) / 30.0

    unrealised_pnl = (
        (mid - inv.entry_price) * inv.inventory * point_value if inv.inventory else 0.0
    )
    equity = inv.equity_usd + inv.realised_pnl_usd + unrealised_pnl
    peak = max(inv.high_water_mark_usd, equity)
    drawdown_pct = (peak - equity) / peak if peak > 0 else 0.0

    position_age_s = (
        (as_of - inv.position_opened_at)
        if (inv.inventory and inv.position_opened_at)
        else 0.0
    )
    fill_ratio = (
        min(1.0, inv.fills / inv.orders_submitted) if inv.orders_submitted else 1.0
    )
    data_age_s = max(0.0, as_of - data_timestamp)
    vwap = inv.vwap_cum_pv / inv.vwap_cum_vol if inv.vwap_cum_vol > 0 else mid

    return {
        "as_of": round(as_of, 3),
        # PRICE
        "mid": round(mid, 5),
        "microprice": round(microprice, 5),
        "vwap": round(vwap, 5),
        "return_1m": _pct_return(trade_prices, as_of, 60),
        "return_5m": _pct_return(trade_prices, as_of, 300),
        "return_30m": _pct_return(trade_prices, as_of, 1800),
        # BOOK
        "spread_bps": round(spread_bps, 2),
        "has_depth": has_depth,
        "depth_levels_available": (
            min(len(bid_depth), len(ask_depth)) if has_depth else 0
        ),
        "bid_depth_3": [[round(p, 5), round(s, 2)] for p, s in bid_depth[:3]],
        "ask_depth_3": [[round(p, 5), round(s, 2)] for p, s in ask_depth[:3]],
        "imbalance": imbalance,
        # FLOW
        "aggressive_buy_ratio": round(aggressive_buy_ratio, 4),
        "trade_intensity_per_s": round(trade_intensity, 4),
        # VOLATILITY
        "realised_vol_short": _realised_vol(trade_prices, as_of, 300),
        "realised_vol_medium": _realised_vol(trade_prices, as_of, 1800),
        # PNL & EXPOSURE
        "inventory": round(inv.inventory, 4),
        "unrealised_pnl_usd": round(unrealised_pnl, 2),
        "daily_loss_usd": round(max(0.0, -inv.realised_pnl_usd - unrealised_pnl), 2),
        "drawdown_pct": round(drawdown_pct, 5),
        "position_age_s": round(position_age_s, 1),
        # SYSTEM HEALTH
        "fill_ratio": round(fill_ratio, 4),
        "reject_count": inv.orders_rejected,
        "last_10_latencies_ms": inv.recent_latencies_ms[-10:],
        "last_10_slippage_bps": inv.recent_slippage_bps[-10:],
        "data_age_s": round(data_age_s, 3),
        "leverage": 1.0,
    }


def approx_token_count(snapshot: dict) -> int:
    """Rough token estimate (~4 chars per token)."""
    return len(json.dumps(snapshot, default=str)) // 4
