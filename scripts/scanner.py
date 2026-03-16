"""
Step 1: SCAN — Find Markets Worth Trading

Connects to the Kalshi REST API, fetches active markets with cursor
pagination, parses dollar-string fields, filters by volume / liquidity /
expiry / category, flags anomalies, scores opportunities, and saves
scan snapshots to disk.

Before scoring, reads failure_log.md to check past mistakes and
deprioritize markets where the bot has previously lost money due to
systematic issues (Bad Prediction, External Shock).
"""

import json
import logging
import math
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import List, Optional, Dict

import requests

# Allow running both as `python scripts/scanner.py` and as an import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import load_settings, MARKET_DIR, DATA_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes (consumed by S02 researcher via dict conversion)
# ---------------------------------------------------------------------------

@dataclass
class Market:
    """A single tradeable prediction market on Kalshi."""

    platform: str              # Always "kalshi" for M001
    market_id: str             # ticker
    title: str
    description: str
    category: str              # Derived from event_ticker prefix
    event_ticker: str

    # Prices (floats, converted from dollar strings)
    yes_price: float           # Current YES ask
    no_price: float            # Current NO ask
    yes_bid: float
    spread: float              # yes_ask - yes_bid

    # Volume & liquidity
    volume_24h: float
    total_volume: float
    liquidity: float
    open_interest: float

    # Timing
    expiry_date: str           # ISO string
    days_to_expiry: int

    last_price: float
    url: str

    # Anomaly flags
    price_move_24h: float = 0.0
    volume_spike: float = 0.0
    is_anomaly: bool = False
    anomaly_reasons: str = ""

    # Scoring
    opportunity_score: float = 0.0


@dataclass
class ScanResult:
    """Output of a full scan run."""

    timestamp: str
    markets_scanned: int
    markets_passed: int
    markets: List[Market]
    scan_duration_seconds: float


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class MarketScanner:
    """Scans Kalshi for tradeable prediction markets."""

    def __init__(self, settings: Optional[dict] = None):
        s = settings or load_settings()
        scan = s.get("scanner", {})
        kalshi = s.get("kalshi", {})
        exec_cfg = s.get("execution", {})

        self.base_url = kalshi.get("base_url", "https://api.elections.kalshi.com/trade-api/v2")
        self.min_volume = scan.get("min_volume", 50)
        self.max_days_to_expiry = scan.get("max_days_to_expiry", 30)
        self.min_liquidity = scan.get("min_liquidity", 0)
        self.anomaly_price_move = scan.get("anomaly_price_move", 0.10)
        self.anomaly_spread = scan.get("anomaly_spread", 0.05)
        self.volume_spike_mult = scan.get("volume_spike_multiplier", 2.0)
        self.skip_prefixes = tuple(scan.get("skip_categories", []))
        self.max_pages = scan.get("max_pages", 10)

        # Retry config
        self.retry_attempts = exec_cfg.get("retry_attempts", 3)
        self.retry_delay = exec_cfg.get("retry_delay_seconds", 5)

        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

        # Load past failures to deprioritize problematic markets
        self.past_failures = self._load_past_failures()

        # Volume history for 7-day average spike detection
        self._volume_history_file = DATA_DIR / "volume_history.json"
        self._volume_history = self._load_volume_history()

    # ------------------------------------------------------------------
    # Volume history (7-day rolling average for spike detection)
    # ------------------------------------------------------------------

    def _load_volume_history(self) -> Dict[str, Dict[str, float]]:
        """
        Load volume history from disk.

        Structure: { "MARKET_ID": { "2026-03-15": 1200.0, "2026-03-14": 800.0, ... } }
        """
        if self._volume_history_file.exists():
            try:
                with open(self._volume_history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load volume history: %s", exc)
        return {}

    def _save_volume_history(self) -> None:
        """Persist volume history to disk."""
        try:
            self._volume_history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._volume_history_file, "w", encoding="utf-8") as f:
                json.dump(self._volume_history, f, indent=1, ensure_ascii=False)
        except OSError as exc:
            logger.warning("Failed to save volume history: %s", exc)

    def _record_volume(self, market_id: str, volume_24h: float) -> None:
        """Record today's volume for a market."""
        today = date.today().isoformat()
        if market_id not in self._volume_history:
            self._volume_history[market_id] = {}
        self._volume_history[market_id][today] = volume_24h

    def _get_7day_avg_volume(self, market_id: str) -> Optional[float]:
        """
        Compute the 7-day average volume for a market.

        Returns None if fewer than 2 days of history (can't detect a spike
        with only one data point).
        """
        history = self._volume_history.get(market_id, {})
        if len(history) < 2:
            return None

        # Get volumes from the last 7 days (excluding today)
        today = date.today()
        volumes = []
        for days_back in range(1, 8):
            day_str = (today - timedelta(days=days_back)).isoformat()
            if day_str in history:
                volumes.append(history[day_str])

        if not volumes:
            return None

        return sum(volumes) / len(volumes)

    def _prune_old_volume_history(self, max_days: int = 14) -> None:
        """Remove volume entries older than max_days to keep file small."""
        cutoff = (date.today() - timedelta(days=max_days)).isoformat()
        for market_id in list(self._volume_history):
            history = self._volume_history[market_id]
            pruned = {d: v for d, v in history.items() if d >= cutoff}
            if pruned:
                self._volume_history[market_id] = pruned
            else:
                del self._volume_history[market_id]

    # ------------------------------------------------------------------
    # Failure log integration
    # ------------------------------------------------------------------

    @staticmethod
    def _load_past_failures() -> dict:
        """
        Load failure_log.md and build a lookup of market_id → failure info.

        Returns dict mapping market_id to list of failure dicts.
        Used during scoring to penalize markets with past systematic failures.
        """
        try:
            from scripts.compounder import Compounder
            entries = Compounder.load_failure_log()
        except Exception as exc:
            logger.debug("Could not load failure log: %s", exc)
            return {}

        failures_by_market: dict = {}
        for entry in entries:
            mid = entry.get("market_id", "")
            if mid:
                failures_by_market.setdefault(mid, []).append(entry)

        if failures_by_market:
            logger.info(
                "Loaded %d past failures across %d markets from failure log",
                sum(len(v) for v in failures_by_market.values()),
                len(failures_by_market),
            )

        return failures_by_market

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan_all(self) -> ScanResult:
        """Run a full scan: fetch -> parse -> filter -> score -> rank."""
        t0 = time.time()
        raw_markets = self._fetch_all_markets()
        total_scanned = len(raw_markets)

        parsed: List[Market] = []
        for raw in raw_markets:
            m = self._parse_market(raw)
            if m and self._passes_filters(m):
                # Record volume for 7-day history before checking anomalies
                self._record_volume(m.market_id, m.volume_24h)
                self._check_anomalies(m)
                m.opportunity_score = self._score(m)
                parsed.append(m)

        # Persist volume history and prune old entries
        self._prune_old_volume_history()
        self._save_volume_history()

        parsed.sort(key=lambda m: m.opportunity_score, reverse=True)

        result = ScanResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            markets_scanned=total_scanned,
            markets_passed=len(parsed),
            markets=parsed,
            scan_duration_seconds=round(time.time() - t0, 2),
        )
        logger.info(
            "Scan complete: %d scanned, %d passed, %d anomalies, %.1fs",
            total_scanned, len(parsed),
            sum(1 for m in parsed if m.is_anomaly),
            result.scan_duration_seconds,
        )
        return result

    def save_snapshot(self, result: ScanResult, output_dir: Optional[Path] = None) -> Path:
        """Persist scan results as a timestamped JSON file."""
        out = output_dir or MARKET_DIR
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = out / f"scan_{ts}.json"
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "timestamp": result.timestamp,
                    "markets_scanned": result.markets_scanned,
                    "markets_passed": result.markets_passed,
                    "scan_duration_seconds": result.scan_duration_seconds,
                    "markets": [asdict(m) for m in result.markets],
                },
                f, indent=2, default=str,
            )
        logger.info("Snapshot saved: %s", fp)
        return fp

    # ------------------------------------------------------------------
    # Kalshi API
    # ------------------------------------------------------------------

    def _fetch_all_markets(self) -> list:
        """
        Fetch markets from Kalshi via the events endpoint.

        The /events?with_nested_markets=true endpoint populates volume and
        price fields that the plain /markets list leaves at zero.
        """
        all_markets: list = []
        cursor: Optional[str] = None
        page = 0

        while page < self.max_pages:
            params = {
                "limit": 100,
                "status": "open",
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor

            try:
                from scripts.retry import retry_call
                resp = retry_call(
                    self.session.get,
                    f"{self.base_url}/events", params=params, timeout=20,
                    max_attempts=self.retry_attempts,
                    base_delay=self.retry_delay,
                    context=f"Kalshi events page {page}",
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as exc:
                logger.warning("Kalshi API error (page %d) after retries: %s", page, exc)
                break

            events = data.get("events", [])
            if not events:
                break

            for event in events:
                category = event.get("category", "")
                for mkt in event.get("markets", []):
                    mkt["_event_category"] = category
                    all_markets.append(mkt)

            cursor = data.get("cursor")
            page += 1

            if not cursor:
                break

        logger.info("Fetched %d raw markets from %d page(s) of events", len(all_markets), page)
        return all_markets

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        """Parse a Kalshi dollar/fp string to float safely."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _parse_market(self, raw: dict) -> Optional[Market]:
        """Convert a raw Kalshi JSON market to our Market dataclass."""
        try:
            ticker = raw.get("ticker", "")
            event_ticker = raw.get("event_ticker", "")

            yes_ask = self._safe_float(raw.get("yes_ask_dollars"))
            yes_bid = self._safe_float(raw.get("yes_bid_dollars"))
            no_ask = self._safe_float(raw.get("no_ask_dollars"))

            # Days to expiry
            expiry_str = raw.get("expiration_time") or raw.get("close_time", "")
            days = 999
            if expiry_str:
                try:
                    exp_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                    days = max(0, (exp_dt - datetime.now(timezone.utc)).days)
                except (ValueError, TypeError):
                    pass

            # Category from nested event data, fallback to ticker prefix
            category = raw.get("_event_category", "") or (
                event_ticker.split("-")[0] if event_ticker else "unknown"
            )

            # Previous price for 24h move estimate
            prev_price = self._safe_float(raw.get("previous_price_dollars"))
            price_move = abs(yes_ask - prev_price) if prev_price > 0 else 0.0

            return Market(
                platform="kalshi",
                market_id=ticker,
                title=raw.get("title", ticker),
                description=raw.get("subtitle", ""),
                category=category,
                event_ticker=event_ticker,
                yes_price=yes_ask,
                no_price=no_ask,
                yes_bid=yes_bid,
                spread=round(yes_ask - yes_bid, 4) if yes_ask and yes_bid else 0.0,
                volume_24h=self._safe_float(raw.get("volume_24h_fp")),
                total_volume=self._safe_float(raw.get("volume_fp")),
                liquidity=self._safe_float(raw.get("liquidity_dollars")),
                open_interest=self._safe_float(raw.get("open_interest_fp")),
                expiry_date=expiry_str,
                days_to_expiry=days,
                last_price=self._safe_float(raw.get("last_price_dollars")),
                url=f"https://kalshi.com/markets/{ticker}",
                price_move_24h=round(price_move, 4),
            )
        except Exception as exc:
            logger.debug("Parse error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _passes_filters(self, m: Market) -> bool:
        # Skip sports / MVE tickers
        if self.skip_prefixes and m.event_ticker.startswith(self.skip_prefixes):
            return False

        # Volume floor
        if m.volume_24h < self.min_volume:
            return False

        # Liquidity floor
        if m.liquidity < self.min_liquidity:
            return False

        # Expiry window (exclude expired and same-day markets)
        if m.days_to_expiry > self.max_days_to_expiry or m.days_to_expiry < 1:
            return False

        # Skip near-certain markets
        if m.yes_price < 0.05 or m.yes_price > 0.95:
            return False

        # Must have a valid price
        if m.yes_price == 0.0:
            return False

        return True

    # ------------------------------------------------------------------
    # Anomaly detection
    # ------------------------------------------------------------------

    def _check_anomalies(self, m: Market) -> None:
        reasons: list = []
        if m.price_move_24h > self.anomaly_price_move:
            reasons.append(f"price moved {m.price_move_24h:.0%} in 24h")
        if m.spread > self.anomaly_spread:
            reasons.append(f"wide spread ${m.spread:.2f}")

        # Volume spike: compare today's volume to 7-day rolling average
        avg_vol = self._get_7day_avg_volume(m.market_id)
        if avg_vol is not None and avg_vol > 0:
            spike_ratio = m.volume_24h / avg_vol
            m.volume_spike = round(spike_ratio, 2)
            if spike_ratio >= self.volume_spike_mult:
                reasons.append(
                    f"volume spike {spike_ratio:.1f}x vs 7d avg "
                    f"({m.volume_24h:.0f} vs {avg_vol:.0f})"
                )

        if reasons:
            m.is_anomaly = True
            m.anomaly_reasons = "; ".join(reasons)

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score(self, m: Market) -> float:
        score = 0.0

        # Volume (log scale, max 30 pts)
        if m.volume_24h > 0:
            score += min(30.0, math.log10(max(m.volume_24h, 1)) * 10)

        # Liquidity (max 20 pts)
        if m.liquidity > 0:
            score += min(20.0, m.liquidity / 50)

        # Tight spread bonus (max 20 pts)
        if 0 < m.spread < 0.10:
            score += (0.10 - m.spread) * 200

        # Expiry sweetspot (max 15 pts)
        if 7 <= m.days_to_expiry <= 21:
            score += 15
        elif m.days_to_expiry < 7:
            score += 10
        else:
            score += 5

        # Anomaly bonus
        if m.is_anomaly:
            score += 15

        # Failure log penalty: deprioritize markets with past losses.
        # -15 per Bad Prediction or External Shock (systematic issues),
        # -5 per Bad Timing (may be worth retrying with different conditions).
        # This doesn't hard-block — just pushes them down the ranked list.
        failures = self.past_failures.get(m.market_id, [])
        for f in failures:
            category = f.get("category", "")
            if category in ("Bad Prediction", "External Shock"):
                score -= 15
            elif category == "Bad Timing":
                score -= 5
            else:
                score -= 10

        return round(score, 2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_table(markets: List[Market], limit: int = 25) -> None:
    header = (
        f"{'#':>3}  {'Ticker':<35} {'Title':<50} "
        f"{'YES':>5} {'Vol24h':>8} {'Spread':>6} {'Days':>4} {'Score':>6} {'Anom':>4}"
    )
    print(header)
    print("-" * len(header))
    for i, m in enumerate(markets[:limit], 1):
        anom = " !" if m.is_anomaly else ""
        print(
            f"{i:3d}  {m.market_id[:35]:<35} {m.title[:50]:<50} "
            f"{m.yes_price:5.2f} {m.volume_24h:8.0f} {m.spread:6.3f} "
            f"{m.days_to_expiry:4d} {m.opportunity_score:6.1f}{anom}"
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    scanner = MarketScanner()
    result = scanner.scan_all()

    print(f"\nKalshi Market Scan")
    print(f"Scanned: {result.markets_scanned} | Passed: {result.markets_passed} "
          f"| Anomalies: {sum(1 for m in result.markets if m.is_anomaly)} "
          f"| Duration: {result.scan_duration_seconds}s\n")

    if result.markets:
        _print_table(result.markets)
    else:
        print("No markets passed filters.")

    fp = scanner.save_snapshot(result)
    print(f"\nSnapshot: {fp}")
