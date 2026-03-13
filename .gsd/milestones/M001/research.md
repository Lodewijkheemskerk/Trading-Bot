# M001: AI-Powered Prediction Market Trading Bot — Research

**Researched:** 2026-03-13
**Domain:** Prediction markets, Kalshi API, trading bot architecture
**Confidence:** HIGH

## Summary

Kalshi's REST API v2 is fully accessible from the Netherlands without authentication for all read endpoints (markets, orderbook, events, series). The API returns rich market data including dollar-denominated prices, volume, liquidity, open interest, bid/ask spreads, and expiration times. Authentication (RSA key-based, PSS-SHA256 signing) is only needed for trading operations (place/cancel orders, portfolio balance).

The official `kalshi-python` SDK (v2.1.4) handles authentication natively. For paper trading mode, we only need unauthenticated read access. The demo API at `demo-api.kalshi.co` also works from NL and mirrors production.

Key architectural finding: Kalshi's API rate limits make high-frequency strategies impractical (50-200ms latency per REST call). Our 15-minute cycle pipeline is well-suited. The API returns prices in dollar strings (e.g., `"0.4500"`) not percentages — need to parse as float.

## Verified API Access (tested 2026-03-13 from NL)

| Endpoint | Auth Required | Status | Notes |
|----------|--------------|--------|-------|
| `GET /trade-api/v2/markets` | No | ✅ Works | Returns cursor-paginated market list |
| `GET /trade-api/v2/markets/{ticker}/orderbook` | No | ✅ Works | Returns `orderbook_fp` with bid/ask levels |
| `GET /trade-api/v2/events` | No | ✅ Works | Returns events + milestones |
| `GET /trade-api/v2/series/{ticker}` | No | ✅ Works | Returns series metadata |
| `POST /trade-api/v2/portfolio/orders` | Yes (RSA) | N/A | Not needed for paper trading |
| `GET /trade-api/v2/portfolio/balance` | Yes (RSA) | N/A | Not needed for paper trading |

**Base URLs:**
- Production: `https://api.elections.kalshi.com/trade-api/v2`
- Demo: `https://demo-api.kalshi.co/trade-api/v2`

## Key Market Data Fields

From a real Kalshi market object:
- `ticker` — Unique market ID (e.g., `KXBTCD-26MAR14-B97500`)
- `title` — Human-readable name
- `status` — `active`, `closed`, `settled`
- `yes_ask_dollars` / `yes_bid_dollars` — Current YES prices as dollar strings
- `no_ask_dollars` / `no_bid_dollars` — Current NO prices
- `volume_24h_fp` — 24h volume as float string
- `volume_fp` — Total volume
- `liquidity_dollars` — Orderbook depth
- `open_interest_fp` — Open interest
- `close_time` / `expiration_time` — Resolution timestamps
- `event_ticker` — Parent event grouping
- `market_type` — `binary` (Yes/No)
- `last_price_dollars` — Last traded price

**Parsing note:** All `_dollars` and `_fp` fields are strings, not floats. Must `float()` them.

## Kalshi Authentication (for future live trading)

Uses RSA-PSS with SHA-256 signing:
1. Generate RSA key pair, upload public key to Kalshi dashboard
2. Sign `{timestamp}{method}{path}` with PSS padding
3. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`

The `kalshi-python` SDK handles this automatically with `configuration.api_key_id` and `configuration.private_key_pem`.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Kalshi API auth signing | Custom RSA signing code | `kalshi-python` SDK v2.1.4 | Handles PSS-SHA256, timestamps, headers automatically |
| HTTP request retries | Manual retry loops | `requests` + `urllib3.util.retry.Retry` | Exponential backoff, connection pooling built-in |
| RSS feed parsing | Regex on XML | `xml.etree.ElementTree` (stdlib) | Already in Python stdlib, handles encoding |
| Reddit API | OAuth flow | Public JSON API (`/search.json`) | No auth needed for public search, just add `.json` suffix |
| Date/time handling | Manual string parsing | `datetime.fromisoformat()` | Handles Kalshi's ISO format natively |
| Config management | .env only | `pyyaml` + `.env` for secrets | YAML for structure, .env for secrets only |

## Common Pitfalls

### Pitfall 1: Kalshi Price Format
**What goes wrong:** Treating `yes_ask_dollars` as a float when it's a string `"0.4500"`, or as cents when it's dollars.
**Why it happens:** API returns prices as dollar-denominated strings, not integers or floats.
**How to avoid:** Always `float(market['yes_ask_dollars'])`. The value IS in dollars (0.00-1.00 range for binary markets).
**Warning signs:** Prices > 1.0 or negative — means you're parsing wrong.

### Pitfall 2: Markets With Zero Volume
**What goes wrong:** Bot tries to trade illiquid markets, can't get filled.
**Why it happens:** Kalshi has hundreds of markets but many have zero volume and no orderbook.
**How to avoid:** Filter `float(volume_24h_fp) > 0` AND `float(liquidity_dollars) > 0` before considering.
**Warning signs:** `yes_ask_dollars == "0.0000"` means no active orders exist.

### Pitfall 3: Cursor Pagination
**What goes wrong:** Only getting first 100 markets, missing the rest.
**Why it happens:** Kalshi API uses cursor-based pagination, not page numbers.
**How to avoid:** Loop with `cursor` parameter from response until no more results.
**Warning signs:** Response always has exactly `limit` items.

### Pitfall 4: Sports/MVE Markets Dominating
**What goes wrong:** Scanner returns mostly sports parlays and multivariate events, not the policy/economics markets where AI prediction has an edge.
**Why it happens:** Kalshi has massive sports market volume. MVE (multivariate) markets have complex multi-leg structures.
**How to avoid:** Filter by `event_ticker` prefix or category. Focus on categories: Politics, Economics, Climate, Technology. Skip `KXMVE*` tickers.
**Warning signs:** Tickers containing `MVE`, `GAME`, `SPREAD` are sports.

### Pitfall 5: Prompt Injection via News Content
**What goes wrong:** Malicious content in scraped news/Reddit could manipulate the AI's prediction.
**Why it happens:** LLMs can be influenced by instruction-like text in their input.
**How to avoid:** Sanitize all external content. Strip instruction-like patterns. Always frame scraped content as "DATA:" in prompts. Never pass raw text as system instructions.
**Warning signs:** Predictions that dramatically change after adding a single news source.

## Relevant Code

No existing codebase — greenfield project.

**Key libraries to install:**
- `requests` — HTTP client for Kalshi API
- `pyyaml` — Config management
- `python-dotenv` — Secret management
- `kalshi-python` — Official Kalshi SDK (for future live trading)

**Optional:**
- `anthropic` — Claude API SDK
- `numpy` / `pandas` — Data analysis (future)

## Market Categories on Kalshi (verified)

| Category | Example | AI Prediction Edge |
|----------|---------|-------------------|
| Politics | "Who will the next Pope be?" | Medium — public info + reasoning |
| Economics | "Will Fed raise rates?" | High — data-driven, analyzable |
| Climate/Weather | "2°C warming threshold?" | Low-Medium — long timeframes |
| Science/Technology | "Humans on Mars before 2050?" | Low — very long timeframes |
| Sports | NBA game outcomes | Low — efficient market, too fast |
| Crypto | "Bitcoin > $100K by March?" | Medium — volatile but analyzable |

**Recommendation:** Focus scanner on Politics, Economics, and Crypto categories. Skip Sports (too efficient, dominated by sharp bettors) and very-long-dated markets (>30 days).

## Sources
- Context7: /websites/kalshi — API docs, Python SDK examples, authentication (HIGH confidence)
- Live API testing: All 4 read endpoints verified working from NL (HIGH confidence)
- Kalshi market data: Real market objects inspected for field names (HIGH confidence)
