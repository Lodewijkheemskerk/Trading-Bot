---
id: S02
parent: M001
milestone: M001
provides:
  - NewsResearcher class with Google News RSS and Reddit fetching
  - SentimentResult and ResearchBrief dataclasses (S02→S03 boundary contract)
  - Keyword sentiment analyzer with financial domain vocabulary and bigram negation handling
  - Consensus aggregation with confidence weighting and gap analysis
  - CLI with --market and --top flags, formatted table output, timestamped JSON snapshots
requires:
  - slice: S01
    provides: config.load_settings(), config.RESEARCH_DIR, Market dataclass, scan snapshots in data/market_snapshots/
affects:
  - S03
key_files:
  - scripts/researcher.py
  - tests/test_researcher.py
key_decisions:
  - score_text is a static method for easy unit testing without instantiation
  - Confidence scales linearly with article count (0.1 + n/25, capped at 0.9)
  - Sentiment-implied probability centered at 0.5 with net_sentiment * confidence * 0.5 offset
  - Gap threshold of ±0.02 for "fair" classification
  - Query extraction fallback to first 5 words when content words < 2
  - key_narratives count used as article signal proxy in CLI table
patterns_established:
  - Keyword sentiment with BULLISH_WORDS/BEARISH_WORDS sets and NEGATION_WORDS bigram handling
  - SentimentResult/ResearchBrief dataclass contract matching S03 consumption spec exactly
  - Researcher follows scanner.py patterns: session-based HTTP, logging, argparse CLI, JSON persistence
  - Windows console encoding handled at CLI entry point with sys.stdout.reconfigure
observability_surfaces:
  - INFO log: "Researching {market_id}: query={query}" on each market
  - INFO/WARNING logs: per-source article counts and zero-result warnings
  - CLI table output for visual scan of sentiment across markets
  - JSON snapshots in data/research_briefs/ for downstream pipeline consumption
drill_down_paths:
  - .gsd/milestones/M001/slices/S02/tasks/T01-SUMMARY.md
  - .gsd/milestones/M001/slices/S02/tasks/T02-SUMMARY.md
duration: 40m
verification_result: passed
completed_at: 2026-03-13
---

# S02: Research Agent

**NewsResearcher pipeline: market title → query extraction → Google News RSS + Reddit fetch → keyword sentiment with bigram negation → consensus aggregation → gap analysis → ResearchBrief with all S03 contract fields.**

## What Happened

Built the complete research agent in two tasks. T01 implemented the core `NewsResearcher` class with all pipeline stages: query extraction from Kalshi market titles (strips "Will" prefixes, date suffixes, stop words), Google News RSS fetching with XML parsing, Reddit JSON search with proper User-Agent and rate limiting, keyword-based sentiment scoring with 30+ financial domain words per polarity and bigram negation handling (no/not/never/lose/lack/without/fail), confidence-weighted consensus aggregation across sources, gap analysis vs market price, and JSON persistence. 29 unit tests cover query extraction, sentiment scoring, negation, zero-result handling, consensus aggregation, and contract field validation.

T02 added the CLI interface following the scanner.py pattern: argparse with `--market` and `--top` flags, formatted table output with sentiment/confidence/gap columns, timestamped research snapshot saving. Verified end-to-end against live Google News RSS and Reddit APIs — 3 markets researched successfully, each pulling 20 articles per source.

## Verification

1. **`python -c "from scripts.researcher import NewsResearcher, ResearchBrief, SentimentResult; print('ok')"`** — clean imports ✅
2. **`python tests/test_researcher.py -v`** — 29 tests pass (0.002s) ✅
3. **`python scripts/researcher.py --top 3`** — completed without errors, printed table, saved JSON ✅
4. **`python scripts/researcher.py --market KXBONDIOUT-26APR01`** — single-market mode works ✅
5. **S03 contract fields in JSON** — `current_yes_price`, `consensus_sentiment`, `consensus_confidence`, `gap`, `narrative_summary` all present ✅
6. **Observability** — INFO logging for fetch counts, WARNING for zero results, JSON briefs persisted ✅

## Deviations

None.

## Known Limitations

- Most markets show neutral sentiment with high confidence — keyword-based analysis catches financial domain terms but many headlines lack them. Expected behavior; LLM-based sentiment in a future milestone will improve signal quality.
- CLI table "News" and "Rdt" columns show key_narratives count (articles with sentiment signal) rather than raw fetch count. Accurate enough for scanning.

## Follow-ups

None. S03 (Prediction Engine) consumes ResearchBrief dicts as designed.

## Files Created/Modified

- `scripts/researcher.py` — Complete NewsResearcher module: dataclasses, query extraction, Google News + Reddit fetching, keyword sentiment with negation, consensus aggregation, gap analysis, JSON persistence, CLI with argparse
- `tests/test_researcher.py` — 29 unit tests covering all pipeline stages and contract compliance

## Forward Intelligence

### What the next slice should know
- `ResearchBrief` is consumed as a dict (via `dataclasses.asdict()`). The S03 predictor should expect: `current_yes_price` (float 0-1), `consensus_sentiment` (bullish/bearish/neutral), `consensus_confidence` (0-1), `gap` (float, positive = sentiment > market), `gap_direction` (underpriced/overpriced/fair), `narrative_summary` (string), `source_results` (list of SentimentResult dicts).
- Research snapshots are saved as `data/research_briefs/research_YYYYMMDD_HHMMSS.json` with structure `{timestamp, markets_researched, briefs: [...]}`.

### What's fragile
- Keyword sentiment produces mostly neutral results for non-financial markets (political, weather). The predictor should not over-weight sentiment confidence for these — the confidence number reflects article volume, not signal quality.
- Reddit rate limiting (429s) is handled gracefully but could cause zero-result briefs during heavy use.

### Authoritative diagnostics
- `python tests/test_researcher.py -v` — fast (0.002s), tests all core logic offline
- `data/research_briefs/research_*.json` — inspect persisted briefs for actual API output quality
- `NewsResearcher.score_text("some text")` — interactive sentiment debugging

### What assumptions changed
- No assumptions changed. Google News RSS and Reddit JSON APIs work as expected. Keyword sentiment performs as designed — adequate for M001 baseline, upgradeable later.
