# S03: Prediction Engine — UAT

**Milestone:** M001
**Written:** 2026-03-13

## UAT Type

- UAT mode: mixed
- Why this mode is sufficient: Core logic verified by 33 unit tests (artifact-driven), Claude API integration verified by live call attempt with graceful fallback (live-runtime). No human-experience testing needed — output is developer-facing CLI and JSON.

## Preconditions

- Python 3.11+ with `requests` and `anthropic` installed
- A recent research snapshot exists in `data/research_briefs/` (run `python scripts/researcher.py` first if missing)
- ANTHROPIC_API_KEY in `.env` (optional — heuristic-only mode works without it)

## Smoke Test

Run `python scripts/predictor.py --heuristic-only --top 1` — should print a table with one market's prediction data and save a JSON snapshot to `data/predictions/`.

## Test Cases

### 1. Unit tests pass

1. Run `python tests/test_predictor.py -v`
2. **Expected:** 33 tests pass, 0 failures, completes in < 1 second

### 2. Heuristic-only mode

1. Run `python scripts/predictor.py --heuristic-only --top 3`
2. **Expected:** Table with predictions for 3 markets, 2 model predictions each (bull + bear), JSON saved

### 3. Full ensemble mode (requires API credits)

1. Run `python scripts/predictor.py --top 1`
2. **Expected:** If API key has credits: 3 model predictions (claude + bull + bear). If no credits: graceful fallback to heuristic-only with WARNING log.

### 4. S04 contract fields present

1. Open the latest `data/predictions/predictions_*.json`
2. Inspect a signal object in the `signals` array
3. **Expected:** Contains `ensemble_probability`, `market_probability`, `edge`, `direction`, `signal_strength`

### 5. Clean imports

1. Run `python -c "from scripts.predictor import PredictionEngine, TradeSignal, ModelPrediction; print('ok')"`
2. **Expected:** Prints "ok" with no errors

## Edge Cases

### No research snapshots

1. Remove or rename all files in `data/research_briefs/`
2. Run `python scripts/predictor.py --heuristic-only`
3. **Expected:** Helpful error message, no crash

### Extreme market prices

1. Create a brief with `current_yes_price: 0.01` or `0.99`
2. Run prediction engine
3. **Expected:** Valid TradeSignal with ensemble_probability clamped to [0.01, 0.99]

## Failure Signals

- Any unit test failure in `test_predictor.py`
- CLI crash or traceback during `python scripts/predictor.py`
- Missing S04 contract fields in output JSON
- Claude API error logged as ERROR (not WARNING) — would indicate broken error handling

## Not Proven By This UAT

- Claude AI prediction quality and calibration — requires funded API credits
- Prediction accuracy over time — no outcome data yet (S05 will track this)
- Performance under high API load — single-threaded sequential design

## Notes for Tester

- Most heuristic-only predictions will have small edges (< 0.04) and should_trade=False with default thresholds. This is expected — heuristics anchor close to market price.
- If Claude API has credits, predictions will be more interesting — Claude provides an independent estimate that can diverge significantly from market price.
- The `--heuristic-only` flag is the reliable offline testing mode.
