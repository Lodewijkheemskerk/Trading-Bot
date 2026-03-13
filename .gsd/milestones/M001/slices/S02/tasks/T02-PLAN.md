---
estimated_steps: 4
estimated_files: 1
---

# T02: Add CLI interface and verify end-to-end with live APIs

**Slice:** S02 — Research Agent
**Milestone:** M001

## Description

Add the `if __name__ == "__main__"` CLI block to `researcher.py` following the scanner.py pattern. Load the latest scan snapshot, research the top N markets, print a formatted table, and save a timestamped JSON snapshot. Then run end-to-end against live Google News RSS and Reddit APIs to verify the full pipeline works with real data.

## Steps

1. Add CLI block: argparse with `--market` (single ticker) and `--top` (number of markets, default 5) flags. Load latest scan snapshot from `MARKET_DIR`, convert to market dicts. Handle Windows console encoding with `sys.stdout.reconfigure(encoding='utf-8', errors='replace')`
2. Add formatted table output: market title (truncated), consensus sentiment, confidence, implied probability, gap, gap direction, article counts. Use aligned columns matching scanner.py style
3. Add timestamped JSON snapshot save: all research briefs to `RESEARCH_DIR` as `research_YYYYMMDD_HHMMSS.json`
4. Run `python scripts/researcher.py` against live APIs, verify table output is readable, JSON is saved, no crashes. Test `--market` flag with a specific ticker from the scan snapshot

## Must-Haves

- [ ] CLI loads latest scan snapshot and researches top N markets
- [ ] `--market` flag researches a single market by ticker
- [ ] Formatted table output with sentiment, confidence, gap columns
- [ ] Timestamped JSON snapshot saved to `RESEARCH_DIR`
- [ ] Windows console encoding handled (no UnicodeEncodeError on emoji/unicode)
- [ ] Graceful exit on no scan snapshots found

## Verification

- `python scripts/researcher.py` completes without errors, prints table, saves JSON file
- `python scripts/researcher.py --market <ticker>` works for a valid ticker from latest scan
- JSON file exists in `data/research_briefs/` after run
- Output table is visually readable and shows sentiment data for each market

## Inputs

- `scripts/researcher.py` — T01's completed module with `NewsResearcher`, `ResearchBrief`, `SentimentResult`
- `data/market_snapshots/` — Latest scan snapshot JSON from S01's scanner
- `scripts/scanner.py` — CLI pattern to follow (argparse, logging, table format, JSON save)

## Expected Output

- `scripts/researcher.py` — Updated with CLI block, table formatting, JSON snapshot save
