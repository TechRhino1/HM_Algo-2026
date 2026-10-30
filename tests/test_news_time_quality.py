"""Time-provenance rules for the economic calendar.

The calendar used to stamp undated MyFxBook rows with the fetch time, then
present them as "LIVE NOW" for days. These tests pin the replacement contract:

* a missing/unparseable time is TBA and carries no timestamp;
* TBA and estimated rows never trigger the Sentinel or the hard news gate;
* only an exactly-timed, live-feed, non-synthetic event can black out entries.
"""

from datetime import datetime, timedelta, timezone
import json

import pytest

from jarvis.application.orchestrator import JarvisOrchestrator
from jarvis.market.news import LiveNewsEngine


@pytest.fixture(autouse=True)
def _restore_disk_cache_path():
    """Kept as a guard: no test in this module may leave a global cache path."""
    original = LiveNewsEngine.DISK_CACHE_PATH
    try:
        yield
    finally:
        LiveNewsEngine.DISK_CACHE_PATH = original


def _engine(tmp_path=None) -> LiveNewsEngine:
    kwargs = {"cache_path": str(tmp_path / "news_cache.json")} if tmp_path is not None else {}
    engine = LiveNewsEngine(**kwargs)
    engine._generate_dynamic_calendar = lambda: []
    return engine


def _raw(title, currency="USD", impact="HIGH", *, event_dt=None,
         quality="exact", feed="biquote", fallback=False):
    row = {
        "title": title,
        "currency": currency,
        "impact": impact,
        "event_dt": event_dt,
        "timestamp_iso": event_dt.isoformat() if event_dt is not None else None,
        "time_quality": quality,
        "feed": feed,
        "is_fallback": fallback,
        "source": "synthetic_calendar" if fallback else "live_feed",
    }
    return row


@pytest.mark.parametrize("text,expected", [
    ("5 min", 300),
    ("12h 50min", 46200),
    ("1 day", 86400),
    ("2 weeks", 1209600),
    ("-1451 seconds", -1451),
    ("TBA", None),
    ("", None),
    ("all day", None),
])
def test_parse_relative_time_left_formats(text, expected):
    assert LiveNewsEngine._parse_relative_time_left(f"<td>{text}</td>") == expected


def test_undated_event_is_tba_not_now(tmp_path):
    engine = _engine(tmp_path)
    feed = engine._organize_news_feed([
        _raw("Undated Release", quality="tba", feed="myfxbook"),
    ])

    row = next(r for r in feed if r["event"] == "Undated Release")
    assert row["is_tba"] is True
    assert row["time_quality"] == "tba"
    assert row["timestamp_iso"] is None
    assert row["diff_seconds"] is None
    assert row["is_live"] is False
    assert row["is_upcoming"] is False
    assert row["is_past"] is False
    assert row["status_badge"].startswith("🗓")


def test_tba_quality_discards_a_smuggled_timestamp(tmp_path):
    engine = _engine(tmp_path)
    now = datetime.now(timezone.utc)
    feed = engine._organize_news_feed([
        _raw("TBA With Timestamp", event_dt=now + timedelta(minutes=5), quality="tba"),
    ])

    row = next(r for r in feed if r["event"] == "TBA With Timestamp")
    assert row["is_tba"] is True
    assert row["timestamp_iso"] is None
    assert row["is_live"] is False


def test_exact_and_estimated_are_preserved(tmp_path):
    engine = _engine(tmp_path)
    now = datetime.now(timezone.utc)
    feed = engine._organize_news_feed([
        _raw("Exact Release", event_dt=now + timedelta(hours=2)),
        _raw("Estimated Release", event_dt=now + timedelta(hours=3),
             quality="estimated", feed="myfxbook"),
    ])

    exact = next(r for r in feed if r["event"] == "Exact Release")
    estimated = next(r for r in feed if r["event"] == "Estimated Release")

    assert exact["time_quality"] == "exact"
    assert exact["feed"] == "biquote"
    assert exact["is_upcoming"] is True
    assert estimated["time_quality"] == "estimated"
    assert estimated["feed"] == "myfxbook"


def test_tba_sorted_after_timed_events(tmp_path):
    engine = _engine(tmp_path)
    now = datetime.now(timezone.utc)
    feed = engine._organize_news_feed([
        _raw("Undated Release", quality="tba"),
        _raw("Exact Release", event_dt=now + timedelta(hours=2)),
    ])

    assert feed.index(next(r for r in feed if r["event"] == "Exact Release")) < \
           feed.index(next(r for r in feed if r["event"] == "Undated Release"))


def test_merge_prefers_exact_timestamp_and_dedupes_within_window(monkeypatch):
    engine = _engine()
    now = datetime.now(timezone.utc)
    exact = _raw("US Core PCE Price Index", event_dt=now + timedelta(hours=1))
    estimated = _raw("US Core PCE Price Index", event_dt=now + timedelta(hours=1, minutes=4),
                     quality="estimated", feed="myfxbook")
    other_day = _raw("US Core PCE Price Index", event_dt=now + timedelta(days=1))

    monkeypatch.setattr(engine, "_fetch_biquote_feed", lambda: [exact])
    monkeypatch.setattr(engine, "_fetch_faireconomy_feed", lambda: [])
    monkeypatch.setattr(engine, "_fetch_myfxbook_feed", lambda: [estimated, other_day])
    monkeypatch.setattr(engine, "_save_disk_cache", lambda events: None)

    events = engine._fetch_all_live_sources()

    # Same currency + title + 15-minute window merges; the same title on the
    # next day is a different release and survives.
    assert len(events) == 2
    merged = next(e for e in events if e["title"] == "US Core PCE Price Index"
                  and e["timestamp_iso"] == exact["timestamp_iso"])
    assert merged["time_quality"] == "exact"
    assert merged["feed"] == "biquote"


def test_versionless_disk_cache_is_rejected(tmp_path):
    cache = tmp_path / "news_cache.json"

    def stored(row):
        return {k: v for k, v in row.items() if k != "event_dt"}

    cache.write_text(json.dumps({
        "saved_at": datetime.now(timezone.utc).timestamp(),
        "events": [stored(_raw("Fabricated Row", event_dt=datetime.now(timezone.utc),
                              quality="tba", feed="myfxbook"))],
    }), encoding="utf-8")

    engine = LiveNewsEngine(cache_path=str(cache))
    assert engine._cached_news == []

    cache.write_text(json.dumps({
        "version": LiveNewsEngine.CACHE_VERSION,
        "saved_at": datetime.now(timezone.utc).timestamp(),
        "events": [stored(_raw("Real Row", event_dt=datetime.now(timezone.utc) + timedelta(hours=1)))],
    }), encoding="utf-8")

    engine = LiveNewsEngine(cache_path=str(cache))
    assert engine._cached_news
    assert engine._cached_news[0]["title"] == "Real Row"


def test_bounded_cold_call_waits_for_inflight_fetch(monkeypatch):
    """A cold second caller must wait out the leader, not answer "synthetic".

    This was observed live: the background syncer started a 2s-budget refresh,
    the page's /api/news arrived while it was in flight, and the first response
    was the hardcoded plan even though real events landed a second later.
    """
    import threading
    import time as _time

    engine = _engine()
    started = threading.Event()
    release = threading.Event()
    now = datetime.now(timezone.utc)

    def slow_fetch():
        started.set()
        release.wait(3.0)
        return [_raw("Live Release", event_dt=now + timedelta(hours=1))]

    monkeypatch.setattr(engine, "_fetch_all_live_sources", slow_fetch)

    first = {}
    leader = threading.Thread(
        target=lambda: first.setdefault(
            "rows", engine.get_news_calendar(max_wait=0.05)))
    leader.start()
    assert started.wait(2.0), "leader never started its fetch"

    # Let the leader land while the follower is waiting, as a real feed would.
    threading.Timer(0.3, release.set).start()

    t0 = _time.perf_counter()
    second = engine.get_news_calendar(max_wait=2.0)
    waited = _time.perf_counter() - t0

    leader.join(timeout=5)

    assert waited >= 0.05, "second caller returned before the leader finished"
    assert any(row["event"] == "Live Release" for row in second), \
        "in-flight result was not served to the waiting caller"
    assert engine.has_cached_events() is True


def test_force_refresh_reports_truthfully(monkeypatch):
    """The refresh button must not claim success off a cache hit."""
    engine = _engine()
    monkeypatch.setattr(engine, "get_live_headlines", lambda **kw: [])

    def fast_fetch():
        return [_raw("Fresh Release", event_dt=datetime.now(timezone.utc) + timedelta(hours=1))]

    monkeypatch.setattr(engine, "_fetch_all_live_sources", fast_fetch)
    state = engine.get_unified_macro_state(force_refresh=True, calendar_max_wait=2.0)
    assert state["calendar_refreshed"] is True
    assert any(row["event"] == "Fresh Release" for row in state["news"])

    def slow_fetch():
        import time
        time.sleep(0.8)
        return [_raw("Too Slow", event_dt=datetime.now(timezone.utc) + timedelta(hours=1))]

    engine2 = _engine()
    engine2._cached_news = [_raw("Cached Release",
                                 event_dt=datetime.now(timezone.utc) + timedelta(hours=1))]
    engine2._last_fetch_time = 0.0
    monkeypatch.setattr(engine2, "get_live_headlines", lambda **kw: [])
    monkeypatch.setattr(engine2, "_fetch_all_live_sources", slow_fetch)
    state2 = engine2.get_unified_macro_state(force_refresh=True, calendar_max_wait=0.1)
    assert state2["calendar_refreshed"] is False
    assert any(row["event"] == "Cached Release" for row in state2["news"])


def _calendar_row(title, impact, diff, quality="exact", currency="USD"):
    now = datetime.now(timezone.utc)
    return {
        "event": title,
        "currency": currency,
        "impact": impact,
        "diff_seconds": diff,
        "is_past": False,
        "time_quality": quality,
        "is_tba": quality == "tba",
        "timestamp_iso": (now + timedelta(seconds=diff)).isoformat() if quality != "tba" else None,
        "affected_pairs": ["XAUUSD", "EURUSD"],
    }


def test_sentinel_ignores_tba_and_estimated_rows(monkeypatch):
    engine = _engine()
    calendar = [
        _calendar_row("TBA Release", "HIGH", 300, quality="tba"),
        _calendar_row("Estimated Release", "HIGH", 300, quality="estimated"),
    ]
    monkeypatch.setattr(engine, "get_news_calendar", lambda **kw: calendar)

    positions = [{"ticket": 1, "symbol": "XAUUSD", "current_r": 0.1, "mfe": 0.2}]
    assert engine.get_at_risk_positions(positions, lookforward_minutes=30) == []
    assert engine.get_imminent_event(max_minutes=30) is None


def test_at_risk_accepts_live_position_snapshots(monkeypatch):
    """The live StateManager stores PositionSnapshot objects, not dicts.

    `/api/news` used to 500 on `'PositionSnapshot' object has no attribute
    'get'` whenever a position was open, which took the whole calendar down.
    """
    from jarvis.data.schemas import PositionSnapshot

    engine = _engine()
    monkeypatch.setattr(
        engine, "get_news_calendar",
        lambda **kw: [_calendar_row("Exact Release", "HIGH", 600)])

    pos = PositionSnapshot(
        ticket=77, symbol="XAUUSD", type="BUY", volume=0.05,
        open_price=2300.0, current_price=2305.0, sl=2295.0, tp=2320.0,
        profit=25.0, swap=0.0, commission=0.0, open_time="2026-10-29",
        magic=0, is_be_locked=False, mfe=0.8, current_r=0.4,
    )

    at_risk = engine.get_at_risk_positions([pos], lookforward_minutes=30)
    assert len(at_risk) == 1
    row = at_risk[0]
    assert row["ticket"] == 77
    assert row["symbol"] == "XAUUSD"
    assert row["direction"] == "BUY"
    assert row["peak_r"] == 0.8
    assert row["current_r"] == 0.4


def test_exact_event_is_imminent_and_at_risk(monkeypatch):
    engine = _engine()
    calendar = [_calendar_row("Exact Release", "HIGH", 900)]
    monkeypatch.setattr(engine, "get_news_calendar", lambda **kw: calendar)

    upcoming = engine.get_imminent_event(max_minutes=30)
    assert upcoming is not None
    assert upcoming["event"] == "Exact Release"
    assert upcoming["time_quality"] == "exact"

    positions = [{"ticket": 1, "symbol": "XAUUSD", "current_r": 0.1, "mfe": 0.2}]
    at_risk = engine.get_at_risk_positions(positions, lookforward_minutes=30)
    assert len(at_risk) == 1


class _Canon:
    def __init__(self, canonical):
        self.canonical = canonical


def test_orchestrator_blackout_only_for_exact_live_event(monkeypatch):
    from jarvis.market import news as news_module
    from jarvis.data import symbol_registry

    now = datetime.now(timezone.utc)

    def calendar(quality="exact", impact="HIGH", fallback=False):
        dt = now + timedelta(minutes=5)
        return [{
            "event": "Exact Release",
            "currency": "USD",
            "impact": impact,
            "timestamp_iso": dt.isoformat(),
            "time_quality": quality,
            "is_tba": quality == "tba",
            "source": "synthetic_calendar" if fallback else "live_feed",
            "is_fallback": fallback,
            "affected_pairs": ["XAUUSD"],
        }]

    monkeypatch.setattr(symbol_registry, "resolve", lambda s: _Canon("XAUUSD"))
    orch = JarvisOrchestrator.__new__(JarvisOrchestrator)

    for quality, impact, fallback, expected in [
        ("exact", "HIGH", False, True),
        ("tba", "HIGH", False, False),
        ("estimated", "HIGH", False, False),
        ("exact", "MEDIUM", False, False),
        ("exact", "HIGH", True, False),
    ]:
        monkeypatch.setattr(news_module.GLOBAL_NEWS_ENGINE, "get_news_calendar",
                            lambda quality=quality, impact=impact, fallback=fallback, **kw:
                            calendar(quality, impact, fallback))
        assert orch._is_news_blackout("XAUUSD", now) is expected
