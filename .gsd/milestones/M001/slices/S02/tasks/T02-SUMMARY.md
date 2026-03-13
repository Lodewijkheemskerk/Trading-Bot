---
id: T02
parent: S02
milestone: M001
provides:
  - CLI interface for researcher.py with --market and --top flags
  - Formatted table output with sentiment, confidence, gap columns
  - Timestamped research snapshot saving (research_YYYYMMDD_HHMMSS.json)
  - save_research_snapshot() and _print_research_table() functions
key_files:
  - scripts/researcher.py
key_decisions:
  - key_narratives count used as article signal proxy in table (shows how many articles had sentiment, not raw fetch count — more useful for scanning)
patterns_established:
  - CLI pattern matches scanner.py: argparse, logging.basicConfig, table print, JSON snapshot save
  - Windows console encoding handled at CLI entry point with sys.stdout.reconfigure
observability_surfaces:
  - CLI table output for visual scan of sentiment across markets
  - JSON snapshots in data/research_briefs/ for downstream pipeline consumption
  - INFO-level logging during research (article counts per source per market)
duration: 15m
verification_result: passed
completed_at: 2026-03-13
blocker_discovered: false
---

# T02: Add CLI interface and verify end-to-end with live APIs

**Added argparse CLI to researcher.py with --market/--top flags, formatted table output, timestamped JSON snapshot saving, and verified full pipeline against live Google News RSS and Reddit APIs.**

## What Happened

Added the `if __name__ == "__main__"` CLI block to `scripts/researcher.py` following the scanner.py pattern. The CLI loads the latest scan snapshot, researches the top N markets (default 5), prints an aligned table with sentiment/confidence/gap columns, and saves a timestamped research snapshot. Also added `save_research_snapshot()` for batch JSON saving and `_print_research_table()` for formatted output.

Ran against live APIs: 3 markets researched successfully, each getting 20 Google News articles and 20 Reddit posts. Table renders cleanly, JSON saved with all S03 contract fields.

## Verification

All slice-level verifications pass (this is the final task of S02):

1. **`python scripts/researcher.py --top 3`** — completed without errors, printed formatted table, saved `data/research_briefs/research_20260313_105721.json` (3 briefs)
2. **`python scripts/researcher.py --market KXBONDIOUT-26APR01`** — single-market research works, saved separate snapshot
3. **`python -c "from scripts.researcher import NewsResearcher, ResearchBrief, SentimentResult; print('import ok')"`** — clean imports confirmed
4. **`python tests/test_researcher.py -v`** — all 29 tests pass
5. **S03 contract fields verified in JSON**: `current_yes_price`, `consensus_sentiment`, `consensus_confidence`, `gap`, `narrative_summary` all present
6. **Graceful exit for unknown ticker**: shows helpful error with sample tickers
7. **JSON files confirmed** in `data/research_briefs/` (2 snapshot files)

## Diagnostics

- Run `python scripts/researcher.py --top 3` to verify live API connectivity and table output
- Inspect `data/research_briefs/research_*.json` for saved research snapshots
- Check logs during execution for per-source article counts and zero-result warnings
- Use `--market <ticker>` to debug a specific market's research pipeline
- `python tests/test_researcher.py -v` for unit test regression

## Deviations

None.

## Known Issues

- "News" and "Rdt" columns in table show key_narratives count (articles with sentiment signal) rather than raw fetch count. Accurate enough for visual scanning.
- Most markets show neutral sentiment with 90% confidence — the 90% confidence is from high article counts (20 per source), while neutral is because many headlines don't contain financial domain keywords. This is expected for keyword-based sentiment; LLM-based upgrade in later milestones will improve signal.

## Files Created/Modified

- `scripts/researcher.py` — Added CLI block (argparse, table printing, JSON snapshot save), `save_research_snapshot()`, `_print_research_table()`
