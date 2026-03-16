"""
Step 4d: TRADE RESOLVER — Check Settled Markets and Close Trades

Polls the Kalshi API for open trades, checks if their markets have settled,
computes realized P&L, updates trade files on disk, and returns portfolio
state to the RiskManager so bankroll and open position counts stay accurate.

Resolution flow:
  1. Load all open trades from individual trade_*.json files
  2. For each, GET /markets/{ticker} from Kalshi
  3. If status == "settled": compute P&L, mark closed, persist
  4. If market expired with no settlement data: mark expired
  5. Update portfolio state via RiskManager.close_position()

Can run standalone:
  python scripts/resolver.py              # Resolve all open trades
  python scripts/resolver.py --dry-run    # Show what would resolve
"""

import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, TRADES_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kalshi market status lookup
# ---------------------------------------------------------------------------

class KalshiMarketChecker:
    """Checks individual market status via Kalshi public API."""

    def __init__(self, settings: Optional[dict] = None):
        s = settings or load_settings()
        kalshi = s.get("kalshi", {})
        exec_cfg = s.get("execution", {})
        self.base_url = kalshi.get("base_url", "https://api.elections.kalshi.com/trade-api/v2")
        self.retry_attempts = exec_cfg.get("retry_attempts", 3)
        self.retry_delay = exec_cfg.get("retry_delay_seconds", 5)
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        # Cache: ticker -> market data (avoid duplicate API calls within a run)
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get_market(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a single market by ticker. Returns the market dict or None.

        Uses an in-memory cache so we only hit the API once per ticker per run.
        """
        if ticker in self._cache:
            return self._cache[ticker]

        try:
            from scripts.retry import retry_call
            resp = retry_call(
                self.session.get,
                f"{self.base_url}/markets/{ticker}",
                timeout=15,
                max_attempts=self.retry_attempts,
                base_delay=self.retry_delay,
                context=f"Kalshi market/{ticker}",
            )
            if resp.status_code == 404:
                logger.warning("Market %s not found (404)", ticker)
                self._cache[ticker] = None
                return None

            resp.raise_for_status()
            data = resp.json()
            market = data.get("market", data)  # API wraps in {"market": {...}}
            self._cache[ticker] = market
            return market

        except requests.RequestException as exc:
            logger.warning("Kalshi API error for %s after retries: %s", ticker, exc)
            return None

    def is_settled(self, market: Dict[str, Any]) -> bool:
        """Check if a market has settled."""
        return market.get("status", "").lower() == "settled"

    def get_result(self, market: Dict[str, Any]) -> Optional[str]:
        """
        Determine the market result from settlement data.

        Returns "yes", "no", or None if unresolvable.

        Kalshi settled markets have result field or we infer from
        the settlement price: $1 = YES won, $0 = NO won.
        """
        # Try explicit result field first
        result = market.get("result", "")
        if result:
            return result.lower()

        # Infer from settlement/last price on settled markets
        # A settled YES contract is worth $1.00, a settled NO is $0.00
        settle_price = self._safe_float(market.get("last_price_dollars"))
        if settle_price is not None:
            if settle_price >= 0.95:
                return "yes"
            elif settle_price <= 0.05:
                return "no"

        # Check yes_ask on settled markets
        yes_ask = self._safe_float(market.get("yes_ask_dollars"))
        if yes_ask is not None:
            if yes_ask >= 0.95:
                return "yes"
            elif yes_ask <= 0.05:
                return "no"

        return None

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        """Parse a value to float, return None on failure."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# P&L calculation
# ---------------------------------------------------------------------------

def compute_pnl(
    direction: str,
    entry_price: float,
    position_size_usd: float,
    outcome_yes: bool,
) -> float:
    """
    Compute realized P&L for a binary prediction market trade.

    In prediction markets:
    - Buying YES at $0.40 means you pay $0.40 per contract.
      If YES wins, you get $1.00 → profit = $0.60 per contract.
      If NO wins, you get $0.00 → loss = $0.40 per contract.

    - Buying NO at $0.60 entry (NO price = 1 - yes_price) means:
      If NO wins, you get $1.00 → profit per contract.
      If YES wins, you get $0.00 → loss per contract.

    Args:
        direction: "buy_yes" or "buy_no"
        entry_price: The YES price at entry (market_probability)
        position_size_usd: Total dollars risked
        outcome_yes: True if the event resolved YES

    Returns:
        Realized P&L in dollars (positive = profit, negative = loss)
    """
    if position_size_usd <= 0:
        return 0.0

    if direction == "buy_yes":
        contracts = position_size_usd / entry_price if entry_price > 0 else 0
        if outcome_yes:
            # Bought YES, event happened: get $1 per contract
            return round((1.0 - entry_price) * contracts, 2)
        else:
            # Bought YES, event didn't happen: lose investment
            return round(-position_size_usd, 2)
    else:  # buy_no
        no_price = 1.0 - entry_price
        contracts = position_size_usd / no_price if no_price > 0 else 0
        if not outcome_yes:
            # Bought NO, event didn't happen: get $1 per contract
            return round((1.0 - no_price) * contracts, 2)
        else:
            # Bought NO, event happened: lose investment
            return round(-position_size_usd, 2)


# ---------------------------------------------------------------------------
# TradeResolver
# ---------------------------------------------------------------------------

class TradeResolver:
    """Checks open trades against Kalshi and resolves settled ones."""

    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or load_settings()
        self.checker = KalshiMarketChecker(settings=self.settings)

    def load_open_trades(self) -> List[Tuple[Path, Dict[str, Any]]]:
        """
        Load all open trades from individual trade_*.json files.

        Returns list of (filepath, trade_dict) tuples.
        """
        open_trades = []

        for fp in sorted(TRADES_DIR.glob("trade_*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    trade = json.load(f)

                if trade.get("status") == "open":
                    open_trades.append((fp, trade))

            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to load %s: %s", fp, exc)

        return open_trades

    def resolve_all(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Check all open trades and resolve settled ones.

        Returns a summary dict with counts and resolved trade details.
        """
        open_trades = self.load_open_trades()

        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "open_checked": len(open_trades),
            "resolved": 0,
            "expired": 0,
            "still_open": 0,
            "api_errors": 0,
            "total_pnl_resolved": 0.0,
            "resolutions": [],
        }

        if not open_trades:
            logger.info("No open trades to resolve")
            return summary

        logger.info("Checking %d open trades for settlement...", len(open_trades))

        for fp, trade in open_trades:
            ticker = trade.get("market_id", "")
            trade_id = trade.get("trade_id", "unknown")

            # Rate-limit: small delay between API calls
            time.sleep(0.2)

            # Fetch market status from Kalshi
            market = self.checker.get_market(ticker)

            if market is None:
                summary["api_errors"] += 1
                logger.warning("Could not fetch market %s for trade %s", ticker, trade_id)
                continue

            if self.checker.is_settled(market):
                result = self.checker.get_result(market)

                if result is None:
                    logger.warning(
                        "Market %s is settled but result unknown — skipping trade %s",
                        ticker, trade_id,
                    )
                    summary["api_errors"] += 1
                    continue

                outcome_yes = result == "yes"

                # Compute P&L
                pnl = compute_pnl(
                    direction=trade.get("direction", "buy_yes"),
                    entry_price=float(trade.get("entry_price", 0.5)),
                    position_size_usd=float(trade.get("position_size_usd", 0)),
                    outcome_yes=outcome_yes,
                )

                resolution = {
                    "trade_id": trade_id,
                    "market_id": ticker,
                    "market_title": trade.get("market_title", ""),
                    "direction": trade.get("direction", ""),
                    "entry_price": trade.get("entry_price"),
                    "position_size_usd": trade.get("position_size_usd"),
                    "model_probability": trade.get("model_probability"),
                    "outcome": result,
                    "pnl": pnl,
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }

                if not dry_run:
                    # Update trade file
                    trade["status"] = "closed"
                    trade["pnl"] = pnl
                    trade["outcome"] = result
                    trade["resolved_at"] = resolution["resolved_at"]
                    trade["exit_price"] = 1.0 if outcome_yes else 0.0

                    with open(fp, "w", encoding="utf-8") as f:
                        json.dump(trade, f, indent=2, ensure_ascii=False, default=str)

                    # Also update the execution snapshot that contains this trade
                    self._update_execution_snapshot(trade_id, trade)

                    logger.info(
                        "RESOLVED: %s %s → %s P&L=$%.2f",
                        trade_id, ticker, result, pnl,
                    )
                else:
                    logger.info(
                        "DRY RUN: %s %s would resolve → %s P&L=$%.2f",
                        trade_id, ticker, result, pnl,
                    )

                summary["resolved"] += 1
                summary["total_pnl_resolved"] += pnl
                summary["resolutions"].append(resolution)

            else:
                # Check if market is closed but not yet settled
                market_status = market.get("status", "unknown")
                if market_status == "closed":
                    logger.info(
                        "Trade %s: market %s is closed but not yet settled",
                        trade_id, ticker,
                    )
                summary["still_open"] += 1

        logger.info(
            "Resolution complete: %d resolved (P&L=$%.2f), %d still open, %d errors",
            summary["resolved"], summary["total_pnl_resolved"],
            summary["still_open"], summary["api_errors"],
        )

        return summary

    def _update_execution_snapshot(self, trade_id: str, updated_trade: Dict[str, Any]) -> None:
        """
        Update the trade in its execution snapshot file so execution_*.json
        stays consistent with individual trade_*.json files.
        """
        for fp in sorted(TRADES_DIR.glob("execution_*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)

                modified = False
                for i, t in enumerate(data.get("trades", [])):
                    if t.get("trade_id") == trade_id:
                        data["trades"][i] = updated_trade
                        modified = True
                        break

                if modified:
                    with open(fp, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
                    logger.debug("Updated trade %s in %s", trade_id, fp.name)
                    return

            except (json.JSONDecodeError, KeyError) as exc:
                logger.debug("Skipping %s: %s", fp, exc)

    def update_portfolio_state(self, resolutions: List[Dict[str, Any]]) -> None:
        """
        Update portfolio state after resolving trades.

        Uses RiskManager.close_position() for each resolved trade
        so bankroll and open_positions stay accurate.
        """
        if not resolutions:
            return

        from scripts.validate_risk import RiskManager

        risk_mgr = RiskManager(settings=self.settings)

        for res in resolutions:
            position_size = float(res.get("position_size_usd", 0))
            pnl = float(res.get("pnl", 0))
            risk_mgr.close_position(position_size=position_size, pnl=pnl)

        logger.info(
            "Portfolio state updated: bankroll=$%.2f, open=%d",
            risk_mgr.state.current_bankroll, risk_mgr.state.open_positions,
        )


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def save_resolution_snapshot(summary: Dict[str, Any]) -> Path:
    """Save resolution results as a timestamped JSON snapshot."""
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = TRADES_DIR / f"resolution_{ts}.json"

    with open(fp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)

    logger.info("Resolution snapshot saved: %s", fp)
    return fp


def _print_resolution_table(summary: Dict[str, Any]) -> None:
    """Print a formatted table of resolution results."""
    resolutions = summary.get("resolutions", [])

    if not resolutions:
        print("  No trades were resolved this cycle.")
        return

    header = (
        f"  {'Trade':<14} {'Market':<30} {'Dir':<8} {'Entry':>5} "
        f"{'Size':>7} {'Model':>5} {'Result':>6} {'P&L':>8}"
    )
    print(header)
    print(f"  {'-' * (len(header) - 2)}")

    for r in resolutions:
        title = r.get("market_title", r.get("market_id", "?"))[:30].ljust(30)
        pnl = r.get("pnl", 0)
        pnl_str = f"${pnl:+.2f}"
        print(
            f"  {r.get('trade_id', '?'):<14} {title} "
            f"{r.get('direction', '?'):<8} {r.get('entry_price', 0):5.3f} "
            f"${r.get('position_size_usd', 0):6.2f} "
            f"{r.get('model_probability', 0):5.3f} "
            f"{r.get('outcome', '?'):>6} {pnl_str:>8}"
        )

    total_pnl = summary.get("total_pnl_resolved", 0)
    print(f"\n  Total resolved P&L: ${total_pnl:+.2f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Resolve open trades by checking Kalshi for settled markets."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would resolve without modifying files.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    resolver = TradeResolver()
    open_trades = resolver.load_open_trades()

    print(f"\nTrade Resolver")
    print(f"Open trades found: {len(open_trades)}")

    if not open_trades:
        print("No open trades to resolve.")
        sys.exit(0)

    if args.dry_run:
        print("[Dry run mode — no files will be modified]\n")

    summary = resolver.resolve_all(dry_run=args.dry_run)

    print(f"\nResolution Summary:")
    print(f"  Checked:     {summary['open_checked']}")
    print(f"  Resolved:    {summary['resolved']}")
    print(f"  Still open:  {summary['still_open']}")
    print(f"  API errors:  {summary['api_errors']}")

    if summary["resolutions"]:
        print()
        _print_resolution_table(summary)

    if not args.dry_run:
        # Update portfolio state
        resolver.update_portfolio_state(summary["resolutions"])

        # Save snapshot
        if summary["resolved"] > 0:
            fp = save_resolution_snapshot(summary)
            print(f"\nResolution snapshot saved: {fp}")
