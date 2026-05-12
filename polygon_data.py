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
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
POLYGON_BASE = "https://api.polygon.io"
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")

# Per-scan cache: bars fetched for IV rank are reused for trend analysis
_bars_cache: dict[str, list[float]] = {}


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
# IV RANK — uses real ATM implied vol from Polygon snapshot
# ============================================================================

def _get_atm_iv(ticker: str, underlying: float) -> Optional[float]:
    """Pull ATM call IV from Polygon options snapshot (30-45 DTE window)."""
    today = datetime.now().date()
    exp_from = (today + timedelta(days=20)).isoformat()
    exp_to = (today + timedelta(days=50)).isoformat()

    url = f"{POLYGON_BASE}/v3/snapshot/options/{ticker}"
    params = {
        "contract_type": "call",
        "expiration_date.gte": exp_from,
        "expiration_date.lte": exp_to,
        "limit": 250,
    }

    resp = _polygon_get(url, params)
    if not resp:
        print(f"[WARN] ATM IV snapshot failed for {ticker}")
        return None
    results = resp.get("results", [])

    # Find contracts closest to ATM with valid IV
    atm_candidates = []
    for c in results:
        strike = c.get("details", {}).get("strike_price", 0)
        iv = c.get("implied_volatility", 0)
        if not iv or iv <= 0:
            continue
        moneyness = abs(strike - underlying) / underlying
        if moneyness <= 0.05:  # within 5% of spot
            atm_candidates.append((moneyness, iv))

    if not atm_candidates:
        return None

    # Average IV of the 2 closest-to-ATM strikes
    atm_candidates.sort(key=lambda x: x[0])
    top = atm_candidates[:2]
    return sum(iv for _, iv in top) / len(top)


_last_polygon_call = 0.0
POLYGON_CALL_INTERVAL = 12.5  # seconds between calls; Polygon Starter = 5 calls/min

def _polygon_get(url: str, params: dict, retries: int = 3) -> Optional[dict]:
    """Polygon API call with pacing, retry, and backoff for rate limiting."""
    global _last_polygon_call
    for attempt in range(retries + 1):
        # Pace: wait until interval has passed since last call
        elapsed = time.time() - _last_polygon_call
        if elapsed < POLYGON_CALL_INTERVAL:
            time.sleep(POLYGON_CALL_INTERVAL - elapsed)
        try:
            _last_polygon_call = time.time()
            r = requests.get(url, params={**params, "apiKey": POLYGON_KEY}, timeout=30)
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"[WARN] Polygon 429, waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            if attempt < retries:
                wait = 15
                print(f"[WARN] Polygon timeout, retry {attempt+1}/{retries} after {wait}s...")
                time.sleep(wait)
            else:
                return None
        except Exception:
            return None
    return None


def get_iv_rank(ticker: str, underlying: float = None, lookback_days: int = 252) -> Optional[float]:
    """
    IV rank = where current ATM IV sits vs trailing 52-week realized vol range.

    Uses real ATM implied volatility from Polygon options snapshot for current
    reading.  Historical baseline is rolling 30-day realized vol (Polygon
    Starter lacks historical IV snapshots).  This is a hybrid approach —
    significantly more accurate than pure RV-vs-RV, especially around earnings
    when IV diverges from RV.

    Pass underlying price to avoid redundant API call.
    """
    if underlying is None:
        underlying = get_underlying_price(ticker)
    if not underlying:
        return None

    today = datetime.now().date()

    # 1. Historical baseline first (heavier call) — rolling 30-day realized vol
    start = (today - timedelta(days=lookback_days + 30)).isoformat()
    end = today.isoformat()
    url = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
    resp = _polygon_get(url, {"adjusted": "true", "limit": 5000})
    if not resp:
        print(f"[ERROR] Polygon history failed for {ticker}")
        return None

    bars = resp.get("results", [])
    if len(bars) < 60:
        print(f"[WARN] {ticker}: only {len(bars)} bars, need 60+")
        return None

    closes = [b["c"] for b in bars]
    _bars_cache[ticker] = closes
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

    # 2. Current IV — real ATM implied vol from options snapshot
    current_iv = _get_atm_iv(ticker, underlying)

    # Fall back to RV if snapshot IV unavailable
    if current_iv is None:
        print(f"[WARN] {ticker}: ATM IV unavailable, falling back to realized vol")
        current_iv = rolling_rv[-1]

    # 3. Rank current IV against historical RV distribution
    below = sum(1 for v in rolling_rv if v < current_iv)
    iv_rank = (below / len(rolling_rv)) * 100
    return round(iv_rank, 1)


def _ensure_spy_cached() -> list[float]:
    """Fetch SPY daily closes into cache if not already present."""
    if "SPY" in _bars_cache:
        return _bars_cache["SPY"]
    today = datetime.now().date()
    start = (today - timedelta(days=282)).isoformat()
    end = today.isoformat()
    url = f"{POLYGON_BASE}/v2/aggs/ticker/SPY/range/1/day/{start}/{end}"
    resp = _polygon_get(url, {"adjusted": "true", "limit": 5000})
    if resp and resp.get("results"):
        closes = [b["c"] for b in resp["results"]]
        _bars_cache["SPY"] = closes
        return closes
    return []


def _sma(closes: list[float], period: int) -> float:
    """Simple moving average of last N closes."""
    if len(closes) < period:
        return closes[-1]
    return sum(closes[-period:]) / period


def _find_swing_points(closes: list[float], lookback: int = 40) -> tuple[list[float], list[float]]:
    """Find swing highs and lows in last N bars (3-bar pivot)."""
    data = closes[-lookback:]
    highs = []
    lows = []
    for i in range(1, len(data) - 1):
        if data[i] > data[i-1] and data[i] > data[i+1]:
            highs.append(data[i])
        if data[i] < data[i-1] and data[i] < data[i+1]:
            lows.append(data[i])
    return highs, lows


def get_trend_bias(ticker: str, underlying: float = None) -> dict:
    """
    3-pillar directional bias: Trend + Volatility Compression + Relative Strength.

    Returns dict with:
      - direction: "call", "put", or "both"
      - conviction: 0-3 (how many pillars align)
      - signals: dict of individual signal values for LLM context
      - summary: one-line description for logging/prompt

    Pillar 1 — Trend & Structure:
      MA alignment (price > 20 > 50) + higher highs/higher lows

    Pillar 2 — Volatility Compression:
      ATR contracting (recent vs prior) + volume dry-up = setup forming

    Pillar 3 — Relative Strength:
      Stock 20-day return vs SPY 20-day return

    Call get_iv_rank first — it populates the bars cache.
    """
    closes = _bars_cache.get(ticker)
    if not closes or len(closes) < 60:
        return {"direction": "both", "conviction": 0, "signals": {}, "summary": "insufficient data"}

    price = underlying if underlying else closes[-1]

    # ── PILLAR 1: Trend & Structure ──────────────────────────────────
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)

    # MA alignment
    bullish_ma = price > sma20 > sma50
    bearish_ma = price < sma20 < sma50

    # Swing structure: higher highs/higher lows or lower highs/lower lows
    swing_highs, swing_lows = _find_swing_points(closes, 40)

    higher_highs = False
    higher_lows = False
    lower_highs = False
    lower_lows = False
    if len(swing_highs) >= 2:
        higher_highs = swing_highs[-1] > swing_highs[-2]
        lower_highs = swing_highs[-1] < swing_highs[-2]
    if len(swing_lows) >= 2:
        higher_lows = swing_lows[-1] > swing_lows[-2]
        lower_lows = swing_lows[-1] < swing_lows[-2]

    bullish_structure = higher_highs and higher_lows
    bearish_structure = lower_highs and lower_lows

    # Trend score: MA alignment + structure confirmation
    if bullish_ma and bullish_structure:
        trend_score = 1  # strong bullish
    elif bullish_ma or (higher_lows and price > sma50):
        trend_score = 0.5  # leaning bullish
    elif bearish_ma and bearish_structure:
        trend_score = -1  # strong bearish
    elif bearish_ma or (lower_highs and price < sma50):
        trend_score = -0.5  # leaning bearish
    else:
        trend_score = 0  # choppy/neutral

    # ── PILLAR 2: Volatility Compression ─────────────────────────────
    # ATR ratio: recent 5-day range vs prior 20-day range
    def avg_range(c, start, end):
        ranges = [abs(c[i] - c[i-1]) for i in range(start, end)]
        return sum(ranges) / len(ranges) if ranges else 1

    atr_recent = avg_range(closes, -5, len(closes))
    atr_prior = avg_range(closes, -25, -5)
    atr_ratio = atr_recent / atr_prior if atr_prior > 0 else 1

    # Volume proxy: not available in closes-only cache, use price range as proxy
    # Tight range = compression. ATR ratio < 0.6 = significant compression
    compression = atr_ratio < 0.7
    extreme_compression = atr_ratio < 0.5

    # Price near 20-day high/low (breakout proximity)
    high_20d = max(closes[-20:])
    low_20d = min(closes[-20:])
    near_high = (price / high_20d) > 0.97  # within 3% of 20d high
    near_low = (price / low_20d) < 1.03   # within 3% of 20d low

    # Compression score
    if compression and near_high:
        compression_score = 1  # bullish: tight near highs, ready to break out
    elif compression and near_low:
        compression_score = -1  # bearish: tight near lows, ready to break down
    elif compression:
        compression_score = 0.5 if trend_score > 0 else (-0.5 if trend_score < 0 else 0)
    else:
        compression_score = 0  # no compression, no edge

    # ── PILLAR 3: Relative Strength vs SPY ───────────────────────────
    spy_closes = _ensure_spy_cached()

    stock_ret_20d = ((price / closes[-21]) - 1) * 100 if len(closes) >= 21 else 0
    if len(spy_closes) >= 21:
        spy_ret_20d = ((spy_closes[-1] / spy_closes[-21]) - 1) * 100
    else:
        spy_ret_20d = 0

    rs_spread = stock_ret_20d - spy_ret_20d

    if rs_spread > 5:
        rs_score = 1  # strong outperformance
    elif rs_spread > 1:
        rs_score = 0.5  # moderate outperformance
    elif rs_spread < -5:
        rs_score = -1  # strong underperformance
    elif rs_spread < -1:
        rs_score = -0.5  # moderate underperformance
    else:
        rs_score = 0  # in line with market

    # ── COMPOSITE DECISION ───────────────────────────────────────────
    total = trend_score + compression_score + rs_score

    # Count pillars aligned (for conviction rating)
    bull_pillars = sum(1 for s in [trend_score, compression_score, rs_score] if s > 0)
    bear_pillars = sum(1 for s in [trend_score, compression_score, rs_score] if s < 0)

    if total >= 1.5:
        direction = "call"
        trend_word = "bullish"
    elif total <= -1.5:
        direction = "put"
        trend_word = "bearish"
    elif total >= 0.5:
        direction = "call"
        trend_word = "leaning bullish"
    elif total <= -0.5:
        direction = "put"
        trend_word = "leaning bearish"
    else:
        direction = "both"
        trend_word = "neutral"

    conviction = max(bull_pillars, bear_pillars)

    signals = {
        "sma20": round(sma20, 2),
        "sma50": round(sma50, 2),
        "ma_alignment": "bull" if bullish_ma else ("bear" if bearish_ma else "mixed"),
        "structure": "HH/HL" if bullish_structure else ("LH/LL" if bearish_structure else "mixed"),
        "atr_ratio": round(atr_ratio, 2),
        "compression": "tight" if compression else ("very tight" if extreme_compression else "normal"),
        "near_breakout": "high" if near_high else ("low" if near_low else "mid"),
        "stock_20d_ret": round(stock_ret_20d, 1),
        "spy_20d_ret": round(spy_ret_20d, 1),
        "rs_spread": round(rs_spread, 1),
        "trend_score": trend_score,
        "compression_score": compression_score,
        "rs_score": rs_score,
    }

    # Build summary
    parts = []
    # Trend
    ma_str = "20>50" if bullish_ma else ("20<50" if bearish_ma else "mixed")
    struct_str = "HH/HL" if bullish_structure else ("LH/LL" if bearish_structure else "choppy")
    parts.append(f"trend:{ma_str},{struct_str}")
    # Compression
    if compression:
        loc = "near highs" if near_high else ("near lows" if near_low else "mid-range")
        parts.append(f"compressed(ATR {atr_ratio:.1f}x) {loc}")
    # RS
    parts.append(f"RS vs SPY:{rs_spread:+.1f}pp")

    summary = f"{trend_word} ({conviction}/3) | {' | '.join(parts)}"

    return {"direction": direction, "conviction": conviction, "signals": signals, "summary": summary}


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

    iv_rank = get_iv_rank(ticker, underlying=price)
    print(f"IV rank: {iv_rank}")

    trend = get_trend_bias(ticker, underlying=price)
    print(f"Trend: {trend['summary']}")
    print(f"Direction: {trend['direction']}")
    print(f"Signals: {trend['signals']}")

    call = find_target_contract(ticker, "call", 35)
    print(f"\nBest call setup: {call}")

    put = find_target_contract(ticker, "put", 35)
    print(f"\nBest put setup: {put}")
