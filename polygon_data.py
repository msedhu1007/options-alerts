"""
POLYGON DATA MODULE
===================
Drop-in replacement for the Tradier functions in options_agent.py.
Uses Polygon.io Options Starter ($29/mo) for chains, greeks, and IV history.

Replace these three functions in options_agent.py:
  - get_underlying_price
  - get_iv_rank
  - find_target_contract

Set POLYGON_API_KEY in your environment.
"""

import os
import statistics
from datetime import datetime, timedelta
from typing import Optional

import requests


POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
POLYGON_BASE = "https://api.polygon.io"


# ============================================================================
# UNDERLYING PRICE
# ============================================================================

def get_underlying_price(ticker: str) -> Optional[float]:
    """Last trade price from Polygon."""
    url = f"{POLYGON_BASE}/v2/last/trade/{ticker}"
    try:
        r = requests.get(url, params={"apiKey": POLYGON_KEY}, timeout=10)
        r.raise_for_status()
        return float(r.json()["results"]["p"])
    except Exception as e:
        # Fallback to previous close if last trade unavailable (e.g., outside market hours)
        try:
            url = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/prev"
            r = requests.get(url, params={"apiKey": POLYGON_KEY, "adjusted": "true"}, timeout=10)
            return float(r.json()["results"][0]["c"])
        except Exception as e2:
            print(f"[ERROR] Polygon price fetch failed for {ticker}: {e2}")
            return None


# ============================================================================
# IV RANK — the previously-stubbed function, now real
# ============================================================================

def get_iv_rank(ticker: str, lookback_days: int = 252) -> Optional[float]:
    """
    IV rank = where current 30-day IV sits in its trailing 52-week range, as 0-100.

    Polygon doesn't expose IV rank directly, so we approximate it by:
      1. Pulling the ATM call IV from the current chain snapshot
      2. Pulling the historical daily snapshots over the lookback period
      3. Computing percentile rank

    Note: For production use, ORATS or FlashAlpha give you IV rank directly
    pre-computed. This approximation is good enough for screening.
    """
    # 1. Get current 30-day ATM IV from snapshot
    try:
        url = f"{POLYGON_BASE}/v3/snapshot/options/{ticker}"
        r = requests.get(url, params={"apiKey": POLYGON_KEY, "limit": 250}, timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        print(f"[ERROR] Polygon snapshot failed for {ticker}: {e}")
        return None

    if not results:
        return None

    # Find ATM contracts with ~30 DTE
    underlying = get_underlying_price(ticker)
    if not underlying:
        return None

    today = datetime.now().date()
    candidates = []
    for c in results:
        details = c.get("details", {})
        if details.get("contract_type") != "call":
            continue
        strike = details.get("strike_price")
        exp = details.get("expiration_date")
        if not (strike and exp):
            continue
        try:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        except Exception:
            continue
        if not (20 <= dte <= 45):
            continue
        iv = c.get("implied_volatility")
        if iv is None:
            continue
        # Closer to ATM = better
        candidates.append((abs(strike - underlying), iv, dte))

    if not candidates:
        return None
    candidates.sort()
    current_iv = candidates[0][1]

    # 2. Historical IV — pull from daily option snapshots for past year
    # Polygon Starter doesn't include historical IV directly per snapshot,
    # so we use a proxy: realized vol from underlying historical prices.
    # This is a reasonable approximation since IV tracks RV closely on average.
    try:
        start = (today - timedelta(days=lookback_days + 30)).isoformat()
        end = today.isoformat()
        url = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
        r = requests.get(url, params={"apiKey": POLYGON_KEY, "adjusted": "true", "limit": 5000}, timeout=15)
        bars = r.json().get("results", [])
    except Exception as e:
        print(f"[ERROR] Polygon history failed for {ticker}: {e}")
        return None

    if len(bars) < 60:
        return None

    # Compute rolling 30-day realized vol as a proxy for historical IV distribution
    closes = [b["c"] for b in bars]
    rolling_rv = []
    for i in range(30, len(closes)):
        window = closes[i-30:i]
        returns = [
            (window[j] / window[j-1]) - 1
            for j in range(1, len(window))
        ]
        if len(returns) >= 2:
            daily_vol = statistics.stdev(returns)
            annualized = daily_vol * (252 ** 0.5)
            rolling_rv.append(annualized)

    if not rolling_rv:
        return None

    # Rank current IV within distribution of historical realized vol (proxy)
    # IV typically trades at premium to RV, so we scale RV by ~1.15 average premium
    scaled_rv = [v * 1.15 for v in rolling_rv]
    below = sum(1 for v in scaled_rv if v < current_iv)
    iv_rank = (below / len(scaled_rv)) * 100
    return round(iv_rank, 1)


# ============================================================================
# OPTION CHAIN + TARGET CONTRACT SELECTION
# ============================================================================

def find_target_contract(ticker: str, direction: str, days_to_expiry_target: int) -> Optional[dict]:
    """
    Find an option contract matching target delta and DTE using Polygon.
    Returns dict with: symbol, strike, expiry, mid, delta, iv, open_interest, spread_pct.
    """
    DELTA_MIN = 0.28
    DELTA_MAX = 0.40
    MAX_SPREAD_PCT = 0.05
    MIN_OI = 500

    # Polygon contract_type uses "call" / "put"
    contract_type = direction.lower()

    # 1. Pull options snapshot for the underlying — this includes greeks + IV
    url = f"{POLYGON_BASE}/v3/snapshot/options/{ticker}"
    params = {
        "apiKey": POLYGON_KEY,
        "contract_type": contract_type,
        "limit": 250,
    }

    today = datetime.now().date()
    target_date = today + timedelta(days=days_to_expiry_target)

    candidates = []
    next_url = url

    # Paginate through results
    page = 0
    while next_url and page < 5:  # cap at 5 pages
        try:
            if page == 0:
                r = requests.get(next_url, params=params, timeout=15)
            else:
                r = requests.get(next_url, params={"apiKey": POLYGON_KEY}, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[ERROR] Polygon chain fetch failed for {ticker}: {e}")
            break

        for c in data.get("results", []):
            details = c.get("details", {})
            greeks = c.get("greeks", {})
            day = c.get("day", {})
            last_quote = c.get("last_quote", {})

            exp = details.get("expiration_date")
            if not exp:
                continue
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            except Exception:
                continue
            dte = (exp_date - today).days

            # Only consider expirations within +/- 14 days of target
            if abs(dte - days_to_expiry_target) > 14:
                continue

            delta = abs(greeks.get("delta", 0))
            if not (DELTA_MIN <= delta <= DELTA_MAX):
                continue

            bid = last_quote.get("bid", 0)
            ask = last_quote.get("ask", 0)
            if not bid or not ask or ask <= 0:
                continue

            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid if mid > 0 else 1
            if spread_pct > MAX_SPREAD_PCT:
                continue

            oi = c.get("open_interest", 0)
            if oi < MIN_OI:
                continue

            iv = c.get("implied_volatility", 0)

            candidates.append({
                "symbol": details.get("ticker"),
                "strike": details.get("strike_price"),
                "expiry": exp,
                "mid": round(mid, 2),
                "delta": delta if contract_type == "call" else -delta,
                "iv": iv,
                "open_interest": oi,
                "spread_pct": round(spread_pct, 3),
                "dte_distance": abs(dte - days_to_expiry_target),
            })

        next_url = data.get("next_url")
        page += 1

    if not candidates:
        return None

    # Sort by closeness to target delta 0.34 AND target DTE
    # Weight delta closeness more heavily
    candidates.sort(key=lambda c: (abs(abs(c["delta"]) - 0.34) * 10) + (c["dte_distance"] * 0.5))
    return candidates[0]


# ============================================================================
# SELF-TEST
# ============================================================================

if __name__ == "__main__":
    # Quick test
    ticker = "NVDA"
    print(f"Testing Polygon module with {ticker}...\n")

    price = get_underlying_price(ticker)
    print(f"Underlying price: ${price}")

    iv_rank = get_iv_rank(ticker)
    print(f"IV rank: {iv_rank}")

    call = find_target_contract(ticker, "call", 35)
    print(f"\nBest call setup: {call}")

    put = find_target_contract(ticker, "put", 35)
    print(f"\nBest put setup: {put}")
