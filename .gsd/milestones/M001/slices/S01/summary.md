---
id: S01
milestone: M001
provides:
  - MarketScanner class scanning live Kalshi events API with pagination
  - Market dataclass with all trading-relevant fields (prices, volume, spread, expiry, anomalies)
  - ScanResult dataclass for pipeline consumption
  - config/settings.yaml with all 7 pipeline sections
  - config/__init__.py with load_settings() and 8 path constants
  - references/formulas.md (Kelly, Brier, EV, Sharpe, VaR, mispricing)
  - references/platforms.md (Kalshi API verified endpoints and field types)
key_files:
  - scripts/scanner.py
  - config/__init__.py
  - config/settings.yaml
  - references/platforms.md
key_decisions:
  - "Use Kalshi /events endpoint, not /markets — /markets returns zero volume"
  - "Dollar-string parsing with float() — all Kalshi _dollars and _fp fields are strings"
  - "Config: YAML for structure, .env for secrets only"
  - "Default min_volume=20, max_days=30, skip MVE/NBA/NFL/MLB/NHL"
patterns_established:
  - "Config import: from config import load_settings, DATA_DIR, etc."
  - "Dollar-string parsing: MarketScanner._safe_float()"
  - "CLI + JSON snapshot pattern for pipeline stages"
drill_down_paths:
  - .gsd/milestones/M001/slices/S01/tasks/T01-summary.md
  - .gsd/milestones/M001/slices/S01/tasks/T02-summary.md
completed_at: 2026-03-13T10:42:00Z
---

# S01: Project Foundation + Kalshi Scanner

**Complete project scaffold with config system, reference docs, and live Kalshi market scanner pulling 4000+ markets via events API**

## What Was Built

Full project foundation: directory structure, dependency management (requests, pyyaml, python-dotenv, schedule), YAML config with all pipeline sections, path constants with auto-directory creation, and reference documentation (trading formulas, Kalshi API docs).

Live market scanner connecting to Kalshi's REST API via the events endpoint, which — unlike the plain /markets endpoint — populates volume and price data for nested markets. Scanner fetches up to 4000+ markets, parses Kalshi's dollar-string fields, filters by volume/liquidity/expiry/category (skipping sports/MVE), detects anomalies (price moves, wide spreads), and ranks by composite opportunity score. Results saved as timestamped JSON snapshots.

Key discovery: Kalshi's `/markets` list endpoint returns zero volume for all markets. Must use `/events?with_nested_markets=true` instead.
