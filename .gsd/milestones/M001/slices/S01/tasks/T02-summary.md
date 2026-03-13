---
id: T02
parent: S01
milestone: M001
provides:
  - MarketScanner class with scan_all() returning ScanResult with ranked Market list
  - Market dataclass with all Kalshi fields (dollar-string parsed to float)
  - ScanResult dataclass with timestamp, counts, and market list
  - Cursor-paginated Kalshi events API fetching (not /markets — events has volume data)
  - JSON snapshot persistence to data/market_snapshots/
requires:
  - slice: S01/T01
    provides: config/__init__.py, settings.yaml, path constants
affects: [S02, S03, S04, S05]
key_files:
  - scripts/scanner.py
key_decisions:
  - "Use /events?with_nested_markets=true instead of /markets — the /markets list endpoint returns zero volume for all markets"
  - "Category from event metadata, not ticker prefix — more accurate"
  - "Default min_volume lowered to 20 — Kalshi non-sports markets have lower volume than Polymarket"
patterns_established:
  - "Dollar-string parsing via _safe_float() static method"
  - "sys.path.insert(0, parent) for running scripts directly and as imports"
  - "CLI with formatted table output + JSON snapshot save"
drill_down_paths:
  - .gsd/milestones/M001/slices/S01/tasks/T02-plan.md
duration: 12min
verification_result: pass
completed_at: 2026-03-13T10:41:00Z
---

# T02: Kalshi API client and market scanner

**Live Kalshi scanner pulling 4000+ markets via events API, filtering to tradeable non-sports markets with volume/expiry/price checks and opportunity scoring**

## What Happened

Built `scripts/scanner.py` with `MarketScanner` class that connects to the Kalshi events API (`/events?with_nested_markets=true`). Key discovery: the plain `/markets` list endpoint returns `volume_24h_fp: 0.00` for all markets — only the events endpoint with nested markets populates volume and price fields. This was caught during verification and the implementation was switched to events-based fetching.

The scanner fetches up to 10 pages of events (4000+ markets), parses dollar-string fields to floats, filters by volume (>20), expiry (<30 days), price (5-95%), and category (skips MVE/NBA/NFL/MLB/NHL). Anomaly detection flags price moves >10% and wide spreads >$0.05. Opportunity scoring combines volume (log scale), liquidity, spread tightness, expiry sweetspot, and anomaly bonus.

Verified with live API: 4458 markets scanned, 4 passed filters (politics, entertainment, legal categories), 1 anomaly flagged, JSON snapshot saved.

## Deviations
- Used `/events` endpoint instead of `/markets` — the plan assumed `/markets` would have volume data, but it doesn't
- Lowered `min_volume` default from 50 to 20 in settings.yaml — Kalshi non-sports markets have lower volume

## Files Created/Modified
- `scripts/scanner.py` — MarketScanner class + Market/ScanResult dataclasses + CLI (310 lines)
- `config/settings.yaml` — min_volume adjusted to 20
