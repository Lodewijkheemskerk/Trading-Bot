---
estimated_steps: 8
estimated_files: 3
---

# T01: Build PredictionEngine with Claude AI model, heuristic models, and ensemble

**Slice:** S03 — Prediction Engine
**Milestone:** M001

## Description

Build the complete `PredictionEngine` class that takes a research brief dict and returns a `TradeSignal` with ensemble probability, edge calculation, and trade decision. Three models: Claude AI (news analyst, weight 0.50), heuristic bull (0.25), heuristic bear (0.25). Graceful fallback to heuristic-only when Claude API unavailable.

## Steps

1. Add `PREDICTIONS_DIR` to `config/__init__.py` and ensure it's auto-created
2. Implement `ModelPrediction` and `TradeSignal` dataclasses matching S03→S04 boundary contract
3. Implement `_predict_claude()` — structured prompt, Anthropic SDK call, JSON response parsing with regex fallback
4. Implement `_predict_heuristic_bull()` — anchors on market price + bullish sentiment gap
5. Implement `_predict_heuristic_bear()` — anchors on market price + bearish sentiment gap
6. Implement `predict()` — runs all models, ensemble aggregation, edge calculation, direction logic, mispricing Z-score, should_trade decision
7. Implement JSON persistence (`save_prediction()`)
8. Write unit tests: heuristics, ensemble, edge, direction, should_trade, contract fields, Claude fallback

## Must-Haves

- [ ] `TradeSignal` has all S04 contract fields: ensemble_probability, market_probability, edge, direction, signal_strength
- [ ] `ModelPrediction` has: model_name, role, weight, predicted_probability, confidence, reasoning
- [ ] Claude model sends structured prompt and parses JSON response
- [ ] Heuristic models produce mechanically different predictions (bull biases up, bear biases down)
- [ ] Ensemble uses config weights, re-normalizes when Claude unavailable
- [ ] `should_trade` respects min_edge and min_confidence from config
- [ ] Graceful Claude fallback without crashing
- [ ] Unit tests cover all core logic paths

## Verification

- `python tests/test_predictor.py -v` passes all tests
- `python -c "from scripts.predictor import PredictionEngine, TradeSignal, ModelPrediction; print('ok')"` works
- Heuristic-only prediction works without ANTHROPIC_API_KEY

## Observability Impact

- Signals added: INFO log per model prediction (model_name, probability, confidence), INFO for ensemble result, WARNING for Claude API failures
- How a future agent inspects: check logs during `predict()`, inspect JSON output for per-model breakdown
- Failure state exposed: Claude errors logged with exception details, `model_predictions` list in TradeSignal shows which models ran

## Inputs

- `scripts/researcher.py` — ResearchBrief dict structure (market_id, current_yes_price, consensus_sentiment, consensus_confidence, gap, narrative_summary, source_results)
- `config/settings.yaml` — prediction section (min_edge, min_confidence, ensemble_models with weights)

## Expected Output

- `scripts/predictor.py` — Complete PredictionEngine module with dataclasses, three models, ensemble, edge calculation, JSON persistence
- `tests/test_predictor.py` — Unit tests for all core logic
- `config/__init__.py` — PREDICTIONS_DIR added
