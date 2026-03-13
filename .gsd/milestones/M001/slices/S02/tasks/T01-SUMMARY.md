---
id: T01
parent: S02
milestone: M001
provides:
  - NewsResearcher class with Google News RSS and Reddit fetching
  - SentimentResult and ResearchBrief dataclasses (S02→S03 contract)
  - Keyword sentiment analyzer with bigram negation handling
  - Consensus aggregation with gap analysis
  - JSON persistence for research briefs
key_files:
  - scripts/researcher.py
  - tests/test_researcher.py
key_decisions:
  - score_text is a static method for easy unit testing without instantiation
  - Confidence scales linearly with article count (0.1 + n/25, capped at 0.9)
  - Sentiment-implied probability centered at 0.5 with net_sentiment * confidence * 0.5 offset
  - Gap threshold of ±0.02 for "fair" classification
  - Query extraction fallback to first 5 words of original title when content words < 2
patterns_established:
  - Keyword sentiment with BULLISH_WORDS/BEARISH_WORDS sets and NEGATION_WORDS bigram handling
  - SentimentResult/ResearchBrief dataclass contract matching S03 consumption spec exactly
  - Researcher follows scanner.py patterns: session-based HTTP, logging, JSON persistence
observability_surfaces:
  - INFO log: "Researching {market_id}: query={query}" on each market
  - INFO log: "{market_id}: {source} returned {n} articles" per source
  - WARNING log: "{market_id}: zero {source} results for query={query}" when no results
  - WARNING log: HTTP errors with status codes and market_id context
  - JSON briefs in data/research_briefs/ for post-hoc inspection
duration: 25m
verification_result: passed
completed_at: 2026-03-13
blocker_discovered: false
---

# T01: Build NewsResearcher module with fetching, sentiment, and aggregation

**Complete research pipeline from market dict to ResearchBrief with keyword sentiment, bigram negation, consensus aggregation, and gap analysis.**

## What Happened

Built `scripts/researcher.py` with the full `NewsResearcher` class implementing all 7 plan steps:

1. **Dataclasses**: `SentimentResult` and `ResearchBrief` matching the S02→S03 boundary contract exactly, including `current_yes_price` pass-through.
2. **Query extraction**: Strips "Will"/"Will the"/etc. prefixes, removes trailing date suffixes, filters stop words, keeps 3-6 content words with fallback for over-stripped titles.
3. **Google News RSS**: Fetches via `requests`, parses with `xml.etree.ElementTree`, splits "Title - Source Name" format, falls back to `<source>` element. Handles HTTP errors and empty results.
4. **Reddit JSON**: Searches with proper User-Agent header, respects 25-per-page limit, 1-second delay after fetch, handles 429s gracefully.
5. **Sentiment analysis**: 30+ bullish and 30+ bearish financial domain keywords (hawkish, dovish, rally, selloff, etc.), bigram negation handling for no/not/never/lose/lack/without/fail, per-article scoring, aggregate to SentimentResult with confidence based on article count.
6. **Consensus**: Confidence-weighted aggregation across sources, sentiment-implied probability calculation, gap and gap_direction vs market price.
7. **Persistence**: `save_brief()` writes JSON with `ensure_ascii=False` to `RESEARCH_DIR`. Unit tests cover query extraction (9 cases), sentiment scoring with negation (9 cases), zero-result handling, consensus aggregation (4 cases), and contract field completeness (3 cases).

## Verification

- `python tests/test_researcher.py -v` — **29 tests, all pass** (0.002s)
- `python -c "from scripts.researcher import NewsResearcher, ResearchBrief, SentimentResult; print('OK')"` — **PASS**
- Contract fields check: all S03 required fields present in ResearchBrief — **PASS**
- JSON round-trip serialization: **PASS**
- Observability: INFO logging for fetch counts, WARNING for zero results verified — **PASS**

### Slice-level verification status (T01 is intermediate):
- ✅ `python -c "from scripts.researcher import ..."` — clean imports pass
- ✅ `python tests/test_researcher.py` — all unit tests pass
- ✅ Output JSON contains all S03 contract fields
- ⬜ `python scripts/researcher.py` CLI — not yet built (T02)
- ⬜ JSON saved to `data/research_briefs/` via CLI — not yet built (T02)

## Diagnostics

- Run `python tests/test_researcher.py -v` to verify all sentiment/query/contract logic
- Inspect `data/research_briefs/brief_*.json` after running CLI (T02) for saved research output
- Check logs during `research_market()` calls for per-source fetch counts and zero-result warnings
- Use `NewsResearcher.score_text("some text")` interactively to debug sentiment scoring

## Deviations

None. All 7 plan steps implemented as specified.

## Known Issues

None discovered during implementation.

## Files Created/Modified

- `scripts/researcher.py` — Complete NewsResearcher module with fetching, sentiment, consensus, gap analysis, and JSON persistence
- `tests/test_researcher.py` — 29 unit tests covering query extraction, sentiment scoring, negation handling, zero-result handling, consensus aggregation, and contract field validation
