"""
Step 4a: RISK MANAGEMENT — Validate Trades Against 12 Deterministic Rules

All risk checks use config thresholds and portfolio state only.
No LLM output is used in risk decisions (D005).

Checks:
  1.  min_edge          — edge must exceed prediction.min_edge
  2.  min_confidence    — confidence must exceed prediction.min_confidence
  3.  max_position_pct  — single position ≤ X% of bankroll
  4.  max_concurrent    — open positions ≤ limit
  5.  max_daily_loss    — daily loss ≤ X% of bankroll
  6.  max_drawdown      — drawdown from peak ≤ X% of peak
  7.  kill_switch       — STOP file must not exist
  8.  max_slippage      — implied slippage ≤ X%
  9.  bankroll_positive — remaining bankroll > 0
  10. max_api_cost      — daily API cost ≤ limit
  11. var_95            — portfolio VaR at 95% confidence ≤ threshold
  12. max_total_exposure — total open exposure + new position ≤ threshold
"""

import json
import logging
import math
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, date
from pathlib import Path
from typing import List, Optional, Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, TRADES_DIR, KILL_SWITCH_FILE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RiskCheck:
    """Result of a single risk check."""
    name: str
    passed: bool
    detail: str        # Human-readable explanation
    threshold: Any     # Config threshold value
    actual: Any        # Actual value checked


@dataclass
class RiskValidation:
    """Aggregate result of all risk checks for a trade."""
    checks: List[RiskCheck]
    overall_pass: bool
    failure_reasons: List[str]
    timestamp: str


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------

@dataclass
class PortfolioState:
    """Tracks open positions, bankroll, and daily P&L."""
    initial_bankroll: float
    current_bankroll: float
    peak_bankroll: float
    open_positions: int
    daily_pnl: float
    daily_api_cost: float
    daily_date: str  # ISO date string — reset counters when date changes
    total_trades: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioState":
        return cls(**d)

    @classmethod
    def default(cls, bankroll: float) -> "PortfolioState":
        return cls(
            initial_bankroll=bankroll,
            current_bankroll=bankroll,
            peak_bankroll=bankroll,
            open_positions=0,
            daily_pnl=0.0,
            daily_api_cost=0.0,
            daily_date=date.today().isoformat(),
            total_trades=0,
        )


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """Validates trades against 10 deterministic risk rules."""

    STATE_FILE = "portfolio_state.json"

    def __init__(self, settings: Optional[dict] = None):
        s = settings or load_settings()
        self.risk_cfg = s.get("risk", {})
        self.pred_cfg = s.get("prediction", {})
        self.bankroll_cfg = s.get("bankroll", {})

        # Thresholds
        self.min_edge = self.pred_cfg.get("min_edge", 0.04)
        self.min_confidence = self.pred_cfg.get("min_confidence", 0.65)
        self.max_position_pct = self.risk_cfg.get("max_position_pct", 0.05)
        self.max_concurrent = self.risk_cfg.get("max_concurrent_positions", 15)
        self.max_daily_loss_pct = self.risk_cfg.get("max_daily_loss_pct", 0.15)
        self.max_drawdown_pct = self.risk_cfg.get("max_drawdown_pct", 0.08)
        self.max_slippage_pct = self.risk_cfg.get("max_slippage_pct", 0.02)
        self.max_daily_api_cost = self.risk_cfg.get("max_daily_api_cost", 50.0)
        self.var_confidence = self.risk_cfg.get("var_confidence", 0.95)
        self.max_var_pct = self.risk_cfg.get("max_var_pct", 0.10)
        self.max_total_exposure_pct = self.risk_cfg.get("max_total_exposure_pct", 0.40)

        initial_bankroll = self.bankroll_cfg.get("initial", 500.0)

        # Load or create portfolio state
        self.state = self._load_state(initial_bankroll)

    # ------------------------------------------------------------------
    # Portfolio state persistence
    # ------------------------------------------------------------------

    def _load_state(self, initial_bankroll: float) -> PortfolioState:
        """Load portfolio state from disk, or create default."""
        fp = TRADES_DIR / self.STATE_FILE
        if fp.exists():
            try:
                with open(fp, "r") as f:
                    data = json.load(f)
                state = PortfolioState.from_dict(data)
                # Reset daily counters if date changed
                if state.daily_date != date.today().isoformat():
                    state.daily_pnl = 0.0
                    state.daily_api_cost = 0.0
                    state.daily_date = date.today().isoformat()
                    logger.info("Daily counters reset for new day")
                return state
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning("Failed to load portfolio state: %s — using defaults", exc)

        return PortfolioState.default(initial_bankroll)

    def save_state(self) -> Path:
        """Persist portfolio state to disk."""
        fp = TRADES_DIR / self.STATE_FILE
        TRADES_DIR.mkdir(parents=True, exist_ok=True)
        with open(fp, "w") as f:
            json.dump(self.state.to_dict(), f, indent=2)
        return fp

    def record_trade(self, position_size: float, pnl: float = 0.0, api_cost: float = 0.0):
        """Update portfolio state after a trade."""
        self.state.open_positions += 1
        self.state.total_trades += 1
        self.state.current_bankroll -= position_size  # Reserve for position
        self.state.daily_pnl += pnl
        self.state.daily_api_cost += api_cost
        if self.state.current_bankroll > self.state.peak_bankroll:
            self.state.peak_bankroll = self.state.current_bankroll
        self.save_state()

    def close_position(self, position_size: float, pnl: float):
        """Update portfolio state when a position closes."""
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.current_bankroll += position_size + pnl
        self.state.daily_pnl += pnl
        if self.state.current_bankroll > self.state.peak_bankroll:
            self.state.peak_bankroll = self.state.current_bankroll
        self.save_state()

    # ------------------------------------------------------------------
    # Open position helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_open_positions() -> List[Dict[str, Any]]:
        """Load all open trade files from TRADES_DIR."""
        positions = []
        for fp in TRADES_DIR.glob("trade_*.json"):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    trade = json.load(f)
                if trade.get("status") == "open":
                    positions.append(trade)
            except (json.JSONDecodeError, OSError):
                continue
        return positions

    def _compute_total_exposure(self, open_positions: List[Dict[str, Any]]) -> float:
        """Sum of position sizes across all open trades (USD)."""
        return sum(float(p.get("position_size_usd", 0)) for p in open_positions)

    def _compute_portfolio_var(
        self, open_positions: List[Dict[str, Any]], new_position_size: float, new_entry_price: float
    ) -> float:
        """
        Compute portfolio Value at Risk at 95% confidence.

        Each binary position has:
          - win payoff: (1 - entry_price) * contracts  →  position_size * (1 - entry) / entry  [for buy_yes]
          - loss payoff: -position_size
          - P(win) = entry_price (market-implied)

        For a portfolio of independent binary bets:
          μ = Σ E[pnl_i]
          σ² = Σ Var(pnl_i)
          VaR_95 = -(μ - 1.645 * σ)   [loss expressed as positive number]

        This is a parametric normal approximation — conservative enough for
        a small number of positions, and exactly what the design doc formula implies.
        """
        all_positions = []

        # Existing open positions
        for p in open_positions:
            entry = float(p.get("entry_price", 0.5))
            size = float(p.get("position_size_usd", 0))
            direction = p.get("direction", "buy_yes")
            all_positions.append((entry, size, direction))

        # Proposed new position
        if new_position_size > 0:
            all_positions.append((new_entry_price, new_position_size, "buy_yes"))

        if not all_positions:
            return 0.0

        total_mean = 0.0
        total_var = 0.0

        for entry, size, direction in all_positions:
            if direction == "buy_yes":
                # Win: gain = size * (1 - entry) / entry, Loss: lose = -size
                p_win = entry  # market-implied probability
                gain = size * (1 - entry) / max(entry, 0.01)
                loss = -size
            else:
                # buy_no: Win if outcome is NO
                p_win = 1 - entry
                gain = size * entry / max(1 - entry, 0.01)
                loss = -size

            mean_i = p_win * gain + (1 - p_win) * loss
            var_i = p_win * (gain - mean_i) ** 2 + (1 - p_win) * (loss - mean_i) ** 2

            total_mean += mean_i
            total_var += var_i

        total_std = math.sqrt(total_var) if total_var > 0 else 0.0

        # VaR = -(μ - z * σ), where z = 1.645 for 95%
        # Positive number = potential loss
        z = 1.645  # 95% confidence
        var_95 = -(total_mean - z * total_std)

        return max(0.0, var_95)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_trade(self, signal: Dict[str, Any], position_size_usd: float = 0.0) -> RiskValidation:
        """
        Run all 10 risk checks on a trade signal.

        Args:
            signal: TradeSignal dict with ensemble_probability, edge,
                    confidence, direction, market_probability, signal_strength
            position_size_usd: Proposed position size from Kelly sizing

        Returns:
            RiskValidation with per-check results and overall verdict.
        """
        checks: List[RiskCheck] = []

        edge = abs(float(signal.get("edge", 0)))
        confidence = float(signal.get("confidence", 0))
        market_prob = float(signal.get("market_probability", 0.5))

        # Check 1: Minimum edge
        c1 = RiskCheck(
            name="min_edge",
            passed=edge >= self.min_edge,
            detail=f"Edge {edge:.4f} {'≥' if edge >= self.min_edge else '<'} threshold {self.min_edge}",
            threshold=self.min_edge,
            actual=edge,
        )
        checks.append(c1)

        # Check 2: Minimum confidence
        c2 = RiskCheck(
            name="min_confidence",
            passed=confidence >= self.min_confidence,
            detail=f"Confidence {confidence:.3f} {'≥' if confidence >= self.min_confidence else '<'} threshold {self.min_confidence}",
            threshold=self.min_confidence,
            actual=confidence,
        )
        checks.append(c2)

        # Check 3: Maximum position size as % of bankroll
        max_pos_usd = self.state.current_bankroll * self.max_position_pct
        pos_ok = position_size_usd <= max_pos_usd
        c3 = RiskCheck(
            name="max_position_pct",
            passed=pos_ok,
            detail=f"Position ${position_size_usd:.2f} {'≤' if pos_ok else '>'} max ${max_pos_usd:.2f} ({self.max_position_pct:.0%} of ${self.state.current_bankroll:.2f})",
            threshold=self.max_position_pct,
            actual=position_size_usd / max(self.state.current_bankroll, 0.01),
        )
        checks.append(c3)

        # Check 4: Maximum concurrent positions
        conc_ok = self.state.open_positions < self.max_concurrent
        c4 = RiskCheck(
            name="max_concurrent_positions",
            passed=conc_ok,
            detail=f"Open positions {self.state.open_positions} {'<' if conc_ok else '≥'} max {self.max_concurrent}",
            threshold=self.max_concurrent,
            actual=self.state.open_positions,
        )
        checks.append(c4)

        # Check 5: Maximum daily loss
        daily_loss_limit = self.state.initial_bankroll * self.max_daily_loss_pct
        daily_loss_ok = abs(self.state.daily_pnl) < daily_loss_limit or self.state.daily_pnl >= 0
        c5 = RiskCheck(
            name="max_daily_loss",
            passed=daily_loss_ok,
            detail=f"Daily P&L ${self.state.daily_pnl:.2f}, loss limit -${daily_loss_limit:.2f}",
            threshold=self.max_daily_loss_pct,
            actual=self.state.daily_pnl / max(self.state.initial_bankroll, 0.01),
        )
        checks.append(c5)

        # Check 6: Maximum drawdown from peak
        drawdown = 0.0
        if self.state.peak_bankroll > 0:
            drawdown = (self.state.peak_bankroll - self.state.current_bankroll) / self.state.peak_bankroll
        dd_ok = drawdown <= self.max_drawdown_pct
        c6 = RiskCheck(
            name="max_drawdown",
            passed=dd_ok,
            detail=f"Drawdown {drawdown:.2%} {'≤' if dd_ok else '>'} max {self.max_drawdown_pct:.2%}",
            threshold=self.max_drawdown_pct,
            actual=drawdown,
        )
        checks.append(c6)

        # Check 7: Kill switch (STOP file)
        kill_switch_active = KILL_SWITCH_FILE.exists()
        c7 = RiskCheck(
            name="kill_switch",
            passed=not kill_switch_active,
            detail=f"Kill switch file {'EXISTS — TRADING HALTED' if kill_switch_active else 'not present'}",
            threshold="file absent",
            actual="present" if kill_switch_active else "absent",
        )
        checks.append(c7)

        # Check 8: Slippage check
        # Pre-trade: flag extreme prices (too close to 0 or 1)
        # Post-trade: check signal_price vs entry_price drift > threshold
        signal_price = float(signal.get("signal_price", market_prob))
        slippage_drift = abs(market_prob - signal_price)
        price_too_extreme = market_prob < self.max_slippage_pct or market_prob > (1 - self.max_slippage_pct)
        slippage_exceeded = slippage_drift > self.max_slippage_pct
        slippage_ok = not price_too_extreme and not slippage_exceeded

        if slippage_exceeded:
            slip_detail = f"Signal-to-fill drift {slippage_drift:.3f} > max {self.max_slippage_pct:.3f}"
        elif price_too_extreme:
            slip_detail = f"Market price {market_prob:.3f} outside valid range [{self.max_slippage_pct}, {1-self.max_slippage_pct}]"
        else:
            slip_detail = f"Market price {market_prob:.3f} in range, drift {slippage_drift:.4f} ≤ {self.max_slippage_pct}"

        c8 = RiskCheck(
            name="max_slippage",
            passed=slippage_ok,
            detail=slip_detail,
            threshold=self.max_slippage_pct,
            actual=max(slippage_drift, market_prob if price_too_extreme else 0),
        )
        checks.append(c8)

        # Check 9: Bankroll positive
        bankroll_ok = self.state.current_bankroll > 0
        c9 = RiskCheck(
            name="bankroll_positive",
            passed=bankroll_ok,
            detail=f"Bankroll ${self.state.current_bankroll:.2f} {'>' if bankroll_ok else '≤'} $0",
            threshold=0,
            actual=self.state.current_bankroll,
        )
        checks.append(c9)

        # Check 10: Daily API cost
        api_ok = self.state.daily_api_cost < self.max_daily_api_cost
        c10 = RiskCheck(
            name="max_daily_api_cost",
            passed=api_ok,
            detail=f"Daily API cost ${self.state.daily_api_cost:.2f} {'<' if api_ok else '≥'} max ${self.max_daily_api_cost:.2f}",
            threshold=self.max_daily_api_cost,
            actual=self.state.daily_api_cost,
        )
        checks.append(c10)

        # Check 11: VaR at 95% confidence
        open_positions = self._load_open_positions()
        var_95 = self._compute_portfolio_var(open_positions, position_size_usd, market_prob)
        var_limit = self.state.current_bankroll * self.max_var_pct
        var_ok = var_95 <= var_limit
        c11 = RiskCheck(
            name="var_95",
            passed=var_ok,
            detail=f"Portfolio VaR(95%) ${var_95:.2f} {'≤' if var_ok else '>'} limit ${var_limit:.2f} ({self.max_var_pct:.0%} of bankroll)",
            threshold=self.max_var_pct,
            actual=var_95 / max(self.state.current_bankroll, 0.01),
        )
        checks.append(c11)

        # Check 12: Total exposure (all open positions + proposed)
        current_exposure = self._compute_total_exposure(open_positions)
        total_exposure = current_exposure + position_size_usd
        exposure_limit = self.state.current_bankroll * self.max_total_exposure_pct
        exposure_ok = total_exposure <= exposure_limit
        c12 = RiskCheck(
            name="max_total_exposure",
            passed=exposure_ok,
            detail=f"Total exposure ${total_exposure:.2f} (open ${current_exposure:.2f} + new ${position_size_usd:.2f}) {'≤' if exposure_ok else '>'} limit ${exposure_limit:.2f} ({self.max_total_exposure_pct:.0%})",
            threshold=self.max_total_exposure_pct,
            actual=total_exposure / max(self.state.current_bankroll, 0.01),
        )
        checks.append(c12)

        # Aggregate
        overall = all(c.passed for c in checks)
        failures = [c.detail for c in checks if not c.passed]

        if not overall:
            logger.warning("Risk validation FAILED: %s", "; ".join(failures))
        else:
            logger.info("Risk validation PASSED (all %d checks)", len(checks))

        return RiskValidation(
            checks=checks,
            overall_pass=overall,
            failure_reasons=failures,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
