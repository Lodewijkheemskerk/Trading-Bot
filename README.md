# PREDICT MKT

An AI-powered trading bot for [Kalshi](https://kalshi.com) prediction markets. It scans thousands of event markets, researches news sentiment from multiple sources, generates probability estimates using a 5-model LLM ensemble, enforces 12 deterministic risk checks with Kelly Criterion sizing, and automatically places limit orders on Kalshi's demo exchange. Every trade outcome is resolved, scored, and fed back into the system through a failure classification loop. A real-time dashboard lets you monitor and control everything.

> **⚠️ Demo only.** This bot is configured for Kalshi's demo environment with mock funds. It does not trade real money by default.

---

## Pipeline

The bot runs a 7-step pipeline on a configurable interval (default: every 15 minutes):

```
┌─────────┐    ┌──────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌──────────┐
│  SCAN   │───▶│ RESEARCH │───▶│ PREDICT │───▶│ EXECUTE │───▶│ MONITOR │───▶│ RESOLVE │───▶│ COMPOUND │
└─────────┘    └──────────┘    └─────────┘    └─────────┘    └─────────┘    └─────────┘    └──────────┘
```

| Step | What it does | Key file |
|------|-------------|----------|
| **Scan** | Fetches 4,000+ markets from Kalshi, filters by volume (≥200), liquidity, and expiry (≤30 days). Flags anomalies: sudden price moves >10%, wide spreads, volume spikes. Outputs a ranked list of tradeable opportunities. | `scripts/scanner.py` |
| **Research** | Pulls news from Google News RSS, Bing News RSS, and Reddit in parallel. Runs keyword-based sentiment analysis on each market. Produces research briefs with sentiment scores. | `scripts/researcher.py` |
| **Predict** | Queries 5 LLM models (Grok, Claude, GPT-4o, Gemini, DeepSeek) with different roles (forecaster, analyst, bull/bear advocate). Combines predictions using weighted ensemble. Calculates edge (predicted probability minus market price). | `scripts/predictor.py` |
| **Execute** | Runs 12 deterministic risk checks (no LLM in the loop). Sizes positions with quarter-Kelly. Checks orderbook depth. Places limit orders on Kalshi demo via authenticated REST API. | `scripts/executor.py` |
| **Monitor** | Scans open positions against 5 exit triggers: 15% stop-loss, 20% take-profit, 240-hour max hold, edge decay, 10% emergency stop. Places sell orders when triggered. | `scripts/position_monitor.py` |
| **Resolve** | Checks Kalshi for settled markets. Computes P&L on closed trades. Records outcomes for backtesting and learning. | `scripts/resolver.py` |
| **Compound** | Classifies trade failures into 4 categories (bad prediction, bad timing, bad execution, external shock). Runs LLM-powered post-mortem analysis. Feeds findings back into scanner penalties and researcher warnings. | `scripts/compounder.py` |

---

## Dashboard

A Flask-based web dashboard at `http://localhost:5000` with three pages:

### Main Dashboard
Live overview of bankroll, positions, P&L, market scanner results, sentiment research, AI predictions with per-model breakdowns, 12 risk checks, and trade log. Kalshi exchange data (balance, orders, positions) shown when connected. Click the status pill to pause/resume.

### Backtest
Replays saved prediction data against resolved market outcomes. Shows Brier score, Sharpe ratio, win rate, calibration curve, P&L breakdown, and a trade-by-trade log. Costs $0 in API calls — all data is local.

### Settings
Control everything from the UI:
- **Ensemble** — Enable/disable models, adjust weights, change roles and model variants
- **Thresholds** — Minimum edge and confidence to trade
- **Position Monitor** — Stop-loss, take-profit, max hold time, edge floor
- **Risk Management** — Kelly fraction, position sizing, drawdown limits, VaR, exposure caps, daily API cost cap
- **Schedule & Maintenance** — Pipeline run interval, Kalshi maintenance blackout window

All settings persist to `config/settings.yaml` via the SAVE ALL button.

---

## LLM Ensemble

Five AI models with different roles to reduce single-model bias:

| Model | Role | Weight | Why |
|-------|------|--------|-----|
| **Grok 3 Fast** | Primary forecaster | 30% | Fast, good at current events |
| **Claude Sonnet 4** | News analyst | 20% | Strong reasoning, finds nuance |
| **GPT-4o Mini** | Bull advocate | 20% | Argues the optimistic case |
| **Gemini 2.5 Flash** | Bear advocate | 15% | Argues the pessimistic case |
| **DeepSeek Chat** | Contrarian | 15% | Independent perspective, low cost |

Each model receives the same market data and research but with role-specific system prompts. The ensemble probability is a weighted average. An optional XGBoost calibrator (disabled until 30+ resolved trades accumulate) can be added as a 6th model.

---

## Risk Management

All risk decisions are deterministic — no LLM output is used in risk checks.

| # | Check | Default | Description |
|---|-------|---------|-------------|
| 1 | Min edge | 4% | Predicted probability must differ from market price by at least this |
| 2 | Min confidence | 65% | Model agreement must exceed this threshold |
| 3 | Max position | 5% | No single trade can exceed this % of bankroll |
| 4 | Max concurrent | 15 | Maximum open positions at once |
| 5 | Max daily loss | 15% | Stop trading for the day at this loss |
| 6 | Max drawdown | 8% | Block all trades when drawdown from peak exceeds this |
| 7 | Kill switch | OFF | Emergency halt — create a `STOP` file |
| 8 | Max slippage | 2% | Abort if price moved more than this since signal |
| 9 | Bankroll positive | >$0 | Must have funds remaining |
| 10 | API cost cap | $50/day | Don't spend more than this on LLM calls per day |
| 11 | VaR 95% | 10% | Portfolio Value-at-Risk must stay under this |
| 12 | Total exposure | 40% | Sum of all open positions can't exceed this % of bankroll |

Position sizing uses **quarter-Kelly Criterion** — the mathematically optimal bet size, divided by 4 for safety.

Before each order, an **orderbook depth check** verifies that resting liquidity is at least 2× the order size. Thin books get a warning.

---

## Project Structure

```
├── config/
│   ├── settings.yaml          # All configuration (models, risk, thresholds, etc.)
│   └── __init__.py            # Config loader, paths, constants
├── dashboard/
│   ├── app.py                 # Flask server + API endpoints
│   └── static/
│       ├── index.html         # Main dashboard
│       ├── backtest.html      # Backtesting page
│       └── settings.html      # Settings page
├── scripts/
│   ├── pipeline.py            # 7-step orchestrator with loop mode
│   ├── scanner.py             # Step 1: Market discovery
│   ├── researcher.py          # Step 2: News + sentiment
│   ├── predictor.py           # Step 3: LLM ensemble predictions
│   ├── executor.py            # Step 4: Risk checks + order placement
│   ├── position_monitor.py    # Step 5: Exit trigger monitoring
│   ├── resolver.py            # Step 6: Trade outcome resolution
│   ├── compounder.py          # Step 7: Failure analysis + feedback
│   ├── kalshi_client.py       # Kalshi REST API client (RSA-PSS auth)
│   ├── kelly_size.py          # Kelly Criterion position sizing
│   ├── validate_risk.py       # 12 deterministic risk checks
│   ├── orderbook.py           # Orderbook depth analysis
│   ├── backtester.py          # Historical backtest framework
│   ├── xgboost_model.py       # XGBoost calibrator (optional)
│   ├── retry.py               # Retry/reconnection logic
│   └── resolver.py            # Market settlement checker
├── tests/                     # 157+ tests across 11 test files
├── data/
│   ├── predictions/           # Saved prediction snapshots
│   ├── research_briefs/       # Saved research data
│   ├── trades/                # Trade log + portfolio state
│   ├── scans/                 # Market scan snapshots
│   └── backtest/              # Backtest results
└── references/                # Design doc, formulas, platform notes
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- API keys for at least one LLM provider (Grok, OpenAI, Anthropic, Google, or DeepSeek)
- Kalshi demo account with API key pair (for live demo trading)

### Setup

```bash
# Clone
git clone https://github.com/Lodewijkheemskerk/Trading-Bot.git
cd Trading-Bot

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Edit .env with your API keys
```

### Run

```bash
# Start the dashboard
python dashboard/app.py
# Open http://localhost:5000

# Run the pipeline once
python scripts/pipeline.py --mode once

# Run on a loop (every 15 minutes)
python scripts/pipeline.py --mode loop --interval 15

# Run individual steps
python scripts/scanner.py
python scripts/researcher.py
python scripts/predictor.py

# Emergency stop
python scripts/pipeline.py --kill

# Run tests
python -m pytest tests/ -v
```

### Kalshi Demo Setup

1. Create a [Kalshi](https://kalshi.com) account
2. Go to Settings → API Keys → Generate a new key pair
3. Save the private key as `config/kalshi_demo_key.pem`
4. Add to `.env`:
   ```
   KALSHI_KEY_ID=your-key-id
   KALSHI_PRIVATE_KEY_PATH=config/kalshi_demo_key.pem
   KALSHI_ENV=demo
   ```
5. Set `mode: demo` in `config/settings.yaml`

---

## Configuration

All settings are in `config/settings.yaml` and can also be changed from the dashboard settings page.

Key sections:

| Section | What it controls |
|---------|-----------------|
| `bankroll` | Starting capital ($500 default) |
| `scanner` | Schedule interval, volume/liquidity filters, anomaly thresholds |
| `ensemble_models` | Which LLMs to use, weights, roles, model variants |
| `prediction` | Min edge (4%), min confidence (65%) |
| `risk` | Kelly fraction, position limits, drawdown, VaR, exposure |
| `position_monitor` | Stop-loss, take-profit, max hold, edge floor |
| `research` | News sources, parallel workers |
| `trading_hours` | Maintenance blackout window |
| `kalshi` | API URLs for demo/production |

---

## Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_tier1.py -v
```

| Test file | What it covers | Tests |
|-----------|---------------|-------|
| `test_tier1.py` | Trade resolution, failure classification, feedback loop, post-mortem | 23 |
| `test_tier2.py` | VaR, exposure checks, prompt injection protection | 22 |
| `test_tier4.py` | Kalshi auth, order placement, demo toggle | 17 |
| `test_position_monitor.py` | All 5 exit triggers, sell orders, pipeline integration | 32 |
| `test_researcher.py` | RSS fetching, parallel research, sentiment | 19 |
| `test_xgboost.py` | Feature extraction, training, cold start | 22 |
| `test_backtester.py` | Brier score, calibration, P&L, data collection | 23 |
| `test_orderbook_hours.py` | Depth checks, spread/midpoint, maintenance blackout | 22 |

---

## How It Makes Decisions

1. **Scanner finds opportunity**: Market has volume, isn't expiring tomorrow, price looks off
2. **Research gathers evidence**: What are the news headlines saying? What's Reddit's take?
3. **5 AI models vote**: Each gives a probability. Weighted average becomes the ensemble prediction
4. **Edge calculated**: `ensemble - market_price`. If positive → BUY YES. If negative → BUY NO
5. **Risk gate**: All 12 checks must pass. Kelly sizes the position. Orderbook must have depth
6. **Order placed**: Limit order on Kalshi at the current market price
7. **Monitored**: Position watched for stop-loss, take-profit, or time expiry
8. **Resolved**: When market settles, P&L computed, outcome logged
9. **Learned from**: Failures classified, post-mortem written, scores adjusted

---

## Security

- API keys stored in `.env` (gitignored)
- Kalshi private key stored as `.pem` file (gitignored)
- Prompt injection protection: 12 regex patterns + LLM data-boundary instructions sanitize all external text before it reaches AI models
- No LLM output is used in risk decisions — all risk checks are deterministic
- Kill switch: create a `STOP` file to immediately halt all trading

---

## Disclaimer

This is an educational project. Prediction market trading involves financial risk. This bot is configured for Kalshi's demo environment with mock funds. Never trade money you can't afford to lose. The authors are not responsible for any financial losses.
