"""
Tests for Tier 4: Kalshi demo exchange integration.

1. Kalshi RSA-PSS authentication
2. Kalshi client API operations
3. Executor demo mode (order placement)
4. Dashboard Kalshi endpoint
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# 1. Kalshi client — auth and API
# ===========================================================================

class TestKalshiClient:
    """Test the Kalshi API client."""

    def test_client_loads_from_env(self):
        """Client should initialize from .env variables."""
        from scripts.kalshi_client import KalshiClient
        client = KalshiClient.from_env()
        assert client.api_key_id
        assert client.env == "demo"
        assert "demo" in client.base_url

    def test_client_signs_requests(self):
        """Signing should produce a non-empty base64 string."""
        from scripts.kalshi_client import KalshiClient
        client = KalshiClient.from_env()
        sig = client._sign("1234567890", "GET", "/trade-api/v2/portfolio/balance")
        assert isinstance(sig, str)
        assert len(sig) > 50  # base64 RSA signature is ~340+ chars

    def test_client_connects(self):
        """Client should successfully connect to demo API."""
        from scripts.kalshi_client import KalshiClient
        client = KalshiClient.from_env()
        assert client.is_connected()

    def test_get_balance(self):
        """Should return balance in cents (dict with 'balance' key)."""
        from scripts.kalshi_client import KalshiClient
        client = KalshiClient.from_env()
        balance = client.get_balance()
        assert "balance" in balance
        assert isinstance(balance["balance"], (int, float))

    def test_get_balance_dollars(self):
        """Dollar balance should be a positive float."""
        from scripts.kalshi_client import KalshiClient
        client = KalshiClient.from_env()
        dollars = client.get_balance_dollars()
        assert isinstance(dollars, float)
        assert dollars >= 0

    def test_get_positions(self):
        """Should return a list (may be empty)."""
        from scripts.kalshi_client import KalshiClient
        client = KalshiClient.from_env()
        positions = client.get_positions()
        assert isinstance(positions, list)

    def test_get_orders(self):
        """Should return a list (may be empty)."""
        from scripts.kalshi_client import KalshiClient
        client = KalshiClient.from_env()
        orders = client.get_orders()
        assert isinstance(orders, list)

    def test_place_and_cancel_order(self):
        """Should place a limit order and cancel it."""
        from scripts.kalshi_client import KalshiClient
        import requests

        client = KalshiClient.from_env()

        # Find an open market
        resp = requests.get(
            f"{client.base_url}/markets?status=open&limit=1",
            timeout=10,
        )
        markets = resp.json().get("markets", [])
        if not markets:
            return  # Skip if no markets available

        ticker = markets[0]["ticker"]

        # Place at 1 cent (minimal, unlikely to fill)
        result = client.place_order(
            ticker=ticker,
            side="yes",
            action="buy",
            price_dollars=0.01,
            count=1,
        )
        order = result.get("order", {})
        assert order.get("status") in ("resting", "executed")
        assert order.get("order_id")

        # Cancel
        oid = order["order_id"]
        cancel = client.cancel_order(oid)
        assert cancel.get("order", {}).get("status") == "canceled"

    def test_invalid_price_raises(self):
        """Should reject prices outside 0.01-0.99."""
        from scripts.kalshi_client import KalshiClient
        import pytest

        client = KalshiClient.from_env()

        with pytest.raises(ValueError, match="price_dollars"):
            client.place_order("FAKE", "yes", "buy", 0.00, 1)

        with pytest.raises(ValueError, match="price_dollars"):
            client.place_order("FAKE", "yes", "buy", 1.50, 1)

    def test_invalid_side_raises(self):
        """Should reject invalid side."""
        from scripts.kalshi_client import KalshiClient
        import pytest

        client = KalshiClient.from_env()
        with pytest.raises(ValueError, match="side"):
            client.place_order("FAKE", "maybe", "buy", 0.50, 1)


# ===========================================================================
# 2. Executor demo mode
# ===========================================================================

class TestExecutorDemoMode:
    """Test that the executor works in demo mode."""

    def test_executor_detects_demo_mode(self):
        """Executor should detect KALSHI_ENV=demo from env."""
        from scripts.executor import TradeExecutor
        executor = TradeExecutor()
        assert executor.mode == "demo"

    def test_executor_has_kalshi_client(self):
        """In demo mode, executor should have a Kalshi client."""
        from scripts.executor import TradeExecutor
        executor = TradeExecutor()
        if executor.mode == "demo":
            assert executor._kalshi is not None

    def test_trade_has_kalshi_fields(self):
        """Trade dataclass should have Kalshi-specific fields."""
        from scripts.executor import Trade
        t = Trade(
            trade_id="test", market_id="TEST", market_title="Test",
            direction="buy_yes", entry_price=0.5, signal_price=0.5,
            slippage=0.0, position_size_usd=10, model_probability=0.6,
            signal_strength=0.1, edge=0.1, status="open", pnl=0,
            risk_passed=True, risk_failures=[], kelly_fraction=0.05,
            timestamp="now", execution_mode="demo",
            kalshi_order_id="abc123", kalshi_status="resting",
            contracts=5, limit_price=0.50, fill_count=0,
        )
        assert t.execution_mode == "demo"
        assert t.kalshi_order_id == "abc123"
        assert t.contracts == 5

    def test_paper_trade_has_default_kalshi_fields(self):
        """Paper trades should have empty Kalshi fields."""
        from scripts.executor import Trade
        t = Trade(
            trade_id="test", market_id="TEST", market_title="Test",
            direction="buy_yes", entry_price=0.5, signal_price=0.5,
            slippage=0.0, position_size_usd=10, model_probability=0.6,
            signal_strength=0.1, edge=0.1, status="open", pnl=0,
            risk_passed=True, risk_failures=[], kelly_fraction=0.05,
            timestamp="now",
        )
        assert t.execution_mode == "paper"
        assert t.kalshi_order_id == ""
        assert t.contracts == 0

    def test_executor_get_kalshi_balance(self):
        """Should return balance from demo account."""
        from scripts.executor import TradeExecutor
        executor = TradeExecutor()
        if executor.mode == "demo":
            balance = executor.get_kalshi_balance()
            assert isinstance(balance, float)
            assert balance >= 0


# ===========================================================================
# 3. Dashboard Kalshi endpoint
# ===========================================================================

class TestDashboardKalshi:
    """Test the /api/kalshi dashboard endpoint."""

    def test_kalshi_endpoint_returns_data(self):
        """The /api/kalshi endpoint should return connection data."""
        import requests
        resp = requests.get("http://localhost:5000/api/kalshi", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert "mode" in data
        assert "connected" in data
        assert "balance" in data

    def test_kalshi_endpoint_shows_demo(self):
        """When KALSHI_ENV=demo, endpoint should show demo mode."""
        import requests
        resp = requests.get("http://localhost:5000/api/kalshi", timeout=10)
        data = resp.json()
        assert data["mode"] == "demo"
        assert data["connected"] is True
        assert data["balance"] > 0
