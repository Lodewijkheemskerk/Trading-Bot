# T02: Kalshi API client and market scanner

**Slice:** S01
**Milestone:** M001

## Goal
Build the MarketScanner class that connects to the live Kalshi REST API, fetches markets with cursor pagination, parses dollar-string fields, filters by volume/liquidity/expiry/category, flags anomalies, calculates opportunity scores, and saves scan snapshots to disk.

## Must-Haves

### Truths
- `python scripts/scanner.py` executes without errors and prints a table of tradeable markets
- Scanner connects to `https://api.elections.kalshi.com/trade-api/v2/markets` and gets real data
- Zero-volume markets are filtered out (`volume_24h_fp == "0.00"`)
- MVE/sports markets are filtered out (tickers containing `MVE`, `GAME`, `SPREAD`)
- Markets expiring >30 days are filtered out
- Markets with YES price <5% or >95% are filtered out (near-certain)
- Anomaly detection flags: price moves >10%, spreads >$0.05, volume spikes >2x
- A JSON snapshot is saved to `data/market_snapshots/scan_YYYYMMDD_HHMMSS.json`
- Scanner handles API pagination (follows `cursor` until exhausted)

### Artifacts
Files that must exist with real implementation:
- `scripts/scanner.py` — MarketScanner class + CLI entry point (min 200 lines)
  - Exports: `MarketScanner`, `Market`, `ScanResult`
  - Methods: `scan_all()`, `save_snapshot()`
  - `Market` dataclass with all fields from boundary map

### Key Links
- `scripts/scanner.py` → `config/__init__.py` via import of `load_settings`, `MARKET_DIR`
- `scripts/scanner.py` → `config/settings.yaml` for scanner config values (min_volume, max_days_to_expiry, etc.)

## Steps
1. Define `Market` dataclass matching Kalshi API fields (use research.md field reference)
2. Define `ScanResult` dataclass for scan output
3. Implement `MarketScanner.__init__()` loading config from settings.yaml
4. Implement `_fetch_markets()` with cursor pagination loop against Kalshi REST API
5. Implement `_parse_kalshi_market()` parsing dollar strings to floats, calculating days_to_expiry
6. Implement `_passes_filters()` with volume, liquidity, expiry, price, and category checks
7. Implement `_check_anomalies()` for price moves, spread width, volume spikes
8. Implement `_calculate_opportunity_score()` composite scoring
9. Implement `scan_all()` orchestrating fetch → parse → filter → score → rank
10. Implement `save_snapshot()` writing JSON to `data/market_snapshots/`
11. Implement `__main__` CLI entry point with formatted output table
12. Test with live API call, verify filtering and output

## Context
- Kalshi prices are in `_dollars` fields as strings like `"0.4500"` — parse with `float()`
- Cursor pagination: response has `cursor` field, pass as query param for next page
- Volume fields end in `_fp` (floating point strings)
- MVE markets have complex multi-leg structures — filter by ticker prefix
- Focus on categories: Politics, Economics, Climate, Technology, Crypto
- See `.gsd/milestones/M001/research.md` for verified endpoints and field names
- See T01 summary for config structure (after T01 completes)
