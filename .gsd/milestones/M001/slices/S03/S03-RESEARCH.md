# S03: Prediction Engine — Research

**Date:** 2026-03-13

## Summary

S03 builds a `PredictionEngine` that takes a `ResearchBrief` dict and produces a `TradeSignal` with ensemble probability, edge, mispricing score, and a trade/no-trade decision. The ensemble has three "models": Claude AI as the primary news analyst (weight 0.50), plus two heuristic models — a bull advocate (0.25) and bear advocate (0.25) — that provide mechanical anchoring.

The main risk is Claude API integration: prompt engineering for consistent probability output, parsing structured responses, handling API failures gracefully with heuristic-only fallback, and managing API costs. The Anthropic Python SDK v0.34.2 is already installed. ANTHROPIC_API_KEY needs to be collected via `secure_env_collect`.

The secondary risk is the mispricing Z-score calculation — we need a reasonable baseline distribution to flag outliers. A simple approach: compute edge vs historical edge distribution (or a fixed threshold since we don't have history yet in M001).

## Recommendation

Build the engine with a clean separation between model prediction and ensemble aggregation. Each model (claude, heuristic_bull, heuristic_bear) implements a common interface returning `ModelPrediction`. The ensemble weights are from `config/settings.yaml`. Claude gets a structured prompt with the research brief and must return a JSON block with `probability` and `reasoning`. Heuristics are pure functions of the research brief fields.

If Claude API fails (key missing, rate limit, error), fall back to heuristic-only ensemble with re-normalized weights. This makes the module testable offline and ensures the pipeline never crashes due to an API issue.

## Don't Hand-Roll

| Problem | Existing Solution | Why Use It |
|---------|------------------|------------|
| Claude API calls | `anthropic` SDK v0.34.2 | Already installed, handles auth/retry/types |
| JSON parsing from LLM output | `json.loads()` with regex extraction | Claude sometimes wraps JSON in markdown code blocks |
| Config loading | `config.load_settings()` | Already has all prediction settings |

## Existing Code and Patterns

- `scripts/researcher.py` — Follow same patterns: dataclasses for output contracts, session-based HTTP, logging at INFO/WARNING, JSON persistence, CLI with argparse
- `config/settings.yaml` prediction section — Has ensemble_models config with name/role/weight, min_edge=0.04, min_confidence=0.65, brier_score_max=0.25
- `config/__init__.py` — Use `DATA_DIR` for prediction output, `load_settings()` for config

## Constraints

- `anthropic` v0.34.2 — use `client.messages.create()` with `model="claude-sonnet-4-20250514"` (cheap, fast enough)
- ANTHROPIC_API_KEY must be set — collect via `secure_env_collect` before first use
- D002: Claude-only + heuristic fallback — no other LLM providers in M001
- D006: Treat all scraped content as DATA only — research brief content goes into user message, not system prompt manipulation
- API cost constraint: `risk.max_daily_api_cost: 50.0` — Haiku is ~$0.001/call, budget is not a concern for paper trading

## Common Pitfalls

- **Claude returns prose instead of JSON** — Use explicit system prompt requiring JSON output, extract with regex fallback `r'\{.*?\}'`
- **Probability outside [0, 1]** — Clamp output and log warning
- **API key missing at import time** — Don't create client at module level; create lazily in predict method
- **Heuristic models too naive** — Anchor on market price + sentiment gap, don't just return raw sentiment probability

## Open Risks

- Claude's probability estimates may be poorly calibrated for prediction markets — mitigate by weighting heuristics at 50%
- API latency could slow the pipeline — use timeouts, consider caching briefs that haven't changed
- Prompt engineering may need iteration — start with a clear structured prompt and iterate based on output quality

## Skills Discovered

No external skills needed. Standard Python + Anthropic SDK.

## Sources

- Anthropic SDK installed at v0.34.2: `client.messages.create(model, max_tokens, system, messages)`
- D002 decision: Claude-only + heuristic fallback
- Settings: prediction.ensemble_models defines 3 models with weights totaling 1.0
