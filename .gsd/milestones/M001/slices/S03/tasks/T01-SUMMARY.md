---
id: T01
parent: S03
milestone: M001
provides:
  - PredictionEngine class with Claude AI + heuristic bull/bear models
  - TradeSignal and ModelPrediction dataclasses (S03→S04 contract)
  - Ensemble aggregation with weight re-normalization for missing models
  - Edge calculation, mispricing Z-score, should_trade decision logic
  - JSON persistence for predictions
  - PREDICTIONS_DIR in config
key_files:
  - scripts/predictor.py
  - tests/test_predictor.py
  - config/__init__.py
key_decisions:
  - Claude client lazy-initialized (not at import time) to avoid crashes when API key missing
  - Heuristic bull shifts up 5-15% from market price depending on sentiment, bear shifts down symmetrically
  - Mispricing Z-score uses BASELINE_STD=0.10 (conservative fixed baseline, no historical data in M001)
  - JSON response parsing uses 4-level fallback: direct parse → code block → first JSON object → regex extraction
  - Ensemble re-normalizes weights when Claude unavailable (heuristic-only mode)
patterns_established:
  - Three-model ensemble with config-driven weights from settings.yaml
  - Graceful degradation pattern: Claude failure → heuristic-only with re-normalized weights
  - Static methods for heuristics enable easy unit testing without client instantiation
  - predictor.py follows researcher.py patterns: dataclasses, logging, JSON persistence, CLI
observability_surfaces:
  - INFO log per model prediction with probability and confidence
  - INFO log for ensemble result with edge, direction, strength, trade decision
  - WARNING log for Claude API failures with exception details
  - JSON predictions in data/predictions/ for post-hoc inspection
duration: 30m
verification_result: passed
completed_at: 2026-03-13
blocker_discovered: false
---

# T01: Build PredictionEngine with Claude AI model, heuristic models, and ensemble

**Complete prediction pipeline from ResearchBrief to TradeSignal with 3-model ensemble (Claude + bull/bear heuristics), edge calculation, mispricing Z-score, and graceful fallback.**

## What Happened

Built `scripts/predictor.py` with the full `PredictionEngine` class:

1. **Dataclasses**: `ModelPrediction` and `TradeSignal` matching S03→S04 boundary contract exactly, including `market_probability` pass-through.
2. **Claude AI model**: Structured system+user prompt, JSON response parsing with 4-level fallback (direct, code block, first object, regex), lazy client initialization, graceful error handling.
3. **Heuristic bull**: Anchors on market price, shifts up 5-15% based on sentiment direction, confidence, and gap. Higher confidence when sentiment confirms bull thesis.
4. **Heuristic bear**: Mirror of bull, shifts down 5-15%. Higher confidence when sentiment confirms bear thesis.
5. **Ensemble**: Confidence-weighted average with weight re-normalization when models are missing. Clamped to [0.01, 0.99].
6. **Edge logic**: `ensemble_prob - market_prob`, direction (buy_yes/buy_no), mispricing Z-score (edge/0.10), signal_strength (|edge| * confidence), should_trade (edge > min_edge AND confidence > min_confidence).
7. **Config**: Added `PREDICTIONS_DIR` to `config/__init__.py` with auto-creation.

33 unit tests cover heuristics (directional bias, sentiment response, clamping), ensemble (weights, re-normalization, empty), edge calculation, should_trade thresholds, Claude response parsing (5 formats), Claude fallback, and contract field validation.

## Verification

- `python tests/test_predictor.py -v` — **33 tests, all pass** (0.002s)
- `python -c "from scripts.predictor import PredictionEngine, TradeSignal, ModelPrediction; print('ok')"` — **PASS**
- `python scripts/predictor.py --heuristic-only --top 3` — runs without API key, table + JSON saved — **PASS**
- S04 contract fields in JSON: ensemble_probability, market_probability, edge, direction, signal_strength all present — **PASS**
- Heuristic-only fallback: 2 model predictions (no Claude), weights re-normalized — **PASS**

## Diagnostics

- `python tests/test_predictor.py -v` for unit test regression
- `data/predictions/predictions_*.json` for saved prediction snapshots
- Check logs during `predict()` for per-model probabilities and ensemble result
- `PredictionEngine.compute_edge(ensemble, market)` for interactive edge debugging

## Deviations

None.

## Known Issues

None discovered during implementation.

## Files Created/Modified

- `scripts/predictor.py` — Complete PredictionEngine module with 3 models, ensemble, edge calculation, JSON persistence, CLI
- `tests/test_predictor.py` — 33 unit tests covering all core logic paths
- `config/__init__.py` — Added PREDICTIONS_DIR constant and auto-creation
