# Kalshi API Reference

## Base URLs
- **Production:** `https://api.elections.kalshi.com/trade-api/v2`
- **Demo:** `https://demo-api.kalshi.co/trade-api/v2`

## Public Endpoints (no auth required)

```
GET /markets                        # List markets (cursor-paginated)
GET /markets?status=open&limit=100  # Filter by status
GET /markets/{ticker}               # Single market details
GET /markets/{ticker}/orderbook     # Orderbook with bid/ask levels
GET /events                         # List events (groups of markets)
GET /events?status=open&limit=100   # Active events
GET /series/{series_ticker}         # Series metadata
```

## Authenticated Endpoints (RSA key required)

```
POST   /portfolio/orders            # Place order
DELETE /portfolio/orders/{order_id}  # Cancel order
GET    /portfolio/orders             # List your orders
GET    /portfolio/positions          # Your positions
GET    /portfolio/balance            # Account balance
```

## Authentication

RSA-PSS with SHA-256:
1. Generate RSA key pair, upload public key to Kalshi dashboard
2. Message to sign: `{timestamp_ms}{METHOD}{path_without_query}`
3. Sign with PSS padding, MGF1(SHA256), base64-encode

Headers:
```
KALSHI-ACCESS-KEY: <api_key_id>
KALSHI-ACCESS-SIGNATURE: <base64_signature>
KALSHI-ACCESS-TIMESTAMP: <unix_ms_string>
```

## Key Market Fields

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | str | Unique market ID |
| `title` | str | Human-readable name |
| `status` | str | `active`, `closed`, `settled` |
| `yes_ask_dollars` | str | YES ask price as dollar string (e.g., `"0.45"`) |
| `yes_bid_dollars` | str | YES bid price |
| `no_ask_dollars` | str | NO ask price |
| `no_bid_dollars` | str | NO bid price |
| `last_price_dollars` | str | Last traded price |
| `volume_24h_fp` | str | 24h volume (float string) |
| `volume_fp` | str | Total volume |
| `liquidity_dollars` | str | Orderbook depth |
| `open_interest_fp` | str | Open interest |
| `close_time` | str | ISO datetime — market close |
| `expiration_time` | str | ISO datetime — settlement |
| `event_ticker` | str | Parent event grouping |
| `market_type` | str | `binary` |

**⚠️ All `_dollars` and `_fp` fields are strings. Parse with `float()`.**

## Pagination

Responses include a `cursor` field. Pass `?cursor=<value>` for the next page.
Loop until `cursor` is empty or `null`.

## Python SDK

```python
import kalshi_python

config = kalshi_python.Configuration(
    host="https://api.elections.kalshi.com/trade-api/v2"
)
config.api_key_id = "your-key-id"
config.private_key_pem = open("key.pem").read()

client = kalshi_python.KalshiClient(config)
market = client.get_market("KXBTCD-26MAR14-B97500")
```
