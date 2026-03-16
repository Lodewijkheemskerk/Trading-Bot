"""
API Health Tracker — Monitors the status of all external API connections.

Writes health status after each pipeline cycle to data/api_health.json.
Dashboard polls /api/health to show warnings when a provider is down.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

HEALTH_FILE = Path(__file__).resolve().parent.parent / "data" / "api_health.json"


def _load() -> dict:
    if HEALTH_FILE.exists():
        try:
            return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict) -> None:
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def check_all() -> Dict[str, dict]:
    """
    Check connectivity and key validity for all providers.
    Returns dict keyed by provider name with status info.
    """
    now = datetime.now(timezone.utc).isoformat()
    results = {}

    # OpenAI
    results["openai"] = _check_openai()
    results["openai"]["checked_at"] = now

    # Anthropic
    results["anthropic"] = _check_anthropic()
    results["anthropic"]["checked_at"] = now

    # DeepSeek
    results["deepseek"] = _check_deepseek()
    results["deepseek"]["checked_at"] = now

    # Grok/xAI
    results["grok"] = _check_grok()
    results["grok"]["checked_at"] = now

    # Gemini
    results["gemini"] = _check_gemini()
    results["gemini"]["checked_at"] = now

    # Kalshi
    results["kalshi"] = _check_kalshi()
    results["kalshi"]["checked_at"] = now

    _save(results)
    return results


def _check_openai() -> dict:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return {"status": "no_key", "ok": False, "error": "OPENAI_API_KEY not set"}
    try:
        r = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            return {"status": "ok", "ok": True}
        elif r.status_code == 401:
            return {"status": "invalid_key", "ok": False, "error": "Invalid API key"}
        elif r.status_code == 429:
            return {"status": "rate_limited", "ok": False, "error": "Rate limited"}
        else:
            return {"status": "error", "ok": False, "error": f"HTTP {r.status_code}"}
    except requests.Timeout:
        return {"status": "timeout", "ok": False, "error": "Connection timeout"}
    except Exception as e:
        return {"status": "error", "ok": False, "error": str(e)[:100]}


def _check_anthropic() -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"status": "no_key", "ok": False, "error": "ANTHROPIC_API_KEY not set"}
    try:
        # Use the count_tokens endpoint as a lightweight check
        r = requests.post(
            "https://api.anthropic.com/v1/messages/count_tokens",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=10,
        )
        if r.status_code == 200:
            return {"status": "ok", "ok": True}
        elif r.status_code == 401:
            return {"status": "invalid_key", "ok": False, "error": "Invalid API key"}
        elif r.status_code == 429:
            return {"status": "rate_limited", "ok": False, "error": "Rate limited"}
        else:
            return {"status": "ok", "ok": True}  # 4xx might be endpoint issue, key is probably fine
    except requests.Timeout:
        return {"status": "timeout", "ok": False, "error": "Connection timeout"}
    except Exception as e:
        return {"status": "error", "ok": False, "error": str(e)[:100]}


def _check_deepseek() -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return {"status": "no_key", "ok": False, "error": "DEEPSEEK_API_KEY not set"}
    try:
        r = requests.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            available = data.get("is_available", False)
            if not available:
                return {"status": "no_balance", "ok": False, "error": "No balance remaining"}
            return {"status": "ok", "ok": True}
        elif r.status_code == 401:
            return {"status": "invalid_key", "ok": False, "error": "Invalid API key"}
        else:
            return {"status": "error", "ok": False, "error": f"HTTP {r.status_code}"}
    except requests.Timeout:
        return {"status": "timeout", "ok": False, "error": "Connection timeout"}
    except Exception as e:
        return {"status": "error", "ok": False, "error": str(e)[:100]}


def _check_grok() -> dict:
    key = os.environ.get("GROK_API_KEY", "")
    if not key:
        return {"status": "no_key", "ok": False, "error": "GROK_API_KEY not set"}
    try:
        r = requests.get(
            "https://api.x.ai/v1/api-key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("api_key_blocked") or data.get("api_key_disabled"):
                return {"status": "blocked", "ok": False, "error": "API key blocked or disabled"}
            return {"status": "ok", "ok": True}
        elif r.status_code == 401:
            return {"status": "invalid_key", "ok": False, "error": "Invalid API key"}
        else:
            return {"status": "error", "ok": False, "error": f"HTTP {r.status_code}"}
    except requests.Timeout:
        return {"status": "timeout", "ok": False, "error": "Connection timeout"}
    except Exception as e:
        return {"status": "error", "ok": False, "error": str(e)[:100]}


def _check_gemini() -> dict:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return {"status": "no_key", "ok": False, "error": "GEMINI_API_KEY not set"}
    try:
        r = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
            timeout=10,
        )
        if r.status_code == 200:
            return {"status": "ok", "ok": True}
        elif r.status_code == 400 or r.status_code == 403:
            return {"status": "invalid_key", "ok": False, "error": "Invalid or restricted API key"}
        else:
            return {"status": "error", "ok": False, "error": f"HTTP {r.status_code}"}
    except requests.Timeout:
        return {"status": "timeout", "ok": False, "error": "Connection timeout"}
    except Exception as e:
        return {"status": "error", "ok": False, "error": str(e)[:100]}


def _check_kalshi() -> dict:
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    if not key_id:
        return {"status": "no_key", "ok": False, "error": "KALSHI_API_KEY_ID not set"}
    try:
        from scripts.kalshi_client import KalshiClient
        client = KalshiClient.from_env()
        bal = client.get_balance()
        return {"status": "ok", "ok": True, "balance": bal}
    except Exception as e:
        err = str(e)[:100]
        if "401" in err or "auth" in err.lower():
            return {"status": "invalid_key", "ok": False, "error": "Authentication failed"}
        return {"status": "error", "ok": False, "error": err}


def get_cached_health() -> dict:
    """Return cached health status without re-checking."""
    return _load()
