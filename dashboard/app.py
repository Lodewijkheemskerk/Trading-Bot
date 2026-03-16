"""
Dashboard API — serves pipeline data to the Bloomberg-style frontend.
Includes manual pipeline trigger endpoint.
"""

import json
import glob
import sys
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, timezone

import yaml
from flask import Flask, jsonify, send_from_directory, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, DATA_DIR, TRADES_DIR, PREDICTIONS_DIR, RESEARCH_DIR, MARKET_DIR, KILL_SWITCH_FILE, PAUSE_FILE

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


import time as _time

_kalshi_cache = {"data": None, "ts": 0}
_KALSHI_CACHE_TTL = 30  # seconds — don't hit Kalshi API more than once per 30s


@app.route("/api/kalshi")
def api_kalshi():
    """Kalshi demo/live connection status, balance, positions, and orders.
    Cached server-side for 30s to avoid hammering the external API."""
    now = _time.time()
    if _kalshi_cache["data"] and (now - _kalshi_cache["ts"]) < _KALSHI_CACHE_TTL:
        return jsonify(_kalshi_cache["data"])

    import os
    from dotenv import load_dotenv
    load_dotenv()

    kalshi_env = os.getenv("KALSHI_ENV", "paper")
    result = {
        "mode": kalshi_env,
        "connected": False,
        "balance": 0.0,
        "portfolio_value": 0.0,
        "positions": [],
        "open_orders": [],
        "recent_fills": [],
    }

    if kalshi_env in ("demo", "live"):
        try:
            from scripts.kalshi_client import KalshiClient
            client = KalshiClient.from_env()

            # Balance
            bal = client.get_balance()
            result["connected"] = True
            result["balance"] = bal.get("balance", 0) / 100.0
            result["portfolio_value"] = bal.get("portfolio_value", 0) / 100.0

            # Positions
            result["positions"] = client.get_positions()

            # Open orders
            result["open_orders"] = client.get_orders(status="resting")

            # Recent fills
            result["recent_fills"] = client.get_fills(limit=20)

        except Exception as exc:
            result["error"] = str(exc)

    _kalshi_cache["data"] = result
    _kalshi_cache["ts"] = now
    return jsonify(result)


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


@app.route("/api/pause", methods=["GET", "POST"])
def api_pause():
    """GET: check pause status. POST: toggle pause on/off."""
    if request.method == "GET":
        paused = PAUSE_FILE.exists()
        paused_at = None
        if paused:
            try:
                paused_at = PAUSE_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return jsonify({"paused": paused, "paused_at": paused_at})

    # POST — toggle
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
        return jsonify({"paused": False, "action": "resumed"})
    else:
        PAUSE_FILE.write_text(
            f"PAUSED at {datetime.now(timezone.utc).isoformat()}\n"
        )
        return jsonify({"paused": True, "action": "paused"})


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


# --- Available models registry (what the system knows about) ---
AVAILABLE_MODELS = [
    {
        "name": "grok", "label": "Grok", "type": "api",
        "env_key": "GROK_API_KEY", "default_role": "primary_forecaster",
        "description": "xAI primary forecaster",
        "default_model_id": "grok-3-fast",
        "model_options": [
            {"id": "grok-3-fast",  "label": "Grok 3 Fast",  "tier": "fast"},
            {"id": "grok-3",       "label": "Grok 3",       "tier": "standard"},
            {"id": "grok-3-mini",  "label": "Grok 3 Mini",  "tier": "mini"},
        ],
    },
    {
        "name": "claude", "label": "Claude", "type": "api",
        "env_key": "ANTHROPIC_API_KEY", "default_role": "news_analyst",
        "description": "Anthropic news analyst",
        "default_model_id": "claude-sonnet-4-20250514",
        "model_options": [
            {"id": "claude-sonnet-4-20250514",  "label": "Claude Sonnet 4",   "tier": "standard"},
            {"id": "claude-opus-4-20250514",    "label": "Claude Opus 4",     "tier": "premium"},
        ],
    },
    {
        "name": "gpt4o", "label": "GPT-4o", "type": "api",
        "env_key": "OPENAI_API_KEY", "default_role": "bull_advocate",
        "description": "OpenAI bull advocate",
        "default_model_id": "gpt-4o-mini",
        "model_options": [
            {"id": "gpt-4o-mini",   "label": "GPT-4o Mini",    "tier": "mini"},
            {"id": "gpt-4o",        "label": "GPT-4o",         "tier": "standard"},
            {"id": "gpt-4.1",       "label": "GPT-4.1",        "tier": "standard"},
            {"id": "gpt-4.1-mini",  "label": "GPT-4.1 Mini",   "tier": "mini"},
            {"id": "gpt-4.1-nano",  "label": "GPT-4.1 Nano",   "tier": "fast"},
        ],
    },
    {
        "name": "gemini", "label": "Gemini", "type": "api",
        "env_key": "GEMINI_API_KEY", "default_role": "bear_advocate",
        "description": "Google AI bear advocate",
        "default_model_id": "gemini-2.5-flash",
        "model_options": [
            {"id": "gemini-2.5-flash",  "label": "Gemini 2.5 Flash", "tier": "fast"},
            {"id": "gemini-2.5-pro",    "label": "Gemini 2.5 Pro",   "tier": "premium"},
            {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "tier": "mini"},
        ],
    },
    {
        "name": "deepseek", "label": "DeepSeek", "type": "api",
        "env_key": "DEEPSEEK_API_KEY", "default_role": "risk_manager",
        "description": "DeepSeek risk manager",
        "default_model_id": "deepseek-chat",
        "model_options": [
            {"id": "deepseek-chat",      "label": "DeepSeek V3",      "tier": "standard"},
            {"id": "deepseek-reasoner",  "label": "DeepSeek R1",      "tier": "premium"},
        ],
    },
    {
        "name": "xgboost", "label": "XGBoost", "type": "local",
        "env_key": None, "default_role": "statistical_calibrator",
        "description": "Statistical calibration model (no API cost)",
        "default_model_id": None,
        "model_options": [],
    },
]

AVAILABLE_ROLES = [
    "primary_forecaster",
    "news_analyst",
    "bull_advocate",
    "bear_advocate",
    "risk_manager",
    "statistical_calibrator",
]


@app.route("/settings")
def settings_page():
    return send_from_directory("static", "settings.html")


@app.route("/api/ensemble", methods=["GET"])
def api_ensemble_get():
    """Return current ensemble config + available models with API key status."""
    import os
    s = load_settings()
    ensemble = s.get("prediction", {}).get("ensemble_models", [])

    # Build active model set
    active_names = {m["name"] for m in ensemble}

    # Check which API keys are set
    models_with_status = []
    for m in AVAILABLE_MODELS:
        has_key = True
        if m["type"] == "local" and m["name"] == "xgboost":
            has_key = Path("config/xgboost_model.json").exists()
        elif m["env_key"]:
            has_key = bool(os.environ.get(m["env_key"]))
        active_entry = next((e for e in ensemble if e["name"] == m["name"]), None)
        models_with_status.append({
            **m,
            "active": m["name"] in active_names,
            "has_api_key": has_key,
            "weight": active_entry["weight"] if active_entry else 0,
            "role": active_entry["role"] if active_entry else m.get("default_role", ""),
            "model_id": active_entry.get("model_id", m.get("default_model_id")) if active_entry else m.get("default_model_id"),
        })

    return jsonify({
        "models": models_with_status,
        "roles": AVAILABLE_ROLES,
        "min_edge": s.get("prediction", {}).get("min_edge", 0.04),
        "min_confidence": s.get("prediction", {}).get("min_confidence", 0.65),
    })


@app.route("/api/ensemble", methods=["POST"])
def api_ensemble_save():
    """Save new ensemble configuration to settings.yaml."""
    data = request.get_json()
    if not data or "models" not in data:
        return jsonify({"error": "Missing 'models' in request body"}), 400

    models = data["models"]

    # Validate: weights must sum to ~1.0
    total_weight = sum(m.get("weight", 0) for m in models if m.get("active"))
    if abs(total_weight - 1.0) > 0.01:
        return jsonify({"error": f"Weights must sum to 100% (currently {total_weight*100:.1f}%)"}), 400

    # Validate: at least one model active
    active = [m for m in models if m.get("active")]
    if not active:
        return jsonify({"error": "At least one model must be active"}), 400

    # Build new ensemble list
    new_ensemble = []
    for m in models:
        if not m.get("active"):
            continue
        entry = {
            "name": m["name"],
            "role": m.get("role", ""),
            "weight": round(m["weight"], 2),
        }
        if m.get("model_id"):
            entry["model_id"] = m["model_id"]
        new_ensemble.append(entry)

    # Load, modify, save settings.yaml
    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    settings["prediction"]["ensemble_models"] = new_ensemble

    # Also update min_edge and min_confidence if provided
    if "min_edge" in data:
        settings["prediction"]["min_edge"] = round(float(data["min_edge"]), 3)
    if "min_confidence" in data:
        settings["prediction"]["min_confidence"] = round(float(data["min_confidence"]), 2)

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(settings, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return jsonify({"saved": True, "ensemble": new_ensemble})


# ---------------------------------------------------------------------------
# Risk & Position Monitor settings API
# ---------------------------------------------------------------------------

@app.route("/api/risk-settings", methods=["GET"])
def api_risk_settings_get():
    """Return current risk management and position monitor settings."""
    s = load_settings()
    risk = s.get("risk", {})
    monitor = s.get("position_monitor", {})
    return jsonify({
        "risk": {
            "kelly_fraction":           risk.get("kelly_fraction", 0.25),
            "max_position_pct":         risk.get("max_position_pct", 0.05),
            "max_concurrent_positions": risk.get("max_concurrent_positions", 15),
            "max_daily_loss_pct":       risk.get("max_daily_loss_pct", 0.15),
            "max_drawdown_pct":         risk.get("max_drawdown_pct", 0.08),
            "max_slippage_pct":         risk.get("max_slippage_pct", 0.02),
            "var_confidence":           risk.get("var_confidence", 0.95),
            "max_var_pct":              risk.get("max_var_pct", 0.10),
            "max_total_exposure_pct":   risk.get("max_total_exposure_pct", 0.40),
            "max_daily_api_cost":       risk.get("max_daily_api_cost", 50.0),
        },
        "position_monitor": {
            "stop_loss_pct":       monitor.get("stop_loss_pct", 0.15),
            "take_profit_pct":     monitor.get("take_profit_pct", 0.20),
            "max_hold_hours":      monitor.get("max_hold_hours", 240),
            "emergency_stop_pct":  monitor.get("emergency_stop_pct", 0.10),
            "edge_floor":          monitor.get("edge_floor", 0.0),
        },
    })


@app.route("/api/risk-settings", methods=["POST"])
def api_risk_settings_save():
    """Save updated risk management and position monitor settings."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Empty request body"}), 400

    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    # Update risk settings
    if "risk" in data:
        r = data["risk"]
        if "risk" not in settings:
            settings["risk"] = {}
        for key in (
            "kelly_fraction", "max_position_pct", "max_concurrent_positions",
            "max_daily_loss_pct", "max_drawdown_pct", "max_slippage_pct",
            "var_confidence", "max_var_pct", "max_total_exposure_pct",
            "max_daily_api_cost",
        ):
            if key in r:
                val = r[key]
                # Integer fields
                if key in ("max_concurrent_positions",):
                    settings["risk"][key] = int(val)
                else:
                    settings["risk"][key] = round(float(val), 4)

    # Update position monitor settings
    if "position_monitor" in data:
        pm = data["position_monitor"]
        if "position_monitor" not in settings:
            settings["position_monitor"] = {}
        for key in (
            "stop_loss_pct", "take_profit_pct", "max_hold_hours",
            "emergency_stop_pct", "edge_floor",
        ):
            if key in pm:
                val = pm[key]
                if key == "max_hold_hours":
                    settings["position_monitor"][key] = int(val)
                else:
                    settings["position_monitor"][key] = round(float(val), 4)

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(settings, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return jsonify({"saved": True})


@app.route("/api/trading-hours", methods=["GET"])
def api_trading_hours_get():
    """Return current schedule and maintenance blackout settings."""
    s = load_settings()
    th = s.get("trading_hours", {})
    scanner = s.get("scanner", {})
    return jsonify({
        "schedule_minutes":     scanner.get("schedule_minutes", 15),
        "maintenance_blackout": th.get("maintenance_blackout", True),
        "blackout_day":         th.get("blackout_day", 3),
        "blackout_start_hour":  th.get("blackout_start_hour", 3),
        "blackout_end_hour":    th.get("blackout_end_hour", 5),
    })


@app.route("/api/trading-hours", methods=["POST"])
def api_trading_hours_save():
    """Save updated schedule and maintenance blackout settings."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Empty request body"}), 400

    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    if "trading_hours" not in settings:
        settings["trading_hours"] = {}
    if "scanner" not in settings:
        settings["scanner"] = {}

    if "schedule_minutes" in data:
        settings["scanner"]["schedule_minutes"] = int(data["schedule_minutes"])
    if "maintenance_blackout" in data:
        settings["trading_hours"]["maintenance_blackout"] = bool(data["maintenance_blackout"])
    if "blackout_day" in data:
        settings["trading_hours"]["blackout_day"] = int(data["blackout_day"])
    for key in ("blackout_start_hour", "blackout_end_hour"):
        if key in data:
            settings["trading_hours"][key] = int(data[key])

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(settings, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return jsonify({"saved": True})


@app.route("/api/runs", methods=["GET"])
def api_runs():
    """Return recent pipeline run history with costs."""
    try:
        from scripts.run_logger import load_runs, get_cost_summary
        limit = request.args.get("limit", 50, type=int)
        runs = load_runs(limit=limit)
        cost_today = get_cost_summary(days=1)
        cost_week = get_cost_summary(days=7)
        return jsonify({
            "runs": runs,
            "cost_today": cost_today,
            "cost_week": cost_week,
        })
    except Exception as exc:
        return jsonify({"runs": [], "error": str(exc)})


@app.route("/backtest")
def backtest_page():
    return send_from_directory("static", "backtest.html")


@app.route("/api/backtest", methods=["GET"])
def api_backtest_get():
    """Return the latest backtest result."""
    from scripts.backtester import Backtester
    result = Backtester.load_latest_result()
    if result:
        return jsonify(result)
    return jsonify({"empty": True, "message": "No backtest results yet. Run a backtest first."})


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    """Trigger a new backtest run."""
    from scripts.backtester import Backtester

    fetch = request.json.get("fetch_outcomes", True) if request.json else True
    bankroll = request.json.get("bankroll", 1000.0) if request.json else 1000.0

    try:
        bt = Backtester()
        bt.bankroll = bankroll
        result = bt.run(fetch_outcomes=fetch)
        return jsonify({"success": True, "result": asdict(result)})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
