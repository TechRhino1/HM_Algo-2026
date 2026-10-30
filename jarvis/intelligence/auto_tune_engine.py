"""
HM Algo 2.0 — Master Trader AI Autonomous Universe Auto-Tune Engine.
Evaluates the entire multi-asset universe across four selectable strategy profiles:
1. BALANCED (Optimal Edge - Default)
2. CONSERVATIVE (Grade A+ / Highest Edge Only)
3. AGGRESSIVE (High Coverage & Broad Discovery)
4. SESSION_ADAPTIVE (Time-of-Day Global Flow Alignment)

Takes into account live market open/closed sessions, spread friction defense,
currency correlation caps, setup quality grades, and master confluence scores.
"""
from __future__ import annotations

import time
import logging
from typing import Dict, List, Set, Any, Optional, Tuple

from jarvis.data.symbol_registry import _REGISTRY, resolve as resolve_symbol, is_crypto
from jarvis.market.sessions import SessionEngine
from jarvis.market.data_feed import _is_weekend_gap

logger = logging.getLogger("JARVIS_AutoTuneEngine")


class AutoTuneEngine:
    """
    Autonomous Universe Auto-Tune Engine for Master Trader AI Symbol Control Center.
    Calculates the mathematically optimal active trading universe based on live market conditions.
    """

    # Currency groups for correlation risk management
    USD_PAIRS = {"EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "NZDUSD", "USDCHF"}
    CRYPTO_PAIRS = {"BTCUSD", "ETHUSD", "SOLUSD"}
    INDEX_PAIRS = {"US500", "GER40", "UK100"}
    COMMODITY_PAIRS = {"XAUUSD", "XAGUSD", "WTI"}

    @classmethod
    def tune_universe(
        cls,
        orchestrator: Any = None,
        state_manager: Any = None,
        mt5_client: Any = None,
        mode: str = "BALANCED",
        asset_class: str = "ALL"
    ) -> Dict[str, Any]:
        """
        Executes multi-factor universe auto-tuning.
        Returns:
            {
                "active_symbols": List[str],
                "active_count": int,
                "summary": str,
                "details": List[Dict[str, Any]],
                "mode": str
            }
        """
        profile = (mode or "BALANCED").strip().upper()
        if profile not in ("BALANCED", "CONSERVATIVE", "AGGRESSIVE", "SESSION_ADAPTIVE"):
            profile = "BALANCED"

        target_class = (asset_class or "ALL").strip().upper()
        now = time.time()
        is_weekend = _is_weekend_gap(now)

        # 1. Gather all symbol data from radar and state manager
        radar_map: Dict[str, Dict[str, Any]] = {}
        if state_manager and hasattr(state_manager, "radar_opportunities"):
            for item in list(state_manager.radar_opportunities or []):
                s = item.get("symbol")
                if s and s not in radar_map:
                    radar_map[s] = item

        latest_decisions = getattr(state_manager, "latest_decisions", {}) or {}

        # 2. Evaluate every registered symbol
        candidates: List[Dict[str, Any]] = []
        for sym_name, spec in _REGISTRY.items():
            sym_upper = sym_name.upper()

            # Filter by asset class if specific asset class was requested
            if target_class != "ALL" and spec.asset_class.upper() != target_class:
                continue

            # Session open status
            mkt_status = SessionEngine.get_market_trading_status(sym_upper)
            is_open = mkt_status.get("is_open", True)
            current_session = mkt_status.get("current_session", "GLOBAL")

            # Check spread & friction
            spread_pips = float(spec.typical_spread_pips or 1.0)
            if mt5_client and hasattr(mt5_client, "get_symbol_trading_spec"):
                try:
                    spec_info = mt5_client.get_symbol_trading_spec(sym_upper)
                    if spec_info:
                        pts = float(spec_info.get("spread") or 0.0)
                        digits = int(spec_info.get("digits") or spec.digits or 5)
                        point = float(spec_info.get("point") or (10 ** -digits))
                        pip_sz = float(spec.pip_size or 0.0001)
                        if pts > 0 and pip_sz > 0:
                            spread_pips = round((pts * point) / pip_sz, 1)
                except Exception:
                    pass

            max_spread = float(spec.max_spread_pips or 5.0)
            is_spread_spiked = spread_pips > (max_spread * 1.4)

            # Radar and setup metrics
            radar_item = radar_map.get(sym_upper, {})
            dec_obj = latest_decisions.get(sym_upper)

            confluence = float(radar_item.get("confluence_score") or getattr(dec_obj, "master_confluence_score", 0.0) or 0.0)
            win_prob = float(radar_item.get("win_prob") or (round(dec_obj.model_confidence * 100.0, 1) if (dec_obj and hasattr(dec_obj, "model_confidence") and dec_obj.model_confidence is not None) else 50.0))
            ev = float(radar_item.get("ev") or getattr(dec_obj, "expected_value", 0.0) or 0.0)
            bias = str(radar_item.get("bias") or (getattr(dec_obj, "bias", None) if dec_obj else "NEUTRAL")).upper()
            regime = str(radar_item.get("regime") or "NORMAL").upper()
            grade = str(radar_item.get("setup_grade") or (radar_item.get("grade") or "B")).upper()

            # Utility score
            utility = float(radar_item.get("utility_score") or 0.0)

            candidates.append({
                "symbol": sym_upper,
                "asset_class": spec.asset_class.upper(),
                "is_open": is_open,
                "is_weekend": is_weekend,
                "is_crypto": is_crypto(sym_upper),
                "current_session": current_session,
                "spread_pips": spread_pips,
                "max_spread": max_spread,
                "is_spread_spiked": is_spread_spiked,
                "confluence": confluence,
                "win_prob": win_prob,
                "ev": ev,
                "bias": bias,
                "regime": regime,
                "grade": grade,
                "utility": utility
            })

        # 3. Apply profile-specific selection rules
        selected_symbols: Set[str] = set()
        tuning_details: List[Dict[str, Any]] = []

        # Rule 0: On weekend gap, non-crypto markets are shut
        if is_weekend:
            for c in candidates:
                if c["is_crypto"]:
                    selected_symbols.add(c["symbol"])
                    tuning_details.append({
                        "symbol": c["symbol"],
                        "status": "ACTIVATED",
                        "reason": f"Weekend 24/7 Crypto Asset (Conf {c['confluence']:.0f})"
                    })
                else:
                    tuning_details.append({
                        "symbol": c["symbol"],
                        "status": "DEACTIVATED",
                        "reason": "Weekend Market Closed (TradFi shut)"
                    })
            summary = f"Weekend Mode: Activated {len(selected_symbols)} 24/7 Crypto symbols; TradFi shut."
            return {
                "active_symbols": sorted(list(selected_symbols)),
                "active_count": len(selected_symbols),
                "summary": summary,
                "details": tuning_details,
                "mode": profile
            }

        # Weekday Selection Logic by Profile:
        if profile == "CONSERVATIVE":
            # Only Grade A/A+ with high confluence, strictly open, normal spread
            # Sort by utility descending
            sorted_cands = sorted(candidates, key=lambda x: (x["confluence"], x["utility"], x["win_prob"]), reverse=True)
            usd_count = 0
            for c in sorted_cands:
                sym = c["symbol"]
                if not c["is_open"]:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "Market session closed"})
                    continue
                if c["is_spread_spiked"]:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": f"Spread friction high ({c['spread_pips']}p > {c['max_spread']}p)"})
                    continue
                if c["grade"] not in ("A+", "A", "GRADE A+", "GRADE A") and c["confluence"] < 55:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": f"Below Conservative quality threshold (Grade {c['grade']}, Conf {c['confluence']:.0f})"})
                    continue
                # Currency correlation cap: max 2 USD pairs
                if sym in cls.USD_PAIRS:
                    if usd_count >= 2:
                        tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "USD correlation concentration cap reached (max 2)"})
                        continue
                    usd_count += 1

                # Cap total symbols to 5
                if len(selected_symbols) >= 5 and "XAU" not in sym:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "Conservative universe cap reached (max 5)"})
                    continue

                selected_symbols.add(sym)
                tuning_details.append({
                    "symbol": sym,
                    "status": "ACTIVATED",
                    "reason": f"Elite Grade {c['grade']} Setup | Conf {c['confluence']:.0f} | WinP {c['win_prob']:.0f}%"
                })

        elif profile == "AGGRESSIVE":
            # Broad coverage: enable anything open with directional bias and normal spread
            for c in candidates:
                sym = c["symbol"]
                if not c["is_open"]:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "Market session closed"})
                    continue
                if c["is_spread_spiked"]:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "Excessive spread friction"})
                    continue
                if any(r in c["regime"] for r in ("CHOP", "COMPRESSION")) and c["confluence"] < 25:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": f"Low-edge choppy compression (Conf {c['confluence']:.0f})"})
                    continue

                selected_symbols.add(sym)
                tuning_details.append({
                    "symbol": sym,
                    "status": "ACTIVATED",
                    "reason": f"Active {c['asset_class']} Setup ({c['bias']} {c['regime']})"
                })

        elif profile == "SESSION_ADAPTIVE":
            # Align with active global session
            # London: EUR, GBP, GER40, UK100, XAUUSD
            # NY: US500, XAUUSD, WTI, USD pairs
            # Asian/Pacific: JPY, AUD, NZD, Crypto
            from datetime import datetime, timezone
            utc_hour = datetime.now(timezone.utc).hour

            for c in candidates:
                sym = c["symbol"]
                if not c["is_open"]:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "Market session closed"})
                    continue
                if c["is_spread_spiked"]:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "Spread spiked"})
                    continue

                is_session_favored = False
                reason_tag = ""

                # Gold is universal priority
                if "XAU" in sym or "GOLD" in sym:
                    is_session_favored = True
                    reason_tag = "Universal Gold Priority"
                elif 7 <= utc_hour < 16:  # London Cash
                    if any(x in sym for x in ("EUR", "GBP", "GER40", "UK100", "USDCHF")):
                        is_session_favored = True
                        reason_tag = "London Core Cash Flow"
                elif 12 <= utc_hour < 21:  # New York Cash
                    if any(x in sym for x in ("US500", "WTI", "USDCAD", "EURUSD", "GBPUSD", "BTC")):
                        is_session_favored = True
                        reason_tag = "New York Core Liquidity"
                else:  # Asian / Pacific
                    if any(x in sym for x in ("JPY", "AUD", "NZD", "BTC", "ETH", "SOL")):
                        is_session_favored = True
                        reason_tag = "Tokyo/Pacific Session Flow"

                if is_session_favored and (c["confluence"] >= 35 or "TREND" in c["regime"]):
                    selected_symbols.add(sym)
                    tuning_details.append({"symbol": sym, "status": "ACTIVATED", "reason": f"{reason_tag} (Conf {c['confluence']:.0f})"})
                else:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": f"Off-peak session or low edge (Conf {c['confluence']:.0f})"})

        else:
            # BALANCED (Default Optimal Institutional Edge)
            # Grade >= B with positive EV, open market, spread within bounds, max 3 USD pairs
            sorted_cands = sorted(candidates, key=lambda x: (x["confluence"], x["utility"], x["win_prob"]), reverse=True)
            usd_count = 0
            for c in sorted_cands:
                sym = c["symbol"]
                if not c["is_open"]:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "Market session closed"})
                    continue
                if c["is_spread_spiked"]:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": f"Spread friction high ({c['spread_pips']}p > {c['max_spread']}p)"})
                    continue

                # Filter out low-confluence chop
                if any(r in c["regime"] for r in ("CHOP", "COMPRESSION", "RANGE")) and c["confluence"] < 35:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": f"Low-confluence range compression ({c['confluence']:.0f})"})
                    continue

                if c["confluence"] < 30 and c["grade"] in ("C", "F", "GRADE C", "GRADE F"):
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": f"Below Balanced threshold (Conf {c['confluence']:.0f})"})
                    continue

                # Currency correlation cap: max 3 USD pairs
                if sym in cls.USD_PAIRS:
                    if usd_count >= 3:
                        tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "USD pair correlation limit reached (max 3)"})
                        continue
                    usd_count += 1

                # Cap total symbols to 8 to avoid margin dilution
                if len(selected_symbols) >= 8 and "XAU" not in sym:
                    tuning_details.append({"symbol": sym, "status": "DEACTIVATED", "reason": "Balanced active universe cap reached (max 8)"})
                    continue

                selected_symbols.add(sym)
                tuning_details.append({
                    "symbol": sym,
                    "status": "ACTIVATED",
                    "reason": f"High Confluence Setup (Grade {c['grade']} | Conf {c['confluence']:.0f} | {c['regime']})"
                })

        # Ensure active universe is never completely empty
        if not selected_symbols:
            # Fallback to liquid anchors
            for anchor in ("XAUUSD", "EURUSD", "BTCUSD"):
                if anchor in _REGISTRY:
                    selected_symbols.add(anchor)
                    tuning_details.append({"symbol": anchor, "status": "ACTIVATED", "reason": "Default liquid anchor guarantee"})

        active_list = sorted(list(selected_symbols))
        summary = f"Auto-Tuned {len(active_list)} active symbols under {profile} profile ({len(active_list)} activated, {len(candidates) - len(active_list)} filtered)."
        logger.info(summary)

        return {
            "active_symbols": active_list,
            "active_count": len(active_list),
            "summary": summary,
            "details": tuning_details,
            "mode": profile
        }
