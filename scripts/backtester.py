"""
Step 8: BACKTEST — Replay Historical Predictions Against Outcomes

Collects settled markets from Kalshi, matches them against our saved
prediction snapshots, and computes performance metrics: Brier Score,
P&L, win rate, Sharpe ratio, calibration curve, max drawdown.

Two modes:
  1. From saved data: uses existing prediction snapshots + Kalshi outcomes
  2. From Kalshi history: fetches settled markets and evaluates our
     historical predictions against actual results

No API cost — uses saved pipeline data + Kalshi settlement status.
"""

import json
import logging
import math
import os
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings

logger = logging.getLogger(__name__)

# Directories
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PREDICTIONS_DIR = DATA_DIR / "predictions"
BACKTEST_DIR = DATA_DIR / "backtest"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    """A single backtested trade with predicted vs actual outcome."""
    market_id: str
    market_title: str
    prediction_timestamp: str
    ensemble_probability: float   # What our model predicted
    market_probability: float     # What the market was pricing
    edge: float                   # ensemble - market
    direction: str                # buy_yes / buy_no
    should_trade: bool            # Did it pass thresholds?
    confidence: float
    signal_strength: float

    # Outcome (filled after matching with settlement)
    outcome: Optional[float] = None     # 1.0 = YES, 0.0 = NO, None = unsettled
    settlement_status: str = "unknown"  # settled_yes, settled_no, unsettled

    # P&L (computed if should_trade and outcome known)
    entry_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    correct: Optional[bool] = None


@dataclass
class CalibrationBucket:
    """One bucket in the calibration curve."""
    bucket_low: float
    bucket_high: float
    label: str
    count: int
    avg_predicted: float
    avg_actual: float
    error: float               # |avg_predicted - avg_actual|


@dataclass
class BacktestResult:
    """Complete backtest output."""
    run_timestamp: str
    date_range: str
    total_markets_evaluated: int
    total_predictions: int
    settled_count: int
    unsettled_count: int

    # Trade metrics (only for should_trade=True & settled)
    trades_taken: int
    trades_skipped: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_pnl_per_trade: float
    max_drawdown: float
    sharpe_ratio: float

    # Calibration (all settled predictions, not just trades)
    brier_score: float
    calibration_buckets: List[CalibrationBucket]

    # Details
    trades: List[BacktestTrade]


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class Backtester:
    """Replays historical predictions against market outcomes."""

    def __init__(self, settings: Optional[dict] = None):
        s = settings or load_settings()
        pred_cfg = s.get("prediction", {})
        self.min_edge = pred_cfg.get("min_edge", 0.04)
        self.min_confidence = pred_cfg.get("min_confidence", 0.65)

        risk_cfg = s.get("risk", {})
        self.position_size_pct = risk_cfg.get("max_position_pct", 0.05)
        self.bankroll = 1000.0  # Default backtest bankroll

        self._kalshi_client = None

    # ------------------------------------------------------------------
    # Kalshi client (lazy)
    # ------------------------------------------------------------------

    def _get_kalshi(self):
        if self._kalshi_client is None:
            try:
                from scripts.kalshi_client import KalshiClient
                self._kalshi_client = KalshiClient.from_env()
            except Exception as exc:
                logger.warning("Could not init Kalshi client: %s", exc)
        return self._kalshi_client

    # ------------------------------------------------------------------
    # Load saved predictions
    # ------------------------------------------------------------------

    def load_all_predictions(self) -> List[Dict[str, Any]]:
        """
        Load all saved prediction snapshots.

        Returns flat list of signal dicts, each with an added
        '_snapshot_file' and '_snapshot_timestamp' key.
        """
        all_signals = []
        for fp in sorted(PREDICTIONS_DIR.glob("predictions_*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ts = data.get("timestamp", fp.stem)
                for sig in data.get("signals", []):
                    sig["_snapshot_file"] = fp.name
                    sig["_snapshot_timestamp"] = ts
                    all_signals.append(sig)
            except Exception as exc:
                logger.warning("Could not load %s: %s", fp.name, exc)

        logger.info("Loaded %d predictions from %d snapshot files",
                     len(all_signals), len(list(PREDICTIONS_DIR.glob("predictions_*.json"))))
        return all_signals

    def deduplicate_predictions(self, signals: List[Dict]) -> List[Dict]:
        """
        Keep only the latest prediction per market_id.

        When we predicted the same market multiple times (across pipeline runs),
        use the most recent prediction for backtesting.
        """
        latest: Dict[str, Dict] = {}
        for sig in signals:
            mid = sig.get("market_id", "")
            ts = sig.get("timestamp", sig.get("_snapshot_timestamp", ""))
            if mid not in latest or ts > latest[mid].get("timestamp", ""):
                latest[mid] = sig
        result = list(latest.values())
        logger.info("Deduplicated: %d unique markets from %d total predictions",
                     len(result), len(signals))
        return result

    # ------------------------------------------------------------------
    # Fetch outcomes from Kalshi
    # ------------------------------------------------------------------

    def fetch_outcome(self, market_id: str) -> Tuple[Optional[float], str]:
        """
        Check if a market has settled on Kalshi.

        Returns (outcome, status):
          outcome: 1.0 (YES), 0.0 (NO), or None (unsettled)
          status: 'settled_yes', 'settled_no', 'unsettled', 'error'
        """
        client = self._get_kalshi()
        if not client:
            return None, "error"

        try:
            market = client.get_market(market_id)
            status = market.get("status", "").lower()
            result = market.get("result", "").lower()

            if status in ("settled", "finalized", "closed"):
                if result == "yes":
                    return 1.0, "settled_yes"
                elif result == "no":
                    return 0.0, "settled_no"
                else:
                    # Some markets settle with a value
                    return None, "unsettled"
            else:
                return None, "unsettled"

        except Exception as exc:
            logger.warning("Could not fetch outcome for %s: %s", market_id, exc)
            return None, "error"

    def fetch_all_outcomes(self, market_ids: List[str]) -> Dict[str, Tuple[Optional[float], str]]:
        """Fetch outcomes for all market IDs. Returns dict of market_id → (outcome, status)."""
        outcomes = {}
        for mid in market_ids:
            outcomes[mid] = self.fetch_outcome(mid)
        settled = sum(1 for _, (o, _) in outcomes.items() if o is not None)
        logger.info("Fetched outcomes: %d/%d settled", settled, len(market_ids))
        return outcomes

    # ------------------------------------------------------------------
    # Run backtest
    # ------------------------------------------------------------------

    def run(self, fetch_outcomes: bool = True) -> BacktestResult:
        """
        Run a full backtest from saved prediction data.

        Args:
            fetch_outcomes: If True, query Kalshi API for settlement status.
                            If False, only use previously cached outcomes.
        """
        # Load and deduplicate predictions
        all_signals = self.load_all_predictions()
        if not all_signals:
            return self._empty_result("No prediction data found")

        signals = self.deduplicate_predictions(all_signals)

        # Fetch outcomes
        market_ids = [s.get("market_id") for s in signals if s.get("market_id")]
        outcomes = {}
        if fetch_outcomes:
            outcomes = self.fetch_all_outcomes(market_ids)

        # Also load any cached outcomes
        cached = self._load_cached_outcomes()
        for mid, val in cached.items():
            if mid not in outcomes:
                outcomes[mid] = val

        # Build BacktestTrade list
        trades = []
        for sig in signals:
            mid = sig.get("market_id", "")
            outcome_val, outcome_status = outcomes.get(mid, (None, "unknown"))

            bt = BacktestTrade(
                market_id=mid,
                market_title=sig.get("market_title", ""),
                prediction_timestamp=sig.get("timestamp", ""),
                ensemble_probability=float(sig.get("ensemble_probability", 0.5)),
                market_probability=float(sig.get("market_probability", 0.5)),
                edge=float(sig.get("edge", 0.0)),
                direction=sig.get("direction", "buy_yes"),
                should_trade=bool(sig.get("should_trade", False)),
                confidence=float(sig.get("confidence", 0.5)),
                signal_strength=float(sig.get("signal_strength", 0.0)),
                outcome=outcome_val,
                settlement_status=outcome_status,
            )

            # Compute P&L for trades we would have taken
            if bt.should_trade and bt.outcome is not None:
                bt.entry_price = bt.market_probability
                position_size = self.bankroll * self.position_size_pct

                if bt.direction == "buy_yes":
                    # Bought YES at market_probability
                    # Pays out $1 if outcome=YES, $0 if NO
                    bt.pnl = position_size * (bt.outcome - bt.entry_price) / max(bt.entry_price, 0.01)
                    bt.correct = bt.outcome == 1.0
                else:
                    # Bought NO at (1 - market_probability)
                    no_price = 1.0 - bt.entry_price
                    bt.pnl = position_size * ((1.0 - bt.outcome) - no_price) / max(no_price, 0.01)
                    bt.correct = bt.outcome == 0.0

                bt.pnl = round(bt.pnl, 2)
                bt.pnl_pct = round(bt.pnl / position_size * 100, 2) if position_size > 0 else 0

            trades.append(bt)

        # Compute metrics
        result = self._compute_metrics(trades)

        # Save outcomes cache
        self._save_cached_outcomes(outcomes)

        # Save full result
        self._save_result(result)

        return result

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _compute_metrics(self, trades: List[BacktestTrade]) -> BacktestResult:
        """Compute all backtest metrics from trade list."""
        settled = [t for t in trades if t.outcome is not None]
        unsettled = [t for t in trades if t.outcome is None]
        taken = [t for t in settled if t.should_trade]
        skipped = [t for t in settled if not t.should_trade]

        # Win/loss
        wins = sum(1 for t in taken if t.correct)
        losses = sum(1 for t in taken if t.correct is False)
        win_rate = wins / max(len(taken), 1)

        # P&L
        pnls = [t.pnl for t in taken]
        total_pnl = round(sum(pnls), 2)
        avg_pnl = round(total_pnl / max(len(taken), 1), 2)

        # Max drawdown
        max_dd = self._compute_max_drawdown(pnls)

        # Sharpe ratio (annualized, assuming daily trades)
        sharpe = self._compute_sharpe(pnls)

        # Brier score (on ALL settled predictions, not just trades)
        brier = self._compute_brier(settled)

        # Calibration
        calibration = self._compute_calibration(settled)

        # Date range
        timestamps = [t.prediction_timestamp for t in trades if t.prediction_timestamp]
        if timestamps:
            dates = sorted(timestamps)
            date_range = f"{dates[0][:10]} to {dates[-1][:10]}"
        else:
            date_range = "unknown"

        return BacktestResult(
            run_timestamp=datetime.now(timezone.utc).isoformat(),
            date_range=date_range,
            total_markets_evaluated=len(trades),
            total_predictions=len(trades),
            settled_count=len(settled),
            unsettled_count=len(unsettled),
            trades_taken=len(taken),
            trades_skipped=len(skipped),
            wins=wins,
            losses=losses,
            win_rate=round(win_rate, 4),
            total_pnl=total_pnl,
            avg_pnl_per_trade=avg_pnl,
            max_drawdown=max_dd,
            sharpe_ratio=sharpe,
            brier_score=brier,
            calibration_buckets=calibration,
            trades=trades,
        )

    @staticmethod
    def _compute_brier(settled: List[BacktestTrade]) -> float:
        """Brier Score: mean of (predicted - outcome)². Lower is better."""
        if not settled:
            return 0.0
        scores = []
        for t in settled:
            pred = t.ensemble_probability
            actual = t.outcome
            scores.append((pred - actual) ** 2)
        return round(float(np.mean(scores)), 4)

    @staticmethod
    def _compute_max_drawdown(pnls: List[float]) -> float:
        """Max drawdown from a sequence of P&L values."""
        if not pnls:
            return 0.0
        cumulative = np.cumsum(pnls)
        peak = np.maximum.accumulate(cumulative)
        drawdowns = cumulative - peak
        return round(float(np.min(drawdowns)), 2)

    @staticmethod
    def _compute_sharpe(pnls: List[float], risk_free: float = 0.0) -> float:
        """Sharpe ratio from P&L series."""
        if len(pnls) < 2:
            return 0.0
        mean_ret = np.mean(pnls) - risk_free
        std_ret = np.std(pnls, ddof=1)
        if std_ret == 0:
            return 0.0
        # Annualize assuming ~250 trading days
        sharpe = (mean_ret / std_ret) * math.sqrt(250)
        return round(float(sharpe), 2)

    @staticmethod
    def _compute_calibration(settled: List[BacktestTrade]) -> List[CalibrationBucket]:
        """Compute calibration curve in 10% buckets."""
        buckets = []
        for low in np.arange(0, 1.0, 0.1):
            high = low + 0.1
            label = f"{int(low*100)}-{int(high*100)}%"
            in_bucket = [t for t in settled if low <= t.ensemble_probability < high]

            if in_bucket:
                avg_pred = float(np.mean([t.ensemble_probability for t in in_bucket]))
                avg_actual = float(np.mean([t.outcome for t in in_bucket]))
                error = abs(avg_pred - avg_actual)
            else:
                avg_pred = 0.0
                avg_actual = 0.0
                error = 0.0

            buckets.append(CalibrationBucket(
                bucket_low=round(float(low), 2),
                bucket_high=round(float(high), 2),
                label=label,
                count=len(in_bucket),
                avg_predicted=round(avg_pred, 4),
                avg_actual=round(avg_actual, 4),
                error=round(error, 4),
            ))

        return buckets

    # ------------------------------------------------------------------
    # Cache & persistence
    # ------------------------------------------------------------------

    def _load_cached_outcomes(self) -> Dict[str, Tuple[Optional[float], str]]:
        """Load previously cached outcomes."""
        fp = BACKTEST_DIR / "outcomes_cache.json"
        if not fp.exists():
            return {}
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: (v[0], v[1]) for k, v in data.items()}
        except Exception:
            return {}

    def _save_cached_outcomes(self, outcomes: Dict[str, Tuple[Optional[float], str]]):
        """Cache outcomes for future runs."""
        BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
        fp = BACKTEST_DIR / "outcomes_cache.json"

        # Merge with existing cache
        existing = self._load_cached_outcomes()
        existing.update(outcomes)

        serializable = {k: list(v) for k, v in existing.items()}
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)

    def _save_result(self, result: BacktestResult):
        """Save backtest result to JSON."""
        BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = BACKTEST_DIR / f"backtest_{ts}.json"

        data = asdict(result)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        logger.info("Backtest result saved: %s", fp)
        return fp

    def _empty_result(self, reason: str) -> BacktestResult:
        return BacktestResult(
            run_timestamp=datetime.now(timezone.utc).isoformat(),
            date_range="N/A",
            total_markets_evaluated=0,
            total_predictions=0,
            settled_count=0,
            unsettled_count=0,
            trades_taken=0,
            trades_skipped=0,
            wins=0, losses=0, win_rate=0.0,
            total_pnl=0.0, avg_pnl_per_trade=0.0,
            max_drawdown=0.0, sharpe_ratio=0.0,
            brier_score=0.0,
            calibration_buckets=[],
            trades=[],
        )

    # ------------------------------------------------------------------
    # Load latest result (for dashboard)
    # ------------------------------------------------------------------

    @staticmethod
    def load_latest_result() -> Optional[Dict[str, Any]]:
        """Load the most recent backtest result for dashboard display."""
        BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(BACKTEST_DIR.glob("backtest_*.json"), reverse=True)
        if not files:
            return None
        try:
            with open(files[0], "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Backtest prediction pipeline against actual outcomes.")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetching outcomes from Kalshi (use cache only).")
    parser.add_argument("--bankroll", type=float, default=1000.0, help="Starting bankroll for P&L calculation.")
    args = parser.parse_args()

    bt = Backtester()
    bt.bankroll = args.bankroll

    print("\nBacktest Engine")
    print("=" * 50)

    result = bt.run(fetch_outcomes=not args.no_fetch)

    print(f"\nDate range:           {result.date_range}")
    print(f"Markets evaluated:    {result.total_markets_evaluated}")
    print(f"Settled:              {result.settled_count}")
    print(f"Unsettled:            {result.unsettled_count}")
    print(f"\nTrades taken:         {result.trades_taken} (edge > {bt.min_edge*100:.0f}%)")
    print(f"Trades skipped:       {result.trades_skipped}")
    print(f"Win rate:             {result.win_rate:.1%} ({result.wins}/{result.wins + result.losses})")
    print(f"Total P&L:            ${result.total_pnl:+.2f}")
    print(f"Avg P&L per trade:    ${result.avg_pnl_per_trade:+.2f}")
    print(f"Max drawdown:         ${result.max_drawdown:.2f}")
    print(f"Sharpe ratio:         {result.sharpe_ratio:.2f}")
    print(f"Brier score:          {result.brier_score:.4f}")

    if result.calibration_buckets:
        print(f"\nCalibration:")
        for b in result.calibration_buckets:
            if b.count > 0:
                bar = "#" * min(b.count * 3, 30)
                print(f"  {b.label:>8}  n={b.count:<3}  pred={b.avg_predicted:.0%}  actual={b.avg_actual:.0%}  err={b.error:.0%}  {bar}")

    # Show individual trades
    settled_trades = [t for t in result.trades if t.outcome is not None and t.should_trade]
    if settled_trades:
        print(f"\nTrade Details:")
        print(f"  {'Market':<40} {'Pred':>5} {'Mkt':>5} {'Edge':>6} {'Dir':<8} {'Out':>4} {'P&L':>8} {'OK':>3}")
        print("  " + "-" * 90)
        for t in settled_trades:
            ok = "Y" if t.correct else "N"
            print(f"  {t.market_title[:40]:<40} {t.ensemble_probability:5.0%} {t.market_probability:5.0%} "
                  f"{t.edge:+5.0%} {t.direction:<8} {t.outcome:4.0f} {t.pnl:+8.2f} {ok:>3}")
