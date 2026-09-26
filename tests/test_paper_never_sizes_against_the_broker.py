"""A18 — paper mode must never size against the live broker balance.

`get_account_snapshot()` called `mt5.account_info()` **before** it looked at
`self.mode`, and the paper branch sat below it, reachable only when the broker
call failed or the package was missing.

That is not a rare path, it is the normal one. `init_connection()` deliberately
does NOT call `mt5.initialize()` for paper — it just sets `is_connected = True`
— but the **data** path does initialise a terminal, because paper mode trades
real MT5 bars with simulated fills. So `mt5.account_info()` answers, and it
answers with the live account. Measured with a paper client and a terminal that
reports 762.51:

    mode=paper  login=12345678  server=XMGlobal-MT5 5  balance=762.51

against a simulated book of 10,000. `MT5StateSynchronizer` then caches it into
`state_manager.account`, so every later risk and sizing decision inherits it.

Two consequences beyond the wrong number:

* a paper run's position sizing is a function of somebody's real equity, so the
  same config sizes differently on two machines;
* test outcomes depended on test order and on the size of a real account — a
  mocked 10,000 came back as 762.51.

The mode check now happens before any broker call. `_reconnect_if_needed()` is
still called first, because it is what rewrites `self.mode` to `"paper"` when
the MT5 package is absent, and that rewrite must be visible to the check.
"""

import types
from unittest import mock

import pytest

import jarvis.execution.mt5_client as mc
from jarvis.execution.mt5_client import MT5Client

REAL_BALANCE = 762.51
REAL_EQUITY = 758.90
PAPER_START = 10000.0


class _RealAccount:
    login = 12345678
    server = "XMGlobal-MT5 5"
    balance = REAL_BALANCE
    equity = REAL_EQUITY
    margin = 10.0
    margin_free = 748.90
    margin_level = 7589.0
    leverage = 500
    profit = -3.61
    name = "Real Trader"
    company = "XM Global"
    currency = "USD"
    trade_allowed = True


def _fake_mt5(account=_RealAccount()):
    return types.SimpleNamespace(account_info=lambda: account, last_error=lambda: (1, "ok"))


@pytest.fixture
def client():
    """A paper client that is 'connected' exactly as init_connection() leaves it."""
    c = MT5Client(magic_number=888999, mode="paper", auto_init=False)
    c.is_connected = True
    return c


class TestPaperDoesNotSeeTheBroker:
    def test_the_balance_is_the_simulated_book(self, client):
        with mock.patch.object(mc, "MT5_AVAILABLE", True), \
             mock.patch.object(mc, "mt5", _fake_mt5()):
            snap = client.get_account_snapshot()
        assert snap.balance == pytest.approx(PAPER_START)
        assert snap.balance != pytest.approx(REAL_BALANCE)

    def test_the_identity_is_the_paper_account(self, client):
        with mock.patch.object(mc, "MT5_AVAILABLE", True), \
             mock.patch.object(mc, "mt5", _fake_mt5()):
            snap = client.get_account_snapshot()
        assert snap.login == 999999
        assert "PAPER" in snap.server.upper()

    def test_the_broker_is_never_asked(self, client):
        # Even a read is a claim: account_info() on a live terminal is what
        # makes the leak possible at all.
        fake = _fake_mt5()
        with mock.patch.object(mc, "MT5_AVAILABLE", True), \
             mock.patch.object(mc, "mt5", fake), \
             mock.patch.object(fake, "account_info") as spy:
            client.get_account_snapshot()
        spy.assert_not_called()

    def test_paper_pnl_moves_equity_not_balance(self, client):
        client._paper_positions[1] = types.SimpleNamespace(profit=250.0)
        try:
            with mock.patch.object(mc, "MT5_AVAILABLE", True), \
                 mock.patch.object(mc, "mt5", _fake_mt5()):
                snap = client.get_account_snapshot()
        finally:
            client._paper_positions.pop(1, None)
        assert snap.balance == pytest.approx(PAPER_START)
        assert snap.equity == pytest.approx(PAPER_START + 250.0)


class TestLiveStillSeesTheBroker:
    def test_live_returns_the_real_account(self):
        c = MT5Client(magic_number=888999, mode="live", auto_init=False)
        c.is_connected = True
        with mock.patch.object(mc, "MT5_AVAILABLE", True), \
             mock.patch.object(mc, "mt5", _fake_mt5()):
            snap = c.get_account_snapshot()
        assert snap.login == _RealAccount.login
        assert snap.balance == pytest.approx(REAL_BALANCE)

    def test_live_without_the_package_fails_closed(self):
        """Spec v2.2 INV-06: live without package strictly fails closed; zero paper fallback."""
        c = MT5Client(magic_number=888999, mode="live", auto_init=False)
        with mock.patch.object(mc, "MT5_AVAILABLE", False), \
             mock.patch.object(mc, "mt5", None):
            snap = c.get_account_snapshot()
        assert c.mode == "live"
        assert snap.login == 0
        assert snap.trade_allowed is False

    def test_a_connected_live_client_that_answers_nothing_is_disconnected(self):
        c = MT5Client(magic_number=888999, mode="live", auto_init=False)
        c.is_connected = True
        with mock.patch.object(mc, "MT5_AVAILABLE", True), \
             mock.patch.object(mc, "mt5", _fake_mt5(account=None)):
            snap = c.get_account_snapshot()
        assert snap.login == 0
        assert snap.server == "DISCONNECTED"
