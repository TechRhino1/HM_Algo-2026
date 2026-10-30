"""Shared test configuration.

WHY THIS FILE EXISTS
--------------------
There was no conftest, so every test that built a ``RiskEngine`` or a
``JarvisOrchestrator`` also built a ``DrawdownGuard`` on its DEFAULT database
path, which ``jarvis.config.paths`` anchors under ``<repo>/data`` — the same file
the live engine reads. Risk state was therefore shared by the whole suite *and*
with the running account:

* Tests inherited whatever the live account last wrote, so results depended on
  the operator's equity rather than on the code.
* Tests wrote back into live risk state. ``data/jarvis_drawdown_state.db`` was
  found holding ``daily_start_equity = 487.36`` beside ``peak_equity = 10150.0``
  — the signature of a test running a small mock account. The old re-anchor rule
  (``baseline > current * 1.5``) took a $500 test balance for a withdrawal and
  wrote it into the live daily-loss baseline, against which the 4% cap would
  then be measured.
* And within a single session the baseline was process-wide, so test A's equity
  set the baseline that test B was judged against. Once the re-anchor was removed
  (see jarvis/risk/drawdown.py) the baseline stopped following large jumps and
  simply stuck to whichever test ran first, which is how
  ``test_d1_online_ml_and_trade_memory_learning_loop`` came to be judged against
  a stranger's equity and blocked.

Redirecting ``JARVIS_DATA_DIR`` is NOT a usable fix: 12 tests in
test_backtest_optimizer.py and test_regime_optimizer.py read real parquet data
out of ``data/`` and break when it is moved. So instead every test gets an
IN-MEMORY drawdown guard (``db_path=""``, the same sentinel ``is_offline()``
uses), which needs no files and cannot leak between tests.

Tests that exercise persistence itself opt out with
``@pytest.mark.drawdown_persistence``.
"""
import pytest

from jarvis.risk.drawdown import DrawdownGuard


@pytest.fixture(autouse=True)
def _hermetic_drawdown_guard(monkeypatch, request):
    """Give every test its own drawdown state instead of the shared on-disk one."""
    if request.node.get_closest_marker("drawdown_persistence"):
        return
    original = DrawdownGuard.__init__

    def in_memory_init(self, *args, **kwargs):
        kwargs["db_path"] = ""
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DrawdownGuard, "__init__", in_memory_init)


@pytest.fixture(autouse=True)
def _hermetic_news_disk_cache(tmp_path):
    """Give every test its own news calendar cache.

    `LiveNewsEngine` writes `data/news_calendar_cache.json` after a successful
    fetch. A test that fetched real feeds (the unified-state contract test)
    left that file behind, and later calendar tests inherited it as a fresh
    cache -- so they never called their mocked fetch and failed for reasons
    unrelated to the code under test. The live operator's calendar is also not
    a test fixture.
    """
    from jarvis.market.news import LiveNewsEngine

    original = LiveNewsEngine.DISK_CACHE_PATH
    LiveNewsEngine.DISK_CACHE_PATH = str(tmp_path / "news_calendar_cache.json")
    try:
        yield
    finally:
        LiveNewsEngine.DISK_CACHE_PATH = original


@pytest.fixture(autouse=True)
def _hermetic_trade_journal(monkeypatch):
    """Keep tests out of the real trade journal.

    `jarvis.data.database.TRADE_DB` is a module-level singleton pointed at
    `data/jarvis_history.db`, and `ExecutionEngine.execute_decision` imports it
    at CALL time, so any test that drives a real execution path writes into the
    live journal. Measured: `data/jarvis_history.db` gained exactly one EURUSD
    BUY row per suite run (rows 264-268, timestamps matching five consecutive
    runs) — fake trades in the same table every realised-P&L statistic is read
    from, indistinguishable from real ones.

    Redirecting `JARVIS_DATA_DIR` is not an option (see the note above: 12
    parquet-reading tests break), so the singleton itself is swapped for one
    backed by a per-test temp file. Tests that need to inspect the journal get
    the replacement and can write freely.
    """
    import os
    import tempfile

    import jarvis.data.database as database

    # Deliberately NOT `tmp_path`: tests such as
    # `test_drawdown_guard.py::TestPersistence::test_an_empty_db_path_writes_nothing`
    # assert that their own tmp_path stays empty, and dropping a journal file in
    # there turns "nothing was written" into three extra entries.
    fd, path = tempfile.mkstemp(suffix=".db", prefix="jarvis_test_journal_")
    os.close(fd)
    temp_db = database.SQLiteTradeDB(db_path=path)
    monkeypatch.setattr(database, "TRADE_DB", temp_db)
    try:
        yield temp_db
    finally:
        try:
            temp_db.close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass
