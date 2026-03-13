---
id: S04
parent: M001
milestone: M001
provides:
  - RiskManager class with 10 deterministic risk checks (D005)
  - RiskValidation with per-check pass/fail and structured failure reasons
  - calculate_kelly() with quarter-Kelly and max position cap
  - TradeExecutor with paper trade simulation
  - Trade dataclass matching S04→S05 boundary contract
  - Portfolio state tracking with JSON persistence
requires:
  - slice: S03
    provides: TradeSignal dicts (ensemble_probability, market_probability, edge, direction, signal_strength, confidence), prediction snapshots in data/predictions/
affects:
  - S05
key_files:
  - scripts/validate_risk.py
  - scripts/kelly_size.py
  - scripts/executor.py
  - tests/test_risk.py
key_decisions:
  - All 10 risk checks use config thresholds only, no LLM output (D005)
  - Kelly formula: f*=(bp-q)/b with configurable fractional multiplier (quarter-Kelly default)
  - Blocked trades recorded with status="blocked" for post-hoc analysis
  - Portfolio state persists to JSON with daily counter auto-reset
  - Slippage check uses price extremity as proxy (paper trading has no real slippage)
patterns_established:
  - 10-check risk validation with structured RiskCheck results
  - TradeExecutor orchestrates Kelly→risk→execute flow
  - executor.py follows the established CLI pattern (argparse, table output, JSON snapshots)
observability_surfaces:
  - INFO/WARNING logs for risk validation pass/fail with per-check details
  - INFO log for Kelly sizing with raw/adjusted fraction and position size
  - INFO log for executed trades, WARNING for blocked trades
  - JSON in data/trades/: execution snapshots, individual trades, portfolio state
drill_down_paths:
  - .gsd/milestones/M001/slices/S04/tasks/T01-SUMMARY.md
  - .gsd/milestones/M001/slices/S04/tasks/T02-SUMMARY.md
duration: 40m
verification_result: passed
completed_at: 2026-03-13
---

# S04: Risk Management + Paper Execution

**10 deterministic risk checks, Kelly Criterion position sizing, and paper trade simulation — signal→risk→kelly→execute pipeline with structured pass/fail reporting and JSON persistence.**

## What Happened

Built three modules in two tasks. T01 created `scripts/validate_risk.py` with `RiskManager` implementing all 10 risk checks from config thresholds (min_edge, min_confidence, max_position_pct, max_concurrent, daily_loss, drawdown, kill_switch, slippage, bankroll_positive, api_cost), plus `scripts/kelly_size.py` with Kelly Criterion position sizing. T02 built `scripts/executor.py` with `TradeExecutor` that orchestrates the full execution flow and a CLI with risk check tables and trade output.

All risk checks are deterministic and use only config values and portfolio state (D005). Kelly sizing handles negative edge, zero bankroll, extreme prices, and both buy_yes/buy_no directions. Portfolio state persists to JSON with daily counter auto-reset. 44 unit tests cover all check types, Kelly edge cases, executor flow, and contract fields.

## Verification

1. **`python -c "from scripts.validate_risk import RiskManager; ...print('ok')"`** — clean imports ✅
2. **`python tests/test_risk.py -v`** — 44 tests pass ✅
3. **`python scripts/executor.py --dry-run`** — 10 risk checks displayed, blocked correctly ✅
4. **`python scripts/executor.py`** — full execution, JSON saved to data/trades/ ✅
5. **S05 contract fields in JSON** — trade_id, entry_price, position_size_usd, status, pnl, risk_passed all present ✅
6. **Risk determinism** — blocked trade when confidence 0.40 < 0.65 threshold (9/10 other checks pass) ✅
7. **Kill switch** — STOP file correctly blocks all trading in unit tests ✅

## Deviations

None.

## Known Limitations

- Paper trading has no real market feedback — all executions succeed at requested price, no slippage simulation.
- Portfolio state file is not atomic-write — could theoretically corrupt under concurrent access (not a concern for single-threaded CLI in M001).
- Drawdown check uses peak bankroll from state file — requires accurate state tracking to be meaningful.

## Follow-ups

None. S05 (Compound Learning + Full Pipeline) consumes Trade dicts as designed.

## Files Created/Modified

- `scripts/validate_risk.py` — RiskManager with 10 risk checks, PortfolioState with JSON persistence
- `scripts/kelly_size.py` — Kelly Criterion position sizing with quarter-Kelly
- `scripts/executor.py` — TradeExecutor with paper trade simulation, CLI with risk check and trade tables
- `tests/test_risk.py` — 44 unit tests for risk checks, Kelly sizing, executor flow, and contracts

## Forward Intelligence

### What the next slice should know
- `Trade` is consumed as a dict (via `dataclasses.asdict()`). S05 should expect: `trade_id` (str), `entry_price` (float), `exit_price` (not set yet — S05 must handle resolution), `pnl` (float, 0 while open), `model_probability` (float), `direction` ("buy_yes"/"buy_no"), `signal_strength` (float), `status` ("open"/"blocked"/"no_signal"), `risk_passed` (bool), `risk_failures` (list of strings).
- Execution snapshots at `data/trades/execution_YYYYMMDD_HHMMSS.json` with structure `{timestamp, total_signals, executed, blocked, trades: [...]}`.
- Portfolio state at `data/trades/portfolio_state.json` — shared mutable state between executor and risk manager.
- `RiskManager` instance holds portfolio state — S05 pipeline should use a single shared instance.

### What's fragile
- Portfolio state file (`portfolio_state.json`) accumulates between runs. If running executor repeatedly during testing, open_positions and bankroll drift. Reset by deleting the file.
- Kill switch check uses `config.KILL_SWITCH_FILE` path — ensure it resolves to project root.

### Authoritative diagnostics
- `python tests/test_risk.py -v` — fast (0.015s), tests all 10 risk checks and Kelly sizing
- `python scripts/executor.py --dry-run` — shows risk check table without side effects
- `data/trades/portfolio_state.json` — current portfolio state

### What assumptions changed
- No assumptions changed. All 10 risk checks work as designed. Kelly sizing correctly handles edge cases.
