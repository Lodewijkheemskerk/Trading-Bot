---
id: T01
parent: S01
milestone: M001
provides:
  - config/settings.yaml with all 7 pipeline sections and sensible defaults
  - config/__init__.py with load_settings(), get_setting(), 8 path constants
  - requirements.txt with core deps (requests, pyyaml, python-dotenv, schedule)
  - references/formulas.md with all trading math (Kelly, Brier, EV, Sharpe, VaR)
  - references/platforms.md with verified Kalshi API endpoints and field reference
requires: []
affects: [S02, S03, S04, S05]
key_files:
  - config/__init__.py
  - config/settings.yaml
  - requirements.txt
  - references/platforms.md
  - references/formulas.md
key_decisions:
  - "Kalshi prices are dollar strings — all parsing uses float(). Documented in platforms.md"
  - "Config uses YAML for structure, .env for secrets only"
  - "Paper mode is default. base_url points to production for read-only scanning"
patterns_established:
  - "Config import pattern: from config import load_settings, DATA_DIR, etc."
  - "All data dirs auto-created on config import"
drill_down_paths:
  - .gsd/milestones/M001/slices/S01/tasks/T01-plan.md
duration: 8min
verification_result: pass
completed_at: 2026-03-13T10:35:00Z
---

# T01: Project scaffold, config system, and shared types

**Complete project foundation with config loader, 8 path constants, reference docs, and all pipeline settings in YAML**

## What Happened

Created the full project directory structure with `scripts/`, `config/`, `references/`, `data/` subdirectories, and `logs/`. The config system loads `settings.yaml` with sections for all 5 pipeline stages (scanner, research, prediction, risk, execution, compound) plus bankroll and Kalshi API URLs. Path constants (`PROJECT_ROOT`, `DATA_DIR`, `TRADES_DIR`, `MARKET_DIR`, `RESEARCH_DIR`, `LOGS_DIR`, `REFERENCES_DIR`, `KILL_SWITCH_FILE`) are exported from `config/__init__.py` and directories are auto-created on import.

Reference docs were written from verified research: `formulas.md` contains all trading math from the source doc, `platforms.md` has the Kalshi API field reference verified against live API responses (dollar-string fields, cursor pagination, auth signing).

## Deviations
None.

## Files Created/Modified
- `requirements.txt` — 4 core deps (requests, pyyaml, python-dotenv, schedule)
- `.env.example` — Kalshi + Anthropic + NewsAPI key placeholders
- `config/__init__.py` — Config loader + 8 path constants + auto-dir creation
- `config/settings.yaml` — 90-line config with all pipeline sections
- `scripts/__init__.py` — Package init
- `references/formulas.md` — Trading math reference (Kelly, Brier, EV, Sharpe, VaR, mispricing)
- `references/platforms.md` — Kalshi API reference with verified endpoints + field types
- `references/failure_log.md` — Empty template for future failure logging
- `README.md` — Project overview with architecture and quickstart
