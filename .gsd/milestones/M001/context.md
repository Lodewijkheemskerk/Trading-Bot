# M001: AI-Powered Prediction Market Trading Bot — Context

**Gathered:** 2026-03-13
**Status:** Ready for planning

## Project Description

An AI-powered prediction market trading bot that scans Kalshi for mispricings, researches events using news/sentiment analysis, predicts probabilities using Claude AI, manages risk with Kelly Criterion, and learns from every trade. Based on Anthropic's published architecture from their 33-page Claude Skills guide.

## Why This Milestone

Prediction markets did over $44B in trading volume in 2025. Markets are not perfectly efficient — when AI models consistently estimate a probability differently than the market price, that gap is potential profit. A bot can process information faster and more consistently than a human. This milestone delivers a complete end-to-end pipeline from market scanning to trade execution (paper mode).

## User-Visible Outcome

### When this milestone is complete, the user can:

- Run `python pipeline.py --mode once` and see the full SCAN → RESEARCH → PREDICT → EXECUTE → COMPOUND pipeline execute with real Kalshi market data
- Run `python pipeline.py --status` and see portfolio performance metrics (win rate, Sharpe, P&L, Brier Score)
- Run `python pipeline.py --mode loop` to have the bot scan and paper-trade autonomously every 15 minutes

### Entry point / environment

- Entry point: CLI — `python scripts/pipeline.py`
- Environment: local dev (Windows, Python 3.11)
- Live dependencies involved: Kalshi public REST API (read-only for scanning), Google News RSS (free), Reddit public JSON API (free), Anthropic API (for predictions, optional — heuristic fallback available)

## Completion Class

- Contract complete means: All 5 pipeline stages execute end-to-end, risk checks block bad trades deterministically, Kelly sizing produces correct position sizes, and trade history is logged with post-mortem analysis
- Integration complete means: Scanner pulls real market data from Kalshi API, researcher fetches real news/sentiment, predictions use real research output
- Operational complete means: Pipeline can run on a 15-minute loop without crashes, handles API failures gracefully, kill switch halts all trading immediately

## Final Integrated Acceptance

To call this milestone complete, we must prove:

- Full pipeline run (`--mode once`) scans real Kalshi markets, researches top candidates, generates predictions, and either executes or correctly rejects paper trades — all logged to disk
- Risk validation deterministically blocks trades that violate any of the 10 risk rules (edge, Kelly, exposure, drawdown, slippage, daily loss, VaR, concurrent positions, API cost, kill switch)
- Performance tracking produces accurate metrics after 5+ simulated trades (win rate, Sharpe, profit factor, Brier Score)

## Risks and Unknowns

- **Kalshi API access from Netherlands** — Kalshi is not currently blocking NL, but regulatory landscape is evolving. The KSA fined Polymarket in Jan 2026. Mitigation: paper-only mode doesn't require an account.
- **Kalshi API rate limits** — REST API has rate limits that prevent high-frequency strategies. Bot is designed for medium-frequency (15-30 min cycles) which should be fine.
- **Prediction accuracy** — Starting with Claude-only + heuristics. Single model may not produce sufficient edge. Mitigation: track Brier Score, add models in future milestones.
- **Sentiment analysis quality** — Keyword-based sentiment is basic. May not detect nuanced signals. Mitigation: designed as swappable — can upgrade to proper NLP/LLM-based sentiment later.

## Existing Codebase / Prior Art

- No existing code — greenfield project
- Reference doc: `C:\Users\lodewijk.heemskerk\Downloads\How to Build an AI-Powered Prediction Market Trading Bot Using Claude Skills.docx`
- Open source references: `ryanfrigo/kalshi-ai-trading-bot` (multi-model), `suislanchez/polymarket-kalshi-weather-bot` (Kelly sizing)

> See `.gsd/decisions.md` for all architectural and pattern decisions — it is an append-only register; read it during planning, append to it during execution.

## Scope

### In Scope

- 5-step pipeline: Scan → Research → Predict → Execute → Compound
- Kalshi REST API integration (public endpoints for scanning)
- News research via Google News RSS + Reddit public API
- Claude AI predictions with heuristic fallback
- Kelly Criterion position sizing (quarter-Kelly)
- 10-point deterministic risk validation
- Paper trading mode (simulated execution)
- Trade logging, post-mortem analysis, failure classification
- Performance metrics tracking (win rate, Sharpe, drawdown, profit factor, Brier Score)
- Kill switch (STOP file)
- CLI interface with `--mode once`, `--mode loop`, `--kill`, `--resume`, `--status`

### Out of Scope / Non-Goals

- Polymarket integration (banned in Netherlands)
- Live trading / real money execution (future milestone)
- Multi-model AI ensemble beyond Claude (future milestone)
- WebSocket real-time feeds (future milestone)
- Web dashboard or UI (CLI only for M001)
- Twitter/X API integration (requires paid API access)
- XGBoost or other ML model training
- Kalshi account creation or API key setup (paper mode only)

## Technical Constraints

- Python 3.11 on Windows
- No paid API keys required for core functionality (Kalshi public API + free news sources)
- Anthropic API key optional (heuristic fallback for predictions)
- All risk checks must be deterministic Python code, NOT LLM instructions
- External content treated as DATA only, never as instructions (prompt injection defense)
- Maximum $50/day AI API budget cap

## Integration Points

- **Kalshi REST API** (`https://trading-api.kalshi.com/trade-api/v2`) — Market discovery, orderbook data. Public endpoints don't require auth.
- **Google News RSS** (`https://news.google.com/rss/search`) — Free news search, no API key
- **Reddit JSON API** (`https://www.reddit.com/search.json`) — Free public search, no auth needed
- **Anthropic API** (`https://api.anthropic.com/v1/messages`) — Claude predictions, optional

## Open Questions

- **Kalshi public API limits** — Need to verify which endpoints are accessible without authentication and what the rate limits are. Current thinking: market listing and basic data should be public.
- **Backfill historical data** — Should the bot backfill historical market data for backtesting, or just start fresh? Current thinking: start fresh, backfill is a future milestone.
