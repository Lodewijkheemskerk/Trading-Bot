"""Unit tests for the compounder and pipeline."""

import json
import sys
import unittest
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.compounder import Compounder, PerformanceReport
from scripts.pipeline import TradingPipeline
from config import KILL_SWITCH_FILE


class TestCompounderAnalyze(unittest.TestCase):
    def _make_trade(self, **overrides):
        trade = {
            "trade_id": "test001",
            "market_id": "MKT-001",
            "direction": "buy_yes",
            "entry_price": 0.50,
            "position_size_usd": 10.0,
            "model_probability": 0.70,
            "signal_strength": 0.10,
            "edge": 0.20,
            "status": "open",
            "pnl": 0.0,
            "risk_passed": True,
            "risk_failures": [],
        }
        trade.update(overrides)
        return trade

    def test_winning_yes_trade(self):
        c = Compounder(settings={"compound": {}})
        trade = self._make_trade(direction="buy_yes", entry_price=0.50)
        result = c.analyze_trade(trade, outcome=True)
        self.assertGreater(result["pnl"], 0)
        self.assertTrue(result["correct_prediction"])

    def test_losing_yes_trade(self):
        c = Compounder(settings={"compound": {}})
        trade = self._make_trade(direction="buy_yes", entry_price=0.50)
        result = c.analyze_trade(trade, outcome=False)
        self.assertLess(result["pnl"], 0)
        self.assertFalse(result["correct_prediction"])

    def test_winning_no_trade(self):
        c = Compounder(settings={"compound": {}})
        trade = self._make_trade(direction="buy_no", entry_price=0.50, model_probability=0.30)
        result = c.analyze_trade(trade, outcome=False)
        self.assertGreater(result["pnl"], 0)
        self.assertTrue(result["correct_prediction"])

    def test_losing_no_trade(self):
        c = Compounder(settings={"compound": {}})
        trade = self._make_trade(direction="buy_no", entry_price=0.50, model_probability=0.30)
        result = c.analyze_trade(trade, outcome=True)
        self.assertLess(result["pnl"], 0)
        self.assertFalse(result["correct_prediction"])

    def test_brier_component_perfect(self):
        """Perfect prediction (prob=1.0, outcome=YES) should have brier=0."""
        c = Compounder(settings={"compound": {}})
        trade = self._make_trade(model_probability=1.0)
        result = c.analyze_trade(trade, outcome=True)
        self.assertAlmostEqual(result["brier_component"], 0.0)

    def test_brier_component_worst(self):
        """Worst prediction (prob=0.0, outcome=YES) should have brier=1."""
        c = Compounder(settings={"compound": {}})
        trade = self._make_trade(model_probability=0.0)
        result = c.analyze_trade(trade, outcome=True)
        self.assertAlmostEqual(result["brier_component"], 1.0)


class TestPerformanceReport(unittest.TestCase):
    def test_empty_trades(self):
        c = Compounder(settings={"compound": {}})
        report = c.get_performance_report(trades=[])
        self.assertEqual(report.total_trades, 0)
        self.assertEqual(report.win_rate, 0.0)
        self.assertEqual(report.total_pnl, 0.0)

    def test_with_closed_trades(self):
        trades = [
            {"risk_passed": True, "status": "closed", "pnl": 5.0, "edge": 0.10,
             "confidence": 0.80, "model_probability": 0.70, "direction": "buy_yes"},
            {"risk_passed": True, "status": "closed", "pnl": -3.0, "edge": 0.05,
             "confidence": 0.70, "model_probability": 0.60, "direction": "buy_yes"},
            {"risk_passed": True, "status": "closed", "pnl": 8.0, "edge": 0.15,
             "confidence": 0.85, "model_probability": 0.75, "direction": "buy_yes"},
        ]
        c = Compounder(settings={"compound": {}})
        report = c.get_performance_report(trades=trades)
        self.assertEqual(report.closed_trades, 3)
        self.assertAlmostEqual(report.win_rate, 2/3, places=2)
        self.assertAlmostEqual(report.total_pnl, 10.0)
        self.assertGreater(report.profit_factor, 1.0)

    def test_blocked_trades_counted(self):
        trades = [
            {"risk_passed": False, "status": "blocked", "pnl": 0, "edge": 0, "confidence": 0},
            {"risk_passed": True, "status": "open", "pnl": 0, "edge": 0.10, "confidence": 0.80},
        ]
        c = Compounder(settings={"compound": {}})
        report = c.get_performance_report(trades=trades)
        self.assertEqual(report.total_trades, 2)
        self.assertEqual(report.blocked_trades, 1)
        self.assertEqual(report.executed_trades, 1)

    def test_report_has_all_fields(self):
        required = [
            "total_trades", "win_rate", "total_pnl", "sharpe_ratio",
            "profit_factor", "brier_score", "max_drawdown",
        ]
        actual = [f.name for f in fields(PerformanceReport)]
        for r in required:
            self.assertIn(r, actual)


class TestKillSwitch(unittest.TestCase):
    def test_activate_creates_file(self):
        try:
            TradingPipeline.activate_kill_switch()
            self.assertTrue(KILL_SWITCH_FILE.exists())
        finally:
            KILL_SWITCH_FILE.unlink(missing_ok=True)

    def test_deactivate_removes_file(self):
        KILL_SWITCH_FILE.write_text("STOP")
        TradingPipeline.deactivate_kill_switch()
        self.assertFalse(KILL_SWITCH_FILE.exists())

    def test_kill_switch_halts_pipeline(self):
        try:
            KILL_SWITCH_FILE.write_text("STOP")
            pipeline = TradingPipeline()
            result = pipeline.run_once()
            self.assertFalse(result["success"])
            self.assertTrue(result.get("halted", False))
        finally:
            KILL_SWITCH_FILE.unlink(missing_ok=True)


class TestPipelineInit(unittest.TestCase):
    def test_imports_clean(self):
        from scripts.compounder import Compounder
        from scripts.pipeline import TradingPipeline
        self.assertIsNotNone(Compounder)
        self.assertIsNotNone(TradingPipeline)

    def test_pipeline_creates(self):
        pipeline = TradingPipeline()
        self.assertIsNotNone(pipeline)
        self.assertFalse(pipeline.heuristic_only)


if __name__ == "__main__":
    unittest.main()
