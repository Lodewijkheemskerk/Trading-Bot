"""
Step 4c: TRADE EXECUTION — Paper or Demo (Kalshi)

Takes TradeSignal dicts, runs risk validation, calculates Kelly position
sizing, and either records paper trades OR places real limit orders on
Kalshi's demo exchange.

Modes (controlled by KALSHI_ENV env var or settings):
  - "paper"  — local bookkeeping only, no API calls (default)
  - "demo"   — real limit orders on Kalshi demo exchange (mock funds)
  - "live"   — production Kalshi exchange (not yet enabled)

Trade flow:
  1. Load prediction snapshot
  2. For each signal where should_trade=True:
     a. Calculate Kelly position size
     b. Run 12 risk checks
     c. If risk passes: create Trade, place order (if demo/live), update portfolio
     d. If risk fails: log reasons, skip
  3. Persist trades and portfolio state as JSON
"""

import json
import logging
import math
import os
import sys
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, TRADES_DIR, PREDICTIONS_DIR
from scripts.validate_risk import RiskManager, RiskValidation
from scripts.kelly_size import calculate_kelly, KellyResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes — S04→S05 boundary contract
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """A trade record — consumed by S05 learning system."""
    trade_id: str                  # Unique trade identifier
    market_id: str
    market_title: str
    direction: str                 # "buy_yes" or "buy_no"
    entry_price: float             # Market price at entry (actual fill)
    signal_price: float            # Market price at signal time (prediction)
    slippage: float                # entry_price - signal_price (drift from signal to fill)
    position_size_usd: float       # Dollar amount risked
    model_probability: float       # Ensemble probability at entry
    signal_strength: float         # Signal strength at entry
    edge: float                    # Edge at entry
    status: str                    # "open", "closed", "expired", "blocked", "no_signal", "resting"
    pnl: float                     # Realized P&L (0 while open)
    risk_passed: bool              # Whether risk validation passed
    risk_failures: List[str]       # Risk check failure reasons (empty if passed)
    kelly_fraction: float          # Kelly fraction used
    timestamp: str                 # ISO datetime of trade creation
    # Kalshi order fields (populated in demo/live mode)
    execution_mode: str = "paper"  # "paper", "demo", or "live"
    kalshi_order_id: str = ""      # Kalshi order ID (empty in paper mode)
    kalshi_status: str = ""        # Kalshi order status (resting, executed, canceled)
    contracts: int = 0             # Number of contracts ordered
    limit_price: float = 0.0       # Limit price submitted to Kalshi
    fill_count: float = 0.0        # Contracts filled so far


# ---------------------------------------------------------------------------
# TradeExecutor
# ---------------------------------------------------------------------------

class TradeExecutor:
    """
    Executes trades after risk validation and Kelly sizing.

    In paper mode: records trades locally (no API calls).
    In demo mode: places real limit orders on Kalshi demo exchange.
    """

    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or load_settings()
        self.risk_manager = RiskManager(settings=self.settings)

        # Determine execution mode
        from dotenv import load_dotenv
        load_dotenv()
        self.mode = os.getenv("KALSHI_ENV", "paper").lower()
        if self.mode not in ("paper", "demo", "live"):
            logger.warning("Unknown KALSHI_ENV '%s', falling back to paper", self.mode)
            self.mode = "paper"

        if self.mode == "live":
            logger.error("LIVE TRADING NOT ENABLED — falling back to paper")
            self.mode = "paper"

        # Initialize Kalshi client for demo/live modes
        self._kalshi = None
        if self.mode == "demo":
            try:
                from scripts.kalshi_client import KalshiClient
                self._kalshi = KalshiClient.from_env(settings=self.settings)
                balance = self._kalshi.get_balance_dollars()
                logger.info("Kalshi demo connected — balance: $%.2f", balance)
            except Exception as exc:
                logger.error("Failed to connect to Kalshi demo: %s — falling back to paper", exc)
                self.mode = "paper"
                self._kalshi = None

    def execute_signal(self, signal: Dict[str, Any]) -> Trade:
        """
        Process a trade signal through risk checks and Kelly sizing.

        Returns a Trade object regardless of whether risk passed
        (risk_passed=False trades are recorded but not executed).
        """
        market_id = signal.get("market_id", "unknown")
        market_title = signal.get("market_title", "")
        direction = signal.get("direction", "buy_yes")
        market_prob = float(signal.get("market_probability", 0.5))
        ensemble_prob = float(signal.get("ensemble_probability", 0.5))
        edge = float(signal.get("edge", 0))
        signal_strength = float(signal.get("signal_strength", 0))
        confidence = float(signal.get("confidence", 0.5))

        # Step 1: Kelly position sizing
        kelly = calculate_kelly(
            ensemble_probability=ensemble_prob,
            market_probability=market_prob,
            direction=direction,
            bankroll=self.risk_manager.state.current_bankroll,
            settings=self.settings,
        )

        # Step 2: Risk validation
        risk_result = self.risk_manager.validate_trade(
            signal, position_size_usd=kelly.position_size_usd
        )

        # Step 3: Create trade record
        trade_id = uuid.uuid4().hex[:12]

        # Slippage: in paper mode, entry = signal price (no real fill)
        # In demo/live mode, entry_price may differ based on fill
        signal_price = market_prob  # Price at signal generation time
        entry_price = market_prob   # Will be updated with fill price in demo mode
        slippage = 0.0

        # Compute contract count and limit price for Kalshi
        # Kalshi contracts pay $1 on win. If buying YES at $0.55, you pay $0.55/contract.
        # position_size_usd / limit_price = number of contracts
        side = "yes" if direction == "buy_yes" else "no"
        limit_price = market_prob if side == "yes" else (1.0 - market_prob)
        # Clamp limit price to valid range
        limit_price = max(0.01, min(0.99, round(limit_price, 2)))
        position_usd = kelly.position_size_usd if risk_result.overall_pass else 0.0
        contracts = max(1, int(position_usd / limit_price)) if position_usd > 0 else 0

        # Kalshi order fields (populated in demo mode)
        kalshi_order_id = ""
        kalshi_status = ""
        fill_count = 0.0

        if risk_result.overall_pass and self.mode == "demo" and self._kalshi:
            # Orderbook depth check before placing order
            try:
                from scripts.orderbook import check_depth
                ob_raw = self._kalshi.get_orderbook(market_id)
                depth_cfg = self.settings.get("risk", {})
                min_depth_ratio = depth_cfg.get("min_depth_ratio", 2.0)
                depth = check_depth(ob_raw, market_id, side, limit_price, contracts, min_depth_ratio)
                if not depth.sufficient:
                    logger.warning("DEPTH CHECK FAILED for %s: %s", market_id, depth.reason)
                    # Still place the order — it's a limit order, it will rest.
                    # But log the warning so we know liquidity was thin.
                else:
                    logger.info("Depth OK for %s: %s", market_id, depth.reason)
            except Exception as exc:
                logger.warning("Orderbook depth check failed for %s: %s (proceeding anyway)", market_id, exc)

            # Place real order on Kalshi demo
            try:
                order_result = self._kalshi.place_order(
                    ticker=market_id,
                    side=side,
                    action="buy",
                    price_dollars=limit_price,
                    count=contracts,
                )
                order = order_result.get("order", {})
                kalshi_order_id = order.get("order_id", "")
                kalshi_status = order.get("status", "")
                fill_count = float(order.get("fill_count_fp", "0") or "0")

                # If we got an immediate fill, record actual fill price
                if fill_count > 0:
                    # Use maker/taker fill cost to compute average fill price
                    taker_cost = float(order.get("taker_fill_cost_dollars", "0") or "0")
                    maker_cost = float(order.get("maker_fill_cost_dollars", "0") or "0")
                    total_cost = taker_cost + maker_cost
                    if fill_count > 0 and total_cost > 0:
                        entry_price = total_cost / fill_count
                        slippage = round(entry_price - signal_price, 6)

                logger.info(
                    "KALSHI ORDER: %s %s %s %s @ $%.2f x%d → %s (filled: %.0f)",
                    kalshi_order_id[:12], "buy", side, market_id,
                    limit_price, contracts, kalshi_status, fill_count,
                )
            except Exception as exc:
                logger.error("Kalshi order failed for %s: %s", market_id, exc)
                kalshi_status = f"error: {exc}"

        trade = Trade(
            trade_id=trade_id,
            market_id=market_id,
            market_title=market_title,
            direction=direction,
            entry_price=entry_price,
            signal_price=signal_price,
            slippage=slippage,
            position_size_usd=position_usd,
            model_probability=ensemble_prob,
            signal_strength=signal_strength,
            edge=edge,
            status="resting" if kalshi_status == "resting" else ("open" if risk_result.overall_pass else "blocked"),
            pnl=0.0,
            risk_passed=risk_result.overall_pass,
            risk_failures=risk_result.failure_reasons,
            kelly_fraction=kelly.adjusted_fraction,
            timestamp=datetime.now(timezone.utc).isoformat(),
            execution_mode=self.mode,
            kalshi_order_id=kalshi_order_id,
            kalshi_status=kalshi_status,
            contracts=contracts,
            limit_price=limit_price,
            fill_count=fill_count,
        )

        if risk_result.overall_pass:
            # Update portfolio state
            self.risk_manager.record_trade(
                position_size=position_usd,
                api_cost=0.001,  # Nominal API cost per trade
            )
            logger.info(
                "TRADE EXECUTED [%s]: %s %s %s @ %.3f size=$%.2f edge=%+.4f",
                self.mode, trade_id, direction, market_id, entry_price,
                position_usd, edge,
            )
        else:
            logger.warning(
                "TRADE BLOCKED: %s %s — %s",
                market_id, direction, "; ".join(risk_result.failure_reasons),
            )

        return trade

    def check_order_status(self, trade: Trade) -> Trade:
        """
        Check the current status of a Kalshi order and update the trade.

        Only works in demo/live mode for orders that have a kalshi_order_id.
        """
        if not trade.kalshi_order_id or not self._kalshi:
            return trade

        try:
            data = self._kalshi.get_order(trade.kalshi_order_id)
            order = data.get("order", {})
            trade.kalshi_status = order.get("status", trade.kalshi_status)
            trade.fill_count = float(order.get("fill_count_fp", "0") or "0")

            remaining = float(order.get("remaining_count_fp", "0") or "0")
            if trade.kalshi_status == "executed" or (trade.fill_count > 0 and remaining == 0):
                trade.status = "open"  # Fully filled → open position
                # Update entry price from fill cost
                taker_cost = float(order.get("taker_fill_cost_dollars", "0") or "0")
                maker_cost = float(order.get("maker_fill_cost_dollars", "0") or "0")
                total_cost = taker_cost + maker_cost
                if trade.fill_count > 0 and total_cost > 0:
                    trade.entry_price = total_cost / trade.fill_count
                    trade.slippage = round(trade.entry_price - trade.signal_price, 6)
            elif trade.kalshi_status == "canceled":
                trade.status = "canceled"
                trade.position_size_usd = 0.0

            logger.info(
                "Order %s: status=%s filled=%.0f/%d",
                trade.kalshi_order_id[:12], trade.kalshi_status,
                trade.fill_count, trade.contracts,
            )
        except Exception as exc:
            logger.warning("Failed to check order %s: %s", trade.kalshi_order_id[:12], exc)

        return trade

    def get_kalshi_balance(self) -> Optional[float]:
        """Get current Kalshi balance in dollars (demo/live only)."""
        if self._kalshi:
            return self._kalshi.get_balance_dollars()
        return None

    def get_kalshi_positions(self) -> List[dict]:
        """Get current Kalshi positions (demo/live only)."""
        if self._kalshi:
            return self._kalshi.get_positions()
        return []

    def save_trade(self, trade: Trade, output_dir: Optional[Path] = None) -> Path:
        """Save a single trade as JSON."""
        out = output_dir or TRADES_DIR
        out.mkdir(parents=True, exist_ok=True)

        fp = out / f"trade_{trade.trade_id}.json"
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(asdict(trade), f, indent=2, ensure_ascii=False, default=str)

        logger.info("Trade saved: %s", fp)
        return fp


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def load_latest_prediction_snapshot() -> Optional[dict]:
    """Load the most recent prediction snapshot."""
    snapshots = sorted(PREDICTIONS_DIR.glob("predictions_*.json"), reverse=True)
    if not snapshots:
        logger.warning("No prediction snapshots found in %s", PREDICTIONS_DIR)
        return None
    fp = snapshots[0]
    logger.info("Loading prediction snapshot: %s", fp)
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_execution_snapshot(trades: List[Trade], output_dir: Optional[Path] = None) -> Path:
    """Save all trade results as a single timestamped JSON snapshot."""
    out = output_dir or TRADES_DIR
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = out / f"execution_{ts}.json"

    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_signals": len(trades),
        "executed": sum(1 for t in trades if t.risk_passed),
        "blocked": sum(1 for t in trades if not t.risk_passed),
        "trades": [asdict(t) for t in trades],
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    logger.info("Execution snapshot saved: %s", fp)
    return fp


def _print_risk_table(signal: Dict[str, Any], risk_manager: RiskManager, kelly: KellyResult) -> None:
    """Print risk check results for a single signal."""
    validation = risk_manager.validate_trade(signal, kelly.position_size_usd)

    print(f"\n  Risk checks for: {signal.get('market_title', '?')[:50]}")
    print(f"  {'Check':<25} {'Result':>6}  {'Detail'}")
    print(f"  {'-'*60}")
    for c in validation.checks:
        status = "PASS" if c.passed else "FAIL"
        print(f"  {c.name:<25} {status:>6}  {c.detail}")
    print(f"  {'─'*60}")
    verdict = "✓ ALL PASS" if validation.overall_pass else f"✗ BLOCKED ({len(validation.failure_reasons)} failures)"
    print(f"  Verdict: {verdict}")


def _print_trade_table(trades: List[Trade]) -> None:
    """Print a formatted table of trade results."""
    header = (
        f"{'#':>3}  {'Market':<35} {'Dir':<8} {'Entry':>5} "
        f"{'Size':>7} {'Edge':>6} {'Kelly':>6} {'Status':<8}"
    )
    print(f"\n{header}")
    print("-" * len(header))

    for i, t in enumerate(trades, 1):
        title = t.market_title[:35].ljust(35)
        print(
            f"{i:3d}  {title} {t.direction:<8} {t.entry_price:5.3f} "
            f"${t.position_size_usd:6.2f} {t.edge:+6.3f} "
            f"{t.kelly_fraction:6.4f} {t.status:<8}"
        )

    executed = sum(1 for t in trades if t.risk_passed)
    blocked = sum(1 for t in trades if not t.risk_passed)
    total_size = sum(t.position_size_usd for t in trades if t.risk_passed)
    print(f"\n{executed} executed, {blocked} blocked, total position: ${total_size:.2f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Execute paper trades from latest prediction snapshot."
    )
    parser.add_argument(
        "--top", type=int, default=None,
        help="Process top N signals only (default: all).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show risk checks only, don't execute trades.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Load latest prediction snapshot
    snapshot = load_latest_prediction_snapshot()
    if snapshot is None:
        print("No prediction snapshots found. Run predictor.py first.")
        sys.exit(1)

    signals = snapshot.get("signals", [])
    if not signals:
        print("Prediction snapshot contains no signals.")
        sys.exit(1)

    if args.top:
        signals = signals[:args.top]

    # Filter to tradeable signals
    tradeable = [s for s in signals if s.get("should_trade", False)]

    print(f"\nPaper Trade Executor")
    print(f"Prediction snapshot: {snapshot.get('timestamp', '?')}")
    print(f"Total signals: {len(signals)}, tradeable: {len(tradeable)}")

    executor = TradeExecutor()

    # Show risk checks for first tradeable signal (or first signal if none tradeable)
    demo_signal = tradeable[0] if tradeable else signals[0]
    kelly_demo = calculate_kelly(
        ensemble_probability=float(demo_signal.get("ensemble_probability", 0.5)),
        market_probability=float(demo_signal.get("market_probability", 0.5)),
        direction=demo_signal.get("direction", "buy_yes"),
        bankroll=executor.risk_manager.state.current_bankroll,
        settings=executor.settings,
    )
    _print_risk_table(demo_signal, executor.risk_manager, kelly_demo)

    if args.dry_run:
        print("\n[Dry run — no trades executed]")
        sys.exit(0)

    # Execute trades
    trades: List[Trade] = []
    for signal in signals:
        if not signal.get("should_trade", False):
            # Record as blocked (didn't even attempt risk checks)
            mkt_prob = float(signal.get("market_probability", 0.5))
            trade = Trade(
                trade_id=uuid.uuid4().hex[:12],
                market_id=signal.get("market_id", "unknown"),
                market_title=signal.get("market_title", ""),
                direction=signal.get("direction", "buy_yes"),
                entry_price=mkt_prob,
                signal_price=mkt_prob,
                slippage=0.0,
                position_size_usd=0.0,
                model_probability=float(signal.get("ensemble_probability", 0.5)),
                signal_strength=float(signal.get("signal_strength", 0)),
                edge=float(signal.get("edge", 0)),
                status="no_signal",
                pnl=0.0,
                risk_passed=False,
                risk_failures=["should_trade=False (below signal thresholds)"],
                kelly_fraction=0.0,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            trades.append(trade)
            continue

        trade = executor.execute_signal(signal)
        trades.append(trade)
        executor.save_trade(trade)

    _print_trade_table(trades)

    # Save execution snapshot
    fp = save_execution_snapshot(trades)
    print(f"Execution snapshot saved: {fp}")

    # Show portfolio state
    state = executor.risk_manager.state
    print(f"\nPortfolio: bankroll=${state.current_bankroll:.2f} "
          f"open={state.open_positions} "
          f"daily_pnl=${state.daily_pnl:.2f} "
          f"total_trades={state.total_trades}")
