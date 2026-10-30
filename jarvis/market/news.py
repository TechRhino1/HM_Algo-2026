"""

HM Algo 2.0 — Live Institutional Macro News & Economic Calendar Engine.

Fetches real-time economic calendar from live financial feeds (FairEconomy + MyFxBook),

parses currency impact, evaluates macro shocks, computes Indian Standard Time (IST) & UTC,

provides deep indicator intelligence for modal inspection, displays the single most recent 1 release on top,

followed by all upcoming news events sorted chronologically.

"""

import re

import json

import os

import ssl

import time

import logging

import threading

import urllib.request

import xml.etree.ElementTree as ET

from datetime import datetime, timezone, timedelta

from typing import Dict, List, Any, ClassVar



from jarvis.common.http import read_bounded

from jarvis.observability import log_event

from jarvis.observability.instruments import (

    NEWS_CALENDAR_SERVED,

    NEWS_FETCHES,

    NEWS_FETCH_LATENCY,

)



logger = logging.getLogger("HM_LiveNewsEngine")



# Indian Standard Time (IST = UTC + 5:30)

IST_TZ = timezone(timedelta(hours=5, minutes=30))



# Macroeconomic Indicator Knowledge Base for Deep Modal Analytics

EVENT_KNOWLEDGE = {

    "pmi": {

        "desc": "Purchasing Managers' Index (PMI) is an institutional benchmark measuring business activity across manufacturing and services sectors. Readings above 50.0 indicate economic expansion, while below 50.0 signal contraction.",

        "impact": "Higher than forecast PMI reflects robust business activity, driving sovereign yield increases and strengthening the base currency (Bullish USD/EUR/GBP, Bearish Gold short-term).",

        "category": "Economic Activity & Production"

    },

    "retail sales": {

        "desc": "Measures total consumer expenditure across retail establishments. Consumer spending accounts for approximately 68-70% of total GDP, making this a top-tier macroeconomic driver.",

        "impact": "Surprise increases in retail spending accelerate consumer price inflation expectations, cementing hawkish rate trajectories.",

        "category": "Consumer Demand & Spending"

    },

    "pce": {

        "desc": "Personal Consumption Expenditures (PCE) Price Index measures changes in the prices of goods and services purchased by consumers in the United States. It is the Federal Reserve's primary preferred metric for tracking inflation.",

        "impact": "Higher PCE prints directly increase terminal interest rate forecasts, producing swift institutional USD rallies and sharp pullbacks in Gold (XAUUSD).",

        "category": "Inflation & Central Bank Target"

    },

    "cpi": {

        "desc": "Consumer Price Index (CPI) evaluates price level changes of a representative basket of consumer goods and services over time.",

        "impact": "Deviations from consensus create the largest single-day volatility shocks across Forex, Metals, and Equity Index futures.",

        "category": "Inflation & Purchasing Power"

    },

    "non-farm": {

        "desc": "Non-Farm Payrolls (NFP) evaluates the total monthly net change in paid US workers across non-agricultural businesses.",

        "impact": "High job gains coupled with rising average hourly earnings generate massive institutional liquidity sweeps across all dollar pairs.",

        "category": "Labor Market & Employment"

    },

    "jobless": {

        "desc": "Initial Jobless Claims tracks the number of individuals seeking initial unemployment insurance benefits during the preceding week.",

        "impact": "Low claims demonstrate labor market tightness, reducing rate-cut probability and supporting the base currency.",

        "category": "High-Frequency Labor Data"

    },

    "rig count": {

        "desc": "Baker Hughes Rig Count serves as an essential supply-side barometer for North American crude oil and natural gas production capacity.",

        "impact": "Lower active rig counts signal declining future drilling capacity, underpinning energy prices and commodity currencies.",

        "category": "Energy & Commodity Supply"

    },

    "confidence": {

        "desc": "Consumer Confidence indices measure sentiment, financial optimism, and labor security perceptions among households.",

        "impact": "Rising confidence indicates resilient future consumption, improving risk appetite across equity and currency markets.",

        "category": "Sentiment & Forward Expectations"

    },

    "monetary policy": {

        "desc": "Central bank interest rate decisions, asset purchase guidance, and monetary policy trajectory statements.",

        "impact": "Directly impacts sovereign bond yield curves, interbank lending rates, and global capital flow allocation.",

        "category": "Central Bank Policy"

    },

    "speaks": {

        "desc": "Official speeches and press conferences by central bank governors and heads of state, offering policy nuance and forward guidance.",

        "impact": "Unscripted remarks regarding inflation, tariffs, or interest rate trajectory induce sharp headline-driven price spikes.",

        "category": "Central Bank & Geopolitical Commentary"

    },

    "trade": {

        "desc": "Balance of Trade measures the net monetary difference between a nation's total exported and imported goods and services.",

        "impact": "Persistent trade surpluses reflect strong foreign currency demand for domestic goods, supporting currency valuation.",

        "category": "Trade & Capital Accounts"

    },

    "gdp": {

        "desc": "Gross Domestic Product (GDP) represents the annualized monetary value of all finished goods and services produced within a country.",

        "impact": "Broad indicator of economic health; higher growth attracts global institutional portfolio inflows.",

        "category": "National Output & Growth"

    }

}



class LiveNewsEngine:

    """Real-time institutional news calendar engine with Indian Time (IST) & deep event analytics."""

    

    FAIRECONOMY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    FAIRECONOMY_MIRROR_URL = "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json"

    BIQUOTE_URL = "https://biquote.io/api/calendar"

    BIQUOTE_UPCOMING_URL = "https://biquote.io/api/calendar/upcoming"

    MYFXBOOK_URL = "https://www.myfxbook.com/rss/forex-economic-calendar-events"

    DISK_CACHE_PATH = os.path.join("data", "news_calendar_cache.json")

    #: Cache schema. Bumped when the event shape changes in a way that makes
    #: older rows untrustworthy. Version 1 held MyFxBook events whose relative
    #: "time left" could not be parsed: they were stamped with the fetch time
    #: and then replayed as "LIVE NOW" for up to seven days. Version 2 carries
    #: `feed` and `time_quality`, so an undated event can never be mistaken for
    #: a real release again.
    CACHE_VERSION = 2

    #: How long to stop asking after a 429. FairEconomy's cooldown is minutes,

    #: so the 90s cache TTL would otherwise re-trigger it on every expiry.

    RATE_LIMIT_BACKOFF_SEC: float = 600.0

    #: How long to stop asking a source that answered 403/401/404. That is a

    #: block, not a blip, and MyFxBook's is a JS challenge urllib cannot pass.

    BLOCKED_SOURCE_BACKOFF_SEC: float = 3600.0

    #: How long a synchronous caller may follow another thread's in-flight fetch

    #: before giving up and serving the cache. The two socket timeouts sum to

    #: 11s, so this is slack, not a real deadline -- it only exists so a wedged

    #: leader cannot pin a caller forever.

    SYNC_FOLLOW_TIMEOUT_SEC: float = 30.0

    

    COUNTRY_MAP: ClassVar[Dict[str, str]] = {

        "United States": "USD", "US": "USD", "Euro Area": "EUR", "Eurozone": "EUR", "Germany": "EUR",

        "France": "EUR", "Italy": "EUR", "Spain": "EUR", "Netherlands": "EUR",

        "United Kingdom": "GBP", "UK": "GBP", "Japan": "JPY", "Switzerland": "CHF",

        "Canada": "CAD", "Australia": "AUD", "New Zealand": "NZD", "China": "CNY"

    }

    

    def __init__(self, cache_ttl_sec: float = 90.0, cache_path: str | None = None):

        self.cache_ttl_sec = cache_ttl_sec

        #: Per-instance override. Tests use it so a live fetch cannot write
        #: into the operator's real calendar cache (or inherit it).
        if cache_path:
            self.DISK_CACHE_PATH = cache_path

        self._lock = threading.Lock()

        self._cached_news: List[Dict[str, Any]] = []

        self._last_fetch_time: float = 0.0

        #: Last time a fetch actually produced real events (not merely finished).

        self._last_fetch_success_time: float = 0.0

        self._ctx = ssl.create_default_context()

        #: True while a background refresh is running, so a burst of callers

        #: (a scan calls this once per candidate) cannot spawn a thread each.

        self._refresh_in_flight: bool = False

        #: Monotonic stamp of the last "the calendar is fabricated" warning, so

        #: the news page's polling does not flood the log with the same fact.

        self._last_fabrication_warn: float = 0.0

        #: Monotonic stamp before which FairEconomy must not be asked again,

        #: set after an HTTP 429 so the cooldown can actually elapse.

        self._fe_backoff_until: float = 0.0

        #: Same, for MyFxBook, which answers 403 behind a Cloudflare challenge

        #: that urllib cannot pass -- so the request is pure waste on every fetch.

        self._mfb_backoff_until: float = 0.0

        self._load_disk_cache()

        #: Set while a live fetch is running, cleared when it lands. A scan

        #: spawns one MACRO analyst per candidate and the cluster runs 8

        #: workers, so nine callers can reach a cold cache at once -- and

        #: `GLOBAL_NEWS_ENGINE` is a single shared instance. Without this they

        #: all read the backoff as 0.0 before any of them can arm it, all nine

        #: request, all nine get 403, and all nine log: measured as nine

        #: identical "MyFxBook is blocked" warnings inside one second. The nine

        #: concurrent requests are also what EARNS FairEconomy's 429.

        self._fetch_done = threading.Event()

        self._fetch_done.set()          # nothing in flight at construction

        # Live Breaking News Headlines Cache & Single-Flight State

        self.HEADLINES_DISK_CACHE_PATH = os.path.join("data", "news_headlines_cache.json")

        self._cached_headlines: List[Dict[str, Any]] = []

        self._last_headlines_fetch_time: float = 0.0

        self._headlines_lock = threading.Lock()

        self._headlines_refresh_in_flight: bool = False

        self._headlines_fetch_done = threading.Event()

        self._headlines_fetch_done.set()

        self._load_headlines_disk_cache()



    def _load_disk_cache(self) -> None:

        """Hydrates news calendar from persistent disk cache on cold start."""

        try:

            if not os.path.exists(self.DISK_CACHE_PATH):

                return

            with open(self.DISK_CACHE_PATH, "r", encoding="utf-8") as f:

                data = json.load(f)

            version = int(data.get("version", 0) or 0)

            if version != self.CACHE_VERSION:

                logger.info(
                    "Ignoring news disk cache version %s (current %s): "
                    "it may contain fabricated timestamps.",
                    version, self.CACHE_VERSION,
                )

                return

            cached_events = data.get("events", [])

            cached_time = float(data.get("saved_at", 0.0))

            # Weekly economic calendar remains valid for up to 7 days

            if cached_events and (time.time() - cached_time) < 86400 * 7:

                self._cached_news = cached_events

                self._last_fetch_time = cached_time

                logger.info("Loaded %d real economic events from persistent disk cache.", len(cached_events))

        except Exception as e:

            logger.debug("Failed to load news disk cache: %s", e)



    def _save_disk_cache(self, events: List[Dict[str, Any]]) -> None:

        """Atomically persists real economic events to local disk cache."""

        try:

            if not events:

                return

            real_events = [e for e in events if not e.get("is_fallback")]

            if not real_events:

                return

            os.makedirs(os.path.dirname(self.DISK_CACHE_PATH), exist_ok=True)

            payload = {

                "version": self.CACHE_VERSION,

                "saved_at": time.time(),

                "saved_at_iso": datetime.now(timezone.utc).isoformat(),

                "events": real_events

            }

            tmp_path = f"{self.DISK_CACHE_PATH}.tmp"

            with open(tmp_path, "w", encoding="utf-8") as f:

                json.dump(payload, f, default=str, indent=2)

            os.replace(tmp_path, self.DISK_CACHE_PATH)

            logger.debug("Persisted %d real economic events to disk cache.", len(real_events))

        except Exception as e:

            logger.debug("Failed to persist news disk cache: %s", e)





    def has_cached_events(self) -> bool:
        """True when a real (non-synthetic) calendar is already in memory."""
        with self._lock:
            return bool(self._cached_news)

    def get_news_calendar(self, force_refresh: bool = False,

                          max_wait: float | None = None) -> List[Dict[str, Any]]:

        """Returns macro economic events formatted in IST & UTC with 1 most recent on top.



        `max_wait` bounds how long this call may block on the network.



        * ``max_wait=None`` (the default) is the original behaviour: fetch

          synchronously and wait as long as the feed takes. The news page wants

          exactly this, and every existing caller keeps it.

        * ``max_wait=<seconds>`` is for callers on a latency budget. The fetch

          moves to a daemon thread that ALSO refreshes the cache when it lands,

          so a slow feed costs this caller nothing and the next caller benefits.



        WHY THIS EXISTS. `MacroAnalyst` calls this on the decision path, inside

        `ParallelAnalystCluster`, which allows it **2.0s**. This method's socket

        timeouts are 5s and 6s behind a 90s TTL, so on a cache miss the analyst

        could not finish and was replaced by a **fabricated score-50 NEUTRAL

        report**. A cache miss was measured at 1.32s -- 66% of the budget before

        any CPU work. Bounding the wait is what lets the analyst keep real news

        instead of losing the analyst. See `docs/AUDIT-3-TRACKS-2026-09-23.md`.

        """

        with self._lock:

            now = time.time()

            if not force_refresh and (now - self._last_fetch_time) < self.cache_ttl_sec:

                if self._cached_news:

                    NEWS_CALENDAR_SERVED.inc(origin="cached")

                    return self._organize_news_feed(self._cached_news)

                elif self._fe_backoff_until > time.monotonic():

                    NEWS_CALENDAR_SERVED.inc(origin="synthetic")

                    return self._organize_news_feed(self._generate_dynamic_calendar())



        if max_wait is None:

            # Single-flight: a concurrent burst makes ONE network pass, not one

            # per caller. See `_fetch_single_flight` -- nine duplicate requests

            # is a correctness problem, not a performance one.

            events = self._fetch_single_flight(self.SYNC_FOLLOW_TIMEOUT_SEC)

            # Instrumented: `synthetic` here means the deterministic calendar,

            # which drives a hard MACRO gate. Counting it is the only way to

            # know how often that gate is decided by data no feed produced.

            NEWS_CALENDAR_SERVED.inc(origin="live" if events else "synthetic")

            with self._lock:

                return self._organize_news_feed(

                    self._cached_news or self._generate_dynamic_calendar())



        # Bounded path. Prefer stale REAL data to a synthetic calendar, and never

        # block longer than the caller's budget.

        in_flight: threading.Event | None = None

        with self._lock:

            if self._refresh_in_flight:

                # Someone is already fetching. Do not pile on. With a real
                # calendar already in memory, serve it immediately -- the
                # latency-budget callers (MACRO analyst, 2.0s) exist for that.
                if self._cached_news:

                    NEWS_CALENDAR_SERVED.inc(origin="cached")

                    return self._organize_news_feed(self._cached_news)

                # No real data yet. Waiting for the leader is strictly better
                # than returning the synthetic plan: the refresh lands in ~3s
                # and the caller asked to wait up to `max_wait` anyway. This is
                # the cold-start race that made the first /api/news response
                # "ENTIRELY fabricated" while the fetch was completing behind it.
                in_flight = self._fetch_done

            else:

                self._refresh_in_flight = True

                self._fetch_done.clear()

                # A caller on a latency budget (the MACRO analyst has 2.0s) is served

                # whatever is already cached while the refresh runs. Counting that is

                # what separates "we have real news" from "we have a calendar".

                NEWS_CALENDAR_SERVED.inc(origin="synthetic")



        if in_flight is not None:

            in_flight.wait(max(0.0, float(max_wait)))

            with self._lock:

                if self._cached_news:

                    NEWS_CALENDAR_SERVED.inc(origin="cached")

                    return self._organize_news_feed(self._cached_news)

            NEWS_CALENDAR_SERVED.inc(origin="synthetic")

            return self._organize_news_feed(self._generate_dynamic_calendar())



        done = threading.Event()



        def _refresh() -> None:

            try:

                events = self._fetch_all_live_sources()

                with self._lock:

                    if events:

                        self._cached_news = events

                        self._last_fetch_success_time = time.time()

                    self._last_fetch_time = time.time()

            except Exception as exc:

                # Never let a background refresh kill the process or the caller.

                logger.warning("Background news refresh failed (%s: %s); "

                               "keeping the cached calendar.", type(exc).__name__, exc)

                with self._lock:

                    self._last_fetch_time = time.time()

            finally:

                with self._lock:

                    # Signal followers BEFORE clearing the flag. A synchronous

                    # caller that sees the flag still set then waits on an event

                    # that is already set and returns immediately, instead of

                    # becoming a second leader and duplicating this request.

                    self._fetch_done.set()

                    self._refresh_in_flight = False

                done.set()



        threading.Thread(target=_refresh, name="news_refresh", daemon=True).start()

        done.wait(max(0.0, float(max_wait)))



        with self._lock:

            if self._cached_news:

                return self._organize_news_feed(self._cached_news)

        # Cold start with a slow feed: no real data exists yet, so this is the one

        # case that still returns the deterministic calendar. The refresh thread

        # will populate the cache for the next caller.

        return self._organize_news_feed(self._generate_dynamic_calendar())



    def _fetch_single_flight(self, timeout: float | None) -> List[Dict[str, Any]]:

        """Run the live fetch exactly ONCE for a concurrent burst of callers.



        Returns the events now in the cache. An empty list means no live data is

        available and the caller should fall back to the deterministic calendar.



        WHY THIS IS A CORRECTNESS FIX, NOT AN OPTIMISATION. `GLOBAL_NEWS_ENGINE`

        is one shared instance, and a scan reaches it from many threads at once

        (one MACRO analyst per candidate, 8 cluster workers). The per-source

        backoffs cannot deduplicate that burst, because a backoff is only armed

        AFTER the first request fails -- so every thread reads `0.0`, passes the

        guard, and issues its own request before any of them can arm anything.

        Measured on a live start: nine identical "MyFxBook is blocked (HTTP 403)"

        warnings, all stamped 16:36:39.



        Two costs, both real:



        * Nine duplicate requests to a source that is rate-limiting us is what

          EARNS the 429 in the first place. The backoff then treats a symptom we

          caused.

        * Nine threads spend ~0.5s each on a request that cannot succeed, inside

          a 2.0s analyst budget, competing for the GIL.



        The leader does the work and publishes the result to the cache; followers

        wait for the leader rather than repeating it. `timeout` bounds the wait so

        a wedged leader cannot pin a caller forever -- on expiry the follower

        serves whatever is cached, which is exactly what it would have had anyway.

        """

        with self._lock:

            if self._refresh_in_flight:

                waiting_on = self._fetch_done

            else:

                self._refresh_in_flight = True

                self._fetch_done.clear()

                waiting_on = None



        if waiting_on is not None:

            waiting_on.wait(timeout)

            with self._lock:

                return list(self._cached_news)



        try:

            events = self._fetch_all_live_sources()

            with self._lock:

                if events:

                    self._cached_news = events

                    self._last_fetch_success_time = time.time()

                elif not self._cached_news:

                    self._load_disk_cache()

                self._last_fetch_time = time.time()

            return events

        except Exception as exc:

            # A feed that raises must not take the caller down with it; the

            # caller decides what to do with an empty result.

            logger.warning("News fetch failed (%s: %s); keeping the cached calendar.",

                           type(exc).__name__, exc)

            with self._lock:

                self._last_fetch_time = time.time()

                return list(self._cached_news)

        finally:

            # Always release the claim, on every path, or every later caller

            # would follow a leader that no longer exists.

            with self._lock:

                self._fetch_done.set()

                self._refresh_in_flight = False



    def _fetch_biquote_feed(self) -> List[Dict[str, Any]]:
        """Fetches real-time institutional economic calendar from Biquote MQL5 feed."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
        try:
            req = urllib.request.Request(self.BIQUOTE_URL, headers=headers)
            with urllib.request.urlopen(req, context=self._ctx, timeout=6) as resp:
                raw = json.loads(read_bounded(resp).decode("utf-8"))
            parsed = []
            now_dt = datetime.now(timezone.utc)
            for it in raw:
                title = str(it.get("name", "Economic Event")).strip()
                currency = str(it.get("currency", "USD")).upper().strip()
                if currency not in ["USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF", "CNY"]:
                    continue
                imp_raw = str(it.get("importance", "low")).upper()
                impact = "HIGH" if imp_raw == "HIGH" else ("MEDIUM" if imp_raw in ("MEDIUM", "MED") else "LOW")
                time_str = it.get("time", "")
                try:
                    event_dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                diff_seconds = (event_dt - now_dt).total_seconds()
                act = it.get("actual")
                fcst = it.get("forecast")
                prev = it.get("previous")
                parsed.append({
                    "title": title,
                    "currency": currency,
                    "impact": impact,
                    "forecast": str(fcst) if fcst is not None else "—",
                    "previous": str(prev) if prev is not None else "—",
                    "actual": str(act) if act is not None else "—",
                    "diff_seconds": diff_seconds,
                    "event_dt": event_dt,
                    "timestamp_iso": event_dt.isoformat(),
                    "time_quality": "exact",
                    "feed": "biquote",
                    "is_fallback": False,
                    "source": "live_feed"
                })
            return parsed
        except Exception as e:
            logger.debug("Biquote calendar fetch error: %s", e)
            return []

    @classmethod
    def _normalise_event_title(cls, title: Any) -> str:
        """Normalise a release title so the three feeds can be compared.

        Country prefixes differ between feeds ("US Non-Farm Payrolls" vs
        "Non-Farm Payrolls"), as do the period suffixes ("m/m" vs "MoM").
        Only the title is normalised here; the timestamp comparison remains
        separate so the same title on a different day is never collapsed.
        """
        text = str(title or "").strip().lower()
        for country in sorted(cls.COUNTRY_MAP, key=len, reverse=True):
            prefix = country.lower() + " "
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        text = (text.replace("m/m", "mom").replace("q/q", "qoq")
                    .replace("y/y", "yoy").replace("w/w", "wow"))
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    @staticmethod
    def _event_datetime(ev: Dict[str, Any]) -> datetime | None:
        raw = ev.get("event_dt") or ev.get("timestamp_iso")
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                return None
        return None

    @staticmethod
    def _position_value(pos: Any, *names: str, default: Any = None) -> Any:
        """Read a field from a position dict OR a PositionSnapshot object.

        `/api/news` passes `state_manager.positions` straight through, and the
        live state manager stores `PositionSnapshot` objects, not dicts. The
        old dict-only access raised `'PositionSnapshot' object has no attribute
        'get'`, which turned the whole calendar endpoint into a 500 whenever a
        position was open. Attribute access keeps both callers working.
        """
        for name in names:
            if isinstance(pos, dict):
                value = pos.get(name)
            else:
                value = getattr(pos, name, None)
            if value is not None:
                return value
        return default

    @staticmethod
    def _event_has_exact_time(ev: Dict[str, Any]) -> bool:
        """True only when the event's clock time is published, not guessed.

        Rows written by `_organize_news_feed` always carry `time_quality`.
        Legacy/API fixtures may predate it; for those, a numeric `diff_seconds`
        is the only available proof of a real time. A TBA row never qualifies.
        """
        if ev.get("is_tba"):
            return False
        quality = str(ev.get("time_quality") or "").strip().lower()
        if quality:
            return quality == "exact"
        try:
            float(ev.get("diff_seconds"))
        except (TypeError, ValueError):
            return False
        return True

    @classmethod
    def _merge_event(cls, existing: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Merge duplicate coverage, preferring an exact timestamp and filling gaps."""
        out = dict(existing)
        for field in ("actual", "forecast", "previous"):
            if str(out.get(field, "")).strip() in ("", "—", "None"):
                incoming = str(candidate.get(field, "")).strip()
                if incoming not in ("", "—", "None"):
                    out[field] = candidate.get(field)
        existing_quality = str(out.get("time_quality") or "").lower()
        candidate_quality = str(candidate.get("time_quality") or "").lower()
        if candidate_quality == "exact" and existing_quality != "exact":
            for field in ("event_dt", "timestamp_iso", "diff_seconds",
                          "time_quality", "feed", "source"):
                out[field] = candidate.get(field)
        return out

    def _fetch_all_live_sources(self) -> List[Dict[str, Any]]:
        """Fetches from Biquote MQL5, FairEconomy, and MyFxBook, merging and deduplicating."""
        all_events: List[Dict[str, Any]] = []

        # 1. Primary Live Feed: Biquote (MQL5 200 real releases)
        bq_items = self._fetch_biquote_feed()
        if bq_items:
            all_events.extend(bq_items)

        # 2. Secondary Live Feed: FairEconomy
        fe_items = self._fetch_faireconomy_feed()

        # 3. Tertiary Live Feed: MyFxBook
        mfb_items = self._fetch_myfxbook_feed()

        # Merge in source-priority order. A duplicate is the same currency and
        # normalised title within 15 minutes; the same title on another day is
        # a genuinely different release and stays. The old substring test
        # ignored time entirely, so it could merge a weekly release with its
        # daily counterpart -- or miss duplicates whose titles were worded
        # differently.
        for candidate in list(fe_items) + list(mfb_items):
            cand_title = self._normalise_event_title(candidate.get("title"))
            cand_currency = str(candidate.get("currency", "")).upper()
            cand_time = self._event_datetime(candidate)
            matched = None
            for existing in all_events:
                if str(existing.get("currency", "")).upper() != cand_currency:
                    continue
                if self._normalise_event_title(existing.get("title")) != cand_title:
                    continue
                existing_time = self._event_datetime(existing)
                if existing_time is None and cand_time is None:
                    matched = existing
                    break
                if existing_time is None or cand_time is None:
                    continue
                if abs((existing_time - cand_time).total_seconds()) <= 900:
                    matched = existing
                    break
            if matched is None:
                all_events.append(candidate)
            else:
                merged = self._merge_event(matched, candidate)
                matched.clear()
                matched.update(merged)

        if all_events:
            self._save_disk_cache(all_events)

        return all_events



    def _fetch_faireconomy_feed(self) -> List[Dict[str, Any]]:

        headers = {

            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",

            "Accept": "application/json"

        }

        # FairEconomy answers 429 with a "Rate Limited" page and a cooldown of

        # minutes. Retrying on the 90s cache TTL keeps the pressure on and keeps

        # the caller stuck on the hardcoded calendar, so back off instead. This

        # does not make the feed reliable -- it stops us from being the reason

        # it stays down. Measured: isolated call 200 / 10,849 bytes, and at 90s

        # spacing 0, 0, 80, 80 items while recovering from a burst.

        # Read the window under the lock. The arm below happens on another

        # thread's failure, so the pair is a check-then-act that is only correct

        # if the two halves cannot interleave. `_fetch_single_flight` is what

        # actually collapses a concurrent burst to one request; this keeps the

        # state coherent for the sequential case, where a second caller must

        # reliably observe the arm. (Coherence hardening, not the fix -- see the

        # note on the MyFxBook guard below.)

        with self._lock:

            if time.monotonic() < self._fe_backoff_until:

                NEWS_FETCHES.inc(source="faireconomy", outcome="skipped")

                return []

        _fe_timer = NEWS_FETCH_LATENCY.time(source="faireconomy")

        data = None

        last_exc = None

        for endpoint in (self.FAIRECONOMY_URL, self.FAIRECONOMY_MIRROR_URL):

            try:

                req = urllib.request.Request(endpoint, headers=headers)

                with urllib.request.urlopen(req, context=self._ctx, timeout=5) as resp:

                    data = json.loads(read_bounded(resp).decode("utf-8"))

                if data:

                    break

            except Exception as e:

                last_exc = e

                continue



        try:

            if data is None and last_exc:

                raise last_exc

                

            parsed = []

            now_dt = datetime.now(timezone.utc)

            

            for item in data:

                title = item.get("title", "Economic Event").strip()

                currency = item.get("country", item.get("currency", "USD")).upper().strip()

                if currency not in ["USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF", "CNY"]:

                    continue



                impact_raw = str(item.get("impact", "Low")).upper()

                if "HIGH" in impact_raw or "RED" in impact_raw:

                    impact = "HIGH"

                elif "MED" in impact_raw or "ORANGE" in impact_raw or "YELLOW" in impact_raw:

                    impact = "MEDIUM"

                else:

                    impact = "LOW"



                date_str = item.get("date", "")

                event_dt = None

                if date_str:

                    try:

                        event_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))

                    except Exception:

                        pass

                # A missing date is NOT "now". Stamping the fetch time here was
                # how an undated row became a fake live release. Keep the event,
                # but mark its time unknown so every consumer can treat it as
                # "TBA" and the risk gates can ignore it.
                time_quality = "exact" if event_dt is not None else "tba"
                diff_seconds = ((event_dt - now_dt).total_seconds()
                                if event_dt is not None else None)



                parsed.append({

                    "title": title,

                    "currency": currency,

                    "impact": impact,

                    "forecast": str(item.get("forecast", "") or "—").strip() or "—",

                    "previous": str(item.get("previous", "") or "—").strip() or "—",

                    "actual": str(item.get("actual", "") or "").strip() or "—",

                    "diff_seconds": diff_seconds,

                    "event_dt": event_dt,

                    "timestamp_iso": event_dt.isoformat() if event_dt is not None else None,

                    "time_quality": time_quality,

                    "feed": "faireconomy",

                    "is_fallback": False,

                    "source": "live_feed"

                })

            _fe_timer.stop()

            NEWS_FETCHES.inc(source="faireconomy", outcome="ok" if parsed else "empty")

            log_event(logger, logging.DEBUG, "news_source_fetched",

                      f"faireconomy: {len(parsed)} event(s)",

                      source="faireconomy", outcome="ok" if parsed else "empty",

                      duration_ms=_fe_timer.elapsed_ms)

            return parsed

        except Exception as e:

            _fe_timer.stop()

            # A 429 is not a transient glitch -- it is a cooldown. Backing off is

            # what lets the feed come back; hammering is what kept it 429-ing.

            if getattr(e, "code", None) == 429:

                with self._lock:

                    self._fe_backoff_until = time.monotonic() + self.RATE_LIMIT_BACKOFF_SEC

                NEWS_FETCHES.inc(source="faireconomy", outcome="rate_limited")

                logger.warning(

                    "FairEconomy rate-limited (HTTP 429). Backing off for %.0fs; "

                    "the news calendar will be synthetic until then.",

                    self.RATE_LIMIT_BACKOFF_SEC)

            else:

                NEWS_FETCHES.inc(source="faireconomy", outcome="error")

                logger.debug(f"FairEconomy news fetch error: {e}")

            return []



    @staticmethod
    def _parse_relative_time_left(description: str) -> int | None:
        """Parse MyFxBook's human "Time left" cell into seconds.

        The feed used to be parsed with a regex that only accepted
        ``<td>-1451 seconds</td>``. Its actual values are written for humans:
        ``5 min``, ``12h 50min``, ``1 day``. Every other form silently became
        zero, which the caller then turned into ``now`` -- the fabricated
        "LIVE NOW" rows. Returning ``None`` here means "unknown", never "now".
        """
        try:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", description or "", re.S | re.I)
            if not cells:
                return None
            text = re.sub(r"<[^>]+>", " ", cells[0])
            text = re.sub(r"&nbsp;?", " ", text, flags=re.I)
            text = re.sub(r"\s+", " ", text).strip().lower()
            if not text or text in {
                "—", "-", "n/a", "na", "tba", "tentative", "all day", "all-day",
                "time tba", "unknown",
            }:
                return None

            sign = -1 if (text.startswith("-") or " ago" in text) else 1
            units = {
                "w": 604800, "week": 604800, "weeks": 604800,
                "d": 86400, "day": 86400, "days": 86400,
                "h": 3600, "hr": 3600, "hrs": 3600,
                "hour": 3600, "hours": 3600,
                "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
            }
            total = 0.0
            matched = False
            for value, unit in re.findall(
                r"(-?\d+(?:\.\d+)?)\s*(weeks?|w|days?|d|hours?|hrs?|hr|h|"
                r"minutes?|mins?|min|m|seconds?|secs?|sec|s)\b",
                text,
            ):
                total += float(value) * units[unit]
                matched = True
            if not matched:
                return None
            # The regex already captures a leading minus. Only apply the textual
            # sign when the value itself did not carry one, or "-1451 seconds"
            # becomes +1451.
            if total > 0 and sign < 0:
                total = -total
            # A relative "time left" older than a week is not a useful release
            # timestamp; reject it rather than letting one bad cell poison the
            # calendar. Keep the sign: negative means already released.
            total = max(-7 * 86400, min(30 * 86400, total))
            return int(round(total))
        except Exception:
            return None

    def _fetch_myfxbook_feed(self) -> List[Dict[str, Any]]:

        headers = {

            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",

            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

        }

        # Read the window under the lock -- same torn check-then-act as

        # FairEconomy above. This is the one that was measured: nine concurrent

        # callers all read 0.0, all requested, and produced nine identical

        # warnings in one second.

        #

        # Honest scope: this lock is COHERENCE hardening, not the fix. A mutation

        # that removes it turns no test red, because CPython stores a float

        # attribute atomically. What actually collapses the burst is

        # `_fetch_single_flight` -- removing THAT turns four tests red. The lock

        # is kept so the guard/arm pair is correct by construction rather than by

        # relying on the interpreter's guarantees.

        with self._lock:

            if time.monotonic() < self._mfb_backoff_until:

                NEWS_FETCHES.inc(source="myfxbook", outcome="skipped")

                return []

        _mfb_timer = NEWS_FETCH_LATENCY.time(source="myfxbook")

        try:

            req = urllib.request.Request(self.MYFXBOOK_URL, headers=headers)

            with urllib.request.urlopen(req, context=self._ctx, timeout=6) as resp:

                xml_data = read_bounded(resp)

                root = ET.fromstring(xml_data)

                

            parsed = []

            now_dt = datetime.now(timezone.utc)

            

            for it in root.findall('.//item'):

                title = it.findtext('title', '').strip()

                desc = it.findtext('description', '')

                

                currency = None

                clean_title = title

                for c_name, code in self.COUNTRY_MAP.items():

                    if title.startswith(c_name):

                        currency = code

                        remainder = title[len(c_name):].strip()

                        if remainder:

                            clean_title = remainder

                        break

                

                if not currency:

                    continue



                diff_seconds = self._parse_relative_time_left(desc)

                time_quality = "estimated" if diff_seconds is not None else "tba"

                

                impact = "LOW"

                if "high-impact" in desc:

                    impact = "HIGH"

                elif "medium-impact" in desc or "med-impact" in desc:

                    impact = "MEDIUM"

                    

                tds = re.findall(r'<td>(.*?)</td>', desc, re.S)

                prev_val = "—"

                fcst_val = "—"

                act_val = "—"

                if len(tds) >= 5:

                    prev_val = re.sub(r'<[^>]*>', '', tds[2]).strip() or "—"

                    fcst_val = re.sub(r'<[^>]*>', '', tds[3]).strip() or "—"

                    act_val = re.sub(r'<[^>]*>', '', tds[4]).strip() or "—"



                event_dt = (now_dt + timedelta(seconds=diff_seconds)
                            if diff_seconds is not None else None)

                

                parsed.append({

                    "title": clean_title,

                    "currency": currency,

                    "impact": impact,

                    "forecast": fcst_val,

                    "previous": prev_val,

                    "actual": act_val,

                    "diff_seconds": diff_seconds,

                    "event_dt": event_dt,

                    "timestamp_iso": event_dt.isoformat() if event_dt is not None else None,

                    "time_quality": time_quality,

                    "feed": "myfxbook",

                    "is_fallback": False,

                    "source": "live_feed"

                })



            _mfb_timer.stop()

            NEWS_FETCHES.inc(source="myfxbook", outcome="ok" if parsed else "empty")

            return parsed

        except Exception as e:

            _mfb_timer.stop()

            # MyFxBook sits behind a Cloudflare challenge that `urllib` can never

            # pass -- it executes no JavaScript. Measured: HTTP 403, body

            # "Just a moment...", every time. So this is not a transient error,

            # and retrying it on every news fetch costs ~0.5s of the ~1.26s the

            # fetch takes -- 40% of the MACRO analyst's 2.0s budget, spent on a

            # request that cannot succeed. Back off hard; if the challenge ever

            # lapses, the next attempt after the window will find out.

            if getattr(e, "code", None) in (403, 401, 404):

                with self._lock:

                    self._mfb_backoff_until = time.monotonic() + self.BLOCKED_SOURCE_BACKOFF_SEC

                NEWS_FETCHES.inc(source="myfxbook", outcome="blocked")

                logger.warning(

                    "MyFxBook is blocked (HTTP %s, Cloudflare challenge). Skipping it "

                    "for %.0fs -- urllib cannot pass a JavaScript challenge.",

                    getattr(e, "code", None), self.BLOCKED_SOURCE_BACKOFF_SEC)

            else:

                NEWS_FETCHES.inc(source="myfxbook", outcome="error")

                logger.debug(f"MyFxBook news fetch error: {e}")

            return []



    def _organize_news_feed(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:

        """

        Organizes news:

        1. Single MOST RECENT news release on top (index 0).

        2. Followed by all UPCOMING events in ascending chronological order.

        3. Formats all times in Indian Standard Time (IST) & UTC.

        4. Injects deep intelligence metadata for click modal.

        """

        now_dt = datetime.now(timezone.utc)

        recalculated = []



        for e in events:

            time_quality = str(e.get("time_quality") or "").strip().lower()

            event_dt = self._event_datetime(e)

            if time_quality == "tba":

                # A feed that says "time TBA" must never smuggle a timestamp in

                # from a stale cache or a previous merge.

                event_dt = None

            elif not time_quality:

                time_quality = "exact" if event_dt is not None else "tba"

            is_tba = event_dt is None

            if is_tba:

                diff_seconds = None

                diff_minutes = None

            else:

                diff_seconds = (event_dt - now_dt).total_seconds()

                diff_minutes = int(diff_seconds / 60)

            

            currency = e.get("currency", "USD")

            impact = e.get("impact", "HIGH")

            title = e.get("title", e.get("event", "Economic Event"))

            fcst = e.get("forecast", "—")

            prev = e.get("previous", "—")

            act = e.get("actual", "—")

            

            # Live Volatility Shock Window: Starts 5 mins prior to release, ends 15 mins after release

            if is_tba:

                is_live = False

                is_upcoming = False

                is_past = False

                live_start_ist = "TBA"

                live_end_ist = "TBA"

                time_ist_str = "TBA"

                date_ist_str = "TBA"

                time_utc_str = "TBA"

            else:

                live_start_dt = event_dt - timedelta(minutes=5)

                live_end_dt = event_dt + timedelta(minutes=15)

                is_live = (live_start_dt <= now_dt <= live_end_dt)

                is_upcoming = (now_dt < live_start_dt)

                is_past = (now_dt > live_end_dt)

                # Timestamps in IST & UTC

                event_ist = event_dt.astimezone(IST_TZ)

                time_ist_str = event_ist.strftime("%I:%M %p IST")

                date_ist_str = event_ist.strftime("%a %b %d")

                time_utc_str = event_dt.strftime("%H:%M UTC")

                live_start_ist = live_start_dt.astimezone(IST_TZ).strftime("%I:%M %p IST")

                live_end_ist = live_end_dt.astimezone(IST_TZ).strftime("%I:%M %p IST")



            # Affected pairs

            affected = []

            if currency == "USD":

                affected = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"]

            elif currency == "EUR":

                affected = ["EURUSD", "EURGBP", "EURJPY"]

            elif currency == "GBP":

                affected = ["GBPUSD", "EURGBP", "GBPJPY"]

            elif currency == "JPY":

                affected = ["USDJPY", "GBPJPY", "EURJPY"]

            elif currency == "AUD":

                affected = ["AUDUSD", "AUDJPY"]

            elif currency == "CAD":

                affected = ["USDCAD", "CADJPY"]

            else:

                affected = [f"{currency}USD", "XAUUSD"]



            # Detailed Indicator Intelligence

            intel = self._generate_event_intel(title, currency, impact, act, fcst, prev)



            if is_tba:

                status_badge = "🗓 TIME TBA"

                time_display = "TBA — release time not published"

                shock_alert = ("Release time not published by the feed. "

                               "This event cannot open a news blackout window.")

            elif is_live:

                rem_mins = max(1, int((live_end_dt - now_dt).total_seconds() / 60))

                status_badge = f"🔴 LIVE NOW (Ends in {rem_mins}m)"

                time_display = f"LIVE NOW ({live_start_ist} – {live_end_ist})"

                shock_alert = f"⚡ Live Volatility Shock Active: Window open from {live_start_ist} to {live_end_ist}."

            elif is_upcoming:

                if diff_minutes < 60:

                    status_badge = f"⏳ IN {diff_minutes}m"

                    time_display = f"{time_ist_str} (IN {diff_minutes}m)"

                elif diff_minutes < 1440:

                    hours = diff_minutes // 60

                    mins = diff_minutes % 60

                    status_badge = f"⏳ IN {hours}h {mins}m"

                    time_display = f"{date_ist_str}, {time_ist_str}"

                else:

                    days = diff_minutes // 1440

                    status_badge = f"📅 IN {days}d"

                    time_display = f"{date_ist_str}, {time_ist_str}"

                shock_alert = f"Approaching Event Release: Scheduled for {date_ist_str} at {time_ist_str}."

            else:

                abs_mins = abs(diff_minutes)

                if abs_mins < 60:

                    status_badge = f"✓ ENDED ({abs_mins}m ago)"

                    time_display = f"{date_ist_str}, {time_ist_str} (Ended {live_end_ist})"

                elif abs_mins < 1440:

                    status_badge = f"✓ ENDED ({abs_mins // 60}h ago)"

                    time_display = f"{date_ist_str}, {time_ist_str} (Ended {live_end_ist})"

                else:

                    status_badge = f"✓ ENDED ({date_ist_str})"

                    time_display = f"{date_ist_str}, {time_ist_str} (Ended {live_end_ist})"

                shock_alert = f"Event Concluded: Live shock window was active from {live_start_ist} to {live_end_ist}."



            act_display = act if act and act != "—" and act != "None" else ("Upcoming" if is_upcoming else "—")



            recalculated.append({

                "time": time_display,

                "time_ist": f"{date_ist_str}, {time_ist_str}" if event_dt is not None else "TBA",

                "time_utc": f"{event_dt.strftime('%b %d')}, {time_utc_str}" if event_dt is not None else "TBA",

                "currency": currency,

                "impact": impact,

                "event": title,

                "forecast": fcst,

                "previous": prev,

                "actual": act_display,

                "shock_risk": "EXTREME" if (impact == "HIGH" and is_live) else ("HIGH" if impact == "HIGH" else "MODERATE"),

                "shock_alert": shock_alert,

                "affected_pairs": affected,

                "is_live": is_live,

                "is_upcoming": is_upcoming,

                "is_past": is_past,

                "status_badge": status_badge,

                "live_start_ist": live_start_ist,

                "live_end_ist": live_end_ist,

                "diff_seconds": diff_seconds,

                "timestamp_iso": event_dt.isoformat() if event_dt is not None else None,

                # Time provenance. `exact` rows may drive the hard news gate;

                # `estimated` (MyFxBook relative "time left") is display-only;

                # `tba` has no time at all and is never live/upcoming.

                "time_quality": "tba" if is_tba else (time_quality or "exact"),

                "feed": e.get("feed"),

                "is_tba": is_tba,

                # Per-item provenance, PROPAGATED from the input rather than

                # assumed. `get_news_calendar` feeds this method the synthetic

                # generator's output when the live fetch fails, and this method

                # also PADS a short real feed with synthetic events (see the

                # `< 4 upcoming` branch below). Stamping everything live is how

                # six hardcoded events reached the MACRO analyst looking real.

                "is_fallback": bool(e.get("is_fallback", False)),

                "source": ("synthetic_calendar" if e.get("is_fallback")

                           else "live_feed"),

                # Deep Intelligence fields for click modal

                "category": intel["category"],

                "description": intel["description"],

                "impact_analysis": intel["impact_analysis"],

                "deviation_summary": intel["deviation_summary"],

                "direction_bias": intel["direction_bias"],

                "execution_warning": intel["execution_warning"]

            })



        # Separate past and upcoming events

        past_events = [e for e in recalculated if e.get("is_past")]

        live_events = [e for e in recalculated if e.get("is_live")]

        upcoming_events = [e for e in recalculated if e.get("is_upcoming")]

        tba_events = [e for e in recalculated if e.get("is_tba")]



        # Sort past events descending (most recent release first)

        past_events.sort(key=lambda x: x["diff_seconds"], reverse=True)

        # Sort upcoming events ascending (nearest upcoming release first)

        upcoming_events.sort(key=lambda x: x["diff_seconds"])



        ordered_feed = []

        # 1. TOP ITEM: If there is a genuinely live event, show it; otherwise nearest upcoming event
        if live_events:
            for lev in live_events:
                ordered_feed.append(lev)
        elif upcoming_events:
            ordered_feed.append(upcoming_events[0])
            upcoming_events = upcoming_events[1:]
        elif past_events:
            most_recent = past_events[0]
            most_recent["is_most_recent"] = True
            most_recent["status_badge"] = f"⚡ LATEST RELEASE (Ended at {most_recent.get('live_end_ist', '')})"
            ordered_feed.append(most_recent)
            past_events = past_events[1:]

        # 2. UPCOMING ITEMS: All remaining upcoming events in ascending chronological order
        for u in upcoming_events:
            if not any(x.get("event") == u.get("event") and x.get("time_ist") == u.get("time_ist") for x in ordered_feed):
                ordered_feed.append(u)

        # 3. If there are fewer than 4 upcoming events, append upcoming institutional calendar items
        if sum(1 for x in ordered_feed if x.get("is_upcoming")) < 4:
            fallback_schedule = self._generate_dynamic_calendar()
            for fb in fallback_schedule:
                if fb.get("diff_seconds", 0) > 0:
                    fb_obj = self._format_dynamic_item(fb, now_dt)
                    if not any(x.get("event") == fb_obj.get("event") for x in ordered_feed):
                        ordered_feed.append(fb_obj)

        # 4. RECENT PAST ITEMS: Keep all recent releases (up to past 48h / 50 items) so trader can inspect today's outcomes
        recent_past = [p for p in past_events if p.get("diff_seconds", -999999) >= -172800][:50]
        for p in recent_past:
            if not any(x.get("event") == p.get("event") and x.get("time_ist") == p.get("time_ist") for x in ordered_feed):
                ordered_feed.append(p)

        # 5. TBA ITEMS: shown last and only once. They carry no timestamp, no

        # countdown, and are never eligible for a news blackout window.

        for t in tba_events:
            if not any(x.get("event") == t.get("event") and x.get("currency") == t.get("currency") for x in ordered_feed):
                ordered_feed.append(t)

        feed = ordered_feed[:150]



        # Say it out loud. This used to be silent: the feed fell back to the

        # hardcoded plan (or padded a short real feed with it) and every

        # consumer -- `MacroAnalyst` above all -- read the result as real

        # releases. Rate-limited so the news page's polling does not flood.

        n_fab = sum(1 for x in feed if x.get("is_fallback"))

        if n_fab:

            now_mono = time.monotonic()

            if now_mono - self._last_fabrication_warn > 300.0:

                self._last_fabrication_warn = now_mono

                logger.warning(

                    "News calendar is %s: %d of %d events are SYNTHETIC "

                    "(source=%r). The live fetch returned nothing, so these are "

                    "hardcoded events, not releases. Anything deriving signal "

                    "from them must check `is_fallback` first.",

                    "ENTIRELY fabricated" if n_fab == len(feed) else "PARTLY fabricated",

                    n_fab, len(feed), "synthetic_calendar")



        return feed



    def _generate_event_intel(self, title: str, currency: str, impact: str, actual: str, forecast: str, previous: str) -> Dict[str, str]:

        """Generates institutional analysis, deviation metrics, and directional bias for the modal."""

        category = "Macroeconomic Telemetry"

        desc = "High-frequency macroeconomic data release tracked by institutional trading desks, sovereign wealth funds, and central banks."

        impact_analysis = f"High liquidity catalyst for {currency} pairs and cross-asset safe havens (Gold / Treasuries)."

        

        t_lower = title.lower()

        for k, v in EVENT_KNOWLEDGE.items():

            if k in t_lower:

                category = v["category"]

                desc = v["desc"]

                impact_analysis = v["impact"]

                break



        # Calculate exact numerical deviation

        deviation_summary = "In Line / Pending Release"

        direction_bias = "NEUTRAL / DATA PENDING"

        

        if actual and actual not in ["—", "Upcoming", "None", ""]:

            if forecast and forecast not in ["—", "None", ""]:

                try:

                    act_num = float(re.sub(r'[^\d.-]', '', actual))

                    fcst_num = float(re.sub(r'[^\d.-]', '', forecast))

                    diff = round(act_num - fcst_num, 2)

                    if diff > 0:

                        deviation_summary = f"+{diff} Above Forecast (Hawkish / Stronger Outcome)"

                        direction_bias = f"BULLISH {currency} / BEARISH XAUUSD (Strong Data Lift)"

                    elif diff < 0:

                        deviation_summary = f"{diff} Below Forecast (Dovish / Weaker Outcome)"

                        direction_bias = f"BEARISH {currency} / BULLISH XAUUSD (Weak Data Boost)"

                    else:

                        deviation_summary = "0.0 Exactly In Line with Forecast"

                        direction_bias = f"NEUTRAL {currency} (Priced-In Equilibrium)"

                except Exception:

                    deviation_summary = f"Actual ({actual}) vs Forecast ({forecast})"

                    direction_bias = f"MARKET DIGESTING {currency} METRIC"

            else:

                deviation_summary = f"Reported Actual: {actual} (Previous: {previous})"

                direction_bias = f"DIRECTIONAL FLOW ON {currency}"



        execution_warning = (

            "⚠️ SPREAD & SLIPPAGE WARNING: Institutional market makers widen bid-ask spreads significantly during high-impact news releases. "

            "Automated stop loss buffers and entry quality filters are actively heightened."

        ) if impact == "HIGH" else (

            "ℹ️ MODERATE VOLATILITY: Normal liquidity absorption expected across trading sessions."

        )



        return {

            "category": category,

            "description": desc,

            "impact_analysis": impact_analysis,

            "deviation_summary": deviation_summary,

            "direction_bias": direction_bias,

            "execution_warning": execution_warning

        }



    def _format_dynamic_item(self, fb: Dict[str, Any], now_dt: datetime) -> Dict[str, Any]:

        event_dt = fb.get("event_dt", now_dt)

        diff_seconds = (event_dt - now_dt).total_seconds()

        diff_minutes = int(diff_seconds / 60)

        currency = fb.get("currency", "USD")

        impact = fb.get("impact", "HIGH")

        title = fb.get("title", "Economic Event")

        fcst = fb.get("forecast", "—")

        prev = fb.get("previous", "—")

        act = fb.get("actual", "—")

        

        live_start_dt = event_dt - timedelta(minutes=5)

        live_end_dt = event_dt + timedelta(minutes=15)



        is_live = (live_start_dt <= now_dt <= live_end_dt)

        is_upcoming = (now_dt < live_start_dt)

        is_past = (now_dt > live_end_dt)



        event_ist = event_dt.astimezone(IST_TZ)

        time_ist_str = event_ist.strftime("%I:%M %p IST")

        date_ist_str = event_ist.strftime("%a %b %d")

        time_utc_str = event_dt.strftime("%H:%M UTC")



        live_start_ist = live_start_dt.astimezone(IST_TZ).strftime("%I:%M %p IST")

        live_end_ist = live_end_dt.astimezone(IST_TZ).strftime("%I:%M %p IST")



        if is_live:

            rem_mins = max(1, int((live_end_dt - now_dt).total_seconds() / 60))

            status_badge = f"🔴 LIVE NOW (Ends in {rem_mins}m)"

            time_display = f"LIVE NOW ({live_start_ist} – {live_end_ist})"

            shock_alert = f"⚡ Live Volatility Shock Active: Window open from {live_start_ist} to {live_end_ist}."

        elif is_upcoming:

            if diff_minutes < 60:

                status_badge = f"⏳ IN {diff_minutes}m"

                time_display = f"{time_ist_str} (IN {diff_minutes}m)"

            elif diff_minutes < 1440:

                hours = diff_minutes // 60

                mins = diff_minutes % 60

                status_badge = f"⏳ IN {hours}h {mins}m"

                time_display = f"{date_ist_str}, {time_ist_str}"

            else:

                days = diff_minutes // 1440

                status_badge = f"📅 IN {days}d"

                time_display = f"{date_ist_str}, {time_ist_str}"

            shock_alert = f"Approaching Event Release: Scheduled for {date_ist_str} at {time_ist_str}."

        else:

            abs_mins = abs(diff_minutes)

            if abs_mins < 60:

                status_badge = f"✓ ENDED ({abs_mins}m ago)"

            elif abs_mins < 1440:

                status_badge = f"✓ ENDED ({abs_mins // 60}h ago)"

            else:

                status_badge = f"✓ ENDED ({date_ist_str})"

            time_display = f"{date_ist_str}, {time_ist_str} (Ended {live_end_ist})"

            shock_alert = f"Event Concluded: Live shock window was active from {live_start_ist} to {live_end_ist}."



        act_display = act if act and act != "—" and act != "None" else ("Upcoming" if is_upcoming else "—")



        affected = []

        if currency == "USD":

            affected = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"]

        elif currency == "EUR":

            affected = ["EURUSD", "EURGBP", "EURJPY"]

        elif currency == "GBP":

            affected = ["GBPUSD", "EURGBP", "GBPJPY"]

        elif currency == "JPY":

            affected = ["USDJPY", "GBPJPY", "EURJPY"]

        else:

            affected = [f"{currency}USD", "XAUUSD"]



        intel = self._generate_event_intel(title, currency, impact, act_display, fcst, prev)



        return {

            "time": time_display,

            "time_ist": f"{date_ist_str}, {time_ist_str}",

            "time_utc": f"{event_dt.strftime('%b %d')}, {time_utc_str}",

            "currency": currency,

            "impact": impact,

            "event": title,

            "forecast": fcst,

            "previous": prev,

            "actual": act_display,

            "shock_risk": "EXTREME" if (impact == "HIGH" and is_live) else ("HIGH" if impact == "HIGH" else "MODERATE"),

            "shock_alert": shock_alert,

            "affected_pairs": affected,

            "is_live": is_live,

            "is_upcoming": is_upcoming,

            "is_past": is_past,

            "status_badge": status_badge,

            "live_start_ist": live_start_ist,

            "live_end_ist": live_end_ist,

            "diff_seconds": diff_seconds,

            "timestamp_iso": event_dt.isoformat(),

            # Built only from the hardcoded plan, so always fabricated. This is

            # the path that PADS a short real feed to a minimum of 4 upcoming

            # events, which is why a working feed can still carry invented ones.

            "is_fallback": True,

            "source": "synthetic_calendar",

            # Same shape as a live row, so consumers do not have to special-case

            # the synthetic plan while iterating a merged feed.

            "time_quality": "exact",

            "feed": None,

            "is_tba": False,

            "category": intel["category"],

            "description": intel["description"],

            "impact_analysis": intel["impact_analysis"],

            "deviation_summary": intel["deviation_summary"],

            "direction_bias": intel["direction_bias"],

            "execution_warning": intel["execution_warning"]

        }



    def _generate_dynamic_calendar(self) -> List[Dict[str, Any]]:

        """Fallback institutional macroeconomic calendar anchored to real-world schedule."""

        now_dt = datetime.now(timezone.utc)

        

        # Calculate next Monday 08:00 UTC (13:30 IST) for upcoming releases

        days_to_monday = (7 - now_dt.weekday()) % 7

        if days_to_monday == 0 and now_dt.weekday() == 0:

            monday_base = now_dt.replace(hour=8, minute=0, second=0, microsecond=0)

        else:

            target_days = 7 if days_to_monday == 0 else days_to_monday

            monday_base = (now_dt + timedelta(days=target_days)).replace(hour=8, minute=0, second=0, microsecond=0)



        # Anchor past events to Friday's actual market releases

        days_since_friday = (now_dt.weekday() - 4) % 7

        friday_base = (now_dt - timedelta(days=days_since_friday)).replace(second=0, microsecond=0)



        plan = [

            # Real Historical Friday Releases

            {

                "event_dt": friday_base.replace(hour=13, minute=45),

                "currency": "USD", "impact": "HIGH",

                "event": "US S&P Global Composite Flash PMI",

                "forecast": "51.4", "previous": "51.1", "actual": "51.8"

            },

            {

                "event_dt": friday_base.replace(hour=17, minute=0),

                "currency": "USD", "impact": "MEDIUM",

                "event": "US Baker Hughes Oil Rig Count",

                "forecast": "485", "previous": "488", "actual": "483"

            },

            {

                "event_dt": friday_base.replace(hour=18, minute=0),

                "currency": "USD", "impact": "HIGH",

                "event": "Federal Reserve Jackson Hole Monetary Assessment",

                "forecast": "5.25%", "previous": "5.25%", "actual": "Reported"

            },

            # Real Upcoming Next Week Market Releases

            {

                "event_dt": monday_base,

                "currency": "EUR", "impact": "HIGH",

                "event": "German Ifo Business Climate Index",

                "forecast": "87.2", "previous": "87.0", "actual": "—"

            },

            {

                "event_dt": monday_base + timedelta(hours=6),

                "currency": "USD", "impact": "HIGH",

                "event": "US Dallas Fed Manufacturing Activity Index",

                "forecast": "-12.0", "previous": "-13.5", "actual": "—"

            },

            {

                "event_dt": monday_base + timedelta(days=1, hours=6),

                "currency": "USD", "impact": "HIGH",

                "event": "US CB Consumer Confidence & Job Openings (JOLTS)",

                "forecast": "100.5", "previous": "100.3", "actual": "—"

            },

            {

                "event_dt": monday_base + timedelta(days=2, hours=4, minutes=30),

                "currency": "USD", "impact": "HIGH",

                "event": "US Preliminary GDP (q/q) & Core PCE Prices",

                "forecast": "2.8%", "previous": "2.8%", "actual": "—"

            },

            {

                "event_dt": monday_base + timedelta(days=3, hours=4, minutes=30),

                "currency": "USD", "impact": "HIGH",

                "event": "US Initial Jobless Claims & Trade Balance",

                "forecast": "228K", "previous": "231K", "actual": "—"

            },

            {

                "event_dt": monday_base + timedelta(days=4, hours=4, minutes=30),

                "currency": "USD", "impact": "HIGH",

                "event": "US Core PCE Price Index (m/m & y/y)",

                "forecast": "0.2%", "previous": "0.2%", "actual": "—"

            }

        ]



        res = []

        for p in plan:

            event_dt = p["event_dt"]

            diff_seconds = (event_dt - now_dt).total_seconds()

            res.append({

                "title": p["event"],

                "currency": p["currency"],

                "impact": p["impact"],

                "forecast": p["forecast"],

                "previous": p["previous"],

                "actual": p["actual"],

                "diff_seconds": diff_seconds,

                "event_dt": event_dt,

                "timestamp_iso": event_dt.isoformat(),

                # This event is INVENTED. The plan above is a hardcoded list

                # anchored to the current week's calendar arithmetic, not to any

                # feed. `tradingview_provider` set the precedent: a labelled

                # fallback beats an unlabelled fabrication. Without this flag the

                # items below are indistinguishable from real releases, and

                # `_organize_news_feed` used to stamp them as live data.

                "is_fallback": True,

                "source": "synthetic_calendar",

            })

        return res



    def evaluate_post_news_sweep_reaction(

        self,

        symbol: str,

        sweep_detected: bool,

        sweep_type: str,

        sweep_magnitude_pips: float,

        lookback_minutes: int = 45

    ) -> Dict[str, Any]:

        """

        Cross-references recent high-impact macroeconomic releases with active order-book liquidity sweeps

        to detect the classic institutional 'Stop-Hunt & Reverse' trade pattern.

        """

        if not sweep_detected or sweep_magnitude_pips <= 0:

            return {"news_reversal_setup": False, "conviction_boost": 0.0, "reason": "No active sweep"}



        calendar = self.get_news_calendar()

        # `is_fallback` items are the hardcoded plan, not releases. This method

        # hands out `conviction_boost` 0.20 to BOTH win probabilities and +8.0

        # to `ai_score` (decision_engine.py:781-788), so a fabricated event must

        # never qualify. The plan's three Friday-anchored "past" entries carry

        # real-looking actuals, and on a Friday within 45 minutes of 13:45 /

        # 17:00 / 18:00 UTC they satisfy every other condition here.

        recent_news = [

            ev for ev in calendar 

            if ev.get("is_past") and ev.get("diff_seconds", 0) >= (-lookback_minutes * 60)

            and ev.get("impact") in ["HIGH", "MEDIUM"]

            and any(p in symbol for p in ev.get("affected_pairs", []))

            and not ev.get("is_fallback")

        ]



        if not recent_news:

            return {"news_reversal_setup": False, "conviction_boost": 0.0, "reason": "No recent high-impact release"}



        latest_news = recent_news[0]

        # Check if the liquidity sweep swept the initial impulse extreme (stop hunt)

        # Buy-side sweep (highs swept) sets up a SELL reversal; sell-side sweep (lows swept) sets up a BUY reversal.

        sweep_upper = str(sweep_type or "").upper()

        is_buy_side_sweep = ("BUY_SIDE" in sweep_upper or "BEARISH_SWEEP" in sweep_upper or "HIGH" in sweep_upper)

        reversal_direction = "SELL" if is_buy_side_sweep else "BUY"



        return {

            "news_reversal_setup": True,

            "catalyst_event": latest_news.get("event"),

            "event_currency": latest_news.get("currency"),

            "reversal_bias": reversal_direction,

            "sweep_magnitude_pips": round(sweep_magnitude_pips, 1),

            "conviction_boost": 0.20,

            "reason": (

                f"Post-News Stop Hunt: {latest_news.get('event')} triggered a {sweep_type} sweep "

                f"({sweep_magnitude_pips:.1f} pips). Institutional reversal favoring {reversal_direction}."

            )

        }



    # ── Live Breaking Headlines & Sentiment Engine ──────────────────────────

    HEADLINE_FEEDS: ClassVar[List[Dict[str, str]]] = [

        {"name": "ForexLive", "url": "https://www.forexlive.com/feed/news", "category": "FOREX"},

        {"name": "FXStreet", "url": "https://www.fxstreet.com/rss/news", "category": "FOREX"},

        {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "category": "CRYPTO"},

        {"name": "ForexLive Crypto", "url": "https://www.forexlive.com/feed/cryptocurrency", "category": "CRYPTO"},

        {"name": "Investing.com", "url": "https://www.investing.com/rss/news.rss", "category": "MARKETS"},

    ]



    BULLISH_KEYWORDS: ClassVar[set] = {

        "surge", "surges", "soar", "soars", "rally", "rallies", "beat", "beats", "exceed", "exceeds",

        "acceleration", "accelerates", "hike", "hikes", "tighten", "tightening", "hawkish", "robust",

        "expansion", "expanding", "strong", "stronger", "inflow", "inflows", "gain", "gains", "jump", "jumps",

        "climb", "climbs", "peak", "upside", "optimism", "recovery", "outperform", "outperforms", "bull", "bullish"

    }



    BEARISH_KEYWORDS: ClassVar[set] = {

        "slump", "slumps", "plunge", "plunges", "fall", "falls", "drop", "drops", "miss", "misses",

        "slowdown", "slows", "cut", "cuts", "ease", "easing", "dovish", "weak", "weaker", "contraction",

        "recession", "outflow", "outflows", "loss", "losses", "retreat", "retreats", "decline", "declines",

        "downside", "pessimism", "struggle", "struggles", "underperform", "deficit", "tumble", "tumbles",

        "crash", "crashes", "warning", "crisis", "fear", "fears", "panic", "bear", "bearish"

    }



    BREAKING_KEYWORDS: ClassVar[set] = {

        "breaking", "urgent", "flash", "alert", "just in", "bulletin", "fomc", "powell", "war", "strike",

        "attack", "sanction", "sanctions", "tariff", "tariffs", "emergency", "crisis", "default", "sec"

    }



    ASSET_KEYWORDS_MAP: ClassVar[Dict[str, List[str]]] = {

        "USD": ["fed", "powell", "dollar", "usd", "fomc", "treasury", "yields", "nonfarm", "nfp", "cpi", "pce", "wall street", "us "],

        "EUR": ["ecb", "lagarde", "euro", "eur", "bunds", "germany", "eurozone", "france"],

        "GBP": ["boe", "bailey", "pound", "gbp", "sterling", "gilt", "gilts", "uk "],

        "JPY": ["boj", "ueda", "yen", "jpy", "japan", "jgb"],

        "AUD": ["australian", "aussie", "aud", "rba", "australia"],

        "CAD": ["cad", "loonie", "boc", "canada"],

        "NZD": ["nzd", "kiwi", "rbnz", "new zealand"],

        "CHF": ["chf", "snb", "swiss", "franc"],

        "GOLD": ["gold", "bullion", "xau", "xauusd", "precious metal", "safe-haven", "safe haven"],

        "CRYPTO": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "sec", "etf", "binance", "coinbase"],

        "OIL": ["crude", "oil", "wti", "brent", "opec", "energy", "petroleum", "rig count"]

    }



    def _load_headlines_disk_cache(self) -> None:

        """Hydrates breaking news headlines from persistent disk cache on cold start."""

        try:

            if not os.path.exists(self.HEADLINES_DISK_CACHE_PATH):

                return

            with open(self.HEADLINES_DISK_CACHE_PATH, "r", encoding="utf-8") as f:

                data = json.load(f)

            cached = data.get("headlines", [])

            cached_time = float(data.get("saved_at", 0.0))

            if cached and (time.time() - cached_time) < 86400 * 2:  # 48 hours

                self._cached_headlines = cached

                self._last_headlines_fetch_time = cached_time

                logger.info("Loaded %d real-time headlines from persistent disk cache.", len(cached))

        except Exception as e:

            logger.debug("Failed to load headlines disk cache: %s", e)



    def _save_headlines_disk_cache(self, headlines: List[Dict[str, Any]]) -> None:

        """Persists breaking headlines to local disk cache."""

        try:

            if not headlines:

                return

            os.makedirs(os.path.dirname(self.HEADLINES_DISK_CACHE_PATH), exist_ok=True)

            payload = {

                "saved_at": time.time(),

                "saved_at_iso": datetime.now(timezone.utc).isoformat(),

                "headlines": headlines[:100]

            }

            tmp = f"{self.HEADLINES_DISK_CACHE_PATH}.tmp"

            with open(tmp, "w", encoding="utf-8") as f:

                json.dump(payload, f, default=str, indent=2)

            os.replace(tmp, self.HEADLINES_DISK_CACHE_PATH)

        except Exception as e:

            logger.debug("Failed to save headlines disk cache: %s", e)



    @classmethod

    def analyze_headline_sentiment(cls, title: str, summary: str = "") -> Dict[str, Any]:

        """

        Rule-based institutional NLP sentiment analyzer and multi-asset tagger.

        Evaluates headline text for hawkish/dovish, risk-on/risk-off sentiment,

        affected currencies, and catalyst urgency.

        """

        text = (str(title or "") + " " + str(summary or "")).lower()

        words = set(re.findall(r"\b[a-z0-9\-]+\b", text))



        bull_count = sum(1 for kw in cls.BULLISH_KEYWORDS if kw in words)

        bear_count = sum(1 for kw in cls.BEARISH_KEYWORDS if kw in words)

        is_breaking = any(kw in text for kw in cls.BREAKING_KEYWORDS)



        total = bull_count + bear_count

        if total > 0:

            raw_score = (bull_count - bear_count) / total

            sentiment_score = round(raw_score * 100.0, 1)

        else:

            sentiment_score = 0.0



        if sentiment_score >= 15.0:

            sentiment = "BULLISH"

        elif sentiment_score <= -15.0:

            sentiment = "BEARISH"

        else:

            sentiment = "NEUTRAL"



        urgency = "BREAKING" if is_breaking else ("HIGH_PRIORITY" if abs(sentiment_score) >= 50.0 else "ROUTINE")



        affected_currencies: List[str] = []

        affected_assets: List[str] = []

        for asset, kws in cls.ASSET_KEYWORDS_MAP.items():

            if any(kw in text for kw in kws):

                if asset in ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"]:

                    if asset not in affected_currencies:

                        affected_currencies.append(asset)

                    if "FOREX" not in affected_assets:

                        affected_assets.append("FOREX")

                elif asset in ["GOLD", "OIL"]:

                    if "COMMODITY" not in affected_assets:

                        affected_assets.append("COMMODITY")

                    if asset == "GOLD" and "USD" not in affected_currencies:

                        affected_currencies.append("USD")

                elif asset == "CRYPTO":

                    if "CRYPTO" not in affected_assets:

                        affected_assets.append("CRYPTO")



        return {

            "sentiment": sentiment,

            "sentiment_score": sentiment_score,

            "urgency": urgency,

            "affected_currencies": affected_currencies or ["USD"],

            "affected_assets": affected_assets or ["FOREX"]

        }



    def _fetch_all_rss_headlines(self) -> List[Dict[str, Any]]:

        """Ingests live RSS feeds across ForexLive, FXStreet, CoinDesk, Investing.com."""

        all_headlines = []

        seen_keys = set()

        headers = {

            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",

            "Accept": "application/rss+xml, application/xml, text/xml, */*"

        }



        for feed in self.HEADLINE_FEEDS:

            try:

                req = urllib.request.Request(feed["url"], headers=headers)

                with urllib.request.urlopen(req, context=self._ctx, timeout=4) as resp:

                    xml_data = read_bounded(resp)

                if not xml_data:

                    continue

                root = ET.fromstring(xml_data)

                items = root.findall(".//item")

                for item in items[:25]:

                    title = (item.findtext("title") or "").strip()

                    if not title or len(title) < 8:

                        continue

                    # Deduplicate by normalized text key

                    key = re.sub(r"[^a-z0-9]", "", title.lower())[:60]

                    if key in seen_keys:

                        continue

                    seen_keys.add(key)



                    pub_date = (item.findtext("pubDate") or "").strip()

                    link = (item.findtext("link") or "").strip()

                    desc = (item.findtext("description") or "").strip()

                    # Strip basic HTML tags from description if present

                    clean_desc = re.sub(r"<[^>]+>", "", desc).strip()[:240]



                    nlp = self.analyze_headline_sentiment(title, clean_desc)



                    # Compute relative timestamp if parseable

                    published_iso = ""

                    diff_min = 0

                    if pub_date:

                        try:

                            import email.utils

                            parsed_tuple = email.utils.parsedate_to_datetime(pub_date)

                            if parsed_tuple:

                                published_iso = parsed_tuple.isoformat()

                                diff_min = max(0, int((datetime.now(timezone.utc) - parsed_tuple).total_seconds() / 60))

                        except Exception:

                            published_iso = pub_date



                    all_headlines.append({

                        "id": key,

                        "title": title,

                        "source": feed["name"],

                        "category": feed["category"],

                        "pub_date": pub_date,

                        "published_iso": published_iso,

                        "age_minutes": diff_min,

                        "age_str": f"{diff_min}m ago" if diff_min < 60 else (f"{diff_min // 60}h ago" if diff_min < 1440 else "1d+ ago"),

                        "link": link,

                        "summary": clean_desc,

                        "sentiment": nlp["sentiment"],

                        "sentiment_score": nlp["sentiment_score"],

                        "urgency": nlp["urgency"],

                        "affected_currencies": nlp["affected_currencies"],

                        "affected_assets": nlp["affected_assets"]

                    })

            except Exception as e:

                logger.debug("Failed fetching RSS feed %s: %s", feed["name"], e)



        return all_headlines



    def get_live_headlines(

        self,

        force_refresh: bool = False,

        max_wait: float = 2.0,

        limit: int = 50,

        currency: Optional[str] = None,

        asset_class: Optional[str] = None,

        sentiment: Optional[str] = None

    ) -> List[Dict[str, Any]]:

        """

        Returns real-time breaking financial news headlines with sentiment scoring.

        Uses cached headlines if fresh (< 90s) or asynchronous single-flight fetch.

        """

        now = time.time()

        with self._headlines_lock:

            is_fresh = (now - self._last_headlines_fetch_time) < 180.0 and bool(self._cached_headlines)

            if not force_refresh and is_fresh:

                items = list(self._cached_headlines)

                return self._filter_headlines(items, limit, currency, asset_class, sentiment)



            if self._headlines_refresh_in_flight:

                items = list(self._cached_headlines)

                return self._filter_headlines(items, limit, currency, asset_class, sentiment)



            self._headlines_refresh_in_flight = True

            self._headlines_fetch_done.clear()



        def _do_fetch():

            try:

                new_items = self._fetch_all_rss_headlines()

                with self._headlines_lock:

                    if new_items:

                        self._cached_headlines = new_items

                        self._save_headlines_disk_cache(new_items)

                    self._last_headlines_fetch_time = time.time()

            except Exception as e:

                logger.warning("Background headlines fetch failed: %s", e)

            finally:

                with self._headlines_lock:

                    self._headlines_fetch_done.set()

                    self._headlines_refresh_in_flight = False



        threading.Thread(target=_do_fetch, name="headlines_refresh", daemon=True).start()

        if max_wait > 0.0:
            self._headlines_fetch_done.wait(max(0.05, float(max_wait)))



        with self._headlines_lock:

            items = list(self._cached_headlines)

            return self._filter_headlines(items, limit, currency, asset_class, sentiment)



    @staticmethod

    def _filter_headlines(

        items: List[Dict[str, Any]],

        limit: int = 50,

        currency: Optional[str] = None,

        asset_class: Optional[str] = None,

        sentiment: Optional[str] = None

    ) -> List[Dict[str, Any]]:

        filtered = items

        if currency and currency.upper() != "ALL":

            c_up = currency.upper()

            filtered = [h for h in filtered if c_up in h.get("affected_currencies", [])]

        if asset_class and asset_class.upper() != "ALL":

            a_up = asset_class.upper()

            filtered = [h for h in filtered if a_up in h.get("affected_assets", []) or a_up == h.get("category", "")]

        if sentiment and sentiment.upper() != "ALL":

            s_up = sentiment.upper()

            filtered = [h for h in filtered if h.get("sentiment") == s_up]

        return filtered[:limit]



    def get_at_risk_positions(

        self,

        open_positions: Optional[List[Dict[str, Any]]],

        lookforward_minutes: int = 60,

        calendar: Optional[List[Dict[str, Any]]] = None,

    ) -> List[Dict[str, Any]]:

        """

        Cross-references currently active portfolio positions against upcoming

        high/medium impact macroeconomic events to evaluate News Sentinel risk.

        Only events with an EXACT published time are eligible. An estimated or

        TBA time must never trigger a bank/protect action: banking on a guess

        is how a position gets closed an hour before the release it feared.

        """

        if not open_positions:

            return []



        if calendar is None:

            calendar = self.get_news_calendar(max_wait=0.0)

        if not calendar:

            return []



        # Find events within lookforward window or in active shock window

        # (-900s is 15 min past release; lookforward is 60 min ahead)

        max_sec = lookforward_minutes * 60

        impending_events = []

        for ev in calendar:

            if ev.get("impact") not in ("HIGH", "MEDIUM"):

                continue

            if ev.get("is_past"):

                continue

            if not self._event_has_exact_time(ev):

                continue

            try:

                diff = float(ev.get("diff_seconds"))

            except (TypeError, ValueError):

                continue

            if -900 <= diff <= max_sec:

                impending_events.append(ev)



        if not impending_events:

            return []



        at_risk: List[Dict[str, Any]] = []

        for pos in open_positions:

            sym = str(self._position_value(pos, "symbol", default="")).upper()

            matched_events = []

            for ev in impending_events:

                ev_curr = str(ev.get("currency", "")).upper()

                affected = ev.get("affected_pairs", [])

                if (ev_curr and ev_curr in sym) or (sym in affected) or ("USD" in sym and ev_curr in ("USD", "XAU")):

                    matched_events.append(ev)



            if not matched_events:

                continue



            # Sort matched events by closest time

            matched_events.sort(key=lambda x: abs(float(x.get("diff_seconds", 0))))

            top_event = matched_events[0]



            peak_r = float(self._position_value(
                pos, "mfe", "peak_r", "max_favorable_r", default=0.0) or 0.0)

            current_r = float(self._position_value(
                pos, "current_r", "r_multiple", default=0.0) or 0.0)

            is_be_locked = bool(self._position_value(
                pos, "is_be_locked", "be_locked", default=False))



            # Determine News Sentinel projected action

            # Sentinel rules: NEWS_BANK_R = 0.60, NEWS_BANK_CURRENT_R = 0.20, NEWS_PROTECT_FLOOR_R = -0.25

            if is_be_locked:

                sentinel_action = "BE_PROTECTED"

                sentinel_reason = "Stop Loss locked at breakeven; downside insulated against macro shock."

            elif peak_r >= 0.60 and current_r >= 0.20:

                sentinel_action = "AUTO_BANK_PROFIT"

                sentinel_reason = f"Peak reached {peak_r:+.2f}R >= 0.60R; News Sentinel will harvest profit before release."

            elif current_r < 0.60:

                sentinel_action = "RATCHET_PROTECT_FLOOR"

                sentinel_reason = f"Position unconfirmed ({current_r:+.2f}R); News Sentinel will ratchet SL floor to -0.25R."

            else:

                sentinel_action = "MONITORED"

                sentinel_reason = "Position under active Master Trader AI monitoring."



            countdown_sec = float(top_event.get("diff_seconds", 0))

            countdown_min = round(countdown_sec / 60.0)



            at_risk.append({

                "ticket": self._position_value(pos, "ticket"),

                "symbol": sym,

                "direction": self._position_value(
                    pos, "type", "direction", default="BUY"),

                "volume": self._position_value(
                    pos, "volume", "lots", default=0.01),

                "profit": self._position_value(pos, "profit", default=0.0),

                "current_r": current_r,

                "peak_r": peak_r,

                "is_be_locked": is_be_locked,

                "catalyst_event": top_event.get("event"),

                "catalyst_impact": top_event.get("impact"),

                "catalyst_currency": top_event.get("currency"),

                "catalyst_countdown_min": countdown_min,

                "catalyst_time_ist": top_event.get("time_ist", ""),

                "sentinel_action": sentinel_action,

                "sentinel_reason": sentinel_reason

            })



        return at_risk



    def get_imminent_event(self, max_minutes: int = 30,

                           calendar: Optional[List[Dict[str, Any]]] = None

                           ) -> Optional[Dict[str, Any]]:

        """

        Returns the closest high-impact event scheduled within `max_minutes`

        or currently active in its volatility shock window.

        Only EXACT published times qualify. TBA rows have no timestamp and

        estimated rows (a relative "time left") are not precise enough to

        justify a hard gate or an alert that says "LIVE NOW".

        """

        if calendar is None:

            calendar = self.get_news_calendar()

        if not calendar:

            return None



        now_sec = max_minutes * 60

        high_impact = []

        for ev in calendar:

            if ev.get("impact") != "HIGH":

                continue

            if not self._event_has_exact_time(ev):

                continue

            try:

                diff = float(ev.get("diff_seconds"))

            except (TypeError, ValueError):

                continue

            if -900 <= diff <= now_sec:

                high_impact.append(ev)

        if not high_impact:

            return None



        # Sort by smallest absolute time difference

        high_impact.sort(key=lambda x: abs(float(x.get("diff_seconds", 0))))

        top = high_impact[0]

        diff_sec = float(top.get("diff_seconds", 0))

        is_live = diff_sec <= 0 and diff_sec >= -900



        return {

            "event": top.get("event"),

            "currency": top.get("currency"),

            "impact": top.get("impact"),

            "forecast": top.get("forecast"),

            "previous": top.get("previous"),

            "actual": top.get("actual"),

            "diff_seconds": diff_sec,

            "time_ist": top.get("time_ist"),

            "time_utc": top.get("time_utc"),

            "timestamp_iso": top.get("timestamp_iso"),

            "time_quality": "exact",

            "affected_pairs": top.get("affected_pairs", []),

            "is_live": is_live,

            "status_text": "⚡ LIVE VOLATILITY WINDOW ACTIVE" if is_live else f"⚠️ RELEASING IN {round(diff_sec/60):.0f} MINUTES",

            "countdown_str": "LIVE NOW" if is_live else f"in {round(diff_sec/60)}m"

        }



    def get_unified_macro_state(

        self,

        open_positions: Optional[List[Dict[str, Any]]] = None,

        force_refresh: bool = False,

        calendar_max_wait: float = 0.0,

        headlines_max_wait: float = 0.0,

    ) -> Dict[str, Any]:

        """

        Assembles unified institutional macroeconomic state:

        calendar, live breaking headlines, at-risk open positions, and imminent alert.

        `calendar_max_wait` > 0 makes `force_refresh` wait for the fetch (the

        refresh button's path); 0 keeps the original non-blocking behaviour.

        """

        started_at = time.time()

        calendar_events = self.get_news_calendar(force_refresh=force_refresh,

                                                 max_wait=calendar_max_wait)

        headlines = self.get_live_headlines(force_refresh=force_refresh,

                                            max_wait=headlines_max_wait, limit=50)

        at_risk = self.get_at_risk_positions(open_positions or [], calendar=calendar_events)

        imminent = self.get_imminent_event(max_minutes=30, calendar=calendar_events)

        with self._lock:

            last_success = self._last_fetch_success_time

            last_fetch = self._last_fetch_time

            in_flight = self._refresh_in_flight

        calendar_refreshed = bool(force_refresh and last_success >= started_at)

        calendar_cache_age = (time.time() - last_fetch) if last_fetch else None



        n_fab = sum(1 for it in calendar_events if it.get("is_fallback"))

        def _has(key: str) -> int:

            return sum(1 for it in calendar_events if it.get(key))

        counts = {

            "total": len(calendar_events),

            "live": _has("is_live"),

            "upcoming": _has("is_upcoming"),

            "past": _has("is_past"),

            "tba": _has("is_tba"),

            "exact": sum(1 for it in calendar_events if it.get("time_quality") == "exact"),

            "estimated": sum(1 for it in calendar_events if it.get("time_quality") == "estimated"),

            "synthetic": n_fab,

        }

        if calendar_events and n_fab == len(calendar_events):

            source = "synthetic_calendar"

        elif n_fab:

            source = "mixed"

        else:

            source = "live_feed"



        return {

            "news": calendar_events,          # 100% backward-compatible key

            "calendar": calendar_events,

            "headlines": headlines,

            "at_risk_positions": at_risk,

            "imminent_event": imminent,

            "counts": counts,

            "calendar_refreshed": calendar_refreshed,

            "calendar_refresh_in_flight": in_flight,

            "calendar_cache_age_sec": (round(calendar_cache_age, 1)

                                       if calendar_cache_age is not None else None),

            "synthetic_count": n_fab,

            "is_fallback": bool(calendar_events) and n_fab == len(calendar_events),

            "source": source,

            "timestamp": datetime.now(timezone.utc).isoformat()

        }





GLOBAL_NEWS_ENGINE = LiveNewsEngine()

