# Implementation Audit: Design Document vs. Actual Code

**Audit Date:** 2026-03-16
**Document:** `references/How to Build an AI-Powered Prediction Market Trading Bot Using Claude Skills.docx`
**Milestone:** M001 (complete)

---

## Step 1: SCAN

| Document Requirement | Status | Detail |
|---|---|---|
| Connect to Kalshi REST API | ✅ | `scanner.py` connects via `/events?with_nested_markets=true` |
| Connect to Polymarket CLOB API | ❌ | No Polymarket support at all. No CLOB, no EIP-712 signing, no Polygon chain integration. |
| Filter by minimum volume (≥200 contracts) | ✅ | `min_volume: 200` in settings, enforced in `_passes_filters` |
| Filter by max time to expiry (30 days) | ✅ | `max_days_to_expiry: 30` in settings |
| Filter by minimum liquidity | ✅ | `min_liquidity: 0` (configured, threshold set to 0) |
| Flag anomalies: sudden price moves >10% | ✅ | `anomaly_price_move: 0.10`, checks `price_move_24h` |
| Flag anomalies: spreads wider than 5 cents | ✅ | `anomaly_spread: 0.05`, checks `spread` |
| Flag anomalies: volume spikes vs 7-day average | ✅ | `volume_history.json` persists daily volumes per market. `_get_7day_avg_volume()` computes rolling average. `_check_anomalies()` flags markets where volume_24h > `volume_spike_multiplier` (default 2x) times 7-day average. Auto-prunes entries older than 14 days. |
| Output ranked list sorted by opportunity | ✅ | `opportunity_score` with multi-factor scoring, sorted descending |
| Run on schedule (every 15-30 min) | ✅ | Pipeline loop with configurable `interval` (default 15 min) |
| WebSocket API for live orderbook updates | ❌ | No WebSocket support. REST-only polling. |
| Check orderbook depth before trading | ✅ | `scripts/orderbook.py` — `check_depth()` fetches orderbook via `KalshiClient.get_orderbook()`, parses levels, checks resting depth ≥ `min_depth_ratio` (default 2x) of our contract count. Warns on thin books. Wired into executor pre-order. |
| Use pmxt unified wrapper | ❌ | Not used. Not in requirements.txt or code. |

---

## Step 2: RESEARCH

| Document Requirement | Status | Detail |
|---|---|---|
| Scrape Google News | ✅ | Google News RSS parsed via XML |
| Scrape Reddit | ✅ | Reddit JSON search endpoint |
| Scrape Twitter/X for real-time sentiment | ⚠️ | Indirect only — uses Grok 4's `x_search` tool via Responses API, not direct Twitter API scraping. Disabled by default. Cost ~$0.22/call. |
| Scrape RSS feeds (configurable) | ⚠️ | Only Google News RSS. No way to add custom RSS sources. |
| Scrape additional news sources | ⚠️ | Only Google News. No additional news APIs (NewsAPI, etc.) |
| Run NLP sentiment classification | ✅ | Keyword-based with bigram negation. Not ML-based NLP, but functional. |
| Bullish/bearish/neutral classification | ✅ | Per-source and consensus |
| Cross-reference multiple sources to reduce noise | ✅ | Confidence-weighted consensus aggregation across sources |
| Compare narrative vs market price | ✅ | Gap analysis (sentiment_implied_probability - yes_price) |
| Output research brief per market | ✅ | `ResearchBrief` dataclass with all specified fields |
| Treat external content as information, not instructions (prompt injection defense) | ✅ | Two-layer defense: (1) `sanitize_external_content()` regex strips 12 injection patterns from headlines/narratives before prompt assembly, (2) all 5 model system prompts include explicit data-boundary instruction telling the LLM to treat HEADLINES/NARRATIVE as data, not commands. |
| Parallel research for multiple markets | ⚠️ | Sequential, not parallel. `parallel_workers: 3` configured but code loops sequentially. |

---

## Step 3: PREDICT

| Document Requirement | Status | Detail |
|---|---|---|
| Edge calculation (p_model - p_market) | ✅ | Exact formula implemented |
| Only trade when edge > 4% | ✅ | `min_edge: 0.04` enforced in predictor and risk manager |
| Ensemble methods: multiple models independently, then aggregate | ✅ | 5 models with weighted aggregation and weight renormalization |
| Grok as primary forecaster (30%) | ✅ | Implemented with xAI API |
| Claude as news analyst (20%) | ✅ | Implemented with Anthropic API |
| GPT-4o as bull advocate (20%) | ✅ | Implemented with OpenAI API |
| Gemini Flash as bear advocate (15%) | ✅ | Implemented with Google genai |
| DeepSeek as risk manager (15%) | ✅ | Implemented with OpenAI-compatible API |
| XGBoost or statistical models alongside LLMs | ❌ | Doc says "combination of statistical models (like XGBoost) and LLM reasoning." No statistical/ML models exist. Only LLMs + heuristic fallbacks. |
| Track calibration (Brier Score) | ✅ | Computed in compounder. `brier_score_max: 0.25` in config. |
| Minimum confidence threshold | ✅ | `min_confidence: 0.65` |
| Log every prediction | ✅ | Predictions saved as timestamped JSON snapshots |
| Expected Value formula | ✅ | `EV = edge * confidence` computed |
| Mispricing Z-score | ✅ | `abs(edge) / BASELINE_STD` computed |
| Target Brier Score < 0.25 | ✅ | Configured in `compound.performance_targets.brier_score` |

---

## Step 4: RISK MANAGEMENT & EXECUTION

| Document Requirement | Status | Detail |
|---|---|---|
| Edge check (>4%) | ✅ | Risk check #1 |
| Position size ≤ Kelly Criterion | ✅ | Kelly sizing + risk check #3 |
| Exposure check (new + existing ≤ max total exposure) | ✅ | Check #12: `_compute_total_exposure()` sums all open `trade_*.json` position sizes + proposed new trade. Blocks if total > `max_total_exposure_pct` (default 40%) of bankroll. |
| VaR check at 95% confidence | ✅ | Check #11: `_compute_portfolio_var()` models each position as a binary bet, computes parametric normal VaR at 95% (z=1.645). Blocks if portfolio VaR > `max_var_pct` (default 10%) of bankroll. |
| Max drawdown check (block at 8%) | ✅ | Risk check #6, `max_drawdown_pct: 0.08` |
| Daily loss limit | ✅ | Risk check #5, `max_daily_loss_pct: 0.15` |
| Kelly Criterion with fractional Kelly | ✅ | `kelly_size.py`, quarter-Kelly by default (0.25) |
| Max 5% per single position | ✅ | `max_position_pct: 0.05` |
| Max 15 concurrent positions | ✅ | `max_concurrent_positions: 15` |
| Max 15% daily loss auto-shutdown | ✅ | `max_daily_loss_pct: 0.15` |
| Max $50/day AI API costs | ✅ | Risk check #10, `max_daily_api_cost: 50.0` |
| Place orders via platform API | ✅ | `KalshiClient.place_order()` submits limit orders to demo API. Executor routes trades to Kalshi when `KALSHI_ENV=demo`. |
| Use limit orders, not market orders | ✅ | All orders placed as `type: limit` with dollar-denominated pricing. Contract count calculated from position size / limit price. |
| Monitor slippage (abort if >2% signal-to-fill) | ✅ | Trade dataclass tracks `signal_price` and `slippage` fields. Risk check #8 now detects both extreme prices AND signal-to-fill drift > `max_slippage_pct`. In paper mode drift is 0; ready for live fill prices. |
| Auto-hedge when conditions shift | ✅ | `scripts/position_monitor.py` — `PositionMonitor` class checks all open positions each pipeline cycle. 5 exit triggers adapted from ryanfrigo/kalshi-ai-trading-bot: stop-loss (15%), take-profit (20%), time-based (240h), edge-decay (edge < 0), emergency stop (10%). Places sell orders on Kalshi demo when exiting. Pipeline is now 7 steps: Scan→Research→Predict→Execute→**Monitor**→Resolve→Compound. Config in `settings.yaml` under `position_monitor:`. 32 tests in `tests/test_position_monitor.py`. |
| Kill switch (STOP file) | ✅ | Fully implemented with 10-second polling during sleep |
| Retry on API failures | ✅ | `scripts/retry.py` provides `retry_call()` and `@with_retry` decorator with exponential backoff + jitter. Wired into scanner (Kalshi), researcher (Google News, Reddit, X Search), and resolver (Kalshi market lookup). Uses `retry_attempts: 3` and `retry_delay_seconds: 5` from config. |
| Graceful API disconnection handling | ✅ | Retry utility handles ConnectionError, Timeout, HTTPError, and OpenAI API errors. Falls through gracefully after max attempts. No orphan position cleanup yet (needs live trading). |
| Kalshi RSA-PSS authentication | ✅ | `scripts/kalshi_client.py` — full RSA-PSS with SHA-256, PSS padding, MGF1. Signs `{timestamp_ms}{METHOD}{path}`. Private key from PEM file. |
| Kalshi demo environment for testing | ✅ | `KALSHI_ENV=demo` switches all API calls to `demo-api.kalshi.co`. Executor, resolver, dashboard all Kalshi-aware. $1000 demo funds active. |

---

## Step 5: COMPOUND LEARNING

| Document Requirement | Status | Detail |
|---|---|---|
| Log every trade (entry, exit, prob, outcome, P&L, time held, conditions) | ✅ | Entry data logged. Resolver adds exit_price, outcome, resolved_at for settled markets. Position monitor adds exit_price, exit_reason, exit_timestamp, hours_held for early exits. Market conditions at exit not tracked (no doc spec for format). |
| Classify failures (bad prediction / bad timing / bad execution / external shock) | ✅ | `compounder.py` classifies losses into 4 categories with root cause, lesson, and action taken. |
| Save lessons to knowledge base that scan/research agents read | ✅ | Scanner reads failure_log.md and penalizes scores; researcher adds warnings to narratives. |
| Track win rate | ✅ | Computed in PerformanceReport |
| Track Sharpe ratio (target >2.0) | ✅ | Annualized daily-return proxy |
| Track max drawdown (block at 8%) | ✅ | From P&L series |
| Track profit factor (target >1.5) | ✅ | gross wins / gross losses |
| Track Brier score | ✅ | Mean squared prediction error |
| Nightly consolidation job | ✅ | `nightly_review()` runs full compound cycle. `_maybe_run_nightly_review()` in pipeline loop triggers at `nightly_review_hour: 23`, runs once per day (date guard), saves dated review to `data/reviews/`. |
| Trade outcome resolution (check settled markets) | ✅ | `scripts/resolver.py` checks Kalshi for settled markets, computes P&L, updates trade files and portfolio state. Runs as pipeline step 5. |

---

## Project Structure & SKILL.md

| Document Requirement | Status | Detail |
|---|---|---|
| SKILL.md with frontmatter (name, description, metadata, tags) | ❌ | No SKILL.md exists |
| Risk validation in Python scripts, not markdown | ✅ | `validate_risk.py` is pure Python |
| `formulas.md` reference | ✅ | Exists with all core formulas |
| `platforms.md` API docs | ✅ | Exists with Kalshi endpoints and auth info |
| `failure_log.md` knowledge base | ✅ | File exists with template. Compounder writes structured failure entries. Scanner and researcher read it before processing. |

---

## "What Can Go Wrong" Mitigations

| Risk from Document | Mitigation Implemented? | Detail |
|---|---|---|
| Bad calibration → Brier Score tracking | ⚠️ | Tracked with real outcomes from resolver. Targets not yet enforced (doesn't auto-disable if Brier >0.25). |
| Overfitting → out-of-sample testing | ❌ | No backtest or train/test split |
| Liquidity traps → orderbook depth check | ❌ | No orderbook data (Tier 5) |
| API failures → graceful disconnection handling | ✅ | Retry with exponential backoff on all external calls. Graceful fallthrough after max attempts. |
| Runaway API costs → daily budget cap | ✅ | Risk check #10 with $50/day limit |

---

## Getting Started Timeline

| Document Guideline | Status | Detail |
|---|---|---|
| Week 1: Set up accounts, use Kalshi demo | ✅ | Demo account created, API keys configured, $1000 mock funds, full order lifecycle verified |
| Week 2: Build scan, log data, don't trade | ✅ | Scanner works standalone |
| Week 3: Build research + prediction, backtest | ❌ | No backtesting capability |
| Week 4: Build risk, paper trade 2+ weeks | ✅ | Paper trading works |
| Week 5+: Go live with $100-500 | ❌ | No live trading path |

---

## Priority Tiers

### Tier 1 — ✅ IMPLEMENTED (2026-03-16)
1. **Trade outcome resolution** — `scripts/resolver.py`: polls Kalshi for settled markets, computes P&L, updates trade files and portfolio state
2. **Failure log feedback loop** — scanner deprioritizes markets with past failures (score penalty), researcher adds warnings to narrative summaries
3. **Failure categorization** — compounder classifies losses as Bad Prediction / Bad Timing / Bad Execution / External Shock, writes structured entries to failure_log.md

### Tier 2 — ✅ IMPLEMENTED (2026-03-16)
4. **VaR risk check** — `_compute_portfolio_var()` models binary bets, parametric normal VaR at 95%, check #11
5. **Total exposure check** — `_compute_total_exposure()` sums open positions, check #12, blocks if > 40% bankroll
6. **Prompt injection protection** — regex sanitizer (12 patterns) + data-boundary fences in all 5 LLM system prompts

### Tier 3 — ✅ IMPLEMENTED (2026-03-16)
7. **Retry/reconnection logic** — `scripts/retry.py` with exponential backoff + jitter, wired into scanner, researcher, resolver
8. **Real slippage monitoring** — Trade tracks `signal_price`/`slippage`, risk check #8 detects signal-to-fill drift
9. **Nightly review scheduler** — `_maybe_run_nightly_review()` triggers at configured hour, once per day, saves dated review
10. **Volume spike detection (7-day avg)** — `volume_history.json` stores daily volumes, 7-day rolling average, flags at configurable multiplier

### Tier 4 — ✅ IMPLEMENTED (2026-03-16)
11. **Kalshi RSA-PSS auth** — `scripts/kalshi_client.py` with full RSA-PSS signing, demo/production URL switching
12. **Demo environment integration** — Connected to `demo-api.kalshi.co`, $1000 mock funds, live order placement verified
13. **Limit order placement** — `place_order()` with dollar-denominated pricing, contract count calculation, fill tracking
14. **Executor demo mode** — `KALSHI_ENV=demo` routes trades to Kalshi demo API; places real limit orders, tracks order status
15. **Dashboard Kalshi panel** — `/api/kalshi` endpoint, exchange balance/positions/orders/fills, green connection badge

### Tier 4 — Needed before going live
11. **Kalshi RSA-PSS authentication**
12. **Kalshi demo environment toggle**
13. **Limit order placement**
14. **Auto-hedging**

### Tier 5 — Polymarket & expansion
15. **Polymarket CLOB + EIP-712**
16. **WebSocket orderbook**
17. **XGBoost/statistical models** — ✅ IMPLEMENTED (2026-03-16): `scripts/xgboost_model.py` with `XGBoostCalibrator` class. 18 features from market data + sentiment (yes_price, spread, volume, sentiment_spread, gap, source_agreement, etc.). Cold-start training from 2000 synthetic samples. Model persisted to `config/xgboost_model.json`. Wired into predictor ensemble as `statistical_calibrator` at 10% weight. Dashboard shows XGBoost as "LOCAL" type with "TRAINED" status. Retrain-from-history method ready for when resolved trades accumulate. 22 tests in `tests/test_xgboost.py`.
18. **Backtesting framework** — ✅ IMPLEMENTED (2026-03-16): `scripts/backtester.py` with `Backtester` class. Loads saved predictions, deduplicates per market, fetches outcomes from Kalshi API, computes Brier Score, P&L, win rate, Sharpe ratio, max drawdown, 10-bucket calibration curve. Caches outcomes. Dashboard page at `/backtest` with stats grid, calibration chart, Brier gauge, trade log table. Navigation: DASHBOARD | BACKTEST | SETTINGS across all pages. 23 tests in `tests/test_backtester.py`.
19. **Additional RSS/news sources, parallel research** — ✅ IMPLEMENTED (2026-03-16): Added Bing News RSS as 3rd source (`_fetch_bing_news()`). All sources fetched in parallel via `ThreadPoolExecutor` within each market. Multiple markets researched in parallel via `research_markets()` using `parallel_workers` config. Pipeline wired to use parallel method. 19 tests in `tests/test_researcher.py`.
20. **SKILL.md, pmxt** — not implemented
21. **Active trading hours** — ✅ IMPLEMENTED (2026-03-16): `trading_hours` config in `settings.yaml` (enabled, start_hour, end_hour, timezone, skip_weekends). Pipeline `_check_trading_hours()` gates the Execute step — scan/research/predict still run for data collection. Dashboard settings panel with sliders, timezone dropdown, weekend toggle. 23 tests in `tests/test_orderbook_hours.py`.
22. **Orderbook depth check** — ✅ IMPLEMENTED (2026-03-16): `scripts/orderbook.py` with `parse_orderbook()`, `check_depth()`, `get_spread()`, `get_midpoint()`. Executor fetches orderbook and checks depth before placing orders. Logs warning on thin books but proceeds (limit orders rest safely). Config: `risk.min_depth_ratio` (default 2.0).
