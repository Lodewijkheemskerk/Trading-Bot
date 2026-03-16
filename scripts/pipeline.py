"""
TRADING PIPELINE — Full 7-Step Orchestrator

Scan → Research → Predict → Execute → Monitor → Resolve → Compound

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
from config import load_settings, KILL_SWITCH_FILE, PAUSE_FILE, DATA_DIR, TRADES_DIR

logger = logging.getLogger(__name__)


class TradingPipeline:
    """Orchestrates the complete 7-step trading pipeline."""

    def __init__(self, settings: Optional[dict] = None, heuristic_only: bool = False):
        self.settings = settings or load_settings()
        self.heuristic_only = heuristic_only
        self._step_count = 0
        self._nightly_review_done_date: Optional[str] = None  # ISO date string

    def run_once(self) -> dict:
        """
        Execute one complete pipeline cycle:
        Scan → Research → Predict → Execute → Monitor → Resolve → Compound.

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

        # Refresh API health cache (non-blocking, failures are non-fatal)
        try:
            from scripts.api_health import check_all as _check_api_health
            health = _check_api_health()
            down = [k for k, v in health.items() if not v.get("ok")]
            if down:
                logger.warning("API health issues: %s", ", ".join(down))
        except Exception as exc:
            logger.debug("API health check failed: %s", exc)

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

        # Step 5: MONITOR — Check open positions for exit conditions
        monitor_result = self._step_monitor()
        summary["steps"]["monitor"] = monitor_result
        # Monitor failures are non-fatal — continue to resolve and compound

        # Step 6: RESOLVE — Check settled markets and close trades
        resolve_result = self._step_resolve()
        summary["steps"]["resolve"] = resolve_result
        # Resolve failures are non-fatal — we still want compound to run

        # Step 6: COMPOUND — Analyze, classify, learn
        compound_result = self._step_compound()
        summary["steps"]["compound"] = compound_result

        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        summary["elapsed_seconds"] = round(elapsed, 1)
        logger.info("Pipeline cycle %d complete in %.1fs", self._step_count, elapsed)

        # Save run log with cost estimate
        try:
            from scripts.run_logger import RunEntry, estimate_cycle_cost, save_run
            signals = predict_result.get("signals_data", [])
            cost, models = estimate_cycle_cost(signals)
            entry = RunEntry(
                timestamp=cycle_start.isoformat(),
                duration_seconds=round(elapsed, 1),
                cycle_number=self._step_count,
                scan=scan_result,
                research=research_result,
                predict=predict_result,
                execute=execute_result,
                monitor=monitor_result,
                resolve=resolve_result,
                compound=compound_result,
                markets_scanned=scan_result.get("markets_scanned", 0),
                markets_passed=scan_result.get("markets_found", 0),
                markets_researched=research_result.get("markets_researched", 0),
                predictions_made=predict_result.get("signals", 0),
                trades_executed=execute_result.get("executed", 0),
                trades_blocked=execute_result.get("blocked", 0),
                estimated_cost_usd=cost,
                models_called=models,
            )
            save_run(entry)
        except Exception as exc:
            logger.warning("Failed to save run log: %s", exc)

        return summary

    def run_loop(self, interval_minutes: int = None):
        """
        Run the pipeline in a continuous loop.

        Checks kill switch between cycles. Sleeps for interval_minutes.
        If interval_minutes is None, reads from settings.yaml scanner.schedule_minutes.
        """
        if interval_minutes is None:
            interval_minutes = self.settings.get("scanner", {}).get("schedule_minutes", 15)
        logger.info("Starting pipeline loop with %d-minute interval", interval_minutes)
        logger.info("Create '%s' file to halt trading", KILL_SWITCH_FILE)

        while True:
            # Check kill switch
            if self._check_kill_switch():
                logger.warning("Kill switch active — stopping loop")
                print("\n[!] Kill switch detected -- trading halted.")
                break

            # Check pause — skip cycle but keep loop alive
            if self._check_paused():
                logger.info("Pipeline paused — skipping cycle")
                print("[PAUSED] Pipeline paused -- skipping cycle, will check again in %d minutes" % interval_minutes)
            else:
                try:
                    result = self.run_once()
                    if result.get("halted"):
                        break
                except Exception as exc:
                    logger.error("Pipeline cycle error: %s", exc, exc_info=True)
                    # Continue to next cycle — don't crash the loop

            # Check if it's time for nightly review
            self._maybe_run_nightly_review()

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
            logger.info("[Step 1/7] SCAN — Fetching Kalshi markets...")

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
            logger.info("[Step 2/7] RESEARCH — Analyzing %d markets...", len(markets))

            researcher = NewsResearcher(settings=self.settings)
            top_n = self.settings.get("research", {}).get("parallel_workers", 3)
            targets = markets[:top_n]

            x_search_n = researcher.x_search_top_n if researcher.x_search_enabled else 0
            if x_search_n:
                logger.info("X Search enabled for top %d markets", x_search_n)

            briefs = researcher.research_markets(targets, x_search_top_n=x_search_n)

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
            logger.info("[Step 3/7] PREDICT — Running ensemble prediction...")

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

            # Serialize signals for run logger cost estimation
            from dataclasses import asdict
            signals_data = []
            for s in signals:
                try:
                    signals_data.append(asdict(s))
                except Exception:
                    signals_data.append({})

            return {
                "success": True,
                "signals": len(signals),
                "tradeable": tradeable,
                "signals_data": signals_data,
            }
        except Exception as exc:
            logger.error("Predict failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _step_execute(self) -> dict:
        """Step 4: Execute paper trades (gated by trading hours)."""
        # Trading hours gate — skip execution outside active window
        hours_check = self._check_trading_hours()
        if not hours_check["allowed"]:
            logger.info("[Step 4/7] EXECUTE — SKIPPED: %s", hours_check["reason"])
            return {
                "success": True,
                "executed": 0,
                "blocked": 0,
                "skipped_reason": hours_check["reason"],
            }

        try:
            from scripts.executor import (
                TradeExecutor, load_latest_prediction_snapshot,
                save_execution_snapshot,
            )
            logger.info("[Step 4/7] EXECUTE — Running risk checks and paper trades...")

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

    def _step_monitor(self) -> dict:
        """Step 5: Monitor open positions for exit conditions."""
        try:
            from scripts.position_monitor import PositionMonitor, save_monitor_snapshot
            logger.info("[Step 5/7] MONITOR — Checking open positions for exits...")

            monitor = PositionMonitor(settings=self.settings)
            summary = monitor.check_all()

            if summary["exits_triggered"] > 0:
                save_monitor_snapshot(summary)

            logger.info(
                "Monitor complete: %d checked, %d exits (P&L=$%.2f), %d held",
                summary["positions_checked"], summary["exits_triggered"],
                summary["total_exit_pnl"], summary["held"],
            )
            return {
                "success": True,
                "positions_checked": summary["positions_checked"],
                "exits_triggered": summary["exits_triggered"],
                "exits_by_reason": summary.get("exits_by_reason", {}),
                "exit_pnl": summary["total_exit_pnl"],
                "held": summary["held"],
            }
        except Exception as exc:
            logger.error("Monitor failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _step_resolve(self) -> dict:
        """Step 6: Resolve settled markets and close trades."""
        try:
            from scripts.resolver import TradeResolver, save_resolution_snapshot
            logger.info("[Step 6/7] RESOLVE — Checking settled markets...")

            resolver = TradeResolver(settings=self.settings)
            summary = resolver.resolve_all()

            # Update portfolio state if trades were resolved
            if summary["resolved"] > 0:
                resolver.update_portfolio_state(summary["resolutions"])
                save_resolution_snapshot(summary)

            logger.info(
                "Resolve complete: %d resolved (P&L=$%.2f), %d still open",
                summary["resolved"], summary["total_pnl_resolved"],
                summary["still_open"],
            )
            return {
                "success": True,
                "resolved": summary["resolved"],
                "still_open": summary["still_open"],
                "pnl_resolved": summary["total_pnl_resolved"],
            }
        except Exception as exc:
            logger.error("Resolve failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _step_compound(self) -> dict:
        """Step 6: Compound learning — analyze outcomes, classify failures, update knowledge base."""
        try:
            from scripts.compounder import Compounder
            logger.info("[Step 7/7] COMPOUND — Analyzing performance and learning...")

            compounder = Compounder(settings=self.settings)

            # Analyze newly closed trades, classify failures, append to failure_log
            trades = compounder.load_individual_trades()
            if not trades:
                trades = compounder.load_all_trades()

            newly_closed = [
                t for t in trades
                if t.get("status") == "closed" and not t.get("analyzed")
            ]

            analyses = []
            for trade in newly_closed:
                analysis = compounder.analyze_trade(trade)
                if analysis.get("status") != "no_outcome":
                    analyses.append(analysis)
                    compounder._mark_analyzed(trade.get("trade_id"))

            failures_logged = compounder.append_failures_to_log(analyses)

            # Generate performance report
            report = compounder.get_performance_report(trades)

            logger.info(
                "Compound complete: %d trades analyzed, %d failures logged, "
                "total=%d win_rate=%.1f%% P&L=$%.2f",
                len(analyses), failures_logged,
                report.total_trades, report.win_rate * 100, report.total_pnl,
            )
            return {
                "success": True,
                "newly_analyzed": len(analyses),
                "failures_logged": failures_logged,
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

    def _maybe_run_nightly_review(self) -> None:
        """
        Run the nightly review if the current hour matches the configured
        nightly_review_hour and we haven't already run it today.

        This triggers the full compound learning cycle: resolve outcomes,
        classify failures, write failure_log.md, generate performance report.
        """
        review_hour = self.settings.get("compound", {}).get("nightly_review_hour", 23)
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if now.hour != review_hour:
            return

        if self._nightly_review_done_date == today:
            return  # Already ran today

        logger.info("="*60)
        logger.info("NIGHTLY REVIEW — triggered at hour %d", review_hour)
        logger.info("="*60)

        try:
            from scripts.compounder import Compounder
            compounder = Compounder(self.settings)
            review_text = compounder.nightly_review()
            self._nightly_review_done_date = today
            logger.info("Nightly review complete")

            # Save review to a dated file
            review_fp = DATA_DIR / "reviews" / f"review_{today}.md"
            review_fp.parent.mkdir(parents=True, exist_ok=True)
            with open(review_fp, "w", encoding="utf-8") as f:
                f.write(review_text)
            logger.info("Review saved to %s", review_fp)

        except Exception as exc:
            logger.error("Nightly review failed: %s", exc, exc_info=True)

    def _check_trading_hours(self) -> dict:
        """
        Check if we're inside Kalshi's maintenance blackout window.

        Kalshi is 24/7 except Thursday 3–5 AM ET maintenance.
        Returns {"allowed": bool, "reason": str}.
        When maintenance_blackout is False, always allows trading.
        """
        hours_cfg = self.settings.get("trading_hours", {})
        if not hours_cfg.get("maintenance_blackout", True):
            return {"allowed": True, "reason": "Maintenance blackout check disabled"}

        blackout_day = hours_cfg.get("blackout_day", 3)       # 0=Mon, 3=Thu
        start_hour = hours_cfg.get("blackout_start_hour", 3)
        end_hour = hours_cfg.get("blackout_end_hour", 5)
        tz_name = hours_cfg.get("blackout_timezone", "America/New_York")

        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            from datetime import timezone as tz_mod
            tz = tz_mod.utc
            tz_name = "UTC"

        now = datetime.now(tz)
        current_hour = now.hour
        weekday = now.weekday()  # 0=Monday, 6=Sunday

        if weekday == blackout_day and start_hour <= current_hour < end_hour:
            return {
                "allowed": False,
                "reason": f"Kalshi maintenance window ({start_hour}:00–{end_hour}:00 {tz_name}, day={weekday})",
            }

        return {
            "allowed": True,
            "reason": "Outside maintenance window — trading allowed",
        }

    @staticmethod
    def _check_kill_switch() -> bool:
        """Check if the kill switch file exists."""
        return KILL_SWITCH_FILE.exists()

    @staticmethod
    def _check_paused() -> bool:
        """Check if the pause file exists."""
        return PAUSE_FILE.exists()

    @staticmethod
    def pause():
        """Create the PAUSE file to pause the pipeline loop."""
        PAUSE_FILE.write_text(
            f"PAUSED at {datetime.now(timezone.utc).isoformat()}\n"
        )
        logger.info("Pipeline PAUSED — created %s", PAUSE_FILE)

    @staticmethod
    def unpause():
        """Remove the PAUSE file to resume the pipeline loop."""
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
            logger.info("Pipeline RESUMED — removed %s", PAUSE_FILE)
        else:
            logger.info("Pipeline was not paused")


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
        "--interval", type=int, default=None,
        help="Loop interval in minutes (default: from settings.yaml). Only used with --mode loop.",
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
    parser.add_argument(
        "--pause", action="store_true",
        help="Pause the pipeline loop (skips cycles, loop stays alive).",
    )
    parser.add_argument(
        "--unpause", action="store_true",
        help="Resume the pipeline loop after a pause.",
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

    if args.pause:
        TradingPipeline.pause()
        print("[PAUSED] Pipeline will skip cycles until --unpause is called.")
        sys.exit(0)

    if args.unpause:
        TradingPipeline.unpause()
        print("[OK] Pipeline resumed.")
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
        interval = args.interval  # None means read from settings.yaml
        effective = interval or pipeline.settings.get("scanner", {}).get("schedule_minutes", 15)
        print(f"\n>> Starting pipeline loop (interval: {effective} minutes)")
        print(f"   Create '{KILL_SWITCH_FILE}' to stop trading\n")
        try:
            pipeline.run_loop(interval_minutes=interval)
        except KeyboardInterrupt:
            print("\n\nPipeline stopped by user (Ctrl+C)")
