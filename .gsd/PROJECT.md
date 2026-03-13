# AI-Powered Prediction Market Trading Bot

A 5-step automated trading pipeline for Kalshi prediction markets.

## Architecture

```
Scan → Research → Predict → Execute → Compound
  |        |          |         |          |
  v        v          v         v          v
Kalshi   Google    Claude    10 Risk    Performance
 API     News +    AI +     Checks +    Metrics +
         Reddit   Heuristic   Kelly    Failure Log
```

## Quick Start

```bash
# Single pipeline run (heuristic-only, no API key needed)
python scripts/pipeline.py --mode once --heuristic-only

# Full pipeline with Claude AI (needs ANTHROPIC_API_KEY in .env)
python scripts/pipeline.py --mode once

# Autonomous loop (15-minute intervals)
python scripts/pipeline.py --mode loop --interval 15

# Performance dashboard
python scripts/pipeline.py --status

# Emergency stop
python scripts/pipeline.py --kill

# Resume after stop
python scripts/pipeline.py --resume
```

## Individual Steps

```bash
python scripts/scanner.py              # Step 1: Scan Kalshi markets
python scripts/researcher.py --top 5   # Step 2: Research sentiment
python scripts/predictor.py            # Step 3: AI predictions
python scripts/executor.py --dry-run   # Step 4: Risk checks + paper trades
python scripts/pipeline.py --status    # Step 5: Performance metrics
```

## Key Files

| File | Purpose |
|------|---------|
| `config/settings.yaml` | All configuration (thresholds, weights, limits) |
| `scripts/scanner.py` | Kalshi market scanner |
| `scripts/researcher.py` | Google News + Reddit sentiment analysis |
| `scripts/predictor.py` | Claude AI + heuristic ensemble prediction |
| `scripts/validate_risk.py` | 10 deterministic risk checks |
| `scripts/kelly_size.py` | Kelly Criterion position sizing |
| `scripts/executor.py` | Paper trade execution |
| `scripts/compounder.py` | Performance metrics + learning |
| `scripts/pipeline.py` | Full 5-step pipeline orchestrator |

## Test Suite

```bash
python tests/test_researcher.py   # 29 tests — sentiment, query extraction
python tests/test_predictor.py    # 33 tests — heuristics, ensemble, Claude parsing
python tests/test_risk.py         # 44 tests — all 10 risk checks, Kelly, executor
python tests/test_pipeline.py     # 15 tests — compounder, kill switch, pipeline
```

121 total tests, all passing.

## Current State

- **Mode:** Paper trading only (D003)
- **Platform:** Kalshi only (D001)
- **AI:** Claude Sonnet + heuristic bull/bear ensemble (D002)
- **Risk:** 10 deterministic checks, quarter-Kelly sizing (D005, D016)
