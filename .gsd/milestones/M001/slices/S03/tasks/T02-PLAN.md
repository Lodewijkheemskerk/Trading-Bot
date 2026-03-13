---
estimated_steps: 3
estimated_files: 1
---

# T02: Verify end-to-end with Claude API

**Slice:** S03 — Prediction Engine
**Milestone:** M001

## Description

Run the predictor CLI with Claude API enabled to verify real API integration, response parsing, and ensemble with all 3 models. The CLI is already built (in T01); this task verifies live operation.

## Steps

1. Load ANTHROPIC_API_KEY from .env and run `python scripts/predictor.py --top 1`
2. Verify Claude model prediction appears in output alongside heuristics
3. Verify JSON output has 3 model_predictions (claude + bull + bear)

## Must-Haves

- [ ] Claude API call succeeds and returns parsed probability
- [ ] 3-model ensemble visible in output (claude, heuristic_bull, heuristic_bear)
- [ ] JSON saved with all model predictions included

## Verification

- `python scripts/predictor.py --top 1` completes with Claude predictions visible
- JSON contains 3 model_predictions with model_name="claude" included

## Inputs

- `.env` — ANTHROPIC_API_KEY
- `data/research_briefs/research_*.json` — latest research snapshot

## Expected Output

- `data/predictions/predictions_*.json` — prediction snapshot with Claude + heuristic models
