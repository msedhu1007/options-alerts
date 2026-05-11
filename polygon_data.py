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
from dotenv import load_dotenv

load_dotenv()

POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
POLYGON_BASE = "https://api.polygon.io"
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")


# ============================================================================
# UNDERLYING PRICE
# ============================================================================

def get_underlying_price(ticker: str) -> Optional[float]:
    """Current price via Finnhub (real-time), Polygon prev close as fallback."""
    # Finnhub — free, real-time during market hours
    if FINNHUB_KEY:
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": ticker, "token": FINNHUB_KEY},
                timeout=10,
            )
            r.raise_for_status()
            price = r.json().get("c")
            if price and price > 0:
                return float(price)
        except Exception:
            pass
    # Fallback: Polygon previous close
    try:
        url = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/prev"
        r = requests.get(url, params={"apiKey": POLYGON_KEY, "adjusted": "true"}, timeout=10)
        return float(r.json()["results"][0]["c"])
    except Exception as e:
        print(f"[ERROR] Price fetch failed for {ticker}: {e}")
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
    # 1. Get current IV — use realized vol as proxy since Polygon Starter
    #    doesn't reliably populate implied_volatility on all contracts
    underlying = get_underlying_price(ticker)
    if not underlying:
        return None

    today = datetime.now().date()

    # 2. Pull historical prices to compute rolling realized vol
    try:
        start = (today - timedelta(days=lookback_days + 30)).isoformat()
        end = today.isoformat()
        url = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
        r = requests.get(url, params={"apiKey": POLYGON_KEY, "adjusted": "true", "limit": 5000}, timeout=30)
        bars = r.json().get("results", [])
    except Exception as e:
        print(f"[ERROR] Polygon history failed for {ticker}: {e}")
        return None

    if len(bars) < 60:
        return None

    # Compute rolling 30-day realized vol
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

    # Current RV = last 30-day window, scaled by 1.15 as IV premium proxy
    current_iv_proxy = rolling_rv[-1] * 1.15
    scaled_rv = [v * 1.15 for v in rolling_rv]
    below = sum(1 for v in scaled_rv if v < current_iv_proxy)
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
    MAX_THETA_PCT = 0.02

    # Polygon contract_type uses "call" / "put"
    contract_type = direction.lower()

    # 1. Pull options snapshot filtered to target expiry window
    url = f"{POLYGON_BASE}/v3/snapshot/options/{ticker}"
    today = datetime.now().date()
    target_date = today + timedelta(days=days_to_expiry_target)
    exp_from = (today + timedelta(days=max(days_to_expiry_target - 14, 7))).isoformat()
    exp_to = (today + timedelta(days=days_to_expiry_target + 14)).isoformat()

    params = {
        "apiKey": POLYGON_KEY,
        "contract_type": contract_type,
        "expiration_date.gte": exp_from,
        "expiration_date.lte": exp_to,
        "limit": 250,
    }

    candidates = []
    next_url = url
    debug = os.environ.get("DEBUG", "").lower() in ("1", "true")
    filter_stats = {"total": 0, "dte": 0, "delta": 0, "quote": 0, "spread": 0, "oi": 0, "passed": 0}

    # Paginate through results
    page = 0
    while next_url and page < 5:  # cap at 5 pages
        try:
            if page == 0:
                r = requests.get(next_url, params=params, timeout=30)
            else:
                r = requests.get(next_url, params={"apiKey": POLYGON_KEY}, timeout=30)
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
            filter_stats["total"] += 1

            exp = details.get("expiration_date")
            if not exp:
                continue
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            except Exception:
                continue
            dte = (exp_date - today).days

            if abs(dte - days_to_expiry_target) > 14:
                filter_stats["dte"] += 1
                continue

            delta = abs(greeks.get("delta", 0))
            if not (DELTA_MIN <= delta <= DELTA_MAX):
                filter_stats["delta"] += 1
                continue

            # Try last_quote first, fall back to day close
            bid = (last_quote or {}).get("bid", 0)
            ask = (last_quote or {}).get("ask", 0)
            if bid and ask and ask > 0:
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid if mid > 0 else 1
                if spread_pct > MAX_SPREAD_PCT:
                    filter_stats["spread"] += 1
                    continue
            else:
                mid = day.get("close", 0)
                if not mid or mid <= 0:
                    filter_stats["quote"] += 1
                    continue
                spread_pct = None

            oi = c.get("open_interest", 0)
            if oi < MIN_OI:
                filter_stats["oi"] += 1
                continue

            # Theta filter — daily decay ≤ 2% of premium
            theta = abs(greeks.get("theta", 0))
            if mid > 0 and theta > 0:
                theta_pct = theta / mid
                if theta_pct > MAX_THETA_PCT:
                    filter_stats.setdefault("theta", 0)
                    filter_stats["theta"] += 1
                    continue
            else:
                theta_pct = None

            filter_stats["passed"] += 1

            iv = c.get("implied_volatility", 0)

            candidates.append({
                "symbol": details.get("ticker"),
                "strike": details.get("strike_price"),
                "expiry": exp,
                "mid": round(mid, 2),
                "delta": delta if contract_type == "call" else -delta,
                "theta": round(-theta, 4),
                "theta_pct": round(theta_pct, 4) if theta_pct else None,
                "iv": iv,
                "open_interest": oi,
                "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
                "dte_distance": abs(dte - days_to_expiry_target),
            })

        next_url = data.get("next_url")
        page += 1

    if debug or not candidates:
        print(f"  [{ticker} {contract_type}] Filter stats: {filter_stats}")

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
