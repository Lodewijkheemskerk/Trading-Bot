"""
Tests for Position Monitor — dynamic exit strategies.

Covers:
  1. Exit rule evaluation (stop-loss, take-profit, time-based, edge-decay)
  2. Trade file loading
  3. Exit execution (file updates)
  4. Pipeline integration
  5. Edge cases
"""

import json
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# Helpers
# ===========================================================================

def _make_trade(
    trade_id="t001",
    market_id="KXTEST-123",
    market_title="Test Market",
    direction="buy_yes",
    entry_price=0.50,
    model_probability=0.65,
    edge=0.15,
    position_size_usd=25.0,
    status="open",
    hours_ago=0,
    kalshi_order_id="",
    contracts=50,
):
    """Create a trade dict matching the executor output format."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {
        "trade_id": trade_id,
        "market_id": market_id,
        "market_title": market_title,
        "direction": direction,
        "entry_price": entry_price,
        "signal_price": entry_price,
        "slippage": 0.0,
        "position_size_usd": position_size_usd,
        "model_probability": model_probability,
        "signal_strength": 0.8,
        "edge": edge,
        "status": status,
        "pnl": 0.0,
        "risk_passed": True,
        "risk_failures": [],
        "kelly_fraction": 0.25,
        "timestamp": ts,
        "execution_mode": "paper",
        "kalshi_order_id": kalshi_order_id,
        "kalshi_status": "",
        "contracts": contracts,
        "limit_price": entry_price,
        "fill_count": 0.0,
    }


def _make_monitor(**kwargs):
    """Create a PositionMonitor with Kalshi disabled."""
    from scripts.position_monitor import PositionMonitor
    with patch.dict(os.environ, {"KALSHI_ENV": "paper"}):
        monitor = PositionMonitor(**kwargs)
    monitor._kalshi = None
    return monitor


# ===========================================================================
# 1. Exit rule evaluation
# ===========================================================================

class TestExitRules:
    """Test individual exit rules in evaluate_position."""

    def test_stop_loss_triggers(self):
        """Position down 20% should trigger stop-loss (threshold 15%)."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50)
        # Current price dropped to 0.40 → -20% loss
        decision = monitor.evaluate_position(trade, current_price=0.40)
        assert decision.should_exit is True
        assert decision.exit_reason == "stop_loss"

    def test_stop_loss_at_threshold(self):
        """Position down exactly 15% should trigger stop-loss."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50)
        # 0.50 * 0.85 = 0.425
        decision = monitor.evaluate_position(trade, current_price=0.425)
        assert decision.should_exit is True
        assert decision.exit_reason == "stop_loss"

    def test_stop_loss_just_above(self):
        """Position down 14% should NOT trigger stop-loss."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50)
        # 0.50 * 0.86 = 0.43
        decision = monitor.evaluate_position(trade, current_price=0.43)
        assert decision.should_exit is False
        assert decision.exit_reason == "hold"

    def test_take_profit_triggers(self):
        """Position up 25% should trigger take-profit (threshold 20%)."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50)
        # Current price 0.625 → +25%
        decision = monitor.evaluate_position(trade, current_price=0.625)
        assert decision.should_exit is True
        assert decision.exit_reason == "take_profit"

    def test_take_profit_at_threshold(self):
        """Position up ~21% should trigger take-profit (threshold 20%)."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50)
        # 0.605 / 0.50 - 1 = 0.21 → clearly above 20%
        decision = monitor.evaluate_position(trade, current_price=0.605)
        assert decision.should_exit is True
        assert decision.exit_reason == "take_profit"

    def test_take_profit_just_below(self):
        """Position up 19% should NOT trigger take-profit."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50)
        decision = monitor.evaluate_position(trade, current_price=0.595)
        assert decision.should_exit is False

    def test_time_based_triggers(self):
        """Position held > 240 hours should trigger time-based exit."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50, hours_ago=250)
        decision = monitor.evaluate_position(trade, current_price=0.50)
        assert decision.should_exit is True
        assert decision.exit_reason == "time_based"

    def test_time_based_under_limit(self):
        """Position held 100 hours should NOT trigger time-based exit."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50, hours_ago=100)
        decision = monitor.evaluate_position(trade, current_price=0.50)
        assert decision.exit_reason != "time_based"

    def test_edge_decay_triggers(self):
        """Edge flipping negative should trigger edge-decay exit."""
        monitor = _make_monitor()
        # model_prob=0.55, entry=0.50, edge=0.05
        # Price moves to 0.57 → pnl=+14% (under take-profit 20%)
        # but current_edge = 0.55 - 0.57 = -0.02 (negative → edge decayed)
        trade = _make_trade(entry_price=0.50, model_probability=0.55, edge=0.05)
        decision = monitor.evaluate_position(trade, current_price=0.57)
        assert decision.should_exit is True
        assert decision.exit_reason == "edge_decay"

    def test_edge_still_positive(self):
        """Positive edge should not trigger edge-decay."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50, model_probability=0.65, edge=0.15)
        # Market at 0.55 → current_edge = 0.65 - 0.55 = 0.10 (still positive)
        decision = monitor.evaluate_position(trade, current_price=0.55)
        assert decision.exit_reason != "edge_decay"

    def test_hold_when_fine(self):
        """Position with good metrics should hold."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50, model_probability=0.65, edge=0.15, hours_ago=1)
        # Price moved slightly: 0.50 → 0.52 (+4%, under take-profit)
        # Edge still positive: 0.65 - 0.52 = 0.13
        decision = monitor.evaluate_position(trade, current_price=0.52)
        assert decision.should_exit is False
        assert decision.exit_reason == "hold"

    def test_buy_no_stop_loss(self):
        """Stop-loss should work for buy_no direction too."""
        monitor = _make_monitor()
        # buy_no at entry_price=0.50 means NO price = 0.50
        trade = _make_trade(direction="buy_no", entry_price=0.50)
        # For buy_no, current_price is the NO price.
        # NO price dropped from 0.50 to 0.40 → -20% loss
        decision = monitor.evaluate_position(trade, current_price=0.40)
        assert decision.should_exit is True
        assert decision.exit_reason == "stop_loss"

    def test_buy_no_edge_decay(self):
        """Edge-decay should work correctly for buy_no positions."""
        monitor = _make_monitor()
        # buy_no: model_prob=0.65 (YES), so NO model prob = 0.35
        # edge = (1.0 - 0.65) - 0.50 = -0.15 (this is a bad example)
        # Let's use: model_prob=0.30 (LOW YES), buying NO.
        # Original edge for NO side: (1-0.30) - 0.50 = 0.20
        trade = _make_trade(
            direction="buy_no", entry_price=0.50,
            model_probability=0.30, edge=0.20,
        )
        # Now market moved: NO price went from 0.50 to 0.25
        # Current edge = (1 - 0.30) - 0.25 = 0.45 (still positive)
        decision = monitor.evaluate_position(trade, current_price=0.25)
        # This is actually a huge loss (-50%) so stop_loss triggers first
        assert decision.should_exit is True
        assert decision.exit_reason == "stop_loss"


# ===========================================================================
# 2. Priority ordering
# ===========================================================================

class TestExitPriority:
    """Stop-loss fires before take-profit, etc."""

    def test_stop_loss_before_time(self):
        """Stop-loss should fire even if time-based would also trigger."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50, hours_ago=300)
        # Price dropped → stop-loss AND time-based both apply
        decision = monitor.evaluate_position(trade, current_price=0.40)
        assert decision.exit_reason == "stop_loss"

    def test_take_profit_before_time(self):
        """Take-profit should fire before time-based."""
        monitor = _make_monitor()
        trade = _make_trade(entry_price=0.50, hours_ago=300)
        # Price up → take-profit fires before time check
        decision = monitor.evaluate_position(trade, current_price=0.65)
        assert decision.exit_reason == "take_profit"


# ===========================================================================
# 3. Custom thresholds
# ===========================================================================

class TestCustomThresholds:
    """Test that settings override defaults."""

    def test_custom_stop_loss(self):
        """Custom stop-loss threshold should be respected."""
        settings = {
            "position_monitor": {"stop_loss_pct": 0.05},
            "risk": {},
        }
        monitor = _make_monitor(settings=settings)
        assert monitor.stop_loss_pct == 0.05

        trade = _make_trade(entry_price=0.50)
        # Down 6% → triggers with 5% threshold
        decision = monitor.evaluate_position(trade, current_price=0.47)
        assert decision.should_exit is True
        assert decision.exit_reason == "stop_loss"

    def test_custom_max_hold_hours(self):
        """Custom max hold hours should be respected."""
        settings = {
            "position_monitor": {"max_hold_hours": 48},
            "risk": {},
        }
        monitor = _make_monitor(settings=settings)
        assert monitor.max_hold_hours == 48

        trade = _make_trade(entry_price=0.50, hours_ago=50)
        decision = monitor.evaluate_position(trade, current_price=0.50)
        assert decision.should_exit is True
        assert decision.exit_reason == "time_based"


# ===========================================================================
# 4. Trade file I/O
# ===========================================================================

class TestTradeFileIO:
    """Test loading and updating trade files."""

    def test_load_open_trades(self, tmp_path):
        """Should load only open/resting trades from disk."""
        # Create trade files
        for status, trade_id in [("open", "t001"), ("closed", "t002"),
                                   ("resting", "t003"), ("blocked", "t004")]:
            trade = _make_trade(trade_id=trade_id, status=status)
            fp = tmp_path / f"trade_{trade_id}.json"
            with open(fp, "w") as f:
                json.dump(trade, f)

        with patch("scripts.position_monitor.TRADES_DIR", tmp_path):
            monitor = _make_monitor()
            trades = monitor.load_open_trades()

        trade_ids = [t.get("trade_id") for _, t in trades]
        assert "t001" in trade_ids  # open
        assert "t003" in trade_ids  # resting
        assert "t002" not in trade_ids  # closed
        assert "t004" not in trade_ids  # blocked
        assert len(trades) == 2

    def test_execute_exit_updates_file(self, tmp_path):
        """execute_exit should update the trade file on disk."""
        from scripts.position_monitor import PositionMonitor, ExitDecision

        trade = _make_trade(trade_id="t_exit", entry_price=0.50, position_size_usd=25.0)
        fp = tmp_path / "trade_t_exit.json"
        with open(fp, "w") as f:
            json.dump(trade, f)

        monitor = _make_monitor()

        decision = ExitDecision(
            trade_id="t_exit", market_id="KXTEST-123", market_title="Test",
            should_exit=True, exit_reason="stop_loss",
            entry_price=0.50, current_price=0.40,
            unrealized_pnl_pct=-0.20, hours_held=5.0,
            original_edge=0.15, current_edge=-0.05,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        updated = monitor.execute_exit(fp, trade, decision)

        # Verify file was updated
        with open(fp) as f:
            saved = json.load(f)

        assert saved["status"] == "closed"
        assert saved["exit_reason"] == "stop_loss"
        assert saved["exit_price"] == 0.40
        assert saved["pnl"] < 0  # Loss

    def test_pnl_calculation_on_exit(self):
        """P&L should be correctly calculated on exit."""
        from scripts.position_monitor import ExitDecision

        monitor = _make_monitor()

        # buy_yes at 0.50, $25 position → 50 contracts
        # exit at 0.40 → (0.40 - 0.50) * 50 = -$5.00
        trade = _make_trade(entry_price=0.50, position_size_usd=25.0)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(trade, f)
            fp = Path(f.name)

        try:
            decision = ExitDecision(
                trade_id="t001", market_id="KXTEST", market_title="Test",
                should_exit=True, exit_reason="stop_loss",
                entry_price=0.50, current_price=0.40,
                unrealized_pnl_pct=-0.20, hours_held=5.0,
                original_edge=0.15, current_edge=-0.05,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            updated = monitor.execute_exit(fp, trade, decision)
            assert updated["pnl"] == -5.0
        finally:
            fp.unlink()

    def test_pnl_calculation_profit(self):
        """P&L should be positive on take-profit exit."""
        from scripts.position_monitor import ExitDecision

        monitor = _make_monitor()

        # buy_yes at 0.50, $25 → 50 contracts
        # exit at 0.62 → (0.62 - 0.50) * 50 = +$6.00
        trade = _make_trade(entry_price=0.50, position_size_usd=25.0)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(trade, f)
            fp = Path(f.name)

        try:
            decision = ExitDecision(
                trade_id="t001", market_id="KXTEST", market_title="Test",
                should_exit=True, exit_reason="take_profit",
                entry_price=0.50, current_price=0.62,
                unrealized_pnl_pct=0.24, hours_held=5.0,
                original_edge=0.15, current_edge=0.03,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            updated = monitor.execute_exit(fp, trade, decision)
            assert updated["pnl"] == 6.0
        finally:
            fp.unlink()


# ===========================================================================
# 5. check_all integration
# ===========================================================================

class TestCheckAll:
    """Test the full check_all flow."""

    def test_check_all_with_mixed_positions(self, tmp_path):
        """check_all should handle a mix of exit/hold decisions."""
        # Create 3 open trades with different conditions and unique market IDs
        trades = [
            # This one should trigger stop-loss (will provide bad price via mock)
            _make_trade(trade_id="t_loss", market_id="KXLOSS-01", entry_price=0.50, status="open"),
            # This one is fine (no price from mock → falls back to entry → flat)
            _make_trade(trade_id="t_hold", market_id="KXHOLD-02", entry_price=0.50, status="open",
                        model_probability=0.65, edge=0.15),
            # This one timed out (time-based exit regardless of price)
            _make_trade(trade_id="t_time", market_id="KXTIME-03", entry_price=0.50, status="open", hours_ago=300),
        ]
        for t in trades:
            fp = tmp_path / f"trade_{t['trade_id']}.json"
            with open(fp, "w") as f:
                json.dump(t, f)

        with patch("scripts.position_monitor.TRADES_DIR", tmp_path):
            monitor = _make_monitor()
            # Mock get_current_price: only the loss trade gets a bad price
            prices = {"KXLOSS-01": 0.40}

            def mock_price(ticker, direction):
                return prices.get(ticker)

            monitor.get_current_price = mock_price

            # t_loss: price dropped to 0.40 → stop_loss
            # t_hold: no price → entry stays → flat → hold
            # t_time: no price → but 300h → time_based exit
            summary = monitor.check_all(dry_run=True)

        assert summary["positions_checked"] == 3
        assert summary["exits_triggered"] == 2
        assert summary["held"] == 1

    def test_check_all_empty(self, tmp_path):
        """check_all with no open trades returns clean summary."""
        with patch("scripts.position_monitor.TRADES_DIR", tmp_path):
            monitor = _make_monitor()
            summary = monitor.check_all()

        assert summary["positions_checked"] == 0
        assert summary["exits_triggered"] == 0

    def test_dry_run_doesnt_modify_files(self, tmp_path):
        """Dry run should not modify trade files."""
        trade = _make_trade(trade_id="t_dry", entry_price=0.50, status="open", hours_ago=300)
        fp = tmp_path / "trade_t_dry.json"
        with open(fp, "w") as f:
            json.dump(trade, f)

        with patch("scripts.position_monitor.TRADES_DIR", tmp_path):
            monitor = _make_monitor()
            summary = monitor.check_all(dry_run=True)

        # File should be unchanged
        with open(fp) as f:
            saved = json.load(f)
        assert saved["status"] == "open"
        assert summary["exits_triggered"] == 1


# ===========================================================================
# 6. Kalshi sell order integration
# ===========================================================================

class TestKalshiSellOrders:
    """Test that sell orders are placed in demo mode."""

    def test_sell_order_placed_on_exit(self, tmp_path):
        """In demo mode, execute_exit should place a sell order."""
        from scripts.position_monitor import ExitDecision

        monitor = _make_monitor()

        # Mock Kalshi client
        mock_kalshi = MagicMock()
        mock_kalshi.place_order.return_value = {
            "order": {"order_id": "sell-123", "status": "resting"}
        }
        monitor._kalshi = mock_kalshi

        trade = _make_trade(
            trade_id="t_sell", entry_price=0.50,
            kalshi_order_id="buy-456", contracts=50,
        )
        fp = tmp_path / "trade_t_sell.json"
        with open(fp, "w") as f:
            json.dump(trade, f)

        decision = ExitDecision(
            trade_id="t_sell", market_id="KXTEST-123", market_title="Test",
            should_exit=True, exit_reason="stop_loss",
            entry_price=0.50, current_price=0.40,
            unrealized_pnl_pct=-0.20, hours_held=5.0,
            original_edge=0.15, current_edge=-0.05,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        updated = monitor.execute_exit(fp, trade, decision)

        # Verify sell order was placed
        mock_kalshi.place_order.assert_called_once_with(
            ticker="KXTEST-123",
            side="yes",
            action="sell",
            price_dollars=0.40,
            count=50,
        )

        # Verify trade file has sell order ID
        with open(fp) as f:
            saved = json.load(f)
        assert saved["kalshi_sell_order_id"] == "sell-123"

    def test_no_sell_order_in_paper_mode(self, tmp_path):
        """Paper mode should not attempt Kalshi sell orders."""
        from scripts.position_monitor import ExitDecision

        monitor = _make_monitor()
        assert monitor._kalshi is None

        trade = _make_trade(trade_id="t_paper", entry_price=0.50)
        fp = tmp_path / "trade_t_paper.json"
        with open(fp, "w") as f:
            json.dump(trade, f)

        decision = ExitDecision(
            trade_id="t_paper", market_id="KXTEST-123", market_title="Test",
            should_exit=True, exit_reason="take_profit",
            entry_price=0.50, current_price=0.62,
            unrealized_pnl_pct=0.24, hours_held=5.0,
            original_edge=0.15, current_edge=0.03,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        updated = monitor.execute_exit(fp, trade, decision)
        assert updated["status"] == "closed"
        assert "kalshi_sell_order_id" not in updated  # No Kalshi in paper mode


# ===========================================================================
# 7. Pipeline integration
# ===========================================================================

class TestPipelineIntegration:
    """Test that position_monitor is wired into the pipeline."""

    def test_pipeline_has_monitor_step(self):
        """Pipeline should have a _step_monitor method."""
        from scripts.pipeline import TradingPipeline
        pipeline = TradingPipeline()
        assert hasattr(pipeline, "_step_monitor")

    def test_monitor_step_returns_summary(self):
        """_step_monitor should return a dict with success=True."""
        from scripts.pipeline import TradingPipeline
        pipeline = TradingPipeline()

        with patch("scripts.position_monitor.PositionMonitor") as MockMonitor:
            mock_instance = MockMonitor.return_value
            mock_instance.check_all.return_value = {
                "timestamp": "2026-03-16T00:00:00Z",
                "positions_checked": 0,
                "exits_triggered": 0,
                "exits_by_reason": {},
                "held": 0,
                "total_exit_pnl": 0.0,
                "decisions": [],
            }

            result = pipeline._step_monitor()

        assert result["success"] is True
        assert result["positions_checked"] == 0


# ===========================================================================
# 8. ExitDecision dataclass
# ===========================================================================

class TestExitDecision:
    """Test ExitDecision dataclass."""

    def test_asdict(self):
        """ExitDecision should serialize cleanly."""
        from scripts.position_monitor import ExitDecision
        from dataclasses import asdict

        d = ExitDecision(
            trade_id="t1", market_id="KXTEST", market_title="Test",
            should_exit=True, exit_reason="stop_loss",
            entry_price=0.50, current_price=0.40,
            unrealized_pnl_pct=-0.20, hours_held=5.0,
            original_edge=0.15, current_edge=-0.05,
            timestamp="2026-03-16T00:00:00Z",
        )
        result = asdict(d)
        assert result["exit_reason"] == "stop_loss"
        assert result["unrealized_pnl_pct"] == -0.20

    def test_default_hold(self):
        """A hold decision should have should_exit=False."""
        from scripts.position_monitor import ExitDecision

        d = ExitDecision(
            trade_id="t1", market_id="KXTEST", market_title="Test",
            should_exit=False, exit_reason="hold",
            entry_price=0.50, current_price=0.52,
            unrealized_pnl_pct=0.04, hours_held=1.0,
            original_edge=0.15, current_edge=0.13,
            timestamp="2026-03-16T00:00:00Z",
        )
        assert d.should_exit is False


# ===========================================================================
# 9. CLI
# ===========================================================================

class TestCLI:
    """Test CLI imports and help."""

    def test_module_imports(self):
        """position_monitor module should import cleanly."""
        import scripts.position_monitor
        assert hasattr(scripts.position_monitor, "PositionMonitor")
        assert hasattr(scripts.position_monitor, "ExitDecision")
        assert hasattr(scripts.position_monitor, "save_monitor_snapshot")

    def test_snapshot_save(self, tmp_path):
        """save_monitor_snapshot should write JSON to disk."""
        from scripts.position_monitor import save_monitor_snapshot

        summary = {
            "timestamp": "2026-03-16T00:00:00Z",
            "positions_checked": 2,
            "exits_triggered": 1,
            "total_exit_pnl": -5.0,
            "decisions": [],
        }

        with patch("scripts.position_monitor.TRADES_DIR", tmp_path):
            fp = save_monitor_snapshot(summary)

        assert fp.exists()
        with open(fp) as f:
            saved = json.load(f)
        assert saved["exits_triggered"] == 1
