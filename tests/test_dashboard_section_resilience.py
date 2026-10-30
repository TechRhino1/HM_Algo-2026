"""Cross-section resilience: one stock scan, and no silent empty panels.

Two defects from the dashboard audit are pinned here.

1. A cold page opened `/api/stocks/screener` and `/api/stocks/heatmap` at the
   same time. Both need the same 47-symbol scan, so both started one; each run
   is long enough that the first request hit the browser's timeout and the
   panel showed "Screener unavailable". `StockService.get_screener_results` is
   now single-flight: the cache check and the scan live under one lock.

2. Every panel that loads on a cold tab silently returned on a failed GET.
   History and missed-trades were worse: a transport failure rendered as
   "no trades" and "all setups passed filters" -- an all-clear the data did not
   support. The source-level test below pins that the retrying loader is used
   and that failures are rendered as errors, not as empty states.
"""

import os
import re
import threading
import time

import pytest

from jarvis.stocks.stock_service import StockService

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO_ROOT, "jarvis", "ui", "static", "js", "dashboard.js")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def fake_scan(monkeypatch):
    """Replace the provider scan with a two-symbol, thread-safe stub."""
    from jarvis.stocks import stock_service as module

    state = {"calls": 0, "lock": threading.Lock()}

    monkeypatch.setattr(module, "get_all_symbols", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(
        module, "get_stock_profile",
        lambda sym: {"symbol": sym, "name": sym + " Corp", "base_price": 100.0,
                     "sector": "Technology", "industry": "Software",
                     "market": "US_EQUITIES", "tags": []},
    )

    class _Hydrator:
        def hydrate_batch(self, *a, **kw):
            return None

    monkeypatch.setattr(
        "jarvis.data.dynamic_hydrator.DYNAMIC_HYDRATOR", _Hydrator(), raising=False)

    def analyze(sym, timeframe="1D"):
        with state["lock"]:
            state["calls"] += 1
        time.sleep(0.05)        # widen the window a duplicate scan would race in
        return None             # exercise the honest fallback row

    monkeypatch.setattr(module.STOCK_ENGINE, "analyze_stock", analyze)
    return state


def test_concurrent_screener_calls_scan_once(fake_scan):
    svc = StockService()
    payloads = []
    errors = []

    def run():
        try:
            payloads.append(svc.get_screener_results())
        except Exception as exc:                  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(payloads) == 2
    # Two symbols, one scan. A duplicate scan would be four calls.
    assert fake_scan["calls"] == 2
    assert {p["total_universe"] for p in payloads} == {2}


def test_warm_cache_is_served_without_rescanning(fake_scan):
    svc = StockService()
    svc.get_screener_results()
    svc.get_screener_results()
    assert fake_scan["calls"] == 2


def test_expired_cache_rescans(fake_scan):
    svc = StockService()
    svc.get_screener_results()
    svc._last_full_scan_time = 0.0              # force TTL expiry
    svc.get_screener_results()
    assert fake_scan["calls"] == 4


class TestDashboardLoaders:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(DASHBOARD)

    def test_retrying_get_exists_and_only_retries_retryable(self, src):
        assert "function apiGetRetry(" in src
        block = src.split("function apiGetRetry(", 1)[1].split("\n  function ", 1)[0]
        assert "status === 0" in block
        assert "status >= 500" in block

    @pytest.mark.parametrize("endpoint", [
        "/api/news",
        "/api/candles",
        "/api/stocks/screener",
        "/api/stocks/heatmap",
        "/api/intelligence/calibration",
        "/api/intelligence/symbol-profiles",
        "/api/intelligence/missed-trades",
        "/api/history",
        "/api/risk_status",
        "/api/backtest/meta",
        "/api/backtest/jobs",
    ])
    def test_section_uses_retrying_loader(self, src, endpoint):
        calls = [m for m in re.finditer(
            r"apiGet(?:Retry)?\(\s*['\"`]" + re.escape(endpoint), src)]
        assert calls, f"no caller found for {endpoint}"
        assert any("apiGetRetry(" in m.group(0) for m in calls), \
            f"{endpoint} still uses the one-shot loader"

    def test_news_fetch_is_single_flight(self, src):
        block = src.split("function loadNews(", 1)[1].split("\n  function ", 1)[0]
        assert "state.newsLoading" in block
        assert "if (state.newsLoading) return" in block

    def test_chart_has_client_cache_and_slow_budget(self, src):
        block = src.split("function loadChart(", 1)[1].split("\n  function ", 1)[0]
        assert "chartCache" in block
        assert "TIMEOUT.slow" in block
        assert "chartRetryTimer" in block

    def test_failures_are_not_rendered_as_all_clear(self, src):
        missed = src.split("function loadMissedTrades(", 1)[1].split("\n  function ", 1)[0]
        assert "'error'" in missed
        assert "All evaluated setups passed filters" not in missed

        history = src.split("function loadHistory(", 1)[1].split("\n  function ", 1)[0]
        assert "'error'" in history

    def test_retry_timer_is_bounded_to_one(self, src):
        block = src.split("function loadNews(", 1)[1].split("\n  function ", 1)[0]
        assert "if (!state.newsRetryTimer)" in block
