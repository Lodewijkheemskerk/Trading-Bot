"""
Balance Checker — Fetch remaining credit from AI provider APIs.

Providers with API access: DeepSeek
Providers requiring manual entry: OpenAI, Anthropic, xAI/Grok, Gemini

Manual balances are stored in data/balances.json and can be updated
via the dashboard settings page.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

BALANCES_FILE = Path(__file__).resolve().parent.parent / "data" / "balances.json"


def _load_stored() -> dict:
    """Load manually-entered balances from disk."""
    if BALANCES_FILE.exists():
        try:
            return json.loads(BALANCES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_stored(data: dict) -> None:
    """Persist balances to disk."""
    BALANCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    BALANCES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def fetch_deepseek_balance() -> Optional[float]:
    """Fetch DeepSeek remaining balance via their API."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None
    try:
        r = requests.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            for info in data.get("balance_infos", []):
                if info.get("currency") == "USD":
                    return float(info.get("total_balance", 0))
        return None
    except Exception as exc:
        logger.warning("DeepSeek balance check failed: %s", exc)
        return None


def get_all_balances() -> Dict[str, dict]:
    """
    Return balance info for all providers.

    Returns dict keyed by provider name:
    {
        "openai": {"balance": 12.50, "source": "manual", "updated": "2026-03-16T..."},
        "deepseek": {"balance": 1.99, "source": "api", "updated": "2026-03-16T..."},
        ...
    }
    """
    stored = _load_stored()
    now = datetime.now(timezone.utc).isoformat()

    providers = {
        "openai": {"name": "OpenAI", "source": "manual", "has_api": False},
        "anthropic": {"name": "Anthropic", "source": "manual", "has_api": False},
        "deepseek": {"name": "DeepSeek", "source": "api", "has_api": True},
        "grok": {"name": "xAI (Grok)", "source": "manual", "has_api": False},
        "gemini": {"name": "Gemini", "source": "manual", "has_api": False},
    }

    result = {}
    for key, meta in providers.items():
        entry = {
            "name": meta["name"],
            "source": meta["source"],
            "balance": None,
            "updated": None,
        }

        if key == "deepseek":
            balance = fetch_deepseek_balance()
            if balance is not None:
                entry["balance"] = balance
                entry["source"] = "api"
                entry["updated"] = now
                # Also save it
                stored[key] = {"balance": balance, "updated": now, "source": "api"}
            elif key in stored:
                entry["balance"] = stored[key].get("balance")
                entry["updated"] = stored[key].get("updated")
        elif key in stored:
            entry["balance"] = stored[key].get("balance")
            entry["updated"] = stored[key].get("updated")
            entry["source"] = "manual"

        result[key] = entry

    _save_stored(stored)
    return result


def update_balance(provider: str, balance: float) -> dict:
    """Manually update a provider's balance."""
    stored = _load_stored()
    now = datetime.now(timezone.utc).isoformat()
    stored[provider] = {
        "balance": balance,
        "updated": now,
        "source": "manual",
    }
    _save_stored(stored)
    return {"provider": provider, "balance": balance, "updated": now}
