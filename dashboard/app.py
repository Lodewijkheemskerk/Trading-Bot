"""
Dashboard API — serves pipeline data to the Bloomberg-style frontend.
"""

import json
import glob
import sys
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, jsonify, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, DATA_DIR, TRADES_DIR, PREDICTIONS_DIR, RESEARCH_DIR, MARKET_DIR, KILL_SWITCH_FILE

app = Flask(__name__, static_folder="static")


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
