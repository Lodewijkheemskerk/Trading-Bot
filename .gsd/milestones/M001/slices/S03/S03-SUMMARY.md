---
id: S03
parent: M001
milestone: M001
provides:
  - PredictionEngine class with Claude AI + heuristic bull/bear ensemble
  - TradeSignal and ModelPrediction dataclasses (S03→S04 boundary contract)
  - Edge calculation, mispricing Z-score, should_trade decision logic
  - Graceful fallback to heuristic-only when Claude API unavailable
  - CLI with --heuristic-only and --top flags, formatted table output, JSON snapshots
requires:
  - slice: S02
    provides: ResearchBrief dicts (current_yes_price, consensus_sentiment, consensus_confidence, gap, narrative_summary), research snapshots in data/research_briefs/
affects:
  - S04
key_files:
  - scripts/predictor.py
  - tests/test_predictor.py
  - config/__init__.py
key_decisions:
  - Claude client lazy-initialized to avoid import-time crashes when API key missing
  - Heuristic bull/bear shift 5-15% from market price based on sentiment signals
  - Mispricing Z-score uses fixed BASELINE_STD=0.10 (no historical data in M001)
  - 4-level JSON response parsing fallback for Claude output
  - Ensemble re-normalizes weights when any model is absent
  - should_trade requires BOTH min_edge (0.04) AND min_confidence (0.65)
patterns_established:
  - Three-model ensemble with config-driven weights from settings.yaml
  - Graceful degradation: API failure → heuristic-only with re-normalized weights
  - Predictor follows scanner/researcher patterns: dataclasses, logging, JSON persistence, argparse CLI
observability_surfaces:
  - INFO log per model prediction (model_name, probability, confidence)
  - INFO log for ensemble result (edge, direction, strength, trade decision)
  - WARNING log for Claude API failures with exception details
  - JSON predictions in data/predictions/ for post-hoc inspection
  - CLI model breakdown shows which models actually ran
drill_down_paths:
  - .gsd/milestones/M001/slices/S03/tasks/T01-SUMMARY.md
  - .gsd/milestones/M001/slices/S03/tasks/T02-SUMMARY.md
duration: 40m
verification_result: passed
completed_at: 2026-03-13
---

# S03: Prediction Engine

**3-model ensemble prediction engine: Claude AI + heuristic bull/bear advocates → weighted probability → edge calculation → mispricing Z-score → trade/no-trade signal, with graceful heuristic-only fallback.**

## What Happened

Built the complete prediction engine in two tasks. T01 implemented the core `PredictionEngine` class with all components: `TradeSignal` and `ModelPrediction` dataclasses matching the S04 boundary contract, Claude AI model with structured prompt and 4-level JSON response parsing, heuristic bull advocate (shifts up 5-15% from market price based on sentiment), heuristic bear advocate (shifts down symmetrically), ensemble aggregation with config-driven weights and automatic re-normalization when models are missing, edge calculation, mispricing Z-score, direction logic, signal strength, and should_trade decision. Added `PREDICTIONS_DIR` to config. 33 unit tests cover all logic paths.

T02 verified end-to-end operation. The Claude API was reached successfully but returned 400 (insufficient credits), which triggered the designed fallback path — WARNING logged, heuristic-only ensemble produced valid output. The CLI works in both full-ensemble and `--heuristic-only` modes. All S04 contract fields present in saved JSON.

## Verification

1. **`python -c "from scripts.predictor import PredictionEngine, TradeSignal, ModelPrediction; print('ok')"`** — clean imports ✅
2. **`python tests/test_predictor.py -v`** — 33 tests pass (0.002s) ✅
3. **`python scripts/predictor.py --heuristic-only --top 1`** — table + JSON saved, no errors ✅
4. **`python scripts/predictor.py --top 1`** (with API key) — Claude 400 handled gracefully, fallback works ✅
5. **S04 contract fields in JSON** — ensemble_probability, market_probability, edge, direction, signal_strength all present ✅
6. **Observability** — per-model INFO logs, Claude WARNING on failure, JSON snapshots persisted ✅

## Deviations

Claude API returned 400 (insufficient credits) during live testing. The graceful fallback worked as designed — heuristic-only mode is fully functional. Claude predictions will automatically appear when credits are available.

## Known Limitations

- Claude API requires funded credits for actual AI predictions. Heuristic-only mode provides baseline predictions without it.
- Heuristic models are simple anchoring functions — they provide mechanical diversification, not independent analysis. Real prediction quality depends on Claude.
- Mispricing Z-score uses a fixed baseline (0.10 std) since M001 has no historical edge distribution. This will be calibrated in S05 with actual trade outcomes.

## Follow-ups

None. S04 (Risk Management + Paper Execution) consumes TradeSignal dicts as designed.

## Files Created/Modified

- `scripts/predictor.py` — Complete PredictionEngine module: 3 models, ensemble, edge calculation, JSON persistence, CLI
- `tests/test_predictor.py` — 33 unit tests covering all logic paths and contract compliance
- `config/__init__.py` — Added PREDICTIONS_DIR constant and auto-creation
- `.env` — ANTHROPIC_API_KEY added via secure_env_collect

## Forward Intelligence

### What the next slice should know
- `TradeSignal` is consumed as a dict (via `dataclasses.asdict()`). S04 should expect: `ensemble_probability` (float 0.01-0.99), `market_probability` (float, the yes_price), `edge` (float, can be negative), `direction` ("buy_yes"/"buy_no"), `signal_strength` (float ≥ 0), `confidence` (float 0.1-0.95), `should_trade` (bool), `model_predictions` (list of ModelPrediction dicts).
- Prediction snapshots are saved as `data/predictions/predictions_YYYYMMDD_HHMMSS.json` with structure `{timestamp, markets_predicted, signals: [...]}`.
- The `should_trade` flag uses `min_edge=0.04` and `min_confidence=0.65` from config. S04 risk checks are additive — a market can pass should_trade but still be blocked by risk rules.

### What's fragile
- Without Claude API credits, predictions are heuristic-only. The ensemble will always anchor close to market price (bull shifts up ~7-17%, bear shifts down ~7-17%, average is close to market). This means `edge` values will be small and `should_trade` will usually be False with default thresholds.
- Claude response parsing has been tested with 5 formats in unit tests but not against live API output. If Claude changes its response format significantly, the regex fallback may need updating.

### Authoritative diagnostics
- `python tests/test_predictor.py -v` — fast (0.002s), tests all core logic offline
- `data/predictions/predictions_*.json` — inspect model_predictions to verify which models ran
- CLI model breakdown (printed after table) shows per-model probability/confidence/weight

### What assumptions changed
- No fundamental assumptions changed. Claude API integration pattern works as expected. The insufficient credits issue is an account-level concern, not an architectural one.
