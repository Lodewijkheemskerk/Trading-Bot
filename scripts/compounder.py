"""
Step 5a: COMPOUND LEARNING — Analyze Trade Outcomes

Tracks performance metrics across all trades, evaluates prediction
accuracy, and appends failures to a knowledge base. The learning
system that closes the feedback loop.

Metrics tracked:
  - Win rate
  - Total P&L
  - Sharpe ratio (annualized)
  - Profit factor (gross wins / gross losses)
  - Brier score (prediction calibration)
  - Max drawdown
  - Trade count
"""

import json
import logging
import math
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import List, Optional, Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, TRADES_DIR, REFERENCES_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PerformanceReport:
    """Aggregate performance metrics."""
    total_trades: int
    executed_trades: int
    blocked_trades: int
    open_trades: int
    closed_trades: int
    win_rate: float               # Wins / closed trades
    total_pnl: float              # Sum of realized P&L
    avg_pnl: float                # Average P&L per closed trade
    sharpe_ratio: float           # Annualized Sharpe (daily returns)
    profit_factor: float          # Gross wins / gross losses
    brier_score: float            # Mean squared prediction error
    max_drawdown: float           # Maximum peak-to-trough as fraction
    best_trade_pnl: float
    worst_trade_pnl: float
    avg_edge: float               # Average edge at entry
    avg_confidence: float         # Average model confidence
    timestamp: str


# ---------------------------------------------------------------------------
# Compounder
# ---------------------------------------------------------------------------

class Compounder:
    """Analyzes trade outcomes and tracks performance metrics."""

    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or load_settings()
        self.compound_cfg = self.settings.get("compound", {})
        self.min_trades_for_stats = self.compound_cfg.get("min_trades_for_stats", 10)

    def load_all_trades(self) -> List[Dict[str, Any]]:
        """Load all trade records from execution snapshots."""
        trades = []
        for fp in sorted(TRADES_DIR.glob("execution_*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for trade in data.get("trades", []):
                    trades.append(trade)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to load %s: %s", fp, exc)
        return trades

    def analyze_trade(self, trade: Dict[str, Any], outcome: bool) -> Dict[str, Any]:
        """
        Analyze a single trade outcome.

        Args:
            trade: Trade dict from executor
            outcome: True if event resolved YES, False if NO

        Returns:
            Analysis dict with P&L, accuracy, and lesson.
        """
        direction = trade.get("direction", "buy_yes")
        entry_price = float(trade.get("entry_price", 0.5))
        position_size = float(trade.get("position_size_usd", 0))
        model_prob = float(trade.get("model_probability", 0.5))

        # P&L calculation for binary outcomes
        if direction == "buy_yes":
            if outcome:
                # Bought YES, event happened: profit = (1 - entry_price) * contracts
                # contracts = position_size / entry_price
                contracts = position_size / entry_price if entry_price > 0 else 0
                pnl = (1 - entry_price) * contracts
            else:
                # Bought YES, event didn't happen: lose position
                pnl = -position_size
        else:  # buy_no
            if not outcome:
                # Bought NO, event didn't happen: profit
                no_price = 1 - entry_price
                contracts = position_size / no_price if no_price > 0 else 0
                pnl = (1 - no_price) * contracts
            else:
                # Bought NO, event happened: lose position
                pnl = -position_size

        # Prediction accuracy (Brier component)
        actual = 1.0 if outcome else 0.0
        brier_component = (model_prob - actual) ** 2

        # Was the prediction correct?
        predicted_yes = model_prob > 0.5
        correct = (predicted_yes and outcome) or (not predicted_yes and not outcome)

        analysis = {
            "trade_id": trade.get("trade_id", "unknown"),
            "market_id": trade.get("market_id", "unknown"),
            "pnl": round(pnl, 2),
            "correct_prediction": correct,
            "brier_component": round(brier_component, 4),
            "model_probability": model_prob,
            "actual_outcome": actual,
            "direction": direction,
        }

        if not correct and position_size > 0:
            lesson = (
                f"Predicted {'YES' if predicted_yes else 'NO'} at {model_prob:.2f}, "
                f"actual was {'YES' if outcome else 'NO'}. "
                f"Edge: {trade.get('edge', 0):+.3f}. Lost ${abs(pnl):.2f}."
            )
            analysis["lesson"] = lesson
            logger.info("Trade %s: LOSS — %s", trade.get("trade_id"), lesson)
        elif position_size > 0:
            logger.info("Trade %s: WIN — P&L $%.2f", trade.get("trade_id"), pnl)

        return analysis

    def get_performance_report(self, trades: Optional[List[Dict]] = None) -> PerformanceReport:
        """
        Generate a performance report from trade history.

        Uses loaded trades if not provided.
        """
        if trades is None:
            trades = self.load_all_trades()

        total = len(trades)
        executed = [t for t in trades if t.get("risk_passed", False)]
        blocked = [t for t in trades if not t.get("risk_passed", False)]

        # Separate open vs closed
        open_trades = [t for t in executed if t.get("status") == "open"]
        closed = [t for t in executed if t.get("status") == "closed"]

        # Metrics from closed trades
        pnls = [float(t.get("pnl", 0)) for t in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        win_rate = len(wins) / len(closed) if closed else 0.0
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / len(closed) if closed else 0.0

        # Sharpe ratio (simplified — daily returns as proxy)
        if len(pnls) >= 2:
            mean_return = sum(pnls) / len(pnls)
            variance = sum((p - mean_return) ** 2 for p in pnls) / len(pnls)
            std_return = math.sqrt(variance) if variance > 0 else 1.0
            sharpe = (mean_return / std_return) * math.sqrt(252)  # Annualized
        else:
            sharpe = 0.0

        # Profit factor
        gross_wins = sum(wins) if wins else 0.0
        gross_losses = abs(sum(losses)) if losses else 0.0
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf") if gross_wins > 0 else 0.0

        # Brier score (from all executed trades with outcomes)
        brier_components = []
        for t in closed:
            model_p = float(t.get("model_probability", 0.5))
            # Infer outcome from P&L sign for closed trades
            pnl_val = float(t.get("pnl", 0))
            direction = t.get("direction", "buy_yes")
            if direction == "buy_yes":
                actual = 1.0 if pnl_val > 0 else 0.0
            else:
                actual = 0.0 if pnl_val > 0 else 1.0
            brier_components.append((model_p - actual) ** 2)
        brier_score = sum(brier_components) / len(brier_components) if brier_components else 0.0

        # Max drawdown from P&L series
        max_dd = 0.0
        peak = 0.0
        cumulative = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / max(peak, 1.0) if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        # Averages
        edges = [float(t.get("edge", 0)) for t in executed]
        confidences = [float(t.get("confidence", 0)) for t in executed]
        avg_edge = sum(edges) / len(edges) if edges else 0.0
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

        return PerformanceReport(
            total_trades=total,
            executed_trades=len(executed),
            blocked_trades=len(blocked),
            open_trades=len(open_trades),
            closed_trades=len(closed),
            win_rate=round(win_rate, 4),
            total_pnl=round(total_pnl, 2),
            avg_pnl=round(avg_pnl, 2),
            sharpe_ratio=round(sharpe, 2),
            profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
            brier_score=round(brier_score, 4),
            max_drawdown=round(max_dd, 4),
            best_trade_pnl=round(max(pnls), 2) if pnls else 0.0,
            worst_trade_pnl=round(min(pnls), 2) if pnls else 0.0,
            avg_edge=round(avg_edge, 4),
            avg_confidence=round(avg_conf, 4),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def nightly_review(self, trades: Optional[List[Dict]] = None) -> str:
        """
        Generate a nightly review summary and append failures to failure_log.md.

        Returns the review text.
        """
        if trades is None:
            trades = self.load_all_trades()

        today = date.today().isoformat()
        today_trades = [
            t for t in trades
            if t.get("timestamp", "").startswith(today)
        ]

        report = self.get_performance_report(trades)

        review_lines = [
            f"## Nightly Review — {today}",
            f"",
            f"**Today's activity:** {len(today_trades)} signals processed",
            f"**All-time:** {report.total_trades} total, {report.executed_trades} executed, {report.blocked_trades} blocked",
            f"**Open positions:** {report.open_trades}",
            f"**Closed trades:** {report.closed_trades}",
            f"**Win rate:** {report.win_rate:.1%}",
            f"**Total P&L:** ${report.total_pnl:.2f}",
            f"**Brier score:** {report.brier_score:.4f}",
            f"",
        ]

        # Append to failure log
        failures = [t for t in today_trades if not t.get("risk_passed", False)]
        if failures:
            review_lines.append(f"### Blocked trades ({len(failures)})")
            for t in failures[:5]:  # Cap at 5
                reasons = t.get("risk_failures", ["unknown"])
                review_lines.append(f"- {t.get('market_id', '?')}: {'; '.join(reasons)}")
            review_lines.append("")

        review_text = "\n".join(review_lines)

        # Append to failure_log.md
        REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
        log_fp = REFERENCES_DIR / "failure_log.md"
        with open(log_fp, "a", encoding="utf-8") as f:
            f.write(f"\n{review_text}\n")

        logger.info("Nightly review written to %s", log_fp)
        return review_text


def print_performance_report(report: PerformanceReport) -> None:
    """Print a formatted performance report."""
    print(f"\n{'='*50}")
    print(f"  PERFORMANCE REPORT")
    print(f"{'='*50}")
    print(f"  Total trades:     {report.total_trades}")
    print(f"  Executed:         {report.executed_trades}")
    print(f"  Blocked:          {report.blocked_trades}")
    print(f"  Open:             {report.open_trades}")
    print(f"  Closed:           {report.closed_trades}")
    print(f"{'─'*50}")
    print(f"  Win rate:         {report.win_rate:.1%}")
    print(f"  Total P&L:        ${report.total_pnl:.2f}")
    print(f"  Avg P&L/trade:    ${report.avg_pnl:.2f}")
    print(f"  Best trade:       ${report.best_trade_pnl:.2f}")
    print(f"  Worst trade:      ${report.worst_trade_pnl:.2f}")
    print(f"{'─'*50}")
    print(f"  Sharpe ratio:     {report.sharpe_ratio:.2f}")
    print(f"  Profit factor:    {report.profit_factor:.2f}")
    print(f"  Brier score:      {report.brier_score:.4f}")
    print(f"  Max drawdown:     {report.max_drawdown:.2%}")
    print(f"{'─'*50}")
    print(f"  Avg edge:         {report.avg_edge:+.4f}")
    print(f"  Avg confidence:   {report.avg_confidence:.4f}")
    print(f"{'='*50}")
