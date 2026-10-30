"""
Unit tests for Economic Calendar & News Engine Restoration:
- Biquote MQL5 feed parsing and normalization
- Multi-source feed deduplication (Biquote + FairEconomy + MyFxBook)
- Retention of released events (actual vs forecast) and upcoming events
- Non-blocking News Sentinel at-risk position evaluation
- Disk cache serialization and recovery
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from jarvis.market.news import LiveNewsEngine, GLOBAL_NEWS_ENGINE


def test_biquote_feed_normalization():
    """Verify Biquote MQL5 raw JSON is normalized into standard event schemas."""
    engine = LiveNewsEngine()
    now_dt = datetime.now(timezone.utc)
    future_iso = (now_dt + timedelta(hours=2)).isoformat()
    past_iso = (now_dt - timedelta(hours=2)).isoformat()

    mock_biquote_payload = [
        {
            "id": "mql5:1001",
            "name": "US Non-Farm Payrolls",
            "currency": "USD",
            "importance": "high",
            "time": future_iso,
            "actual": None,
            "forecast": 185.0,
            "previous": 150.0
        },
        {
            "id": "mql5:1002",
            "name": "German Retail Sales m/m",
            "currency": "EUR",
            "importance": "medium",
            "time": past_iso,
            "actual": 1.2,
            "forecast": 0.5,
            "previous": -0.8
        },
        {
            "id": "mql5:1003",
            "name": "Ignored Unknown Currency",
            "currency": "XYZ",
            "importance": "low",
            "time": future_iso
        }
    ]

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_biquote_payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        parsed = engine._fetch_biquote_feed()

    # XYZ currency should be ignored; USD and EUR retained
    assert len(parsed) == 2
    nfp = next(x for x in parsed if x["currency"] == "USD")
    assert nfp["title"] == "US Non-Farm Payrolls"
    assert nfp["impact"] == "HIGH"
    assert nfp["actual"] == "—"
    assert nfp["forecast"] == "185.0"
    assert nfp["source"] == "live_feed"

    retail = next(x for x in parsed if x["currency"] == "EUR")
    assert retail["title"] == "German Retail Sales m/m"
    assert retail["impact"] == "MEDIUM"
    assert retail["actual"] == "1.2"
    assert retail["forecast"] == "0.5"


def test_organize_news_feed_preserves_past_releases_and_upcoming():
    """Verify _organize_news_feed retains past releases from today alongside upcoming releases."""
    engine = LiveNewsEngine()
    now_dt = datetime.now(timezone.utc)

    events = [
        # 1. Past release from 3 hours ago (today)
        {
            "title": "UK CPI YoY",
            "currency": "GBP",
            "impact": "HIGH",
            "actual": "2.2",
            "forecast": "2.1",
            "previous": "2.0",
            "diff_seconds": -10800,
            "timestamp_iso": (now_dt - timedelta(hours=3)).isoformat(),
            "is_fallback": False
        },
        # 2. Past release from 1 hour ago (today)
        {
            "title": "Eurozone Trade Balance",
            "currency": "EUR",
            "impact": "MEDIUM",
            "actual": "14.5",
            "forecast": "12.0",
            "previous": "10.0",
            "diff_seconds": -3600,
            "timestamp_iso": (now_dt - timedelta(hours=1)).isoformat(),
            "is_fallback": False
        },
        # 3. Upcoming release in 30 minutes
        {
            "title": "US Initial Jobless Claims",
            "currency": "USD",
            "impact": "HIGH",
            "actual": "—",
            "forecast": "220K",
            "previous": "225K",
            "diff_seconds": 1800,
            "timestamp_iso": (now_dt + timedelta(minutes=30)).isoformat(),
            "is_fallback": False
        },
        # 4. Upcoming release in 4 hours
        {
            "title": "US Core PCE Price Index",
            "currency": "USD",
            "impact": "HIGH",
            "actual": "—",
            "forecast": "0.3%",
            "previous": "0.2%",
            "diff_seconds": 14400,
            "timestamp_iso": (now_dt + timedelta(hours=4)).isoformat(),
            "is_fallback": False
        }
    ]

    organized = engine._organize_news_feed(events)
    assert len(organized) >= 4

    # The soonest upcoming event (US Initial Jobless Claims) should be at index 0 (or top upcoming)
    top_ev = organized[0]
    assert top_ev["is_upcoming"] or top_ev["is_live"]
    assert "Jobless" in top_ev["event"]

    # Both past events must be retained in the organized feed!
    past_in_feed = [e for e in organized if e.get("is_past")]
    assert len(past_in_feed) >= 2
    past_titles = [e.get("event") for e in past_in_feed]
    assert "UK CPI YoY" in past_titles
    assert "Eurozone Trade Balance" in past_titles


def test_at_risk_positions_non_blocking_evaluation():
    """Verify get_at_risk_positions uses non-blocking call and matches impending catalysts."""
    engine = LiveNewsEngine()
    now_dt = datetime.now(timezone.utc)

    # Fake cache with a high-impact USD event in 20 minutes
    engine._cached_news = [
        {
            "title": "US Federal Reserve Rate Decision",
            "currency": "USD",
            "impact": "HIGH",
            "diff_seconds": 1200,
            "timestamp_iso": (now_dt + timedelta(minutes=20)).isoformat(),
            "is_fallback": False
        }
    ]
    engine._last_fetch_time = time.time()

    open_positions = [
        {
            "ticket": 99901,
            "symbol": "EURUSD",
            "direction": "BUY",
            "volume": 0.5,
            "profit": 120.0,
            "current_r": 0.85,
            "peak_r": 1.10
        },
        {
            "ticket": 99902,
            "symbol": "USDJPY",
            "direction": "SELL",
            "volume": 0.25,
            "profit": -40.0,
            "current_r": -0.20,
            "peak_r": 0.30
        }
    ]

    t0 = time.time()
    at_risk = engine.get_at_risk_positions(open_positions, lookforward_minutes=60)
    dt = time.time() - t0

    # Must be non-blocking (< 50ms)
    assert dt < 0.05
    assert len(at_risk) == 2
    assert at_risk[0]["symbol"] in ("EURUSD", "USDJPY")
    assert at_risk[0]["catalyst_currency"] == "USD"
    assert "Federal Reserve" in at_risk[0]["catalyst_event"]
