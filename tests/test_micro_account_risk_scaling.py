import unittest
from datetime import datetime
from jarvis.data.schemas import PositionSnapshot, AccountSnapshot
from jarvis.risk.exposure import ExposureManager
from jarvis.risk.portfolio_heat import PortfolioHeatEngine

class TestMicroAccountRiskScaling(unittest.TestCase):
    def setUp(self):
        self.exp = ExposureManager(
            max_open_positions=3,
            max_portfolio_risk_pct=2.5
        )
        self.heat = PortfolioHeatEngine(
            max_portfolio_risk_pct=2.5,
            max_open_positions=3
        )

    def test_micro_account_allows_higher_portfolio_risk_and_4_positions(self):
        # 3 micro-positions on a $372 account totaling $30 risk (8.07% risk)
        positions = [
            PositionSnapshot(ticket=1, symbol="BTCUSD", type="BUY", volume=0.01, open_price=83400.0, current_price=83700.0, sl=81234.0, tp=89000.0, profit=3.0, swap=0.0, commission=0.0, open_time=datetime.now(), magic=888999),
            PositionSnapshot(ticket=2, symbol="EURUSD", type="BUY", volume=0.02, open_price=1.1188, current_price=1.1186, sl=1.1153, tp=1.1290, profit=-0.5, swap=0.0, commission=0.0, open_time=datetime.now(), magic=888999),
            PositionSnapshot(ticket=3, symbol="ETHUSD", type="SELL", volume=0.03, open_price=2504.0, current_price=2502.0, sl=2551.0, tp=2370.0, profit=0.5, swap=0.0, commission=0.0, open_time=datetime.now(), magic=888999)
        ]
        micro_account = AccountSnapshot(
            login=101059540,
            server="XMGlobal-MT5",
            balance=372.0,
            equity=372.0,
            margin=15.0,
            free_margin=357.0,
            margin_level=2480.0,
            leverage=500
        )

        # Check exposure for a 4th trade (XAUUSD Gold with incoming risk ~$2.0)
        res = self.exp.check_exposure("XAUUSD", positions, micro_account, incoming_risk_usd=2.0)
        
        # Must pass because micro account (<$500) has an adaptive cap of 10.0% and 4 open positions
        self.assertTrue(res["passed"], f"Expected to pass on micro account, but breached: {res.get('breaches')}")
        self.assertEqual(len(res["breaches"]), 0)

    def test_standard_account_strictly_enforces_2_point_5_ceiling(self):
        # Same $30 risk on an institutional account where equity is $1000
        positions = [
            PositionSnapshot(ticket=1, symbol="BTCUSD", type="BUY", volume=0.01, open_price=83400.0, current_price=83700.0, sl=81234.0, tp=89000.0, profit=3.0, swap=0.0, commission=0.0, open_time=datetime.now(), magic=888999),
            PositionSnapshot(ticket=2, symbol="EURUSD", type="BUY", volume=0.02, open_price=1.1188, current_price=1.1186, sl=1.1153, tp=1.1290, profit=-0.5, swap=0.0, commission=0.0, open_time=datetime.now(), magic=888999),
            PositionSnapshot(ticket=3, symbol="ETHUSD", type="SELL", volume=0.03, open_price=2504.0, current_price=2502.0, sl=2551.0, tp=2370.0, profit=0.5, swap=0.0, commission=0.0, open_time=datetime.now(), magic=888999)
        ]
        large_account = AccountSnapshot(
            login=101059540,
            server="XMGlobal-MT5",
            balance=10000.0,
            equity=10000.0,
            margin=15.0,
            free_margin=9985.0,
            margin_level=66000.0,
            leverage=500
        )
        # On a $10,000 account, if incoming risk is $300 (total $330 = 3.3% > 2.5%), it must breach
        res = self.exp.check_exposure("XAUUSD", positions, large_account, incoming_risk_usd=300.0)
        self.assertFalse(res["passed"])
        self.assertTrue(any("Portfolio Risk Budget exceeded" in b for b in res["breaches"]))

if __name__ == "__main__":
    unittest.main()
