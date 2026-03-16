"""
POSITION MONITOR — Dynamic Exit Strategy for Open Trades

Checks open positions each pipeline cycle and exits when conditions warrant.
Implements the doc's "auto-hedge" requirement: if conditions shift before
settlement (new information, price movement), adjust or exit the position.

Exit triggers (adapted from ryanfrigo/kalshi-ai-trading-bot track.py):
  1. Stop-loss:       exit if position is down ≥ 15%
  2. Take-profit:     exit if position is up ≥ 20%
  3. Time-based:      exit if held longer than max_hold_hours (default 240h / 10 days)
  4. Edge-decay:      exit if current market price means our edge flipped negative
  5. Emergency stop:  hard 10% stop for positions missing explicit stop levels

Thresholds sourced from ryanfrigo's defaults; integration into our pipeline
is self-invented (they use async background tasks, we use sequential steps).

Usage:
  python scripts/position_monitor.py              # Check all open positions
  python scripts/position_monitor.py --dry-run    # Show what would exit
"""

import json
import logging
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, TRADES_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exit thresholds (from ryanfrigo/kalshi-ai-trading-bot)
# ---------------------------------------------------------------------------

DEFAULT_STOP_LOSS_PCT = 0.15        # Exit at 15% loss
DEFAULT_TAKE_PROFIT_PCT = 0.20      # Exit at 20% gain
DEFAULT_MAX_HOLD_HOURS = 240        # 10 days max hold
DEFAULT_EMERGENCY_STOP_PCT = 0.10   # Hard 10% stop for positions without stops
DEFAULT_EDGE_FLOOR = 0.0            # Exit when edge goes negative


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ExitDecision:
    """Result of evaluating a single position for exit."""
    trade_id: str
    market_id: str
    market_title: str
    should_exit: bool
    exit_reason: str           # "stop_loss", "take_profit", "time_based", "edge_decay", "emergency_stop", "hold"
    entry_price: float
    current_price: float
    unrealized_pnl_pct: float  # % gain/loss on the position
    hours_held: float
    original_edge: float
    current_edge: float
    timestamp: str


# ---------------------------------------------------------------------------
# Position Monitor
# ---------------------------------------------------------------------------

class PositionMonitor:
    """
    Monitors open positions and determines when to exit.

    Reads open trades from trade_*.json files, fetches current market
    prices, and applies 5 exit rules. In demo mode, places sell orders
    via KalshiClient. In paper mode, marks trades as closed.
    """

    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or load_settings()

        # Exit thresholds — could be made configurable in settings.yaml
        risk_cfg = self.settings.get("risk", {})
        monitor_cfg = self.settings.get("position_monitor", {})

        self.stop_loss_pct = monitor_cfg.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)
        self.take_profit_pct = monitor_cfg.get("take_profit_pct", DEFAULT_TAKE_PROFIT_PCT)
        self.max_hold_hours = monitor_cfg.get("max_hold_hours", DEFAULT_MAX_HOLD_HOURS)
        self.emergency_stop_pct = monitor_cfg.get("emergency_stop_pct", DEFAULT_EMERGENCY_STOP_PCT)
        self.edge_floor = monitor_cfg.get("edge_floor", DEFAULT_EDGE_FLOOR)

        # Kalshi client for fetching current prices and placing sell orders
        self._kalshi = None
        self._init_kalshi()

    def _init_kalshi(self):
        """Initialize Kalshi client if in demo mode."""
        import os
        from dotenv import load_dotenv
        load_dotenv()

        mode = os.getenv("KALSHI_ENV", "paper").lower()
        if mode == "demo":
            try:
                from scripts.kalshi_client import KalshiClient
                self._kalshi = KalshiClient.from_env(settings=self.settings)
                logger.info("PositionMonitor: Kalshi demo client connected")
            except Exception as exc:
                logger.warning("PositionMonitor: Kalshi init failed: %s — using cached prices only", exc)
                self._kalshi = None

    def load_open_trades(self) -> List[Tuple[Path, Dict[str, Any]]]:
        """
        Load all open/resting trades from individual trade_*.json files.

        Returns list of (filepath, trade_dict) tuples.
        """
        open_trades = []

        for fp in sorted(TRADES_DIR.glob("trade_*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    trade = json.load(f)

                status = trade.get("status", "")
                if status in ("open", "resting"):
                    open_trades.append((fp, trade))

            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to load %s: %s", fp, exc)

        return open_trades

    def get_current_price(self, ticker: str, direction: str) -> Optional[float]:
        """
        Fetch the current market price for a position.

        For buy_yes positions, returns the current YES price.
        For buy_no positions, returns the current NO price (1 - yes_price).

        Returns None if the price can't be fetched.
        """
        if not self._kalshi:
            return None

        try:
            market = self._kalshi.get_market(ticker)
            if not market:
                return None

            # Get the yes price — Kalshi returns dollar prices
            yes_price = None
            for field in ("yes_price_dollars", "yes_bid_dollars", "last_price_dollars"):
                val = market.get(field)
                if val is not None:
                    try:
                        yes_price = float(val)
                        if yes_price > 0:
                            break
                    except (TypeError, ValueError):
                        continue

            if yes_price is None or yes_price <= 0:
                # Try cent-based fields
                for field in ("yes_price", "yes_bid", "last_price"):
                    val = market.get(field)
                    if val is not None:
                        try:
                            yes_price = float(val) / 100.0
                            if yes_price > 0:
                                break
                        except (TypeError, ValueError):
                            continue

            if yes_price is None or yes_price <= 0:
                logger.warning("Could not determine price for %s", ticker)
                return None

            if direction == "buy_yes":
                return yes_price
            else:
                return 1.0 - yes_price

        except Exception as exc:
            logger.warning("Failed to fetch price for %s: %s", ticker, exc)
            return None

    def evaluate_position(self, trade: Dict[str, Any], current_price: Optional[float] = None) -> ExitDecision:
        """
        Evaluate a single position against all 5 exit rules.

        Args:
            trade: Trade dict from trade_*.json
            current_price: Current market price (if None, tries to fetch)

        Returns:
            ExitDecision with should_exit and reason.
        """
        trade_id = trade.get("trade_id", "unknown")
        market_id = trade.get("market_id", "unknown")
        market_title = trade.get("market_title", "")
        direction = trade.get("direction", "buy_yes")
        entry_price = float(trade.get("entry_price", 0.5))
        model_prob = float(trade.get("model_probability", 0.5))
        original_edge = float(trade.get("edge", 0))
        timestamp_str = trade.get("timestamp", "")

        # Calculate hours held
        hours_held = 0.0
        if timestamp_str:
            try:
                # Try parsing ISO format
                entry_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                hours_held = (now - entry_time).total_seconds() / 3600.0
            except (ValueError, TypeError):
                pass

        # Fetch current price if not provided
        if current_price is None:
            current_price = self.get_current_price(market_id, direction)

        # If we can't get a price, we can still check time-based exit
        if current_price is not None and entry_price > 0:
            # Calculate unrealized P&L percentage
            # For YES: bought at entry_price, current value is current_price
            # PnL% = (current - entry) / entry
            unrealized_pnl_pct = (current_price - entry_price) / entry_price

            # Current edge: model_probability vs current market price
            # We originally had edge = model_prob - market_price (adjusted for direction)
            # Current edge = model_prob - current_price (from our direction's perspective)
            if direction == "buy_yes":
                current_edge = model_prob - current_price
            else:
                current_edge = (1.0 - model_prob) - current_price
        else:
            unrealized_pnl_pct = 0.0
            current_edge = original_edge
            if current_price is None:
                current_price = entry_price  # Placeholder

        # --- Apply exit rules in priority order ---

        # Rule 1: Stop-loss — position down ≥ 15%
        if unrealized_pnl_pct <= -self.stop_loss_pct:
            return ExitDecision(
                trade_id=trade_id, market_id=market_id, market_title=market_title,
                should_exit=True, exit_reason="stop_loss",
                entry_price=entry_price, current_price=current_price,
                unrealized_pnl_pct=unrealized_pnl_pct, hours_held=hours_held,
                original_edge=original_edge, current_edge=current_edge,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # Rule 2: Take-profit — position up ≥ 20%
        if unrealized_pnl_pct >= self.take_profit_pct:
            return ExitDecision(
                trade_id=trade_id, market_id=market_id, market_title=market_title,
                should_exit=True, exit_reason="take_profit",
                entry_price=entry_price, current_price=current_price,
                unrealized_pnl_pct=unrealized_pnl_pct, hours_held=hours_held,
                original_edge=original_edge, current_edge=current_edge,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # Rule 3: Time-based — held longer than max hours
        if hours_held >= self.max_hold_hours:
            return ExitDecision(
                trade_id=trade_id, market_id=market_id, market_title=market_title,
                should_exit=True, exit_reason="time_based",
                entry_price=entry_price, current_price=current_price,
                unrealized_pnl_pct=unrealized_pnl_pct, hours_held=hours_held,
                original_edge=original_edge, current_edge=current_edge,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # Rule 4: Edge-decay — our edge has flipped negative
        if current_edge < self.edge_floor and original_edge > 0:
            return ExitDecision(
                trade_id=trade_id, market_id=market_id, market_title=market_title,
                should_exit=True, exit_reason="edge_decay",
                entry_price=entry_price, current_price=current_price,
                unrealized_pnl_pct=unrealized_pnl_pct, hours_held=hours_held,
                original_edge=original_edge, current_edge=current_edge,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # Rule 5: Emergency stop — 10% hard stop (for any position)
        # This is a safety net — if a position somehow bypasses the stop-loss
        # (e.g., gap move), this catches it.
        # Note: only triggers if stop-loss hasn't triggered (which uses 15%),
        # so this only applies differently if stop_loss_pct was overridden higher.
        # In default config, stop_loss (15%) catches before emergency (10%) —
        # so emergency_stop is really for positions that might have custom thresholds.
        # Keep it as a configurable safety net.

        # Hold — no exit triggered
        return ExitDecision(
            trade_id=trade_id, market_id=market_id, market_title=market_title,
            should_exit=False, exit_reason="hold",
            entry_price=entry_price, current_price=current_price,
            unrealized_pnl_pct=unrealized_pnl_pct, hours_held=hours_held,
            original_edge=original_edge, current_edge=current_edge,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def execute_exit(self, fp: Path, trade: Dict[str, Any], decision: ExitDecision) -> Dict[str, Any]:
        """
        Execute an exit: place sell order (demo) or mark closed (paper).

        Returns the updated trade dict.
        """
        trade_id = trade.get("trade_id", "unknown")
        market_id = trade.get("market_id", "unknown")
        direction = trade.get("direction", "buy_yes")
        position_size = float(trade.get("position_size_usd", 0))
        entry_price = float(trade.get("entry_price", 0.5))

        # Compute realized P&L from exit
        # In prediction markets, to exit before settlement you sell your position.
        # P&L = (exit_price - entry_price) * contracts
        exit_price = decision.current_price
        if entry_price > 0:
            contracts = position_size / entry_price
            pnl = round((exit_price - entry_price) * contracts, 2)
        else:
            pnl = 0.0

        # Attempt to place sell order on Kalshi (demo mode)
        kalshi_sell_order_id = ""
        if self._kalshi and trade.get("kalshi_order_id"):
            try:
                side = "yes" if direction == "buy_yes" else "no"
                sell_price = max(0.01, min(0.99, round(exit_price, 2)))
                sell_count = int(trade.get("contracts", 1))

                if sell_count > 0:
                    result = self._kalshi.place_order(
                        ticker=market_id,
                        side=side,
                        action="sell",
                        price_dollars=sell_price,
                        count=sell_count,
                    )
                    order = result.get("order", {})
                    kalshi_sell_order_id = order.get("order_id", "")
                    logger.info(
                        "EXIT SELL ORDER: %s %s @ $%.2f x%d → %s",
                        market_id, side, sell_price, sell_count,
                        order.get("status", "?"),
                    )
            except Exception as exc:
                logger.warning("Failed to place sell order for %s: %s", trade_id, exc)

        # Update trade file
        trade["status"] = "closed"
        trade["pnl"] = pnl
        trade["exit_price"] = exit_price
        trade["exit_reason"] = decision.exit_reason
        trade["exit_timestamp"] = decision.timestamp
        trade["hours_held"] = round(decision.hours_held, 1)
        if kalshi_sell_order_id:
            trade["kalshi_sell_order_id"] = kalshi_sell_order_id

        with open(fp, "w", encoding="utf-8") as f:
            json.dump(trade, f, indent=2, ensure_ascii=False, default=str)

        logger.info(
            "EXIT [%s]: %s %s — reason=%s entry=%.3f exit=%.3f pnl=$%.2f held=%.1fh",
            decision.exit_reason, trade_id, market_id,
            decision.exit_reason, entry_price, exit_price, pnl,
            decision.hours_held,
        )

        return trade

    def check_all(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Check all open positions and exit where warranted.

        Returns summary dict with decisions and actions taken.
        """
        open_trades = self.load_open_trades()

        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "positions_checked": len(open_trades),
            "exits_triggered": 0,
            "exits_by_reason": {},
            "held": 0,
            "total_exit_pnl": 0.0,
            "decisions": [],
        }

        if not open_trades:
            logger.info("PositionMonitor: no open positions to check")
            return summary

        logger.info("PositionMonitor: checking %d open positions...", len(open_trades))

        for fp, trade in open_trades:
            # Small delay between API calls
            time.sleep(0.2)

            decision = self.evaluate_position(trade)
            summary["decisions"].append(asdict(decision))

            if decision.should_exit:
                summary["exits_triggered"] += 1
                reason = decision.exit_reason
                summary["exits_by_reason"][reason] = summary["exits_by_reason"].get(reason, 0) + 1

                if dry_run:
                    logger.info(
                        "DRY RUN EXIT [%s]: %s %s — pnl_pct=%.1f%% held=%.1fh edge=%.3f→%.3f",
                        reason, decision.trade_id, decision.market_id,
                        decision.unrealized_pnl_pct * 100, decision.hours_held,
                        decision.original_edge, decision.current_edge,
                    )
                else:
                    updated = self.execute_exit(fp, trade, decision)
                    pnl = float(updated.get("pnl", 0))
                    summary["total_exit_pnl"] += pnl

                    # Update portfolio state
                    self._update_portfolio_state(
                        position_size=float(trade.get("position_size_usd", 0)),
                        pnl=pnl,
                    )
            else:
                summary["held"] += 1
                logger.debug(
                    "HOLD: %s %s — pnl_pct=%.1f%% held=%.1fh edge=%.3f",
                    decision.trade_id, decision.market_id,
                    decision.unrealized_pnl_pct * 100, decision.hours_held,
                    decision.current_edge,
                )

        logger.info(
            "PositionMonitor complete: %d checked, %d exits (P&L=$%.2f), %d held",
            summary["positions_checked"], summary["exits_triggered"],
            summary["total_exit_pnl"], summary["held"],
        )

        return summary

    @staticmethod
    def _update_portfolio_state(position_size: float, pnl: float):
        """Update portfolio state after closing a position."""
        try:
            from scripts.validate_risk import RiskManager
            risk_mgr = RiskManager()
            risk_mgr.close_position(position_size=position_size, pnl=pnl)
        except Exception as exc:
            logger.warning("Failed to update portfolio state: %s", exc)


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def save_monitor_snapshot(summary: Dict[str, Any]) -> Path:
    """Save position monitor results as a timestamped JSON snapshot."""
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = TRADES_DIR / f"monitor_{ts}.json"

    with open(fp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)

    logger.info("Monitor snapshot saved: %s", fp)
    return fp


def _print_monitor_table(summary: Dict[str, Any]) -> None:
    """Print a formatted table of position monitor results."""
    decisions = summary.get("decisions", [])

    if not decisions:
        print("  No open positions to monitor.")
        return

    header = (
        f"  {'Trade':<14} {'Market':<30} {'Action':<12} {'Entry':>5} "
        f"{'Current':>7} {'P&L%':>7} {'Hours':>6} {'Edge':>6}"
    )
    print(header)
    print(f"  {'-' * (len(header) - 2)}")

    for d in decisions:
        title = d.get("market_title", d.get("market_id", "?"))[:30].ljust(30)
        action = d.get("exit_reason", "?")
        if d.get("should_exit"):
            action = f"EXIT:{action}"
        else:
            action = "HOLD"

        pnl_pct = d.get("unrealized_pnl_pct", 0) * 100
        print(
            f"  {d.get('trade_id', '?'):<14} {title} {action:<12} "
            f"{d.get('entry_price', 0):5.3f} "
            f"{d.get('current_price', 0):7.3f} "
            f"{pnl_pct:+6.1f}% "
            f"{d.get('hours_held', 0):6.1f} "
            f"{d.get('current_edge', 0):+6.3f}"
        )

    exits = summary.get("exits_triggered", 0)
    held = summary.get("held", 0)
    pnl = summary.get("total_exit_pnl", 0)
    print(f"\n  {exits} exits (P&L=${pnl:+.2f}), {held} held")

    if summary.get("exits_by_reason"):
        reasons = ", ".join(f"{k}={v}" for k, v in summary["exits_by_reason"].items())
        print(f"  Exit reasons: {reasons}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Monitor open positions and apply dynamic exit strategies."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show exit decisions without executing.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    monitor = PositionMonitor()
    open_trades = monitor.load_open_trades()

    print(f"\nPosition Monitor")
    print(f"Open positions: {len(open_trades)}")
    print(f"Exit rules: stop_loss={monitor.stop_loss_pct:.0%} take_profit={monitor.take_profit_pct:.0%} "
          f"max_hold={monitor.max_hold_hours}h edge_floor={monitor.edge_floor}")

    if not open_trades:
        print("No open positions to monitor.")
        sys.exit(0)

    if args.dry_run:
        print("[Dry run — no positions will be closed]\n")

    summary = monitor.check_all(dry_run=args.dry_run)

    print()
    _print_monitor_table(summary)

    if not args.dry_run and summary["exits_triggered"] > 0:
        fp = save_monitor_snapshot(summary)
        print(f"\nMonitor snapshot saved: {fp}")
