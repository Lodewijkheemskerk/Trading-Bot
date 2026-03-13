# S02: Research Agent

**Goal:** A `NewsResearcher` class that takes a market dict, fetches headlines from Google News RSS + Reddit, runs keyword sentiment analysis, and produces a `ResearchBrief` with consensus sentiment, gap analysis vs market price, and narrative summary.
**Demo:** `python scripts/researcher.py` researches top markets from the latest scan snapshot and prints sentiment results with bullish/bearish/neutral classification per source, gap analysis, and saves JSON briefs to `data/research_briefs/`.

## Must-Haves

- `ResearchBrief` and `SentimentResult` dataclasses matching the S02→S03 boundary contract exactly
- Query extraction from Kalshi market titles (strip "Will" prefix, question marks, extract key content words)
- Google News RSS fetching with `xml.etree.ElementTree` parsing (title + source + pubDate per item)
- Reddit JSON search fetching with proper User-Agent header and 1-second delay between requests
- Keyword-based sentiment with financial domain vocabulary and bigram negation handling
- Consensus aggregation across sources with confidence weighting
- Gap analysis: `sentiment_implied_probability - yes_price` with direction classification
- Graceful handling of zero-result queries (low-confidence neutral, no errors)
- JSON persistence of research briefs to `RESEARCH_DIR`
- CLI with table output and JSON snapshot, following scanner.py pattern
- `current_yes_price` passed through in ResearchBrief (required by S03 contract)

## Proof Level

- This slice proves: contract (S02→S03 boundary) + operational (live API integration)
- Real runtime required: yes (Google News RSS + Reddit are the data sources)
- Human/UAT required: no

## Verification

- `python scripts/researcher.py` completes without errors, prints formatted table, saves JSON to `data/research_briefs/`
- `python -c "from scripts.researcher import NewsResearcher, ResearchBrief, SentimentResult; print('import ok')"` confirms clean imports
- `python tests/test_researcher.py` passes unit tests for query extraction, sentiment scoring, negation handling, zero-result handling, and ResearchBrief contract fields
- Output JSON contains all S03 contract fields: `current_yes_price`, `consensus_sentiment`, `consensus_confidence`, `gap`, `narrative_summary`

## Observability / Diagnostics

- Runtime signals: logging at INFO (markets researched, article counts, sentiment scores) and WARNING (zero results, HTTP errors, rate limiting)
- Inspection surfaces: JSON briefs in `data/research_briefs/`, CLI table output
- Failure visibility: per-source fetch errors logged with market_id context, zero-result queries logged as warnings
- Redaction constraints: none (no secrets in research data)

## Integration Closure

- Upstream surfaces consumed: `config.load_settings()`, `config.RESEARCH_DIR`, `Market` dataclass from `scripts/scanner.py` (via dict conversion), scan snapshots from `data/market_snapshots/`
- New wiring introduced: `researcher.py` reads latest scan snapshot JSON to get markets, produces `ResearchBrief` dicts for S03 consumption
- What remains: S03 (prediction engine), S04 (risk + execution), S05 (pipeline + learning)

## Tasks

- [x] **T01: Build NewsResearcher module with fetching, sentiment, and aggregation** `est:1h30m`
  - Why: Core slice deliverable — the entire research pipeline from market dict to ResearchBrief
  - Files: `scripts/researcher.py`, `tests/test_researcher.py`
  - Do: Implement dataclasses (`SentimentResult`, `ResearchBrief`), query extractor, Google News RSS fetcher, Reddit JSON fetcher with User-Agent and delays, keyword sentiment analyzer with financial vocabulary and bigram negation, consensus aggregation, gap analysis, JSON persistence to `RESEARCH_DIR`. Include unit tests for query extraction, sentiment scoring (including negation), zero-result handling, and contract field validation.
  - Verify: `python tests/test_researcher.py` passes all tests, `python -c "from scripts.researcher import NewsResearcher, ResearchBrief, SentimentResult"` works
  - Done when: `NewsResearcher.research_market(market_dict)` returns a valid `ResearchBrief` with all contract fields, unit tests pass

- [x] **T02: Add CLI interface and verify end-to-end with live APIs** `est:30m`
  - Why: Makes the module runnable as a standalone script (demo requirement) and verifies real API integration
  - Files: `scripts/researcher.py`
  - Do: Add `if __name__ == "__main__"` block following scanner.py pattern: load latest scan snapshot, research top N markets, print formatted table (market title, sentiment, confidence, gap, direction), save timestamped JSON snapshot. Handle Windows console encoding (`sys.stdout.reconfigure`). Add `--market` flag for single-market research.
  - Verify: `python scripts/researcher.py` completes against live APIs, prints table, saves JSON; `python scripts/researcher.py --market <ticker>` works for a single market
  - Done when: CLI produces formatted output matching scanner.py style, JSON briefs saved to disk, zero crashes on Windows

## Files Likely Touched

- `scripts/researcher.py`
- `tests/test_researcher.py`
