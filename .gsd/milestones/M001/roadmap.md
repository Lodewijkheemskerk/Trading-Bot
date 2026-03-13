# M001: AI-Powered Prediction Market Trading Bot

**Vision:** A complete 5-step trading pipeline (Scan → Research → Predict → Execute → Compound) that scans Kalshi prediction markets, researches events via news/sentiment, estimates true probabilities using Claude AI, manages risk with Kelly Criterion, paper-trades, and learns from every outcome. Runnable as a CLI on a 15-minute loop.

**Success Criteria:**
- `python scripts/pipeline.py --mode once` completes all 5 pipeline steps with real Kalshi market data
- Risk validation deterministically blocks trades violating any of the 10 rules
- `python scripts/pipeline.py --status` shows accurate performance metrics after 5+ paper trades
- `python scripts/pipeline.py --mode loop --interval 15` runs autonomously without crashes
- Creating a `STOP` file halts all trading immediately

---

## Slices

- [x] **S01: Project Foundation + Kalshi Scanner** `risk:medium` `depends:[]`
  > After this: User runs `python scripts/scanner.py` and sees a ranked list of tradeable Kalshi markets with volume, prices, spreads, and anomaly flags pulled from the live API.

- [ ] **S02: Research Agent** `risk:medium` `depends:[S01]`
  > After this: User runs `python scripts/researcher.py` and sees sentiment analysis from Google News + Reddit for any market, with bullish/bearish/neutral classification and gap analysis vs market price.

- [ ] **S03: Prediction Engine** `risk:high` `depends:[S02]`
  > After this: User runs `python scripts/predictor.py` with a research brief and gets an ensemble probability estimate, edge calculation, mispricing Z-score, and a trade/no-trade signal.

- [ ] **S04: Risk Management + Paper Execution** `risk:medium` `depends:[S03]`
  > After this: User runs `python scripts/executor.py` with a trade signal and sees all 10 risk checks pass/fail, Kelly-sized position, and a simulated paper trade logged to disk.

- [ ] **S05: Compound Learning + Full Pipeline** `risk:low` `depends:[S04]`
  > After this: User runs `python scripts/pipeline.py --mode once` for the complete end-to-end pipeline, and `--status` shows performance metrics. The bot learns from every trade outcome.

---

## Boundary Map

### S01 → S02
Produces:
  `scripts/scanner.py` → `MarketScanner` class with `scan_all()` returning `ScanResult`
  `scripts/scanner.py` → `Market` dataclass (market_id, title, yes_price, volume, spread, days_to_expiry, opportunity_score, is_anomaly)
  `config/settings.yaml` → Centralized configuration
  `config/__init__.py` → `load_settings()`, project path constants (`DATA_DIR`, `MARKET_DIR`, etc.)

Consumes: nothing (leaf node)

### S02 → S03
Produces:
  `scripts/researcher.py` → `NewsResearcher` class with `research_market(market_dict)` returning `ResearchBrief`
  `scripts/researcher.py` → `ResearchBrief` dataclass (consensus_sentiment, consensus_confidence, sentiment_implied_probability, gap, gap_direction, narrative_summary)
  `scripts/researcher.py` → `SentimentResult` dataclass (source, bullish, bearish, neutral, confidence, key_narratives)

Consumes from S01:
  `config/__init__.py` → `RESEARCH_DIR`, `load_settings()`
  `scripts/scanner.py` → `Market` dataclass (converted to dict for researcher input)

### S03 → S04
Produces:
  `scripts/predictor.py` → `PredictionEngine` class with `predict(research_brief_dict)` returning `TradeSignal`
  `scripts/predictor.py` → `TradeSignal` dataclass (ensemble_probability, edge, mispricing_score, expected_value, direction, signal_strength, confidence, should_trade, model_predictions)
  `scripts/predictor.py` → `ModelPrediction` dataclass (model_name, role, weight, predicted_probability, confidence, reasoning)

Consumes from S02:
  `scripts/researcher.py` → `ResearchBrief` (as dict: current_yes_price, consensus_sentiment, consensus_confidence, gap, narrative_summary)

### S04 → S05
Produces:
  `scripts/validate_risk.py` → `RiskManager` class with `validate_trade()` returning `RiskValidation` (10 checks)
  `scripts/kelly_size.py` → `calculate_kelly()` returning `KellyResult` (position_size_usd, edge, expected_value)
  `scripts/executor.py` → `TradeExecutor` class with `execute_signal(signal_dict)` returning `Trade`
  `scripts/executor.py` → `Trade` dataclass (trade_id, entry_price, position_size_usd, status, pnl, risk_passed)

Consumes from S03:
  `scripts/predictor.py` → `TradeSignal` (as dict: ensemble_probability, market_probability, edge, direction, signal_strength)

### S05 → (terminal)
Produces:
  `scripts/compounder.py` → `Compounder` class with `analyze_trade()`, `get_performance_report()`, `nightly_review()`
  `scripts/pipeline.py` → `TradingPipeline` class with `run_once()`, `run_loop()`, `activate_kill_switch()`
  `references/failure_log.md` → Append-only knowledge base of trade failures

Consumes from S04:
  `scripts/executor.py` → `Trade` (as dict: trade_id, entry_price, exit_price, pnl, model_probability, direction, signal_strength)
  `scripts/validate_risk.py` → `RiskManager` (shared instance for portfolio state)
  `config/__init__.py` → `KILL_SWITCH_FILE`, all directory constants
