"""
HM Algo 2.0 — Macroeconomic Event & Directional Shock Analyst Agent.
Features:
- Live Macro News Shock Directional Prediction (USD Bullish/Bearish impact on Gold, FX, Crypto)
- High-Impact Economic Event Blackout Window Management
- Session-Aware Macro Liquidity Bias
"""
import time
from typing import Dict, Any, List, Optional
from jarvis.data.schemas import MarketContext, RegimeOutput, AnalystReport, AnalystRole
from jarvis.analysts.base_analyst import BaseAnalyst


def _parse_metric(value: str) -> Optional[float]:
    """Parse a numeric macro metric, honouring %/K/M/B magnitude suffixes."""
    s = str(value).strip().replace("%", "")
    if not s:
        return None
    mult = 1.0
    if s[-1] in ("K", "k"):
        mult = 1_000.0
        s = s[:-1]
    elif s[-1] in ("M", "m"):
        mult = 1_000_000.0
        s = s[:-1]
    elif s[-1] in ("B", "b"):
        mult = 1_000_000_000.0
        s = s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return None


class MacroAnalyst(BaseAnalyst):
    def __init__(self, news_calendar: Optional[List[Dict[str, Any]]] = None):
        super().__init__(AnalystRole.MACRO)
        # Kept as `None` rather than coalesced to `[]`: `None` means "go and
        # ask the global news engine", an explicit `[]` means "there is no
        # news" and must not trigger a live lookup.
        self.news_calendar = news_calendar

    def analyze(self, context: MarketContext, regime: RegimeOutput) -> AnalystReport:
        t0 = time.perf_counter()
        evidence = []
        risk_factors = []

        score = 65.0
        bias = "NEUTRAL"
        sym = context.symbol.upper()

        # 1. Trading Session Liquidity Assessment
        session = context.session
        if session.is_prime_session:
            score += 15.0
            evidence.append(f"Institutional Prime Session Active ({session.current_session}).")
        else:
            evidence.append(f"Off-hours / Asian liquidity session ({session.current_session}).")
            if session.current_session == "ASIAN":
                risk_factors.append("Asian session low volume; high risk of false breakouts.")
                score -= 10.0

        # 2. Real-Time Macro News Directional Shock Scoring
        if self.news_calendar is None:
            try:
                from jarvis.market.news import GLOBAL_NEWS_ENGINE
                active_news = GLOBAL_NEWS_ENGINE.get_news_calendar()
            except Exception:
                active_news = []
        else:
            active_news = self.news_calendar

        # Provenance gate, BEFORE any scoring. `LiveNewsEngine` falls back to a
        # hardcoded plan when both live feeds fail, and pads a short real feed
        # with it, so `active_news` can contain invented events. Those must not
        # become market facts: the hardcoded calendar carries 5 USD HIGH
        # "Upcoming" entries and each one costs -5.0 below, a constant -25 that
        # carries no information yet moves `ai_score` by -4.2 points -- straight
        # into the hard gate at 70/72/75/78/80/82/85 in `decision_engine`.
        # Measured: MACRO scored 40.0 on the live calendar and 65.0 on no news.
        # Items without the flag are treated as real (back-compatible).
        fabricated = [it for it in active_news if it.get("is_fallback")]
        active_news = [it for it in active_news if not it.get("is_fallback")]
        if fabricated:
            risk_factors.append(
                f"News feed unavailable — {len(fabricated)} of "
                f"{len(fabricated) + len(active_news)} calendar events are "
                "synthetic and were excluded from scoring."
            )

        usd_bull_shock = False
        usd_bear_shock = False

        for item in active_news:
            curr = item.get("currency", "")
            impact = item.get("impact", "")
            actual_str = str(item.get("actual", ""))
            fcst_str = str(item.get("forecast", ""))
            event_name = str(item.get("event", "")).lower()

            if curr == "USD" and impact == "HIGH":
                # Skip events that haven't released yet
                if not actual_str or actual_str in ("Upcoming", "—", "", "Pending"):
                    risk_factors.append(f"⏳ Upcoming HIGH-impact USD event: {item.get('event')} — volatility spike imminent.")
                    score -= 5.0
                    continue

                # ── Recency filter ───────────────────────────────────────────
                # Only events within the last 6 hours should influence
                # directional shock scoring.  Without this, a USD beat from
                # Monday keeps usd_bull_shock=True all week, permanently
                # biasing Gold SELL even when the market has reversed.
                _event_time_str = item.get("datetime") or item.get("time") or ""
                _event_stale = False
                if _event_time_str:
                    try:
                        from datetime import datetime, timezone, timedelta
                        _evt_dt = datetime.fromisoformat(str(_event_time_str).replace("Z", "+00:00"))
                        if _evt_dt.tzinfo is None:
                            _evt_dt = _evt_dt.replace(tzinfo=timezone.utc)
                        _age_hours = (datetime.now(timezone.utc) - _evt_dt).total_seconds() / 3600.0
                        if _age_hours > 6.0:
                            _event_stale = True
                    except Exception:
                        pass  # unparseable timestamp — treat as recent
                if _event_stale:
                    continue

                try:
                    act = _parse_metric(actual_str)
                    fcst = _parse_metric(fcst_str)
                    if act is None or fcst is None:
                        raise ValueError("unparseable macro metric")

                    # Detect inverse indicators where higher actual = weaker USD
                    is_inverse = any(kw in event_name for kw in [
                        "jobless", "unemployment", "trade deficit", "deficit"
                    ])

                    if is_inverse:
                        # Higher actual = weaker economy = bearish USD
                        if act > fcst:
                            usd_bear_shock = True
                            evidence.append(f"Macro Shock: Weaker USD (inverse) ({item.get('event')} {actual_str} vs {fcst_str} fcst).")
                        elif act < fcst:
                            usd_bull_shock = True
                            evidence.append(f"Macro Shock: Stronger USD (inverse) ({item.get('event')} {actual_str} vs {fcst_str} fcst).")
                    else:
                        # Standard indicators: higher actual = stronger USD
                        if act > fcst:
                            usd_bull_shock = True
                            evidence.append(f"Macro Shock: Stronger USD Event ({item.get('event')} {actual_str} vs {fcst_str} fcst).")
                        elif act < fcst:
                            usd_bear_shock = True
                            evidence.append(f"Macro Shock: Weaker USD Event ({item.get('event')} {actual_str} vs {fcst_str} fcst).")
                except Exception:
                    # Cannot parse — do NOT default to any shock direction
                    risk_factors.append(f"⚠️ Unparseable USD event data: {item.get('event')}. No directional assumption made.")
                    continue

        # 3. Directional Bias Mapping for Target Asset
        if any(k in sym for k in ["XAU", "GOLD", "EUR", "GBP", "BTC"]):
            if usd_bull_shock:
                # Strong USD puts heavy downward pressure on Gold & Foreign Currencies
                bias = "BEARISH"
                score += 20.0
                evidence.append(f"Macro Directional Forecast: Strong USD yield pressure triggers institutional SELL bias on {sym}.")
            elif usd_bear_shock:
                bias = "BULLISH"
                score += 20.0
                evidence.append(f"Macro Directional Forecast: Weak USD sentiment triggers institutional BUY impulse on {sym}.")
        elif "USDJPY" in sym or "USDCAD" in sym:
            if usd_bull_shock:
                bias = "BULLISH"
                score += 20.0
                evidence.append(f"Macro Directional Forecast: Strong USD rally triggers BUY bias on {sym}.")
            elif usd_bear_shock:
                bias = "BEARISH"
                score += 20.0
                evidence.append(f"Macro Directional Forecast: Weaker USD triggers SELL bias on {sym}.")

        # 3b. No macro shock: remain NEUTRAL
        # Macro analyst only provides directional vote when genuine economic shocks or events occur.
        if bias == "NEUTRAL" and not usd_bull_shock and not usd_bear_shock:
            evidence.append("Macro: No active macro news shocks detected — remaining NEUTRAL.")

        # 4. Check regime event risk / blackout window
        if regime.primary_regime.value == "EVENT_RISK":
            score = 30.0
            risk_factors.append("High-impact economic event active — wide spreads and slippage expected.")

        final_score = min(100.0, max(0.0, score))
        confidence = min(0.95, max(0.40, final_score / 100.0))
        elapsed = (time.perf_counter() - t0) * 1000.0

        return AnalystReport(
            role=self.role,
            symbol=context.symbol,
            bias=bias,
            score=round(final_score, 1),
            confidence=round(confidence, 2),
            evidence=evidence,
            risk_factors=risk_factors,
            execution_time_ms=round(elapsed, 2),
            metadata={"session": session.current_session, "is_prime": session.is_prime_session, "usd_bull_shock": usd_bull_shock}
        )
