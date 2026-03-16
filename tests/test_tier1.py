"""
Tests for Tier 1 improvements:
  1. Trade outcome resolution (resolver.py)
  2. Failure categorization (compounder.py)
  3. Failure log feedback loop (scanner + researcher read failure_log.md)

These tests use local test data — no API calls.
"""

import json
import math
import os
import sys
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# 1. resolver.py — P&L computation
# ===========================================================================

class TestComputePnl:
    """Test the core P&L calculation for binary prediction markets."""

    def test_buy_yes_wins(self):
        from scripts.resolver import compute_pnl
        # Buy YES at $0.40, event happens → profit
        pnl = compute_pnl("buy_yes", 0.40, 10.0, outcome_yes=True)
        # 10 / 0.40 = 25 contracts, each pays (1 - 0.40) = $0.60 profit
        assert pnl == 15.0, f"Expected 15.0, got {pnl}"

    def test_buy_yes_loses(self):
        from scripts.resolver import compute_pnl
        # Buy YES at $0.40, event doesn't happen → lose investment
        pnl = compute_pnl("buy_yes", 0.40, 10.0, outcome_yes=False)
        assert pnl == -10.0, f"Expected -10.0, got {pnl}"

    def test_buy_no_wins(self):
        from scripts.resolver import compute_pnl
        # Buy NO when YES is at $0.70 → NO costs $0.30
        # Event doesn't happen → NO wins
        pnl = compute_pnl("buy_no", 0.70, 10.0, outcome_yes=False)
        # NO price = 1 - 0.70 = 0.30, contracts = 10 / 0.30 ≈ 33.33
        # Each pays (1 - 0.30) = $0.70, profit = 0.70 * 33.33 ≈ 23.33
        assert abs(pnl - 23.33) < 0.01, f"Expected ~23.33, got {pnl}"

    def test_buy_no_loses(self):
        from scripts.resolver import compute_pnl
        # Buy NO when YES is at $0.70, event happens → lose investment
        pnl = compute_pnl("buy_no", 0.70, 10.0, outcome_yes=True)
        assert pnl == -10.0, f"Expected -10.0, got {pnl}"

    def test_zero_position(self):
        from scripts.resolver import compute_pnl
        pnl = compute_pnl("buy_yes", 0.50, 0.0, outcome_yes=True)
        assert pnl == 0.0


# ===========================================================================
# 2. resolver.py — Trade resolution flow
# ===========================================================================

class TestTradeResolver:
    """Test the resolver loads open trades, checks Kalshi, and updates files."""

    def setup_method(self):
        """Create a temp directory with test trade files."""
        self.tmpdir = Path(tempfile.mkdtemp())

        # Open trade
        self.open_trade = {
            "trade_id": "test_open_001",
            "market_id": "KXTEST-OPEN",
            "market_title": "Will test event happen?",
            "direction": "buy_yes",
            "entry_price": 0.40,
            "position_size_usd": 10.0,
            "model_probability": 0.65,
            "signal_strength": 0.1,
            "edge": 0.25,
            "status": "open",
            "pnl": 0.0,
            "risk_passed": True,
            "risk_failures": [],
            "kelly_fraction": 0.05,
            "timestamp": "2026-03-13T12:00:00+00:00",
        }
        with open(self.tmpdir / "trade_test_open_001.json", "w") as f:
            json.dump(self.open_trade, f)

        # Closed trade (should be skipped)
        closed_trade = dict(self.open_trade)
        closed_trade["trade_id"] = "test_closed_001"
        closed_trade["status"] = "closed"
        closed_trade["pnl"] = 5.0
        with open(self.tmpdir / "trade_test_closed_001.json", "w") as f:
            json.dump(closed_trade, f)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_loads_only_open_trades(self):
        from scripts.resolver import TradeResolver
        resolver = TradeResolver()

        with patch("scripts.resolver.TRADES_DIR", self.tmpdir):
            open_trades = resolver.load_open_trades()

        assert len(open_trades) == 1
        fp, trade = open_trades[0]
        assert trade["trade_id"] == "test_open_001"
        assert trade["status"] == "open"

    def test_resolve_settled_market(self):
        """Mock Kalshi API returning a settled market and verify trade gets closed."""
        from scripts.resolver import TradeResolver

        # Mock Kalshi returning settled YES
        mock_market = {
            "ticker": "KXTEST-OPEN",
            "status": "settled",
            "result": "yes",
            "last_price_dollars": "1.00",
        }

        resolver = TradeResolver()
        resolver.checker.get_market = MagicMock(return_value=mock_market)

        with patch("scripts.resolver.TRADES_DIR", self.tmpdir):
            summary = resolver.resolve_all()

        assert summary["resolved"] == 1
        assert summary["still_open"] == 0
        assert summary["total_pnl_resolved"] == 15.0  # Buy YES at 0.40, wins

        # Check the trade file was updated
        with open(self.tmpdir / "trade_test_open_001.json") as f:
            updated = json.load(f)
        assert updated["status"] == "closed"
        assert updated["pnl"] == 15.0
        assert updated["outcome"] == "yes"
        assert "resolved_at" in updated

    def test_resolve_still_open(self):
        """Market still active — trade stays open."""
        from scripts.resolver import TradeResolver

        mock_market = {
            "ticker": "KXTEST-OPEN",
            "status": "active",
        }

        resolver = TradeResolver()
        resolver.checker.get_market = MagicMock(return_value=mock_market)

        with patch("scripts.resolver.TRADES_DIR", self.tmpdir):
            summary = resolver.resolve_all()

        assert summary["resolved"] == 0
        assert summary["still_open"] == 1

    def test_dry_run_no_file_changes(self):
        """Dry run shows what would resolve without modifying files."""
        from scripts.resolver import TradeResolver

        mock_market = {
            "ticker": "KXTEST-OPEN",
            "status": "settled",
            "result": "no",
        }

        resolver = TradeResolver()
        resolver.checker.get_market = MagicMock(return_value=mock_market)

        with patch("scripts.resolver.TRADES_DIR", self.tmpdir):
            summary = resolver.resolve_all(dry_run=True)

        assert summary["resolved"] == 1

        # File should NOT be modified in dry run
        with open(self.tmpdir / "trade_test_open_001.json") as f:
            unchanged = json.load(f)
        assert unchanged["status"] == "open"
        assert unchanged["pnl"] == 0.0


# ===========================================================================
# 3. compounder.py — Failure categorization
# ===========================================================================

class TestFailureCategorization:
    """Test the rule-based failure classification logic (post-mortem disabled)."""

    def _make_compounder(self):
        """Create a Compounder with post-mortem disabled for isolated testing."""
        from scripts.compounder import Compounder
        c = Compounder()
        c.post_mortem_enabled = False  # Test rule-based logic in isolation
        return c

    def _make_trade(self, **overrides):
        trade = {
            "trade_id": "test_001",
            "market_id": "KXTEST",
            "market_title": "Test Market",
            "direction": "buy_yes",
            "entry_price": 0.50,
            "exit_price": 0.0,
            "position_size_usd": 10.0,
            "model_probability": 0.70,
            "edge": 0.20,
            "status": "closed",
            "pnl": -10.0,
            "outcome": "no",
            "risk_passed": True,
            "risk_failures": [],
            "kelly_fraction": 0.05,
            "timestamp": "2026-03-13T12:00:00+00:00",
        }
        trade.update(overrides)
        return trade

    def test_bad_prediction(self):
        """Model was confident (70%) but event didn't happen → Bad Prediction."""
        from scripts.compounder import FAILURE_BAD_PREDICTION
        c = self._make_compounder()
        trade = self._make_trade(model_probability=0.75, entry_price=0.50, outcome="no")
        analysis = c.analyze_trade(trade)
        assert analysis["failure_category"] == FAILURE_BAD_PREDICTION

    def test_bad_timing(self):
        """Edge was marginal, model wasn't far off → Bad Timing."""
        from scripts.compounder import FAILURE_BAD_TIMING
        c = self._make_compounder()
        trade = self._make_trade(
            model_probability=0.38, entry_price=0.33, edge=0.05,
            outcome="no", pnl=-10.0,
        )
        analysis = c.analyze_trade(trade)
        assert analysis["failure_category"] == FAILURE_BAD_TIMING, \
            f"Expected Bad Timing, got {analysis.get('failure_category')}"

    def test_external_shock(self):
        """Market was pricing YES at 80%, outcome was NO → External Shock."""
        from scripts.compounder import FAILURE_EXTERNAL_SHOCK
        c = self._make_compounder()
        # Market at 0.80 (YES), model at 0.85, outcome NO
        # market_error = |0.80 - 0| = 0.80 > 0.65 → External Shock
        trade = self._make_trade(
            model_probability=0.85, entry_price=0.80, edge=0.05,
            outcome="no", pnl=-10.0,
        )
        analysis = c.analyze_trade(trade)
        assert analysis["failure_category"] == FAILURE_EXTERNAL_SHOCK

    def test_win_has_no_failure_category(self):
        """Winning trade should have no failure category."""
        c = self._make_compounder()
        trade = self._make_trade(
            model_probability=0.70, entry_price=0.50, outcome="yes", pnl=10.0,
        )
        analysis = c.analyze_trade(trade)
        assert analysis["failure_category"] is None
        assert analysis["correct_prediction"] is True


# ===========================================================================
# 4. compounder.py — Failure log writing and reading
# ===========================================================================

class TestFailureLog:
    """Test writing to and reading from failure_log.md."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.log_fp = self.tmpdir / "failure_log.md"
        # Write initial content matching the project template
        self.log_fp.write_text(
            "# Failure Log\n\n"
            "## Entries\n\n"
            "*(No failures logged yet)*\n"
        )

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_append_and_read_failures(self):
        from scripts.compounder import Compounder

        c = Compounder()

        analyses = [
            {
                "trade_id": "test_001",
                "market_id": "KXTEST-MKT1",
                "market_title": "Will X happen?",
                "failure_category": "Bad Prediction",
                "entry_price": 0.50,
                "exit_price": 0.0,
                "model_probability": 0.75,
                "actual_outcome": "no",
                "pnl": -10.0,
                "root_cause": "Model was overconfident",
                "lesson": "Don't trust sentiment when data is thin",
                "action_taken": "Review calibration",
                "date": "2026-03-16",
            },
            {
                "trade_id": "test_002",
                "market_id": "KXTEST-MKT2",
                "market_title": "Will Y happen?",
                "failure_category": "External Shock",
                "entry_price": 0.80,
                "exit_price": 0.0,
                "model_probability": 0.85,
                "actual_outcome": "no",
                "pnl": -15.0,
                "root_cause": "Unexpected event",
                "lesson": "This market category is volatile",
                "action_taken": "Flag for higher uncertainty",
                "date": "2026-03-16",
            },
        ]

        with patch("scripts.compounder.REFERENCES_DIR", self.tmpdir):
            count = c.append_failures_to_log(analyses)

        assert count == 2

        # Now read them back
        with patch("scripts.compounder.REFERENCES_DIR", self.tmpdir):
            entries = Compounder.load_failure_log()

        assert len(entries) == 2
        assert entries[0]["market_id"] == "KXTEST-MKT1"
        assert entries[0]["category"] == "Bad Prediction"
        assert "overconfident" in entries[0].get("root_cause", "").lower() or \
               "thin" in entries[0].get("lesson", "").lower()

        assert entries[1]["market_id"] == "KXTEST-MKT2"
        assert entries[1]["category"] == "External Shock"

    def test_no_failures_returns_empty(self):
        from scripts.compounder import Compounder
        # Point to a dir with no failure_log.md
        empty_dir = self.tmpdir / "empty"
        empty_dir.mkdir()
        with patch("scripts.compounder.REFERENCES_DIR", empty_dir):
            entries = Compounder.load_failure_log()
        assert entries == []


# ===========================================================================
# 4b. compounder.py — LLM post-mortem
# ===========================================================================

class TestPostMortem:
    """Test the LLM post-mortem analysis on losing trades."""

    def _make_trade(self, **overrides):
        trade = {
            "trade_id": "test_pm_001",
            "market_id": "KXTEST-PM",
            "market_title": "Will post-mortem test event happen?",
            "direction": "buy_yes",
            "entry_price": 0.50,
            "exit_price": 0.0,
            "position_size_usd": 10.0,
            "model_probability": 0.70,
            "edge": 0.20,
            "signal_strength": 0.14,
            "status": "closed",
            "pnl": -10.0,
            "outcome": "no",
            "risk_passed": True,
            "risk_failures": [],
            "kelly_fraction": 0.05,
            "timestamp": "2026-03-16T12:00:00+00:00",
        }
        trade.update(overrides)
        return trade

    def test_post_mortem_disabled_returns_rule_based(self):
        """When post_mortem is disabled, rule-based result passes through unchanged."""
        from scripts.compounder import Compounder, FailureEntry, FAILURE_BAD_PREDICTION

        c = Compounder()
        c.post_mortem_enabled = False

        rule_based = FailureEntry(
            date="2026-03-16", market_id="KXTEST", market_title="Test",
            category=FAILURE_BAD_PREDICTION, entry_price=0.5, exit_price=0.0,
            model_probability=0.7, actual_outcome="no", pnl=-10.0,
            root_cause="Rule-based cause", lesson="Rule-based lesson",
            action_taken="Rule-based action",
        )

        result = c._run_post_mortem(self._make_trade(), rule_based)
        assert result.category == FAILURE_BAD_PREDICTION
        assert result.lesson == "Rule-based lesson"

    def test_post_mortem_no_api_keys_returns_rule_based(self):
        """When no API keys are set, falls back to rule-based."""
        from scripts.compounder import Compounder, FailureEntry, FAILURE_BAD_PREDICTION

        c = Compounder()
        c.post_mortem_enabled = True
        c.post_mortem_models = [
            {"name": "test_model", "model_id": "test", "env_key": "NONEXISTENT_KEY_12345", "provider": "openai"},
        ]

        rule_based = FailureEntry(
            date="2026-03-16", market_id="KXTEST", market_title="Test",
            category=FAILURE_BAD_PREDICTION, entry_price=0.5, exit_price=0.0,
            model_probability=0.7, actual_outcome="no", pnl=-10.0,
            root_cause="Rule cause", lesson="Rule lesson",
            action_taken="Rule action",
        )

        result = c._run_post_mortem(self._make_trade(), rule_based)
        assert result.category == FAILURE_BAD_PREDICTION
        assert result.lesson == "Rule lesson"

    def test_post_mortem_consensus_overrides_category(self):
        """When both LLMs agree, their category wins."""
        from scripts.compounder import Compounder, FailureEntry, FAILURE_BAD_PREDICTION, FAILURE_EXTERNAL_SHOCK

        c = Compounder()
        c.post_mortem_enabled = True
        c.post_mortem_models = [{"name": "m1"}, {"name": "m2"}]

        rule_based = FailureEntry(
            date="2026-03-16", market_id="KXTEST", market_title="Test",
            category=FAILURE_BAD_PREDICTION, entry_price=0.5, exit_price=0.0,
            model_probability=0.7, actual_outcome="no", pnl=-10.0,
            root_cause="Rule cause", lesson="Rule lesson",
            action_taken="Rule action",
        )

        # Mock both LLMs returning External Shock
        llm_response = {
            "category": "External Shock",
            "root_cause": "LLM saw a shock",
            "lesson": "LLM lesson about shock",
            "action_taken": "LLM action",
            "model_name": "test",
        }

        with patch.object(c, '_call_post_mortem_model', return_value=llm_response):
            result = c._run_post_mortem(self._make_trade(), rule_based)

        assert result.category == FAILURE_EXTERNAL_SHOCK
        assert "LLM lesson about shock" in result.lesson
        assert "Rule lesson" in result.lesson  # Rule-based lesson also included

    def test_post_mortem_disagreement_keeps_rule_based(self):
        """When LLMs disagree, rule-based category is kept."""
        from scripts.compounder import Compounder, FailureEntry, FAILURE_BAD_PREDICTION, FAILURE_BAD_TIMING

        c = Compounder()
        c.post_mortem_enabled = True
        c.post_mortem_models = [{"name": "m1"}, {"name": "m2"}]

        rule_based = FailureEntry(
            date="2026-03-16", market_id="KXTEST", market_title="Test",
            category=FAILURE_BAD_PREDICTION, entry_price=0.5, exit_price=0.0,
            model_probability=0.7, actual_outcome="no", pnl=-10.0,
            root_cause="Rule cause", lesson="Rule lesson",
            action_taken="Rule action",
        )

        # Mock LLMs returning different categories
        responses = [
            {"category": "Bad Timing", "root_cause": "r1", "lesson": "l1", "action_taken": "a1", "model_name": "m1"},
            {"category": "External Shock", "root_cause": "r2", "lesson": "l2", "action_taken": "a2", "model_name": "m2"},
        ]
        call_count = [0]

        def mock_call(model_cfg, prompt):
            result = responses[call_count[0]]
            call_count[0] += 1
            return result

        with patch.object(c, '_call_post_mortem_model', side_effect=mock_call):
            result = c._run_post_mortem(self._make_trade(), rule_based)

        # Rule-based wins on disagreement
        assert result.category == FAILURE_BAD_PREDICTION
        # But lessons from all sources are merged
        assert "Rule lesson" in result.lesson
        assert "l1" in result.lesson
        assert "l2" in result.lesson

    def test_parse_post_mortem_response(self):
        """Test JSON parsing of LLM responses."""
        from scripts.compounder import Compounder

        # Clean JSON
        result = Compounder._parse_post_mortem_response(
            '{"category": "Bad Prediction", "root_cause": "test", "lesson": "test", "action_taken": "test"}'
        )
        assert result["category"] == "Bad Prediction"

        # Markdown code block
        result = Compounder._parse_post_mortem_response(
            '```json\n{"category": "External Shock", "root_cause": "x"}\n```'
        )
        assert result["category"] == "External Shock"

        # Garbage
        result = Compounder._parse_post_mortem_response("not json at all")
        assert result == {}


# ===========================================================================
# 5. Scanner — Failure log integration (scoring penalty)
# ===========================================================================

class TestScannerFailureIntegration:
    """Test that the scanner penalizes markets with past failures."""

    def test_score_penalizes_bad_prediction(self):
        from scripts.scanner import MarketScanner, Market

        scanner = MarketScanner()
        # Inject a fake failure
        scanner.past_failures = {
            "KXTEST-FAIL": [
                {"market_id": "KXTEST-FAIL", "category": "Bad Prediction", "lesson": "test"},
            ]
        }

        # Create two identical markets, one with a past failure
        base_market = Market(
            platform="kalshi", market_id="KXTEST-GOOD", title="Good Market",
            description="", category="test", event_ticker="KXTEST",
            yes_price=0.50, no_price=0.50, yes_bid=0.48, spread=0.02,
            volume_24h=500, total_volume=1000, liquidity=100, open_interest=50,
            expiry_date="2026-04-01", days_to_expiry=14, last_price=0.50,
            url="https://kalshi.com/markets/KXTEST-GOOD",
        )

        fail_market = Market(
            platform="kalshi", market_id="KXTEST-FAIL", title="Fail Market",
            description="", category="test", event_ticker="KXTEST",
            yes_price=0.50, no_price=0.50, yes_bid=0.48, spread=0.02,
            volume_24h=500, total_volume=1000, liquidity=100, open_interest=50,
            expiry_date="2026-04-01", days_to_expiry=14, last_price=0.50,
            url="https://kalshi.com/markets/KXTEST-FAIL",
        )

        score_good = scanner._score(base_market)
        score_fail = scanner._score(fail_market)

        assert score_fail < score_good, \
            f"Failed market score ({score_fail}) should be lower than good ({score_good})"
        assert score_good - score_fail == 15.0, \
            f"Penalty should be 15 for Bad Prediction, got diff={score_good - score_fail}"


# ===========================================================================
# 6. Researcher — Failure log integration (narrative warning)
# ===========================================================================

class TestResearcherFailureIntegration:
    """Test that the researcher includes past failure warnings in narratives."""

    def test_failure_context_added_to_narrative(self):
        from scripts.researcher import NewsResearcher

        researcher = NewsResearcher()
        researcher.past_failures = {
            "KXTEST-WARN": [
                {
                    "market_id": "KXTEST-WARN",
                    "category": "Bad Prediction",
                    "lesson": "Model was wrong on this market type",
                },
            ]
        }

        context = researcher._get_failure_context("KXTEST-WARN")
        assert "WARNING" in context
        assert "Bad Prediction" in context
        assert "wrong on this market type" in context

    def test_no_failure_returns_empty(self):
        from scripts.researcher import NewsResearcher
        researcher = NewsResearcher()
        researcher.past_failures = {}
        context = researcher._get_failure_context("KXTEST-CLEAN")
        assert context == ""


# ===========================================================================
# Run with pytest
# ===========================================================================

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
