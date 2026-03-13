---
estimated_steps: 7
estimated_files: 2
---

# T01: Build NewsResearcher module with fetching, sentiment, and aggregation

**Slice:** S02 — Research Agent
**Milestone:** M001

## Description

Build the complete `NewsResearcher` class in `scripts/researcher.py` that takes a market dict (from `Market` dataclass via `asdict()`), extracts search queries from the market title, fetches headlines from Google News RSS and Reddit JSON, runs keyword-based sentiment analysis with bigram negation handling, aggregates results into a consensus, calculates gap vs market price, and saves research briefs as JSON. Includes unit tests for all non-network logic.

## Steps

1. Define `SentimentResult` and `ResearchBrief` dataclasses matching the S02→S03 boundary contract exactly (including `current_yes_price`, `market_id`, `market_title`, `sources`, `timestamp`)
2. Implement query extraction: strip "Will"/"Will the" prefix, remove question marks, extract 3-6 key content words. Handle edge cases like quoted names, dates, compound titles
3. Implement Google News RSS fetcher: build URL, fetch with `requests`, parse XML with `xml.etree.ElementTree`, extract title/source/pubDate per item, split "Title - Source Name" format. Handle HTTP errors and empty results gracefully
4. Implement Reddit JSON fetcher: search endpoint with `User-Agent: predict-market-bot/0.1`, parse JSON response, extract title/selftext/score/subreddit per post, 1-second delay between requests. Handle 429s and empty results gracefully
5. Implement keyword sentiment analyzer: financial domain bullish/bearish dictionaries, bigram negation handling (no/not/never/lose/lack/without/fail + bullish word → bearish, and vice versa), per-article scoring, aggregate to `SentimentResult` per source with confidence based on article count
6. Implement consensus aggregation: weight sources by confidence, compute `sentiment_implied_probability` from consensus, calculate `gap` and `gap_direction` vs `current_yes_price`, generate `narrative_summary`
7. Implement JSON persistence: save `ResearchBrief` as JSON to `RESEARCH_DIR` with `ensure_ascii=False`, write unit tests covering query extraction, sentiment scoring with negation, zero-result handling, and contract field completeness

## Must-Haves

- [ ] `SentimentResult` dataclass with fields: source, bullish, bearish, neutral, confidence, key_narratives
- [ ] `ResearchBrief` dataclass with fields: market_id, market_title, current_yes_price, sources, consensus_sentiment, consensus_confidence, sentiment_implied_probability, gap, gap_direction, narrative_summary, timestamp
- [ ] Query extraction handles "Will X?", "Will the X?", titles with dates, and multi-word entity names
- [ ] Google News RSS parsing extracts title, source name, and pubDate
- [ ] Reddit fetcher includes User-Agent header and 1-second delay
- [ ] Sentiment dictionaries include financial domain terms (hawkish, dovish, rally, selloff, etc.)
- [ ] Bigram negation flips sentiment for common negation patterns
- [ ] Zero-result queries return low-confidence neutral sentiment (no errors)
- [ ] JSON output includes all S03 contract fields

## Verification

- `python tests/test_researcher.py` passes all tests
- `python -c "from scripts.researcher import NewsResearcher, ResearchBrief, SentimentResult; print('OK')"` succeeds
- Unit tests cover: query extraction (3+ cases), sentiment with negation (2+ cases), zero-result handling, ResearchBrief field completeness

## Observability Impact

- Signals added: INFO logging for fetch counts per source, WARNING for zero results and HTTP errors
- How a future agent inspects this: check log output during `research_market()` calls, inspect saved JSON briefs
- Failure state exposed: HTTP status codes and error messages logged with market_id context

## Inputs

- `config/__init__.py` — `RESEARCH_DIR`, `load_settings()`
- `config/settings.yaml` — `research:` section (sources, max_articles_per_source, sentiment_threshold)
- `scripts/scanner.py` — `Market` dataclass shape (used via `asdict()` to produce market dicts)
- S02 research doc — API response shapes, sentiment approach, constraint details

## Expected Output

- `scripts/researcher.py` — Complete module with `NewsResearcher`, `ResearchBrief`, `SentimentResult` classes
- `tests/test_researcher.py` — Unit tests for query extraction, sentiment, negation, zero-result, contract fields
