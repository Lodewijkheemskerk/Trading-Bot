# S05: Compound Learning + Full Pipeline — UAT

**Milestone:** M001
**Written:** 2026-03-13

## UAT Type

- UAT mode: live-runtime
- Why this mode is sufficient: Final assembly slice — must verify the real pipeline runs end-to-end against live APIs. Unit tests verify component logic; this UAT verifies integration.

## Preconditions

- Python 3.11+ with `requests`, `pyyaml`, `anthropic` installed
- Internet access for Kalshi API, Google News RSS, Reddit
- No `STOP` file in project root

## Smoke Test

Run `python scripts/pipeline.py --mode once --heuristic-only` — should complete all 5 steps and print results.

## Test Cases

### 1. Full pipeline once

1. Run `python scripts/pipeline.py --mode once --heuristic-only`
2. **Expected:** All 5 steps show OK status, completes in < 30 seconds

### 2. Performance status

1. Run `python scripts/pipeline.py --status`
2. **Expected:** Performance report table with total trades, win rate, P&L, Sharpe, etc.

### 3. Kill switch halt

1. Run `python scripts/pipeline.py --kill`
2. Run `python scripts/pipeline.py --mode once`
3. **Expected:** Pipeline halts immediately with kill switch message
4. Run `python scripts/pipeline.py --resume`
5. **Expected:** Kill switch deactivated, STOP file removed

### 4. Unit tests pass

1. Run `python tests/test_pipeline.py -v`
2. **Expected:** 15 tests pass

### 5. All test suites pass

1. Run all four test files
2. **Expected:** 121 total tests pass (29 + 33 + 44 + 15)

## Edge Cases

### Pipeline with no prior data

1. Delete all files in `data/` subdirectories
2. Run `python scripts/pipeline.py --mode once --heuristic-only`
3. **Expected:** Scan creates new snapshot, pipeline completes from scratch

## Failure Signals

- Any pipeline step showing FAIL status
- Crash or traceback during `--mode once`
- Kill switch not halting the pipeline
- Performance report showing errors instead of metrics

## Not Proven By This UAT

- Trade resolution (closing open trades based on market outcomes)
- Long-running loop stability (would need hours of runtime)
- Claude AI prediction quality (requires funded API credits)

## Notes for Tester

- All signals currently show should_trade=False due to low heuristic confidence. This is expected — heuristics are conservative baseline models.
- To see trades execute, temporarily lower thresholds: set prediction.min_confidence to 0.30 in config/settings.yaml.
- Portfolio state persists between runs. Delete `data/trades/portfolio_state.json` for a clean start.
