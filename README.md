# Options Setup Scanner Agent

A Python agent that scans a watchlist daily for high-conviction options setups and alerts you only when the LLM evaluator scores 7/10 or higher. Designed to fire 2–5 times per week, not constantly.

## What it does

1. **Pulls upcoming catalysts** (earnings, etc.) from Finnhub for your watchlist
2. **Filters to the 3–7 week window** — the sweet spot for catalyst-driven option trades
3. **Checks the option chain** — finds contracts at your target delta (0.28–0.40) with acceptable liquidity and premium
4. **Skips inflated IV** — won't alert when implied volatility is already pricing the move
5. **Sends candidates to Claude** for a 0–10 score and written thesis
6. **Alerts you via email + SMS + push** for setups scoring 7+

It does NOT auto-trade. You always make the buy decision. The agent is a discipline-enforcement tool, not a trading bot.

## Architecture

Two-file project:

- **`options_agent.py`** — main pipeline: catalysts → setup eval → LLM scoring → alerts
- **`polygon_data.py`** — all Polygon.io API calls (price, IV rank, option chain selection)

The split lets you swap data providers later (e.g., to ORATS or FlashAlpha) by only modifying `polygon_data.py`.

### Why Polygon over Tradier

Tradier requires opening a brokerage account just for API access. Polygon Options Starter ($29/mo) gives you:
- Full option chains with greeks
- Historical price data for IV rank calculation
- 5 calls/sec rate limit (plenty for daily scans)
- No brokerage signup, no funding requirements

If you outgrow Polygon Starter, drop-in alternatives (modify only `polygon_data.py`):
- **ORATS Data API** ($99/mo) — pre-computed IV rank, smoothed greeks
- **FlashAlpha** — purpose-built options analytics (GEX, vol surface)
- **MarketData.app** ($29–49/mo) — simpler indie alternative

## Setup (one-time, ~30 minutes)

### 1. Install dependencies
```bash
pip install requests anthropic sendgrid twilio
```

### 2. Get API keys (all free or low cost)

- **Finnhub** (earnings calendar) — free tier: https://finnhub.io/register
- **Polygon.io Options Starter** (option chains + greeks + IV history) — $29/mo, no brokerage required: https://polygon.io/pricing
- **Anthropic** (LLM evaluator) — pay-as-you-go: https://console.anthropic.com
- **SendGrid** (email) — free up to 100/day: https://sendgrid.com
- **Twilio** (SMS) — $1–5/mo at this volume: https://twilio.com
- **ntfy.sh** (push) — free, install the ntfy app on your phone: https://ntfy.sh

### 3. Set environment variables

Create `.env` or export directly:
```bash
export FINNHUB_API_KEY="..."
export POLYGON_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export SENDGRID_API_KEY="..."
export ALERT_EMAIL="you@example.com"
export TWILIO_ACCOUNT_SID="..."
export TWILIO_AUTH_TOKEN="..."
export TWILIO_FROM_NUMBER="+1..."
export TWILIO_TO_NUMBER="+1..."
export NTFY_TOPIC="your-secret-topic-name"
```

### 4. Edit the watchlist

In `options_agent.py`, modify `WATCHLIST` to match the tickers you actually want to track. Keep it focused (~30–50 names with liquid options). Adding more dilutes signal quality.

### 5. Tune the criteria

`CRITERIA` dict at the top lets you adjust:
- `catalyst_window_min/max_days` — the DTE sweet spot
- `target_delta_min/max` — how OTM you'll go
- `max_iv_rank` — how much IV inflation you'll tolerate
- `min_score_to_alert` — your alert threshold (raise to 8 if you want even fewer alerts)
- `max_premium_per_contract` — keeps trades sized for your $1k budget

## Running it

### Test first
```bash
python options_agent.py --dry-run         # don't send alerts
python options_agent.py --test NVDA       # scan a single ticker
```

### Daily run
```bash
python options_agent.py
```

### Deploy to schedule

**Option 1: Local cron (Mac/Linux)**
```cron
0 7 * * 1-5 cd /path/to/agent && /usr/bin/python3 options_agent.py >> agent.log 2>&1
```
Runs at 7am ET, weekdays.

**Option 2: Railway** (easiest cloud deploy, ~$5/mo)
1. Push code to GitHub
2. Connect Railway to repo
3. Add env vars in Railway dashboard
4. Set cron schedule in `railway.toml`:
   ```toml
   [[crons]]
   schedule = "0 11 * * 1-5"  # 11 UTC = 7am ET
   command = "python options_agent.py"
   ```

**Option 3: Fly.io** (~$3/mo) — similar to Railway, see fly.io/docs

## What "good" looks like

Realistic expectations after running this for 3 months:

- **Alerts per week:** 1–4. If it's firing more, raise `min_score_to_alert` to 8.
- **Win rate on alerts:** ~40–50% reach +50% profit. ~25–35% reach +100%. ~30–40% lose 50%+.
- **Expected EV:** slightly positive if you follow exit discipline. Negative if you hold losers hoping they come back.

## Tuning over time

Keep a trade journal. After each closed trade, log:
- Date / ticker / direction / score the agent gave it
- Entry / exit / P&L
- What worked / what didn't

After ~20 trades you'll see patterns: maybe scores 7 perform worse than scores 9, maybe puts work better than calls, maybe earnings setups underperform FDA setups. Tune `CRITERIA` accordingly.

## What's NOT included (intentionally)

- **Auto-execution** — you always click the buy yourself
- **Position management** — the agent fires the entry, doesn't manage the exit
- **Sentiment / news feeds** — adding these is a v2 feature
- **Backtesting** — backtests on options are notoriously misleading due to bid-ask, IV regimes, and survivorship. Forward-test small, real money.

## Cost ceiling

At the scan frequency described (~1 run/day, 30 tickers, ~10–30 LLM evals per run), total cost should land around **$40–55/month** including LLM API. Comfortably inside your $100 budget.

## When to turn it off

Turn the agent off entirely if:
- You're trading the alerts emotionally rather than following the framework
- You're losing more than you're comfortable with — there's no shame in stopping
- The market regime changes (e.g., sustained VIX > 30) and the IV criteria stop making sense

This tool helps disciplined traders. It can't make undisciplined trading profitable.
