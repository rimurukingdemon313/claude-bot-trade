from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import trading_bot_db


def cycle_payload(*, approved: bool) -> dict:
    smc = {
        "symbol": "EURUSD",
        "timeframe": "M15",
        "live": True,
        "dataSource": "Test feed",
        "latestPrice": 1.10,
        "candleCount": 8,
        "latestCandle": {
            "timestamp": "2026-09-02T09:00:00+00:00",
            "open": 1.10,
            "high": 1.10,
            "low": 1.09,
            "close": 1.10,
        },
        "liquiditySweeps": [{"direction": "bullish"}],
        "structureBreaks": [],
        "fairValueGaps": [],
        "orderBlocks": [],
    }
    return {
        "scan": {
            "candleTimestamp": "2026-09-02T09:00:00+00:00",
            "scannedAt": "2026-09-02T09:00:05+00:00",
            "symbol": "EURUSD",
            "timeframe": "M15",
            "live": True,
            "dataSource": "Test feed",
            "latestPrice": 1.10,
            "candleCount": 8,
            "latestCandle": smc["latestCandle"],
            "smc": smc,
        },
        "ai": {
            "decision": "BUY" if approved else "NO TRADE",
            "confidence": 80,
            "reasoning": "Test decision",
            "entryPrice": 1.10 if approved else None,
            "stopLoss": 1.09 if approved else None,
            "takeProfit": 1.12 if approved else None,
            "riskRewardRatio": 2 if approved else None,
            "riskAmount": 5 if approved else None,
        },
        "risk": {
            "approved": approved,
            "state": "BUY" if approved else "NO TRADE",
            "reasons": ["All risk rules passed"] if approved else ["AI decision is NO TRADE"],
            "rules": {"risk_amount": 5},
        },
    }


class TradingBotDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = trading_bot_db.DB_PATH
        trading_bot_db.DB_PATH = Path(self.temp_dir.name) / "trading_bot.db"
        self.connection = trading_bot_db.connect()
        trading_bot_db.initialize(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        trading_bot_db.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_rejection_logs_scan_smc_ai_and_risk(self) -> None:
        result = trading_bot_db.record_cycle(
            self.connection, cycle_payload(approved=False)
        )
        self.connection.commit()
        self.assertIsNone(result["paperTrade"])
        self.assertEqual(result["account"]["trades"], 0)
        counts = {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("scans", "smc_setups", "ai_decisions", "risk_decisions")
        }
        self.assertEqual(counts, {
            "scans": 1,
            "smc_setups": 1,
            "ai_decisions": 1,
            "risk_decisions": 1,
        })

    def test_approved_trade_closes_at_take_profit_and_updates_account(self) -> None:
        result = trading_bot_db.record_cycle(
            self.connection, cycle_payload(approved=True)
        )
        self.assertIsNotNone(result["paperTrade"])
        self.assertEqual(result["account"]["trades"], 1)
        settled = trading_bot_db.settle(
            self.connection,
            {
                "latestPrice": 1.12,
                "latestCandle": {"high": 1.12, "low": 1.11},
            },
        )
        self.assertEqual(settled["closedTrades"][0]["closeReason"], "Take profit hit")
        self.assertAlmostEqual(settled["account"]["balance"], 1010.0)
        self.assertEqual(settled["account"]["wins"], 1)


if __name__ == "__main__":
    unittest.main()