"""
Statistical calibration model using XGBoost.

Sits alongside the LLM ensemble in the prediction pipeline. Takes numerical
features from scanner output (market data) and researcher output (sentiment)
and produces a probability estimate. Gets blended into the ensemble as one
more model — purely data-driven, no API cost.

Features:
  Market:    yes_price, spread, volume_24h, open_interest, days_to_expiry,
             price_move_24h, volume_spike
  Sentiment: bullish_google, bearish_google, bullish_bing, bearish_bing,
             bullish_reddit, bearish_reddit, consensus_confidence, gap
  Derived:   spread_pct, volume_oi_ratio, price_momentum

Training:
  - Cold-start: trained on synthetic calibration data derived from
    market prices + sentiment signals
  - Continuous: retrained when resolved trades accumulate (outcome feedback)
  - Model persisted to config/xgboost_model.json

Usage in predictor.py:
  model = XGBoostCalibrator.load()
  prob = model.predict_proba(features_dict)
"""

import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Allow running both as `python scripts/xgboost_model.py` and as an import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

# Ordered feature names — must match between training and prediction
FEATURE_NAMES = [
    # Market data (from scanner)
    "yes_price",
    "spread",
    "spread_pct",          # spread / yes_price (relative)
    "volume_24h_log",      # log(1 + volume_24h)
    "open_interest_log",   # log(1 + open_interest)
    "days_to_expiry",
    "price_move_24h",
    "volume_spike",
    "volume_oi_ratio",     # volume_24h / max(open_interest, 1)
    # Sentiment (from researcher)
    "bullish_avg",         # average bullish across sources
    "bearish_avg",         # average bearish across sources
    "neutral_avg",         # average neutral across sources
    "sentiment_spread",    # bullish_avg - bearish_avg
    "consensus_confidence",
    "gap",                 # sentiment_implied_prob - market_price
    "gap_abs",             # abs(gap)
    "source_count",        # number of sources
    "source_agreement",    # 1 - std(bullish across sources) — high = sources agree
]


def extract_features(
    market_data: Dict[str, Any],
    research_brief: Dict[str, Any],
) -> Dict[str, float]:
    """
    Extract feature vector from scanner market data and researcher brief.

    Returns dict mapping feature_name → float value.
    Both inputs are dicts (from asdict() or JSON).
    """
    yes_price = float(market_data.get("yes_price", 0.5))
    spread = float(market_data.get("spread", 0.0))
    volume = float(market_data.get("volume_24h", 0))
    oi = float(market_data.get("open_interest", 0))
    days = float(market_data.get("days_to_expiry", 30))
    move = float(market_data.get("price_move_24h", 0.0))
    v_spike = float(market_data.get("volume_spike", 1.0))

    # Sentiment aggregation from sources
    sources = research_brief.get("sources", [])
    if sources:
        bull_vals = [float(s.get("bullish", 0)) for s in sources]
        bear_vals = [float(s.get("bearish", 0)) for s in sources]
        neut_vals = [float(s.get("neutral", 0)) for s in sources]
    else:
        bull_vals = [0.0]
        bear_vals = [0.0]
        neut_vals = [1.0]

    bull_avg = np.mean(bull_vals)
    bear_avg = np.mean(bear_vals)
    neut_avg = np.mean(neut_vals)

    # Source agreement: low std across sources = high agreement
    source_agreement = 1.0 - float(np.std(bull_vals)) if len(bull_vals) > 1 else 0.5

    gap = float(research_brief.get("gap", 0.0))
    conf = float(research_brief.get("consensus_confidence", 0.5))

    features = {
        "yes_price": yes_price,
        "spread": spread,
        "spread_pct": spread / max(yes_price, 0.01),
        "volume_24h_log": float(np.log1p(volume)),
        "open_interest_log": float(np.log1p(oi)),
        "days_to_expiry": days,
        "price_move_24h": move,
        "volume_spike": v_spike,
        "volume_oi_ratio": volume / max(oi, 1),
        "bullish_avg": bull_avg,
        "bearish_avg": bear_avg,
        "neutral_avg": neut_avg,
        "sentiment_spread": bull_avg - bear_avg,
        "consensus_confidence": conf,
        "gap": gap,
        "gap_abs": abs(gap),
        "source_count": float(len(sources)),
        "source_agreement": source_agreement,
    }

    return features


def features_to_array(features: Dict[str, float]) -> np.ndarray:
    """Convert feature dict to numpy array in canonical order."""
    return np.array([features.get(f, 0.0) for f in FEATURE_NAMES], dtype=np.float32)


# ---------------------------------------------------------------------------
# XGBoost Calibrator
# ---------------------------------------------------------------------------

MODEL_PATH = Path(__file__).resolve().parent.parent / "config" / "xgboost_model.json"
TRAINING_LOG_PATH = Path(__file__).resolve().parent.parent / "config" / "xgboost_training_log.json"


class XGBoostCalibrator:
    """
    XGBoost-based probability calibrator for prediction markets.

    Produces a probability estimate from market + sentiment features.
    Can be used standalone or blended into the LLM ensemble.
    """

    def __init__(self, model=None):
        self._model = model
        self._is_trained = model is not None

    @property
    def is_trained(self) -> bool:
        return self._is_trained

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_proba(self, features: Dict[str, float]) -> float:
        """
        Predict probability from feature dict.

        Returns float in (0, 1). Falls back to 0.5 if model not trained.
        """
        if not self._is_trained:
            return 0.5

        X = features_to_array(features).reshape(1, -1)
        try:
            prob = float(self._model.predict(X)[0])
            return max(0.01, min(0.99, prob))
        except Exception as exc:
            logger.warning("XGBoost prediction failed: %s", exc)
            return 0.5

    def predict_from_data(
        self,
        market_data: Dict[str, Any],
        research_brief: Dict[str, Any],
    ) -> float:
        """Convenience: extract features and predict in one call."""
        features = extract_features(market_data, research_brief)
        return self.predict_proba(features)

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        learning_rate: float = 0.05,
        n_estimators: int = 200,
        max_depth: int = 4,
        min_child_weight: int = 3,
        subsample: float = 0.8,
        colsample_bytree: float = 0.8,
        reg_alpha: float = 0.1,
        reg_lambda: float = 1.0,
    ) -> Dict[str, float]:
        """
        Train the XGBoost model.

        Args:
            X: Feature matrix (n_samples, n_features).
            y: Target probabilities or binary outcomes (n_samples,).

        Returns:
            Dict with training metrics (rmse, mae, r2).
        """
        import xgboost as xgb
        from sklearn.model_selection import cross_val_score

        self._model = xgb.XGBRegressor(
            objective="reg:squarederror",
            learning_rate=learning_rate,
            n_estimators=n_estimators,
            max_depth=max_depth,
            min_child_weight=min_child_weight,
            subsample=subsample,
            colsample_bytree=colsample_bytree,
            reg_alpha=reg_alpha,
            reg_lambda=reg_lambda,
            random_state=42,
            verbosity=0,
        )

        self._model.fit(X, y)
        self._is_trained = True

        # Compute metrics
        preds = self._model.predict(X)
        preds = np.clip(preds, 0.01, 0.99)

        rmse = float(np.sqrt(np.mean((preds - y) ** 2)))
        mae = float(np.mean(np.abs(preds - y)))
        ss_res = np.sum((y - preds) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r2 = float(1 - ss_res / max(ss_tot, 1e-10))

        # Cross-validation if enough samples
        cv_rmse = None
        if len(y) >= 20:
            try:
                cv_scores = cross_val_score(
                    self._model, X, y,
                    cv=min(5, len(y) // 4),
                    scoring="neg_root_mean_squared_error",
                )
                cv_rmse = float(-np.mean(cv_scores))
            except Exception:
                pass

        metrics = {"rmse": rmse, "mae": mae, "r2": r2}
        if cv_rmse is not None:
            metrics["cv_rmse"] = cv_rmse

        logger.info(
            "XGBoost trained: %d samples, RMSE=%.4f, MAE=%.4f, R²=%.4f%s",
            len(y), rmse, mae, r2,
            f", CV-RMSE={cv_rmse:.4f}" if cv_rmse else "",
        )

        return metrics

    def feature_importance(self) -> Dict[str, float]:
        """Return feature importance scores."""
        if not self._is_trained:
            return {}
        imp = self._model.feature_importances_
        return {FEATURE_NAMES[i]: float(imp[i]) for i in range(len(imp))}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Optional[Path] = None) -> Path:
        """Save model to JSON file."""
        p = path or MODEL_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        if self._is_trained:
            self._model.save_model(str(p))
            logger.info("XGBoost model saved: %s", p)
        return p

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "XGBoostCalibrator":
        """Load model from JSON file. Returns untrained instance if file missing."""
        p = path or MODEL_PATH
        if not p.exists():
            logger.info("No XGBoost model found at %s — using untrained", p)
            return cls()

        try:
            import xgboost as xgb
            model = xgb.XGBRegressor()
            model.load_model(str(p))
            logger.info("XGBoost model loaded from %s", p)
            return cls(model=model)
        except Exception as exc:
            logger.warning("Failed to load XGBoost model: %s", exc)
            return cls()

    # ------------------------------------------------------------------
    # Synthetic training data (cold-start)
    # ------------------------------------------------------------------

    @staticmethod
    def generate_synthetic_training_data(n_samples: int = 2000, seed: int = 42) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generate synthetic training data for cold-start.

        Simulates realistic prediction market scenarios where:
        - Market prices are somewhat efficient (true prob ≈ market price ± noise)
        - Sentiment signals provide weak but real information
        - Volume and liquidity correlate with price efficiency
        - Days to expiry affects uncertainty

        Returns (X, y) where y is the "true" probability.
        """
        rng = np.random.RandomState(seed)

        # Generate true probabilities (uniform)
        true_probs = rng.uniform(0.05, 0.95, n_samples)

        # Market price: true prob + market noise (efficient-ish)
        market_noise = rng.normal(0, 0.08, n_samples)
        yes_prices = np.clip(true_probs + market_noise, 0.02, 0.98)

        # Spread: wider for extreme prices and low liquidity
        spreads = rng.uniform(0.01, 0.08, n_samples)

        # Volume: log-normal
        volumes = np.exp(rng.normal(6, 2, n_samples))  # median ~400

        # Open interest
        ois = np.exp(rng.normal(7, 1.5, n_samples))

        # Days to expiry
        days = rng.uniform(1, 90, n_samples)

        # Price move 24h: near-zero centered
        moves = rng.normal(0, 0.03, n_samples)

        # Volume spike
        spikes = np.abs(rng.normal(1.0, 0.5, n_samples))

        # Sentiment: weakly correlated with true probability
        # Higher true prob → slightly more bullish sentiment
        sent_signal = (true_probs - 0.5) * 0.6  # Weak signal
        sent_noise = rng.normal(0, 0.15, n_samples)

        bullish_avg = np.clip(0.3 + sent_signal + sent_noise, 0, 1)
        bearish_avg = np.clip(0.3 - sent_signal + sent_noise, 0, 1)
        neutral_avg = np.clip(1.0 - bullish_avg - bearish_avg, 0, 1)

        # Consensus confidence: higher when more data available
        conf = np.clip(0.4 + np.log1p(volumes) * 0.03 + rng.normal(0, 0.1, n_samples), 0.1, 0.95)

        # Gap: sentiment-implied vs market
        sent_implied = 0.5 + (bullish_avg - bearish_avg) * conf * 0.5
        gaps = sent_implied - yes_prices

        # Build feature matrix
        X = np.column_stack([
            yes_prices,
            spreads,
            spreads / np.maximum(yes_prices, 0.01),  # spread_pct
            np.log1p(volumes),
            np.log1p(ois),
            days,
            moves,
            spikes,
            volumes / np.maximum(ois, 1),  # volume_oi_ratio
            bullish_avg,
            bearish_avg,
            neutral_avg,
            bullish_avg - bearish_avg,  # sentiment_spread
            conf,
            gaps,
            np.abs(gaps),  # gap_abs
            rng.choice([2, 3], n_samples).astype(float),  # source_count
            np.clip(1.0 - rng.uniform(0, 0.3, n_samples), 0.5, 1.0),  # source_agreement
        ]).astype(np.float32)

        return X, true_probs.astype(np.float32)

    @classmethod
    def train_cold_start(cls, n_samples: int = 2000) -> "XGBoostCalibrator":
        """
        Train from synthetic data for cold-start deployment.

        Returns a trained XGBoostCalibrator and saves the model.
        """
        logger.info("Training XGBoost cold-start model with %d synthetic samples...", n_samples)

        X, y = cls.generate_synthetic_training_data(n_samples)
        cal = cls()
        metrics = cal.train(X, y)
        cal.save()

        # Save training log
        log = {
            "type": "cold_start",
            "n_samples": n_samples,
            "n_features": len(FEATURE_NAMES),
            "feature_names": FEATURE_NAMES,
            "metrics": metrics,
            "feature_importance": cal.feature_importance(),
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        }
        TRAINING_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TRAINING_LOG_PATH, "w") as f:
            json.dump(log, f, indent=2)
        logger.info("Training log saved: %s", TRAINING_LOG_PATH)

        return cal

    # ------------------------------------------------------------------
    # Retrain from resolved trades
    # ------------------------------------------------------------------

    @classmethod
    def train_from_history(
        cls,
        trades_dir: Optional[Path] = None,
        research_dir: Optional[Path] = None,
        scan_dir: Optional[Path] = None,
    ) -> Optional["XGBoostCalibrator"]:
        """
        Retrain from resolved trade outcomes + original features.

        Looks for trades with status='closed' and an actual outcome,
        matches them back to their research/scan data, and trains.
        Falls back to cold-start if insufficient data (< 30 resolved trades).

        Returns trained calibrator or None if not enough data.
        """
        from config import TRADES_DIR, RESEARCH_DIR, MARKET_DIR

        t_dir = trades_dir or TRADES_DIR
        r_dir = research_dir or RESEARCH_DIR
        s_dir = scan_dir or MARKET_DIR

        # Collect resolved trades
        resolved = []
        for fp in sorted(t_dir.glob("execution_*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for t in data.get("trades", []):
                    if t.get("status") == "closed" and "pnl" in t:
                        resolved.append(t)
            except Exception:
                continue

        if len(resolved) < 30:
            logger.info(
                "Only %d resolved trades (need 30+). Using cold-start model.",
                len(resolved),
            )
            return None

        logger.info("Retraining XGBoost from %d resolved trades", len(resolved))

        # Build training data from historical snapshots
        # For now, use the trade's own fields as proxy features
        # A full implementation would match back to original research/scan snapshots
        X_list = []
        y_list = []

        for t in resolved:
            mp = float(t.get("entry_price", 0.5))
            edge = float(t.get("edge", 0.0))
            pnl = float(t.get("pnl", 0.0))

            # Outcome: 1 if profitable, 0 if loss
            # True probability proxy: market price + edge direction
            outcome = 1.0 if pnl > 0 else 0.0

            # Minimal feature set from trade data
            features = {
                "yes_price": mp,
                "spread": 0.02,  # Not stored in trade — use default
                "spread_pct": 0.04,
                "volume_24h_log": 6.0,
                "open_interest_log": 7.0,
                "days_to_expiry": 30.0,
                "price_move_24h": 0.0,
                "volume_spike": 1.0,
                "volume_oi_ratio": 0.5,
                "bullish_avg": 0.35 + edge * 2,
                "bearish_avg": 0.35 - edge * 2,
                "neutral_avg": 0.3,
                "sentiment_spread": edge * 4,
                "consensus_confidence": float(t.get("signal_strength", 0.5)),
                "gap": edge,
                "gap_abs": abs(edge),
                "source_count": 3.0,
                "source_agreement": 0.7,
            }

            X_list.append(features_to_array(features))
            y_list.append(outcome)

        X = np.array(X_list, dtype=np.float32)
        y = np.array(y_list, dtype=np.float32)

        cal = cls()
        metrics = cal.train(X, y, n_estimators=100, max_depth=3)
        cal.save()

        log = {
            "type": "retrained_from_history",
            "n_samples": len(resolved),
            "metrics": metrics,
            "feature_importance": cal.feature_importance(),
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        }
        with open(TRAINING_LOG_PATH, "w") as f:
            json.dump(log, f, indent=2)

        return cal


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="XGBoost calibration model for prediction markets.")
    parser.add_argument("--train", action="store_true", help="Train cold-start model from synthetic data.")
    parser.add_argument("--retrain", action="store_true", help="Retrain from resolved trade history.")
    parser.add_argument("--info", action="store_true", help="Show model info and feature importance.")
    parser.add_argument("--test", action="store_true", help="Run a test prediction with sample data.")
    args = parser.parse_args()

    if args.train:
        cal = XGBoostCalibrator.train_cold_start()
        print(f"\nModel saved to {MODEL_PATH}")
        print(f"Training log: {TRAINING_LOG_PATH}")
        imp = cal.feature_importance()
        print("\nTop features:")
        for k, v in sorted(imp.items(), key=lambda x: x[1], reverse=True)[:8]:
            print(f"  {k:<25} {v:.4f}")

    elif args.retrain:
        cal = XGBoostCalibrator.train_from_history()
        if cal:
            print("Model retrained from historical trades.")
        else:
            print("Not enough resolved trades. Run --train for cold-start.")

    elif args.info:
        cal = XGBoostCalibrator.load()
        if cal.is_trained:
            imp = cal.feature_importance()
            print("XGBoost model loaded. Feature importance:")
            for k, v in sorted(imp.items(), key=lambda x: x[1], reverse=True):
                bar = "#" * int(v * 50)
                print(f"  {k:<25} {v:.4f}  {bar}")
        else:
            print("No trained model. Run --train first.")

    elif args.test:
        cal = XGBoostCalibrator.load()
        if not cal.is_trained:
            print("No trained model. Training cold-start...")
            cal = XGBoostCalibrator.train_cold_start()

        sample_market = {
            "yes_price": 0.45, "spread": 0.03, "volume_24h": 500,
            "open_interest": 2000, "days_to_expiry": 15,
            "price_move_24h": 0.02, "volume_spike": 1.2,
        }
        sample_brief = {
            "sources": [
                {"bullish": 0.5, "bearish": 0.2, "neutral": 0.3, "confidence": 0.7},
                {"bullish": 0.4, "bearish": 0.3, "neutral": 0.3, "confidence": 0.6},
            ],
            "consensus_confidence": 0.65,
            "gap": 0.08,
        }

        prob = cal.predict_from_data(sample_market, sample_brief)
        print(f"\nSample prediction: {prob:.4f}")
        print(f"Market price:      {sample_market['yes_price']:.4f}")
        print(f"XGBoost edge:      {prob - sample_market['yes_price']:+.4f}")

    else:
        parser.print_help()
