# S04: Risk Management + Paper Execution

**Goal:** A `RiskManager` that validates trades against 10 deterministic risk rules, a `calculate_kelly()` function for position sizing, and a `TradeExecutor` that simulates paper trades with local persistence. All consume `TradeSignal` dicts from S03.
**Demo:** `python scripts/executor.py` loads the latest prediction snapshot, runs all risk checks, sizes positions with Kelly, and executes paper trades. Prints a table of risk check results and trade details, saves trades to `data/trades/`.

## Must-Haves

- `RiskValidation` dataclass with pass/fail per check, overall verdict, and failure reasons
- 10 deterministic risk checks using config thresholds (D005)
- `KellyResult` dataclass with position_size_usd, edge, expected_value, kelly_fraction
- Kelly Criterion with quarter-Kelly fraction from config
- `Trade` dataclass matching S04→S05 boundary contract
- `TradeExecutor` with paper trade simulation (instant fill at requested price)
- Portfolio state tracking: open positions, daily P&L, drawdown
- JSON persistence for trades in TRADES_DIR
- CLI with table output for risk checks and trade results
- Unit tests for all 10 risk checks, Kelly sizing, and contract fields

## Proof Level

- This slice proves: contract (S04→S05 boundary) + operational (risk checks block bad trades)
- Real runtime required: no (all local deterministic logic)
- Human/UAT required: no

## Verification

- `python scripts/executor.py` completes without errors, prints risk checks and trade table, saves JSON
- `python -c "from scripts.validate_risk import RiskManager; from scripts.kelly_size import calculate_kelly; from scripts.executor import TradeExecutor, Trade; print('import ok')"` confirms clean imports
- `python tests/test_risk.py` passes unit tests for all 10 risk checks, Kelly sizing, and contract fields
- Risk checks deterministically block trades violating thresholds
- Output JSON contains all S05 contract fields: trade_id, entry_price, position_size_usd, status, pnl, risk_passed

## Observability / Diagnostics

- Runtime signals: logging at INFO (risk check results, trade execution) and WARNING (risk check failures, blocked trades)
- Inspection surfaces: JSON trades in `data/trades/`, portfolio state file, CLI table output
- Failure visibility: per-check pass/fail in RiskValidation, blocked trades logged with failure reasons
- Redaction constraints: none (no secrets in risk/trade data)

## Integration Closure

- Upstream surfaces consumed: `config.load_settings()` risk section, `config.TRADES_DIR`, `config.KILL_SWITCH_FILE`, `TradeSignal` dicts from `data/predictions/`
- New wiring introduced: `executor.py` reads latest prediction snapshot, runs risk checks + Kelly sizing + paper execution, produces `Trade` dicts for S05 consumption
- What remains: S05 (pipeline + learning)

## Tasks

- [x] **T01: Build RiskManager with 10 risk checks and Kelly sizing** `est:1h`
  - Why: Core risk validation — all 10 checks must be deterministic and configurable
  - Files: `scripts/validate_risk.py`, `scripts/kelly_size.py`, `tests/test_risk.py`
  - Do: Implement `RiskValidation` dataclass (checks list, overall_pass, failure_reasons). Build `RiskManager` with portfolio state tracking (open_positions, daily_pnl, peak_bankroll, current_bankroll). Implement 10 risk checks: (1) min_edge, (2) min_confidence, (3) max_position_pct, (4) max_concurrent_positions, (5) max_daily_loss_pct, (6) max_drawdown_pct, (7) kill_switch_check, (8) max_slippage_pct check on price, (9) bankroll > 0, (10) max_daily_api_cost. Implement `calculate_kelly()` with quarter-Kelly and position sizing in USD. Write unit tests for each risk check, Kelly edge cases (negative edge, zero prob), and validation contract.
  - Verify: `python tests/test_risk.py` passes all tests, risk checks block correctly
  - Done when: All 10 risk checks work with config thresholds, Kelly sizing returns correct amounts, unit tests pass

- [x] **T02: Build TradeExecutor with paper trade simulation and CLI** `est:45m`
  - Why: Completes the execution loop — takes trade signals through risk + Kelly + paper execution
  - Files: `scripts/executor.py`, `tests/test_risk.py`
  - Do: Implement `Trade` dataclass matching S05 boundary contract. Build `TradeExecutor` that: loads prediction snapshot, runs `RiskManager.validate_trade()`, calculates Kelly position size, creates `Trade` objects for passing trades, persists to JSON in TRADES_DIR. Add CLI: argparse with table output of risk checks + trades, save timestamped JSON. Track portfolio state in `data/trades/portfolio_state.json`.
  - Verify: `python scripts/executor.py` completes, prints table, saves trades. Blocked trades show failure reasons. `python tests/test_risk.py` still passes with added executor tests.
  - Done when: Full signal→risk→kelly→paper_trade pipeline works, JSON trades saved, CLI produces formatted output

## Files Likely Touched

- `scripts/validate_risk.py`
- `scripts/kelly_size.py`
- `scripts/executor.py`
- `tests/test_risk.py`
