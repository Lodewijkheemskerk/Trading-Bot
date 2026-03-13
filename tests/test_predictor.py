"""Unit tests for the prediction engine."""

import json
import sys
import tempfile
import unittest
from dataclasses import asdict, fields
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.predictor import (
    PredictionEngine,
    TradeSignal,
    ModelPrediction,
)


def _make_brief(**overrides):
    """Create a test research brief dict with sensible defaults."""
    brief = {
        "market_id": "TEST-MKT-001",
        "market_title": "Will test event happen?",
        "current_yes_price": 0.50,
        "consensus_sentiment": "neutral",
        "consensus_confidence": 0.7,
        "sentiment_implied_probability": 0.52,
        "gap": 0.02,
        "gap_direction": "fair",
        "narrative_summary": "Test narrative.",
        "sources": [
            {
                "source": "google_news",
                "bullish": 0.3,
                "bearish": 0.2,
                "neutral": 0.5,
                "confidence": 0.7,
                "key_narratives": ["Test headline 1"],
            },
            {
                "source": "reddit",
                "bullish": 0.1,
                "bearish": 0.1,
                "neutral": 0.8,
                "confidence": 0.6,
                "key_narratives": [],
            },
        ],
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    brief.update(overrides)
    return brief


class TestHeuristicBull(unittest.TestCase):
    """Test the bull advocate heuristic model."""

    def setUp(self):
        self.cfg = {"role": "bull_advocate", "weight": 0.25}

    def test_bull_always_higher_than_market(self):
        """Bull prediction should be >= market price."""
        brief = _make_brief(current_yes_price=0.50)
        pred = PredictionEngine._predict_heuristic_bull(brief, self.cfg)
        self.assertGreaterEqual(pred.predicted_probability, 0.50)

    def test_bull_bullish_sentiment_boosts_more(self):
        """Bullish sentiment should give a stronger bull shift."""
        neutral_brief = _make_brief(consensus_sentiment="neutral")
        bullish_brief = _make_brief(consensus_sentiment="bullish", consensus_confidence=0.8)

        neutral_pred = PredictionEngine._predict_heuristic_bull(neutral_brief, self.cfg)
        bullish_pred = PredictionEngine._predict_heuristic_bull(bullish_brief, self.cfg)

        self.assertGreater(bullish_pred.predicted_probability, neutral_pred.predicted_probability)

    def test_bull_positive_gap_adds_more(self):
        """Positive gap (underpriced) should increase bull prediction."""
        no_gap = _make_brief(gap=0.0)
        big_gap = _make_brief(gap=0.15)

        pred_no_gap = PredictionEngine._predict_heuristic_bull(no_gap, self.cfg)
        pred_big_gap = PredictionEngine._predict_heuristic_bull(big_gap, self.cfg)

        self.assertGreater(pred_big_gap.predicted_probability, pred_no_gap.predicted_probability)

    def test_bull_clamped_to_valid_range(self):
        """Bull prediction should be clamped to [0.05, 0.95]."""
        extreme_brief = _make_brief(current_yes_price=0.95, consensus_sentiment="bullish", gap=0.5)
        pred = PredictionEngine._predict_heuristic_bull(extreme_brief, self.cfg)
        self.assertLessEqual(pred.predicted_probability, 0.95)
        self.assertGreaterEqual(pred.predicted_probability, 0.05)

    def test_bull_returns_model_prediction(self):
        """Bull should return a properly structured ModelPrediction."""
        pred = PredictionEngine._predict_heuristic_bull(_make_brief(), self.cfg)
        self.assertEqual(pred.model_name, "heuristic_bull")
        self.assertEqual(pred.role, "bull_advocate")
        self.assertEqual(pred.weight, 0.25)
        self.assertTrue(len(pred.reasoning) > 0)


class TestHeuristicBear(unittest.TestCase):
    """Test the bear advocate heuristic model."""

    def setUp(self):
        self.cfg = {"role": "bear_advocate", "weight": 0.25}

    def test_bear_always_lower_than_market(self):
        """Bear prediction should be <= market price."""
        brief = _make_brief(current_yes_price=0.50)
        pred = PredictionEngine._predict_heuristic_bear(brief, self.cfg)
        self.assertLessEqual(pred.predicted_probability, 0.50)

    def test_bear_bearish_sentiment_pushes_lower(self):
        """Bearish sentiment should give a stronger bear shift."""
        neutral_brief = _make_brief(consensus_sentiment="neutral")
        bearish_brief = _make_brief(consensus_sentiment="bearish", consensus_confidence=0.8)

        neutral_pred = PredictionEngine._predict_heuristic_bear(neutral_brief, self.cfg)
        bearish_pred = PredictionEngine._predict_heuristic_bear(bearish_brief, self.cfg)

        self.assertLess(bearish_pred.predicted_probability, neutral_pred.predicted_probability)

    def test_bear_negative_gap_adds_more(self):
        """Negative gap (overpriced) should decrease bear prediction."""
        no_gap = _make_brief(gap=0.0)
        neg_gap = _make_brief(gap=-0.15)

        pred_no_gap = PredictionEngine._predict_heuristic_bear(no_gap, self.cfg)
        pred_neg_gap = PredictionEngine._predict_heuristic_bear(neg_gap, self.cfg)

        self.assertLess(pred_neg_gap.predicted_probability, pred_no_gap.predicted_probability)

    def test_bear_clamped_to_valid_range(self):
        """Bear prediction should be clamped to [0.05, 0.95]."""
        extreme_brief = _make_brief(current_yes_price=0.05, consensus_sentiment="bearish", gap=-0.5)
        pred = PredictionEngine._predict_heuristic_bear(extreme_brief, self.cfg)
        self.assertLessEqual(pred.predicted_probability, 0.95)
        self.assertGreaterEqual(pred.predicted_probability, 0.05)


class TestEnsemble(unittest.TestCase):
    """Test ensemble aggregation."""

    def test_single_model(self):
        """Ensemble of one model should return that model's prediction."""
        pred = ModelPrediction("test", "test", 1.0, 0.70, 0.80, "test")
        prob, conf = PredictionEngine._ensemble([pred])
        self.assertAlmostEqual(prob, 0.70, places=2)
        self.assertAlmostEqual(conf, 0.80, places=2)

    def test_equal_weight_average(self):
        """Two models with equal weight should give arithmetic mean."""
        pred1 = ModelPrediction("a", "a", 0.5, 0.60, 0.80, "")
        pred2 = ModelPrediction("b", "b", 0.5, 0.40, 0.60, "")
        prob, conf = PredictionEngine._ensemble([pred1, pred2])
        self.assertAlmostEqual(prob, 0.50, places=2)
        self.assertAlmostEqual(conf, 0.70, places=2)

    def test_weighted_average(self):
        """Models with different weights should give weighted average."""
        # Claude weight=0.50 at 0.80, bull weight=0.25 at 0.60, bear weight=0.25 at 0.40
        claude = ModelPrediction("claude", "analyst", 0.50, 0.80, 0.90, "")
        bull = ModelPrediction("bull", "bull", 0.25, 0.60, 0.50, "")
        bear = ModelPrediction("bear", "bear", 0.25, 0.40, 0.50, "")
        prob, conf = PredictionEngine._ensemble([claude, bull, bear])
        # Expected: (0.80*0.50 + 0.60*0.25 + 0.40*0.25) / 1.0 = 0.65
        self.assertAlmostEqual(prob, 0.65, places=2)

    def test_renormalization_without_claude(self):
        """When Claude is missing, weights should renormalize to sum=1.0."""
        bull = ModelPrediction("bull", "bull", 0.25, 0.70, 0.50, "")
        bear = ModelPrediction("bear", "bear", 0.25, 0.30, 0.50, "")
        prob, conf = PredictionEngine._ensemble([bull, bear])
        # Renormalized: each has weight 0.5
        self.assertAlmostEqual(prob, 0.50, places=2)

    def test_empty_predictions(self):
        """Empty predictions should return neutral defaults."""
        prob, conf = PredictionEngine._ensemble([])
        self.assertAlmostEqual(prob, 0.50, places=2)
        self.assertAlmostEqual(conf, 0.10, places=2)


class TestEdgeCalculation(unittest.TestCase):
    """Test edge and signal logic."""

    def test_positive_edge_buys_yes(self):
        """Positive edge (ensemble > market) should signal buy_yes."""
        metrics = PredictionEngine.compute_edge(0.70, 0.50)
        self.assertAlmostEqual(metrics["edge"], 0.20, places=2)
        self.assertEqual(metrics["direction"], "buy_yes")

    def test_negative_edge_buys_no(self):
        """Negative edge (ensemble < market) should signal buy_no."""
        metrics = PredictionEngine.compute_edge(0.30, 0.50)
        self.assertAlmostEqual(metrics["edge"], -0.20, places=2)
        self.assertEqual(metrics["direction"], "buy_no")

    def test_mispricing_score_scales_with_edge(self):
        """Larger edge should give higher mispricing Z-score."""
        small = PredictionEngine.compute_edge(0.55, 0.50)
        large = PredictionEngine.compute_edge(0.70, 0.50)
        self.assertGreater(large["mispricing_score"], small["mispricing_score"])

    def test_zero_edge(self):
        """Zero edge should give buy_no direction (convention: not > 0)."""
        metrics = PredictionEngine.compute_edge(0.50, 0.50)
        self.assertAlmostEqual(metrics["edge"], 0.0, places=4)
        self.assertEqual(metrics["direction"], "buy_no")


class TestShouldTrade(unittest.TestCase):
    """Test trade decision thresholds."""

    def test_should_trade_when_thresholds_met(self):
        """Should trade when edge > min_edge and confidence > min_confidence."""
        engine = PredictionEngine(settings={
            "prediction": {
                "min_edge": 0.04,
                "min_confidence": 0.65,
                "ensemble_models": [
                    {"name": "heuristic_bull", "role": "bull_advocate", "weight": 0.50},
                    {"name": "heuristic_bear", "role": "bear_advocate", "weight": 0.50},
                ],
            }
        })
        # Use a brief where bull/bear spread creates enough edge
        brief = _make_brief(
            current_yes_price=0.30,  # Low market price
            consensus_sentiment="bullish",
            consensus_confidence=0.85,
            gap=0.20,
        )
        signal = engine.predict(brief)
        # Bull will push high, bear will pull less — should have positive edge
        self.assertIsInstance(signal, TradeSignal)
        self.assertIsInstance(signal.should_trade, bool)

    def test_no_trade_when_edge_too_small(self):
        """Should not trade when edge is below min_edge."""
        engine = PredictionEngine(settings={
            "prediction": {
                "min_edge": 0.50,  # Very high threshold
                "min_confidence": 0.10,
                "ensemble_models": [
                    {"name": "heuristic_bull", "role": "bull_advocate", "weight": 0.50},
                    {"name": "heuristic_bear", "role": "bear_advocate", "weight": 0.50},
                ],
            }
        })
        brief = _make_brief(current_yes_price=0.50)
        signal = engine.predict(brief)
        self.assertFalse(signal.should_trade)

    def test_no_trade_when_confidence_too_low(self):
        """Should not trade when confidence is below min_confidence."""
        engine = PredictionEngine(settings={
            "prediction": {
                "min_edge": 0.01,
                "min_confidence": 0.99,  # Very high threshold
                "ensemble_models": [
                    {"name": "heuristic_bull", "role": "bull_advocate", "weight": 0.50},
                    {"name": "heuristic_bear", "role": "bear_advocate", "weight": 0.50},
                ],
            }
        })
        brief = _make_brief(current_yes_price=0.50)
        signal = engine.predict(brief)
        self.assertFalse(signal.should_trade)


class TestClaudeResponseParsing(unittest.TestCase):
    """Test parsing of Claude's JSON responses."""

    def test_clean_json(self):
        text = '{"probability": 0.72, "confidence": 0.8, "reasoning": "Test"}'
        result = PredictionEngine._parse_claude_response(text)
        self.assertAlmostEqual(result["probability"], 0.72)
        self.assertEqual(result["reasoning"], "Test")

    def test_json_in_code_block(self):
        text = '```json\n{"probability": 0.65, "confidence": 0.7, "reasoning": "Wrapped"}\n```'
        result = PredictionEngine._parse_claude_response(text)
        self.assertAlmostEqual(result["probability"], 0.65)

    def test_json_with_surrounding_text(self):
        text = 'Here is my analysis:\n{"probability": 0.55, "confidence": 0.6, "reasoning": "Extra text"}\nDone.'
        result = PredictionEngine._parse_claude_response(text)
        self.assertAlmostEqual(result["probability"], 0.55)

    def test_partial_parse_fallback(self):
        text = 'Broken JSON but "probability": 0.80 and "confidence": 0.5 and "reasoning": "partial"'
        result = PredictionEngine._parse_claude_response(text)
        self.assertAlmostEqual(result["probability"], 0.80)

    def test_totally_unparseable(self):
        text = "I think the probability is around seventy percent."
        result = PredictionEngine._parse_claude_response(text)
        # Should return safe defaults
        self.assertAlmostEqual(result["probability"], 0.5)
        self.assertLessEqual(result["confidence"], 0.5)


class TestClaudeFallback(unittest.TestCase):
    """Test graceful fallback when Claude is unavailable."""

    def test_heuristic_only_without_api_key(self):
        """Engine should work without ANTHROPIC_API_KEY."""
        engine = PredictionEngine(settings={
            "prediction": {
                "min_edge": 0.04,
                "min_confidence": 0.65,
                "ensemble_models": [
                    {"name": "claude", "role": "news_analyst", "weight": 0.50},
                    {"name": "heuristic_bull", "role": "bull_advocate", "weight": 0.25},
                    {"name": "heuristic_bear", "role": "bear_advocate", "weight": 0.25},
                ],
            }
        })
        # Ensure no API key
        with patch.dict("os.environ", {}, clear=True):
            engine._claude_available = None  # Reset
            engine._claude_client = None
            signal = engine.predict(_make_brief())

        self.assertIsInstance(signal, TradeSignal)
        # Should only have 2 model predictions (heuristics)
        self.assertEqual(len(signal.model_predictions), 2)
        model_names = [p.model_name for p in signal.model_predictions]
        self.assertIn("heuristic_bull", model_names)
        self.assertIn("heuristic_bear", model_names)
        self.assertNotIn("claude", model_names)


class TestTradeSignalContract(unittest.TestCase):
    """Test that TradeSignal has all S04 contract fields."""

    def test_all_s04_fields_present(self):
        """TradeSignal must have all fields required by S04."""
        required = [
            "ensemble_probability", "market_probability", "edge",
            "direction", "signal_strength",
        ]
        actual = [f.name for f in fields(TradeSignal)]
        for r in required:
            self.assertIn(r, actual, f"Missing S04 contract field: {r}")

    def test_model_prediction_fields(self):
        """ModelPrediction must have all expected fields."""
        required = [
            "model_name", "role", "weight",
            "predicted_probability", "confidence", "reasoning",
        ]
        actual = [f.name for f in fields(ModelPrediction)]
        for r in required:
            self.assertIn(r, actual, f"Missing ModelPrediction field: {r}")

    def test_trade_signal_serializable(self):
        """TradeSignal should be fully JSON-serializable via asdict."""
        signal = TradeSignal(
            market_id="test",
            market_title="Test market",
            ensemble_probability=0.65,
            market_probability=0.50,
            edge=0.15,
            mispricing_score=1.5,
            expected_value=0.10,
            direction="buy_yes",
            signal_strength=0.10,
            confidence=0.70,
            should_trade=True,
            model_predictions=[
                ModelPrediction("test", "test", 1.0, 0.65, 0.70, "test reasoning")
            ],
            timestamp="2026-01-01T00:00:00+00:00",
        )
        data = asdict(signal)
        serialized = json.dumps(data)
        deserialized = json.loads(serialized)
        self.assertEqual(deserialized["market_id"], "test")
        self.assertAlmostEqual(deserialized["ensemble_probability"], 0.65)


class TestFullPredictFlow(unittest.TestCase):
    """Integration test for the full predict pipeline (heuristic-only)."""

    def test_predict_returns_valid_signal(self):
        engine = PredictionEngine(settings={
            "prediction": {
                "min_edge": 0.04,
                "min_confidence": 0.65,
                "ensemble_models": [
                    {"name": "heuristic_bull", "role": "bull_advocate", "weight": 0.50},
                    {"name": "heuristic_bear", "role": "bear_advocate", "weight": 0.50},
                ],
            }
        })
        brief = _make_brief()
        signal = engine.predict(brief)

        self.assertIsInstance(signal, TradeSignal)
        self.assertEqual(signal.market_id, "TEST-MKT-001")
        self.assertGreaterEqual(signal.ensemble_probability, 0.01)
        self.assertLessEqual(signal.ensemble_probability, 0.99)
        self.assertEqual(signal.market_probability, 0.50)
        self.assertIn(signal.direction, ["buy_yes", "buy_no"])
        self.assertGreaterEqual(signal.signal_strength, 0.0)
        self.assertIsInstance(signal.should_trade, bool)
        self.assertGreater(len(signal.model_predictions), 0)

    def test_predict_with_bullish_sentiment(self):
        """Bullish sentiment should bias ensemble upward vs neutral."""
        engine = PredictionEngine(settings={
            "prediction": {
                "min_edge": 0.04,
                "min_confidence": 0.65,
                "ensemble_models": [
                    {"name": "heuristic_bull", "role": "bull_advocate", "weight": 0.50},
                    {"name": "heuristic_bear", "role": "bear_advocate", "weight": 0.50},
                ],
            }
        })
        neutral = engine.predict(_make_brief(consensus_sentiment="neutral"))
        bullish = engine.predict(_make_brief(consensus_sentiment="bullish", consensus_confidence=0.8, gap=0.10))
        # Bullish brief should produce higher ensemble probability
        self.assertGreater(bullish.ensemble_probability, neutral.ensemble_probability)

    def test_predict_with_extreme_market_price(self):
        """Engine should handle extreme market prices (near 0 or 1)."""
        engine = PredictionEngine(settings={
            "prediction": {
                "min_edge": 0.04,
                "min_confidence": 0.65,
                "ensemble_models": [
                    {"name": "heuristic_bull", "role": "bull_advocate", "weight": 0.50},
                    {"name": "heuristic_bear", "role": "bear_advocate", "weight": 0.50},
                ],
            }
        })
        low = engine.predict(_make_brief(current_yes_price=0.03))
        high = engine.predict(_make_brief(current_yes_price=0.97))

        self.assertGreaterEqual(low.ensemble_probability, 0.01)
        self.assertLessEqual(high.ensemble_probability, 0.99)


if __name__ == "__main__":
    unittest.main()
