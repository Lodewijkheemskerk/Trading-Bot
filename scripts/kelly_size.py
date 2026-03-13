"""
Step 4b: KELLY CRITERION — Position Sizing

Calculates optimal position size using the Kelly Criterion with
fractional Kelly (quarter-Kelly by default per D003).

Kelly formula: f* = (b*p - q) / b
  where b = odds (payout ratio), p = prob of win, q = 1 - p

For prediction markets with binary outcomes:
  b = (1 / market_price) - 1   (for YES)
  b = (1 / (1 - market_price)) - 1   (for NO)
"""

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings

logger = logging.getLogger(__name__)


@dataclass
class KellyResult:
    """Position sizing result from Kelly Criterion."""
    kelly_fraction: float      # Raw Kelly fraction (can be > 1)
    adjusted_fraction: float   # After applying fractional Kelly
    position_size_usd: float   # Dollar amount to risk
    edge: float                # Expected edge
    expected_value: float      # Expected dollar return
    bankroll: float            # Bankroll used for calculation


def calculate_kelly(
    ensemble_probability: float,
    market_probability: float,
    direction: str,
    bankroll: float,
    kelly_multiplier: Optional[float] = None,
    max_position_pct: Optional[float] = None,
    settings: Optional[dict] = None,
) -> KellyResult:
    """
    Calculate Kelly Criterion position size.

    Args:
        ensemble_probability: Model's estimated probability (0-1)
        market_probability: Current market yes price (0-1)
        direction: "buy_yes" or "buy_no"
        bankroll: Current bankroll in USD
        kelly_multiplier: Fractional Kelly multiplier (default from config)
        max_position_pct: Max single position as fraction of bankroll (default from config)
        settings: Optional settings dict (loads from config if None)

    Returns:
        KellyResult with position size and metrics.
    """
    s = settings or load_settings()
    risk_cfg = s.get("risk", {})

    if kelly_multiplier is None:
        kelly_multiplier = risk_cfg.get("kelly_fraction", 0.25)
    if max_position_pct is None:
        max_position_pct = risk_cfg.get("max_position_pct", 0.05)

    # Determine effective probability and odds based on direction
    if direction == "buy_yes":
        p = ensemble_probability        # Probability event happens
        entry_price = market_probability  # Cost to buy YES
    else:  # buy_no
        p = 1 - ensemble_probability     # Probability event doesn't happen
        entry_price = 1 - market_probability  # Cost to buy NO

    q = 1 - p

    # Odds: payout per dollar risked
    # If you pay `entry_price` for a contract worth $1 on win:
    # profit = 1 - entry_price, so b = (1 - entry_price) / entry_price
    if entry_price <= 0 or entry_price >= 1:
        logger.warning("Invalid entry price %.4f — returning zero position", entry_price)
        return KellyResult(
            kelly_fraction=0.0,
            adjusted_fraction=0.0,
            position_size_usd=0.0,
            edge=0.0,
            expected_value=0.0,
            bankroll=bankroll,
        )

    b = (1 - entry_price) / entry_price

    # Kelly formula: f* = (b*p - q) / b
    kelly_raw = (b * p - q) / b

    # Negative Kelly means negative expected value — don't bet
    if kelly_raw <= 0:
        logger.info("Kelly fraction ≤ 0 (%.4f) — no bet", kelly_raw)
        return KellyResult(
            kelly_fraction=kelly_raw,
            adjusted_fraction=0.0,
            position_size_usd=0.0,
            edge=ensemble_probability - market_probability if direction == "buy_yes" else (1 - ensemble_probability) - (1 - market_probability),
            expected_value=0.0,
            bankroll=bankroll,
        )

    # Apply fractional Kelly
    adjusted = kelly_raw * kelly_multiplier

    # Position size in USD (capped by max_position_pct)
    max_size = bankroll * max_position_pct
    position_usd = min(adjusted * bankroll, max_size)
    position_usd = max(0, position_usd)  # Floor at 0

    # Edge (in probability terms)
    edge = ensemble_probability - market_probability

    # Expected value
    expected_value = position_usd * kelly_raw  # EV per unit * units

    logger.info(
        "Kelly: raw=%.4f adjusted=%.4f size=$%.2f (max=$%.2f) edge=%+.4f",
        kelly_raw, adjusted, position_usd, max_size, edge,
    )

    return KellyResult(
        kelly_fraction=round(kelly_raw, 6),
        adjusted_fraction=round(adjusted, 6),
        position_size_usd=round(position_usd, 2),
        edge=round(edge, 4),
        expected_value=round(expected_value, 4),
        bankroll=bankroll,
    )
