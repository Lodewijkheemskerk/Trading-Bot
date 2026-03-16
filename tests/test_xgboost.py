"""
Tests for XGBoost calibration model — Tier 5 Item 17.

Tests:
- Feature extraction
- Cold-start training
- Prediction
- Model save/load
- Integration with predictor ensemble
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.xgboost_model import (
    XGBoostCalibrator,
    extract_features,
    features_to_array,
    FEATURE_NAMES,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def sample_market():
    return {
        "yes_price": 0.45,
        "spread": 0.03,
        "volume_24h": 500,
        "open_interest": 2000,
        "days_to_expiry": 15,
        "price_move_24h": 0.02,
        "volume_spike": 1.2,
    }


@pytest.fixture
def sample_brief():
    return {
        "sources": [
            {"bullish": 0.5, "bearish": 0.2, "neutral": 0.3, "confidence": 0.7},
            {"bullish": 0.4, "bearish": 0.3, "neutral": 0.3, "confidence": 0.6},
            {"bullish": 0.6, "bearish": 0.1, "neutral": 0.3, "confidence": 0.8},
        ],
        "consensus_confidence": 0.70,
        "gap": 0.08,
    }


@pytest.fixture
def trained_model(tmp_path):
    """Train a model on synthetic data for testing."""
    X, y = XGBoostCalibrator.generate_synthetic_training_data(n_samples=500, seed=99)
    cal = XGBoostCalibrator()
    cal.train(X, y, n_estimators=50, max_depth=3)
    cal.save(tmp_path / "test_model.json")
    return cal, tmp_path / "test_model.json"


# ── Feature Extraction ───────────────────────────────────────────────────


class TestFeatureExtraction:
    def test_returns_all_features(self, sample_market, sample_brief):
        features = extract_features(sample_market, sample_brief)
        assert isinstance(features, dict)
        for name in FEATURE_NAMES:
            assert name in features, f"Missing feature: {name}"

    def test_feature_values_reasonable(self, sample_market, sample_brief):
        features = extract_features(sample_market, sample_brief)
        assert features["yes_price"] == 0.45
        assert features["days_to_expiry"] == 15
        assert features["gap"] == 0.08
        assert features["source_count"] == 3.0

    def test_bullish_avg_computed(self, sample_market, sample_brief):
        features = extract_features(sample_market, sample_brief)
        expected = (0.5 + 0.4 + 0.6) / 3
        assert abs(features["bullish_avg"] - expected) < 0.01

    def test_sentiment_spread(self, sample_market, sample_brief):
        features = extract_features(sample_market, sample_brief)
        assert features["sentiment_spread"] > 0  # More bullish than bearish

    def test_empty_sources(self, sample_market):
        brief = {"sources": [], "consensus_confidence": 0.3, "gap": 0.0}
        features = extract_features(sample_market, brief)
        assert features["source_count"] == 0
        assert features["bullish_avg"] == 0
        assert features["neutral_avg"] == 1.0

    def test_features_to_array_order(self, sample_market, sample_brief):
        features = extract_features(sample_market, sample_brief)
        arr = features_to_array(features)
        assert isinstance(arr, np.ndarray)
        assert arr.shape == (len(FEATURE_NAMES),)
        assert arr[0] == features["yes_price"]  # First feature

    def test_log_transforms(self, sample_market, sample_brief):
        features = extract_features(sample_market, sample_brief)
        assert features["volume_24h_log"] == float(np.log1p(500))
        assert features["open_interest_log"] == float(np.log1p(2000))

    def test_spread_pct(self, sample_market, sample_brief):
        features = extract_features(sample_market, sample_brief)
        assert abs(features["spread_pct"] - 0.03 / 0.45) < 0.001


# ── Training ─────────────────────────────────────────────────────────────


class TestTraining:
    def test_synthetic_data_shape(self):
        X, y = XGBoostCalibrator.generate_synthetic_training_data(n_samples=100)
        assert X.shape == (100, len(FEATURE_NAMES))
        assert y.shape == (100,)
        assert y.min() >= 0.05
        assert y.max() <= 0.95

    def test_train_returns_metrics(self):
        X, y = XGBoostCalibrator.generate_synthetic_training_data(n_samples=200)
        cal = XGBoostCalibrator()
        metrics = cal.train(X, y, n_estimators=30, max_depth=3)
        assert "rmse" in metrics
        assert "mae" in metrics
        assert "r2" in metrics
        assert metrics["rmse"] < 0.15  # Should fit reasonably
        assert cal.is_trained

    def test_cold_start(self, tmp_path):
        from scripts.xgboost_model import MODEL_PATH, TRAINING_LOG_PATH
        import scripts.xgboost_model as xm

        # Temporarily override paths
        orig_model = xm.MODEL_PATH
        orig_log = xm.TRAINING_LOG_PATH
        xm.MODEL_PATH = tmp_path / "model.json"
        xm.TRAINING_LOG_PATH = tmp_path / "log.json"

        try:
            cal = XGBoostCalibrator.train_cold_start(n_samples=300)
            assert cal.is_trained
            assert (tmp_path / "model.json").exists()
            assert (tmp_path / "log.json").exists()

            log = json.loads((tmp_path / "log.json").read_text())
            assert log["type"] == "cold_start"
            assert "metrics" in log
            assert "feature_importance" in log
        finally:
            xm.MODEL_PATH = orig_model
            xm.TRAINING_LOG_PATH = orig_log


# ── Prediction ───────────────────────────────────────────────────────────


class TestPrediction:
    def test_untrained_returns_0_5(self):
        cal = XGBoostCalibrator()
        assert not cal.is_trained
        assert cal.predict_proba({"yes_price": 0.4}) == 0.5

    def test_trained_returns_reasonable_prob(self, trained_model, sample_market, sample_brief):
        cal, _ = trained_model
        features = extract_features(sample_market, sample_brief)
        prob = cal.predict_proba(features)
        assert 0.01 <= prob <= 0.99

    def test_predict_from_data(self, trained_model, sample_market, sample_brief):
        cal, _ = trained_model
        prob = cal.predict_from_data(sample_market, sample_brief)
        assert 0.01 <= prob <= 0.99

    def test_higher_bullish_gives_higher_prob(self, trained_model):
        cal, _ = trained_model

        bullish_market = {"yes_price": 0.50, "spread": 0.02, "volume_24h": 300,
                          "open_interest": 1000, "days_to_expiry": 20,
                          "price_move_24h": 0.0, "volume_spike": 1.0}
        bullish_brief = {
            "sources": [{"bullish": 0.8, "bearish": 0.1, "neutral": 0.1, "confidence": 0.8}],
            "consensus_confidence": 0.8, "gap": 0.15,
        }
        bearish_brief = {
            "sources": [{"bullish": 0.1, "bearish": 0.8, "neutral": 0.1, "confidence": 0.8}],
            "consensus_confidence": 0.8, "gap": -0.15,
        }

        prob_bull = cal.predict_from_data(bullish_market, bullish_brief)
        prob_bear = cal.predict_from_data(bullish_market, bearish_brief)
        assert prob_bull > prob_bear, f"Bullish ({prob_bull}) should be > bearish ({prob_bear})"


# ── Save / Load ──────────────────────────────────────────────────────────


class TestPersistence:
    def test_save_and_load(self, trained_model, sample_market, sample_brief):
        cal, path = trained_model

        # Predict before save
        prob_before = cal.predict_from_data(sample_market, sample_brief)

        # Load from file
        loaded = XGBoostCalibrator.load(path)
        assert loaded.is_trained
        prob_after = loaded.predict_from_data(sample_market, sample_brief)

        assert abs(prob_before - prob_after) < 0.001

    def test_load_missing_file(self, tmp_path):
        cal = XGBoostCalibrator.load(tmp_path / "nonexistent.json")
        assert not cal.is_trained

    def test_feature_importance(self, trained_model):
        cal, _ = trained_model
        imp = cal.feature_importance()
        assert isinstance(imp, dict)
        assert len(imp) == len(FEATURE_NAMES)
        assert sum(imp.values()) > 0


# ── Predictor Integration ────────────────────────────────────────────────


class TestPredictorIntegration:
    def test_predictor_loads_xgboost(self):
        """PredictionEngine should detect xgboost in model_configs."""
        from scripts.predictor import PredictionEngine
        import inspect

        source = inspect.getsource(PredictionEngine.__init__)
        assert "xgboost" in source.lower()
        assert "XGBoostCalibrator" in source

    def test_predict_method_has_xgboost_branch(self):
        """predict() should have XGBoost prediction code."""
        from scripts.predictor import PredictionEngine
        import inspect

        source = inspect.getsource(PredictionEngine.predict)
        assert "xgboost" in source.lower()
        assert "statistical_calibrator" in source

    def test_predict_accepts_market_data(self):
        """predict() should accept optional market_data parameter."""
        from scripts.predictor import PredictionEngine
        import inspect

        sig = inspect.signature(PredictionEngine.predict)
        assert "market_data" in sig.parameters

    def test_xgboost_in_settings(self):
        """settings.yaml should have xgboost in ensemble_models."""
        from config import load_settings
        s = load_settings()
        models = s.get("prediction", {}).get("ensemble_models", [])
        names = [m["name"] for m in models]
        assert "xgboost" in names

        xgb = next(m for m in models if m["name"] == "xgboost")
        assert xgb["role"] == "statistical_calibrator"
        assert xgb["weight"] > 0
