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
from polygon_data import get_underlying_price, get_iv_rank, get_trend_bias, find_target_contract

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
    "CRM", "NOW", "DDOG", "NET", "PLTR", "SHOP", "MNDY", "CRWV","ADBE",
    # Cybersecurity
    "CRWD", "PANW",
    # Networking / infra
    "ANET", "CSCO", "DELL",
    # Aerospace / defense / space
    "RKLB",
    # Mid-cap growth
    "TOST", "CELH", "VRT",
    # Retail / consumer
    "WMT", "COST", "TGT", "CHWY", 
    # Financials / crypto
    "SCHW", "COIN", "HOOD",
    # Storage
    "WDC", "STX","SNDK",
    # Healthcare / biotech
    "LLY", "MRNA", "VRTX", "ISRG",
    # Index ETFs for hedge / macro plays
    "SPY", "QQQ",
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
    trend_summary: str = ""
    score: Optional[int] = None
    thesis: Optional[str] = None


# ============================================================================
# LAYER 1 — CATALYST FEED
# ============================================================================

# FOMC meeting dates — updated annually from federalreserve.gov
FOMC_DATES_2025 = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
]
FOMC_DATES_2026 = [
    "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
FOMC_DATES = FOMC_DATES_2025 + FOMC_DATES_2026

# Macro-sensitive tickers — get FOMC/econ catalysts assigned
MACRO_SENSITIVE = {
    "SPY", "QQQ",                   # index ETFs
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",  # mega-cap
    "SCHW", "COIN", "HOOD",               # financials
}

# High-impact economic events worth trading around
HIGH_IMPACT_ECON = {
    "CPI", "Consumer Price Index",
    "Nonfarm Payrolls", "Non-Farm Payrolls", "NFP",
    "GDP", "Gross Domestic Product",
    "PCE", "Personal Consumption Expenditures",
    "PPI", "Producer Price Index",
    "FOMC",  # sometimes shows up in Finnhub econ calendar too
    "Retail Sales",
    "Initial Jobless Claims",
}


def _fetch_earnings(tickers: list, days_ahead: int) -> list[Catalyst]:
    """Pull upcoming earnings dates from Finnhub."""
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


def _fetch_fomc(tickers: list, days_ahead: int) -> list[Catalyst]:
    """Generate FOMC catalysts for macro-sensitive tickers."""
    catalysts = []
    today = datetime.now().date()
    watchlist_macro = [t for t in tickers if t in MACRO_SENSITIVE]

    if not watchlist_macro:
        return []

    for date_str in FOMC_DATES:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        days_until = (d - today).days
        if not (CRITERIA["catalyst_window_min_days"] <= days_until <= CRITERIA["catalyst_window_max_days"]):
            continue
        for ticker in watchlist_macro:
            catalysts.append(Catalyst(
                ticker=ticker,
                event_type="fed",
                event_date=date_str,
                days_until=days_until,
                description="FOMC rate decision",
            ))
    return catalysts


def _fetch_economic_calendar(tickers: list, days_ahead: int) -> list[Catalyst]:
    """Pull high-impact US economic events from Finnhub."""
    if not FINNHUB_KEY:
        return []

    catalysts = []
    today = datetime.now().date()
    end = today + timedelta(days=days_ahead)
    watchlist_macro = [t for t in tickers if t in MACRO_SENSITIVE]

    if not watchlist_macro:
        return []

    url = "https://finnhub.io/api/v1/calendar/economic"
    params = {
        "from": today.isoformat(),
        "to": end.isoformat(),
        "token": FINNHUB_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("economicCalendar", [])
    except Exception as e:
        print(f"[ERROR] Finnhub econ calendar failed: {e}")
        return []

    seen_events = set()
    for event in data:
        country = event.get("country", "")
        if country != "US":
            continue

        event_name = event.get("event", "")
        if not any(keyword in event_name for keyword in HIGH_IMPACT_ECON):
            continue

        event_date = event.get("date", "")[:10]
        try:
            d = datetime.strptime(event_date, "%Y-%m-%d").date()
        except Exception:
            continue

        days_until = (d - today).days
        if not (CRITERIA["catalyst_window_min_days"] <= days_until <= CRITERIA["catalyst_window_max_days"]):
            continue

        # Dedupe: one catalyst per event date per ticker
        event_key = (event_date, event_name)
        if event_key in seen_events:
            continue
        seen_events.add(event_key)

        impact = event.get("impact", "")
        estimate = event.get("estimate")
        prior = event.get("prev")
        desc_parts = [event_name]
        if estimate is not None:
            desc_parts.append(f"est: {estimate}")
        if prior is not None:
            desc_parts.append(f"prior: {prior}")
        if impact:
            desc_parts.append(f"impact: {impact}")

        for ticker in watchlist_macro:
            catalysts.append(Catalyst(
                ticker=ticker,
                event_type="economic",
                event_date=event_date,
                days_until=days_until,
                description=" — ".join(desc_parts),
            ))
    return catalysts


def fetch_catalysts(tickers: list, days_ahead: int = 49) -> list[Catalyst]:
    """Merge all catalyst sources. Deduplicates by (ticker, date) keeping highest-priority type."""
    all_catalysts = []
    all_catalysts.extend(_fetch_earnings(tickers, days_ahead))
    all_catalysts.extend(_fetch_fomc(tickers, days_ahead))
    all_catalysts.extend(_fetch_economic_calendar(tickers, days_ahead))

    # Dedupe: if a ticker has earnings AND a macro event on the same day,
    # keep earnings (more specific). Priority: earnings > fed > economic
    priority = {"earnings": 0, "fed": 1, "economic": 2}
    seen = {}
    for cat in all_catalysts:
        key = (cat.ticker, cat.event_date)
        if key not in seen or priority.get(cat.event_type, 99) < priority.get(seen[key].event_type, 99):
            seen[key] = cat

    result = sorted(seen.values(), key=lambda c: c.days_until)
    return result


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
- Technical trend: {trend_summary}

Evaluate on these dimensions:
1. Is the catalyst genuinely likely to move the stock 8%+ in the expected direction?
2. Is IV cheap enough to justify buying? Low IV rank (≤30) is a strong positive — options are underpriced. Score higher when IV rank is low.
3. Is theta decay manageable relative to expected holding period and catalyst timing?
4. Does the technical trend support the direction? A call setup needs bullish trend; a put needs bearish. Trend-contrary setups should score lower.
5. Is the strike well-placed (room to run, not too far OTM)?
6. Is there a clear thesis or just a vague hope?
7. Are there obvious risks that argue against this trade?

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
        trend_summary=setup.trend_summary or "no trend data",
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
        f"Trend: {setup.trend_summary}\n"
        f"Catalyst: {setup.catalyst.description} ({setup.catalyst.event_date})\n\n"
        f"{setup.thesis}"
    )


def send_email(subject: str, body: str):
    if not SENDGRID_KEY:
        return
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
    mail = Mail(from_email="alerts@yourdomain.com", to_emails=ALERT_EMAIL,
                subject=subject, plain_text_content=body)
    SendGridAPIClient(SENDGRID_KEY).send(mail)
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
# SCANNER SUMMARY TABLE
# ============================================================================

def _iv_label(rank: Optional[float]) -> str:
    """Human-readable IV rank label."""
    if rank is None:
        return "N/A"
    if rank <= 20:
        return f"{rank:.0f} (very low)"
    elif rank <= 30:
        return f"{rank:.0f} (low)"
    elif rank <= 40:
        return f"{rank:.0f} (fair)"
    elif rank <= 60:
        return f"{rank:.0f} (elevated)"
    elif rank <= 80:
        return f"{rank:.0f} (high)"
    else:
        return f"{rank:.0f} (very high)"


def _trend_label(trend: dict) -> str:
    """Trend direction with conviction."""
    conv = trend.get("conviction", 0)
    signals = trend.get("signals", {})
    ts = signals.get("trend_score", 0)

    if ts >= 1:
        return f"Strong Bullish ({conv}/3)"
    elif ts >= 0.5:
        return f"Bullish ({conv}/3)"
    elif ts <= -1:
        return f"Strong Bearish ({conv}/3)"
    elif ts <= -0.5:
        return f"Bearish ({conv}/3)"
    else:
        return f"Neutral ({conv}/3)"


def _setup_label(trend: dict) -> str:
    """Setup type from compression and breakout proximity."""
    signals = trend.get("signals", {})
    atr_ratio = signals.get("atr_ratio", 1.0)
    near = signals.get("near_breakout", "mid")

    if atr_ratio < 0.5 and near == "high":
        return "Tight @ highs"
    elif atr_ratio < 0.7 and near == "high":
        return "Consolidating"
    elif atr_ratio < 0.5 and near == "low":
        return "Tight @ lows"
    elif atr_ratio < 0.7 and near == "low":
        return "Bear flag"
    elif atr_ratio < 0.7:
        return "Compressing"
    elif near == "high":
        return "Breakout zone"
    elif near == "low":
        return "Breakdown zone"
    else:
        return "Ranging"


def print_scanner_table(catalysts: list, ticker_data: dict):
    """Print rich summary table of all scanned tickers."""

    # Build rows: one per unique ticker (with its best/first catalyst)
    seen = set()
    rows = []
    for cat in catalysts:
        if cat.ticker in seen:
            continue
        seen.add(cat.ticker)

        data = ticker_data.get(cat.ticker)
        if not data:
            rows.append({
                "ticker": cat.ticker, "catalyst": cat.event_type,
                "days": cat.days_until, "iv_label": "N/A",
                "iv_rank": None, "trend": "—", "ma": "—",
                "structure": "—", "rs": "—", "setup": "—",
                "direction": "—", "status": "NO DATA",
            })
            continue

        underlying, iv_rank, trend = data
        signals = trend.get("signals", {})

        # Determine status
        if iv_rank is None:
            status = "IV FAIL"
        elif iv_rank > CRITERIA["max_iv_rank"]:
            status = "IV HIGH"
        else:
            status = "PASS"

        rows.append({
            "ticker": cat.ticker,
            "catalyst": cat.event_type,
            "days": cat.days_until,
            "iv_label": _iv_label(iv_rank),
            "iv_rank": iv_rank,
            "trend": _trend_label(trend),
            "ma": signals.get("ma_alignment", "—"),
            "structure": signals.get("structure", "—"),
            "rs": f"{signals.get('rs_spread', 0):+.1f}pp" if signals.get("rs_spread") is not None else "—",
            "setup": _setup_label(trend),
            "direction": trend.get("direction", "—"),
            "status": status,
        })

    # Sort: PASS first (lowest IV rank), then rest
    rows.sort(key=lambda r: (0 if r["status"] == "PASS" else 1, r["iv_rank"] or 999))

    # Print table
    print(f"\n{'='*120}")
    print(f"{'SCANNER SUMMARY':^120}")
    print(f"{'='*120}")

    header = (f"{'Ticker':<7} {'Cat':>5} {'Days':>4}  {'IV Rank':<15} "
              f"{'Trend':<22} {'MAs':<6} {'Struct':<7} {'RS/SPY':<9} "
              f"{'Setup':<16} {'Dir':<6} {'Status':<8}")
    print(header)
    print("-" * 120)

    for r in rows:
        line = (f"{r['ticker']:<7} {r['catalyst']:>5} {r['days']:>4}  "
                f"{r['iv_label']:<15} {r['trend']:<22} {r['ma']:<6} "
                f"{r['structure']:<7} {r['rs']:<9} {r['setup']:<16} "
                f"{r['direction']:<6} {r['status']:<8}")

        # Mark passing rows
        if r["status"] == "PASS":
            line = f"▶ {line}"
        else:
            line = f"  {line}"
        print(line)

    print("-" * 120)
    passing = sum(1 for r in rows if r["status"] == "PASS")
    print(f"  {len(rows)} tickers scanned | {passing} passed all filters\n")


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def run(dry_run: bool = False, test_ticker: Optional[str] = None):
    print(f"[{datetime.now().isoformat()}] Starting scan...")

    tickers = [test_ticker] if test_ticker else WATCHLIST

    # 1. Get catalysts in the sweet-spot window
    catalysts = fetch_catalysts(tickers)
    earnings_count = sum(1 for c in catalysts if c.event_type == "earnings")
    fed_count = sum(1 for c in catalysts if c.event_type == "fed")
    econ_count = sum(1 for c in catalysts if c.event_type == "economic")
    print(f"Found {len(catalysts)} catalysts in {CRITERIA['catalyst_window_min_days']}-"
          f"{CRITERIA['catalyst_window_max_days']} day window "
          f"(earnings: {earnings_count}, FOMC: {fed_count}, econ: {econ_count})")

    # Pre-fetch price + IV rank per unique ticker (avoids redundant API calls
    # when multiple catalysts exist for the same ticker, e.g. FOMC + earnings)
    unique_tickers = list(dict.fromkeys(cat.ticker for cat in catalysts))
    print(f"Fetching data for {len(unique_tickers)} unique tickers...")

    ticker_data = {}  # ticker -> (underlying, iv_rank, trend)
    for t in unique_tickers:
        underlying = get_underlying_price(t)
        if not underlying:
            print(f"  {t}: no price, skipping")
            continue
        rank = get_iv_rank(t, underlying=underlying)
        trend = get_trend_bias(t, underlying=underlying)
        ticker_data[t] = (underlying, rank, trend)
        if rank is None:
            print(f"  {t}: IV rank fetch failed")
        elif rank > CRITERIA["max_iv_rank"]:
            print(f"  {t}: IV rank {rank:.1f} (too high) | {trend['summary']}")
        else:
            print(f"  {t}: IV rank {rank:.1f} ✓ | {trend['summary']} → {trend['direction']}")

    # Print summary table before proceeding to contract selection + LLM eval
    print_scanner_table(catalysts, ticker_data)

    qualified = []
    for cat in catalysts:
        data = ticker_data.get(cat.ticker)
        if not data:
            continue
        underlying, iv_rank, trend = data
        if iv_rank is None or iv_rank > CRITERIA["max_iv_rank"]:
            continue

        # Trend filter: only evaluate direction(s) matching technical bias
        if trend["direction"] == "both":
            directions = ("call", "put")
        else:
            directions = (trend["direction"],)

        for direction in directions:
            dte_target = cat.days_until + 21
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
                trend_summary=trend["summary"],
            )
            setup = evaluate_setup(setup)
            print(f"  {cat.ticker} {direction} ({cat.event_type}): scored {setup.score}/10")
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
