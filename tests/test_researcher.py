"""
Tests for researcher.py — Tier 5 Item 19: Additional sources + parallel research.

Tests:
- Bing News RSS fetcher
- Parallel source fetching within a market
- Parallel multi-market research
- Source config from settings
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.researcher import (
    NewsResearcher,
    ResearchBrief,
    SentimentResult,
    save_research_snapshot,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def researcher():
    """NewsResearcher with default settings."""
    return NewsResearcher()


@pytest.fixture
def researcher_all_sources():
    """NewsResearcher with all free sources enabled."""
    settings = {
        "research": {
            "sources": ["google_news_rss", "bing_news_rss", "reddit"],
            "max_articles_per_source": 10,
            "sentiment_threshold": 0.6,
            "parallel_workers": 3,
            "x_search": {"enabled": False},
        },
        "execution": {"retry_attempts": 1, "retry_delay_seconds": 1},
    }
    return NewsResearcher(settings=settings)


@pytest.fixture
def sample_market():
    return {
        "market_id": "TEST-MARKET",
        "title": "Will interest rates be cut before June 2026?",
        "yes_price": 0.45,
    }


@pytest.fixture
def sample_markets():
    return [
        {"market_id": "MKT-A", "title": "Will SpaceX launch Starship in March?", "yes_price": 0.60},
        {"market_id": "MKT-B", "title": "Will Bitcoin exceed 100000 before April?", "yes_price": 0.35},
        {"market_id": "MKT-C", "title": "Will Trump sign new trade deal?", "yes_price": 0.22},
    ]


FAKE_ARTICLES = [
    {"title": "Markets rally on rate cut hopes", "text": "Markets rally on rate cut hopes", "source": "test"},
    {"title": "Fed signals bearish outlook", "text": "Fed signals bearish outlook for economy", "source": "test"},
    {"title": "Rate cut likely in June", "text": "Rate cut likely in June according to analysts", "source": "test"},
]


# ── Config ────────────────────────────────────────────────────────────────


class TestSourceConfig:
    def test_default_sources_include_bing(self, researcher):
        """Bing News RSS should be in default sources after config update."""
        assert "bing_news_rss" in researcher.sources

    def test_all_three_free_sources(self, researcher):
        assert "google_news_rss" in researcher.sources
        assert "bing_news_rss" in researcher.sources
        assert "reddit" in researcher.sources

    def test_parallel_workers_loaded(self, researcher):
        assert researcher.parallel_workers >= 1


# ── Bing News RSS ─────────────────────────────────────────────────────────


class TestBingNewsFetcher:
    def test_fetch_bing_news_method_exists(self, researcher):
        assert hasattr(researcher, "_fetch_bing_news")
        assert callable(researcher._fetch_bing_news)

    def test_fetch_bing_news_returns_list(self, researcher):
        """Live test — hits Bing RSS (free, no key)."""
        articles = researcher._fetch_bing_news("stock market today")
        assert isinstance(articles, list)
        # Bing should return at least a few results for a broad query
        assert len(articles) > 0

    def test_bing_article_shape(self, researcher):
        """Articles have required keys: title, source, text."""
        articles = researcher._fetch_bing_news("artificial intelligence")
        if articles:
            a = articles[0]
            assert "title" in a
            assert "text" in a
            assert "source" in a
            assert len(a["title"]) > 0

    def test_bing_bad_query_returns_empty_or_results(self, researcher):
        """Even a nonsense query should not crash."""
        articles = researcher._fetch_bing_news("xyzzy12345qwert")
        assert isinstance(articles, list)


# ── Parallel Source Fetching ──────────────────────────────────────────────


class TestParallelSourceFetch:
    def test_research_market_returns_multiple_sources(self, researcher_all_sources, sample_market):
        """With 3 sources configured, brief should have 3 SentimentResults."""
        brief = researcher_all_sources.research_market(sample_market)
        assert isinstance(brief, ResearchBrief)
        assert len(brief.sources) == 3

    def test_source_names_match_config(self, researcher_all_sources, sample_market):
        brief = researcher_all_sources.research_market(sample_market)
        source_names = {s.source for s in brief.sources}
        assert "google_news" in source_names
        assert "bing_news" in source_names
        assert "reddit" in source_names

    def test_parallel_faster_than_sequential_threshold(self, researcher_all_sources, sample_market):
        """Parallel fetch should complete in reasonable time (< 15s for 3 sources)."""
        start = time.time()
        brief = researcher_all_sources.research_market(sample_market)
        elapsed = time.time() - start
        assert elapsed < 15, f"Research took {elapsed:.1f}s, expected < 15s"
        assert brief is not None


# ── Parallel Multi-Market Research ────────────────────────────────────────


class TestParallelMarketResearch:
    def test_research_markets_method_exists(self, researcher):
        assert hasattr(researcher, "research_markets")

    def test_research_markets_returns_list(self, researcher_all_sources, sample_markets):
        briefs = researcher_all_sources.research_markets(sample_markets[:2])
        assert isinstance(briefs, list)
        assert len(briefs) == 2

    def test_research_markets_all_briefs_valid(self, researcher_all_sources, sample_markets):
        briefs = researcher_all_sources.research_markets(sample_markets[:2])
        for b in briefs:
            assert isinstance(b, ResearchBrief)
            assert b.market_id in {"MKT-A", "MKT-B"}
            assert len(b.sources) == 3
            assert b.consensus_sentiment in {"bullish", "bearish", "neutral"}

    def test_research_markets_empty_input(self, researcher):
        briefs = researcher.research_markets([])
        assert briefs == []

    def test_research_markets_single_market_no_threads(self):
        """Single market should work without thread overhead."""
        settings = {
            "research": {
                "sources": ["google_news_rss"],
                "max_articles_per_source": 5,
                "parallel_workers": 1,
                "x_search": {"enabled": False},
            },
            "execution": {"retry_attempts": 1, "retry_delay_seconds": 1},
        }
        r = NewsResearcher(settings=settings)
        briefs = r.research_markets([
            {"market_id": "SOLO", "title": "Will Tesla stock rise?", "yes_price": 0.50}
        ])
        assert len(briefs) == 1
        assert briefs[0].market_id == "SOLO"


# ── Sentiment Analysis (unchanged but verify) ────────────────────────────


class TestSentimentIntegration:
    def test_bing_results_get_sentiment(self, researcher):
        """Bing articles should be analyzed for sentiment."""
        articles = [
            {"title": "Markets rally strongly today", "text": "Markets rally strongly today surge gains", "source": "bing"},
            {"title": "Economy shows growth", "text": "Economy shows strong growth optimistic outlook", "source": "bing"},
        ]
        sr = researcher._analyze_sentiment(articles, "bing_news")
        assert isinstance(sr, SentimentResult)
        assert sr.source == "bing_news"
        assert sr.bullish > 0  # "rally", "surge", "growth", "optimistic" are bullish

    def test_consensus_with_three_sources(self, researcher):
        results = [
            SentimentResult(source="google_news", bullish=0.6, bearish=0.2, neutral=0.2, confidence=0.8, key_narratives=["headline1"]),
            SentimentResult(source="bing_news", bullish=0.5, bearish=0.3, neutral=0.2, confidence=0.5, key_narratives=["headline2"]),
            SentimentResult(source="reddit", bullish=0.4, bearish=0.4, neutral=0.2, confidence=0.7, key_narratives=["headline3"]),
        ]
        consensus = researcher._aggregate_consensus(results)
        assert consensus["consensus_sentiment"] in {"bullish", "bearish", "neutral"}
        assert 0 <= consensus["consensus_confidence"] <= 1
        assert 0.05 <= consensus["sentiment_implied_probability"] <= 0.95


# ── Snapshot Saving ───────────────────────────────────────────────────────


class TestSnapshotSave:
    def test_save_includes_all_sources(self, tmp_path, researcher_all_sources, sample_market):
        brief = researcher_all_sources.research_market(sample_market)
        fp = save_research_snapshot([brief], output_dir=tmp_path)
        data = json.loads(fp.read_text())
        assert data["markets_researched"] == 1
        sources = data["briefs"][0]["sources"]
        source_names = {s["source"] for s in sources}
        assert len(source_names) == 3


# ── Pipeline Integration ─────────────────────────────────────────────────


class TestPipelineIntegration:
    def test_pipeline_uses_research_markets(self):
        """Pipeline._step_research should call research_markets (not loop)."""
        from scripts.pipeline import TradingPipeline
        import inspect
        source = inspect.getsource(TradingPipeline._step_research)
        assert "research_markets" in source, "Pipeline should use parallel research_markets method"
        assert "for i, market in enumerate" not in source, "Pipeline should not loop sequentially"
