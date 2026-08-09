# Binance Testnet Bot — Setup Guide

**Testnet only. No mainnet config exists here by design.**

---

## How this differs from the Saxo SIM setup

| Dimension | Saxo (existing) | Binance (this bot) |
|---|---|---|
| Auth model | OAuth2 24-hour token | HMAC-SHA256 API key + secret |
| Token refresh | Manual `python set_token.py` | Not needed — keys don't expire |
| SIM/testnet | Saxo SIM account (real login) | Binance testnet (GitHub SSO) |
| Market hours | Mon-Fri 09:30-16:00 ET | 24/7 |
| Quote currency | SEK | USDT |
| Config file | `config/capital.json` | `binance/config/binance_testnet_config.yaml` |
| Entry point | `atos_runner.py` | `bots/run_binance_bot.py` |
| Secrets file | `saxo_token.json` + `config/email.json` | `.env.binance` |

---

## 1. Get testnet API keys

1. Go to **https://testnet.binance.vision**
2. Click **Log In with GitHub** (no separate account needed)
3. Under **API Management**, click **Generate HMAC_SHA256 Key**
4. Copy both the **API Key** and the **Secret Key** — you only see the secret once

---

## 2. Configure credentials

Paste your keys into `.env.binance` at the project root:

```
BINANCE_TESTNET_API_KEY=<your testnet key here>
BINANCE_TESTNET_API_SECRET=<your testnet secret here>
BINANCE_TESTNET_BASE_URL=https://testnet.binance.vision/api
```

`.env.binance` is gitignored. It will never be committed.

---

## 3. Install dependencies

```bash
pip install python-binance python-dotenv pyyaml
```

These are separate from the Saxo bot's requirements. The main `requirements.txt`
has been updated to include them. If you're using a virtual env, activate it first.

---

## 4. Run the bot

```bash
# Dry run — scan for signals, print results, place no orders
python bots/run_binance_bot.py

# Place testnet orders when signals fire
python bots/run_binance_bot.py --execute

# Continuous loop (interval set in binance_testnet_config.yaml)
python bots/run_binance_bot.py --loop
python bots/run_binance_bot.py --loop --execute
```

---

## 5. Configure the strategy

Edit `binance/config/binance_testnet_config.yaml`:

- **symbols**: which pairs to scan (default: BTC, ETH, BNB, SOL, ADA — all USDT pairs)
- **strategy.rsi_oversold**: RSI threshold for entry (default: 35)
- **strategy.dip_threshold_pct**: how far below SMA-20 the price must be (default: 5%)
- **capital.max_slots**: max simultaneous positions (default: 3)
- **capital.position_size_pct**: size per position as % of free USDT (default: 25%)
- **execution.dry_run**: set to `false` to enable real testnet orders

---

## 6. Check logs

Logs go to `logs/binance_YYYY-MM-DD.log` (separate from the Saxo bot's `logs/atos_*.log`).
Errors also append to `logs/binance_errors.log`.

---

## What to watch for during testnet

- **Lot-size errors**: Binance enforces minimum quantity and step-size per symbol.
  The adapter handles this via `BinanceClient.format_qty()` but log output will
  show if a precision error was hit.
- **Testnet balance resets**: Binance resets testnet balances periodically (usually
  monthly). When this happens, the position count drops to 0 and a fresh balance
  appears. This is expected — not a bug.
- **WebSocket not used yet**: The current implementation polls REST endpoints.
  A WebSocket stream for live price updates is a future enhancement
  (see `docs/binance/api_reference_notes.md`).
