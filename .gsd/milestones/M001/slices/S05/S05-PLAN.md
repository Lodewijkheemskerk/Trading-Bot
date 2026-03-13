# S05: Compound Learning + Full Pipeline

**Goal:** A `Compounder` class that analyzes trade outcomes and tracks performance metrics, plus a `TradingPipeline` that orchestrates all 5 steps (Scan→Research→Predict→Execute→Compound) with `--mode once`, `--mode loop --interval 15`, `--status`, and kill switch via `STOP` file.
**Demo:** `python scripts/pipeline.py --mode once` runs the complete pipeline end-to-end. `python scripts/pipeline.py --status` shows performance metrics. Creating a `STOP` file halts trading.

## Must-Haves

- `Compounder` class with `analyze_trade()`, `get_performance_report()`, `nightly_review()`
- Performance metrics: win rate, total P&L, Sharpe ratio, profit factor, Brier score, max drawdown
- Failure log appending to `references/failure_log.md`
- `TradingPipeline` with `run_once()`, `run_loop()`, `activate_kill_switch()`
- `--mode once` runs all 5 pipeline steps end-to-end
- `--mode loop --interval 15` runs autonomously on a timer
- `--status` shows performance metrics from trade history
- STOP file creation halts all trading immediately
- Pipeline handles errors gracefully (per-step catch, continues to next cycle in loop)

## Proof Level

- This slice proves: final-assembly (all 5 steps integrated end-to-end)
- Real runtime required: yes (uses live Kalshi + news APIs from S01/S02)
- Human/UAT required: no

## Verification

- `python scripts/pipeline.py --mode once` completes all 5 steps without crashing
- `python scripts/pipeline.py --status` shows performance metrics
- Kill switch: create STOP file → pipeline halts immediately
- `python -c "from scripts.compounder import Compounder; from scripts.pipeline import TradingPipeline; print('ok')"` clean imports
- `python tests/test_pipeline.py` passes unit tests for compounder and pipeline logic

## Observability / Diagnostics

- Runtime signals: INFO logging per pipeline step completion, WARNING for step failures
- Inspection surfaces: `--status` CLI, performance metrics in JSON, failure_log.md
- Failure visibility: per-step error logging, failure_log.md append-only, pipeline continues on non-fatal errors
- Redaction constraints: ANTHROPIC_API_KEY never logged

## Integration Closure

- Upstream surfaces consumed: all S01-S04 modules, config constants (KILL_SWITCH_FILE, all DIR constants)
- New wiring introduced: pipeline.py orchestrates scanner→researcher→predictor→executor→compounder
- What remains: nothing — this completes M001

## Tasks

- [x] **T01: Build Compounder and TradingPipeline with full end-to-end orchestration** `est:1h30m`
  - Why: Final assembly — integrates all 5 steps into a runnable pipeline with learning
  - Files: `scripts/compounder.py`, `scripts/pipeline.py`, `tests/test_pipeline.py`
  - Do: Build `Compounder` with `analyze_trade()` (evaluate outcome, update stats), `get_performance_report()` (win rate, P&L, Sharpe, profit factor, Brier, drawdown), `nightly_review()` (summarize day's trades, append to failure_log.md). Build `TradingPipeline` with `run_once()` (scan→research→predict→execute→compound), `run_loop(interval)` (timer-based loop with STOP file check), `activate_kill_switch()`, `--mode once/loop`, `--status`, `--interval`. Pipeline handles per-step errors gracefully. Reset portfolio state file for clean start. Unit tests for compounder metrics and pipeline integration.
  - Verify: `python scripts/pipeline.py --mode once` completes, `--status` shows metrics, STOP file halts loop
  - Done when: All M001 success criteria met — complete pipeline, risk validation, status metrics, autonomous loop, kill switch

## Files Likely Touched

- `scripts/compounder.py`
- `scripts/pipeline.py`
- `tests/test_pipeline.py`
- `references/failure_log.md`
