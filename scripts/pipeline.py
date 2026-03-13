"""
TRADING PIPELINE — Full 5-Step Orchestrator

Scan → Research → Predict → Execute → Compound

Usage:
  python scripts/pipeline.py --mode once          # Single pipeline run
  python scripts/pipeline.py --mode loop --interval 15  # Autonomous 15-min loop
  python scripts/pipeline.py --status             # Show performance metrics
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, KILL_SWITCH_FILE, DATA_DIR, TRADES_DIR

logger = logging.getLogger(__name__)


class TradingPipeline:
    """Orchestrates the complete 5-step trading pipeline."""

    def __init__(self, settings: Optional[dict] = None, heuristic_only: bool = False):
        self.settings = settings or load_settings()
        self.heuristic_only = heuristic_only
        self._step_count = 0

    def run_once(self) -> dict:
        """
        Execute one complete pipeline cycle: Scan → Research → Predict → Execute → Compound.

        Returns a summary dict of what happened in each step.
        """
        self._step_count += 1
        cycle_start = datetime.now(timezone.utc)
        summary = {
            "cycle": self._step_count,
            "timestamp": cycle_start.isoformat(),
            "steps": {},
            "success": True,
        }

        logger.info("="*60)
        logger.info("Pipeline cycle %d starting at %s", self._step_count, cycle_start.isoformat())
        logger.info("="*60)

        # Check kill switch before starting
        if self._check_kill_switch():
            summary["success"] = False
            summary["halted"] = True
            logger.warning("Kill switch active — pipeline halted")
            return summary

        # Step 1: SCAN
        scan_result = self._step_scan()
        summary["steps"]["scan"] = scan_result
        if not scan_result["success"]:
            summary["success"] = False
            logger.error("Scan step failed — aborting cycle")
            return summary

        # Step 2: RESEARCH
        research_result = self._step_research(scan_result.get("markets", []))
        summary["steps"]["research"] = research_result
        if not research_result["success"]:
            summary["success"] = False
            logger.error("Research step failed — aborting cycle")
            return summary

        # Step 3: PREDICT
        predict_result = self._step_predict()
        summary["steps"]["predict"] = predict_result
        if not predict_result["success"]:
            summary["success"] = False
            logger.error("Predict step failed — aborting cycle")
            return summary

        # Step 4: EXECUTE
        execute_result = self._step_execute()
        summary["steps"]["execute"] = execute_result
        if not execute_result["success"]:
            summary["success"] = False
            logger.error("Execute step failed — aborting cycle")
            return summary

        # Step 5: COMPOUND
        compound_result = self._step_compound()
        summary["steps"]["compound"] = compound_result

        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        summary["elapsed_seconds"] = round(elapsed, 1)
        logger.info("Pipeline cycle %d complete in %.1fs", self._step_count, elapsed)

        return summary

    def run_loop(self, interval_minutes: int = 15):
        """
        Run the pipeline in a continuous loop.

        Checks kill switch between cycles. Sleeps for interval_minutes.
        """
        logger.info("Starting pipeline loop with %d-minute interval", interval_minutes)
        logger.info("Create '%s' file to halt trading", KILL_SWITCH_FILE)

        while True:
            # Check kill switch
            if self._check_kill_switch():
                logger.warning("Kill switch active — stopping loop")
                print("\n[!] Kill switch detected -- trading halted.")
                break

            try:
                result = self.run_once()
                if result.get("halted"):
                    break
            except Exception as exc:
                logger.error("Pipeline cycle error: %s", exc, exc_info=True)
                # Continue to next cycle — don't crash the loop

            # Sleep until next cycle (check kill switch every 10 seconds)
            logger.info("Sleeping %d minutes until next cycle...", interval_minutes)
            sleep_seconds = interval_minutes * 60
            while sleep_seconds > 0:
                if self._check_kill_switch():
                    logger.warning("Kill switch activated during sleep — stopping")
                    print("\n[!] Kill switch detected -- trading halted.")
                    return
                time.sleep(min(10, sleep_seconds))
                sleep_seconds -= 10

    @staticmethod
    def activate_kill_switch():
        """Create the STOP file to halt all trading."""
        KILL_SWITCH_FILE.write_text(
            f"STOP — Trading halted at {datetime.now(timezone.utc).isoformat()}\n"
        )
        logger.warning("Kill switch ACTIVATED — created %s", KILL_SWITCH_FILE)
        print(f"[!] Kill switch activated: {KILL_SWITCH_FILE}")

    @staticmethod
    def deactivate_kill_switch():
        """Remove the STOP file to resume trading."""
        if KILL_SWITCH_FILE.exists():
            KILL_SWITCH_FILE.unlink()
            logger.info("Kill switch deactivated — removed %s", KILL_SWITCH_FILE)
            print(f"[OK] Kill switch deactivated: {KILL_SWITCH_FILE}")
        else:
            print("Kill switch was not active.")

    # ------------------------------------------------------------------
    # Pipeline steps
    # ------------------------------------------------------------------

    def _step_scan(self) -> dict:
        """Step 1: Scan Kalshi markets."""
        try:
            from scripts.scanner import MarketScanner
            from dataclasses import asdict
            logger.info("[Step 1/5] SCAN — Fetching Kalshi markets...")

            scanner = MarketScanner(settings=self.settings)
            result = scanner.scan_all()

            # ScanResult is a dataclass with .markets list of Market dataclasses
            markets = [asdict(m) for m in result.markets]
            logger.info("Scan complete: %d markets found", len(markets))

            return {
                "success": True,
                "markets_found": len(markets),
                "markets": markets[:10],  # Top 10 for research
            }
        except Exception as exc:
            logger.error("Scan failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _step_research(self, markets: list) -> dict:
        """Step 2: Research top markets."""
        try:
            from scripts.researcher import NewsResearcher, save_research_snapshot
            logger.info("[Step 2/5] RESEARCH — Analyzing %d markets...", len(markets))

            researcher = NewsResearcher(settings=self.settings)
            top_n = self.settings.get("research", {}).get("parallel_workers", 3)
            targets = markets[:top_n]

            briefs = []
            for market in targets:
                try:
                    brief = researcher.research_market(market)
                    briefs.append(brief)
                except Exception as exc:
                    logger.warning("Research failed for %s: %s", market.get("market_id", "?"), exc)

            if briefs:
                save_research_snapshot(briefs)

            logger.info("Research complete: %d briefs produced", len(briefs))
            return {
                "success": True,
                "markets_researched": len(briefs),
            }
        except Exception as exc:
            logger.error("Research failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _step_predict(self) -> dict:
        """Step 3: Run prediction engine."""
        try:
            from scripts.predictor import (
                PredictionEngine, load_latest_research_snapshot,
                save_prediction_snapshot,
            )
            import os
            logger.info("[Step 3/5] PREDICT — Running ensemble prediction...")

            # Force heuristic-only if configured
            if self.heuristic_only:
                os.environ.pop("ANTHROPIC_API_KEY", None)

            snapshot = load_latest_research_snapshot()
            if snapshot is None:
                return {"success": False, "error": "No research snapshot found"}

            briefs = snapshot.get("briefs", [])
            engine = PredictionEngine(settings=self.settings)
            signals = []

            for brief in briefs:
                try:
                    signal = engine.predict(brief)
                    signals.append(signal)
                except Exception as exc:
                    logger.warning("Predict failed for %s: %s", brief.get("market_id", "?"), exc)

            if signals:
                save_prediction_snapshot(signals)

            tradeable = sum(1 for s in signals if s.should_trade)
            logger.info("Predict complete: %d signals, %d tradeable", len(signals), tradeable)
            return {
                "success": True,
                "signals": len(signals),
                "tradeable": tradeable,
            }
        except Exception as exc:
            logger.error("Predict failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _step_execute(self) -> dict:
        """Step 4: Execute paper trades."""
        try:
            from scripts.executor import (
                TradeExecutor, load_latest_prediction_snapshot,
                save_execution_snapshot,
            )
            logger.info("[Step 4/5] EXECUTE — Running risk checks and paper trades...")

            snapshot = load_latest_prediction_snapshot()
            if snapshot is None:
                return {"success": False, "error": "No prediction snapshot found"}

            signals = snapshot.get("signals", [])
            executor = TradeExecutor(settings=self.settings)
            trades = []

            for signal in signals:
                if not signal.get("should_trade", False):
                    continue
                try:
                    trade = executor.execute_signal(signal)
                    trades.append(trade)
                    executor.save_trade(trade)
                except Exception as exc:
                    logger.warning("Execute failed for %s: %s", signal.get("market_id", "?"), exc)

            if trades:
                save_execution_snapshot(trades)

            executed = sum(1 for t in trades if t.risk_passed)
            blocked = sum(1 for t in trades if not t.risk_passed)
            logger.info("Execute complete: %d executed, %d blocked", executed, blocked)
            return {
                "success": True,
                "executed": executed,
                "blocked": blocked,
            }
        except Exception as exc:
            logger.error("Execute failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _step_compound(self) -> dict:
        """Step 5: Compound learning."""
        try:
            from scripts.compounder import Compounder
            logger.info("[Step 5/5] COMPOUND — Analyzing performance...")

            compounder = Compounder(settings=self.settings)
            report = compounder.get_performance_report()

            logger.info(
                "Compound complete: %d total trades, win_rate=%.1f%%, P&L=$%.2f",
                report.total_trades, report.win_rate * 100, report.total_pnl,
            )
            return {
                "success": True,
                "total_trades": report.total_trades,
                "win_rate": report.win_rate,
                "total_pnl": report.total_pnl,
            }
        except Exception as exc:
            logger.error("Compound failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_kill_switch() -> bool:
        """Check if the kill switch file exists."""
        return KILL_SWITCH_FILE.exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="AI-Powered Prediction Market Trading Bot — Full Pipeline"
    )
    parser.add_argument(
        "--mode", choices=["once", "loop"],
        help="Run mode: 'once' for single cycle, 'loop' for continuous.",
    )
    parser.add_argument(
        "--interval", type=int, default=15,
        help="Loop interval in minutes (default: 15). Only used with --mode loop.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show performance metrics and exit.",
    )
    parser.add_argument(
        "--heuristic-only", action="store_true",
        help="Skip Claude API, use heuristic models only.",
    )
    parser.add_argument(
        "--kill", action="store_true",
        help="Activate kill switch (create STOP file).",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Deactivate kill switch (remove STOP file).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Handle kill switch commands
    if args.kill:
        TradingPipeline.activate_kill_switch()
        sys.exit(0)

    if args.resume:
        TradingPipeline.deactivate_kill_switch()
        sys.exit(0)

    # Show status
    if args.status:
        from scripts.compounder import Compounder, print_performance_report
        compounder = Compounder()
        report = compounder.get_performance_report()
        print_performance_report(report)
        sys.exit(0)

    # Require --mode for pipeline execution
    if not args.mode:
        parser.print_help()
        print("\nExamples:")
        print("  python scripts/pipeline.py --mode once")
        print("  python scripts/pipeline.py --mode loop --interval 15")
        print("  python scripts/pipeline.py --status")
        print("  python scripts/pipeline.py --kill")
        print("  python scripts/pipeline.py --resume")
        sys.exit(0)

    pipeline = TradingPipeline(heuristic_only=args.heuristic_only)

    if args.mode == "once":
        print("\n>> Running single pipeline cycle...\n")
        result = pipeline.run_once()
        print(f"\nPipeline {'completed' if result['success'] else 'failed'}")
        if result.get("halted"):
            print("[!] Trading was halted by kill switch")
        for step_name, step_result in result.get("steps", {}).items():
            status = "OK" if step_result.get("success") else "FAIL"
            print(f"  {status} {step_name}: {step_result}")
        if "elapsed_seconds" in result:
            print(f"\nTotal time: {result['elapsed_seconds']:.1f}s")

    elif args.mode == "loop":
        print(f"\n>> Starting pipeline loop (interval: {args.interval} minutes)")
        print(f"   Create '{KILL_SWITCH_FILE}' to stop trading\n")
        try:
            pipeline.run_loop(interval_minutes=args.interval)
        except KeyboardInterrupt:
            print("\n\nPipeline stopped by user (Ctrl+C)")
