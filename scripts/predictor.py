"""
Step 3: PREDICT — Estimate True Probabilities

Takes ResearchBrief dicts (from the research agent), runs them through
a Claude AI model and two heuristic models (bull/bear advocates),
produces an ensemble probability estimate with edge calculation,
mispricing Z-score, and a trade/no-trade decision.

Ensemble weights from config/settings.yaml:
  - claude (news_analyst): 0.50
  - heuristic_bull (bull_advocate): 0.25
  - heuristic_bear (bear_advocate): 0.25

Falls back to heuristic-only mode when ANTHROPIC_API_KEY is not set
or Claude API calls fail.
"""

import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

# Allow running both as `python scripts/predictor.py` and as an import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, PREDICTIONS_DIR, RESEARCH_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes — S03→S04 boundary contract
# ---------------------------------------------------------------------------

@dataclass
class ModelPrediction:
    """A single model's probability prediction."""
    model_name: str            # "claude", "heuristic_bull", "heuristic_bear"
    role: str                  # "news_analyst", "bull_advocate", "bear_advocate"
    weight: float              # Ensemble weight (0.0-1.0)
    predicted_probability: float  # Model's estimated probability (0.0-1.0)
    confidence: float          # Model's self-reported confidence (0.0-1.0)
    reasoning: str             # Brief explanation of the prediction


@dataclass
class TradeSignal:
    """Ensemble prediction output — consumed by S04 risk/execution."""
    market_id: str
    market_title: str
    ensemble_probability: float    # Weighted average probability
    market_probability: float      # Current market yes_price
    edge: float                    # ensemble_prob - market_prob
    mispricing_score: float        # Z-score: abs(edge) / baseline_std
    expected_value: float          # edge * confidence
    direction: str                 # "buy_yes" or "buy_no"
    signal_strength: float         # abs(edge) * confidence
    confidence: float              # Ensemble confidence
    should_trade: bool             # Meets min_edge and min_confidence thresholds
    model_predictions: List[ModelPrediction]
    timestamp: str                 # ISO datetime


# ---------------------------------------------------------------------------
# PredictionEngine
# ---------------------------------------------------------------------------

class PredictionEngine:
    """Runs research briefs through an ensemble of models to produce trade signals."""

    # Baseline std for mispricing Z-score (conservative assumption)
    BASELINE_STD = 0.10

    def __init__(self, settings: Optional[dict] = None):
        s = settings or load_settings()
        pred_cfg = s.get("prediction", {})

        self.min_edge = pred_cfg.get("min_edge", 0.04)
        self.min_confidence = pred_cfg.get("min_confidence", 0.65)

        # Load ensemble model configs
        self.model_configs = {}
        for m in pred_cfg.get("ensemble_models", []):
            self.model_configs[m["name"]] = {
                "role": m["role"],
                "weight": m["weight"],
            }

        # Lazy Claude client — only created when needed
        self._claude_client = None
        self._claude_available = None  # None = not checked yet

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict(self, research_brief: Dict[str, Any]) -> TradeSignal:
        """
        Run prediction ensemble on a research brief and return a TradeSignal.

        Args:
            research_brief: Dict from asdict(ResearchBrief) — must have
                            market_id, market_title, current_yes_price,
                            consensus_sentiment, consensus_confidence,
                            gap, narrative_summary.
        """
        market_id = research_brief.get("market_id", "unknown")
        market_title = research_brief.get("market_title", "")
        market_prob = float(research_brief.get("current_yes_price", 0.5))

        predictions: List[ModelPrediction] = []

        # --- Claude model ---
        claude_cfg = self.model_configs.get("claude", {"role": "news_analyst", "weight": 0.50})
        claude_pred = self._predict_claude(research_brief, claude_cfg)
        if claude_pred is not None:
            predictions.append(claude_pred)

        # --- Heuristic bull ---
        bull_cfg = self.model_configs.get("heuristic_bull", {"role": "bull_advocate", "weight": 0.25})
        bull_pred = self._predict_heuristic_bull(research_brief, bull_cfg)
        predictions.append(bull_pred)

        # --- Heuristic bear ---
        bear_cfg = self.model_configs.get("heuristic_bear", {"role": "bear_advocate", "weight": 0.25})
        bear_pred = self._predict_heuristic_bear(research_brief, bear_cfg)
        predictions.append(bear_pred)

        # --- Ensemble ---
        ensemble_prob, ensemble_conf = self._ensemble(predictions)

        # --- Edge & signal ---
        edge = round(ensemble_prob - market_prob, 4)
        direction = "buy_yes" if edge > 0 else "buy_no"
        signal_strength = round(abs(edge) * ensemble_conf, 4)
        mispricing_score = round(abs(edge) / self.BASELINE_STD, 2)
        expected_value = round(edge * ensemble_conf, 4)

        # --- Trade decision ---
        should_trade = abs(edge) >= self.min_edge and ensemble_conf >= self.min_confidence

        logger.info(
            "%s: ensemble=%.3f market=%.3f edge=%+.3f dir=%s strength=%.3f trade=%s",
            market_id, ensemble_prob, market_prob, edge, direction,
            signal_strength, should_trade,
        )

        return TradeSignal(
            market_id=market_id,
            market_title=market_title,
            ensemble_probability=ensemble_prob,
            market_probability=market_prob,
            edge=edge,
            mispricing_score=mispricing_score,
            expected_value=expected_value,
            direction=direction,
            signal_strength=signal_strength,
            confidence=ensemble_conf,
            should_trade=should_trade,
            model_predictions=predictions,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def save_prediction(self, signal: TradeSignal, output_dir: Optional[Path] = None) -> Path:
        """Save a TradeSignal as JSON."""
        out = output_dir or PREDICTIONS_DIR
        out.mkdir(parents=True, exist_ok=True)

        safe_id = re.sub(r"[^\w\-]", "_", signal.market_id)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = out / f"prediction_{safe_id}_{ts}.json"

        data = asdict(signal)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        logger.info("Prediction saved: %s", fp)
        return fp

    # ------------------------------------------------------------------
    # Claude AI model
    # ------------------------------------------------------------------

    def _get_claude_client(self):
        """Lazy-initialize Claude client. Returns None if API key not available."""
        if self._claude_available is False:
            return None

        if self._claude_client is not None:
            return self._claude_client

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set — Claude model unavailable, using heuristic-only mode")
            self._claude_available = False
            return None

        try:
            import anthropic
            self._claude_client = anthropic.Anthropic(api_key=api_key)
            self._claude_available = True
            return self._claude_client
        except Exception as exc:
            logger.warning("Failed to initialize Claude client: %s", exc)
            self._claude_available = False
            return None

    def _predict_claude(self, brief: Dict[str, Any], cfg: dict) -> Optional[ModelPrediction]:
        """
        Use Claude to estimate probability from research brief.

        Returns None if Claude is unavailable (triggers heuristic-only fallback).
        """
        client = self._get_claude_client()
        if client is None:
            return None

        market_id = brief.get("market_id", "unknown")
        market_title = brief.get("market_title", "")
        market_prob = brief.get("current_yes_price", 0.5)
        sentiment = brief.get("consensus_sentiment", "neutral")
        confidence = brief.get("consensus_confidence", 0.5)
        gap = brief.get("gap", 0.0)
        narrative = brief.get("narrative_summary", "No research available.")

        # Collect source details for richer context
        source_details = []
        for src in brief.get("sources", []):
            source_details.append(
                f"- {src.get('source', '?')}: bullish={src.get('bullish', 0):.0%}, "
                f"bearish={src.get('bearish', 0):.0%}, neutral={src.get('neutral', 0):.0%}, "
                f"confidence={src.get('confidence', 0):.0%}"
            )
        sources_text = "\n".join(source_details) if source_details else "No source data."

        system_prompt = (
            "You are a prediction market analyst. Your job is to estimate the true "
            "probability of an event occurring based on research data. You must respond "
            "with ONLY a JSON object, no other text. The JSON must have exactly these fields:\n"
            '{"probability": <float 0.01-0.99>, "confidence": <float 0.1-0.9>, "reasoning": "<1-2 sentences>"}\n'
            "Be calibrated: a 70% prediction should resolve YES about 70% of the time. "
            "Consider base rates, sentiment signals, and market context. "
            "Do not simply echo the market price — form an independent estimate."
        )

        user_prompt = (
            f"Market: {market_title}\n"
            f"Market ID: {market_id}\n"
            f"Current market price (YES): {market_prob:.2f}\n\n"
            f"Research Summary:\n"
            f"Consensus sentiment: {sentiment}\n"
            f"Sentiment confidence: {confidence:.2f}\n"
            f"Sentiment-implied probability vs market gap: {gap:+.4f}\n\n"
            f"Source breakdown:\n{sources_text}\n\n"
            f"Narrative: {narrative}\n\n"
            f"Based on this research, what is the true probability this event resolves YES? "
            f"Respond with ONLY a JSON object."
        )

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=256,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )

            # Extract text from response
            text = response.content[0].text.strip()
            parsed = self._parse_claude_response(text)

            prob = max(0.01, min(0.99, parsed["probability"]))
            conf = max(0.1, min(0.9, parsed["confidence"]))
            reasoning = parsed.get("reasoning", "No reasoning provided.")

            logger.info(
                "%s: Claude predicted %.3f (confidence=%.2f)",
                market_id, prob, conf,
            )

            return ModelPrediction(
                model_name="claude",
                role=cfg.get("role", "news_analyst"),
                weight=cfg.get("weight", 0.50),
                predicted_probability=round(prob, 4),
                confidence=round(conf, 3),
                reasoning=reasoning,
            )

        except Exception as exc:
            logger.warning("%s: Claude API error: %s — falling back to heuristic-only", market_id, exc)
            return None

    @staticmethod
    def _parse_claude_response(text: str) -> Dict[str, Any]:
        """
        Parse Claude's JSON response. Handles markdown code blocks.

        Falls back to regex extraction if json.loads fails.
        """
        # Try direct JSON parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        code_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if code_match:
            try:
                return json.loads(code_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try extracting first JSON object
        json_match = re.search(r"\{[^{}]*\}", text)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

        # Last resort: regex for probability and confidence values
        prob_match = re.search(r'"probability"\s*:\s*([\d.]+)', text)
        conf_match = re.search(r'"confidence"\s*:\s*([\d.]+)', text)
        reasoning_match = re.search(r'"reasoning"\s*:\s*"([^"]*)"', text)

        if prob_match:
            return {
                "probability": float(prob_match.group(1)),
                "confidence": float(conf_match.group(1)) if conf_match else 0.5,
                "reasoning": reasoning_match.group(1) if reasoning_match else "Parsed from partial response.",
            }

        logger.warning("Could not parse Claude response: %s", text[:200])
        return {"probability": 0.5, "confidence": 0.3, "reasoning": "Failed to parse Claude response."}

    # ------------------------------------------------------------------
    # Heuristic models
    # ------------------------------------------------------------------

    @staticmethod
    def _predict_heuristic_bull(brief: Dict[str, Any], cfg: dict) -> ModelPrediction:
        """
        Bull advocate heuristic — biases probability upward.

        Anchors on market price, then adjusts upward based on:
        - Bullish sentiment gap
        - High consensus confidence
        - Positive momentum signals
        """
        market_prob = float(brief.get("current_yes_price", 0.5))
        sentiment = brief.get("consensus_sentiment", "neutral")
        confidence = float(brief.get("consensus_confidence", 0.5))
        gap = float(brief.get("gap", 0.0))

        # Start from market price
        prob = market_prob

        # Bull bias: shift up by 5-15% depending on signals
        bull_shift = 0.05  # Base bull bias

        if sentiment == "bullish":
            bull_shift += 0.05 + (confidence * 0.05)  # Up to +15%
        elif sentiment == "neutral":
            bull_shift += 0.02  # Mild optimism for neutral
        # Bearish: keep minimal bull_shift (contrarian anchor)

        if gap > 0:
            # Sentiment says underpriced — bull agrees more strongly
            bull_shift += min(gap * 0.5, 0.10)

        prob = prob + bull_shift
        prob = max(0.05, min(0.95, prob))

        # Confidence: higher when sentiment confirms bull thesis
        bull_confidence = 0.4
        if sentiment == "bullish":
            bull_confidence = 0.5 + (confidence * 0.2)
        elif sentiment == "neutral":
            bull_confidence = 0.4

        reasoning = (
            f"Bull case: market at {market_prob:.2f}, sentiment is {sentiment} "
            f"({confidence:.0%} conf). Adjusting up by {bull_shift:.0%} for bull thesis."
        )

        return ModelPrediction(
            model_name="heuristic_bull",
            role=cfg.get("role", "bull_advocate"),
            weight=cfg.get("weight", 0.25),
            predicted_probability=round(prob, 4),
            confidence=round(bull_confidence, 3),
            reasoning=reasoning,
        )

    @staticmethod
    def _predict_heuristic_bear(brief: Dict[str, Any], cfg: dict) -> ModelPrediction:
        """
        Bear advocate heuristic — biases probability downward.

        Anchors on market price, then adjusts downward based on:
        - Bearish sentiment gap
        - High consensus confidence
        - Negative momentum signals
        """
        market_prob = float(brief.get("current_yes_price", 0.5))
        sentiment = brief.get("consensus_sentiment", "neutral")
        confidence = float(brief.get("consensus_confidence", 0.5))
        gap = float(brief.get("gap", 0.0))

        # Start from market price
        prob = market_prob

        # Bear bias: shift down by 5-15% depending on signals
        bear_shift = 0.05  # Base bear bias

        if sentiment == "bearish":
            bear_shift += 0.05 + (confidence * 0.05)  # Up to -15%
        elif sentiment == "neutral":
            bear_shift += 0.02  # Mild pessimism for neutral
        # Bullish: keep minimal bear_shift (contrarian anchor)

        if gap < 0:
            # Sentiment says overpriced — bear agrees more strongly
            bear_shift += min(abs(gap) * 0.5, 0.10)

        prob = prob - bear_shift
        prob = max(0.05, min(0.95, prob))

        # Confidence: higher when sentiment confirms bear thesis
        bear_confidence = 0.4
        if sentiment == "bearish":
            bear_confidence = 0.5 + (confidence * 0.2)
        elif sentiment == "neutral":
            bear_confidence = 0.4

        reasoning = (
            f"Bear case: market at {market_prob:.2f}, sentiment is {sentiment} "
            f"({confidence:.0%} conf). Adjusting down by {bear_shift:.0%} for bear thesis."
        )

        return ModelPrediction(
            model_name="heuristic_bear",
            role=cfg.get("role", "bear_advocate"),
            weight=cfg.get("weight", 0.25),
            predicted_probability=round(prob, 4),
            confidence=round(bear_confidence, 3),
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Ensemble aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _ensemble(predictions: List[ModelPrediction]) -> tuple:
        """
        Compute weighted ensemble probability and confidence.

        Re-normalizes weights if some models are missing (e.g., Claude unavailable).
        Returns (ensemble_probability, ensemble_confidence).
        """
        if not predictions:
            return 0.5, 0.1

        total_weight = sum(p.weight for p in predictions)
        if total_weight == 0:
            total_weight = 1.0

        ensemble_prob = sum(
            p.predicted_probability * (p.weight / total_weight)
            for p in predictions
        )
        ensemble_conf = sum(
            p.confidence * (p.weight / total_weight)
            for p in predictions
        )

        # Clamp
        ensemble_prob = max(0.01, min(0.99, ensemble_prob))
        ensemble_conf = max(0.1, min(0.95, ensemble_conf))

        return round(ensemble_prob, 4), round(ensemble_conf, 4)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def compute_edge(ensemble_prob: float, market_prob: float) -> Dict[str, Any]:
        """Compute edge metrics. Utility for external callers."""
        edge = ensemble_prob - market_prob
        direction = "buy_yes" if edge > 0 else "buy_no"
        signal_strength = abs(edge) * 0.7  # Default confidence
        mispricing = abs(edge) / 0.10
        return {
            "edge": round(edge, 4),
            "direction": direction,
            "signal_strength": round(signal_strength, 4),
            "mispricing_score": round(mispricing, 2),
        }


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def load_latest_research_snapshot() -> Optional[dict]:
    """Load the most recent research snapshot from RESEARCH_DIR."""
    snapshots = sorted(RESEARCH_DIR.glob("research_*.json"), reverse=True)
    if not snapshots:
        logger.warning("No research snapshots found in %s", RESEARCH_DIR)
        return None
    fp = snapshots[0]
    logger.info("Loading research snapshot: %s", fp)
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_prediction_snapshot(signals: List[TradeSignal], output_dir: Optional[Path] = None) -> Path:
    """Save all trade signals as a single timestamped JSON snapshot."""
    out = output_dir or PREDICTIONS_DIR
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = out / f"predictions_{ts}.json"

    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "markets_predicted": len(signals),
        "signals": [asdict(s) for s in signals],
    }
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    logger.info("Prediction snapshot saved: %s (%d signals)", fp, len(signals))
    return fp


def _print_prediction_table(signals: List[TradeSignal]) -> None:
    """Print a formatted table of prediction results."""
    header = (
        f"{'#':>3}  {'Market Title':<40} {'EnsPr':>5} {'MktPr':>5} "
        f"{'Edge':>6} {'Dir':<8} {'Str':>5} {'Conf':>5} {'Trade':>5}"
    )
    print(header)
    print("-" * len(header))

    for i, s in enumerate(signals, 1):
        title_trunc = s.market_title[:40].ljust(40)
        trade_flag = "YES" if s.should_trade else "no"
        print(
            f"{i:3d}  {title_trunc} "
            f"{s.ensemble_probability:5.2f} {s.market_probability:5.2f} "
            f"{s.edge:+6.3f} {s.direction:<8} "
            f"{s.signal_strength:5.3f} {s.confidence:5.2f} "
            f"{trade_flag:>5}"
        )

    # Summary
    trade_count = sum(1 for s in signals if s.should_trade)
    print(f"\n{trade_count}/{len(signals)} markets have tradeable signals")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    # Handle Windows console encoding
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Run prediction engine on latest research briefs."
    )
    parser.add_argument(
        "--heuristic-only", action="store_true",
        help="Skip Claude API, use heuristic models only.",
    )
    parser.add_argument(
        "--top", type=int, default=None,
        help="Predict for top N markets only (default: all in snapshot).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Load latest research snapshot
    snapshot = load_latest_research_snapshot()
    if snapshot is None:
        print("No research snapshots found. Run researcher.py first.")
        sys.exit(1)

    briefs = snapshot.get("briefs", [])
    if not briefs:
        print("Research snapshot contains no briefs.")
        sys.exit(1)

    if args.top:
        briefs = briefs[:args.top]

    # Force heuristic-only if flag is set
    settings = None
    if args.heuristic_only:
        settings = load_settings()
        # Remove claude from sources by clearing API key environment
        # The engine will detect missing key and fall back
        os.environ.pop("ANTHROPIC_API_KEY", None)

    print(f"\nPrediction Engine — Ensemble Analysis")
    print(f"Research snapshot: {snapshot.get('timestamp', '?')}")
    print(f"Markets to predict: {len(briefs)}")
    print(f"Mode: {'heuristic-only' if args.heuristic_only else 'full ensemble (Claude + heuristics)'}\n")

    engine = PredictionEngine(settings=settings)
    signals: List[TradeSignal] = []

    for brief in briefs:
        try:
            signal = engine.predict(brief)
            signals.append(signal)
        except Exception as exc:
            market_id = brief.get("market_id", "unknown")
            logger.error("Failed to predict %s: %s", market_id, exc)

    if signals:
        print()
        _print_prediction_table(signals)

        # Save snapshot
        fp = save_prediction_snapshot(signals)
        print(f"\nPrediction snapshot saved: {fp}")

        # Print per-model breakdown for first signal
        if signals:
            print(f"\n--- Model breakdown for: {signals[0].market_title[:60]} ---")
            for mp in signals[0].model_predictions:
                print(
                    f"  {mp.model_name:<18} prob={mp.predicted_probability:.3f}  "
                    f"conf={mp.confidence:.2f}  w={mp.weight:.2f}"
                )
                print(f"    {mp.reasoning[:100]}")
    else:
        print("No markets were successfully predicted.")
