---
id: T01
parent: S04
milestone: M001
provides:
  - RiskManager class with 10 deterministic risk checks
  - RiskValidation, RiskCheck, PortfolioState dataclasses
  - calculate_kelly() with quarter-Kelly and position cap
  - KellyResult dataclass
key_files:
  - scripts/validate_risk.py
  - scripts/kelly_size.py
  - tests/test_risk.py
key_decisions:
  - All 10 risk checks use config thresholds only, no LLM output (D005)
  - Kelly uses standard formula f*=(bp-q)/b with fractional multiplier
  - Portfolio state persisted as JSON in TRADES_DIR, daily counters auto-reset on date change
  - Slippage check uses market price extremity as proxy (paper trading has no real slippage)
  - Daily loss check allows positive P&L regardless of magnitude
patterns_established:
  - RiskCheck dataclass with name/passed/detail/threshold/actual for structured pass/fail reporting
  - PortfolioState with JSON round-trip serialization
  - Kelly returns zero position for negative edge or invalid prices
observability_surfaces:
  - INFO log for risk validation pass
  - WARNING log for risk validation failures with per-check details
  - INFO log for Kelly sizing with raw/adjusted fraction and position size
duration: 25m
verification_result: passed
completed_at: 2026-03-13
blocker_discovered: false
---

# T01: Build RiskManager with 10 risk checks and Kelly sizing

**10 deterministic risk checks (min_edge, min_confidence, max_position, max_concurrent, daily_loss, drawdown, kill_switch, slippage, bankroll, api_cost) plus Kelly Criterion position sizing with quarter-Kelly and max position cap.**

## What Happened

Built `scripts/validate_risk.py` with `RiskManager` class implementing all 10 risk checks from config thresholds. Each check returns a `RiskCheck` dataclass with pass/fail, human-readable detail, threshold, and actual value. `PortfolioState` tracks open positions, bankroll, peak, daily P&L, and daily API cost with auto-reset on date change.

Built `scripts/kelly_size.py` with `calculate_kelly()` implementing the standard Kelly formula for binary prediction markets. Handles both buy_yes and buy_no directions, negative edge (returns zero), extreme prices, and position cap.

38 unit tests cover each risk check individually (pass and fail cases), overall validation, Kelly edge cases, portfolio state serialization, and contract fields.

## Verification

- `python tests/test_risk.py -v` — 38 tests pass (before executor tests added)
- Kill switch test creates/removes STOP file correctly
- Kelly handles negative edge, zero bankroll, extreme prices
- All 10 checks correctly use config thresholds

## Diagnostics

- `python tests/test_risk.py -v` for regression testing
- `data/trades/portfolio_state.json` for portfolio state inspection

## Deviations

None.

## Known Issues

None.

## Files Created/Modified

- `scripts/validate_risk.py` — RiskManager with 10 risk checks, PortfolioState with JSON persistence
- `scripts/kelly_size.py` — Kelly Criterion position sizing with quarter-Kelly
- `tests/test_risk.py` — 38 unit tests for risk checks and Kelly sizing
