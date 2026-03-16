"""
Tests for backtester.py — Tier 5 Item 18.

Tests:
- Prediction loading and deduplication
- Metric computation (Brier, Sharpe, drawdown, calibration)
- BacktestTrade P&L calculation
- Result persistence
- Dashboard API endpoints
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backtester import (
    Backtester,
    BacktestTrade,
    BacktestResult,
    CalibrationBucket,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def backtester():
    return Backtester()


@pytest.fixture
def sample_predictions():
    """Realistic prediction signals."""
    return [
        {
            "market_id": "MKT-A", "market_title": "Will event A happen?",
            "ensemble_probability": 0.72, "market_probability": 0.50,
            "edge": 0.22, "direction": "buy_yes", "should_trade": True,
            "confidence": 0.75, "signal_strength": 0.165,
            "timestamp": "2026-03-10T10:00:00Z",
            "_snapshot_timestamp": "2026-03-10T10:00:00Z",
        },
        {
            "market_id": "MKT-B", "market_title": "Will event B happen?",
            "ensemble_probability": 0.30, "market_probability": 0.45,
            "edge": -0.15, "direction": "buy_no", "should_trade": True,
            "confidence": 0.70, "signal_strength": 0.105,
            "timestamp": "2026-03-10T10:00:00Z",
            "_snapshot_timestamp": "2026-03-10T10:00:00Z",
        },
        {
            "market_id": "MKT-C", "market_title": "Will event C happen?",
            "ensemble_probability": 0.55, "market_probability": 0.52,
            "edge": 0.03, "direction": "buy_yes", "should_trade": False,
            "confidence": 0.60, "signal_strength": 0.018,
            "timestamp": "2026-03-10T10:00:00Z",
            "_snapshot_timestamp": "2026-03-10T10:00:00Z",
        },
    ]


@pytest.fixture
def settled_trades():
    """BacktestTrades with known outcomes."""
    return [
        BacktestTrade(
            market_id="MKT-A", market_title="Event A", prediction_timestamp="2026-03-10",
            ensemble_probability=0.72, market_probability=0.50, edge=0.22,
            direction="buy_yes", should_trade=True, confidence=0.75, signal_strength=0.165,
            outcome=1.0, settlement_status="settled_yes",
            entry_price=0.50, pnl=50.0, pnl_pct=100.0, correct=True,
        ),
        BacktestTrade(
            market_id="MKT-B", market_title="Event B", prediction_timestamp="2026-03-10",
            ensemble_probability=0.30, market_probability=0.45, edge=-0.15,
            direction="buy_no", should_trade=True, confidence=0.70, signal_strength=0.105,
            outcome=0.0, settlement_status="settled_no",
            entry_price=0.45, pnl=30.0, pnl_pct=60.0, correct=True,
        ),
        BacktestTrade(
            market_id="MKT-C", market_title="Event C", prediction_timestamp="2026-03-10",
            ensemble_probability=0.55, market_probability=0.52, edge=0.03,
            direction="buy_yes", should_trade=False, confidence=0.60, signal_strength=0.018,
            outcome=1.0, settlement_status="settled_yes",
        ),
    ]


# ── Deduplication ─────────────────────────────────────────────────────────


class TestDeduplication:
    def test_keeps_latest_per_market(self, backtester):
        signals = [
            {"market_id": "A", "timestamp": "2026-03-10T08:00:00Z"},
            {"market_id": "A", "timestamp": "2026-03-10T12:00:00Z"},
            {"market_id": "B", "timestamp": "2026-03-10T08:00:00Z"},
        ]
        result = backtester.deduplicate_predictions(signals)
        assert len(result) == 2
        a = next(s for s in result if s["market_id"] == "A")
        assert a["timestamp"] == "2026-03-10T12:00:00Z"

    def test_single_prediction_per_market(self, backtester):
        signals = [
            {"market_id": "X", "timestamp": "2026-03-10T10:00:00Z"},
        ]
        result = backtester.deduplicate_predictions(signals)
        assert len(result) == 1

    def test_empty_input(self, backtester):
        assert backtester.deduplicate_predictions([]) == []


# ── Brier Score ───────────────────────────────────────────────────────────


class TestBrierScore:
    def test_perfect_predictions(self):
        trades = [
            BacktestTrade("A", "A", "", 1.0, 0.5, 0.5, "buy_yes", True, 0.9, 0.5, outcome=1.0, settlement_status="settled_yes"),
            BacktestTrade("B", "B", "", 0.0, 0.5, -0.5, "buy_no", True, 0.9, 0.5, outcome=0.0, settlement_status="settled_no"),
        ]
        brier = Backtester._compute_brier(trades)
        assert brier == 0.0

    def test_worst_predictions(self):
        trades = [
            BacktestTrade("A", "A", "", 0.0, 0.5, -0.5, "buy_no", True, 0.9, 0.5, outcome=1.0, settlement_status="settled_yes"),
            BacktestTrade("B", "B", "", 1.0, 0.5, 0.5, "buy_yes", True, 0.9, 0.5, outcome=0.0, settlement_status="settled_no"),
        ]
        brier = Backtester._compute_brier(trades)
        assert brier == 1.0

    def test_moderate_predictions(self):
        trades = [
            BacktestTrade("A", "A", "", 0.7, 0.5, 0.2, "buy_yes", True, 0.7, 0.1, outcome=1.0, settlement_status="settled_yes"),
            BacktestTrade("B", "B", "", 0.3, 0.5, -0.2, "buy_no", True, 0.7, 0.1, outcome=0.0, settlement_status="settled_no"),
        ]
        brier = Backtester._compute_brier(trades)
        assert brier == 0.09  # (0.3^2 + 0.3^2) / 2

    def test_empty_trades(self):
        assert Backtester._compute_brier([]) == 0.0


# ── Sharpe Ratio ──────────────────────────────────────────────────────────


class TestSharpe:
    def test_positive_consistent_returns(self):
        pnls = [10.0, 12.0, 8.0, 11.0, 9.0]
        sharpe = Backtester._compute_sharpe(pnls)
        assert sharpe > 0

    def test_negative_returns(self):
        pnls = [-5.0, -3.0, -8.0, -2.0]
        sharpe = Backtester._compute_sharpe(pnls)
        assert sharpe < 0

    def test_single_trade(self):
        assert Backtester._compute_sharpe([10.0]) == 0.0

    def test_empty(self):
        assert Backtester._compute_sharpe([]) == 0.0


# ── Max Drawdown ──────────────────────────────────────────────────────────


class TestMaxDrawdown:
    def test_no_drawdown(self):
        pnls = [10.0, 10.0, 10.0]
        dd = Backtester._compute_max_drawdown(pnls)
        assert dd == 0.0

    def test_drawdown_after_peak(self):
        pnls = [10.0, 10.0, -25.0, 5.0]
        dd = Backtester._compute_max_drawdown(pnls)
        # cum = [10, 20, -5, 0], peak = [10, 20, 20, 20], dd = [0, 0, -25, -20], min = -25
        assert dd == -25.0

    def test_empty(self):
        assert Backtester._compute_max_drawdown([]) == 0.0


# ── Calibration ───────────────────────────────────────────────────────────


class TestCalibration:
    def test_returns_10_buckets(self):
        trades = [
            BacktestTrade("A", "A", "", 0.15, 0.5, -0.35, "buy_no", False, 0.5, 0.1, outcome=0.0, settlement_status="settled_no"),
            BacktestTrade("B", "B", "", 0.75, 0.5, 0.25, "buy_yes", True, 0.8, 0.2, outcome=1.0, settlement_status="settled_yes"),
        ]
        cal = Backtester._compute_calibration(trades)
        assert len(cal) == 10
        assert all(isinstance(b, CalibrationBucket) for b in cal)

    def test_bucket_labels(self):
        cal = Backtester._compute_calibration([])
        labels = [b.label for b in cal]
        assert labels[0] == "0-10%"
        assert labels[-1] == "90-100%"


# ── Full Metrics ──────────────────────────────────────────────────────────


class TestComputeMetrics:
    def test_with_settled_trades(self, backtester, settled_trades):
        result = backtester._compute_metrics(settled_trades)
        assert isinstance(result, BacktestResult)
        assert result.settled_count == 3
        assert result.trades_taken == 2
        assert result.wins == 2
        assert result.win_rate == 1.0
        assert result.total_pnl == 80.0

    def test_empty_trades(self, backtester):
        result = backtester._compute_metrics([])
        assert result.settled_count == 0
        assert result.brier_score == 0.0
        assert result.trades_taken == 0


# ── Result Persistence ────────────────────────────────────────────────────


class TestPersistence:
    def test_save_result(self, backtester, settled_trades, tmp_path):
        import scripts.backtester as bmod
        orig = bmod.BACKTEST_DIR
        bmod.BACKTEST_DIR = tmp_path

        try:
            result = backtester._compute_metrics(settled_trades)
            fp = backtester._save_result(result)
            assert fp.exists()
            data = json.loads(fp.read_text())
            assert data["trades_taken"] == 2
        finally:
            bmod.BACKTEST_DIR = orig

    def test_load_latest_result(self, tmp_path):
        import scripts.backtester as bmod
        orig = bmod.BACKTEST_DIR
        bmod.BACKTEST_DIR = tmp_path

        try:
            # No files
            assert Backtester.load_latest_result() is None

            # Write one
            (tmp_path / "backtest_20260316_120000.json").write_text(
                json.dumps({"trades_taken": 5, "brier_score": 0.21})
            )
            result = Backtester.load_latest_result()
            assert result is not None
            assert result["trades_taken"] == 5
        finally:
            bmod.BACKTEST_DIR = orig


# ── Dashboard API ─────────────────────────────────────────────────────────


class TestDashboardAPI:
    def test_backtest_page_route(self):
        """Flask should serve backtest.html."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from dashboard.app import app
        client = app.test_client()
        resp = client.get("/backtest")
        assert resp.status_code == 200

    def test_backtest_api_get(self):
        from dashboard.app import app
        client = app.test_client()
        resp = client.get("/api/backtest")
        assert resp.status_code == 200
        data = resp.get_json()
        # Either has results or "empty" flag
        assert "empty" in data or "trades_taken" in data

    def test_navigation_links_exist(self):
        """All three pages should have consistent navigation."""
        from dashboard.app import app
        client = app.test_client()

        for path in ["/backtest"]:
            resp = client.get(path)
            html = resp.data.decode()
            assert 'href="/"' in html
            assert 'href="/backtest"' in html
            assert 'href="/settings"' in html
