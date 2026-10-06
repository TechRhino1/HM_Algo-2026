"""
HM Algo 2.0 — Multi-Timeframe Data Feed Engine.
Provides thread-safe, timeout-guarded OHLCV data streaming from MT5 with realistic synthetic fallback generation.
"""
import time

from jarvis.data.broker_symbols import resolve_broker_symbol, ensure_mt5_terminal
from jarvis.data.broker_time import broker_utc_offset
# One bar-duration map for the whole codebase. The provider already owned the
# canonical copy; duplicating it here is how a second source of truth starts.
from jarvis.data.tradingview_provider import TF_SECONDS_MAP
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, Optional, Any
from jarvis.common.timeout_guard import TimeoutGuard
from jarvis.common.bounded_cache import BoundedTTLCache
from jarvis.observability import log_event
from jarvis.observability.instruments import MT5_RATES_FETCHES, MT5_RATES_LATENCY, CACHE_ENTRIES

import threading

logger = logging.getLogger("JARVIS_DataFeed")

#: Ceiling on the rates cache. A scan covers (symbols x timeframes x styles);
#: 512 leaves room for the whole configured universe several times over while
#: still bounding memory when a caller supplies keys we have never seen.
_MAX_CACHE_ENTRIES = 512

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    MT5_AVAILABLE = False

TF_MAP = {
    "M1": mt5.TIMEFRAME_M1 if (MT5_AVAILABLE and hasattr(mt5, "TIMEFRAME_M1")) else 1,
    "M5": mt5.TIMEFRAME_M5 if (MT5_AVAILABLE and hasattr(mt5, "TIMEFRAME_M5")) else 5,
    "M10": getattr(mt5, "TIMEFRAME_M10", 10) if MT5_AVAILABLE else 10,
    "M15": mt5.TIMEFRAME_M15 if (MT5_AVAILABLE and hasattr(mt5, "TIMEFRAME_M15")) else 15,
    "M30": mt5.TIMEFRAME_M30 if (MT5_AVAILABLE and hasattr(mt5, "TIMEFRAME_M30")) else 30,
    "H1": mt5.TIMEFRAME_H1 if (MT5_AVAILABLE and hasattr(mt5, "TIMEFRAME_H1")) else 16385,
    "H4": mt5.TIMEFRAME_H4 if (MT5_AVAILABLE and hasattr(mt5, "TIMEFRAME_H4")) else 16388,
    "D1": mt5.TIMEFRAME_D1 if (MT5_AVAILABLE and hasattr(mt5, "TIMEFRAME_D1")) else 16408,
}

# ─── Trade-style → timeframe map (single source of truth) ────────────────────
# The three trade styles differ ONLY by the timeframes they request; nothing
# else about them is distinct. This map was previously inlined inside
# ``fetch_multi_timeframe``, which meant the live path and any offline
# backtest could silently drift apart. Both now read it from here.
STYLE_TIMEFRAMES: Dict[str, Dict[str, str]] = {
    "SWING": {"macro": "D1", "context": "H4", "primary": "H1", "setup": "H4", "timing": "M15", "anchor": "D1"},
    "DAY_TRADING": {"macro": "H4", "context": "H1", "primary": "M15", "setup": "H1", "timing": "M5", "anchor": "D1"},
    "SCALP": {"macro": "H1", "context": "M15", "primary": "M5", "setup": "M5", "timing": "M1", "anchor": "D1"},
}

# Accepted spellings for the day-trading style.
_DAY_ALIASES = ("DAY_TRADING", "INTRADAY", "DAY")


def normalise_style(trade_style: Optional[str]) -> str:
    """Canonicalise a trade-style name; unknown/None falls back to SWING."""
    style = (trade_style or "SWING").upper().strip()
    if style in _DAY_ALIASES:
        return "DAY_TRADING"
    return style if style in STYLE_TIMEFRAMES else "SWING"


def style_timeframes(trade_style: Optional[str]) -> Dict[str, str]:
    """Role → timeframe map for a trade style (macro/context/primary/setup/timing)."""
    return dict(STYLE_TIMEFRAMES[normalise_style(trade_style)])


def style_timeframe_set(trade_style: Optional[str]) -> set:
    """The distinct timeframes a style needs — i.e. what it must have data for."""
    return set(style_timeframes(trade_style).values())


# ─── Bar freshness ──────────────────────────────────────────────────────────
# A bar is "fresh" while the newest bar's open is no older than this many bar
# durations. It cannot be 1: with `include_current_bar=False` the newest bar
# returned is the last CLOSED one, so its open is already 1-2 durations old and
# a 1x rule would flag every healthy frame.
# In addition, precious metals (Gold, Silver) and CFD indices have a 1-hour daily
# rollover / maintenance break where no bars form. During the first hour after
# reopen, the last closed bar is 2.5 to 3.0 bar durations old.
# A closed-bar tolerance of 3.5 bridges this daily break without accepting
# genuinely stalled feeds (4-8h+).
_FRESH_BAR_TOLERANCE = 2.5
_FRESH_BAR_TOLERANCE_LIVE = 2.5
_FRESH_BAR_TOLERANCE_CLOSED = 3.5

# Cold-history warm-up budget. MT5 downloads a symbol's history on demand and
# does it asynchronously, so the first read after `symbol_select` can return a
# stale tail. Measured: 5 of 6 untouched symbols read ~19.6h stale, unchanged
# across 7 back-to-back calls (30-47ms), then fresh within a few hundred ms.
# Polled in small steps rather than one guessed sleep.
_COLD_SYNC_BUDGET_SEC = 1.2
_COLD_SYNC_POLL_SEC = 0.3

FRESH = "FRESH"
STALE = "STALE"
MARKET_CLOSED = "MARKET_CLOSED"
FRESHNESS_UNKNOWN = "UNKNOWN"


def _is_weekend_gap(now_utc: float) -> bool:
    """True when the clock sits in the weekly close (forex/metals).

    Without this, the 48h gap a Sunday frame legitimately has would be reported
    as a stalled feed. The market is closed from ~21:00 UTC Friday to ~21:00 UTC
    Sunday, so a large age there is expected rather than a defect.
    """
    moment = datetime.fromtimestamp(now_utc, tz=timezone.utc)
    weekday, hour = moment.weekday(), moment.hour          # Mon=0, Sun=6
    if weekday == 5:                                       # Saturday
        return True
    if weekday == 4 and hour >= 21:                        # Friday, after close
        return True
    if weekday == 6 and hour < 21:                         # Sunday, before open
        return True
    return False


def classify_bar_freshness(
    last_bar_epoch: Optional[float],
    timeframe: str,
    *,
    include_current_bar: bool = False,
    now_utc: Optional[float] = None,
    offset_sec: int = 0,
) -> tuple:
    """Verdict and age for the newest bar, in the CLOCK THE BARS ARE STAMPED IN.

    `copy_rates_from_pos` stamps bar opens on the BROKER's clock, exactly like
    tick times, so subtracting them from the real UTC clock under-reports the
    age by the broker offset (2-3h for XM) and would hide a stalled feed. Pass
    the offset from `jarvis.data.broker_time`; `offset_sec=0` is the old
    (wrong, but visible) behaviour.

    Returns `(verdict, age_sec)` where age may be negative if the bar is stamped
    slightly ahead of now (clock skew). `verdict` is one of FRESH, STALE,
    MARKET_CLOSED, UNKNOWN - UNKNOWN when the timeframe is unrecognised, so an
    unverifiable frame is never silently reported as fresh.
    """
    bar_sec = TF_SECONDS_MAP.get(str(timeframe or "").upper())
    if not bar_sec or not last_bar_epoch:
        return FRESHNESS_UNKNOWN, None

    now = time.time() if now_utc is None else float(now_utc)
    age = (now + float(offset_sec)) - float(last_bar_epoch)

    # A small negative age is normal skew (the newest bar can open "now").
    if age < 0:
        return (FRESH if -age <= bar_sec else FRESHNESS_UNKNOWN), age

    # Weekend gap bridge for closed D1 and H4 bars:
    # Forex, Gold, Silver and Indices close from Friday 21:00 UTC to Sunday 21:00 UTC (~48 hours = 172,800s).
    # On Monday (and Sunday open), the newest closed D1 bar is Friday's bar (opened Fri 00:00 UTC).
    # By Monday afternoon, Friday 00:00 is 3.5+ days old (~300,000s). Without subtracting the 48h
    # non-trading weekend closure, Friday's D1 bar exceeds the 3.5 * 86,400s tolerance (302,400s)
    # and is falsely marked STALE, blocking all Monday trading across all traditional assets.
    effective_age = age
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    tf_upper = str(timeframe or "").upper()
    if tf_upper in ("D1", "H4"):
        # If today is Monday (weekday == 0) or Sunday post-open (weekday == 6):
        # Subtract the 48-hour weekend market closure (172,800s) when spanning the weekend.
        if moment.weekday() == 0 or (moment.weekday() == 6 and moment.hour >= 21):
            effective_age = max(0.0, age - 172800.0)

    tol = _FRESH_BAR_TOLERANCE_LIVE if include_current_bar else _FRESH_BAR_TOLERANCE_CLOSED
    if effective_age <= bar_sec * tol:
        return FRESH, age
    if _is_weekend_gap(now):
        return MARKET_CLOSED, age
    return STALE, age


def first_stale_frame(mtf_data) -> tuple:
    """First ``(role, age_sec)`` whose frame is STALE, else ``(None, 0.0)``.

    Reads the verdict stamped by `fetch_rates` rather than re-deriving it - a
    second age calculation would be a second thing to keep in step with the
    broker's clock. Used by every decision path so "do not act on a stalled
    feed" cannot be enforced in one entry point and forgotten in another.

    Narrower than :func:`first_untrusted_frame`, which is what the decision paths
    use. Kept because "is any frame stale" is still a question worth being able
    to ask on its own.
    """
    for role, frame in (mtf_data or {}).items():
        attrs = getattr(frame, "attrs", None) or {}
        if attrs.get("freshness") == STALE:
            return role, float(attrs.get("bar_age_sec") or 0.0)
    return None, 0.0


LIVE_SOURCE = "LIVE_MT5"


def first_untrusted_frame(mtf_data) -> tuple:
    """First ``(role, reason, age_sec)`` we must not reason on, else ``(None, None, 0.0)``.

    Broader than :func:`first_stale_frame` in exactly one way, and that way is
    the one that matters: a frame stamped ``LIVE_MT5`` whose freshness is
    ``UNKNOWN`` is refused, because we cannot say how old it is.

    This closes a fail-open. ``classify_bar_freshness`` returns ``UNKNOWN`` not
    only for an unrecognised timeframe but also when ``last_bar_epoch`` is absent
    or the age is more negative than one bar. The gate used to block on ``STALE``
    alone, on the assumption that ``UNKNOWN`` meant "synthetic/fallback frame".
    It does not. When ``broker_utc_offset`` returned 0 every age came out
    negative, every frame became ``UNKNOWN``, and the stale-feed gate silently
    stopped blocking anything - two independent safety mechanisms sharing one
    failure mode, with the check reporting success throughout.

    ``UNKNOWN`` on a frame that does **not** claim to be live is still tolerated:
    a synthetic frame is already labelled and surfaced, and refusing it would
    break the dev paths rather than protect anything. ``MARKET_CLOSED`` never
    blocks - a shut market is not a broken feed.
    """
    for role, frame in (mtf_data or {}).items():
        attrs = getattr(frame, "attrs", None) or {}
        verdict = attrs.get("freshness")
        age = float(attrs.get("bar_age_sec") or 0.0)
        if verdict == STALE:
            return role, "stale", age
        if verdict == FRESHNESS_UNKNOWN and attrs.get("data_source") == LIVE_SOURCE:
            return role, "unverifiable_age", age
    return None, None, 0.0


# Sources that are NOT real broker data. A frame stamped with one of these must
# not be reasoned on when a broker link is available -- see
# `Orchestrator.run_cycle_for_symbol`.
UNUSABLE_SOURCES = frozenset({"SYNTHETIC_FALLBACK"})


def first_unusable_frame(mtf_data) -> tuple:
    """First role whose frame is fabricated, with its source.

    ``(None, None)`` when every frame is real market data.
    """
    for role, frame in (mtf_data or {}).items():
        attrs = getattr(frame, "attrs", None) or {}
        source = attrs.get("data_source")
        if source in UNUSABLE_SOURCES:
            return role, source
    return None, None


class DataFeedEngine:
    _mt5_fetch_lock = threading.Lock()

    def __init__(self, mt5_client: Any = None, timeout_sec: float = 3.0):
        self.mt5_client = mt5_client
        self.timeout_sec = timeout_sec
        self._cache_ttl_sec = 8.0
        # Bounded, not a plain dict. The key is built from caller-supplied
        # values (`symbol`, `timeframe`, `num_bars`), and one of them arrives
        # straight off an HTTP query string — so an unbounded dict here is a
        # memory growth path driven by remote input. See
        # `jarvis/common/bounded_cache.py`.
        self._cache = BoundedTTLCache(max_entries=_MAX_CACHE_ENTRIES,
                                      ttl_sec=self._cache_ttl_sec,
                                      name="data_feed.rates")
        # One warning per symbol/timeframe: a stalled feed repeats on every poll
        # and would otherwise bury the rest of the log.
        self._stale_warned: set = set()
        # Broker symbols whose on-demand history has already been waited for.
        self._warmed: set = set()
        # A symbol the broker does not offer re-warns on every poll, for every
        # timeframe. One line each, like `_stale_warned`.
        self._no_rates_warned: set = set()
        # A missing terminal is an *expected* condition here (paper mode never
        # establishes one, and a stopped terminal is not an error), but it is
        # re-tested on every poll: 20 symbols x 12 timeframes x every 8s cache
        # expiry produced ~3,300 WARNING lines in 17 minutes, which is how a
        # real error gets missed. One line per (symbol, timeframe), and the set
        # is cleared on recovery so a *later* outage still warns.
        self._terminal_unavailable_warned: set = set()
        # What the last fetch actually returned. Market-data health is a
        # *measured* property of this engine; it used to be inferred from the
        # execution account's login, which reported OFFLINE forever in paper
        # mode while real bars were streaming.
        self._health: Dict[str, Any] = {
            "source": None,
            "freshness": None,
            "symbol": None,
            "timeframe": None,
            "at": 0.0,
        }
        self._health_lock = threading.Lock()

    def _note_health(self, df: pd.DataFrame, symbol: str, timeframe: str) -> None:
        with self._health_lock:
            self._health = {
                "source": df.attrs.get("data_source"),
                "freshness": df.attrs.get("freshness"),
                "symbol": symbol,
                "timeframe": timeframe,
                "at": time.time(),
            }

    def data_health(self, max_age_sec: float = 120.0) -> Dict[str, Any]:
        """Honest market-data health, derived from the last fetch that ran.

        ``status`` is one of:
          STREAMING - real broker bars, verified fresh
          STALE     - real broker bars, but the newest one is old
          CLOSED    - real broker bars, market shut (weekend/holiday gap)
          SYNTHETIC - not real bars; the feed could not reach the broker
          OFFLINE   - nothing has been fetched yet, or not for a long while
        """
        with self._health_lock:
            h = dict(self._health)

        age = time.time() - float(h.get("at") or 0.0)
        if not h.get("source") or age > max_age_sec:
            h["status"] = "OFFLINE"
            h["age_sec"] = None if not h.get("at") else round(age, 1)
            return h

        source = h.get("source")
        freshness = h.get("freshness")
        if source != "LIVE_MT5":
            h["status"] = "SYNTHETIC"
        elif freshness == FRESH:
            h["status"] = "STREAMING"
        elif freshness == MARKET_CLOSED:
            h["status"] = "CLOSED"
        elif freshness == STALE:
            h["status"] = "STALE"
        else:
            # Live bars of unverifiable age: real, but not a freshness claim.
            h["status"] = "STREAMING"
        h["age_sec"] = round(age, 1)
        return h

    def fetch_rates(self, symbol: str, timeframe: str = "H1", num_bars: int = 300, include_current_bar: bool = False) -> pd.DataFrame:
        cache_key = f"{symbol}_{timeframe}_{num_bars}_{include_current_bar}"
        now = time.time()
        entry = self._cache.get(cache_key)
        if entry is not None and now - entry["timestamp"] < self._cache_ttl_sec:
            return entry["df"]

        def _fetch():
            if not MT5_AVAILABLE or self.mt5_client is None or getattr(self.mt5_client, "mode", "dry_run") == "dry_run":
                df = self._generate_realistic_rates(symbol, timeframe, num_bars)
                df.attrs["data_source"] = "SYNTHETIC_FALLBACK"
                # Fabricated bars have no age to verify. Marked UNKNOWN rather
                # than FRESH so nothing downstream can mistake a synthetic
                # frame for a verified one.
                df.attrs["freshness"] = FRESHNESS_UNKNOWN
                return df

            # Reading bars needs an initialized terminal, and paper mode
            # deliberately does not establish one (it simulates fills). Do it
            # here so "paper" means simulated fills on REAL bars -- not
            # simulated bars. Without this the canonical name reaches
            # `copy_rates_from_pos`, the broker answers 0 rows, and every frame
            # silently becomes SYNTHETIC_FALLBACK.
            if not ensure_mt5_terminal():
                key = (symbol, timeframe)
                if key not in self._terminal_unavailable_warned:
                    self._terminal_unavailable_warned.add(key)
                    logger.warning(
                        "MT5 terminal unavailable for market data; %s %s falls back to "
                        "synthetic bars. (Reading bars is independent of execution mode.) "
                        "Further fallbacks for this symbol/timeframe are silent until the "
                        "terminal returns.",
                        symbol, timeframe,
                    )
                df = self._generate_realistic_rates(symbol, timeframe, num_bars)
                df.attrs["data_source"] = "SYNTHETIC_FALLBACK"
                df.attrs["freshness"] = FRESHNESS_UNKNOWN
                return df

            if self._terminal_unavailable_warned:
                logger.info(
                    "MT5 terminal is back; market data for %d symbol/timeframe "
                    "combination(s) is live again.",
                    len(self._terminal_unavailable_warned),
                )
                self._terminal_unavailable_warned.clear()

            resolved_sym = self.mt5_client.resolve_symbol_name(symbol) if hasattr(self.mt5_client, "resolve_symbol_name") else symbol
            mt5_tf = TF_MAP.get(timeframe, 16385)
            start_pos = 0 if include_current_bar else 1
            _broker_sym = resolve_broker_symbol(resolved_sym) or resolved_sym

            def _read():
                with DataFeedEngine._mt5_fetch_lock:
                    r = mt5.copy_rates_from_pos(_broker_sym, mt5_tf, start_pos, num_bars)
                    if r is None or len(r) == 0:
                        # Fallback to pos 0 if start_pos returns empty
                        r = mt5.copy_rates_from_pos(_broker_sym, mt5_tf, 0, num_bars)
                return r

            def _build(rates):
                """Frame + freshness verdict. `rates["time"]` is the broker's
                clock, so the age must be measured against now-in-broker-time;
                against raw UTC it under-reports by the offset (2-3h for XM) and
                a stalled feed looks healthy. `_broker_sym` is the symbol the
                broker actually answered for, which is the only one whose tick
                offset is valid."""
                d = pd.DataFrame(rates)
                d["time"] = pd.to_datetime(d["time"], unit="s")
                d.rename(columns={"tick_volume": "volume"}, inplace=True)
                cols = ["time", "open", "high", "low", "close", "volume"]
                if "spread" in d.columns:
                    # MT5's `copy_rates_from_pos` returns a per-bar `spread`
                    # column measured in **points** (integer). Keep it, but do
                    # NOT convert here: the canonical points→pips conversion is
                    #   pips = spread_points * 10**-digits / pip_size
                    # and is applied by the consumer (the same formula the
                    # backtest scan uses, backtesting/signal_scan.py:170-180).
                    # Reporting only — nothing prices, sizes or gates on this
                    # column; the decision path still uses the registry
                    # constant. Guarded because the fallback read and any
                    # non-MT5 source need not carry the column.
                    cols.append("spread")
                out = d[cols].copy()
                out.attrs["data_source"] = "LIVE_MT5"
                off = broker_utc_offset(mt5_module=mt5, symbols=[_broker_sym])
                v, a = classify_bar_freshness(
                    float(rates["time"][-1]),
                    timeframe,
                    include_current_bar=include_current_bar,
                    offset_sec=off,
                )
                out.attrs["freshness"] = v
                out.attrs["bar_age_sec"] = None if a is None else round(a, 1)
                return out, v, a

            rates = _read()
            if rates is None or len(rates) == 0:
                if _broker_sym not in self._no_rates_warned:
                    self._no_rates_warned.add(_broker_sym)
                    logger.warning(
                        "MT5 returned 0 rates for %s (%s) - the broker does not appear to "
                        "offer '%s'. Falling back to synthetic rates; a live session "
                        "refuses to decide on them.",
                        symbol, timeframe, _broker_sym,
                    )
                df = self._generate_realistic_rates(symbol, timeframe, num_bars)
                df.attrs["data_source"] = "SYNTHETIC_FALLBACK"
                # Fabricated bars have no age to verify. Marked UNKNOWN rather
                # than FRESH so nothing downstream can mistake a synthetic
                # frame for a verified one.
                df.attrs["freshness"] = FRESHNESS_UNKNOWN
                return df

            res_df, verdict, age = _build(rates)
            first_age = age

            if verdict == STALE and _broker_sym not in self._warmed:
                # MT5 pulls a symbol's history on demand, and the download is
                # ASYNCHRONOUS: the first read after `symbol_select` returns a
                # stale tail (measured ~19.6h behind on 5 of 6 cold symbols,
                # unchanged across 7 rapid calls) while the real bars are still
                # being fetched. They land in well under a second. Without this
                # bounded wait the freshness gate refuses a perfectly good symbol
                # on the first cycle after every restart - intermittently, and
                # only right after a restart, which is what a phantom bug looks
                # like. Marked warmed either way so the wait is paid once.
                self._warmed.add(_broker_sym)
                try:
                    mt5.symbol_select(_broker_sym, True)
                except Exception as e:
                    logger.warning("symbol_select failed while warming cold history for %s: %s", _broker_sym, e)
                deadline = time.time() + _COLD_SYNC_BUDGET_SEC
                started = time.time()
                while time.time() < deadline:
                    time.sleep(_COLD_SYNC_POLL_SEC)
                    retry = _read()
                    if retry is None or len(retry) == 0:
                        continue
                    res2, v2, a2 = _build(retry)
                    if v2 != STALE:
                        res_df, verdict, age = res2, v2, a2
                        logger.info(
                            "Warmed cold history for %s %s: first read was %.0fs stale, "
                            "now %s after %.1fs.",
                            symbol, timeframe,
                            first_age if first_age is not None else 0.0,
                            v2, time.time() - started,
                        )
                        break

            if verdict == STALE and cache_key not in self._stale_warned:
                self._stale_warned.add(cache_key)
                bar_sec = TF_SECONDS_MAP.get(str(timeframe).upper(), 1)
                logger.warning(
                    "Stale candles for %s %s: newest bar is %.0fs old (%.1f bar durations) "
                    "while the market is open - decisions should not be taken on this frame.",
                    symbol, timeframe, age, age / max(1, bar_sec),
                )
            return res_df


        def _fallback_gen():
            df = self._generate_realistic_rates(symbol, timeframe, num_bars)
            df.attrs["data_source"] = "SYNTHETIC_FALLBACK"
            df.attrs["freshness"] = FRESHNESS_UNKNOWN
            return df

        # Instrumented: the fetch is the only place that learns whether the bars
        # a decision will be taken on came from a broker or from the synthetic
        # generator. Nothing downstream re-checks, so if this is not counted
        # here, a fully synthetic session is indistinguishable from a live one.
        with MT5_RATES_LATENCY.time(timeframe=str(timeframe)) as _fetch_timer:
            df_result = TimeoutGuard.run_sync(
                _fetch,
                timeout_sec=self.timeout_sec,
                default=_fallback_gen,
                task_name=f"DataFeed_fetch_{symbol}_{timeframe}"
            )

        if "data_source" not in df_result.attrs:
            df_result.attrs["data_source"] = "SYNTHETIC_FALLBACK"
        if "freshness" not in df_result.attrs:
            # A frame of unknown origin must not read as verified.
            df_result.attrs["freshness"] = FRESHNESS_UNKNOWN

        source = str(df_result.attrs.get("data_source") or "SYNTHETIC_FALLBACK").lower()
        MT5_RATES_FETCHES.inc(timeframe=str(timeframe), source=source)
        log_event(
            logger,
            logging.WARNING if source != "live_mt5" else logging.DEBUG,
            "rates_fetched",
            f"{symbol} {timeframe}: {source}",
            symbol=symbol,
            timeframe=str(timeframe),
            source=source,
            duration_ms=_fetch_timer.elapsed_ms,
        )

        self._note_health(df_result, symbol, timeframe)
        self._cache[cache_key] = {"df": df_result, "timestamp": now}
        CACHE_ENTRIES.set(len(self._cache), cache="data_feed.rates")
        return df_result

    def fetch_multi_timeframe(
        self,
        symbol: str,
        trade_style: str = "SWING",
        timeframes: Optional[Dict[str, str]] = None,
        num_bars: int = 250
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetches multi-timeframe OHLCV rates mapped by role.
        Configures timeframes dynamically based on trade_style:
        - SWING: macro=D1, context=H4, primary=H1, setup=H4, timing=M15
        - DAY_TRADING / INTRADAY / DAY: macro=H4, context=H1, primary=M15, setup=H1, timing=M5
        - SCALP: macro=H1, context=M15, primary=M5, setup=M5, timing=M1
        """
        if timeframes is None:
            timeframes = style_timeframes(trade_style)

        result = {}
        for role, tf in timeframes.items():
            result[role] = self.fetch_rates(symbol, timeframe=tf, num_bars=num_bars)
        return result

    def _generate_realistic_rates(self, symbol: str, timeframe: str, num_bars: int) -> pd.DataFrame:
        """Generates realistic institutional market price series with trend cycles, liquidity sweeps, and volatility clusters."""
        import zlib
        seed = zlib.crc32(f"{symbol.upper()}_{timeframe}".encode("utf-8"))
        np.random.seed(seed)
        
        u_sym = symbol.upper()
        if any(k in u_sym for k in ["XAU", "GOLD"]):
            base_price = 4380.0
            vol = 0.0025
        elif "BTC" in u_sym:
            base_price = 77000.0
            vol = 0.0050
        elif "ETH" in u_sym:
            base_price = 3400.0
            vol = 0.0055
        elif "SOL" in u_sym:
            base_price = 185.0
            vol = 0.0065
        elif any(k in u_sym for k in ["US500", "SPX"]):
            base_price = 5850.0
            vol = 0.0018
        elif any(k in u_sym for k in ["NAS100", "USTEC", "NDX"]):
            base_price = 20500.0
            vol = 0.0022
        elif any(k in u_sym for k in ["US30", "DJ"]):
            base_price = 42500.0
            vol = 0.0016
        elif any(k in u_sym for k in ["WTI", "OIL", "CRUDE"]):
            base_price = 76.50
            vol = 0.0035
        elif "EURUSD" in u_sym:
            base_price = 1.1580
            vol = 0.0012
        elif "GBPUSD" in u_sym:
            base_price = 1.3480
            vol = 0.0014
        elif "USDJPY" in u_sym:
            base_price = 158.50
            vol = 0.0015
        elif "EURJPY" in u_sym:
            base_price = 172.00
            vol = 0.0015
        elif "GBPJPY" in u_sym:
            base_price = 205.00
            vol = 0.0016
        elif "AUDUSD" in u_sym:
            base_price = 0.6650
            vol = 0.0013
        elif "NZDUSD" in u_sym:
            base_price = 0.6050
            vol = 0.0013
        elif "USDCHF" in u_sym:
            base_price = 0.8850
            vol = 0.0012
        elif "USDCAD" in u_sym:
            base_price = 1.3750
            vol = 0.0012
        else:
            base_price = 100.0
            vol = 0.0020

        # Generate regime cycles: Bullish expansion -> Consolidation -> Pullback -> Breakout
        returns = []
        regimes = [0.0006, 0.0001, -0.0005, 0.0008, 0.0002, -0.0004]
        reg_idx = 0
        cycle_len = 200

        for i in range(num_bars):
            if i > 0 and i % cycle_len == 0:
                reg_idx = (reg_idx + 1) % len(regimes)
            drift = regimes[reg_idx]
            noise = np.random.normal(drift, vol * 0.6)
            returns.append(noise)


        returns = np.array(returns)
        prices = base_price * np.exp(np.cumsum(returns))

        freq_map = {"M1": "1min", "M5": "5min", "M10": "10min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1D"}
        freq = freq_map.get(timeframe, "1h")
        import datetime
        dates = pd.date_range(end=pd.Timestamp.now(tz=datetime.timezone.utc).tz_localize(None), periods=num_bars, freq=freq)

        closes = prices
        opens = np.roll(closes, 1)
        opens[0] = base_price

        bodies = np.abs(closes - opens)
        wick_upper = bodies * np.abs(np.random.normal(0.15, 0.10, num_bars)) + (vol * prices * 0.15)
        wick_lower = bodies * np.abs(np.random.normal(0.15, 0.10, num_bars)) + (vol * prices * 0.15)


        highs = np.maximum(opens, closes) + wick_upper
        lows = np.minimum(opens, closes) - wick_lower
        volumes = np.random.randint(800, 4500, num_bars).astype(float)

        return pd.DataFrame({
            "time": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes
        })


