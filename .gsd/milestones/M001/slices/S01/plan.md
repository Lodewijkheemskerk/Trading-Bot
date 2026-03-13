# S01: Project Foundation + Kalshi Scanner

**Goal:** Set up the project structure, configuration system, and a working market scanner that connects to the live Kalshi API and returns a ranked list of tradeable markets.
**Demo:** User runs `python scripts/scanner.py` and sees real Kalshi markets ranked by opportunity score with volume, prices, spreads, anomaly flags, and category filtering.

## Must-Haves
- Project has `requirements.txt`, `.env.example`, `config/settings.yaml`, and clean directory structure
- `python scripts/scanner.py` executes without errors and connects to the live Kalshi API
- Scanner filters out zero-volume markets, sports parlays (MVE tickers), and markets expiring >30 days
- Output shows ranked markets with: title, ticker, YES price, volume, spread, days to expiry, opportunity score
- Scan results are saved as JSON snapshots in `data/market_snapshots/`
- Configuration loads from `config/settings.yaml` with sensible defaults

## Tasks

- [x] **T01: Project scaffold, config system, and shared types**
  Set up directory structure, requirements.txt, .env.example, config/settings.yaml, config/__init__.py with loader and path constants. Define core dataclasses (Market, ScanResult) that downstream slices will consume.

- [x] **T02: Kalshi API client and market scanner**
  Build the MarketScanner class that connects to Kalshi REST API, handles pagination, parses market data (dollar string fields), filters by volume/liquidity/expiry/category, flags anomalies, scores opportunities, and saves snapshots. Include CLI entry point.

## Files Likely Touched
- `requirements.txt`
- `.env.example`
- `config/__init__.py`
- `config/settings.yaml`
- `scripts/__init__.py`
- `scripts/scanner.py`
- `references/formulas.md`
- `references/platforms.md`
- `README.md`
