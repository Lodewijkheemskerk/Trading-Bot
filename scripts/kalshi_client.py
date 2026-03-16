"""
Kalshi API Client — RSA-PSS authenticated.

Supports demo and production environments.
Handles signing, order placement, position/balance queries, and market data.
"""

import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.retry import retry_call

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "production": "https://api.elections.kalshi.com/trade-api/v2",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class KalshiOrder:
    """Represents an order placed on Kalshi."""
    order_id: str
    ticker: str
    side: str          # "yes" or "no"
    action: str        # "buy" or "sell"
    status: str        # "resting", "canceled", "executed", "pending"
    yes_price: float   # Limit price in dollars
    count: float       # Number of contracts
    fill_count: float  # Contracts filled so far
    remaining_count: float
    created_time: str
    client_order_id: str = ""


@dataclass
class KalshiPosition:
    """Represents an open position on Kalshi."""
    ticker: str
    market_exposure: float   # Total exposure in cents
    resting_orders_count: int
    total_traded: float
    realized_pnl: float      # In cents


@dataclass
class KalshiFill:
    """Represents a single fill (execution)."""
    trade_id: str
    ticker: str
    side: str
    action: str
    count: float
    yes_price: float
    no_price: float
    created_time: str
    is_taker: bool


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class KalshiClient:
    """
    Authenticated Kalshi API client.

    Usage:
        client = KalshiClient.from_env()        # Reads .env
        balance = client.get_balance()
        order = client.place_order("KXBTC-...", "yes", "buy", 0.55, 10)
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_path: str,
        env: str = "demo",
        retry_attempts: int = 3,
        retry_delay: float = 5.0,
    ):
        self.api_key_id = api_key_id
        self.env = env
        self.base_url = BASE_URLS[env]
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay

        # Load RSA private key
        key_path = Path(private_key_path)
        if not key_path.exists():
            raise FileNotFoundError(f"Private key not found: {key_path}")

        with open(key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

        logger.info("KalshiClient initialized [env=%s, key=%s...%s]",
                     env, api_key_id[:8], api_key_id[-4:])

    @classmethod
    def from_env(cls, settings: Optional[dict] = None) -> "KalshiClient":
        """Create client from environment variables and settings."""
        from dotenv import load_dotenv
        load_dotenv()

        api_key_id = os.getenv("KALSHI_API_KEY_ID", "")
        private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "config/kalshi_demo_key.pem")
        env = os.getenv("KALSHI_ENV", "demo")

        if not api_key_id:
            raise ValueError("KALSHI_API_KEY_ID not set in environment")

        retry_attempts = 3
        retry_delay = 5.0
        if settings:
            exec_cfg = settings.get("execution", {})
            retry_attempts = exec_cfg.get("retry_attempts", 3)
            retry_delay = exec_cfg.get("retry_delay_seconds", 5.0)

        return cls(
            api_key_id=api_key_id,
            private_key_path=private_key_path,
            env=env,
            retry_attempts=retry_attempts,
            retry_delay=retry_delay,
        )

    # ------------------------------------------------------------------
    # Auth / signing
    # ------------------------------------------------------------------

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """
        Create RSA-PSS signature for a request.

        Message format: {timestamp_ms}{METHOD}{path_without_query}
        """
        path_clean = path.split("?")[0]
        message = f"{timestamp_ms}{method}{path_clean}".encode("utf-8")

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        """Build authenticated headers for a request."""
        timestamp_ms = str(int(time.time() * 1000))
        # The full path from root is what gets signed
        full_path = urlparse(self.base_url + path).path
        signature = self._sign(timestamp_ms, method, full_path)

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # HTTP helpers (with retry)
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """Authenticated GET with retry."""
        def do_get():
            headers = self._headers("GET", path)
            url = self.base_url + path
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()

        return retry_call(
            do_get,
            max_attempts=self.retry_attempts,
            base_delay=self.retry_delay,
        )

    def _post(self, path: str, body: dict) -> dict:
        """Authenticated POST with retry."""
        def do_post():
            headers = self._headers("POST", path)
            url = self.base_url + path
            resp = requests.post(url, headers=headers, json=body, timeout=15)
            resp.raise_for_status()
            return resp.json()

        return retry_call(
            do_post,
            max_attempts=self.retry_attempts,
            base_delay=self.retry_delay,
        )

    def _delete(self, path: str) -> dict:
        """Authenticated DELETE with retry."""
        def do_delete():
            headers = self._headers("DELETE", path)
            url = self.base_url + path
            resp = requests.delete(url, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.json()

        return retry_call(
            do_delete,
            max_attempts=self.retry_attempts,
            base_delay=self.retry_delay,
        )

    # ------------------------------------------------------------------
    # Portfolio endpoints
    # ------------------------------------------------------------------

    def get_balance(self) -> dict:
        """
        Get account balance.

        Returns: {"balance": <cents>, "payout": <cents>, ...}
        Balance is in cents — divide by 100 for dollars.
        """
        data = self._get("/portfolio/balance")
        logger.info("Balance: $%.2f", data.get("balance", 0) / 100)
        return data

    def get_positions(self, ticker: Optional[str] = None) -> List[dict]:
        """
        Get open positions.

        Returns list of position dicts with market_exposure, realized_pnl, etc.
        """
        params = {}
        if ticker:
            params["ticker"] = ticker
        data = self._get("/portfolio/positions", params=params)
        positions = data.get("market_positions", [])
        logger.info("Positions: %d open", len(positions))
        return positions

    def get_fills(self, ticker: Optional[str] = None, limit: int = 100) -> List[dict]:
        """
        Get recent fills (executed trades).
        """
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = self._get("/portfolio/fills", params=params)
        return data.get("fills", [])

    def get_settlements(self, limit: int = 100) -> List[dict]:
        """
        Get settlement history.
        """
        data = self._get("/portfolio/settlements", params={"limit": limit})
        return data.get("settlements", [])

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def place_order(
        self,
        ticker: str,
        side: str,          # "yes" or "no"
        action: str,         # "buy" or "sell"
        price_dollars: float,  # Limit price as a dollar amount (0.01 to 0.99)
        count: int,          # Number of contracts
        client_order_id: Optional[str] = None,
        expiration_ts: Optional[int] = None,
    ) -> dict:
        """
        Place a limit order on Kalshi.

        Args:
            ticker: Market ticker (e.g., "KXBTC-26MAR14-B97500")
            side: "yes" or "no"
            action: "buy" or "sell"
            price_dollars: Limit price as dollars (e.g., 0.55 for 55 cents)
            count: Number of contracts
            client_order_id: Optional client-side ID for idempotency
            expiration_ts: Optional Unix timestamp for order expiry

        Returns:
            Order response dict from Kalshi API.
        """
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got '{side}'")
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be 'buy' or 'sell', got '{action}'")
        if not (0.01 <= price_dollars <= 0.99):
            raise ValueError(f"price_dollars must be 0.01-0.99, got {price_dollars}")
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")

        body: Dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "type": "limit",
            "count": count,
        }

        # Use dollar-denominated pricing
        if side == "yes":
            body["yes_price_dollars"] = f"{price_dollars:.2f}"
        else:
            body["no_price_dollars"] = f"{price_dollars:.2f}"

        if client_order_id:
            body["client_order_id"] = client_order_id
        else:
            body["client_order_id"] = uuid.uuid4().hex[:16]

        if expiration_ts:
            body["expiration_ts"] = expiration_ts

        logger.info(
            "Placing order: %s %s %s @ $%.2f x%d [%s]",
            action, side, ticker, price_dollars, count, self.env,
        )

        data = self._post("/portfolio/orders", body)
        order = data.get("order", {})

        logger.info(
            "Order placed: %s status=%s fill=%s/%s",
            order.get("order_id", "?"),
            order.get("status", "?"),
            order.get("fill_count_fp", "0"),
            order.get("initial_count_fp", str(count)),
        )
        return data

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a resting order."""
        logger.info("Cancelling order %s", order_id)
        return self._delete(f"/portfolio/orders/{order_id}")

    def get_order(self, order_id: str) -> dict:
        """Get details of a specific order."""
        return self._get(f"/portfolio/orders/{order_id}")

    def get_orders(
        self,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        """
        Get orders, optionally filtered.

        Args:
            ticker: Filter by market ticker
            status: Filter by status (resting, canceled, executed, pending)
            limit: Max results
        """
        params: Dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        data = self._get("/portfolio/orders", params=params)
        return data.get("orders", [])

    # ------------------------------------------------------------------
    # Market data (public, but uses auth for consistency)
    # ------------------------------------------------------------------

    def get_market(self, ticker: str) -> dict:
        """Get full market details."""
        data = self._get(f"/markets/{ticker}")
        return data.get("market", data)

    def get_orderbook(self, ticker: str) -> dict:
        """Get orderbook for a market."""
        return self._get(f"/markets/{ticker}/orderbook")

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_balance_dollars(self) -> float:
        """Get balance as a dollar float."""
        data = self.get_balance()
        return data.get("balance", 0) / 100.0

    def is_connected(self) -> bool:
        """Test connectivity by hitting the balance endpoint."""
        try:
            self.get_balance()
            return True
        except Exception as exc:
            logger.warning("Kalshi connection test failed: %s", exc)
            return False
