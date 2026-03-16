"""
Tests for Tier 3 improvements:
  1. Retry/reconnection logic (exponential backoff)
  2. Slippage monitoring (signal_price vs entry_price)
  3. Nightly review scheduler (clock-triggered in pipeline loop)
  4. Volume spike detection (7-day rolling average)
"""

import json
import sys
import time
import tempfile
import shutil
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# 1. Retry logic
# ===========================================================================

class TestRetry:
    """Test the shared retry utility."""

    def test_succeeds_first_try(self):
        """Function that works immediately should return its value."""
        from scripts.retry import retry_call
        result = retry_call(lambda: 42, max_attempts=3)
        assert result == 42

    def test_retries_on_transient_error(self):
        """Should retry and succeed on second attempt."""
        from scripts.retry import retry_call

        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] < 2:
                raise ConnectionError("transient")
            return "ok"

        result = retry_call(flaky, max_attempts=3, base_delay=0.01)
        assert result == "ok"
        assert call_count[0] == 2

    def test_raises_after_max_attempts(self):
        """Should raise the last exception after exhausting retries."""
        from scripts.retry import retry_call
        import pytest

        def always_fail():
            raise ConnectionError("permanent")

        with pytest.raises(ConnectionError):
            retry_call(always_fail, max_attempts=2, base_delay=0.01)

    def test_non_retryable_exception_raises_immediately(self):
        """Non-retryable exceptions should not trigger retries."""
        from scripts.retry import retry_call
        import pytest

        call_count = [0]

        def bad_code():
            call_count[0] += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            retry_call(bad_code, max_attempts=3, base_delay=0.01)

        assert call_count[0] == 1  # No retries for ValueError

    def test_decorator_version(self):
        """@with_retry decorator should work the same as retry_call."""
        from scripts.retry import with_retry

        call_count = [0]

        @with_retry(max_attempts=3, base_delay=0.01)
        def flaky_fn():
            call_count[0] += 1
            if call_count[0] < 2:
                raise ConnectionError("transient")
            return "decorated_ok"

        result = flaky_fn()
        assert result == "decorated_ok"
        assert call_count[0] == 2

    def test_scanner_has_retry_config(self):
        """Scanner should load retry config from settings."""
        from scripts.scanner import MarketScanner
        s = MarketScanner()
        assert s.retry_attempts == 3
        assert s.retry_delay == 5

    def test_researcher_has_retry_config(self):
        """Researcher should load retry config from settings."""
        from scripts.researcher import NewsResearcher
        r = NewsResearcher()
        assert r.retry_attempts == 3
        assert r.retry_delay == 5


# ===========================================================================
# 2. Slippage monitoring
# ===========================================================================

class TestSlippage:
    """Test signal-to-fill slippage tracking."""

    def test_trade_has_slippage_fields(self):
        """Trade dataclass should have signal_price and slippage."""
        from scripts.executor import Trade
        t = Trade(
            trade_id="test", market_id="TEST", market_title="Test",
            direction="buy_yes", entry_price=0.52, signal_price=0.50,
            slippage=0.02, position_size_usd=10, model_probability=0.6,
            signal_strength=0.1, edge=0.1, status="open", pnl=0,
            risk_passed=True, risk_failures=[], kelly_fraction=0.05,
            timestamp="now",
        )
        assert t.signal_price == 0.50
        assert t.slippage == 0.02

    def test_paper_trade_zero_slippage(self):
        """In paper mode, signal_price equals entry_price so slippage is 0."""
        from scripts.executor import TradeExecutor
        executor = TradeExecutor()

        signal = {
            "market_id": "KXTEST",
            "market_title": "Test",
            "direction": "buy_yes",
            "market_probability": 0.50,
            "ensemble_probability": 0.60,
            "edge": 0.10,
            "signal_strength": 0.07,
            "confidence": 0.70,
            "should_trade": True,
        }
        trade = executor.execute_signal(signal)
        assert trade.signal_price == trade.entry_price
        assert trade.slippage == 0.0

    def test_slippage_risk_check_detects_drift(self):
        """Risk check #8 should detect signal-to-fill drift."""
        from scripts.validate_risk import RiskManager
        rm = RiskManager()
        rm.max_slippage_pct = 0.02  # 2% max

        # Signal with 3% drift (exceeds threshold)
        signal = {
            "edge": 0.06,
            "confidence": 0.70,
            "market_probability": 0.53,
            "signal_price": 0.50,  # Was 0.50 at signal time, now 0.53
        }
        result = rm.validate_trade(signal, position_size_usd=10.0)
        slip_check = next(c for c in result.checks if c.name == "max_slippage")
        assert not slip_check.passed, f"Should fail: {slip_check.detail}"

    def test_slippage_risk_check_passes_small_drift(self):
        """Risk check #8 should pass for small drift."""
        from scripts.validate_risk import RiskManager
        rm = RiskManager()
        rm.max_slippage_pct = 0.02

        signal = {
            "edge": 0.06,
            "confidence": 0.70,
            "market_probability": 0.51,
            "signal_price": 0.50,  # Only 1% drift
        }
        result = rm.validate_trade(signal, position_size_usd=10.0)
        slip_check = next(c for c in result.checks if c.name == "max_slippage")
        assert slip_check.passed, f"Should pass: {slip_check.detail}"


# ===========================================================================
# 3. Nightly review scheduler
# ===========================================================================

class TestNightlyReview:
    """Test the clock-triggered nightly review in the pipeline."""

    def test_review_triggers_at_configured_hour(self):
        """Review should trigger when current hour matches config."""
        from scripts.pipeline import TradingPipeline
        from datetime import datetime

        p = TradingPipeline()
        p.settings["compound"]["nightly_review_hour"] = 14  # 2 PM

        mock_now = datetime(2026, 3, 16, 14, 15, 0)

        with patch("scripts.pipeline.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with patch("scripts.compounder.Compounder") as MockCompounder:
                mock_instance = MockCompounder.return_value
                mock_instance.nightly_review.return_value = "## Review"

                # Need to also patch open for the review file save
                from unittest.mock import mock_open
                m = mock_open()
                with patch("builtins.open", m):
                    p._maybe_run_nightly_review()

                mock_instance.nightly_review.assert_called_once()
                assert p._nightly_review_done_date == "2026-03-16"

    def test_review_does_not_run_twice_same_day(self):
        """Review should only run once per day."""
        from scripts.pipeline import TradingPipeline
        from datetime import datetime

        p = TradingPipeline()
        p.settings["compound"]["nightly_review_hour"] = 14
        p._nightly_review_done_date = "2026-03-16"  # Already done

        mock_now = datetime(2026, 3, 16, 14, 30, 0)

        with patch("scripts.pipeline.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now

            with patch("scripts.compounder.Compounder") as MockCompounder:
                p._maybe_run_nightly_review()
                MockCompounder.assert_not_called()  # Should NOT have been called

    def test_review_skips_wrong_hour(self):
        """Review should not trigger outside the configured hour."""
        from scripts.pipeline import TradingPipeline
        from datetime import datetime

        p = TradingPipeline()
        p.settings["compound"]["nightly_review_hour"] = 23

        mock_now = datetime(2026, 3, 16, 10, 0, 0)  # 10 AM, not 11 PM

        with patch("scripts.pipeline.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now

            with patch("scripts.compounder.Compounder") as MockCompounder:
                p._maybe_run_nightly_review()
                MockCompounder.assert_not_called()


# ===========================================================================
# 4. Volume spike detection
# ===========================================================================

class TestVolumeSpike:
    """Test 7-day rolling average volume spike detection."""

    def _make_scanner_with_history(self, history):
        """Create a scanner with injected volume history."""
        from scripts.scanner import MarketScanner
        s = MarketScanner()
        s._volume_history = history
        return s

    def test_no_history_returns_none(self):
        """No history data should return None (can't compute average)."""
        from scripts.scanner import MarketScanner
        s = MarketScanner()
        assert s._get_7day_avg_volume("UNKNOWN") is None

    def test_single_day_returns_none(self):
        """Single day of history is not enough for spike detection."""
        s = self._make_scanner_with_history({
            "KXTEST": {date.today().isoformat(): 100.0},
        })
        assert s._get_7day_avg_volume("KXTEST") is None

    def test_computes_average(self):
        """Should compute correct 7-day average from history."""
        today = date.today()
        history = {
            "KXTEST": {
                (today - timedelta(days=1)).isoformat(): 100.0,
                (today - timedelta(days=2)).isoformat(): 200.0,
                (today - timedelta(days=3)).isoformat(): 300.0,
            },
        }
        s = self._make_scanner_with_history(history)
        avg = s._get_7day_avg_volume("KXTEST")
        assert avg == 200.0  # (100 + 200 + 300) / 3

    def test_spike_flagged_in_anomaly_check(self):
        """Volume spike should be flagged as anomaly."""
        from scripts.scanner import MarketScanner, Market

        today = date.today()
        history = {
            "KXTEST": {
                (today - timedelta(days=i)).isoformat(): 100.0
                for i in range(1, 8)
            },
        }
        s = self._make_scanner_with_history(history)
        s.volume_spike_mult = 3.0  # Flag at 3x average

        m = Market(
            platform="kalshi", market_id="KXTEST", title="Test",
            description="", category="test", event_ticker="KXTEST",
            yes_price=0.50, no_price=0.50, yes_bid=0.49, spread=0.01,
            volume_24h=500.0,  # 5x the 100 avg -> spike!
            total_volume=10000, liquidity=100, open_interest=50,
            expiry_date="2026-04-01", days_to_expiry=16,
            last_price=0.50, url="",
        )
        s._check_anomalies(m)
        assert m.is_anomaly is True
        assert "volume spike" in m.anomaly_reasons
        assert m.volume_spike == 5.0

    def test_no_spike_below_threshold(self):
        """Volume below threshold should not flag."""
        from scripts.scanner import Market

        today = date.today()
        history = {
            "KXTEST": {
                (today - timedelta(days=i)).isoformat(): 100.0
                for i in range(1, 8)
            },
        }
        s = self._make_scanner_with_history(history)
        s.volume_spike_mult = 3.0

        m = Market(
            platform="kalshi", market_id="KXTEST", title="Test",
            description="", category="test", event_ticker="KXTEST",
            yes_price=0.50, no_price=0.50, yes_bid=0.49, spread=0.01,
            volume_24h=150.0,  # 1.5x avg -> not a spike
            total_volume=10000, liquidity=100, open_interest=50,
            expiry_date="2026-04-01", days_to_expiry=16,
            last_price=0.50, url="",
        )
        s._check_anomalies(m)
        assert "volume spike" not in m.anomaly_reasons

    def test_record_and_persist(self):
        """Volume recording should update history."""
        from scripts.scanner import MarketScanner
        s = MarketScanner()
        s._volume_history = {}
        s._record_volume("KXTEST", 500.0)
        today = date.today().isoformat()
        assert s._volume_history["KXTEST"][today] == 500.0

    def test_prune_old_history(self):
        """Old entries beyond max_days should be pruned."""
        from scripts.scanner import MarketScanner

        old_date = (date.today() - timedelta(days=30)).isoformat()
        recent_date = (date.today() - timedelta(days=1)).isoformat()

        s = MarketScanner()
        s._volume_history = {
            "KXTEST": {
                old_date: 100.0,
                recent_date: 200.0,
            },
        }
        s._prune_old_volume_history(max_days=14)

        assert old_date not in s._volume_history.get("KXTEST", {})
        assert recent_date in s._volume_history.get("KXTEST", {})
