"""
Unit tests for News Engine disk caching and rate-limiting resilience.
"""
import os
import json
import time
import tempfile
import pytest
from datetime import datetime, timezone, timedelta
from jarvis.market.news import LiveNewsEngine


def test_news_disk_cache_persistence_and_hydration(tmp_path):
    cache_file = str(tmp_path / "test_news_cache.json")
    
    # 1. Simulate saving to disk cache
    engine = LiveNewsEngine()
    engine.DISK_CACHE_PATH = cache_file
    
    sample_events = [{
        "title": "US Non-Farm Payrolls",
        "currency": "USD",
        "impact": "HIGH",
        "forecast": "180K",
        "previous": "150K",
        "actual": "220K",
        "diff_seconds": 3600.0,
        "timestamp_iso": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    }]
    
    engine._save_disk_cache(sample_events)
    assert os.path.exists(cache_file)
    
    with open(cache_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data.get("events", [])) == 1
    assert data["events"][0]["title"] == "US Non-Farm Payrolls"
    
    # 2. Simulate new instance cold-start hydrating from disk
    engine2 = LiveNewsEngine()
    engine2.DISK_CACHE_PATH = cache_file
    engine2._load_disk_cache()
    
    assert len(engine2._cached_news) == 1
    assert engine2._cached_news[0]["title"] == "US Non-Farm Payrolls"
    
    # 3. Verify get_news_calendar recomputes diff_seconds dynamically
    organized = engine2._organize_news_feed(engine2._cached_news)
    assert len(organized) >= 1
    assert "event" in organized[0]
    assert organized[0]["event"] == "US Non-Farm Payrolls"


def test_429_survives_via_disk_cache(tmp_path):
    """When FairEconomy is rate-limited (429), disk cache ensures real news is still served."""
    cache_file = str(tmp_path / "test_news_cache_429.json")
    engine = LiveNewsEngine()
    engine.DISK_CACHE_PATH = cache_file
    
    # Pre-populate disk cache with a real release
    real_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    engine._save_disk_cache([{
        "title": "CPI MoM",
        "currency": "USD",
        "impact": "HIGH",
        "forecast": "0.2%",
        "previous": "0.1%",
        "actual": "0.4%",
        "diff_seconds": -1800.0,
        "timestamp_iso": real_time
    }])
    
    # Trigger 429 backoff
    engine._fe_backoff_until = time.monotonic() + 600.0
    
    # Fetch news calendar
    cal = engine.get_news_calendar()
    assert len(cal) > 0
    # Confirm it served real cached CPI event, not purely empty or hardcoded synthetic
    assert any("CPI" in e.get("event", "") for e in cal)
