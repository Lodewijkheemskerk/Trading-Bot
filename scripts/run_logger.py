"""
Run Logger — Tracks pipeline cycle history with cost estimates.

Saves a JSON log entry after each pipeline cycle with:
- Timestamp, duration
- Per-step results (success/fail, counts)
- Estimated LLM API cost based on token pricing
- Markets scanned, researched, predicted, traded

Stored in data/runs/run_YYYYMMDD_HHMMSS.json
History served via /api/runs endpoint.
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

RUNS_DIR = Path(__file__).resolve().parent.parent / "data" / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

# ── Cost estimates per 1M tokens (USD) ──
# These are approximate and should be updated when prices change.
TOKEN_COSTS = {
    "grok":     {"input": 5.00,  "output": 15.00},   # Grok 3 Fast
    "claude":   {"input": 3.00,  "output": 15.00},   # Claude Sonnet 4
    "gpt4o":    {"input": 0.15,  "output": 0.60},    # GPT-4o Mini
    "gemini":   {"input": 0.15,  "output": 0.60},    # Gemini 2.5 Flash
    "deepseek": {"input": 0.27,  "output": 1.10},    # DeepSeek Chat
}

# Estimated tokens per prediction call (prompt + completion)
# Conservative estimates based on typical ~800 token prompts + ~150 token responses
EST_INPUT_TOKENS = 900
EST_OUTPUT_TOKENS = 180


@dataclass
class RunEntry:
    """One pipeline cycle record."""
    timestamp: str = ""
    duration_seconds: float = 0.0
    cycle_number: int = 0

    # Step results
    scan: Dict[str, Any] = field(default_factory=dict)
    research: Dict[str, Any] = field(default_factory=dict)
    predict: Dict[str, Any] = field(default_factory=dict)
    execute: Dict[str, Any] = field(default_factory=dict)
    monitor: Dict[str, Any] = field(default_factory=dict)
    resolve: Dict[str, Any] = field(default_factory=dict)
    compound: Dict[str, Any] = field(default_factory=dict)

    # Summary
    markets_scanned: int = 0
    markets_passed: int = 0
    markets_researched: int = 0
    predictions_made: int = 0
    trades_executed: int = 0
    trades_blocked: int = 0

    # Cost
    estimated_cost_usd: float = 0.0
    models_called: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_cycle_cost(signals: List[Dict[str, Any]]) -> tuple:
    """
    Estimate the LLM API cost for a prediction cycle.

    Args:
        signals: List of prediction signal dicts, each containing model_predictions

    Returns:
        (total_cost_usd, models_called_list)
    """
    total_cost = 0.0
    models_called = set()

    for signal in signals:
        for mp in signal.get("model_predictions", []):
            model_name = mp.get("model_name", "")
            if model_name in TOKEN_COSTS:
                models_called.add(model_name)
                costs = TOKEN_COSTS[model_name]
                input_cost = (EST_INPUT_TOKENS / 1_000_000) * costs["input"]
                output_cost = (EST_OUTPUT_TOKENS / 1_000_000) * costs["output"]
                total_cost += input_cost + output_cost

    return round(total_cost, 6), sorted(models_called)


def save_run(entry: RunEntry) -> Path:
    """Save a run entry to disk."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RUNS_DIR / f"run_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry.to_dict(), f, indent=2, default=str)
    logger.info("Run log saved: %s", path.name)
    return path


def load_runs(limit: int = 50) -> List[dict]:
    """Load recent run entries, newest first."""
    files = sorted(RUNS_DIR.glob("run_*.json"), reverse=True)[:limit]
    runs = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                runs.append(json.load(fh))
        except Exception as exc:
            logger.warning("Failed to load run %s: %s", f.name, exc)
    return runs


def get_cost_summary(days: int = 1) -> dict:
    """
    Summarize costs over the last N days.

    Returns: {total_cost, run_count, avg_cost_per_run, models_used}
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    runs = load_runs(limit=500)

    total_cost = 0.0
    count = 0
    models = set()

    for r in runs:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                total_cost += r.get("estimated_cost_usd", 0)
                count += 1
                models.update(r.get("models_called", []))
        except (KeyError, ValueError):
            continue

    return {
        "total_cost_usd": round(total_cost, 4),
        "run_count": count,
        "avg_cost_per_run": round(total_cost / max(count, 1), 4),
        "models_used": sorted(models),
        "period_days": days,
    }
