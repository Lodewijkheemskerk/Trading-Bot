"""
Unit tests for scripts/researcher.py

Covers: query extraction, sentiment scoring with negation,
zero-result handling, and ResearchBrief contract fields.
All tests are offline — no network calls.
"""

import sys
import unittest
from pathlib import Path
from dataclasses import asdict, fields

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.researcher import (
    NewsResearcher,
    ResearchBrief,
    SentimentResult,
)


class TestQueryExtraction(unittest.TestCase):
    """Test query extraction from Kalshi market titles."""

    def setUp(self):
        self.researcher = NewsResearcher(settings={
            "research": {
                "sources": [],  # No network calls
                "max_articles_per_source": 20,
                "sentiment_threshold": 0.6,
            }
        })

    def test_strip_will_prefix(self):
        query = NewsResearcher.extract_query("Will Bitcoin reach $100k?")
        self.assertNotIn("Will", query.split()[0] if query else "")
        self.assertIn("Bitcoin", query)

    def test_strip_will_the_prefix(self):
        query = NewsResearcher.extract_query("Will the Fed raise rates?")
        self.assertNotIn("Will", query.split()[0] if query else "")
        self.assertIn("Fed", query)
        self.assertIn("raise", query)
        self.assertIn("rates", query)

    def test_remove_question_mark(self):
        query = NewsResearcher.extract_query("Will inflation exceed 5%?")
        self.assertNotIn("?", query)

    def test_date_suffix_stripped(self):
        query = NewsResearcher.extract_query(
            "Will Pam Bondi leave Attorney General before April 2026?"
        )
        self.assertIn("Pam", query)
        self.assertIn("Bondi", query)
        self.assertNotIn("April", query)
        self.assertNotIn("2026", query)

    def test_multi_word_entity(self):
        query = NewsResearcher.extract_query(
            "Will the official trailer for Spider-Man: Beyond release?"
        )
        self.assertIn("Spider-Man:", query)

    def test_max_six_words(self):
        query = NewsResearcher.extract_query(
            "Will the very long market title with many extra words happen soon?"
        )
        words = query.split()
        self.assertLessEqual(len(words), 6)

    def test_short_title_fallback(self):
        """Titles that reduce to <2 words should fallback."""
        query = NewsResearcher.extract_query("Will it?")
        self.assertTrue(len(query.split()) >= 2)

    def test_compound_title_with_quoted_name(self):
        query = NewsResearcher.extract_query(
            'Will "Operation Aurora" be declassified?'
        )
        self.assertIn("Aurora", query)

    def test_is_prefix(self):
        query = NewsResearcher.extract_query("Is the recession coming?")
        self.assertIn("recession", query)
        self.assertIn("coming", query)


class TestSentimentScoring(unittest.TestCase):
    """Test keyword-based sentiment scoring."""

    def test_bullish_text(self):
        scores = NewsResearcher.score_text("Markets rally as stocks surge higher")
        self.assertGreater(scores["bullish_score"], 0)
        self.assertEqual(scores["bearish_score"], 0)

    def test_bearish_text(self):
        scores = NewsResearcher.score_text("Markets crash as selloff deepens")
        self.assertEqual(scores["bullish_score"], 0)
        self.assertGreater(scores["bearish_score"], 0)

    def test_neutral_text(self):
        scores = NewsResearcher.score_text("The meeting was held on Tuesday")
        self.assertEqual(scores["bullish_score"], 0)
        self.assertEqual(scores["bearish_score"], 0)

    def test_mixed_text(self):
        scores = NewsResearcher.score_text("Rally expected despite recession fears")
        self.assertGreater(scores["bullish_score"], 0)
        self.assertGreater(scores["bearish_score"], 0)

    def test_negation_flips_bullish(self):
        """'not bullish' should register as bearish, not bullish."""
        scores = NewsResearcher.score_text("Analysts say market is not bullish")
        self.assertGreater(scores["bearish_score"], 0)
        # "not" + "bullish" → bearish. Only direct negation counted.

    def test_negation_flips_bearish(self):
        """'no recession' should register as bullish, not bearish."""
        scores = NewsResearcher.score_text("Experts see no recession ahead")
        self.assertGreater(scores["bullish_score"], 0)

    def test_negation_with_never(self):
        scores = NewsResearcher.score_text("They will never fail to succeed")
        # "never" + "fail" → fail is bearish, negated → bullish
        self.assertGreater(scores["bullish_score"], 0)

    def test_lose_negation(self):
        """'lose' before a bullish word should flip to bearish."""
        scores = NewsResearcher.score_text("They lose momentum quickly")
        # "lose" + "momentum" → momentum is bullish, negated → bearish
        self.assertGreater(scores["bearish_score"], 0)

    def test_financial_domain_words(self):
        scores = NewsResearcher.score_text("Fed hawkish stance signals tighten policy")
        self.assertGreater(scores["bullish_score"], 0)

        scores2 = NewsResearcher.score_text("Dovish pivot with rate cut expected")
        self.assertGreater(scores2["bearish_score"], 0)


class TestSentimentAnalysis(unittest.TestCase):
    """Test sentiment aggregation across articles."""

    def setUp(self):
        self.researcher = NewsResearcher(settings={
            "research": {
                "sources": [],
                "max_articles_per_source": 20,
                "sentiment_threshold": 0.6,
            }
        })

    def test_zero_articles_returns_neutral(self):
        """Zero-result queries should return low-confidence neutral."""
        result = self.researcher._analyze_sentiment([], "google_news")
        self.assertEqual(result.source, "google_news")
        self.assertEqual(result.neutral, 1.0)
        self.assertLessEqual(result.confidence, 0.2)
        self.assertEqual(result.key_narratives, [])

    def test_single_bullish_article(self):
        articles = [{"title": "Stock rally", "text": "Stock rally continues with strong gains"}]
        result = self.researcher._analyze_sentiment(articles, "test")
        self.assertGreater(result.bullish, 0)

    def test_confidence_scales_with_count(self):
        """More articles = higher confidence."""
        few = [{"title": f"headline {i}", "text": f"rally surge {i}"} for i in range(2)]
        many = [{"title": f"headline {i}", "text": f"rally surge {i}"} for i in range(15)]

        result_few = self.researcher._analyze_sentiment(few, "test")
        result_many = self.researcher._analyze_sentiment(many, "test")

        self.assertGreater(result_many.confidence, result_few.confidence)


class TestConsensusAggregation(unittest.TestCase):
    """Test consensus aggregation across sources."""

    def test_empty_results(self):
        consensus = NewsResearcher._aggregate_consensus([])
        self.assertEqual(consensus["consensus_sentiment"], "neutral")
        self.assertAlmostEqual(consensus["sentiment_implied_probability"], 0.5, places=1)

    def test_bullish_consensus(self):
        results = [
            SentimentResult("a", bullish=0.8, bearish=0.1, neutral=0.1, confidence=0.7, key_narratives=[]),
            SentimentResult("b", bullish=0.6, bearish=0.2, neutral=0.2, confidence=0.5, key_narratives=[]),
        ]
        consensus = NewsResearcher._aggregate_consensus(results)
        self.assertEqual(consensus["consensus_sentiment"], "bullish")
        self.assertGreater(consensus["sentiment_implied_probability"], 0.5)

    def test_bearish_consensus(self):
        results = [
            SentimentResult("a", bullish=0.1, bearish=0.8, neutral=0.1, confidence=0.7, key_narratives=[]),
            SentimentResult("b", bullish=0.1, bearish=0.7, neutral=0.2, confidence=0.6, key_narratives=[]),
        ]
        consensus = NewsResearcher._aggregate_consensus(results)
        self.assertEqual(consensus["consensus_sentiment"], "bearish")
        self.assertLess(consensus["sentiment_implied_probability"], 0.5)

    def test_implied_prob_clamped(self):
        """Implied probability should stay within [0.05, 0.95]."""
        extreme = [
            SentimentResult("x", bullish=1.0, bearish=0.0, neutral=0.0, confidence=1.0, key_narratives=[]),
        ]
        consensus = NewsResearcher._aggregate_consensus(extreme)
        self.assertLessEqual(consensus["sentiment_implied_probability"], 0.95)
        self.assertGreaterEqual(consensus["sentiment_implied_probability"], 0.05)


class TestResearchBriefContract(unittest.TestCase):
    """Verify ResearchBrief has all fields required by S03 contract."""

    REQUIRED_FIELDS = {
        "market_id", "market_title", "current_yes_price", "sources",
        "consensus_sentiment", "consensus_confidence",
        "sentiment_implied_probability", "gap", "gap_direction",
        "narrative_summary", "timestamp",
    }

    def test_dataclass_has_all_contract_fields(self):
        field_names = {f.name for f in fields(ResearchBrief)}
        for req in self.REQUIRED_FIELDS:
            self.assertIn(req, field_names, f"Missing contract field: {req}")

    def test_sentiment_result_contract_fields(self):
        required = {"source", "bullish", "bearish", "neutral", "confidence", "key_narratives"}
        field_names = {f.name for f in fields(SentimentResult)}
        for req in required:
            self.assertIn(req, field_names, f"Missing SentimentResult field: {req}")

    def test_brief_serializable(self):
        """ResearchBrief should be fully JSON-serializable via asdict."""
        brief = ResearchBrief(
            market_id="TEST-123",
            market_title="Will test pass?",
            current_yes_price=0.55,
            sources=[
                SentimentResult("google_news", 0.5, 0.3, 0.2, 0.6, ["headline"]),
            ],
            consensus_sentiment="bullish",
            consensus_confidence=0.6,
            sentiment_implied_probability=0.58,
            gap=0.03,
            gap_direction="underpriced",
            narrative_summary="Test narrative.",
            timestamp="2026-03-13T12:00:00+00:00",
        )
        import json
        data = asdict(brief)
        serialized = json.dumps(data, ensure_ascii=False)
        deserialized = json.loads(serialized)
        self.assertEqual(deserialized["market_id"], "TEST-123")
        self.assertEqual(deserialized["current_yes_price"], 0.55)
        self.assertEqual(len(deserialized["sources"]), 1)
        self.assertIn("consensus_sentiment", deserialized)
        self.assertIn("gap", deserialized)


class TestResearchMarketOffline(unittest.TestCase):
    """Test research_market with mocked data (no network)."""

    def test_research_with_no_sources(self):
        """With no sources configured, should still produce a valid brief."""
        researcher = NewsResearcher(settings={
            "research": {
                "sources": [],  # No fetching
                "max_articles_per_source": 20,
                "sentiment_threshold": 0.6,
            }
        })

        market = {
            "market_id": "TEST-OFFLINE",
            "title": "Will something happen?",
            "yes_price": 0.50,
        }

        brief = researcher.research_market(market)
        self.assertEqual(brief.market_id, "TEST-OFFLINE")
        self.assertEqual(brief.current_yes_price, 0.50)
        self.assertEqual(brief.consensus_sentiment, "neutral")
        self.assertIsInstance(brief.timestamp, str)
        self.assertIsInstance(brief.narrative_summary, str)


if __name__ == "__main__":
    unittest.main()
