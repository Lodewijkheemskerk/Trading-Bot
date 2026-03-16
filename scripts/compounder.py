"""
Step 5a: COMPOUND LEARNING — Analyze Trade Outcomes

Tracks performance metrics across all trades, evaluates prediction
accuracy, classifies failures, and appends structured lessons to
failure_log.md. The scan and research agents read this log before
processing new markets so the system doesn't repeat mistakes.

Losing trades are analyzed by multiple LLM agents (post-mortem) when
API keys are available. Each agent independently classifies the failure
and extracts a lesson. Consensus drives the final classification.
Falls back to rule-based classification when no API keys are set.

Metrics tracked:
  - Win rate
  - Total P&L
  - Sharpe ratio (annualized)
  - Profit factor (gross wins / gross losses)
  - Brier score (prediction calibration)
  - Max drawdown
  - Trade count

Failure categories (per design document):
  1. Bad Prediction — Model probability was significantly wrong
  2. Bad Timing    — Right direction, wrong entry/exit timing
  3. Bad Execution — Slippage, partial fills, API errors
  4. External Shock — Unpredictable event invalidated thesis
"""

import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import List, Optional, Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, TRADES_DIR, REFERENCES_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Failure categories (per design document)
# ---------------------------------------------------------------------------

FAILURE_BAD_PREDICTION = "Bad Prediction"
FAILURE_BAD_TIMING = "Bad Timing"
FAILURE_BAD_EXECUTION = "Bad Execution"
FAILURE_EXTERNAL_SHOCK = "External Shock"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PerformanceReport:
    """Aggregate performance metrics."""
    total_trades: int
    executed_trades: int
    blocked_trades: int
    open_trades: int
    closed_trades: int
    win_rate: float               # Wins / closed trades
    total_pnl: float              # Sum of realized P&L
    avg_pnl: float                # Average P&L per closed trade
    sharpe_ratio: float           # Annualized Sharpe (daily returns)
    profit_factor: float          # Gross wins / gross losses
    brier_score: float            # Mean squared prediction error
    max_drawdown: float           # Maximum peak-to-trough as fraction
    best_trade_pnl: float
    worst_trade_pnl: float
    avg_edge: float               # Average edge at entry
    avg_confidence: float         # Average model confidence
    timestamp: str


@dataclass
class FailureEntry:
    """A structured failure log entry for the knowledge base."""
    date: str
    market_id: str
    market_title: str
    category: str                 # One of the FAILURE_* constants
    entry_price: float
    exit_price: float
    model_probability: float
    actual_outcome: str           # "yes" or "no"
    pnl: float
    root_cause: str
    lesson: str
    action_taken: str


# ---------------------------------------------------------------------------
# Compounder
# ---------------------------------------------------------------------------

class Compounder:
    """Analyzes trade outcomes, classifies failures, and tracks performance."""

    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or load_settings()
        self.compound_cfg = self.settings.get("compound", {})
        self.min_trades_for_stats = self.compound_cfg.get("min_trades_for_stats", 10)

        # LLM post-mortem config
        pm_cfg = self.compound_cfg.get("post_mortem", {})
        self.post_mortem_enabled = pm_cfg.get("enabled", False)
        self.post_mortem_models = pm_cfg.get("models", [])

    # ------------------------------------------------------------------
    # Trade loading
    # ------------------------------------------------------------------

    def load_all_trades(self) -> List[Dict[str, Any]]:
        """Load all trade records from execution snapshots."""
        trades = []
        for fp in sorted(TRADES_DIR.glob("execution_*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for trade in data.get("trades", []):
                    trades.append(trade)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to load %s: %s", fp, exc)
        return trades

    def load_individual_trades(self) -> List[Dict[str, Any]]:
        """
        Load trades from individual trade_*.json files.

        These files are kept up-to-date by the resolver (status, pnl, outcome
        fields are updated when markets settle). Prefer this over execution
        snapshots when you need current trade state.
        """
        trades = []
        for fp in sorted(TRADES_DIR.glob("trade_*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    trade = json.load(f)
                trades.append(trade)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to load %s: %s", fp, exc)
        return trades

    # ------------------------------------------------------------------
    # Trade analysis & failure classification
    # ------------------------------------------------------------------

    def analyze_trade(self, trade: Dict[str, Any], outcome: Optional[bool] = None) -> Dict[str, Any]:
        """
        Analyze a single trade outcome. Computes P&L if not already set
        and classifies the result.

        Args:
            trade: Trade dict — if status is "closed" and has "outcome" field,
                   P&L and outcome are read from the trade itself.
                   Otherwise, outcome param is used.
            outcome: True if event resolved YES, False if NO.
                     Ignored if trade already has outcome/pnl from resolver.

        Returns:
            Analysis dict with P&L, accuracy, failure category, and lesson.
        """
        direction = trade.get("direction", "buy_yes")
        entry_price = float(trade.get("entry_price", 0.5))
        position_size = float(trade.get("position_size_usd", 0))
        model_prob = float(trade.get("model_probability", 0.5))
        edge = float(trade.get("edge", 0))

        # Determine outcome: prefer trade's own resolved data
        if trade.get("status") == "closed" and "outcome" in trade:
            outcome_yes = trade["outcome"] == "yes"
            pnl = float(trade.get("pnl", 0))
        elif outcome is not None:
            outcome_yes = outcome
            # Compute P&L
            from scripts.resolver import compute_pnl
            pnl = compute_pnl(direction, entry_price, position_size, outcome_yes)
        else:
            # Can't analyze without outcome
            return {
                "trade_id": trade.get("trade_id", "unknown"),
                "market_id": trade.get("market_id", "unknown"),
                "status": "no_outcome",
            }

        # Prediction accuracy
        actual = 1.0 if outcome_yes else 0.0
        brier_component = (model_prob - actual) ** 2

        # Was the prediction directionally correct?
        if direction == "buy_yes":
            predicted_yes = True
        else:
            predicted_yes = False
        correct = (predicted_yes and outcome_yes) or (not predicted_yes and not outcome_yes)

        exit_price = float(trade.get("exit_price", 1.0 if outcome_yes else 0.0))

        analysis = {
            "trade_id": trade.get("trade_id", "unknown"),
            "market_id": trade.get("market_id", "unknown"),
            "market_title": trade.get("market_title", ""),
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "position_size_usd": position_size,
            "model_probability": model_prob,
            "edge": edge,
            "pnl": round(pnl, 2),
            "correct_prediction": correct,
            "brier_component": round(brier_component, 4),
            "actual_outcome": "yes" if outcome_yes else "no",
        }

        # Classify if it's a loss
        if not correct and position_size > 0:
            # Step 1: Rule-based classification (always runs, fast, free)
            failure = self._classify_failure(trade, model_prob, outcome_yes, edge, pnl)

            # Step 2: LLM post-mortem (runs if enabled and API keys available)
            failure = self._run_post_mortem(trade, failure)

            analysis["failure_category"] = failure.category
            analysis["root_cause"] = failure.root_cause
            analysis["lesson"] = failure.lesson
            analysis["action_taken"] = failure.action_taken

            logger.info(
                "Trade %s: LOSS [%s] — P&L $%.2f — %s",
                trade.get("trade_id"), failure.category, pnl, failure.lesson,
            )
        elif position_size > 0:
            analysis["failure_category"] = None
            analysis["lesson"] = f"Correct call: {direction} at {entry_price:.2f}, outcome={analysis['actual_outcome']}, P&L=${pnl:.2f}"
            logger.info("Trade %s: WIN — P&L $%.2f", trade.get("trade_id"), pnl)

        return analysis

    def _classify_failure(
        self,
        trade: Dict[str, Any],
        model_prob: float,
        outcome_yes: bool,
        edge: float,
        pnl: float,
    ) -> FailureEntry:
        """
        Classify a losing trade into one of the four failure categories.

        Classification logic:
        - Bad Prediction: Model probability was significantly wrong
          (model said >60% for YES but outcome was NO, or vice versa)
        - Bad Timing: The direction was eventually correct but the market
          moved against us before settlement (edge was small, close to threshold)
        - Bad Execution: Slippage or execution issues (entry price diverged
          significantly from what the model expected)
        - External Shock: Low-probability event that invalidated all models
          (market was pricing >70% one way and the opposite happened)
        """
        market_id = trade.get("market_id", "unknown")
        market_title = trade.get("market_title", "")
        direction = trade.get("direction", "buy_yes")
        entry_price = float(trade.get("entry_price", 0.5))
        exit_price = float(trade.get("exit_price", 1.0 if outcome_yes else 0.0))
        actual = "yes" if outcome_yes else "no"

        # Determine which category fits
        category = FAILURE_BAD_PREDICTION  # default
        root_cause = ""
        lesson = ""
        action_taken = "Logged for future reference."

        # How far off was the model?
        model_error = abs(model_prob - (1.0 if outcome_yes else 0.0))

        # Was the market also very wrong? (External shock indicator)
        market_prob_at_entry = entry_price  # YES price is the market's implied prob
        market_error = abs(market_prob_at_entry - (1.0 if outcome_yes else 0.0))

        # External Shock: both model AND market were very wrong
        # Market was pricing >65% in one direction and the opposite happened
        if market_error > 0.65:
            category = FAILURE_EXTERNAL_SHOCK
            root_cause = (
                f"Market was pricing {'YES' if market_prob_at_entry > 0.5 else 'NO'} "
                f"at {market_prob_at_entry:.0%} but {actual.upper()} won. "
                f"Unexpected event likely invalidated all prior signals."
            )
            lesson = (
                f"Market {market_id} experienced an external shock. "
                f"Neither model ({model_prob:.0%}) nor market ({market_prob_at_entry:.0%}) "
                f"predicted {actual.upper()}."
            )
            action_taken = "Flag similar event types for higher uncertainty in future."

        # Bad Timing: edge was marginal (< 8%) — direction may have been right
        # but timing was off. Small edge trades are inherently higher variance.
        elif abs(edge) < 0.08 and model_error < 0.40:
            category = FAILURE_BAD_TIMING
            root_cause = (
                f"Edge was marginal at {edge:+.1%}. "
                f"Model probability {model_prob:.0%} wasn't far enough from market "
                f"to absorb variance."
            )
            lesson = (
                f"Marginal edge trade on {market_id}. "
                f"Consider raising min_edge threshold or requiring higher confidence "
                f"for markets in this category."
            )
            action_taken = "Review if min_edge threshold should increase."

        # Bad Prediction: model was confident but wrong
        elif model_error > 0.40:
            category = FAILURE_BAD_PREDICTION
            root_cause = (
                f"Model predicted {model_prob:.0%} probability for "
                f"{'YES' if direction == 'buy_yes' else 'NO'} but actual was {actual.upper()}. "
                f"Error: {model_error:.0%}."
            )
            lesson = (
                f"Model significantly miscalibrated on {market_id} "
                f"({trade.get('market_title', '')[:60]}). "
                f"Predicted {model_prob:.0%}, actual was {actual}."
            )
            action_taken = "Review model calibration for this market category."

        # Catch-all: moderate error, likely a prediction issue
        else:
            category = FAILURE_BAD_PREDICTION
            root_cause = (
                f"Model predicted {model_prob:.0%}, market was at {market_prob_at_entry:.0%}, "
                f"actual was {actual.upper()}. Moderate prediction error."
            )
            lesson = (
                f"Prediction error on {market_id}: model={model_prob:.0%}, "
                f"market={market_prob_at_entry:.0%}, outcome={actual}."
            )
            action_taken = "Logged for calibration tracking."

        return FailureEntry(
            date=datetime.now().strftime("%Y-%m-%d"),
            market_id=market_id,
            market_title=market_title,
            category=category,
            entry_price=entry_price,
            exit_price=exit_price,
            model_probability=model_prob,
            actual_outcome=actual,
            pnl=round(pnl, 2),
            root_cause=root_cause,
            lesson=lesson,
            action_taken=action_taken,
        )

    # ------------------------------------------------------------------
    # LLM post-mortem (multiple agents analyze each loss)
    # ------------------------------------------------------------------

    def _run_post_mortem(self, trade: Dict[str, Any], rule_based: FailureEntry) -> FailureEntry:
        """
        Send a losing trade to 2 cheap LLM agents for independent analysis.

        Each agent receives the trade data and must classify it into one of
        the 4 failure categories with a root cause, lesson, and action.

        Consensus logic:
        - If both LLMs agree on category → use that category
        - If LLMs disagree → keep rule-based category
        - Lessons from all sources (rule-based + LLMs) are merged
        - If no LLM is available → return rule-based unchanged

        Cost: ~$0.007 per post-mortem (GPT-4o-mini + DeepSeek)
        """
        if not self.post_mortem_enabled or not self.post_mortem_models:
            return rule_based

        prompt = self._build_post_mortem_prompt(trade, rule_based)
        llm_results: List[Dict[str, str]] = []

        for model_cfg in self.post_mortem_models:
            result = self._call_post_mortem_model(model_cfg, prompt)
            if result:
                llm_results.append(result)

        if not llm_results:
            logger.info("Post-mortem: no LLM responses, using rule-based classification")
            return rule_based

        # Consensus: check if LLMs agree on category
        llm_categories = [r.get("category", "") for r in llm_results]
        valid_categories = {FAILURE_BAD_PREDICTION, FAILURE_BAD_TIMING,
                           FAILURE_BAD_EXECUTION, FAILURE_EXTERNAL_SHOCK}

        # Normalize LLM categories (they might return slightly different text)
        normalized = []
        for cat in llm_categories:
            for valid in valid_categories:
                if valid.lower() in cat.lower():
                    normalized.append(valid)
                    break
            else:
                normalized.append("")

        # Determine final category
        non_empty = [c for c in normalized if c]
        if len(non_empty) >= 2 and len(set(non_empty)) == 1:
            # Both LLMs agree
            final_category = non_empty[0]
            logger.info(
                "Post-mortem consensus: both agents agree on [%s]",
                final_category,
            )
        elif len(non_empty) == 1:
            # Only one LLM responded with valid category — use it if rule-based agrees
            if non_empty[0] == rule_based.category:
                final_category = rule_based.category
            else:
                final_category = rule_based.category  # Tie goes to rule-based
                logger.info(
                    "Post-mortem: single LLM says [%s], rule-based says [%s] — keeping rule-based",
                    non_empty[0], rule_based.category,
                )
        else:
            final_category = rule_based.category
            logger.info("Post-mortem: LLMs disagree, keeping rule-based [%s]", rule_based.category)

        # Merge lessons from all sources
        all_lessons = [rule_based.lesson]
        all_root_causes = [rule_based.root_cause]
        all_actions = [rule_based.action_taken]

        for r in llm_results:
            model_name = r.get("model_name", "LLM")
            if r.get("lesson"):
                all_lessons.append(f"[{model_name}] {r['lesson']}")
            if r.get("root_cause"):
                all_root_causes.append(f"[{model_name}] {r['root_cause']}")
            if r.get("action_taken"):
                all_actions.append(f"[{model_name}] {r['action_taken']}")

        return FailureEntry(
            date=rule_based.date,
            market_id=rule_based.market_id,
            market_title=rule_based.market_title,
            category=final_category,
            entry_price=rule_based.entry_price,
            exit_price=rule_based.exit_price,
            model_probability=rule_based.model_probability,
            actual_outcome=rule_based.actual_outcome,
            pnl=rule_based.pnl,
            root_cause=" | ".join(all_root_causes),
            lesson=" | ".join(all_lessons),
            action_taken=" | ".join(all_actions),
        )

    @staticmethod
    def _build_post_mortem_prompt(trade: Dict[str, Any], rule_based: FailureEntry) -> str:
        """Build the prompt sent to each post-mortem LLM agent."""
        return (
            "You are a trading post-mortem analyst. A prediction market trade lost money. "
            "Analyze the trade and classify the failure.\n\n"
            "FAILURE CATEGORIES (pick exactly one):\n"
            "1. Bad Prediction — Model probability was significantly wrong\n"
            "2. Bad Timing — Right direction, wrong entry/exit timing\n"
            "3. Bad Execution — Slippage, partial fills, API errors\n"
            "4. External Shock — Unpredictable event invalidated thesis\n\n"
            f"TRADE DATA:\n"
            f"- Market: {trade.get('market_title', trade.get('market_id', '?'))}\n"
            f"- Direction: {trade.get('direction', '?')}\n"
            f"- Entry price (YES): {trade.get('entry_price', '?')}\n"
            f"- Model probability: {trade.get('model_probability', '?')}\n"
            f"- Edge at entry: {trade.get('edge', '?')}\n"
            f"- Outcome: {rule_based.actual_outcome}\n"
            f"- P&L: ${rule_based.pnl:.2f}\n"
            f"- Signal strength: {trade.get('signal_strength', '?')}\n\n"
            "Respond with ONLY a JSON object:\n"
            '{"category": "<one of the 4 categories>", '
            '"root_cause": "<1-2 sentences on what went wrong>", '
            '"lesson": "<1 sentence actionable lesson for future trades>", '
            '"action_taken": "<1 sentence on what to change>"}\n'
        )

    def _call_post_mortem_model(
        self, model_cfg: Dict[str, Any], prompt: str
    ) -> Optional[Dict[str, str]]:
        """
        Call a single post-mortem LLM model.

        Returns parsed dict with category/root_cause/lesson/action_taken,
        or None if the call fails.
        """
        model_name = model_cfg.get("name", "unknown")
        model_id = model_cfg.get("model_id", "")
        env_key = model_cfg.get("env_key", "")
        provider = model_cfg.get("provider", "openai")
        base_url = model_cfg.get("base_url")

        api_key = os.environ.get(env_key, "")
        if not api_key:
            logger.debug("Post-mortem: %s skipped (no %s)", model_name, env_key)
            return None

        try:
            from openai import OpenAI

            client_kwargs = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url

            client = OpenAI(**client_kwargs)

            response = client.chat.completions.create(
                model=model_id,
                max_tokens=200,
                temperature=0.3,
                messages=[
                    {"role": "user", "content": prompt},
                ],
            )

            text = response.choices[0].message.content.strip()
            parsed = self._parse_post_mortem_response(text)
            parsed["model_name"] = model_name

            logger.info(
                "Post-mortem [%s]: category=%s",
                model_name, parsed.get("category", "?"),
            )
            return parsed

        except Exception as exc:
            logger.warning("Post-mortem [%s] failed: %s", model_name, exc)
            return None

    @staticmethod
    def _parse_post_mortem_response(text: str) -> Dict[str, str]:
        """Parse JSON response from a post-mortem LLM. Handles markdown fences."""
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from code block
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

        logger.warning("Could not parse post-mortem response: %s", text[:200])
        return {}

    # ------------------------------------------------------------------
    # Failure log (knowledge base)
    # ------------------------------------------------------------------

    def append_failures_to_log(self, analyses: List[Dict[str, Any]]) -> int:
        """
        Append failure entries to failure_log.md in the format defined
        by the design document. Returns the number of failures appended.

        Only appends trades that have a failure_category set.
        """
        failures = [a for a in analyses if a.get("failure_category")]
        if not failures:
            return 0

        REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
        log_fp = REFERENCES_DIR / "failure_log.md"

        lines = []
        for f in failures:
            lines.append(f"\n### [{f['date'] if 'date' in f else date.today().isoformat()}] "
                         f"- [{f.get('market_id', '?')}] "
                         f"- [{f['failure_category']}]")
            lines.append(f"- **Market:** {f.get('market_title', f.get('market_id', '?'))}")
            lines.append(f"- Entry Price: {f.get('entry_price', '?')}")
            lines.append(f"- Exit Price: {f.get('exit_price', '?')}")
            lines.append(f"- Model Probability: {f.get('model_probability', '?')}")
            lines.append(f"- Actual Outcome: {f.get('actual_outcome', '?')}")
            lines.append(f"- P&L: ${f.get('pnl', 0):.2f}")
            lines.append(f"- Root Cause: {f.get('root_cause', 'Unknown')}")
            lines.append(f"- Lesson: {f.get('lesson', 'None')}")
            lines.append(f"- Action Taken: {f.get('action_taken', 'None')}")
            lines.append("")

        with open(log_fp, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        logger.info("Appended %d failure entries to %s", len(failures), log_fp)
        return len(failures)

    @staticmethod
    def load_failure_log() -> List[Dict[str, str]]:
        """
        Parse failure_log.md and return structured failure entries.

        Used by scanner and researcher to check past mistakes before
        processing new markets.

        Skips markdown code blocks (``` ... ```) to avoid parsing
        the template example in the file header.

        Returns list of dicts with keys:
            date, market_id, category, lesson, root_cause, action_taken
        """
        log_fp = REFERENCES_DIR / "failure_log.md"
        if not log_fp.exists():
            return []

        entries = []
        current_entry: Optional[Dict[str, str]] = None
        in_code_block = False

        try:
            with open(log_fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip()

                    # Skip markdown code blocks (template examples)
                    if line.startswith("```"):
                        in_code_block = not in_code_block
                        continue
                    if in_code_block:
                        continue

                    # New entry header: ### [DATE] - [MARKET] - [CATEGORY]
                    # Must have a real market_id (not placeholder text like "MARKET")
                    if line.startswith("### ["):
                        if current_entry and current_entry.get("market_id"):
                            entries.append(current_entry)

                        # Parse header
                        parts = line.replace("### ", "").split(" - ")
                        market_id = parts[1].strip("[] ") if len(parts) > 1 else ""

                        # Skip placeholder/template entries
                        if market_id and market_id not in ("MARKET", "market_id"):
                            current_entry = {
                                "date": parts[0].strip("[] ") if len(parts) > 0 else "",
                                "market_id": market_id,
                                "category": parts[2].strip("[] ") if len(parts) > 2 else "",
                            }
                        else:
                            current_entry = None

                    elif current_entry is not None:
                        # Parse key-value fields
                        if line.startswith("- Lesson:"):
                            current_entry["lesson"] = line.replace("- Lesson:", "").strip()
                        elif line.startswith("- Root Cause:"):
                            current_entry["root_cause"] = line.replace("- Root Cause:", "").strip()
                        elif line.startswith("- Action Taken:"):
                            current_entry["action_taken"] = line.replace("- Action Taken:", "").strip()
                        elif line.startswith("- **Market:**"):
                            current_entry["market_title"] = line.replace("- **Market:**", "").strip()
                        elif line.startswith("- P&L:"):
                            current_entry["pnl"] = line.replace("- P&L:", "").strip()

                # Don't forget last entry
                if current_entry and current_entry.get("market_id"):
                    entries.append(current_entry)

        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Failed to read failure log: %s", exc)

        return entries

    # ------------------------------------------------------------------
    # Performance report
    # ------------------------------------------------------------------

    def get_performance_report(self, trades: Optional[List[Dict]] = None) -> PerformanceReport:
        """
        Generate a performance report from trade history.

        Prefers individual trade files (kept current by resolver) over
        execution snapshots. Falls back to execution snapshots if no
        individual trade files exist.
        """
        if trades is None:
            # Prefer individual trade files (resolver keeps these current)
            trades = self.load_individual_trades()
            if not trades:
                # Fallback to execution snapshots
                trades = self.load_all_trades()

        total = len(trades)
        executed = [t for t in trades if t.get("risk_passed", False)]
        blocked = [t for t in trades if not t.get("risk_passed", False)]

        # Separate open vs closed
        open_trades = [t for t in executed if t.get("status") == "open"]
        closed = [t for t in executed if t.get("status") == "closed"]

        # Metrics from closed trades
        pnls = [float(t.get("pnl", 0)) for t in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        win_rate = len(wins) / len(closed) if closed else 0.0
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / len(closed) if closed else 0.0

        # Sharpe ratio (simplified — daily returns as proxy)
        if len(pnls) >= 2:
            mean_return = sum(pnls) / len(pnls)
            variance = sum((p - mean_return) ** 2 for p in pnls) / len(pnls)
            std_return = math.sqrt(variance) if variance > 0 else 1.0
            sharpe = (mean_return / std_return) * math.sqrt(252)  # Annualized
        else:
            sharpe = 0.0

        # Profit factor
        gross_wins = sum(wins) if wins else 0.0
        gross_losses = abs(sum(losses)) if losses else 0.0
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf") if gross_wins > 0 else 0.0

        # Brier score — use stored outcome for closed trades
        brier_components = []
        for t in closed:
            model_p = float(t.get("model_probability", 0.5))

            # Prefer explicit outcome from resolver
            if "outcome" in t:
                actual = 1.0 if t["outcome"] == "yes" else 0.0
            else:
                # Infer from P&L sign (legacy fallback)
                pnl_val = float(t.get("pnl", 0))
                direction = t.get("direction", "buy_yes")
                if direction == "buy_yes":
                    actual = 1.0 if pnl_val > 0 else 0.0
                else:
                    actual = 0.0 if pnl_val > 0 else 1.0

            brier_components.append((model_p - actual) ** 2)
        brier_score = sum(brier_components) / len(brier_components) if brier_components else 0.0

        # Max drawdown from P&L series
        max_dd = 0.0
        peak = 0.0
        cumulative = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / max(peak, 1.0) if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        # Averages
        edges = [float(t.get("edge", 0)) for t in executed]
        confidences = [float(t.get("confidence", t.get("signal_strength", 0))) for t in executed]
        avg_edge = sum(edges) / len(edges) if edges else 0.0
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

        return PerformanceReport(
            total_trades=total,
            executed_trades=len(executed),
            blocked_trades=len(blocked),
            open_trades=len(open_trades),
            closed_trades=len(closed),
            win_rate=round(win_rate, 4),
            total_pnl=round(total_pnl, 2),
            avg_pnl=round(avg_pnl, 2),
            sharpe_ratio=round(sharpe, 2),
            profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
            brier_score=round(brier_score, 4),
            max_drawdown=round(max_dd, 4),
            best_trade_pnl=round(max(pnls), 2) if pnls else 0.0,
            worst_trade_pnl=round(min(pnls), 2) if pnls else 0.0,
            avg_edge=round(avg_edge, 4),
            avg_confidence=round(avg_conf, 4),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------
    # Nightly review
    # ------------------------------------------------------------------

    def nightly_review(self, trades: Optional[List[Dict]] = None) -> str:
        """
        Generate a nightly review: resolve outcomes, classify failures,
        append to failure_log.md, and return summary text.

        This is the full compound learning cycle.
        """
        if trades is None:
            trades = self.load_individual_trades()
            if not trades:
                trades = self.load_all_trades()

        today = date.today().isoformat()
        today_trades = [
            t for t in trades
            if t.get("timestamp", "").startswith(today)
        ]

        # Analyze all closed trades that haven't been analyzed yet
        # (we check for the presence of 'analyzed' flag)
        newly_closed = [
            t for t in trades
            if t.get("status") == "closed" and not t.get("analyzed")
        ]

        analyses = []
        for trade in newly_closed:
            analysis = self.analyze_trade(trade)
            if analysis.get("status") != "no_outcome":
                analyses.append(analysis)
                # Mark as analyzed in the trade file
                self._mark_analyzed(trade.get("trade_id"))

        # Append failures to knowledge base
        failures_logged = self.append_failures_to_log(analyses)

        # Generate report
        report = self.get_performance_report(trades)

        review_lines = [
            f"## Nightly Review — {today}",
            f"",
            f"**Today's activity:** {len(today_trades)} signals processed",
            f"**Newly resolved:** {len(analyses)} trades analyzed",
            f"**Failures logged:** {failures_logged}",
            f"**All-time:** {report.total_trades} total, {report.executed_trades} executed, {report.blocked_trades} blocked",
            f"**Open positions:** {report.open_trades}",
            f"**Closed trades:** {report.closed_trades}",
            f"**Win rate:** {report.win_rate:.1%}",
            f"**Total P&L:** ${report.total_pnl:.2f}",
            f"**Sharpe ratio:** {report.sharpe_ratio:.2f}",
            f"**Brier score:** {report.brier_score:.4f}",
            f"",
        ]

        # Detail on newly analyzed trades
        if analyses:
            review_lines.append("### Trade Outcomes")
            for a in analyses:
                pnl = a.get("pnl", 0)
                cat = a.get("failure_category", "Win")
                review_lines.append(
                    f"- {a.get('market_id', '?')}: "
                    f"{'WIN' if a.get('correct_prediction') else 'LOSS'} "
                    f"${pnl:+.2f} [{cat or 'Win'}]"
                )
            review_lines.append("")

        # Blocked trades today
        blocked_today = [t for t in today_trades if not t.get("risk_passed", False)]
        if blocked_today:
            review_lines.append(f"### Blocked trades ({len(blocked_today)})")
            for t in blocked_today[:5]:
                reasons = t.get("risk_failures", ["unknown"])
                review_lines.append(f"- {t.get('market_id', '?')}: {'; '.join(reasons)}")
            review_lines.append("")

        review_text = "\n".join(review_lines)

        # Append review to failure_log.md (separate from individual failure entries)
        REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
        log_fp = REFERENCES_DIR / "failure_log.md"
        with open(log_fp, "a", encoding="utf-8") as f:
            f.write(f"\n{review_text}\n")

        logger.info("Nightly review written to %s", log_fp)
        return review_text

    def _mark_analyzed(self, trade_id: str) -> None:
        """Mark a trade as analyzed in its individual file."""
        if not trade_id:
            return

        fp = TRADES_DIR / f"trade_{trade_id}.json"
        if not fp.exists():
            return

        try:
            with open(fp, "r", encoding="utf-8") as f:
                trade = json.load(f)
            trade["analyzed"] = True
            trade["analyzed_at"] = datetime.now(timezone.utc).isoformat()
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(trade, f, indent=2, ensure_ascii=False, default=str)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to mark trade %s as analyzed: %s", trade_id, exc)


def print_performance_report(report: PerformanceReport) -> None:
    """Print a formatted performance report."""
    print(f"\n{'='*50}")
    print(f"  PERFORMANCE REPORT")
    print(f"{'='*50}")
    print(f"  Total trades:     {report.total_trades}")
    print(f"  Executed:         {report.executed_trades}")
    print(f"  Blocked:          {report.blocked_trades}")
    print(f"  Open:             {report.open_trades}")
    print(f"  Closed:           {report.closed_trades}")
    print(f"{'─'*50}")
    print(f"  Win rate:         {report.win_rate:.1%}")
    print(f"  Total P&L:        ${report.total_pnl:.2f}")
    print(f"  Avg P&L/trade:    ${report.avg_pnl:.2f}")
    print(f"  Best trade:       ${report.best_trade_pnl:.2f}")
    print(f"  Worst trade:      ${report.worst_trade_pnl:.2f}")
    print(f"{'─'*50}")
    print(f"  Sharpe ratio:     {report.sharpe_ratio:.2f}")
    print(f"  Profit factor:    {report.profit_factor:.2f}")
    print(f"  Brier score:      {report.brier_score:.4f}")
    print(f"  Max drawdown:     {report.max_drawdown:.2%}")
    print(f"{'─'*50}")
    print(f"  Avg edge:         {report.avg_edge:+.4f}")
    print(f"  Avg confidence:   {report.avg_confidence:.4f}")
    print(f"{'='*50}")
