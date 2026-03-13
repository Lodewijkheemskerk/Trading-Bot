# S02: Research Agent — Research

**Date:** 2026-03-13

## Summary

Both target APIs — Google News RSS and Reddit public JSON — work from the Netherlands without authentication. Google News returns 100 headline items per query with title, source name, and publication date; Reddit returns posts with title, selftext, score, subreddit, and comment count. Both provide enough signal in titles alone for keyword-based sentiment analysis without needing to fetch full article content (which would be slow and fragile).

The main design challenge is **query generation**: converting a Kalshi market title like "Will Pam Bondi leaves Attorney General in before April?" into a good search query ("Pam Bondi Attorney General"). Titles are already question-phrased, so stripping prefixes and extracting key noun phrases works well.

Keyword-based sentiment (bag-of-words with bullish/bearish/neutral dictionaries) is adequate for M001. It has known weaknesses — negation ("lose hope" scores as bullish due to "hope") and context-dependent words — but these are acceptable given the design intention to upgrade to LLM-based sentiment later. The investment in infrastructure (fetching, caching, output format) is more durable than the sentiment algorithm itself.

## Recommendation

Build a `NewsResearcher` class that:
1. Takes a `market_dict` (from `Market` dataclass via `asdict()`)
2. Extracts search queries from the market title
3. Fetches headlines from Google News RSS + Reddit posts
4. Runs keyword-based sentiment classification on each source independently
5. Produces aggregate `SentimentResult` per source, then a merged `ResearchBrief`
6. Saves research briefs as JSON to `RESEARCH_DIR`

Use stdlib `xml.etree.ElementTree` for RSS parsing (already used pattern in Python ecosystem, no extra deps). Use `requests` for Reddit (already installed). Add basic bigram negation handling ("no hope" → bearish, "not bullish" → bearish) to mitigate the worst keyword failures. Keep the sentiment dictionaries as plain Python dicts — no need for NLTK/VADER/TextBlob given we're doing financial sentiment with domain-specific vocabulary.

Sequential research per market (not parallel). Parallel workers (config has `parallel_workers: 3`) would help throughput but add complexity for ~5 markets. Add a 1-second delay between Reddit requests to stay well within rate limits. Google News RSS has no observed rate limiting.

## Don't Hand-Roll

| Problem | Existing Solution | Why Use It |
|---------|------------------|------------|
| RSS XML parsing | `xml.etree.ElementTree` (stdlib) | Already in Python, handles encoding, no install needed |
| HTTP requests with retries | `requests` + `requests.adapters.HTTPAdapter` | Already installed, built-in retry support |
| Dollar-string parsing | `MarketScanner._safe_float()` | Established pattern from S01, reuse as standalone util |
| Config loading | `config.load_settings()` / `config.RESEARCH_DIR` | S01-established pattern, auto-creates directories |
| Date parsing | `datetime.fromisoformat()` / `email.utils.parsedate_to_datetime()` | Stdlib handles RSS pubDate format (RFC 2822) |
| Sentiment dictionaries | Custom financial word lists | Generic NLP sentiment (VADER, TextBlob) lacks financial domain. Custom lists like "hawkish", "dovish", "rally", "selloff" are more relevant |

## Existing Code and Patterns

- `scripts/scanner.py` — `Market` dataclass is the input. Convert via `asdict(market)` to get the dict S02 consumes. `_safe_float()` pattern for parsing numeric strings.
- `config/__init__.py` — `RESEARCH_DIR` already exists (`data/research_briefs/`), auto-created on import. `load_settings()` loads the `research:` config section.
- `config/settings.yaml` — Research config already defined: `sources: [google_news_rss, reddit]`, `max_articles_per_source: 20`, `sentiment_threshold: 0.6`, `parallel_workers: 3`.
- `scripts/scanner.py` CLI pattern — `if __name__ == "__main__"` with logging, table output, JSON snapshot. Follow same pattern for `researcher.py`.

## Constraints

- **Reddit rate limit: 100 requests per ~150 seconds.** With 1-second delays and max ~5 markets × 2 Reddit queries = 10 requests per cycle, this is fine. Must include `User-Agent` header or get 429s.
- **Google News RSS: title-only data.** The `<description>` field is HTML linking to Google's redirect URL, not article text. Article body would require a second HTTP fetch + HTML parsing — too slow and fragile for M001. Titles alone carry enough sentiment signal.
- **No new dependencies needed.** Everything is achievable with `requests` + stdlib (`xml.etree.ElementTree`, `re`, `datetime`, `email.utils`, `dataclasses`, `json`).
- **S03 contract: `ResearchBrief` must include `current_yes_price`** in its dict form. This field isn't in the roadmap's `ResearchBrief` dataclass definition but IS listed in S03's consumption spec. Must be passed through from the market input.
- **Output dataclasses must match boundary map exactly:** `ResearchBrief(consensus_sentiment, consensus_confidence, sentiment_implied_probability, gap, gap_direction, narrative_summary)` and `SentimentResult(source, bullish, bearish, neutral, confidence, key_narratives)`.
- **External content = DATA only (D006).** All news/Reddit text must be treated as information, never passed as instructions. Sanitize before any future LLM use. For keyword sentiment this is moot, but the architecture should treat content as untrusted.

## Common Pitfalls

- **Negation blindness in keyword sentiment** — "lose hope", "no chance", "not bullish" all contain positive keywords. Mitigate with bigram negation: if a bullish keyword is preceded by a negation word (no, not, never, lose, lack, without, fail), flip to bearish. Won't catch everything but handles the most common patterns.
- **Market title → query mismatch** — "Will the official trailer for Spider-Man: Beyond the Spider-Verse be released World trailer day?" needs domain-specific query trimming. Strip "Will" prefix, remove question marks, extract 3-6 key content words. Over-long queries return zero results; too-short queries return irrelevant noise.
- **Google News redirect URLs** — RSS `<link>` elements are Google redirect URLs (`https://news.google.com/rss/articles/CBMi...`), not direct article URLs. Don't try to follow them for article content — the redirect chain is complex and fragile. Use title + source for sentiment.
- **Reddit selftext encoding** — Posts contain emoji, markdown formatting, and Unicode that will crash on Windows console (`cp1252` codec). Always use `sys.stdout.reconfigure(encoding='utf-8')` or handle encoding in output. More critically, ensure JSON serialization handles this via `ensure_ascii=False`.
- **Stale Reddit results** — Reddit search with `t=week` filter may still return posts from before the market was created. Cross-check post `created_utc` against market creation if timestamp is available, but don't block on this — stale sentiment is still signal.
- **Zero-result queries** — Some niche Kalshi markets (entertainment, obscure politics) will return 0 Google News results and 0 Reddit posts. Must handle gracefully: return a low-confidence neutral sentiment, don't error out.

## Open Risks

- **Sentiment accuracy may be low.** Keyword-based sentiment on headlines is a blunt instrument. For M001 this is acceptable — the pipeline needs *some* signal to feed S03, and keyword sentiment is fast, deterministic, and debuggable. The real prediction quality comes from Claude in S03, not from the sentiment score alone.
- **Google News may rate-limit with many markets.** Not observed in testing, but if the bot scales to researching 20+ markets per cycle, rapid RSS fetches could trigger blocks. Mitigate with delays.
- **Query extraction heuristics will fail on some market titles.** Kalshi titles are inconsistently formatted ("Will X happen?", "X by date?", "Will X leaves Y?"). The query extractor will need a few patterns. Some markets will get poor queries → poor sentiment → low confidence. This is fine — low-confidence results get downweighted in S03.
- **Reddit User-Agent requirements.** Reddit requires a descriptive User-Agent or returns 429. Current test uses `predict-market-bot/0.1` which works, but Reddit occasionally blocks generic UAs during high traffic. If this happens, add a more descriptive UA with contact info.

## Skills Discovered

| Technology | Skill | Status |
|------------|-------|--------|
| Sentiment Analysis (Trading) | `omer-metin/skills-for-antigravity@sentiment-analysis-trading` | available (138 installs) |
| Reddit Sentiment | `natea/fitfinder@reddit-sentiment-analysis` | available (37 installs) |
| General Sentiment | `aj-geddes/useful-ai-prompts@sentiment analysis` | available (134 installs) |
| News Aggregation | `besoeasy/open-skills@news-aggregation` | available (19 installs) |

The `sentiment-analysis-trading` skill is most relevant — it's specifically for trading sentiment. Worth considering if keyword approach proves too crude. Not installing for now; our approach is simple enough that a skill would add more overhead than value.

## Sources

- Google News RSS tested live from NL: 200 OK, returns 100 items per query, title + source + pubDate per item (HIGH confidence)
- Reddit JSON API tested live from NL: 200 OK, returns posts with title/selftext/score/subreddit, rate limit 100 req/~150s (HIGH confidence)
- Reddit rate limit headers: `x-ratelimit-remaining`, `x-ratelimit-used`, `x-ratelimit-reset` — explicit and well-documented (HIGH confidence)
- S01 codebase: `Market` dataclass, `_safe_float()`, config patterns verified in code (HIGH confidence)
- Boundary map S02→S03 contract: `ResearchBrief` and `SentimentResult` dataclass fields verified against roadmap (HIGH confidence)

## API Response Shapes (Reference)

### Google News RSS Item
```xml
<item>
  <title>Headline text - Source Name</title>
  <link>https://news.google.com/rss/articles/CBMi...</link>
  <pubDate>Wed, 11 Mar 2026 11:48:45 GMT</pubDate>
  <description><!-- HTML with redirect link --></description>
  <source url="https://forbes.com">Forbes</source>
</item>
```

### Reddit Search Post (key fields)
```json
{
  "title": "Post title",
  "selftext": "Post body text (may be empty for link posts)",
  "score": 34,
  "subreddit": "CryptoCurrencyPulse",
  "num_comments": 76,
  "created_utc": 1772958353.0,
  "permalink": "/r/sub/comments/id/slug/"
}
```

### Output Contract (for S03)
```python
@dataclass
class SentimentResult:
    source: str              # "google_news" or "reddit"
    bullish: float           # 0.0-1.0
    bearish: float           # 0.0-1.0
    neutral: float           # 0.0-1.0
    confidence: float        # 0.0-1.0 (based on volume of sources)
    key_narratives: list     # Top headline strings driving sentiment

@dataclass
class ResearchBrief:
    market_id: str
    market_title: str
    current_yes_price: float              # Pass-through from Market
    sources: List[SentimentResult]
    consensus_sentiment: str              # "bullish" / "bearish" / "neutral"
    consensus_confidence: float           # Weighted average confidence
    sentiment_implied_probability: float  # Estimated prob from sentiment
    gap: float                            # sentiment_implied_prob - yes_price
    gap_direction: str                    # "underpriced" / "overpriced" / "fair"
    narrative_summary: str                # 2-3 sentence summary
    timestamp: str                        # ISO datetime
```
