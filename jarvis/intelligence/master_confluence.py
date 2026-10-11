"""
HM Algo 2.0 — Master Confluence Engine.
Synthesizes PROVEN combinations from top trading masters — the highest win-rate
stacks documented over 100 years, verified by backtests and audited track records.
Sources researched (2026):
- Wyckoff + ICT/SMC fusion: "strongest approach that exists"
- Mark Minervini SEPA + VCP: 8-point Trend Template + VCP, audited 255% and 334.8%
- ICT Core 5: Market Structure + Liquidity + FVG + Kill Zones + Displacement
- ICT Silver Bullet: sweep + displacement + FVG inside Kill Zone — 50-65% win
- Triple Confluence: Breaker Block + FVG + HTF Order Block — "probability is absurd"
"""
from typing import Dict, Any
import logging
import pandas as pd
from jarvis.market.fair_value_gap import FairValueGapEngine
from jarvis.market.sessions import SessionEngine

logger = logging.getLogger("JARVIS_MasterConfluence")

class MasterConfluenceEngine:
    def __init__(self):
        self.fvg_engine = FairValueGapEngine()

    @staticmethod
    def _regime_name(regime: Any) -> str:
        """Upper-cased regime label, tolerating None, a plain str or an enum.

        The old one-liner reached into ``regime.primary_regime`` directly, so a
        ``regime`` of ``None`` raised — see the note in ``score`` for why that
        mattered far more than it looks.
        """
        raw = getattr(regime, "primary_regime", "RANGE")
        if not isinstance(raw, str):
            raw = getattr(raw, "value", "RANGE")
        return str(raw).upper()

    @staticmethod
    def _primary_frame(mtf_data: Any):
        """The primary DataFrame from mtf_data, or any non-empty one."""
        if not isinstance(mtf_data, dict):
            return None
        df = mtf_data.get("primary")
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
        for v in mtf_data.values():
            if isinstance(v, pd.DataFrame) and not v.empty:
                return v
        return None

    def score(self, context, regime, rr_ratio: float, ai_score: float, mtf_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Score the five confluence stacks, 0-20 each, 100 total.

        ``ai_score`` is accepted for signature compatibility with the other
        scorers and is not part of any stack here.
        """
        breakdown: Dict[str, float] = {}
        details: Dict[str, str] = {}

        # Resolved once, *outside* the per-block try/excepts. `reg`, `st`, `liq`,
        # `mo`, `vol` and `df` are each read by more than one block; binding them
        # inside the first try meant that one failure there (an unreadable
        # `regime`, most likely) left them unbound, and every later block then
        # died with a NameError that its own handler swallowed. A single bad
        # input silently scored 0/100 — WEAK — and `decision_engine` gates the
        # trade on this number, so it blocked the order rather than reporting a
        # fault. Now a failure costs one component, not all five.
        reg = self._regime_name(regime)
        st = getattr(context, "structure", None)
        liq = getattr(context, "liquidity", None)
        mo = getattr(context, "momentum", None)
        vol = getattr(context, "volatility", None)
        sess = getattr(context, "session", None)
        pa = getattr(context, "candlestick_pattern", {}) or {}
        df = self._primary_frame(mtf_data)

        try:
            wyck = 0
            if "TREND_BULL" in reg and getattr(liq, "sweep_detected", False):
                wyck += 8
                details["wyckoff"] = "TREND_BULL + sweep (Wyckoff Spring)"
            elif "TREND_BEAR" in reg and getattr(liq, "sweep_detected", False):
                wyck += 8
                details["wyckoff"] = "TREND_BEAR + sweep (Wyckoff Upthrust)"
            elif "RANGE" in reg:
                wyck += 4
                details["wyckoff"] = "RANGE — Wyckoff consolidation"
            if getattr(st, "bos", False): wyck += 6
            if getattr(st, "choch", False): wyck += 4
            if getattr(st, "bias", "NEUTRAL") != "NEUTRAL": wyck += 2
            
            # Candlestick Price Action & VSA Confirmation
            if pa.get("pattern_detected", False):
                p_name = pa.get("pattern_name", "")
                p_conf = float(pa.get("confidence", 0.0))
                p_vol = float(pa.get("volume_ratio", 1.0))
                if pa.get("volume_confirmed", False) or p_vol >= 1.3:
                    wyck += 4
                else:
                    wyck += 2
                details["candlestick"] = f"{p_name} (conf={p_conf:.2f}, vol={p_vol:.1f}x)"
        except Exception:
            logger.warning("master_confluence: wyckoff/ICT scoring failed; scored 0", exc_info=True)
            wyck = 0
        wyck = min(20, wyck)
        breakdown["wyckoff_ict_fusion"] = wyck
        try:
            ts = float(getattr(mo, "trend_score", 0) or 0)
            adx = float(getattr(mo, "adx", 0) or 0)
            trend = 0
            if ts > 0 and adx >= 20: trend += 8
            elif ts > 0: trend += 4
            if adx >= 25: trend += 4
            elif adx >= 15: trend += 2
            if "TREND_BULL" in reg and ts >= 20: trend += 4
            elif "TREND_BEAR" in reg and ts <= -20: trend += 4
            if getattr(st, "bos", False) and abs(ts) >= 20: trend += 4
        except Exception:
            logger.warning("master_confluence: Minervini trend template failed; scored 0", exc_info=True)
            trend = 0
        trend = min(20, trend)
        breakdown["minervini_trend_template"] = trend
        try:
            vcp = 0
            if df is not None and len(df) >= 30:
                closes = df["close"].values[-30:]
                ranges = []
                for i in range(3):
                    seg = closes[i*10:(i+1)*10]
                    ranges.append(float(max(seg) - min(seg)))
                if len(ranges) == 3 and ranges[0] > 0:
                    if ranges[1] < ranges[0] * 0.85 and ranges[2] < ranges[1] * 0.85:
                        vcp += 12
                        details["vcp"] = f"VCP {ranges[0]:.2f}→{ranges[1]:.2f}→{ranges[2]:.2f}"
                    elif ranges[1] < ranges[0] and ranges[2] < ranges[1]:
                        vcp += 6
                        details["vcp"] = f"soft VCP {ranges[0]:.2f}→{ranges[1]:.2f}→{ranges[2]:.2f}"
                    if vcp > 0 and getattr(st, "bos", False): vcp += 4
                    if vcp > 0 and rr_ratio >= 2.0: vcp += 4
            if vcp == 0 and vol and str(getattr(vol, "state", "")) == "COMPRESSION":
                vcp += 6
                details["vcp"] = "COMPRESSION proxy for VCP"
        except Exception:
            logger.warning("master_confluence: VCP scoring failed; scored 0", exc_info=True)
            vcp = 0
        vcp = min(20, vcp)
        breakdown["vcp"] = vcp
        try:
            hour = int(getattr(sess, "utc_hour", 12) or 12)
            is_prime = bool(getattr(sess, "is_prime_session", False))
            kz_info = SessionEngine.get_active_killzone(getattr(context, "timestamp", None))
            kill = 0
            if kz_info.get("is_in_killzone", False):
                kill += 10
                details["killzone"] = f"active killzone={kz_info.get('active_killzone')}"
            elif is_prime:
                kill += 6
                details["killzone"] = f"prime hour={hour}"

            disp = False
            if mo and float(getattr(mo, "adx", 0) or 0) >= 22 and getattr(st, "bos", False):
                disp = True
            if getattr(liq, "sweep_detected", False) and disp:
                kill += 8
        except Exception:
            logger.warning("master_confluence: killzone/AMD scoring failed; scored 0", exc_info=True)
            kill = 0
        kill = min(20, kill)
        breakdown["ict_killzone_amd"] = kill
        try:
            triple = 0
            has_fvg = False
            has_ob = False
            has_breaker = False
            
            # Use dedicated FairValueGapEngine if primary dataframe is available
            fvg_res = None
            if df is not None and len(df) >= 10:
                try:
                    fvg_res = self.fvg_engine.analyze(df)
                except Exception:
                    fvg_res = None
                    
            if fvg_res:
                if fvg_res.get("price_in_fvg") or fvg_res.get("active_bullish_fvg") or fvg_res.get("active_bearish_fvg"):
                    has_fvg = True
                    triple += 7
                if fvg_res.get("price_in_ob") or fvg_res.get("active_bullish_ob") or fvg_res.get("active_bearish_ob"):
                    has_ob = True
                    triple += 7
                if fvg_res.get("fvg_ob_confluence", False):
                    triple += 5
                    details["fvg_ob_confluence"] = "FVG + OB overlapping confluence zone"
            elif st:
                fvgs = getattr(st, "fair_value_gaps", []) or []
                if len(fvgs) > 0: has_fvg = True; triple += 7
                obs = getattr(st, "order_blocks", []) or []
                if len(obs) > 0: has_ob = True; triple += 7

            if getattr(st, "bos", False) and getattr(st, "choch", False): has_breaker = True; triple += 6
            elif getattr(st, "bos", False): triple += 3
            
            mtf_align = getattr(context, "mtf_alignment", {}) or {}
            bullish = sum(1 for v in mtf_align.values() if v == "BULLISH")
            bearish = sum(1 for v in mtf_align.values() if v == "BEARISH")
            if max(bullish, bearish) >= 3 and (has_fvg or has_ob): triple += 4
            if pa.get("at_support_resistance", False):
                triple += 3
                details["sr_proximity"] = f"near {pa.get('key_level_type')} @ {pa.get('nearest_level')}"
            if has_fvg and has_ob and has_breaker: triple = 20
        except Exception:
            logger.warning("master_confluence: triple confluence scoring failed; scored 0", exc_info=True)
            triple = 0
        triple = min(20, triple)
        breakdown["triple_confluence"] = triple
        total = sum(breakdown.values())
        if total >= 70: tier, boost = "ELITE", 0.05
        elif total >= 55: tier, boost = "HIGH", 0.03
        elif total >= 40: tier, boost = "MODERATE", 0.015
        elif total >= 25: tier, boost = "LOW", 0.00
        else: tier, boost = "WEAK", 0.00
        return {"breakdown": breakdown, "total": total, "tier": tier, "prob_boost": boost, "details": details}
