# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Options Setup Scanner Agent — scans a watchlist daily for high-conviction options setups (catalyst-driven, 3–7 week window), scores them via Claude LLM, and alerts via email/SMS/push for scores 7+. Not a trading bot — no auto-execution.

## Running

```bash
pip install requests anthropic sendgrid twilio   # one-time

python options_agent.py                          # full scan
python options_agent.py --dry-run                # print only, no alerts
python options_agent.py --test NVDA              # single-ticker test
python polygon_data.py                           # self-test Polygon API calls
```

Requires env vars: `FINNHUB_API_KEY`, `POLYGON_API_KEY`, `ANTHROPIC_API_KEY`. Optional: `SENDGRID_API_KEY`, `ALERT_EMAIL`, `TWILIO_*`, `NTFY_TOPIC`.

## Architecture

Two-file pipeline, split so the data provider is swappable:

- **`options_agent.py`** — main pipeline with 4 layers:
  1. **Catalyst feed** (`fetch_earnings_catalysts`) — Finnhub API, filters to `CRITERIA` window
  2. **Option chain + IV rank** — delegates to `polygon_data.py` via three imported functions
  3. **LLM evaluator** (`evaluate_setup`) — Claude scores 0–10 with JSON-only response, uses `claude-sonnet-4-5`
  4. **Notifications** (`dispatch_alert`) — email (SendGrid), SMS (Twilio), push (ntfy.sh)

- **`polygon_data.py`** — all Polygon.io API calls, three exported functions:
  - `get_underlying_price(ticker)` — last trade, falls back to previous close
  - `get_iv_rank(ticker)` — approximated via rolling 30-day realized vol (scaled 1.15x) as proxy since Polygon Starter lacks direct IV rank
  - `find_target_contract(ticker, direction, dte_target)` — paginated snapshot search, filters by delta (0.28–0.40), OI (500+), spread (<5%), sorts by closeness to delta 0.34

## Key Configuration

Both in `options_agent.py`:
- `WATCHLIST` list — tickers to scan (~30–50 recommended)
- `CRITERIA` dict — catalyst window, delta range, IV rank cap, OI minimum, spread limit, score threshold, max premium

## Data Provider Swap

To switch from Polygon to another provider (ORATS, FlashAlpha, MarketData.app), only modify `polygon_data.py`. Keep the same three function signatures. The agent imports only `get_underlying_price`, `get_iv_rank`, `find_target_contract`.

## LLM Integration

`evaluate_setup` sends a structured prompt expecting JSON-only response with `score`, `thesis`, `key_risk`, `profit_target_pct`, `stop_level_underlying`. Code-fence stripping is handled. Failed evals default to score 0.

## Deployment

Designed for weekday cron at 7am ET pre-market: `0 7 * * 1-5`. Also deployable on Railway or Fly.io. Expected cost ~$40–55/mo total including Polygon ($29) and LLM API.
