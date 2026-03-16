"""
Orderbook Depth Check — Verifies sufficient liquidity before trading.

Fetches the orderbook from Kalshi and checks that the resting depth
at or better than our limit price can absorb the trade size.

Used as risk check #13 in validate_risk.py and as a pre-trade gate
in the executor.

Kalshi orderbook format:
  {"orderbook_fp": {
      "yes_dollars": [["0.55", "120"], ["0.54", "80"], ...],
      "no_dollars":  [["0.42", "90"],  ["0.41", "50"], ...],
  }}
  Each level: [price_dollars_str, quantity_str]
  YES is sorted descending (best ask first).
  NO  is sorted descending (best ask first).
"""

import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class DepthCheck:
    """Result of an orderbook depth check."""
    ticker: str
    side: str                      # "yes" or "no"
    limit_price: float             # Our intended limit price
    contracts_wanted: int          # How many contracts we want to buy
    depth_at_price: int            # Resting quantity at or better than limit
    total_levels: int              # Total orderbook levels on this side
    best_price: Optional[float]    # Best (tightest) price available
    spread: Optional[float]        # Yes best ask - No best ask complement
    sufficient: bool               # Can the book absorb our order?
    reason: str                    # Human-readable explanation


def parse_orderbook(raw: Dict[str, Any]) -> Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]:
    """
    Parse Kalshi orderbook response into typed level lists.

    Returns (yes_levels, no_levels) where each level is (price_float, quantity_int).
    Levels are sorted best-price-first (highest bid / lowest ask).
    """
    ob = raw.get("orderbook_fp", raw.get("orderbook", {}))

    def parse_side(levels_raw):
        levels = []
        for entry in (levels_raw or []):
            try:
                price = float(entry[0])
                qty = int(float(entry[1]))
                levels.append((price, qty))
            except (IndexError, ValueError, TypeError):
                continue
        return levels

    yes_levels = parse_side(ob.get("yes_dollars", []))
    no_levels = parse_side(ob.get("no_dollars", []))
    return yes_levels, no_levels


def check_depth(
    orderbook_raw: Dict[str, Any],
    ticker: str,
    side: str,
    limit_price: float,
    contracts_wanted: int,
    min_depth_ratio: float = 2.0,
) -> DepthCheck:
    """
    Check whether the orderbook has enough depth to absorb our trade.

    Args:
        orderbook_raw: Raw Kalshi API response from get_orderbook()
        ticker: Market ticker
        side: "yes" or "no"
        limit_price: Our intended limit price (dollars, 0.01–0.99)
        contracts_wanted: Number of contracts we want
        min_depth_ratio: Required ratio of resting depth to our order size.
                         2.0 means the book must have 2x our contracts
                         at or better than our price. Prevents us from
                         eating too much of the available liquidity.

    Returns:
        DepthCheck with sufficient=True/False and details
    """
    yes_levels, no_levels = parse_orderbook(orderbook_raw)
    levels = yes_levels if side == "yes" else no_levels
    other_levels = no_levels if side == "yes" else yes_levels

    total_levels = len(levels)

    # Best price
    best_price = levels[0][0] if levels else None

    # Spread: difference between YES best and (1 - NO best)
    spread = None
    if yes_levels and no_levels:
        yes_best = yes_levels[0][0]
        no_best = no_levels[0][0]
        spread = round(1.0 - yes_best - no_best, 4)

    # Empty orderbook
    if not levels:
        return DepthCheck(
            ticker=ticker, side=side, limit_price=limit_price,
            contracts_wanted=contracts_wanted,
            depth_at_price=0, total_levels=0,
            best_price=None, spread=spread,
            sufficient=False,
            reason="Empty orderbook — no resting orders",
        )

    # Sum quantity at or better than our limit price
    # For buying YES: we want prices ≤ our limit (we're the buyer, book shows asks)
    # For buying NO: same — prices ≤ our limit
    depth_at_price = 0
    for price, qty in levels:
        if price <= limit_price:
            depth_at_price += qty

    # Check sufficiency
    required = int(contracts_wanted * min_depth_ratio)

    if depth_at_price == 0:
        return DepthCheck(
            ticker=ticker, side=side, limit_price=limit_price,
            contracts_wanted=contracts_wanted,
            depth_at_price=0, total_levels=total_levels,
            best_price=best_price, spread=spread,
            sufficient=False,
            reason=f"No resting depth at ${limit_price:.2f} or better",
        )

    if depth_at_price < required:
        return DepthCheck(
            ticker=ticker, side=side, limit_price=limit_price,
            contracts_wanted=contracts_wanted,
            depth_at_price=depth_at_price, total_levels=total_levels,
            best_price=best_price, spread=spread,
            sufficient=False,
            reason=f"Thin book: {depth_at_price} resting vs {required} required ({min_depth_ratio}x our {contracts_wanted})",
        )

    return DepthCheck(
        ticker=ticker, side=side, limit_price=limit_price,
        contracts_wanted=contracts_wanted,
        depth_at_price=depth_at_price, total_levels=total_levels,
        best_price=best_price, spread=spread,
        sufficient=True,
        reason=f"OK: {depth_at_price} resting ≥ {required} required at ${limit_price:.2f}",
    )


def get_spread(orderbook_raw: Dict[str, Any]) -> Optional[float]:
    """
    Get the spread from an orderbook.

    Returns the gap between YES best ask and (1 - NO best ask), or None.
    Positive spread = normal. Negative = crossed book.
    """
    yes_levels, no_levels = parse_orderbook(orderbook_raw)
    if not yes_levels or not no_levels:
        return None
    return round(1.0 - yes_levels[0][0] - no_levels[0][0], 4)


def get_midpoint(orderbook_raw: Dict[str, Any]) -> Optional[float]:
    """
    Get the YES midpoint price from the orderbook.

    Returns (yes_best + (1 - no_best)) / 2, or None if no data.
    """
    yes_levels, no_levels = parse_orderbook(orderbook_raw)
    if not yes_levels or not no_levels:
        if yes_levels:
            return yes_levels[0][0]
        return None
    yes_best = yes_levels[0][0]
    no_implied_yes = 1.0 - no_levels[0][0]
    return round((yes_best + no_implied_yes) / 2, 4)
