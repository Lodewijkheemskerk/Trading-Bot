"""Unit tests for risk management and Kelly sizing."""

import json
import sys
import tempfile
import unittest
from dataclasses import fields, asdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import KILL_SWITCH_FILE
from scripts.validate_risk import (
    RiskManager,
    RiskValidation,
    RiskCheck,
    PortfolioState,
)
from scripts.kelly_size import calculate_kelly, KellyResult


def _make_signal(**overrides):
    """Create a test trade signal dict with sensible defaults."""
    sig = {
        "market_id": "TEST-001",
        "market_title": "Test market",
        "ensemble_probability": 0.65,
        "market_probability": 0.50,
        "edge": 0.15,
        "direction": "buy_yes",
        "signal_strength": 0.10,
        "confidence": 0.75,
        "should_trade": True,
        "mispricing_score": 1.5,
        "expected_value": 0.10,
    }
    sig.update(overrides)
    return sig


def _make_risk_manager(**overrides):
    """Create a RiskManager with test settings."""
    settings = {
        "risk": {
            "kelly_fraction": 0.25,
            "max_position_pct": 0.05,
            "max_concurrent_positions": 15,
            "max_daily_loss_pct": 0.15,
            "max_drawdown_pct": 0.08,
            "max_slippage_pct": 0.02,
            "max_daily_api_cost": 50.0,
        },
        "prediction": {
            "min_edge": 0.04,
            "min_confidence": 0.65,
        },
        "bankroll": {
            "initial": 500.0,
        },
    }
    for k, v in overrides.items():
        if k in settings["risk"]:
            settings["risk"][k] = v
        elif k in settings["prediction"]:
            settings["prediction"][k] = v
    # Use temp dir for state to avoid polluting real data
    rm = RiskManager(settings=settings)
    rm.state = PortfolioState.default(500.0)
    return rm


# ======================================================================
# Risk check tests
# ======================================================================

class TestRiskCheck1_MinEdge(unittest.TestCase):
    def test_passes_when_edge_above_threshold(self):
        rm = _make_risk_manager()
        sig = _make_signal(edge=0.10)
        result = rm.validate_trade(sig, position_size_usd=10)
        check = next(c for c in result.checks if c.name == "min_edge")
        self.assertTrue(check.passed)

    def test_fails_when_edge_below_threshold(self):
        rm = _make_risk_manager()
        sig = _make_signal(edge=0.02)
        result = rm.validate_trade(sig, position_size_usd=10)
        check = next(c for c in result.checks if c.name == "min_edge")
        self.assertFalse(check.passed)

    def test_uses_absolute_edge(self):
        """Negative edge (buy_no) should still be checked by absolute value."""
        rm = _make_risk_manager()
        sig = _make_signal(edge=-0.10)
        result = rm.validate_trade(sig, position_size_usd=10)
        check = next(c for c in result.checks if c.name == "min_edge")
        self.assertTrue(check.passed)


class TestRiskCheck2_MinConfidence(unittest.TestCase):
    def test_passes_when_confidence_above_threshold(self):
        rm = _make_risk_manager()
        sig = _make_signal(confidence=0.80)
        result = rm.validate_trade(sig, position_size_usd=10)
        check = next(c for c in result.checks if c.name == "min_confidence")
        self.assertTrue(check.passed)

    def test_fails_when_confidence_below_threshold(self):
        rm = _make_risk_manager()
        sig = _make_signal(confidence=0.30)
        result = rm.validate_trade(sig, position_size_usd=10)
        check = next(c for c in result.checks if c.name == "min_confidence")
        self.assertFalse(check.passed)


class TestRiskCheck3_MaxPositionPct(unittest.TestCase):
    def test_passes_when_position_within_limit(self):
        rm = _make_risk_manager()
        # Max = 500 * 0.05 = $25
        result = rm.validate_trade(_make_signal(), position_size_usd=20)
        check = next(c for c in result.checks if c.name == "max_position_pct")
        self.assertTrue(check.passed)

    def test_fails_when_position_exceeds_limit(self):
        rm = _make_risk_manager()
        result = rm.validate_trade(_make_signal(), position_size_usd=30)
        check = next(c for c in result.checks if c.name == "max_position_pct")
        self.assertFalse(check.passed)


class TestRiskCheck4_MaxConcurrent(unittest.TestCase):
    def test_passes_when_under_limit(self):
        rm = _make_risk_manager()
        rm.state.open_positions = 5
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_concurrent_positions")
        self.assertTrue(check.passed)

    def test_fails_when_at_limit(self):
        rm = _make_risk_manager()
        rm.state.open_positions = 15
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_concurrent_positions")
        self.assertFalse(check.passed)


class TestRiskCheck5_MaxDailyLoss(unittest.TestCase):
    def test_passes_when_profitable(self):
        rm = _make_risk_manager()
        rm.state.daily_pnl = 10.0
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_daily_loss")
        self.assertTrue(check.passed)

    def test_fails_when_daily_loss_exceeded(self):
        rm = _make_risk_manager()
        rm.state.daily_pnl = -100.0  # -$100 > 15% of $500 = $75
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_daily_loss")
        self.assertFalse(check.passed)


class TestRiskCheck6_MaxDrawdown(unittest.TestCase):
    def test_passes_when_no_drawdown(self):
        rm = _make_risk_manager()
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_drawdown")
        self.assertTrue(check.passed)

    def test_fails_when_drawdown_exceeded(self):
        rm = _make_risk_manager()
        rm.state.peak_bankroll = 500.0
        rm.state.current_bankroll = 450.0  # 10% drawdown > 8% max
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_drawdown")
        self.assertFalse(check.passed)


class TestRiskCheck7_KillSwitch(unittest.TestCase):
    def test_passes_when_no_stop_file(self):
        rm = _make_risk_manager()
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "kill_switch")
        self.assertTrue(check.passed)

    def test_fails_when_stop_file_exists(self):
        rm = _make_risk_manager()
        # Create temp STOP file
        stop_file = Path(KILL_SWITCH_FILE)
        try:
            stop_file.write_text("STOP")
            result = rm.validate_trade(_make_signal(), position_size_usd=10)
            check = next(c for c in result.checks if c.name == "kill_switch")
            self.assertFalse(check.passed)
        finally:
            stop_file.unlink(missing_ok=True)


class TestRiskCheck8_MaxSlippage(unittest.TestCase):
    def test_passes_for_normal_price(self):
        rm = _make_risk_manager()
        sig = _make_signal(market_probability=0.50)
        result = rm.validate_trade(sig, position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_slippage")
        self.assertTrue(check.passed)

    def test_fails_for_extreme_low_price(self):
        rm = _make_risk_manager()
        sig = _make_signal(market_probability=0.01)  # Too close to 0
        result = rm.validate_trade(sig, position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_slippage")
        self.assertFalse(check.passed)

    def test_fails_for_extreme_high_price(self):
        rm = _make_risk_manager()
        sig = _make_signal(market_probability=0.99)  # Too close to 1
        result = rm.validate_trade(sig, position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_slippage")
        self.assertFalse(check.passed)


class TestRiskCheck9_BankrollPositive(unittest.TestCase):
    def test_passes_with_positive_bankroll(self):
        rm = _make_risk_manager()
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "bankroll_positive")
        self.assertTrue(check.passed)

    def test_fails_with_zero_bankroll(self):
        rm = _make_risk_manager()
        rm.state.current_bankroll = 0
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "bankroll_positive")
        self.assertFalse(check.passed)


class TestRiskCheck10_MaxApiCost(unittest.TestCase):
    def test_passes_when_under_limit(self):
        rm = _make_risk_manager()
        rm.state.daily_api_cost = 10.0
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_daily_api_cost")
        self.assertTrue(check.passed)

    def test_fails_when_over_limit(self):
        rm = _make_risk_manager()
        rm.state.daily_api_cost = 55.0
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        check = next(c for c in result.checks if c.name == "max_daily_api_cost")
        self.assertFalse(check.passed)


# ======================================================================
# Overall validation tests
# ======================================================================

class TestOverallValidation(unittest.TestCase):
    def test_all_pass_gives_overall_pass(self):
        rm = _make_risk_manager()
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        self.assertTrue(result.overall_pass)
        self.assertEqual(len(result.failure_reasons), 0)

    def test_any_fail_gives_overall_fail(self):
        rm = _make_risk_manager()
        sig = _make_signal(edge=0.01, confidence=0.20)  # Fails checks 1 and 2
        result = rm.validate_trade(sig, position_size_usd=10)
        self.assertFalse(result.overall_pass)
        self.assertGreater(len(result.failure_reasons), 0)

    def test_returns_exactly_10_checks(self):
        rm = _make_risk_manager()
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        self.assertEqual(len(result.checks), 10)

    def test_validation_has_timestamp(self):
        rm = _make_risk_manager()
        result = rm.validate_trade(_make_signal(), position_size_usd=10)
        self.assertIsInstance(result.timestamp, str)
        self.assertIn("T", result.timestamp)


# ======================================================================
# Kelly sizing tests
# ======================================================================

class TestKellySizing(unittest.TestCase):
    def test_positive_edge_gives_positive_size(self):
        """Positive expected value should produce a position."""
        result = calculate_kelly(
            ensemble_probability=0.70,
            market_probability=0.50,
            direction="buy_yes",
            bankroll=500,
            kelly_multiplier=0.25,
            max_position_pct=0.05,
            settings={"risk": {}},
        )
        self.assertGreater(result.position_size_usd, 0)
        self.assertGreater(result.kelly_fraction, 0)

    def test_negative_edge_gives_zero_size(self):
        """Negative expected value should produce zero position."""
        result = calculate_kelly(
            ensemble_probability=0.30,  # Lower than market
            market_probability=0.50,
            direction="buy_yes",
            bankroll=500,
            kelly_multiplier=0.25,
            max_position_pct=0.05,
            settings={"risk": {}},
        )
        self.assertEqual(result.position_size_usd, 0)

    def test_position_capped_by_max_pct(self):
        """Position should not exceed max_position_pct of bankroll."""
        result = calculate_kelly(
            ensemble_probability=0.95,  # Very high edge
            market_probability=0.50,
            direction="buy_yes",
            bankroll=500,
            kelly_multiplier=1.0,  # Full Kelly (large)
            max_position_pct=0.05,
            settings={"risk": {}},
        )
        self.assertLessEqual(result.position_size_usd, 500 * 0.05)

    def test_quarter_kelly_reduces_position(self):
        """Quarter-Kelly should produce 1/4 of full Kelly position."""
        full = calculate_kelly(
            ensemble_probability=0.70,
            market_probability=0.50,
            direction="buy_yes",
            bankroll=10000,  # Large bankroll so cap doesn't bind
            kelly_multiplier=1.0,
            max_position_pct=1.0,
            settings={"risk": {}},
        )
        quarter = calculate_kelly(
            ensemble_probability=0.70,
            market_probability=0.50,
            direction="buy_yes",
            bankroll=10000,
            kelly_multiplier=0.25,
            max_position_pct=1.0,
            settings={"risk": {}},
        )
        self.assertAlmostEqual(quarter.position_size_usd, full.position_size_usd * 0.25, delta=0.01)

    def test_buy_no_direction(self):
        """Buy NO should work correctly with inverted probabilities."""
        result = calculate_kelly(
            ensemble_probability=0.30,  # Low prob = buy NO has edge
            market_probability=0.50,    # Market at 50 cents
            direction="buy_no",
            bankroll=500,
            kelly_multiplier=0.25,
            max_position_pct=0.05,
            settings={"risk": {}},
        )
        self.assertGreater(result.position_size_usd, 0)

    def test_zero_bankroll_gives_zero_size(self):
        result = calculate_kelly(
            ensemble_probability=0.70,
            market_probability=0.50,
            direction="buy_yes",
            bankroll=0,
            kelly_multiplier=0.25,
            max_position_pct=0.05,
            settings={"risk": {}},
        )
        self.assertEqual(result.position_size_usd, 0)

    def test_extreme_market_price(self):
        """Market price at 0 or 1 should return zero position."""
        result = calculate_kelly(
            ensemble_probability=0.70,
            market_probability=0.0,
            direction="buy_yes",
            bankroll=500,
            kelly_multiplier=0.25,
            max_position_pct=0.05,
            settings={"risk": {}},
        )
        self.assertEqual(result.position_size_usd, 0)

    def test_kelly_result_has_all_fields(self):
        result = calculate_kelly(
            ensemble_probability=0.70,
            market_probability=0.50,
            direction="buy_yes",
            bankroll=500,
            kelly_multiplier=0.25,
            max_position_pct=0.05,
            settings={"risk": {}},
        )
        required = ["kelly_fraction", "adjusted_fraction", "position_size_usd", "edge", "expected_value", "bankroll"]
        actual = [f.name for f in fields(KellyResult)]
        for r in required:
            self.assertIn(r, actual)


# ======================================================================
# Portfolio state tests
# ======================================================================

class TestPortfolioState(unittest.TestCase):
    def test_default_state(self):
        state = PortfolioState.default(500.0)
        self.assertEqual(state.initial_bankroll, 500.0)
        self.assertEqual(state.current_bankroll, 500.0)
        self.assertEqual(state.open_positions, 0)
        self.assertEqual(state.daily_pnl, 0.0)

    def test_round_trip_serialization(self):
        state = PortfolioState.default(500.0)
        state.open_positions = 3
        state.daily_pnl = -25.0
        data = state.to_dict()
        restored = PortfolioState.from_dict(data)
        self.assertEqual(restored.open_positions, 3)
        self.assertAlmostEqual(restored.daily_pnl, -25.0)


# ======================================================================
# Contract tests
# ======================================================================

class TestRiskValidationContract(unittest.TestCase):
    def test_risk_validation_fields(self):
        required = ["checks", "overall_pass", "failure_reasons", "timestamp"]
        actual = [f.name for f in fields(RiskValidation)]
        for r in required:
            self.assertIn(r, actual)

    def test_risk_check_fields(self):
        required = ["name", "passed", "detail", "threshold", "actual"]
        actual = [f.name for f in fields(RiskCheck)]
        for r in required:
            self.assertIn(r, actual)


# ======================================================================
# Executor tests
# ======================================================================

from scripts.executor import TradeExecutor, Trade


class TestTradeExecutor(unittest.TestCase):
    def _make_executor(self):
        settings = {
            "risk": {
                "kelly_fraction": 0.25,
                "max_position_pct": 0.05,
                "max_concurrent_positions": 15,
                "max_daily_loss_pct": 0.15,
                "max_drawdown_pct": 0.08,
                "max_slippage_pct": 0.02,
                "max_daily_api_cost": 50.0,
            },
            "prediction": {
                "min_edge": 0.04,
                "min_confidence": 0.65,
            },
            "bankroll": {
                "initial": 500.0,
            },
        }
        ex = TradeExecutor(settings=settings)
        ex.risk_manager.state = PortfolioState.default(500.0)
        return ex

    def test_execute_passing_signal(self):
        """Signal that passes risk should produce an open trade."""
        ex = self._make_executor()
        sig = _make_signal(edge=0.15, confidence=0.80, market_probability=0.50)
        trade = ex.execute_signal(sig)
        self.assertTrue(trade.risk_passed)
        self.assertEqual(trade.status, "open")
        self.assertGreater(trade.position_size_usd, 0)

    def test_execute_blocked_signal(self):
        """Signal that fails risk should produce a blocked trade."""
        ex = self._make_executor()
        sig = _make_signal(edge=0.01, confidence=0.20)  # Below thresholds
        trade = ex.execute_signal(sig)
        self.assertFalse(trade.risk_passed)
        self.assertEqual(trade.status, "blocked")
        self.assertEqual(trade.position_size_usd, 0)
        self.assertGreater(len(trade.risk_failures), 0)

    def test_trade_has_unique_id(self):
        ex = self._make_executor()
        t1 = ex.execute_signal(_make_signal(edge=0.15, confidence=0.80))
        t2 = ex.execute_signal(_make_signal(edge=0.15, confidence=0.80))
        self.assertNotEqual(t1.trade_id, t2.trade_id)

    def test_portfolio_state_updated_after_trade(self):
        ex = self._make_executor()
        initial = ex.risk_manager.state.current_bankroll
        sig = _make_signal(edge=0.15, confidence=0.80, market_probability=0.50)
        trade = ex.execute_signal(sig)
        if trade.risk_passed:
            self.assertLess(ex.risk_manager.state.current_bankroll, initial)
            self.assertEqual(ex.risk_manager.state.open_positions, 1)


class TestTradeContract(unittest.TestCase):
    def test_trade_has_all_s05_fields(self):
        required = [
            "trade_id", "entry_price", "position_size_usd",
            "status", "pnl", "risk_passed",
        ]
        actual = [f.name for f in fields(Trade)]
        for r in required:
            self.assertIn(r, actual, f"Missing S05 contract field: {r}")

    def test_trade_serializable(self):
        trade = Trade(
            trade_id="test123",
            market_id="MKT-001",
            market_title="Test",
            direction="buy_yes",
            entry_price=0.50,
            position_size_usd=10.0,
            model_probability=0.65,
            signal_strength=0.10,
            edge=0.15,
            status="open",
            pnl=0.0,
            risk_passed=True,
            risk_failures=[],
            kelly_fraction=0.05,
            timestamp="2026-01-01T00:00:00+00:00",
        )
        data = json.dumps(asdict(trade))
        restored = json.loads(data)
        self.assertEqual(restored["trade_id"], "test123")


if __name__ == "__main__":
    unittest.main()
