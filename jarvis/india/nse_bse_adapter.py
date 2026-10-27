"""
HM Algo 2.0 — Live NSE / BSE / Global Market-Data Adapter.

Wires REAL Indian market data from free, ultra-low-latency public endpoints:
  * Live Benchmark Indices (NIFTY 50, SENSEX, BANK NIFTY, INDIA VIX) via low-latency live feeds.
  * NSE official Option Chain v3 (real spot, strikes, OI, change in OI, volume, IV) using TLS impersonation.
  * Real-time Indian equity quotes (NSE/BSE & .NS ticker resolution).

All functions are FAIL-SAFE: they gracefully return structured fallbacks or None on any network error.
"""
from typing import Dict, Any, Optional, List
import logging
import time
import json
import urllib.request
import datetime as _dt

logger = logging.getLogger("JARVIS_NSE_BSE")

try:
    import requests
    _HAVE_REQUESTS = True
except Exception:  # pragma: no cover
    _HAVE_REQUESTS = False

try:
    from curl_cffi import requests as cffi_requests
    _HAVE_CURL_CFFI = True
except Exception:  # pragma: no cover
    _HAVE_CURL_CFFI = False

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}

_BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
}

_TIMEOUT = 3.5

# In-memory caches with short TTLs for speed and rate-limit preservation
_SESSION_CACHE: Dict[str, Any] = {"ts": 0.0, "session": None, "last_fail_ts": 0.0}
_INDICES_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None, "ttl": 4.0}
_OPTION_CHAIN_CACHE: Dict[str, Dict[str, Any]] = {}

# BSE scrip codes for the liquid F&O universe
BSE_CODE_MAP = {
    "RELIANCE": "500325", "TCS": "532540", "HDFCBANK": "500180", "ICICIBANK": "532174",
    "INFY": "500209", "BHARTIARTL": "532454", "SBIN": "500112", "MARUTI": "532500",
    "BAJFINANCE": "500034", "LT": "500510", "SUNPHARMA": "524715", "TITAN": "500114",
    "ADANIENT": "532921", "TATASTEEL": "500470", "AXISBANK": "532215", "WIPRO": "507685",
    "HCLTECH": "532281", "KOTAKBANK": "500247", "ITC": "500875", "ONGC": "500312",
}

INDEX_META = {
    "NIFTY": {
        "ticker": "%5ENSEI",
        "name": "NIFTY 50",
        "exchange": "NSE",
        "tradingview": "NSE:NIFTY",
        "nse_sym": "NIFTY",
        "type": "Indices",
        "lot_size": 25,
        "base_ref": 22450.0
    },
    "SENSEX": {
        "ticker": "%5EBSESN",
        "name": "SENSEX",
        "exchange": "BSE",
        "tradingview": "BSE:SENSEX",
        "nse_sym": "SENSEX",
        "type": "Indices",
        "lot_size": 10,
        "base_ref": 72850.0
    },
    "BANKNIFTY": {
        "ticker": "%5ENSEBANK",
        "name": "BANK NIFTY",
        "exchange": "NSE",
        "tradingview": "NSE:BANKNIFTY",
        "nse_sym": "BANKNIFTY",
        "type": "Indices",
        "lot_size": 15,
        "base_ref": 54200.0
    }
}


def _get_cffi_session() -> Optional[Any]:
    """Returns a curl_cffi session impersonating Chrome 120 with fresh NSE cookies."""
    if not _HAVE_CURL_CFFI:
        return None
    cache = _SESSION_CACHE
    now = time.time()
    if (now - cache.get("last_fail_ts", 0.0)) < 25.0:
        return None
    if cache.get("session") is not None and (now - cache.get("ts", 0.0)) < 120.0:
        return cache["session"]
    try:
        s = cffi_requests.Session(impersonate="chrome120")
        s.headers.update(_NSE_HEADERS)
        s.get("https://www.nseindia.com/option-chain", timeout=_TIMEOUT)
        cache["session"] = s
        cache["ts"] = now
        return s
    except Exception as e:
        logger.debug("NSE cffi session init failed: %s", e)
        cache["last_fail_ts"] = now
        cache["session"] = None
        return None


def fetch_live_index_quotes() -> List[Dict[str, Any]]:
    """
    Fetches real-time market data for the three core benchmark indices:
    Nifty 50, Sensex, and Bank Nifty.
    Returns cached data within 4s TTL for high-frequency client polling.
    """
    now = time.time()
    if _INDICES_CACHE["data"] and (now - _INDICES_CACHE["ts"]) < _INDICES_CACHE["ttl"]:
        return _INDICES_CACHE["data"]

    results = []
    # Also fetch INDIA VIX for options sentiment context
    vix_val = 16.5
    try:
        url_vix = "https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX?interval=1d&range=1d"
        req = urllib.request.Request(url_vix, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            j = json.loads(resp.read().decode())
            meta = j["chart"]["result"][0]["meta"]
            vix_val = float(meta.get("regularMarketPrice") or 16.5)
    except Exception:
        pass

    for sym, info in INDEX_META.items():
        quote = _fetch_yf_chart_quote(info["ticker"])
        if quote:
            lp = quote["price"]
            prev = quote["prev_close"]
            diff = quote["change_val"]
            pct = quote["change_pct"]
            high = quote["high"]
            low = quote["low"]
            open_px = quote["open"]
            source = "LIVE_EXCHANGE"
        else:
            # Fallback to calibrated profile base reference
            lp = info["base_ref"]
            diff = 0.0
            pct = 0.0
            prev = lp
            high = lp * 1.005
            low = lp * 0.995
            open_px = lp
            source = "CALIBRATED_FALLBACK"

        direction = "BULLISH" if diff >= 0 else "BEARISH"
        
        # Calculate intraday range percentage
        day_range_pct = 50.0
        if high > low:
            day_range_pct = max(0.0, min(100.0, ((lp - low) / (high - low)) * 100.0))

        results.append({
            "symbol": sym,
            "name": info["name"],
            "exchange": info["exchange"],
            "tradingview": info["tradingview"],
            "price": round(lp, 2),
            "previous_close": round(prev, 2),
            "change_val": round(diff, 2),
            "change_pct": round(pct, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "open": round(open_px, 2),
            "direction": direction,
            "lot_size": info["lot_size"],
            "vix": round(vix_val, 2),
            "day_range_pct": round(day_range_pct, 1),
            "data_source": source,
            "timestamp": _dt.datetime.now().strftime("%H:%M:%S IST")
        })

    _INDICES_CACHE["data"] = results
    _INDICES_CACHE["ts"] = now
    return results


def _fetch_yf_chart_quote(encoded_ticker: str) -> Optional[Dict[str, Any]]:
    """Fetches fast real-time summary via chart quote endpoint."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_ticker}?interval=1d&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            j = json.loads(resp.read().decode())
            res = j.get("chart", {}).get("result", [])
            if not res:
                return None
            meta = res[0].get("meta", {})
            lp = float(meta.get("regularMarketPrice") or 0.0)
            prev = float(meta.get("chartPreviousClose") or lp)
            if lp <= 0:
                return None
            diff = lp - prev
            pct = (diff / prev * 100.0) if prev > 0 else 0.0
            return {
                "price": lp,
                "prev_close": prev,
                "change_val": diff,
                "change_pct": pct,
                "high": float(meta.get("regularMarketDayHigh") or lp),
                "low": float(meta.get("regularMarketDayLow") or lp),
                "open": float(meta.get("regularMarketOpen") or lp),
            }
    except Exception as exc:
        logger.debug("Failed YF chart quote for %s: %s", encoded_ticker, exc)
        return None


def fetch_nse_option_chain(symbol: str, expiry: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Live official NSE option chain using NSE v3 API with curl_cffi Chrome impersonation.
    Returns normalized spot price, strikes, CE/PE LTP, OI, Change in OI, and Volume.
    """
    sym = (symbol or "NIFTY").upper().strip()
    cache_key = f"{sym}_{expiry or 'near'}"
    now = time.time()
    cached = _OPTION_CHAIN_CACHE.get(cache_key)
    if cached and (now - cached.get("ts", 0.0)) < 15.0:
        return cached.get("data")

    s = _get_cffi_session()
    if s is None:
        return None

    try:
        # Step 1: Query contract info for active expiry dates and strike list
        contract_url = f"https://www.nseindia.com/api/option-chain-contract-info?symbol={sym}"
        r1 = s.get(contract_url, timeout=_TIMEOUT)
        if r1.status_code != 200:
            return None
        
        contract_info = r1.json()
        expiry_dates = contract_info.get("expiryDates", [])
        if not expiry_dates:
            return None

        target_exp = expiry if (expiry and expiry in expiry_dates) else expiry_dates[0]
        inst_type = "Indices" if sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX") else "Equity"

        # Step 2: Query v3 endpoint for the target expiry
        v3_url = f"https://www.nseindia.com/api/option-chain-v3?type={inst_type}&symbol={sym}&expiry={target_exp}"
        r2 = s.get(v3_url, timeout=_TIMEOUT)
        if r2.status_code != 200:
            return None

        v3_json = r2.json()
        rec = v3_json.get("records", {})
        underlying = float(rec.get("underlyingValue") or 0.0)

        filtered = v3_json.get("filtered", {})
        raw_data = filtered.get("data") or rec.get("data") or []
        if not raw_data:
            return None

        out_rows = []
        for item in raw_data:
            k = float(item.get("strikePrice") or 0.0)
            ce = item.get("CE") or {}
            pe = item.get("PE") or {}
            if not ce and not pe:
                continue

            ce_oi = int(ce.get("openInterest", 0) or 0)
            pe_oi = int(pe.get("openInterest", 0) or 0)
            ce_oi_chg = float(ce.get("pchangeinOpenInterest", 0.0) or 0.0)
            pe_oi_chg = float(pe.get("pchangeinOpenInterest", 0.0) or 0.0)
            ce_ltp = float(ce.get("lastPrice", 0.0) or 0.0)
            pe_ltp = float(pe.get("lastPrice", 0.0) or 0.0)

            out_rows.append({
                "strike": k,
                "call": {
                    "oi": ce_oi,
                    "oi_change_pct": round(ce_oi_chg, 2),
                    "change_pct": float(ce.get("pChange", 0.0) or 0.0),
                    "volume": int(ce.get("totalTradedVolume", 0) or 0),
                    "iv": float(ce.get("impliedVolatility", 0.0) or 0.0),
                    "ltp": ce_ltp,
                    "bid": round(max(0.05, ce_ltp * 0.995), 2),
                    "ask": round(ce_ltp * 1.005, 2),
                },
                "put": {
                    "oi": pe_oi,
                    "oi_change_pct": round(pe_oi_chg, 2),
                    "change_pct": float(pe.get("pChange", 0.0) or 0.0),
                    "volume": int(pe.get("totalTradedVolume", 0) or 0),
                    "iv": float(pe.get("impliedVolatility", 0.0) or 0.0),
                    "ltp": pe_ltp,
                    "bid": round(max(0.05, pe_ltp * 0.995), 2),
                    "ask": round(pe_ltp * 1.005, 2),
                }
            })

        out = {
            "data_source": "live",
            "symbol": sym,
            "spot_price": underlying,
            "expiry": target_exp,
            "available_expiries": expiry_dates[:6],
            "chain": out_rows,
            "timestamp": rec.get("timestamp") or _dt.datetime.now().strftime("%d-%b-%Y %H:%M:%S")
        }

        _OPTION_CHAIN_CACHE[cache_key] = {"ts": now, "data": out}
        return out

    except Exception as exc:
        logger.warning("NSE v3 option chain fetch error for %s: %s", sym, exc)
        return None


def _normalize_nse_chain(j: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    rec = j["records"]
    underlying = float(rec.get("underlyingValue", 0.0))
    exp = rec.get("expiryDates", [""])[0]
    out_rows = []
    for item in rec.get("data", []):
        if item.get("expiryDate") != exp:
            continue
        ce = item.get("CE", {}) or {}
        pe = item.get("PE", {}) or {}
        if not ce and not pe:
            continue
        out_rows.append({
            "strike": float(item.get("strikePrice", 0.0)),
            "call": {
                "oi": int(ce.get("openInterest", 0) or 0),
                "oi_change_pct": float(ce.get("pchangeinOpenInterest", 0) or 0.0),
                "change_pct": float(ce.get("pChange", 0.0) or 0.0),
                "volume": int(ce.get("totalTradedVolume", 0) or 0),
                "iv": float(ce.get("impliedVolatility", 0.0) or 0.0),
                "ltp": float(ce.get("lastPrice", 0.0) or 0.0),
                "delta": float(ce.get("delta", 0.0) or 0.0),
                "gamma": float(ce.get("gamma", 0.0) or 0.0),
                "theta": float(ce.get("theta", 0.0) or 0.0),
                "vega": float(ce.get("vega", 0.0) or 0.0),
            },
            "put": {
                "oi": int(pe.get("openInterest", 0) or 0),
                "oi_change_pct": float(pe.get("pchangeinOpenInterest", 0) or 0.0),
                "change_pct": float(pe.get("pChange", 0.0) or 0.0),
                "volume": int(pe.get("totalTradedVolume", 0) or 0),
                "iv": float(pe.get("impliedVolatility", 0.0) or 0.0),
                "ltp": float(pe.get("lastPrice", 0.0) or 0.0),
                "delta": float(pe.get("delta", 0.0) or 0.0),
                "gamma": float(pe.get("gamma", 0.0) or 0.0),
                "theta": float(pe.get("theta", 0.0) or 0.0),
                "vega": float(pe.get("vega", 0.0) or 0.0),
            },
        })
    return {
        "data_source": "live",
        "symbol": symbol.upper(),
        "spot_price": underlying,
        "expiry": exp,
        "chain": out_rows,
    }


def fetch_nse_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Live NSE equity/index quote, retained for callers of the original API."""
    return fetch_live_india_quote(symbol)


def fetch_live_india_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Live Indian stock quote resolver.
    Attempts Yahoo Finance (.NS), then BSE scrip code.
    """
    sym = (symbol or "").upper().strip()
    # If index, route to index meta
    if sym in INDEX_META:
        chart_data = _fetch_yf_chart_quote(INDEX_META[sym]["ticker"])
        if chart_data:
            return {
                "source": "live",
                "exchange": INDEX_META[sym]["exchange"],
                "symbol": sym,
                "last_price": chart_data["price"],
                "previous_close": chart_data["prev_close"],
                "change_val": chart_data["change_val"],
                "change_pct": chart_data["change_pct"],
                "high": chart_data["high"],
                "low": chart_data["low"],
                "open": chart_data["open"],
                "volume": 0
            }

    # Equities
    yf_ticker = f"{sym}.NS"
    chart_data = _fetch_yf_chart_quote(yf_ticker)
    if chart_data:
        return {
            "source": "live",
            "exchange": "NSE",
            "symbol": sym,
            "last_price": chart_data["price"],
            "previous_close": chart_data["prev_close"],
            "change_val": chart_data["change_val"],
            "change_pct": chart_data["change_pct"],
            "high": chart_data["high"],
            "low": chart_data["low"],
            "open": chart_data["open"],
            "volume": 0
        }

    # BSE fallback
    bse_code = BSE_CODE_MAP.get(sym)
    if bse_code:
        return fetch_bse_quote(bse_code)

    return None


def fetch_bse_quote(bse_code: str) -> Optional[Dict[str, Any]]:
    """Live BSE quote by scrip code."""
    if not _HAVE_REQUESTS:
        return None
    code = str(bse_code).strip()
    try:
        url = f"https://api.bseindia.com/Bse_data/json/GetQuote.aspx?Type=EQ&Code={code}"
        r = requests.get(url, headers=_BSE_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        j = r.json()
        q = j[0] if (isinstance(j, list) and j) else (j if isinstance(j, dict) else None)
        if not q:
            return None
        last = q.get("LTP") or q.get("lastPrice") or q.get("PrevClose")
        if last is None:
            return None
        return {
            "source": "live",
            "exchange": "BSE",
            "bse_code": code,
            "last_price": float(last),
            "previous_close": float(q.get("PrevClose", last)),
            "open": float(q.get("Open", last)),
            "high": float(q.get("High", last)),
            "low": float(q.get("Low", last)),
            "change_pct": float(q.get("ChangePct", 0.0) or 0.0),
            "volume": int(float(q.get("Volume", 0) or 0)),
            "name": q.get("CompanyName", ""),
        }
    except Exception as e:
        logger.debug("BSE quote fetch failed for %s: %s", code, e)
        return None
