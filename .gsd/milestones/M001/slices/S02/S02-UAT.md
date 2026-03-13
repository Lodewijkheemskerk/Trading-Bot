# S02: Research Agent — UAT

**Milestone:** M001
**Written:** 2026-03-13

## UAT Type

- UAT mode: mixed
- Why this mode is sufficient: Core logic verified by 29 unit tests (artifact-driven), live API integration verified by CLI runs against Google News RSS and Reddit (live-runtime). No human-experience testing needed — output is developer-facing CLI and JSON.

## Preconditions

- Python 3.11+ with `requests` installed
- A recent scan snapshot exists in `data/market_snapshots/` (run `python scripts/scanner.py` first if missing)
- Internet access for Google News RSS and Reddit API

## Smoke Test

Run `python scripts/researcher.py --top 1` — should print a table with one market's sentiment data and save a JSON snapshot to `data/research_briefs/`.

## Test Cases

### 1. Unit tests pass

1. Run `python tests/test_researcher.py -v`
2. **Expected:** 29 tests pass, 0 failures, completes in < 1 second

### 2. CLI researches top markets

1. Run `python scripts/researcher.py --top 3`
2. **Expected:** Table with 3 rows showing market title, sentiment (bullish/bearish/neutral), confidence (0-1), gap, direction. JSON snapshot saved to `data/research_briefs/research_*.json`.

### 3. Single market research

1. Pick a market ticker from the latest scan snapshot
2. Run `python scripts/researcher.py --market <ticker>`
3. **Expected:** Table with 1 row for that market, separate JSON snapshot saved

### 4. S03 contract fields present

1. Open the latest `data/research_briefs/research_*.json`
2. Inspect a brief object in the `briefs` array
3. **Expected:** Contains `current_yes_price`, `consensus_sentiment`, `consensus_confidence`, `gap`, `narrative_summary`

### 5. Clean imports

1. Run `python -c "from scripts.researcher import NewsResearcher, ResearchBrief, SentimentResult; print('ok')"`
2. **Expected:** Prints "ok" with no errors

## Edge Cases

### Unknown market ticker

1. Run `python scripts/researcher.py --market NONEXISTENT-TICKER`
2. **Expected:** Helpful error message with sample valid tickers, no crash

### Zero-result query

1. In a Python shell: `r = NewsResearcher(); brief = r.research_market({"market_id": "test", "title": "xyzzy foobarbaz", "yes_price": 0.5})`
2. **Expected:** Returns a valid ResearchBrief with `consensus_sentiment="neutral"`, low confidence, no crash

## Failure Signals

- Any unit test failure in `test_researcher.py`
- CLI crash or traceback during `python scripts/researcher.py`
- Missing contract fields in output JSON (especially `current_yes_price` or `gap`)
- HTTP errors logged as ERROR (not WARNING) — would indicate broken retry/fallback logic

## Not Proven By This UAT

- Sentiment accuracy for edge-case market types (weather, sports) — keyword approach has known limitations
- Performance under heavy concurrent use — single-threaded sequential design
- Reddit API availability during rate-limiting storms — graceful degradation tested in unit tests but not under sustained load

## Notes for Tester

- Most markets will show "neutral" sentiment — this is expected with keyword-based analysis. The confidence number reflects article volume, not signal quality.
- Reddit fetches have a 1-second delay built in. Researching many markets will be slow (2+ seconds per market).
- If scan snapshots are stale, run `python scripts/scanner.py` first to refresh market data.
