---
id: T02
parent: S04
milestone: M001
provides:
  - TradeExecutor class with paper trade simulation
  - Trade dataclass matching S04→S05 boundary contract
  - CLI with --dry-run and --top flags, risk check table, trade table
  - Execution snapshot saving
key_files:
  - scripts/executor.py
  - tests/test_risk.py
key_decisions:
  - Blocked trades recorded with status="blocked" (not silently skipped)
  - Non-tradeable signals recorded with status="no_signal"
  - Portfolio state updated only for risk-passing trades
  - Risk check table shown for first signal as demo output
patterns_established:
  - executor.py follows scanner/researcher/predictor CLI pattern
  - Trade records include risk_failures list for post-hoc analysis
  - Execution snapshots include executed/blocked counts
observability_surfaces:
  - INFO log for executed trades with trade_id, direction, size, edge
  - WARNING log for blocked trades with failure reasons
  - JSON execution snapshots in data/trades/
  - Portfolio state summary in CLI output
duration: 15m
verification_result: passed
completed_at: 2026-03-13
blocker_discovered: false
---

# T02: Build TradeExecutor with paper trade simulation and CLI

**Full signal→risk→kelly→paper_trade pipeline with CLI showing 10 risk check results and trade table, plus JSON persistence.**

## What Happened

Built `scripts/executor.py` with `TradeExecutor` class that orchestrates the full execution flow: load prediction snapshot → for each signal, calculate Kelly position size → run 10 risk checks → create Trade object (open if passed, blocked if failed) → update portfolio state → persist to JSON. Added CLI with `--dry-run` (show risk checks only) and `--top` flags.

Verified against live data: the executor correctly blocks the current prediction (confidence 0.40 < 0.65 threshold) while correctly passing the other 9 checks. Risk check table renders cleanly. Added 6 executor tests to test_risk.py bringing total to 44 tests.

## Verification

- `python scripts/executor.py --dry-run` — shows 10 risk checks, blocks correctly ✅
- `python scripts/executor.py` — full execution, trade recorded as no_signal, JSON saved ✅
- `python tests/test_risk.py -v` — 44 tests pass ✅
- S05 contract fields in JSON: trade_id, entry_price, position_size_usd, status, pnl, risk_passed all present ✅
- Portfolio state updated correctly after trades ✅

## Diagnostics

- `python scripts/executor.py --dry-run` to see risk checks without executing
- `data/trades/execution_*.json` for execution snapshots
- `data/trades/portfolio_state.json` for portfolio state
- `data/trades/trade_*.json` for individual trade records

## Deviations

None.

## Known Issues

None.

## Files Created/Modified

- `scripts/executor.py` — TradeExecutor with paper trade simulation, CLI with risk check table and trade table
- `tests/test_risk.py` — Added 6 executor tests (44 total)
