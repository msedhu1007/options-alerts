"""
OPTIONS SETUP SCANNER AGENT
============================
Scans a watchlist for high-conviction options setups based on:
  - Upcoming catalyst (earnings, FDA, Fed, product launches) in 3-6 week window
  - Reasonable IV environment (IV rank < 50)
  - Liquid option chains
  - Technical setup confirming direction

Sends alerts ONLY for setups scoring >= 7/10 from LLM evaluation.
Designed to fire 2-5 times per week, not every day.

USAGE:
    python options_agent.py            # run once
    python options_agent.py --dry-run  # don't send alerts, just print
    python options_agent.py --test     # use a single test ticker

DEPLOYMENT:
    Schedule via cron: 0 7 * * 1-5 (weekdays at 7am ET, before market open)
    Or use Railway/Fly.io scheduled jobs.
"""

import os
import json
import argparse
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

import requests  # pip install requests
import anthropic  # pip install anthropic

from dotenv import load_dotenv
load_dotenv() 

# Polygon data module — replace the Tradier-based functions below
from polygon_data import get_underlying_price, get_iv_rank, find_target_contract

# Optional notification libs (install only what you'll use):
# from sendgrid import SendGridAPIClient                          # pip install sendgrid
# from sendgrid.helpers.mail import Mail
# from twilio.rest import Client as TwilioClient                  # pip install twilio


# ============================================================================
# CONFIGURATION — edit these for your setup
# ============================================================================

# Your watchlist — keep it focused. ~30-50 liquid mid-to-large caps with active options.
# These are the only names the agent will scan.
WATCHLIST = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "ORCL", "NFLX",
    # Semis
    "AMD", "AVGO", "MU", "INTC", "QCOM", "TXN", "MRVL", "AMAT", "SNDK",
    # Enterprise SaaS / cloud
    "CRM", "NOW", "DDOG", "NET", "PLTR", "SHOP", "MNDY", "CRWV",
    # Cybersecurity
    "CRWD", "PANW",
    # Networking / infra
    "ANET", "CSCO", "DELL",
    # Aerospace / defense / space
    "RKLB",
    # Mid-cap growth
    "TOST", "CELH", "VRT",
    # Retail / consumer
    "WMT", "COST", "DECK", "ELF", "WING", "CHWY", "ROKU",
    # Financials / crypto
    "SCHW", "COIN", "HOOD",
    # Storage
    "WDC", "STX",
    # Healthcare / biotech
    "LLY", "MRNA", "VRTX", "ISRG",
    # Index ETFs for hedge / macro plays
    "SPY", "QQQ", "IWM",
]

# Setup criteria — tune these to your risk preferences
CRITERIA = {
    "catalyst_window_min_days": 21,   # 3 weeks out
    "catalyst_window_max_days": 49,   # 7 weeks out
    "target_delta_min": 0.28,
    "target_delta_max": 0.40,
    "max_iv_rank": 40,                # skip if IV already inflated; ≤30 is ideal
    "min_open_interest": 500,
    "max_bid_ask_spread_pct": 0.05,   # 5% of mid price
    "min_score_to_alert": 7,          # 0-10 scale from LLM
    "max_premium_per_contract": 1500, # keeps $1k-per-trade plans viable
    "max_theta_pct_of_premium": 0.02, # daily theta ≤ 2% of premium
}

# API keys — set as environment variables
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Notification config
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "you@example.com")
SENDGRID_KEY = os.environ.get("SENDGRID_API_KEY")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER")
TWILIO_TO = os.environ.get("TWILIO_TO_NUMBER")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # e.g. "your-secret-topic-xyz"


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Catalyst:
    ticker: str
    event_type: str          # "earnings", "fda", "fed", "product"
    event_date: str          # YYYY-MM-DD
    days_until: int
    description: str

@dataclass
class OptionSetup:
    ticker: str
    direction: str           # "call" or "put"
    strike: float
    expiry: str
    premium_mid: float
    delta: float
    iv: float
    iv_rank: float
    theta: float
    theta_pct: Optional[float]
    open_interest: int
    bid_ask_spread_pct: float
    underlying_price: float
    catalyst: Catalyst
    score: Optional[int] = None
    thesis: Optional[str] = None


# ============================================================================
# LAYER 1 — CATALYST FEED (Finnhub free tier)
# ============================================================================

def fetch_earnings_catalysts(tickers: list, days_ahead: int = 49) -> list[Catalyst]:
    """Pull upcoming earnings dates for watchlist from Finnhub."""
    catalysts = []
    today = datetime.now().date()
    end = today + timedelta(days=days_ahead)

    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {
        "from": today.isoformat(),
        "to": end.isoformat(),
        "token": FINNHUB_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("earningsCalendar", [])
    except Exception as e:
        print(f"[ERROR] Finnhub earnings fetch failed: {e}")
        return []

    for event in data:
        ticker = event.get("symbol")
        if ticker not in tickers:
            continue
        event_date = event.get("date")
        try:
            d = datetime.strptime(event_date, "%Y-%m-%d").date()
        except Exception:
            continue
        days_until = (d - today).days
        if not (CRITERIA["catalyst_window_min_days"] <= days_until <= CRITERIA["catalyst_window_max_days"]):
            continue
        catalysts.append(Catalyst(
            ticker=ticker,
            event_type="earnings",
            event_date=event_date,
            days_until=days_until,
            description=f"Q{event.get('quarter', '?')} earnings — EPS est: ${event.get('epsEstimate', 'n/a')}",
        ))
    return catalysts


# ============================================================================
# LAYER 2 — OPTION CHAIN + IV RANK
# ============================================================================
# Functions get_underlying_price, get_iv_rank, and find_target_contract
# are imported from polygon_data.py at the top of this file.
# That module uses Polygon.io Options Starter ($29/mo, no brokerage account required).


# ============================================================================
# LAYER 3 — LLM EVALUATOR (Claude scores the setup + writes thesis)
# ============================================================================

EVALUATOR_PROMPT = """You are an options trading analyst evaluating a candidate setup.
Be SKEPTICAL by default. Only score 7+ for genuinely high-conviction setups.

The setup:
- Ticker: {ticker}
- Direction: {direction}
- Strike: ${strike}
- Expiry: {expiry} ({dte} DTE)
- Premium mid: ${premium:.2f}
- Delta: {delta:.2f}
- IV: {iv:.1f}% (IV rank: {iv_rank:.0f}) — NOTE: IV rank ≤30 is cheap (good for buying), 30-40 is fair, >40 is elevated
- Theta: ${theta}/day ({theta_pct_display} of premium daily)
- Underlying price: ${underlying:.2f}
- Catalyst: {catalyst_desc} on {catalyst_date} ({days_to_catalyst} days away)

Evaluate on these dimensions:
1. Is the catalyst genuinely likely to move the stock 8%+ in the expected direction?
2. Is IV cheap enough to justify buying? Low IV rank (≤30) is a strong positive — options are underpriced. Score higher when IV rank is low.
3. Is theta decay manageable relative to expected holding period and catalyst timing?
4. Is the strike well-placed (room to run, not too far OTM)?
5. Is there a clear thesis or just a vague hope?
6. Are there obvious risks that argue against this trade?

Return ONLY a JSON object, no other text:
{{
  "score": <integer 0-10>,
  "thesis": "<2-3 sentence thesis if score >=7, else why you'd skip>",
  "key_risk": "<single biggest risk in one sentence>",
  "profit_target_pct": <integer, % gain at which to take profit, typically 80-150>,
  "stop_level_underlying": <float, price of underlying at which to exit>
}}
"""

def evaluate_setup(setup: OptionSetup) -> OptionSetup:
    """Use Claude to score and write a thesis for the candidate setup."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    dte = (datetime.strptime(setup.expiry, "%Y-%m-%d").date() - datetime.now().date()).days
    days_to_catalyst = setup.catalyst.days_until

    prompt = EVALUATOR_PROMPT.format(
        ticker=setup.ticker,
        direction=setup.direction,
        strike=setup.strike,
        expiry=setup.expiry,
        dte=dte,
        premium=setup.premium_mid,
        delta=setup.delta,
        iv=setup.iv * 100 if setup.iv < 1 else setup.iv,
        iv_rank=setup.iv_rank,
        theta=abs(setup.theta) if hasattr(setup, 'theta') and setup.theta else 0,
        theta_pct_display=f"{setup.theta_pct*100:.1f}%" if hasattr(setup, 'theta_pct') and setup.theta_pct else "N/A",
        underlying=setup.underlying_price,
        catalyst_desc=setup.catalyst.description,
        catalyst_date=setup.catalyst.event_date,
        days_to_catalyst=days_to_catalyst,
    )

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5",   # cheaper than Opus for repeated calls
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        setup.score = int(result["score"])
        setup.thesis = (
            f"{result['thesis']}\n"
            f"Key risk: {result['key_risk']}\n"
            f"Profit target: +{result['profit_target_pct']}% | "
            f"Stop: {setup.ticker} below ${result['stop_level_underlying']}"
        )
    except Exception as e:
        print(f"[ERROR] LLM eval failed for {setup.ticker}: {e}")
        setup.score = 0
        setup.thesis = "Evaluation failed."
    return setup


# ============================================================================
# LAYER 4 — NOTIFICATIONS
# ============================================================================

def format_alert(setup: OptionSetup) -> str:
    """Build the alert body. Same content for email, SMS, push."""
    dte = (datetime.strptime(setup.expiry, "%Y-%m-%d").date() - datetime.now().date()).days
    direction_word = "CALL" if setup.direction == "call" else "PUT"
    return (
        f"[{setup.score}/10] {setup.ticker} {direction_word} setup\n"
        f"Buy: {setup.ticker} {setup.expiry} ${setup.strike} {direction_word}\n"
        f"Premium ~${setup.premium_mid:.2f} | Delta {setup.delta:.2f} | "
        f"IV rank {setup.iv_rank:.0f} | DTE {dte}\n"
        f"Catalyst: {setup.catalyst.description} ({setup.catalyst.event_date})\n\n"
        f"{setup.thesis}"
    )


def send_email(subject: str, body: str):
    if not SENDGRID_KEY:
        return
    # from sendgrid import SendGridAPIClient
    # from sendgrid.helpers.mail import Mail
    # mail = Mail(from_email="alerts@yourdomain.com", to_emails=ALERT_EMAIL,
    #             subject=subject, plain_text_content=body)
    # SendGridAPIClient(SENDGRID_KEY).send(mail)
    print(f"[EMAIL] {subject}\n{body}\n")  # placeholder


def send_sms(body: str):
    if not (TWILIO_SID and TWILIO_TOKEN):
        return
    # from twilio.rest import Client as TwilioClient
    # TwilioClient(TWILIO_SID, TWILIO_TOKEN).messages.create(
    #     body=body[:1500], from_=TWILIO_FROM, to=TWILIO_TO)
    print(f"[SMS] {body[:160]}...\n")


def send_push(title: str, body: str):
    """Free push via ntfy.sh — install ntfy app, subscribe to your topic."""
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "chart_with_upwards_trend"},
            timeout=5,
        )
    except Exception as e:
        print(f"[ERROR] Push failed: {e}")


def dispatch_alert(setup: OptionSetup, dry_run: bool = False):
    body = format_alert(setup)
    subject = f"Options setup: {setup.ticker} {setup.direction.upper()} ({setup.score}/10)"
    if dry_run:
        print(f"\n{'='*60}\n{subject}\n{'='*60}\n{body}\n")
        return
    send_email(subject, body)
    send_sms(body)
    send_push(subject, body)


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def run(dry_run: bool = False, test_ticker: Optional[str] = None):
    print(f"[{datetime.now().isoformat()}] Starting scan...")

    tickers = [test_ticker] if test_ticker else WATCHLIST

    # 1. Get catalysts in the sweet-spot window
    catalysts = fetch_earnings_catalysts(tickers)
    print(f"Found {len(catalysts)} catalysts in {CRITERIA['catalyst_window_min_days']}-"
          f"{CRITERIA['catalyst_window_max_days']} day window")

    qualified = []
    for cat in catalysts:
        underlying = get_underlying_price(cat.ticker)
        if not underlying:
            continue
        iv_rank = get_iv_rank(cat.ticker)
        if iv_rank is None or iv_rank > CRITERIA["max_iv_rank"]:
            print(f"  {cat.ticker}: skipped (IV rank {iv_rank})")
            continue

        # For pre-earnings setups, try BOTH directions and let the LLM pick the better thesis.
        # In production, add a directional bias signal (e.g., trend filter, sentiment).
        for direction in ("call", "put"):
            dte_target = cat.days_until + 21  # ~3 weeks after catalyst for follow-through
            contract = find_target_contract(cat.ticker, direction, dte_target)
            if not contract:
                continue
            setup = OptionSetup(
                ticker=cat.ticker,
                direction=direction,
                strike=contract["strike"],
                expiry=contract["expiry"],
                premium_mid=contract["mid"],
                delta=contract["delta"],
                iv=contract["iv"],
                iv_rank=iv_rank,
                theta=contract.get("theta", 0),
                theta_pct=contract.get("theta_pct"),
                open_interest=contract["open_interest"],
                bid_ask_spread_pct=contract.get("spread_pct"),
                underlying_price=underlying,
                catalyst=cat,
            )
            setup = evaluate_setup(setup)
            print(f"  {cat.ticker} {direction}: scored {setup.score}/10")
            if setup.score and setup.score >= CRITERIA["min_score_to_alert"]:
                qualified.append(setup)

    print(f"\n{len(qualified)} setup(s) qualified for alert")
    for setup in qualified:
        dispatch_alert(setup, dry_run=dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Don't send alerts, just print")
    parser.add_argument("--test", type=str, help="Test with a single ticker")
    args = parser.parse_args()
    run(dry_run=args.dry_run, test_ticker=args.test)
