# Prediction Market Trading Bot

AI-powered trading bot for Kalshi prediction markets. Scans markets, researches events, predicts probabilities, manages risk with Kelly Criterion, and learns from every trade.

## Architecture

```
SCAN → RESEARCH → PREDICT → EXECUTE → COMPOUND
```

| Step | What it does |
|------|-------------|
| **Scan** | Connects to Kalshi API, filters 300+ markets by volume/liquidity/expiry |
| **Research** | Scrapes Google News + Reddit, runs sentiment analysis |
| **Predict** | Claude AI + heuristic ensemble estimates true probability |
| **Execute** | 10-point risk check, Kelly sizing, paper/live trade |
| **Compound** | Post-mortem every trade, update knowledge base |

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env

# Scan markets
python scripts/scanner.py

# Full pipeline (paper trading)
python scripts/pipeline.py --mode once

# Run on loop
python scripts/pipeline.py --mode loop --interval 15

# Emergency stop
python scripts/pipeline.py --kill
```

## Risk Limits

| Rule | Limit |
|------|-------|
| Max per position | 5% of bankroll |
| Max concurrent | 15 positions |
| Daily loss limit | 15% → shutdown |
| Max drawdown | 8% → block trades |
| Slippage abort | >2% |
| Kelly fraction | 0.25 (quarter-Kelly) |

## Disclaimer

Educational purposes only. Trading involves financial risk. Never trade money you can't afford to lose.
