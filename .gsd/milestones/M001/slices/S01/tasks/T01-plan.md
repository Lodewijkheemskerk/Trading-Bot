# T01: Project scaffold, config system, and shared types

**Slice:** S01
**Milestone:** M001

## Goal
Set up the complete project directory structure, dependency management, configuration system, and shared data types that all downstream slices will build on.

## Must-Haves

### Truths
- `pip install -r requirements.txt` completes without errors
- `python -c "from config import load_settings, PROJECT_ROOT, DATA_DIR; print(load_settings()['bot_name'])"` prints `predict-market-bot`
- All data directories exist: `data/trades/`, `data/market_snapshots/`, `data/research_briefs/`
- `config/settings.yaml` contains all sections: scanner, research, prediction, risk, execution, compound

### Artifacts
Files that must exist with real implementation:
- `requirements.txt` — Python dependencies (min 4 packages: requests, pyyaml, python-dotenv, schedule)
- `.env.example` — Template with all API key placeholders
- `config/__init__.py` — Config loader with `load_settings()`, `get_setting()`, path constants (min 30 lines)
- `config/settings.yaml` — Full configuration with all pipeline sections (min 50 lines)
- `scripts/__init__.py` — Package init
- `references/formulas.md` — All trading math formulas from the doc (Kelly, Brier, EV, edge, mispricing, Sharpe, VaR)
- `references/platforms.md` — Kalshi API reference with endpoints and auth details
- `references/failure_log.md` — Empty template for future failure logging
- `README.md` — Project overview with architecture diagram and quickstart

### Key Links
- `config/__init__.py` imports and parses `config/settings.yaml`
- `config/__init__.py` exports `PROJECT_ROOT`, `DATA_DIR`, `TRADES_DIR`, `MARKET_DIR`, `RESEARCH_DIR`, `LOGS_DIR`, `REFERENCES_DIR`, `KILL_SWITCH_FILE`

## Steps
1. Create directory structure: `scripts/`, `config/`, `references/`, `data/{trades,market_snapshots,research_briefs}`, `logs/`
2. Write `requirements.txt` with core dependencies
3. Write `.env.example` with API key placeholders (Kalshi, Anthropic, NewsAPI, Reddit)
4. Write `config/settings.yaml` with all configuration sections and sensible defaults (paper mode, $500 bankroll, quarter-Kelly)
5. Write `config/__init__.py` with YAML loader, path constants, directory auto-creation
6. Write `scripts/__init__.py` package init
7. Write `references/formulas.md` with all trading math from the source doc
8. Write `references/platforms.md` with Kalshi API reference (verified endpoints from research)
9. Write `references/failure_log.md` empty template
10. Write `README.md` with project overview
11. Verify: install deps, import config, check paths

## Context
- Research confirmed Kalshi API field names use `_dollars` and `_fp` string suffixes (must parse as float)
- Base URL: `https://api.elections.kalshi.com/trade-api/v2` for production
- Demo URL: `https://demo-api.kalshi.co/trade-api/v2`
- Config should default to paper mode with demo URL
- See `.gsd/milestones/M001/research.md` for full API field reference
