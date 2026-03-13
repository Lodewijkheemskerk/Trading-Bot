# S03: Prediction Engine

**Goal:** A `PredictionEngine` class that takes a research brief dict, runs it through a Claude AI model and two heuristic models, produces an ensemble probability estimate with edge calculation, mispricing score, and a trade/no-trade signal as a `TradeSignal` dataclass.
**Demo:** `python scripts/predictor.py` loads the latest research snapshot, runs prediction on each brief, and prints a table with ensemble probability, edge, signal strength, and trade recommendation. Saves prediction results as JSON.

## Must-Haves

- `TradeSignal` and `ModelPrediction` dataclasses matching the S03→S04 boundary contract exactly
- Claude AI model: structured prompt with research brief → probability + reasoning, JSON response parsing with regex fallback
- Heuristic bull advocate model: anchors on market price + bullish sentiment signals, biases probability upward
- Heuristic bear advocate model: anchors on market price + bearish sentiment signals, biases probability downward
- Ensemble aggregation: confidence-weighted average of all model predictions using config weights
- Edge calculation: `ensemble_probability - market_probability` with direction (buy_yes/buy_no)
- Mispricing Z-score: `abs(edge) / baseline_std` with configurable baseline
- `should_trade` decision: edge > min_edge AND confidence > min_confidence
- Graceful fallback to heuristic-only when Claude API fails (key missing, errors, timeouts)
- `market_probability` passed through in TradeSignal (required by S04 contract)
- JSON persistence of prediction results
- CLI with table output following researcher.py pattern
- Unit tests for heuristics, ensemble, edge calculation, and contract fields

## Proof Level

- This slice proves: contract (S03→S04 boundary) + operational (Claude API integration)
- Real runtime required: yes (Claude API for primary model, heuristic-only fallback for offline)
- Human/UAT required: no

## Verification

- `python scripts/predictor.py` completes without errors, prints formatted table, saves JSON
- `python -c "from scripts.predictor import PredictionEngine, TradeSignal, ModelPrediction; print('import ok')"` confirms clean imports
- `python tests/test_predictor.py` passes unit tests for heuristics, ensemble, edge calculation, signal logic, and contract fields
- Output JSON contains all S04 contract fields: `ensemble_probability`, `market_probability`, `edge`, `direction`, `signal_strength`
- When ANTHROPIC_API_KEY is not set, predictor falls back to heuristic-only mode without crashing

## Observability / Diagnostics

- Runtime signals: logging at INFO (model predictions, ensemble result, trade decision) and WARNING (Claude API errors, fallback to heuristic-only)
- Inspection surfaces: JSON predictions in `data/predictions/`, CLI table output, per-model breakdown in prediction output
- Failure visibility: Claude API errors logged with response details, fallback mode clearly indicated in output
- Redaction constraints: ANTHROPIC_API_KEY never logged or persisted

## Integration Closure

- Upstream surfaces consumed: `config.load_settings()`, `config.DATA_DIR`, research snapshots from `data/research_briefs/`
- New wiring introduced: `predictor.py` reads latest research snapshot JSON, produces `TradeSignal` dicts for S04 consumption, new `PREDICTIONS_DIR` constant in config
- What remains: S04 (risk + execution), S05 (pipeline + learning)

## Tasks

- [x] **T01: Build PredictionEngine with Claude AI model, heuristic models, and ensemble** `est:1h30m`
  - Why: Core slice deliverable — the entire prediction pipeline from ResearchBrief to TradeSignal
  - Files: `scripts/predictor.py`, `tests/test_predictor.py`, `config/__init__.py`
  - Do: Implement `TradeSignal` and `ModelPrediction` dataclasses matching S04 boundary contract. Build `PredictionEngine` with three model methods: `_predict_claude()` using Anthropic SDK with structured prompt requiring JSON response (probability, confidence, reasoning), `_predict_heuristic_bull()` anchoring on market price + bullish gap, `_predict_heuristic_bear()` anchoring on market price + bearish gap. Ensemble aggregation with config weights, edge calculation (`ensemble_prob - market_prob`), direction logic (buy_yes if edge > 0, buy_no if < 0), mispricing Z-score (`abs(edge) / 0.10` as baseline std), signal_strength (abs(edge) * confidence), should_trade decision (edge > min_edge AND confidence > min_confidence). Graceful Claude fallback: if API key missing or call fails, skip Claude model and re-normalize heuristic weights. Add `PREDICTIONS_DIR` to `config/__init__.py`. Write unit tests for: heuristic models, ensemble with/without Claude, edge calculation, direction logic, should_trade thresholds, contract field validation. JSON persistence of predictions.
  - Verify: `python tests/test_predictor.py` passes all tests, `python -c "from scripts.predictor import PredictionEngine, TradeSignal, ModelPrediction"` works, heuristic-only fallback works without API key
  - Done when: `PredictionEngine.predict(research_brief_dict)` returns a valid `TradeSignal` with all S04 contract fields, unit tests pass, Claude fallback tested

- [x] **T02: Add CLI interface and verify end-to-end with Claude API** `est:30m`
  - Why: Makes the module runnable as a standalone script (demo requirement) and verifies real Claude API integration
  - Files: `scripts/predictor.py`
  - Do: Add `if __name__ == "__main__"` block following researcher.py pattern: load latest research snapshot, predict for each brief, print formatted table (market, ensemble prob, market prob, edge, direction, signal, trade?), save timestamped JSON snapshot. Collect ANTHROPIC_API_KEY via `secure_env_collect` if not set. Add `--heuristic-only` flag for offline mode. Handle Windows console encoding.
  - Verify: `python scripts/predictor.py --heuristic-only` works without API key, `python scripts/predictor.py` works with API key (Claude predictions visible in output), JSON saved to `data/predictions/`
  - Done when: CLI produces formatted output, JSON saved to disk, both Claude and heuristic-only modes work

## Files Likely Touched

- `scripts/predictor.py`
- `tests/test_predictor.py`
- `config/__init__.py`
