"""
Tests for:
  - scripts/orderbook.py — orderbook depth checking
  - Trading hours gate in pipeline
  - Dashboard API endpoints for trading hours
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.orderbook import (
    parse_orderbook, check_depth, get_spread, get_midpoint, DepthCheck,
)


# ══════════════════════════════════════════════════════════════════
# Orderbook parsing
# ══════════════════════════════════════════════════════════════════

class TestParseOrderbook:
    def test_parse_normal(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.55", "120"], ["0.54", "80"]],
            "no_dollars": [["0.42", "90"]],
        }}
        yes, no = parse_orderbook(raw)
        assert len(yes) == 2
        assert yes[0] == (0.55, 120)
        assert yes[1] == (0.54, 80)
        assert no[0] == (0.42, 90)

    def test_parse_empty(self):
        yes, no = parse_orderbook({})
        assert yes == []
        assert no == []

    def test_parse_nested_key(self):
        """Handle both 'orderbook_fp' and 'orderbook' keys."""
        raw = {"orderbook": {
            "yes_dollars": [["0.60", "50"]],
            "no_dollars": [],
        }}
        yes, no = parse_orderbook(raw)
        assert len(yes) == 1
        assert no == []

    def test_parse_bad_entries_skipped(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.55", "120"], ["bad", "x"], ["0.50", "30"]],
            "no_dollars": [],
        }}
        yes, _ = parse_orderbook(raw)
        # "bad" entry is skipped
        assert len(yes) == 2


# ══════════════════════════════════════════════════════════════════
# Depth check
# ══════════════════════════════════════════════════════════════════

class TestCheckDepth:
    def test_sufficient_depth(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.55", "200"], ["0.54", "150"]],
            "no_dollars": [["0.42", "100"]],
        }}
        result = check_depth(raw, "MKT-1", "yes", 0.55, 50)
        assert result.sufficient is True
        assert result.depth_at_price >= 100  # 200 at 0.55, 50 * 2.0 = 100 required
        assert result.total_levels == 2

    def test_insufficient_depth(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.55", "10"]],
            "no_dollars": [],
        }}
        result = check_depth(raw, "MKT-1", "yes", 0.55, 50)
        assert result.sufficient is False
        assert "Thin book" in result.reason

    def test_empty_orderbook(self):
        raw = {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
        result = check_depth(raw, "MKT-1", "yes", 0.55, 10)
        assert result.sufficient is False
        assert "Empty orderbook" in result.reason

    def test_no_depth_at_price(self):
        # Resting orders only at 0.60, but we want 0.55
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.60", "500"]],
            "no_dollars": [],
        }}
        result = check_depth(raw, "MKT-1", "yes", 0.55, 10)
        assert result.sufficient is False
        assert "No resting depth" in result.reason

    def test_custom_min_depth_ratio(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.55", "60"]],
            "no_dollars": [],
        }}
        # 60 resting, want 50 contracts, ratio 1.0 → need 50 → OK
        result = check_depth(raw, "MKT-1", "yes", 0.55, 50, min_depth_ratio=1.0)
        assert result.sufficient is True

        # Same but ratio 2.0 → need 100 → NOT OK
        result = check_depth(raw, "MKT-1", "yes", 0.55, 50, min_depth_ratio=2.0)
        assert result.sufficient is False

    def test_no_side(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.55", "200"]],
            "no_dollars": [["0.40", "300"]],
        }}
        result = check_depth(raw, "MKT-1", "no", 0.45, 50)
        assert result.sufficient is True

    def test_spread_computed(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.55", "100"]],
            "no_dollars": [["0.42", "100"]],
        }}
        result = check_depth(raw, "MKT-1", "yes", 0.55, 10)
        # spread = 1.0 - 0.55 - 0.42 = 0.03
        assert result.spread == pytest.approx(0.03, abs=0.001)


# ══════════════════════════════════════════════════════════════════
# Spread & midpoint helpers
# ══════════════════════════════════════════════════════════════════

class TestSpreadMidpoint:
    def test_spread(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.55", "100"]],
            "no_dollars": [["0.42", "100"]],
        }}
        assert get_spread(raw) == pytest.approx(0.03, abs=0.001)

    def test_spread_empty(self):
        assert get_spread({}) is None

    def test_midpoint(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.55", "100"]],
            "no_dollars": [["0.42", "100"]],
        }}
        # midpoint = (0.55 + (1 - 0.42)) / 2 = (0.55 + 0.58) / 2 = 0.565
        assert get_midpoint(raw) == pytest.approx(0.565, abs=0.001)

    def test_midpoint_empty(self):
        assert get_midpoint({}) is None


# ══════════════════════════════════════════════════════════════════
# Trading hours gate
# ══════════════════════════════════════════════════════════════════

class TestTradingHours:
    def _make_pipeline(self, hours_cfg):
        settings = {"trading_hours": hours_cfg}
        from scripts.pipeline import TradingPipeline
        return TradingPipeline(settings=settings)

    def test_blackout_disabled_always_allows(self):
        p = self._make_pipeline({"maintenance_blackout": False})
        result = p._check_trading_hours()
        assert result["allowed"] is True

    def test_outside_blackout_window(self):
        """Normal trading time on a Thursday (outside 3-5 AM)."""
        p = self._make_pipeline({
            "maintenance_blackout": True, "blackout_day": 3,
            "blackout_start_hour": 3, "blackout_end_hour": 5,
            "blackout_timezone": "UTC",
        })
        with patch("scripts.pipeline.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 14
            mock_now.weekday.return_value = 3  # Thursday
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = p._check_trading_hours()
        assert result["allowed"] is True

    def test_inside_blackout_window(self):
        """During maintenance on a Thursday 4 AM."""
        p = self._make_pipeline({
            "maintenance_blackout": True, "blackout_day": 3,
            "blackout_start_hour": 3, "blackout_end_hour": 5,
            "blackout_timezone": "UTC",
        })
        with patch("scripts.pipeline.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 4
            mock_now.weekday.return_value = 3  # Thursday
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = p._check_trading_hours()
        assert result["allowed"] is False
        assert "maintenance" in result["reason"]

    def test_wrong_day_allows(self):
        """Same hour window but on a Wednesday — should be allowed."""
        p = self._make_pipeline({
            "maintenance_blackout": True, "blackout_day": 3,
            "blackout_start_hour": 3, "blackout_end_hour": 5,
            "blackout_timezone": "UTC",
        })
        with patch("scripts.pipeline.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 4
            mock_now.weekday.return_value = 2  # Wednesday
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = p._check_trading_hours()
        assert result["allowed"] is True

    def test_execute_skipped_during_maintenance(self):
        """Pipeline execute step should return skipped during blackout."""
        p = self._make_pipeline({
            "maintenance_blackout": True, "blackout_day": 3,
            "blackout_start_hour": 3, "blackout_end_hour": 5,
            "blackout_timezone": "UTC",
        })
        with patch("scripts.pipeline.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 4
            mock_now.weekday.return_value = 3  # Thursday
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = p._step_execute()
        assert result["success"] is True
        assert result["executed"] == 0
        assert "skipped_reason" in result


# ══════════════════════════════════════════════════════════════════
# Dashboard API
# ══════════════════════════════════════════════════════════════════

class TestDashboardAPI:
    def test_trading_hours_get(self):
        from dashboard.app import app
        client = app.test_client()
        resp = client.get("/api/trading-hours")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "schedule_minutes" in data
        assert "maintenance_blackout" in data
        assert "blackout_day" in data
        assert "blackout_start_hour" in data
        assert "blackout_end_hour" in data

    def test_settings_has_hours_tab(self):
        from dashboard.app import app
        client = app.test_client()
        resp = client.get("/settings")
        html = resp.data.decode()
        assert "tab-hours" in html
        assert "Maintenance" in html
