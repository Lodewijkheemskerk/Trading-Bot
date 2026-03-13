# S04: Risk Management + Paper Execution — UAT

**Milestone:** M001
**Written:** 2026-03-13

## UAT Type

- UAT mode: artifact-driven
- Why this mode is sufficient: All logic is deterministic — 44 unit tests cover every risk check pass/fail, Kelly edge case, and executor flow. No external APIs, no real money, no user-facing UI.

## Preconditions

- Python 3.11+ with `requests` and `pyyaml` installed
- A prediction snapshot exists in `data/predictions/` (run `python scripts/predictor.py --heuristic-only` first if missing)
- No `STOP` file in project root (would trigger kill switch)

## Smoke Test

Run `python scripts/executor.py --dry-run` — should show 10 risk checks for the first prediction signal.

## Test Cases

### 1. Unit tests pass

1. Run `python tests/test_risk.py -v`
2. **Expected:** 44 tests pass, 0 failures

### 2. Risk checks display correctly

1. Run `python scripts/executor.py --dry-run`
2. **Expected:** Table with 10 risk checks showing name, PASS/FAIL, and detail. Overall verdict shown.

### 3. Paper trade execution

1. Delete `data/trades/portfolio_state.json` if it exists (clean state)
2. Run `python scripts/executor.py`
3. **Expected:** Trade table shows trades with status (open/blocked/no_signal), JSON saved to data/trades/

### 4. Kill switch blocks trading

1. Create a file named `STOP` in the project root
2. Run `python scripts/executor.py --dry-run`
3. **Expected:** kill_switch check shows FAIL
4. Delete the `STOP` file

### 5. S05 contract fields present

1. Open latest `data/trades/execution_*.json`
2. Inspect a trade in the `trades` array
3. **Expected:** Contains trade_id, entry_price, position_size_usd, status, pnl, risk_passed

## Edge Cases

### Zero bankroll

1. Edit `data/trades/portfolio_state.json` to set current_bankroll=0
2. Run `python scripts/executor.py --dry-run`
3. **Expected:** bankroll_positive check fails, max_position_pct check fails, max_drawdown check fails

## Failure Signals

- Any unit test failure in `test_risk.py`
- Risk checks not returning exactly 10 checks
- Kill switch not blocking when STOP file exists
- Missing S05 contract fields in trade JSON

## Not Proven By This UAT

- Real market slippage handling (paper trading has zero slippage)
- Trade resolution and P&L calculation (deferred to S05)
- Portfolio state accuracy over many trades (single-run verification only)

## Notes for Tester

- Portfolio state accumulates between runs. Delete `data/trades/portfolio_state.json` for a clean start.
- Current predictions are heuristic-only with low confidence — most trades will be blocked by the min_confidence check. This is correct behavior.
