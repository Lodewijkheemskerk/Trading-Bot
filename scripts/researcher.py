"""
Step 2: RESEARCH — Gather Sentiment for Markets

Takes market dicts (from Market dataclass via asdict()), extracts search
queries from titles, fetches headlines from Google News RSS, Bing News RSS,
and Reddit public JSON, optionally searches X/Twitter via Grok's x_search
tool (top N markets only), runs keyword-based sentiment analysis with bigram
negation, aggregates consensus across sources, computes gap vs market
price, and saves research briefs as JSON.

Sources are fetched in parallel (ThreadPoolExecutor) within each market.
Multiple markets are also researched in parallel using configurable
`parallel_workers` (default 3) from settings.yaml.

Before researching, reads failure_log.md to check past mistakes and
includes relevant warnings in the narrative summary so the prediction
engine can factor in prior losses on similar markets.
"""

import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

import requests

from dotenv import load_dotenv
load_dotenv()

# Allow running both as `python scripts/researcher.py` and as an import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, RESEARCH_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes — S02→S03 boundary contract
# ---------------------------------------------------------------------------

@dataclass
class SentimentResult:
    """Sentiment analysis result for a single source."""
    source: str              # "google_news" or "reddit"
    bullish: float           # 0.0-1.0
    bearish: float           # 0.0-1.0
    neutral: float           # 0.0-1.0
    confidence: float        # 0.0-1.0 (based on article count)
    key_narratives: list     # Top headline strings driving sentiment


@dataclass
class ResearchBrief:
    """Aggregated research output for a single market."""
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


# ---------------------------------------------------------------------------
# Sentiment dictionaries — financial domain
# ---------------------------------------------------------------------------

BULLISH_WORDS = {
    # Market positive
    "rally", "surge", "gain", "rise", "soar", "jump", "climb", "boost",
    "bull", "bullish", "upbeat", "optimistic", "positive", "growth",
    "strong", "strength", "recover", "recovery", "rebound", "uptick",
    "advance", "breakthrough", "momentum", "outperform", "upgrade",
    # Policy / macro positive
    "hawkish", "tighten", "approve", "pass", "support", "agree",
    "deal", "victory", "win", "winning", "success", "succeed",
    "confirm", "likely", "expected", "progress", "improve",
    # Prediction market affirmative
    "yes", "will", "certain", "confident", "probable", "favor",
    "endorse", "announce", "launch", "expand", "increase",
}

BEARISH_WORDS = {
    # Market negative
    "crash", "plunge", "drop", "fall", "decline", "selloff", "sell-off",
    "bear", "bearish", "downturn", "slump", "tumble", "sink",
    "loss", "lose", "losing", "weak", "weakness", "recession",
    "collapse", "contraction", "deficit", "downgrade", "underperform",
    # Policy / macro negative
    "dovish", "cut", "reject", "block", "oppose", "fail", "failure",
    "resign", "quit", "fire", "fired", "delay", "stall", "deadlock",
    "scandal", "crisis", "threat", "risk", "warning", "concern",
    # Prediction market negative
    "unlikely", "doubt", "uncertain", "suspend", "cancel", "withdraw",
    "postpone", "abandon", "defeat", "veto", "deny", "denied",
}

NEGATION_WORDS = {"no", "not", "never", "lose", "lack", "without", "fail", "neither", "nor", "cannot", "wont", "dont"}

# Words to strip from titles for query extraction
STRIP_PREFIXES = [
    r"^will\s+the\s+",
    r"^will\s+",
    r"^is\s+",
    r"^are\s+",
    r"^does\s+",
    r"^do\s+",
    r"^has\s+",
    r"^have\s+",
    r"^can\s+",
    r"^should\s+",
]

# Common filler words to remove from queries
STOP_WORDS = {
    "a", "an", "the", "be", "been", "being", "is", "are", "was", "were",
    "in", "on", "at", "to", "for", "of", "by", "from", "with", "and",
    "or", "but", "if", "than", "that", "this", "it", "its", "as", "up",
    "so", "how", "when", "what", "which", "who", "whom", "where", "there",
    "than", "each", "every", "all", "both", "few", "more", "most", "other",
    "some", "any", "such", "into", "over", "after", "before", "between",
    "under", "again", "further", "then", "once", "here",
}


# ---------------------------------------------------------------------------
# NewsResearcher
# ---------------------------------------------------------------------------

class NewsResearcher:
    """Fetches news and Reddit posts, runs sentiment analysis, produces ResearchBriefs."""

    def __init__(self, settings: Optional[dict] = None):
        s = settings or load_settings()
        research_cfg = s.get("research", {})
        self.max_articles = research_cfg.get("max_articles_per_source", 20)
        self.sentiment_threshold = research_cfg.get("sentiment_threshold", 0.6)
        self.sources = research_cfg.get("sources", ["google_news_rss", "reddit"])

        # X Search config (via Grok 4 Responses API)
        x_cfg = research_cfg.get("x_search", {})
        self.x_search_enabled = x_cfg.get("enabled", False)
        self.x_search_top_n = x_cfg.get("top_n", 5)
        self.x_search_max_results = x_cfg.get("max_results", 10)
        self.grok_api_key = os.environ.get("GROK_API_KEY", "")

        # Parallel config
        self.parallel_workers = research_cfg.get("parallel_workers", 3)

        # Retry config
        exec_cfg = s.get("execution", {})
        self.retry_attempts = exec_cfg.get("retry_attempts", 3)
        self.retry_delay = exec_cfg.get("retry_delay_seconds", 5)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "predict-market-bot/0.1 (research-agent)",
        })

        # Load past failures for context in research briefs
        self.past_failures = self._load_past_failures()

    # ------------------------------------------------------------------
    # Failure log integration
    # ------------------------------------------------------------------

    @staticmethod
    def _load_past_failures() -> Dict[str, List[Dict[str, str]]]:
        """
        Load failure_log.md and build a lookup of market_id → failure info.

        Returns dict mapping market_id to list of failure dicts.
        Used to add warnings to research briefs for markets with past losses.
        """
        try:
            from scripts.compounder import Compounder
            entries = Compounder.load_failure_log()
        except Exception as exc:
            logger.debug("Could not load failure log: %s", exc)
            return {}

        failures_by_market: Dict[str, List[Dict[str, str]]] = {}
        for entry in entries:
            mid = entry.get("market_id", "")
            if mid:
                failures_by_market.setdefault(mid, []).append(entry)

        if failures_by_market:
            logger.info(
                "Loaded %d past failures across %d markets from failure log",
                sum(len(v) for v in failures_by_market.values()),
                len(failures_by_market),
            )

        return failures_by_market

    def _get_failure_context(self, market_id: str) -> str:
        """
        Build a warning string for markets with past failures.

        Returns empty string if no past failures exist for this market.
        """
        failures = self.past_failures.get(market_id, [])
        if not failures:
            return ""

        warnings = []
        for f in failures:
            cat = f.get("category", "Unknown")
            lesson = f.get("lesson", "No details")
            warnings.append(f"[{cat}] {lesson}")

        return (
            f"WARNING: Past failures on this market ({len(failures)} recorded): "
            + " | ".join(warnings[:3])  # Cap at 3 to avoid bloat
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def research_market(self, market_dict: Dict[str, Any], use_x_search: bool = False) -> ResearchBrief:
        """
        Research a single market and return a ResearchBrief.

        Args:
            market_dict: Dict from asdict(Market) — must have
                         market_id, title, yes_price keys.
            use_x_search: If True and x_search is enabled, also search X/Twitter
                          via Grok 4 Responses API.
        """
        market_id = market_dict.get("market_id", "unknown")
        title = market_dict.get("title", "")
        yes_price = float(market_dict.get("yes_price", 0.0))

        query = self.extract_query(title)
        logger.info("Researching %s: query=%r", market_id, query)

        source_results: List[SentimentResult] = []

        # Build list of source fetch tasks: (fetch_fn, args, source_label)
        fetch_tasks: List[Tuple[Any, tuple, str]] = []

        if "google_news_rss" in self.sources:
            fetch_tasks.append((self._fetch_google_news, (query,), "google_news"))
        if "bing_news_rss" in self.sources:
            fetch_tasks.append((self._fetch_bing_news, (query,), "bing_news"))
        if "reddit" in self.sources:
            fetch_tasks.append((self._fetch_reddit, (query,), "reddit"))
        if use_x_search and self.x_search_enabled and self.grok_api_key:
            fetch_tasks.append((self._fetch_x_search, (query, title), "x_twitter"))

        # Fetch all sources in parallel
        def _fetch_source(task):
            fn, args, label = task
            try:
                articles = fn(*args)
                return label, articles
            except Exception as exc:
                logger.warning("%s: %s fetch failed: %s", market_id, label, exc)
                return label, []

        if len(fetch_tasks) > 1:
            with ThreadPoolExecutor(max_workers=len(fetch_tasks)) as pool:
                futures = {pool.submit(_fetch_source, t): t for t in fetch_tasks}
                for future in as_completed(futures):
                    label, articles = future.result()
                    count = len(articles)
                    logger.info("%s: %s returned %d items", market_id, label, count)
                    if not articles:
                        logger.warning("%s: zero results from %s for query=%r", market_id, label, query)
                    sr = self._analyze_sentiment(articles, label)
                    source_results.append(sr)
        elif fetch_tasks:
            # Single source — no thread overhead
            label, articles = _fetch_source(fetch_tasks[0])
            logger.info("%s: %s returned %d items", market_id, label, len(articles))
            sr = self._analyze_sentiment(articles, label)
            source_results.append(sr)
        else:
            logger.warning("%s: no sources configured", market_id)

        # Consensus
        consensus = self._aggregate_consensus(source_results)

        # Gap analysis
        implied_prob = consensus["sentiment_implied_probability"]
        gap = round(implied_prob - yes_price, 4)
        if gap > 0.02:
            gap_direction = "underpriced"
        elif gap < -0.02:
            gap_direction = "overpriced"
        else:
            gap_direction = "fair"

        # Narrative
        narrative = self._build_narrative(
            title, source_results, consensus["consensus_sentiment"],
            consensus["consensus_confidence"], gap, gap_direction,
        )

        # Append failure context if this market has past losses
        failure_context = self._get_failure_context(market_id)
        if failure_context:
            narrative = f"{narrative} {failure_context}"
            logger.info("%s: past failure context added to narrative", market_id)

        brief = ResearchBrief(
            market_id=market_id,
            market_title=title,
            current_yes_price=yes_price,
            sources=source_results,
            consensus_sentiment=consensus["consensus_sentiment"],
            consensus_confidence=consensus["consensus_confidence"],
            sentiment_implied_probability=implied_prob,
            gap=gap,
            gap_direction=gap_direction,
            narrative_summary=narrative,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        return brief

    def save_brief(self, brief: ResearchBrief, output_dir: Optional[Path] = None) -> Path:
        """Save a ResearchBrief as JSON."""
        out = output_dir or RESEARCH_DIR
        out.mkdir(parents=True, exist_ok=True)

        # Sanitize market_id for filename
        safe_id = re.sub(r"[^\w\-]", "_", brief.market_id)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = out / f"brief_{safe_id}_{ts}.json"

        data = asdict(brief)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        logger.info("Brief saved: %s", fp)
        return fp

    def research_markets(
        self,
        market_dicts: List[Dict[str, Any]],
        x_search_top_n: int = 0,
    ) -> List[ResearchBrief]:
        """
        Research multiple markets in parallel using ThreadPoolExecutor.

        Args:
            market_dicts: List of dicts from asdict(Market).
            x_search_top_n: Enable X Search for the first N markets (0 = off).

        Returns:
            List of ResearchBriefs (order may differ from input).
        """
        if not market_dicts:
            return []

        workers = min(self.parallel_workers, len(market_dicts))
        logger.info(
            "Researching %d markets with %d parallel workers",
            len(market_dicts), workers,
        )

        briefs: List[ResearchBrief] = []

        def _research_one(idx_market):
            idx, mkt = idx_market
            use_x = idx < x_search_top_n
            return self.research_market(mkt, use_x_search=use_x)

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_research_one, (i, m)): m
                    for i, m in enumerate(market_dicts)
                }
                for future in as_completed(futures):
                    mkt = futures[future]
                    ticker = mkt.get("market_id", "unknown")
                    try:
                        brief = future.result()
                        briefs.append(brief)
                    except Exception as exc:
                        logger.error("Failed to research %s: %s", ticker, exc)
        else:
            # Single worker — sequential (avoids thread overhead for 1 market)
            for i, mkt in enumerate(market_dicts):
                try:
                    brief = _research_one((i, mkt))
                    briefs.append(brief)
                except Exception as exc:
                    logger.error("Failed to research %s: %s", mkt.get("market_id", "unknown"), exc)

        logger.info("Completed research: %d/%d markets", len(briefs), len(market_dicts))
        return briefs

    # ------------------------------------------------------------------
    # Query extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_query(title: str) -> str:
        """
        Extract a search query from a Kalshi market title.

        Strips "Will"/"Will the" prefixes, question marks,
        and reduces to 3-6 key content words.
        """
        q = title.strip()

        # Remove question mark
        q = q.rstrip("?").strip()

        # Strip common question prefixes (case-insensitive)
        for pattern in STRIP_PREFIXES:
            q = re.sub(pattern, "", q, flags=re.IGNORECASE).strip()

        # Remove date-like suffixes: "by March 15", "before April 2026", "in 2026"
        # Keep dates that are part of entity names by only stripping trailing dates
        q = re.sub(
            r"\s+(by|before|after|on|in|during)\s+"
            r"(January|February|March|April|May|June|July|August|September|"
            r"October|November|December|\d{4})"
            r"[\s\d,]*$",
            "", q, flags=re.IGNORECASE,
        ).strip()

        # Remove standalone year at end
        q = re.sub(r"\s+\d{4}\s*$", "", q).strip()

        # Tokenize, remove stop words, keep content words
        words = q.split()
        content_words = [w for w in words if w.lower() not in STOP_WORDS and len(w) > 1]

        # Keep 3-6 words for optimal search results
        if len(content_words) > 6:
            content_words = content_words[:6]

        result = " ".join(content_words)

        # Fallback: if we stripped too much, use first 5 words of original
        if len(result.split()) < 2:
            fallback_words = title.strip().rstrip("?").split()[:5]
            result = " ".join(fallback_words)

        return result

    # ------------------------------------------------------------------
    # Google News RSS
    # ------------------------------------------------------------------

    def _fetch_google_news(self, query: str) -> List[Dict[str, str]]:
        """
        Fetch headlines from Google News RSS.

        Returns list of dicts with keys: title, source, pub_date, text.
        """
        encoded = requests.utils.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"

        try:
            from scripts.retry import retry_call
            resp = retry_call(
                self.session.get, url, timeout=15,
                max_attempts=self.retry_attempts,
                base_delay=self.retry_delay,
                context="Google News RSS",
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Google News fetch error after retries: %s", exc)
            return []

        articles = []
        try:
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")

            for item in items[:self.max_articles]:
                raw_title = item.findtext("title", "")

                # Google News format: "Headline text - Source Name"
                # Split on last " - " to separate headline from source
                source_name = ""
                headline = raw_title
                if " - " in raw_title:
                    parts = raw_title.rsplit(" - ", 1)
                    headline = parts[0].strip()
                    source_name = parts[1].strip() if len(parts) > 1 else ""

                # Try <source> element first for source name
                source_el = item.find("source")
                if source_el is not None and source_el.text:
                    source_name = source_el.text.strip()

                pub_date = item.findtext("pubDate", "")

                articles.append({
                    "title": headline,
                    "source": source_name,
                    "pub_date": pub_date,
                    "text": headline,  # Sentiment runs on headline text
                })

        except ET.ParseError as exc:
            logger.warning("Google News XML parse error: %s", exc)
            return []

        return articles

    # ------------------------------------------------------------------
    # Reddit
    # ------------------------------------------------------------------

    def _fetch_reddit(self, query: str) -> List[Dict[str, Any]]:
        """
        Fetch posts from Reddit search JSON endpoint.

        Returns list of dicts with keys: title, text, score, subreddit.
        """
        url = "https://www.reddit.com/search.json"
        params = {
            "q": query,
            "sort": "relevance",
            "t": "week",
            "limit": min(self.max_articles, 25),  # Reddit caps at 25 per page
        }

        try:
            from scripts.retry import retry_call
            resp = retry_call(
                self.session.get, url, params=params, timeout=15,
                max_attempts=self.retry_attempts,
                base_delay=self.retry_delay,
                context="Reddit search",
            )

            if resp.status_code == 429:
                logger.warning("Reddit rate limited (429), returning empty")
                return []

            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Reddit fetch error after retries: %s", exc)
            return []

        posts = []
        try:
            data = resp.json()
            children = data.get("data", {}).get("children", [])

            for child in children[:self.max_articles]:
                post = child.get("data", {})
                title = post.get("title", "")
                selftext = post.get("selftext", "")

                # Combine title + selftext for sentiment analysis
                combined_text = title
                if selftext and len(selftext) < 2000:
                    combined_text = f"{title}. {selftext}"

                posts.append({
                    "title": title,
                    "text": combined_text,
                    "score": post.get("score", 0),
                    "subreddit": post.get("subreddit", ""),
                })

        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Reddit JSON parse error: %s", exc)
            return []

        # 1-second delay between Reddit requests (rate limiting)
        time.sleep(1)

        return posts

    # ------------------------------------------------------------------
    # Bing News RSS
    # ------------------------------------------------------------------

    def _fetch_bing_news(self, query: str) -> List[Dict[str, str]]:
        """
        Fetch headlines from Bing News RSS.

        Free, no API key needed. Returns list of dicts with keys:
        title, source, pub_date, text — same shape as Google News.
        """
        encoded = requests.utils.quote(query)
        url = f"https://www.bing.com/news/search?q={encoded}&format=rss"

        try:
            from scripts.retry import retry_call
            resp = retry_call(
                self.session.get, url, timeout=15,
                max_attempts=self.retry_attempts,
                base_delay=self.retry_delay,
                context="Bing News RSS",
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Bing News fetch error after retries: %s", exc)
            return []

        articles = []
        try:
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")

            for item in items[:self.max_articles]:
                headline = item.findtext("title", "").strip()
                if not headline:
                    continue

                # Bing uses <news:Source> or plain <source> for publisher
                source_name = ""
                # Try namespace version first
                for ns_prefix in ["news", "bing"]:
                    for el in item:
                        if el.tag.endswith("Source") or el.tag.endswith("source"):
                            if el.text:
                                source_name = el.text.strip()
                                break
                    if source_name:
                        break

                pub_date = item.findtext("pubDate", "")

                articles.append({
                    "title": headline,
                    "source": source_name or "bing_news",
                    "pub_date": pub_date,
                    "text": headline,
                })

        except ET.ParseError as exc:
            logger.warning("Bing News XML parse error: %s", exc)
            return []

        return articles

    # ------------------------------------------------------------------
    # X/Twitter via Grok x_search (Responses API)
    # ------------------------------------------------------------------

    def _fetch_x_search(self, query: str, full_title: str = "") -> List[Dict[str, str]]:
        """
        Search X/Twitter using Grok 4's built-in x_search tool.

        Uses the xAI Responses API (not Chat Completions — x_search
        requires Grok 4 family models). Returns a list of dicts with
        keys: title, text, source — same shape as Google News / Reddit
        results for unified sentiment analysis.

        Cost: ~$0.22 per call (Grok 4 + x_search tool invocation).
        """
        if not self.grok_api_key:
            logger.warning("X Search: GROK_API_KEY not set, skipping")
            return []

        prompt = (
            f"Search X for the most recent posts about: {query}\n"
            f"Full market question: {full_title}\n\n"
            f"Return ONLY a JSON array of the {self.x_search_max_results} most "
            f"relevant posts. Each element should have:\n"
            f'  "text": the post content,\n'
            f'  "author": the username,\n'
            f'  "sentiment": "bullish" or "bearish" or "neutral"\n'
            f"No other text — just the JSON array."
        )

        try:
            # x_search requires Grok 4 family — Grok 3 is not supported
            from scripts.retry import retry_call
            resp = retry_call(
                requests.post,
                "https://api.x.ai/v1/responses",
                headers={
                    "Authorization": f"Bearer {self.grok_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "grok-4.20-beta-latest-non-reasoning",
                    "input": [{"role": "user", "content": prompt}],
                    "tools": [{"type": "x_search"}],
                    "max_output_tokens": 1500,
                },
                timeout=60,
                max_attempts=self.retry_attempts,
                base_delay=self.retry_delay,
                context="X Search (Grok 4)",
            )

            if resp.status_code != 200:
                logger.warning("X Search API error %d: %s", resp.status_code, resp.text[:200])
                return []

            data = resp.json()

            # Track cost
            usage = data.get("usage", {})
            cost_ticks = usage.get("cost_in_usd_ticks", 0)
            cost_usd = cost_ticks / 1_000_000_000
            logger.info("X Search cost: $%.4f (in=%s out=%s)",
                        cost_usd,
                        usage.get("input_tokens", "?"),
                        usage.get("output_tokens", "?"))

            # Extract text content from Responses API output
            text_content = ""
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            text_content = c["text"]

            if not text_content:
                logger.warning("X Search: no text content in response")
                return []

            # Parse JSON array from response
            # Strip markdown code fences if present
            cleaned = text_content.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
                cleaned = re.sub(r"\s*```\s*$", "", cleaned)

            posts_data = json.loads(cleaned)
            if not isinstance(posts_data, list):
                logger.warning("X Search: expected JSON array, got %s", type(posts_data).__name__)
                return []

            # Convert to standard article format for sentiment analysis
            articles = []
            for post in posts_data[:self.x_search_max_results]:
                text = post.get("text", "")
                author = post.get("author", "unknown")
                articles.append({
                    "title": f"@{author}: {text[:100]}",
                    "text": text,
                    "source": "x_twitter",
                })

            return articles

        except json.JSONDecodeError:
            logger.warning("X Search: failed to parse JSON from Grok response")
            return []
        except requests.RequestException as exc:
            logger.warning("X Search request error: %s", exc)
            return []
        except Exception as exc:
            logger.error("X Search unexpected error: %s", exc, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Sentiment analysis
    # ------------------------------------------------------------------

    @staticmethod
    def score_text(text: str) -> Dict[str, float]:
        """
        Score a single text for bullish/bearish sentiment.

        Uses keyword matching with bigram negation handling.
        Returns dict with bullish_score, bearish_score.
        """
        words = re.findall(r"[a-z]+", text.lower())
        bullish_score = 0.0
        bearish_score = 0.0

        for i, word in enumerate(words):
            prev_word = words[i - 1] if i > 0 else ""
            is_negated = prev_word in NEGATION_WORDS

            if word in BULLISH_WORDS:
                if is_negated:
                    bearish_score += 1.0  # Negated bullish → bearish
                else:
                    bullish_score += 1.0
            elif word in BEARISH_WORDS:
                if is_negated:
                    bullish_score += 1.0  # Negated bearish → bullish
                else:
                    bearish_score += 1.0

        return {"bullish_score": bullish_score, "bearish_score": bearish_score}

    def _analyze_sentiment(
        self, articles: List[Dict[str, Any]], source_name: str
    ) -> SentimentResult:
        """
        Analyze sentiment across a list of articles for a single source.

        Returns a SentimentResult with aggregate scores.
        """
        if not articles:
            return SentimentResult(
                source=source_name,
                bullish=0.0,
                bearish=0.0,
                neutral=1.0,
                confidence=0.1,  # Low confidence for zero results
                key_narratives=[],
            )

        total_bullish = 0.0
        total_bearish = 0.0
        total_neutral = 0.0
        scored_articles: List[Dict[str, Any]] = []

        for article in articles:
            text = article.get("text", "")
            scores = self.score_text(text)

            bull = scores["bullish_score"]
            bear = scores["bearish_score"]
            total = bull + bear

            if total == 0:
                total_neutral += 1
                scored_articles.append({
                    "title": article.get("title", ""),
                    "sentiment": "neutral",
                    "magnitude": 0.0,
                })
            else:
                if bull > bear:
                    total_bullish += 1
                    scored_articles.append({
                        "title": article.get("title", ""),
                        "sentiment": "bullish",
                        "magnitude": bull - bear,
                    })
                elif bear > bull:
                    total_bearish += 1
                    scored_articles.append({
                        "title": article.get("title", ""),
                        "sentiment": "bearish",
                        "magnitude": bear - bull,
                    })
                else:
                    total_neutral += 1
                    scored_articles.append({
                        "title": article.get("title", ""),
                        "sentiment": "neutral",
                        "magnitude": 0.0,
                    })

        n = len(articles)
        bullish_pct = total_bullish / n
        bearish_pct = total_bearish / n
        neutral_pct = total_neutral / n

        # Confidence based on article count: more articles = higher confidence
        # Scale: 1 article = 0.2, 5 = 0.5, 10 = 0.7, 20+ = 0.9
        confidence = min(0.9, 0.1 + (n / 25.0))

        # Pick key narratives: highest-magnitude articles first, then any remaining
        key = sorted(scored_articles, key=lambda a: a["magnitude"], reverse=True)
        key_narratives = [a["title"] for a in key[:8] if a["title"].strip()]

        return SentimentResult(
            source=source_name,
            bullish=round(bullish_pct, 3),
            bearish=round(bearish_pct, 3),
            neutral=round(neutral_pct, 3),
            confidence=round(confidence, 3),
            key_narratives=key_narratives,
        )

    # ------------------------------------------------------------------
    # Consensus aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate_consensus(results: List[SentimentResult]) -> Dict[str, Any]:
        """
        Aggregate multiple SentimentResults into a consensus.

        Weights each source by its confidence. Returns dict with
        consensus_sentiment, consensus_confidence, sentiment_implied_probability.
        """
        if not results:
            return {
                "consensus_sentiment": "neutral",
                "consensus_confidence": 0.1,
                "sentiment_implied_probability": 0.5,
            }

        total_weight = sum(r.confidence for r in results)
        if total_weight == 0:
            total_weight = 1.0  # Avoid division by zero

        weighted_bullish = sum(r.bullish * r.confidence for r in results) / total_weight
        weighted_bearish = sum(r.bearish * r.confidence for r in results) / total_weight

        # Determine consensus direction
        if weighted_bullish > weighted_bearish and weighted_bullish > 0.3:
            consensus = "bullish"
        elif weighted_bearish > weighted_bullish and weighted_bearish > 0.3:
            consensus = "bearish"
        else:
            consensus = "neutral"

        # Consensus confidence: average of source confidences
        avg_confidence = sum(r.confidence for r in results) / len(results)

        # Sentiment-implied probability:
        # bullish sentiment → higher probability (> 0.5)
        # bearish → lower probability (< 0.5)
        # Scale: net sentiment * confidence, centered at 0.5
        net_sentiment = weighted_bullish - weighted_bearish
        implied_prob = 0.5 + (net_sentiment * avg_confidence * 0.5)
        implied_prob = max(0.05, min(0.95, implied_prob))  # Clamp to valid range

        return {
            "consensus_sentiment": consensus,
            "consensus_confidence": round(avg_confidence, 3),
            "sentiment_implied_probability": round(implied_prob, 4),
        }

    # ------------------------------------------------------------------
    # Narrative generation
    # ------------------------------------------------------------------

    @staticmethod
    def _build_narrative(
        title: str,
        sources: List[SentimentResult],
        consensus: str,
        confidence: float,
        gap: float,
        gap_direction: str,
    ) -> str:
        """Build a 2-3 sentence narrative summary."""
        # Collect all key narratives
        all_narratives = []
        for src in sources:
            all_narratives.extend(src.key_narratives[:2])

        # Source breakdown
        source_parts = []
        for src in sources:
            dominant = "bullish" if src.bullish > src.bearish else (
                "bearish" if src.bearish > src.bullish else "neutral"
            )
            source_parts.append(f"{src.source} ({dominant}, {src.confidence:.0%} conf)")

        sources_str = ", ".join(source_parts) if source_parts else "no sources"

        # Build narrative
        lines = [
            f"Research across {sources_str} shows {consensus} sentiment "
            f"with {confidence:.0%} confidence.",
        ]

        if gap_direction != "fair":
            lines.append(
                f"Market appears {gap_direction} by {abs(gap):.1%} based on sentiment signals."
            )

        if all_narratives:
            top = all_narratives[0][:80]
            lines.append(f"Key headline: \"{top}\".")

        return " ".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_latest_snapshot() -> Optional[dict]:
    """Load the most recent scan snapshot from MARKET_DIR."""
    from config import MARKET_DIR as mdir
    snapshots = sorted(mdir.glob("scan_*.json"), reverse=True)
    if not snapshots:
        logger.warning("No scan snapshots found in %s", mdir)
        return None
    fp = snapshots[0]
    logger.info("Loading snapshot: %s", fp)
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_research_snapshot(briefs: List[ResearchBrief], output_dir: Optional[Path] = None) -> Path:
    """Save all research briefs as a single timestamped JSON snapshot."""
    out = output_dir or RESEARCH_DIR
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = out / f"research_{ts}.json"

    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "markets_researched": len(briefs),
        "briefs": [asdict(b) for b in briefs],
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    logger.info("Research snapshot saved: %s (%d briefs)", fp, len(briefs))
    return fp


def _print_research_table(briefs: List[ResearchBrief]) -> None:
    """Print a formatted table of research results."""
    # Sentiment emoji for visual scan
    emoji = {"bullish": "+", "bearish": "-", "neutral": "~"}

    header = (
        f"{'#':>3}  {'Market Title':<42} {'Sent':>5} {'Conf':>5} "
        f"{'YesP':>5} {'ImpP':>5} {'Gap':>6} {'Dir':<11} "
        f"{'GNws':>4} {'Bing':>4} {'Rdt':>4} {'X':>3}"
    )
    print(header)
    print("-" * len(header))

    for i, b in enumerate(briefs, 1):
        sent_char = emoji.get(b.consensus_sentiment, "?")
        title_trunc = b.market_title[:42].ljust(42)

        # Count articles per source
        counts = {"google_news": 0, "bing_news": 0, "reddit": 0, "x_twitter": 0}
        for src in b.sources:
            n = len(src.key_narratives)
            if src.source in counts:
                counts[src.source] = n

        print(
            f"{i:3d}  {title_trunc} "
            f"{sent_char:>5} {b.consensus_confidence:5.1%} "
            f"{b.current_yes_price:5.2f} {b.sentiment_implied_probability:5.2f} "
            f"{b.gap:+6.1%} {b.gap_direction:<11} "
            f"{counts['google_news']:4d} {counts['bing_news']:4d} "
            f"{counts['reddit']:4d} {counts['x_twitter']:3d}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    # Handle Windows console encoding for unicode characters
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Research sentiment for Kalshi markets from the latest scan snapshot."
    )
    parser.add_argument(
        "--market", type=str, default=None,
        help="Research a single market by ticker (market_id).",
    )
    parser.add_argument(
        "--top", type=int, default=5,
        help="Number of top markets to research (default: 5). Ignored if --market is set.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Load latest scan snapshot
    snapshot = load_latest_snapshot()
    if snapshot is None:
        print("No scan snapshots found. Run scanner.py first.")
        sys.exit(1)

    all_markets = snapshot.get("markets", [])
    if not all_markets:
        print("Snapshot contains no markets.")
        sys.exit(1)

    # Select markets to research
    if args.market:
        # Find the specific market by ticker
        matched = [m for m in all_markets if m.get("market_id") == args.market]
        if not matched:
            print(f"Market '{args.market}' not found in snapshot ({len(all_markets)} markets available).")
            # Show a few tickers as hints
            sample = [m.get("market_id", "?") for m in all_markets[:5]]
            print(f"Sample tickers: {', '.join(sample)}")
            sys.exit(1)
        targets = matched
    else:
        # Top N markets by opportunity_score (already sorted in snapshot)
        targets = all_markets[:args.top]

    print(f"\nResearch Agent — Sentiment Analysis")
    print(f"Snapshot: {snapshot.get('timestamp', '?')}")
    print(f"Markets to research: {len(targets)}\n")

    researcher = NewsResearcher()

    x_search_count = researcher.x_search_top_n if researcher.x_search_enabled else 0
    if x_search_count:
        print(f"X Search enabled for top {x_search_count} markets (~$0.22/market)")

    print(f"Sources: {', '.join(researcher.sources)}")
    print(f"Parallel workers: {researcher.parallel_workers}\n")

    briefs = researcher.research_markets(targets, x_search_top_n=x_search_count)

    if briefs:
        print()
        _print_research_table(briefs)

        # Save timestamped snapshot
        fp = save_research_snapshot(briefs)
        print(f"\nResearched {len(briefs)} markets. Snapshot saved: {fp}")
    else:
        print("No markets were successfully researched.")
