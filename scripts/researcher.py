"""
Step 2: RESEARCH — Gather Sentiment for Markets

Takes market dicts (from Market dataclass via asdict()), extracts search
queries from titles, fetches headlines from Google News RSS and Reddit
public JSON, runs keyword-based sentiment analysis with bigram negation,
aggregates consensus across sources, computes gap vs market price, and
saves research briefs as JSON.
"""

import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

import requests

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

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "predict-market-bot/0.1 (research-agent)",
        })

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def research_market(self, market_dict: Dict[str, Any]) -> ResearchBrief:
        """
        Research a single market and return a ResearchBrief.

        Args:
            market_dict: Dict from asdict(Market) — must have
                         market_id, title, yes_price keys.
        """
        market_id = market_dict.get("market_id", "unknown")
        title = market_dict.get("title", "")
        yes_price = float(market_dict.get("yes_price", 0.0))

        query = self.extract_query(title)
        logger.info("Researching %s: query=%r", market_id, query)

        source_results: List[SentimentResult] = []

        # Google News RSS
        if "google_news_rss" in self.sources:
            articles = self._fetch_google_news(query)
            logger.info("%s: Google News returned %d articles", market_id, len(articles))
            if not articles:
                logger.warning("%s: zero Google News results for query=%r", market_id, query)
            sr = self._analyze_sentiment(articles, "google_news")
            source_results.append(sr)

        # Reddit
        if "reddit" in self.sources:
            posts = self._fetch_reddit(query)
            logger.info("%s: Reddit returned %d posts", market_id, len(posts))
            if not posts:
                logger.warning("%s: zero Reddit results for query=%r", market_id, query)
            sr = self._analyze_sentiment(posts, "reddit")
            source_results.append(sr)

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
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Google News fetch error: %s", exc)
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
            resp = self.session.get(url, params=params, timeout=15)

            if resp.status_code == 429:
                logger.warning("Reddit rate limited (429), returning empty")
                return []

            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Reddit fetch error: %s", exc)
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

        # Pick key narratives: highest-magnitude non-neutral articles
        key = sorted(scored_articles, key=lambda a: a["magnitude"], reverse=True)
        key_narratives = [a["title"] for a in key[:5] if a["magnitude"] > 0]

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
        f"{'#':>3}  {'Market Title':<45} {'Sent':>6} {'Conf':>5} "
        f"{'YesP':>5} {'ImpP':>5} {'Gap':>6} {'Dir':<11} {'News':>4} {'Rdt':>4}"
    )
    print(header)
    print("-" * len(header))

    for i, b in enumerate(briefs, 1):
        sent_char = emoji.get(b.consensus_sentiment, "?")
        title_trunc = b.market_title[:45].ljust(45)

        # Count articles per source
        news_count = 0
        reddit_count = 0
        for src in b.sources:
            n = len(src.key_narratives)  # Proxy for article count from key_narratives
            if src.source == "google_news":
                news_count = n
            elif src.source == "reddit":
                reddit_count = n

        print(
            f"{i:3d}  {title_trunc} "
            f"{sent_char:>6} {b.consensus_confidence:5.1%} "
            f"{b.current_yes_price:5.2f} {b.sentiment_implied_probability:5.2f} "
            f"{b.gap:+6.1%} {b.gap_direction:<11} "
            f"{news_count:4d} {reddit_count:4d}"
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
    briefs: List[ResearchBrief] = []

    for market_dict in targets:
        try:
            brief = researcher.research_market(market_dict)
            briefs.append(brief)
        except Exception as exc:
            ticker = market_dict.get("market_id", "unknown")
            logger.error("Failed to research %s: %s", ticker, exc)

    if briefs:
        print()
        _print_research_table(briefs)

        # Save timestamped snapshot
        fp = save_research_snapshot(briefs)
        print(f"\nResearched {len(briefs)} markets. Snapshot saved: {fp}")
    else:
        print("No markets were successfully researched.")
