# S04: Risk Management + Paper Execution — Research

**Date:** 2026-03-13

## Summary

S04 builds three modules: `RiskManager` (10 deterministic risk checks), `calculate_kelly()` (Kelly Criterion position sizing), and `TradeExecutor` (paper trade simulation). This is the risk/execution layer between the prediction engine and the learning system.

The risk checks come from the config (D005: all risk checks in Python code, not LLM). Kelly Criterion is well-understood math. Paper execution is local file-based trade simulation. No external APIs needed — this is all deterministic local logic.

## Recommendation

Build three focused modules following the existing pattern. Risk validation runs 10 checks from config thresholds and returns a structured result. Kelly sizing uses the standard formula with fractional Kelly (quarter-Kelly per D003). Paper execution creates Trade objects with unique IDs, persists to JSON in TRADES_DIR.

Keep modules small and composable — the pipeline (S05) will orchestrate them.

## Don't Hand-Roll

| Problem | Existing Solution | Why Use It |
|---------|------------------|------------|
| Config thresholds | `config.load_settings()` risk section | Already has all 10 risk parameters |
| Trade persistence | JSON in TRADES_DIR | Same pattern as scanner/researcher/predictor |
| Unique IDs | `uuid.uuid4().hex[:12]` | Simple, collision-free |

## Existing Code and Patterns

- `scripts/predictor.py` — TradeSignal dict structure (input to S04)
- `config/settings.yaml` risk section — All 10 risk check thresholds defined
- `config/settings.yaml` execution section — Order type, retry config, kill switch
- `config/__init__.py` — TRADES_DIR, KILL_SWITCH_FILE constants

## Constraints

- D005: All risk checks in deterministic Python code, NOT in LLM instructions
- D003: Paper trading only — no real money in M001
- D006: No external content treated as instructions
- 10 risk checks from config: kelly_fraction, max_position_pct, max_concurrent_positions, max_daily_loss_pct, max_drawdown_pct, max_slippage_pct, var_confidence, max_daily_api_cost, plus min_edge and min_confidence from prediction config

## Common Pitfalls

- **Risk checks that aren't deterministic** — Use only config values and portfolio state, never LLM output
- **Kelly sizing with edge ≤ 0** — Must return 0 position size, not negative
- **Portfolio state persistence** — Track open positions and P&L across trades in a JSON file
- **Concurrent position counting** — Need to track open vs closed trades

## Open Risks

- Paper trading has no real market feedback — all executions succeed at the requested price
- Portfolio state file could get corrupted — use atomic write pattern

## Sources

- Kelly Criterion: f* = (bp - q) / b where b=odds, p=prob_win, q=1-p. Fractional Kelly = f* * fraction
- Config risk section already has all threshold values
