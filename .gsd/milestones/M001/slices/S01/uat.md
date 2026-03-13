# S01: Project Foundation + Kalshi Scanner — UAT

## Setup
```bash
pip install -r requirements.txt
```

## Test 1: Config loads
```bash
python -c "from config import load_settings; print(load_settings()['bot_name'])"
```
**Expected:** Prints `predict-market-bot`

## Test 2: Scanner runs and shows real markets
```bash
python scripts/scanner.py
```
**Expected:**
- Shows "Kalshi Market Scan" header with scanned/passed/anomalies counts
- Scanned count should be >1000 (fetches from live Kalshi API)
- Passed count should be >0 (at least some non-sports markets with volume)
- Table shows ticker, title, YES price, volume, spread, days to expiry, score
- "Snapshot:" line shows a saved JSON file path

## Test 3: Snapshot saved
```bash
ls data/market_snapshots/
```
**Expected:** At least one `scan_YYYYMMDD_HHMMSS.json` file exists

## Test 4: No sports/MVE markets in results
In the scanner output table, no tickers should contain `MVE`, `GAME`, `NBA`, `NFL`, `MLB`, `NHL`.
