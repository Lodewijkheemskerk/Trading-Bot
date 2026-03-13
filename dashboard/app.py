"""
Dashboard API — serves pipeline data to the Bloomberg-style frontend.
Includes manual pipeline trigger endpoint.
"""

import json
import glob
import sys
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, jsonify, send_from_directory, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, DATA_DIR, TRADES_DIR, PREDICTIONS_DIR, RESEARCH_DIR, MARKET_DIR, KILL_SWITCH_FILE

app = Flask(__name__, static_folder="static")

# Track pipeline run state
_pipeline_lock = threading.Lock()
_pipeline_running = False
_pipeline_last_run = None
_pipeline_last_error = None


def _load_latest(directory, pattern):
    """Load the most recent JSON file matching a glob pattern."""
    files = sorted(directory.glob(pattern), reverse=True)
    if not files:
        return None
    with open(files[0], "r", encoding="utf-8") as f:
        return json.load(f)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/scan")
def api_scan():
    data = _load_latest(MARKET_DIR, "scan_*.json")
    if data is None:
        return jsonify({"markets": [], "timestamp": None})
    return jsonify(data)


@app.route("/api/research")
def api_research():
    data = _load_latest(RESEARCH_DIR, "research_*.json")
    if data is None:
        return jsonify({"briefs": [], "timestamp": None})
    return jsonify(data)


@app.route("/api/predictions")
def api_predictions():
    data = _load_latest(PREDICTIONS_DIR, "predictions_*.json")
    if data is None:
        return jsonify({"signals": [], "timestamp": None})
    return jsonify(data)


@app.route("/api/trades")
def api_trades():
    data = _load_latest(TRADES_DIR, "execution_*.json")
    if data is None:
        return jsonify({"trades": [], "timestamp": None})
    return jsonify(data)


@app.route("/api/portfolio")
def api_portfolio():
    fp = TRADES_DIR / "portfolio_state.json"
    if fp.exists():
        with open(fp, "r") as f:
            return jsonify(json.load(f))
    settings = load_settings()
    bankroll = settings.get("bankroll", {}).get("initial", 500.0)
    return jsonify({
        "initial_bankroll": bankroll,
        "current_bankroll": bankroll,
        "peak_bankroll": bankroll,
        "open_positions": 0,
        "daily_pnl": 0.0,
        "daily_api_cost": 0.0,
        "total_trades": 0,
    })


@app.route("/api/performance")
def api_performance():
    """Compute performance metrics from all execution snapshots."""
    all_trades = []
    for fp in sorted(TRADES_DIR.glob("execution_*.json")):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            all_trades.extend(data.get("trades", []))
        except Exception:
            pass

    executed = [t for t in all_trades if t.get("risk_passed", False)]
    blocked = [t for t in all_trades if not t.get("risk_passed", False)]
    closed = [t for t in executed if t.get("status") == "closed"]
    open_trades = [t for t in executed if t.get("status") == "open"]

    pnls = [float(t.get("pnl", 0)) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    return jsonify({
        "total_trades": len(all_trades),
        "executed": len(executed),
        "blocked": len(blocked),
        "open": len(open_trades),
        "closed": len(closed),
        "win_rate": len(wins) / len(closed) if closed else 0,
        "total_pnl": sum(pnls),
        "gross_wins": sum(wins),
        "gross_losses": abs(sum(losses)) if losses else 0,
        "best_trade": max(pnls) if pnls else 0,
        "worst_trade": min(pnls) if pnls else 0,
    })


@app.route("/api/kill_switch")
def api_kill_switch():
    return jsonify({"active": KILL_SWITCH_FILE.exists()})


@app.route("/api/run", methods=["POST"])
def api_run_pipeline():
    """Trigger a single pipeline run in the background."""
    global _pipeline_running, _pipeline_last_run, _pipeline_last_error

    with _pipeline_lock:
        if _pipeline_running:
            return jsonify({"status": "already_running"}), 409

        _pipeline_running = True
        _pipeline_last_error = None

    def _run():
        global _pipeline_running, _pipeline_last_run, _pipeline_last_error
        try:
            project_root = Path(__file__).resolve().parent.parent
            result = subprocess.run(
                [sys.executable, str(project_root / "scripts" / "pipeline.py"), "--mode", "once"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
            )
            _pipeline_last_run = datetime.now(timezone.utc).isoformat()
            if result.returncode != 0:
                _pipeline_last_error = result.stderr[-500:] if result.stderr else "Unknown error"
        except subprocess.TimeoutExpired:
            _pipeline_last_error = "Pipeline timed out (120s)"
        except Exception as exc:
            _pipeline_last_error = str(exc)[:500]
        finally:
            with _pipeline_lock:
                _pipeline_running = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/pipeline_status")
def api_pipeline_status():
    """Check if a pipeline run is in progress, and compute next loop run time."""
    # Find last prediction snapshot timestamp as proxy for last completed cycle
    last_cycle = None
    preds = sorted(PREDICTIONS_DIR.glob("predictions_*.json"), reverse=True)
    if preds:
        # Parse timestamp from filename: predictions_YYYYMMDD_HHMMSS.json
        # These timestamps are in LOCAL time (not UTC)
        fname = preds[0].stem  # predictions_20260313_152328
        try:
            ts_str = fname.replace("predictions_", "")
            local_dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            # Return as epoch ms so frontend doesn't need timezone math
            last_cycle = local_dt.timestamp() * 1000
        except ValueError:
            pass

    return jsonify({
        "running": _pipeline_running,
        "last_run": _pipeline_last_run or last_cycle,
        "last_error": _pipeline_last_error,
        "loop_interval_minutes": 30,
    })


@app.route("/api/config")
def api_config():
    s = load_settings()
    return jsonify({
        "mode": s.get("mode", "paper"),
        "bankroll": s.get("bankroll", {}),
        "risk": s.get("risk", {}),
        "prediction": s.get("prediction", {}),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
