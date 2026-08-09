# Binance API — Reference Notes

All notes below apply to the **testnet** unless stated otherwise.
Testnet and mainnet share the same API surface but different base URLs.

---

## Auth model: HMAC-SHA256 (not OAuth2)

Binance uses **API key + secret** signed with HMAC-SHA256, which is completely
different from Saxo's OAuth2 24-hour token flow.

- Every signed endpoint requires: `timestamp` (Unix ms) + `signature` over the
  full query string / request body.
- The `python-binance` library handles signing automatically when instantiated
  with `testnet=True`.
- Keys do **not** expire — there is no token refresh. If you rotate keys on the
  testnet dashboard, update `.env.binance` and restart the bot.

---

## Base URLs

| Endpoint | Testnet URL |
|---|---|
| Spot REST | `https://testnet.binance.vision/api` |
| Spot WebSocket | `wss://testnet.binance.vision/ws` |
| Futures REST | `https://testnet.binancefuture.com/fapi` |
| Futures WebSocket | `wss://stream.binancefuture.com/ws` |

**Mainnet URLs are deliberately not documented here.** See `testnet_vs_live.md`
when the time comes to switch.

---

## Endpoints actually used

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v3/ticker/24hr` | 24h stats — price, volume |
| GET | `/api/v3/depth` | Order book (bid/ask) |
| GET | `/api/v3/klines` | OHLCV candles (used by strategy) |
| GET | `/api/v3/ticker/price` | Single-symbol last price |
| GET | `/api/v3/account` | Account info + all balances |
| GET | `/api/v3/openOrders` | Open orders (used for slot counting) |
| POST | `/api/v3/order` | Place market / limit order |
| DELETE | `/api/v3/order` | Cancel a single order |
| GET | `/api/v3/exchangeInfo` | Symbol filters (lot size, min notional) |

---

## Rate limits

| Limit | Testnet cap | Notes |
|---|---|---|
| Request weight | 1200 / 1 min | Each endpoint has a weight (usually 1-10) |
| Raw requests | 6000 / 5 min | Counts every HTTP request |
| Orders | 100 / 10 sec | Order placement only |

The bot scans 5 symbols and fetches ~3 endpoints each = ~15 requests per cycle.
Even at 1-minute intervals this is well within limits.

If you expand the universe to 50+ symbols and reduce the scan interval below
5 minutes, add request-weight tracking (python-binance exposes response headers).

---

## Symbol filters (exchange rules)

Every symbol has filters enforced at order time. Key ones:

- **LOT_SIZE** — min quantity, max quantity, step size (e.g. BTC step = 0.00001)
- **MIN_NOTIONAL** — minimum order value in USDT (usually 5-10 USDT on testnet)
- **PRICE_FILTER** — tickSize for limit order prices

`BinanceClient.format_qty()` handles LOT_SIZE rounding automatically.
`BinanceAdapter._price_precision()` handles PRICE_FILTER for limit orders.

---

## Order types used

| Type | When |
|---|---|
| MARKET | Default — fills immediately at best available price |
| LIMIT GTC | Optional — set `order_type: LIMIT` in config |

For a mean-reversion strategy, MARKET is preferred for entries (speed matters
when RSI recovers quickly). LIMIT is appropriate for exits at a target price.

---

## WebSocket (not yet wired — future)

Live tick streaming via WebSocket would allow real-time RSI updates without
polling the REST klines endpoint. When you add this:

- Use `BinanceSocketManager` from `python-binance`
- Stream: `wss://testnet.binance.vision/ws/<symbol>@kline_1d`
- Run in a background thread; update a shared price cache that the strategy reads
- See: https://python-binance.readthedocs.io/en/latest/websockets.html

---

## Known testnet quirks

1. **Balance resets**: Testnet balances reset periodically without notice. When
   total USDT drops to the initial grant and all positions disappear, that is a
   reset — not a code bug. Restart the bot fresh.

2. **Thin order book**: Testnet has very few participants so bid/ask spreads can
   be wide. Market order fills may look odd. This does NOT affect signal logic
   (which uses daily OHLCV) but does affect the recorded fill price.

3. **Delayed klines**: Testnet candles occasionally lag mainnet by a few minutes.
   For a daily-bar strategy this is irrelevant.

4. **`exchangeInfo` parity**: Symbol filters on testnet mirror mainnet closely but
   may diverge on new listings. Treat testnet filter values as directionally correct.
