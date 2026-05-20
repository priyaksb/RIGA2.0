"""
RIGA AI - Final main.py v11.2
Book/PDF-based option buying scanner with Angel One SmartAPI

Rules:
- Final trades are OPTION BUYING only.
- Bullish index bias  -> BUY_CE
- Bearish index bias  -> BUY_PE
- No SELL / SHORT / writing output.
- Entry, SL, targets are on OPTION PREMIUM.
- Uses candle structure, swing breakout, VWAP, volume expansion, trap filter,
  exhaustion/chase filter, ATM preference, risk cap.
"""

from __future__ import annotations

import os
import requests
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel
from SmartApi import SmartConnect

load_dotenv()

APP_VERSION = "11.6-BALANCED-OPPORTUNITY"

app = FastAPI(title="RIGA AI Option Buying Scanner v11.6 Balanced Opportunity", version=APP_VERSION)

Side = Literal["CE", "PE"]
Bias = Literal["BULLISH", "BEARISH", "NEUTRAL"]

IST = timezone(timedelta(hours=5, minutes=30))

# TEST ONLY fallback values.
# Environment variables are still preferred. If Render env values exist, they will be used first.
# Remove these fallback values after testing and rotate exposed credentials.
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN")

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

CONFIDENCE_MIN = 70
MAX_RISK_PCT = 12.0
MIN_RISK_PCT = 1.0
DEFAULT_BUFFER_PCT = 0.015
MIN_CANDLES = 8

TRANSITION_SCAN_MIN_SCORE = 40
TRANSITION_STRIKES_AROUND = 2
MAX_SCAN_STRIKES_AROUND = 5
PREMIUM_LED_SCAN_ALWAYS = True

# v11.6 balanced opportunity controls: more scans, fewer false rejections, final confidence gate still active
# FINNIFTY is manually blacklisted due repeated weak/loss signals in live testing.
BLACKLISTED_INDEXES = set()
FINAL_REQUIRE_FRESH_INDEX = True
DEVELOPING_MIN_CONFIDENCE = 50
WATCHLIST_MAX_ITEMS = 8
REJECTED_SUMMARY_MAX_ITEMS = 12
MOMENTUM_BREAKOUT_ALLOW = True
MOMENTUM_BREAKOUT_MAX_PREMIUM_PCT = 3.5
CHASE_VWAP_HARD_REJECT_PCT = 6.5
LTP_SIGNAL_DISTANCE_MAX_PCT = 4.0

client_obj = None
scrip_master_cache = None

INDEX_CONFIG = {
    "NIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY 50", "symboltoken": "26000", "hist_token": "99926000"},
        "option_exchange": "NFO",
        "option_name": "NIFTY",
        "step": 50,
    },
    "BANKNIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY BANK", "symboltoken": "26009", "hist_token": "99926009"},
        "option_exchange": "NFO",
        "option_name": "BANKNIFTY",
        "step": 100,
    },
    "FINNIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY FIN SERVICE", "symboltoken": "26037", "hist_token": "99926037"},
        "option_exchange": "NFO",
        "option_name": "FINNIFTY",
        "step": 50,
    },
    "SENSEX": {
        "spot": {"exchange": "BSE", "tradingsymbol": "SENSEX", "symboltoken": "1", "hist_token": "99919000"},
        "option_exchange": "BFO",
        "option_name": "SENSEX",
        "step": 100,
    },
}


# -----------------------------
# Auth / Client
# -----------------------------

def now_ist() -> datetime:
    return datetime.now(IST)


def check_token(authorization: Optional[str], token: Optional[str]):
    if not RIGA_ACTION_TOKEN:
        return
    if authorization == f"Bearer {RIGA_ACTION_TOKEN}":
        return
    if token == RIGA_ACTION_TOKEN:
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def get_client():
    global client_obj

    if client_obj:
        return client_obj

    if not all([ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PASSWORD, ANGEL_TOTP_SECRET]):
        raise HTTPException(status_code=500, detail="Missing Angel credentials in Render/.env")

    client = SmartConnect(api_key=ANGEL_API_KEY)
    totp = pyotp.TOTP(ANGEL_TOTP_SECRET.strip().replace(" ", "").upper()).now()
    session = client.generateSession(ANGEL_CLIENT_CODE, ANGEL_PASSWORD, totp)

    if not session or not session.get("status"):
        raise HTTPException(status_code=500, detail=f"Angel login failed: {session}")

    client_obj = client
    return client


def load_scrip_master():
    global scrip_master_cache

    if scrip_master_cache:
        return scrip_master_cache

    res = requests.get(SCRIP_MASTER_URL, timeout=25)
    res.raise_for_status()
    scrip_master_cache = res.json()
    return scrip_master_cache


# -----------------------------
# Time / Market Data
# -----------------------------

def last_trading_date(dt: datetime) -> datetime:
    d = dt
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def candle_window_ist():
    n = now_ist()
    d = last_trading_date(n)

    start = d.replace(hour=9, minute=15, second=0, microsecond=0)
    end = d.replace(hour=15, minute=30, second=0, microsecond=0)

    if n < start:
        d = last_trading_date(d - timedelta(days=1))
        start = d.replace(hour=9, minute=15, second=0, microsecond=0)
        end = d.replace(hour=15, minute=30, second=0, microsecond=0)
        return start, end

    if n > end:
        return start, end

    return start, n


def market_session_status() -> Dict[str, Any]:
    n = now_ist()
    today_open = n.replace(hour=9, minute=15, second=0, microsecond=0)
    today_close = n.replace(hour=15, minute=30, second=0, microsecond=0)

    is_weekend = n.weekday() >= 5
    is_open = (not is_weekend) and today_open <= n <= today_close

    if is_weekend:
        status = "MARKET_CLOSED_WEEKEND"
    elif n < today_open:
        status = "MARKET_NOT_OPEN_YET"
    elif n > today_close:
        status = "MARKET_CLOSED_AFTER_HOURS"
    else:
        status = "MARKET_OPEN"

    return {
        "status": status,
        "is_open": is_open,
        "server_time_ist": n.strftime("%Y-%m-%d %H:%M:%S IST"),
        "today_open_ist": today_open.strftime("%Y-%m-%d %H:%M:%S IST"),
        "today_close_ist": today_close.strftime("%Y-%m-%d %H:%M:%S IST"),
    }


def parse_candle_time(value: Any) -> Optional[datetime]:
    if not value:
        return None

    s = str(value)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=IST)
        except Exception:
            continue

    return None


def is_candle_data_fresh(candles: List[Dict[str, Any]], max_stale_minutes: int = 20) -> Tuple[bool, str]:
    session = market_session_status()
    if not session["is_open"]:
        return False, session["status"]

    if not candles:
        return False, "NO_CANDLES"

    last_dt = parse_candle_time(candles[-1].get("time"))
    if not last_dt:
        return False, "CANDLE_TIME_PARSE_FAILED"

    age_min = (now_ist() - last_dt).total_seconds() / 60.0
    if age_min > max_stale_minutes:
        return False, f"STALE_CANDLE_DATA_{round(age_min, 1)}_MIN_OLD"

    return True, "FRESH"


def normalize_interval(interval: str) -> str:
    allowed = {
        "ONE_MINUTE",
        "THREE_MINUTE",
        "FIVE_MINUTE",
        "TEN_MINUTE",
        "FIFTEEN_MINUTE",
        "THIRTY_MINUTE",
        "ONE_HOUR",
        "ONE_DAY",
    }
    interval = (interval or "FIVE_MINUTE").upper()
    return interval if interval in allowed else "FIVE_MINUTE"


def get_ltp(client, item: Dict[str, Any]):
    try:
        res = client.ltpData(item["exchange"], item["tradingsymbol"], str(item["symboltoken"]))
    except Exception as e:
        print("LTP ERROR:", item, str(e))
        return None

    if not res or not res.get("status"):
        print("LTP FAILED:", item, res)
        return None

    d = res.get("data", {}) or {}
    return {
        "symbol": item["tradingsymbol"],
        "exchange": item["exchange"],
        "token": str(item["symboltoken"]),
        "ltp": d.get("ltp"),
        "open": d.get("open"),
        "high": d.get("high"),
        "low": d.get("low"),
        "close": d.get("close"),
    }


def get_candles(client, exchange: str, symboltoken: str, interval: str = "FIVE_MINUTE"):
    interval = normalize_interval(interval)
    start, end = candle_window_ist()

    params = {
        "exchange": exchange,
        "symboltoken": str(symboltoken),
        "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }

    try:
        res = client.getCandleData(params)
    except Exception as e:
        print("CANDLE ERROR:", params, str(e))
        return []

    if not res or not res.get("status"):
        print("CANDLE FAILED:", params, res)
        return []

    candles = []
    for row in res.get("data", []) or []:
        try:
            candles.append({
                "time": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
            })
        except Exception:
            continue

    return candles


def get_candles_debug(client, exchange: str, symboltoken: str, interval: str = "FIVE_MINUTE"):
    interval = normalize_interval(interval)
    start, end = candle_window_ist()

    params = {
        "exchange": exchange,
        "symboltoken": str(symboltoken),
        "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }

    try:
        res = client.getCandleData(params)
    except Exception as e:
        return {"params": params, "error": str(e), "count": 0, "sample": []}

    data = res.get("data", []) if isinstance(res, dict) else []
    return {
        "params": params,
        "status": res.get("status") if isinstance(res, dict) else None,
        "message": res.get("message") if isinstance(res, dict) else None,
        "count": len(data or []),
        "sample": data[-3:] if data else [],
    }


def fetch_option_candles_debug(client, opt: Dict[str, Any], interval: str = "FIVE_MINUTE") -> Dict[str, Any]:
    dbg = get_candles_debug(client, opt.get("exchange", "NFO"), str(opt.get("symboltoken")), interval=interval)
    dbg["symbol"] = opt.get("tradingsymbol")
    dbg["strike"] = opt.get("strike")
    dbg["type"] = opt.get("type")
    return dbg


# -----------------------------
# Technical Helpers
# -----------------------------

def safe_num(value):
    return isinstance(value, (int, float)) and value is not None


def round_to_step(price, step):
    return int(round(price / step) * step)


def parse_expiry(expiry):
    for fmt in ("%d%b%Y", "%d%b%y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(expiry).upper(), fmt)
        except Exception:
            pass
    return None


def avg(values: List[float]) -> float:
    vals = [v for v in values if safe_num(v)]
    return sum(vals) / len(vals) if vals else 0.0


def pct_change(a: float, b: float) -> float:
    if not b:
        return 0.0
    return ((a - b) / b) * 100


def candle_body(c):
    return abs(c["close"] - c["open"])


def candle_range(c):
    return max(c["high"] - c["low"], 0.01)


def candle_strength(c):
    return candle_body(c) / candle_range(c)


def candle_position(c):
    return (c["close"] - c["low"]) / candle_range(c)


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def is_bull_candle(c):
    return c["close"] > c["open"]


def is_bear_candle(c):
    return c["close"] < c["open"]


def calc_vwap(candles: List[Dict[str, Any]]):
    if not candles:
        return None

    total_pv = 0.0
    total_v = 0.0
    typical_prices = []

    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        typical_prices.append(tp)
        v = c.get("volume", 0) or 0
        if v > 0:
            total_pv += tp * v
            total_v += v

    if total_v > 0:
        return total_pv / total_v

    return sum(typical_prices) / len(typical_prices)


def find_swing_levels(candles: List[Dict[str, Any]], lookback: int = 20):
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if not recent:
        return None
    prev = recent[:-1] if len(recent) > 1 else recent
    return {
        "swing_high": max(c["high"] for c in recent),
        "swing_low": min(c["low"] for c in recent),
        "prev_high": max(c["high"] for c in prev),
        "prev_low": min(c["low"] for c in prev),
    }


def volume_spike(candles: List[Dict[str, Any]], lookback: int = 20):
    if len(candles) < 5:
        return False, 0.0, False

    last_v = candles[-1].get("volume", 0) or 0
    prev_vols = [c.get("volume", 0) or 0 for c in candles[-lookback - 1:-1]]
    positive_prev = [v for v in prev_vols if v > 0]

    if last_v <= 0 or not positive_prev:
        return False, 0.0, False

    base = avg(positive_prev)
    if base <= 0:
        return False, 0.0, False

    ratio = last_v / base
    return ratio >= 1.25, round(ratio, 2), True


def classify_candle(c):
    strength = candle_strength(c)
    pos = candle_position(c)

    if strength >= 0.70 and pos >= 0.75 and is_bull_candle(c):
        return "A_PLUS_BULL"
    if strength >= 0.60 and pos >= 0.65 and is_bull_candle(c):
        return "A_BULL"
    if strength >= 0.45 and pos >= 0.60 and is_bull_candle(c):
        return "B_BULL"

    if strength >= 0.70 and pos <= 0.25 and is_bear_candle(c):
        return "A_PLUS_BEAR"
    if strength >= 0.60 and pos <= 0.35 and is_bear_candle(c):
        return "A_BEAR"
    if strength >= 0.45 and pos <= 0.40 and is_bear_candle(c):
        return "B_BEAR"

    return "LOW_QUALITY"


def trap_filter(c):
    body = max(candle_body(c), 0.01)
    uw = upper_wick(c)
    lw = lower_wick(c)
    pos = candle_position(c)

    if candle_strength(c) < 0.35:
        return True, "weak body / indecision"
    if uw > body * 1.8 and pos < 0.75:
        return True, "upper wick rejection / fake breakout risk"
    if lw > body * 1.8 and pos > 0.25:
        return True, "lower wick rejection / fake breakdown risk"

    return False, "no liquidity trap"


# -----------------------------
# Book Knowledge Engine v11.3
# Extracted as rule logic from uploaded trading books/PDFs.
# Important: this is not a book text dump; it is executable knowledge:
# candlestick context, chart-pattern activation, scalping filters,
# trade-location/retest logic, false-breakout checks, and risk discipline.
# -----------------------------

BOOK_KNOWLEDGE_VERSION = "RIGA_BOOK_ENGINE_11.3"

BOOK_KNOWLEDGE_SOURCES = {
    "candlesticks": [
        "Hammer / Inverted Hammer / Dragonfly Doji at support = bullish reversal context",
        "Bullish Engulfing / Piercing / Morning Star / Bullish Kicker = bullish reversal confirmation",
        "Bullish Marubozu / Three White Soldiers / Rising Three Methods = bullish continuation",
        "Hanging Man / Shooting Star / Dark Cloud Cover at resistance = bearish reversal context",
        "Bearish Engulfing / Evening Star / Bearish Kicker = bearish reversal confirmation",
        "Bearish Marubozu / Three Black Crows / Falling Three Methods = bearish continuation",
        "Candles are valid only with trend + level + volume/context confirmation",
    ],
    "patterns": [
        "Pattern is not active until breakout/breakdown candle closes beyond level",
        "False breakout: breakout then fast return inside range; failed breakout/trap: breakout opposite direction",
        "Rectangles, triangles, wedges, flags, channels, double top/bottom, H&S require level confirmation",
        "Targets are measured from pattern height; stops must sit beyond invalidation level with buffer",
    ],
    "scalping": [
        "1/3/5 min focus; trend filter first; use EMA/SMA/VWAP direction with momentum confirmation",
        "EMA trend + stochastic recovery/overbought rejection + price pullback to average = higher quality scalp",
        "MACD/Stochastic/Bollinger rules act as support filters, never standalone trade reason",
        "Every scalp must have SL and realistic targets; exit quickly when setup fails",
    ],
    "trade_location": [
        "Best entries come from retest/throwback/pullback to VWAP, EMA, support/resistance, or breakout level",
        "Avoid chasing far from VWAP or day range; wait for price to come to your level",
        "Ambush/retest entries need high confluence and clear invalidation",
    ],
    "risk": [
        "No forced trade; sometimes NO TRADE is best trade",
        "Protective stop is mandatory before entry",
        "Minimum 1:2 style reward potential preferred; avoid wide/noisy SL",
        "Avoid emotional/FOMO/revenge trades and limit number of trades",
    ],
}


def ema(values: List[float], period: int) -> Optional[float]:
    vals = [float(v) for v in values if safe_num(v)]
    if len(vals) < period:
        return None
    k = 2 / (period + 1)
    e = sum(vals[:period]) / period
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e


def sma(values: List[float], period: int) -> Optional[float]:
    vals = [float(v) for v in values if safe_num(v)]
    if len(vals) < period:
        return None
    return sum(vals[-period:]) / period


def calc_rsi(candles: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    gains, losses = [], []
    closes = [float(c["close"]) for c in candles]
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = avg(gains)
    avg_loss = avg(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calc_stochastic(candles: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(candles) < period:
        return None
    recent = candles[-period:]
    high = max(c["high"] for c in recent)
    low = min(c["low"] for c in recent)
    if high == low:
        return None
    return round(((candles[-1]["close"] - low) / (high - low)) * 100, 2)


def calc_bollinger(candles: List[Dict[str, Any]], period: int = 20, mult: float = 2.0) -> Optional[Dict[str, float]]:
    if len(candles) < period:
        return None
    closes = [float(c["close"]) for c in candles[-period:]]
    mid = sum(closes) / period
    var = sum((x - mid) ** 2 for x in closes) / period
    sd = var ** 0.5
    return {"upper": round(mid + mult * sd, 2), "middle": round(mid, 2), "lower": round(mid - mult * sd, 2)}


def calc_macd(candles: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    closes = [float(c["close"]) for c in candles]
    if len(closes) < 26:
        return None
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    if e12 is None or e26 is None:
        return None
    macd_line = e12 - e26
    # lightweight signal proxy using recent close momentum around MACD line
    return {"macd": round(macd_line, 4), "histogram": round(macd_line, 4)}


def near_level(price: float, level: Optional[float], tolerance_pct: float = 0.45) -> bool:
    if not safe_num(price) or not safe_num(level) or not level:
        return False
    return abs(float(price) - float(level)) / float(level) * 100 <= tolerance_pct


def detect_candlestick_patterns(candles: List[Dict[str, Any]], levels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Detects high/medium quality candle patterns with context.
    Returns bullish/bearish score, pattern names, and rejection context.
    """
    out = {"bull_score": 0, "bear_score": 0, "bullish": [], "bearish": [], "neutral": []}
    if not candles:
        return out

    c1 = candles[-1]
    c2 = candles[-2] if len(candles) >= 2 else None
    c3 = candles[-3] if len(candles) >= 3 else None
    body = max(candle_body(c1), 0.01)
    rng = candle_range(c1)
    pos = candle_position(c1)
    lw = lower_wick(c1)
    uw = upper_wick(c1)
    strength = candle_strength(c1)

    support = levels.get("prev_low") if levels else None
    resistance = levels.get("prev_high") if levels else None
    at_support = near_level(c1["low"], support, 0.65) or (levels and c1["low"] <= levels.get("swing_low", c1["low"]) * 1.006)
    at_resistance = near_level(c1["high"], resistance, 0.65) or (levels and c1["high"] >= levels.get("swing_high", c1["high"]) * 0.994)

    # Single-candle reversal / continuation knowledge
    if is_bull_candle(c1) and lw >= body * 1.8 and pos >= 0.62:
        name = "Hammer/Dragonfly rejection" if at_support else "Bullish long-lower-wick rejection"
        out["bullish"].append(name)
        out["bull_score"] += 14 if at_support else 8
    if is_bear_candle(c1) and uw >= body * 1.8 and pos <= 0.38:
        name = "Shooting Star/Hanging Man rejection" if at_resistance else "Bearish long-upper-wick rejection"
        out["bearish"].append(name)
        out["bear_score"] += 14 if at_resistance else 8
    if is_bull_candle(c1) and strength >= 0.78 and uw <= body * 0.25 and lw <= body * 0.35:
        out["bullish"].append("Bullish Marubozu momentum")
        out["bull_score"] += 16
    if is_bear_candle(c1) and strength >= 0.78 and uw <= body * 0.35 and lw <= body * 0.25:
        out["bearish"].append("Bearish Marubozu momentum")
        out["bear_score"] += 16

    # Two-candle patterns
    if c2:
        if is_bear_candle(c2) and is_bull_candle(c1) and c1["open"] <= c2["close"] and c1["close"] >= c2["open"]:
            out["bullish"].append("Bullish Engulfing")
            out["bull_score"] += 18 if at_support else 12
        if is_bull_candle(c2) and is_bear_candle(c1) and c1["open"] >= c2["close"] and c1["close"] <= c2["open"]:
            out["bearish"].append("Bearish Engulfing")
            out["bear_score"] += 18 if at_resistance else 12
        if is_bear_candle(c2) and is_bull_candle(c1) and c1["close"] > (c2["open"] + c2["close"]) / 2 and c1["open"] < c2["close"]:
            out["bullish"].append("Piercing Line")
            out["bull_score"] += 10
        if is_bull_candle(c2) and is_bear_candle(c1) and c1["close"] < (c2["open"] + c2["close"]) / 2 and c1["open"] > c2["close"]:
            out["bearish"].append("Dark Cloud Cover")
            out["bear_score"] += 12
        if abs(c1["low"] - c2["low"]) / max(c1["low"], 0.01) * 100 <= 0.15 and is_bull_candle(c1):
            out["bullish"].append("Tweezer Bottom")
            out["bull_score"] += 8
        if abs(c1["high"] - c2["high"]) / max(c1["high"], 0.01) * 100 <= 0.15 and is_bear_candle(c1):
            out["bearish"].append("Tweezer Top")
            out["bear_score"] += 8

    # Three-candle patterns
    if c2 and c3:
        small_mid = candle_body(c2) <= candle_range(c2) * 0.35
        if is_bear_candle(c3) and small_mid and is_bull_candle(c1) and c1["close"] > (c3["open"] + c3["close"]) / 2:
            out["bullish"].append("Morning Star / Morning Doji Star")
            out["bull_score"] += 18
        if is_bull_candle(c3) and small_mid and is_bear_candle(c1) and c1["close"] < (c3["open"] + c3["close"]) / 2:
            out["bearish"].append("Evening Star / Evening Doji Star")
            out["bear_score"] += 18
        last3 = candles[-3:]
        if all(is_bull_candle(x) and candle_strength(x) >= 0.5 for x in last3) and c1["close"] > c2["close"] > c3["close"]:
            out["bullish"].append("Three White Soldiers")
            out["bull_score"] += 16
        if all(is_bear_candle(x) and candle_strength(x) >= 0.5 for x in last3) and c1["close"] < c2["close"] < c3["close"]:
            out["bearish"].append("Three Black Crows")
            out["bear_score"] += 16

    if not out["bullish"] and not out["bearish"]:
        out["neutral"].append("No strong named candlestick pattern")
    return out


def detect_chart_patterns(candles: List[Dict[str, Any]], levels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Lightweight executable chart-pattern recognition for live API candles."""
    out = {"bull_score": 0, "bear_score": 0, "patterns": [], "activation": None, "target_hint": None, "invalidations": []}
    if len(candles) < 12:
        return out
    recent = candles[-20:] if len(candles) >= 20 else candles
    last = candles[-1]
    prev = candles[-2]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]
    closes = [c["close"] for c in recent]
    resistance = max(highs[:-1]) if len(highs) > 1 else max(highs)
    support = min(lows[:-1]) if len(lows) > 1 else min(lows)
    height = max(resistance - support, 0.01)
    range_pct = height / max(last["close"], 0.01) * 100

    # Breakout / breakdown activation: close beyond level, not merely wick.
    if last["close"] > resistance and prev["close"] <= resistance:
        out["patterns"].append("Activated resistance breakout")
        out["activation"] = "BULLISH_BREAKOUT_CLOSE"
        out["bull_score"] += 22
        out["target_hint"] = round(resistance + height, 2)
        out["invalidations"].append(round(resistance, 2))
    if last["close"] < support and prev["close"] >= support:
        out["patterns"].append("Activated support breakdown")
        out["activation"] = "BEARISH_BREAKDOWN_CLOSE"
        out["bear_score"] += 22
        out["target_hint"] = round(support - height, 2)
        out["invalidations"].append(round(support, 2))

    # Rectangle / congestion breakout context.
    if 0.35 <= range_pct <= 4.5:
        touches_hi = sum(1 for h in highs[:-1] if abs(h - resistance) / resistance * 100 <= 0.35)
        touches_lo = sum(1 for l in lows[:-1] if abs(l - support) / support * 100 <= 0.35)
        if touches_hi >= 2 and touches_lo >= 2:
            out["patterns"].append("Rectangle / horizontal congestion")
            if out["activation"] == "BULLISH_BREAKOUT_CLOSE":
                out["bull_score"] += 10
            elif out["activation"] == "BEARISH_BREAKDOWN_CLOSE":
                out["bear_score"] += 10

    # Compression: triangles/wedges often precede expansion; needs breakout close.
    first_half = recent[:len(recent)//2]
    second_half = recent[len(recent)//2:]
    if first_half and second_half:
        first_range = max(c["high"] for c in first_half) - min(c["low"] for c in first_half)
        second_range = max(c["high"] for c in second_half) - min(c["low"] for c in second_half)
        if first_range > 0 and second_range < first_range * 0.72:
            out["patterns"].append("Triangle/Wedge volatility compression")
            if out["activation"] == "BULLISH_BREAKOUT_CLOSE":
                out["bull_score"] += 8
            elif out["activation"] == "BEARISH_BREAKDOWN_CLOSE":
                out["bear_score"] += 8

    # Double top / bottom logic.
    if len(recent) >= 10:
        highs_idx = sorted(range(len(recent)), key=lambda i: recent[i]["high"], reverse=True)[:2]
        lows_idx = sorted(range(len(recent)), key=lambda i: recent[i]["low"])[:2]
        if len(highs_idx) == 2:
            h1, h2 = recent[highs_idx[0]]["high"], recent[highs_idx[1]]["high"]
            if abs(h1 - h2) / max(h1, 0.01) * 100 <= 0.35 and abs(highs_idx[0] - highs_idx[1]) >= 3:
                out["patterns"].append("Double Top risk near resistance")
                if is_bear_candle(last) and near_level(last["high"], max(h1, h2), 0.50):
                    out["bear_score"] += 10
        if len(lows_idx) == 2:
            l1, l2 = recent[lows_idx[0]]["low"], recent[lows_idx[1]]["low"]
            if abs(l1 - l2) / max(l1, 0.01) * 100 <= 0.35 and abs(lows_idx[0] - lows_idx[1]) >= 3:
                out["patterns"].append("Double Bottom support base")
                if is_bull_candle(last) and near_level(last["low"], min(l1, l2), 0.50):
                    out["bull_score"] += 10

    # Channel / flag continuation proxy.
    if len(closes) >= 8:
        slope = closes[-1] - closes[-8]
        pullback_small = abs(closes[-1] - closes[-4]) < abs(closes[-4] - closes[-8]) if len(closes) >= 8 else False
        if slope > 0 and pullback_small and last["close"] > prev["high"]:
            out["patterns"].append("Bull flag/channel continuation trigger")
            out["bull_score"] += 9
        if slope < 0 and pullback_small and last["close"] < prev["low"]:
            out["patterns"].append("Bear flag/channel continuation trigger")
            out["bear_score"] += 9

    if not out["patterns"]:
        out["patterns"].append("No activated chart pattern")
    return out


def false_breakout_filter(candles: List[Dict[str, Any]], levels: Optional[Dict[str, Any]], side: str) -> Tuple[bool, str]:
    if len(candles) < 3 or not levels:
        return False, "no false-breakout evidence"
    last = candles[-1]
    prev = candles[-2]
    resistance = levels.get("prev_high")
    support = levels.get("prev_low")

    if side == "CE" and resistance:
        if prev["high"] > resistance and prev["close"] < resistance and last["close"] < resistance:
            return True, "false bullish breakout returned below resistance"
        if last["high"] > resistance and last["close"] < resistance:
            return True, "intrabar bullish breakout failed to close above resistance"
    if side == "PE" and support:
        if prev["low"] < support and prev["close"] > support and last["close"] > support:
            return True, "false bearish breakdown returned above support"
        if last["low"] < support and last["close"] > support:
            return True, "intrabar bearish breakdown failed to close below support"
    return False, "no false-breakout evidence"


def trade_location_filter(entry: float, candles: List[Dict[str, Any]], vwap: Optional[float], levels: Optional[Dict[str, Any]], side: str) -> Tuple[bool, str, int]:
    """Book rule: negotiate entry; prefer retest/throwback near VWAP/SR, reject chase."""
    if not candles:
        return False, "no candles for trade-location check", 0
    last = candles[-1]
    score = 0
    reasons = []
    if vwap:
        dist = abs(entry - vwap) / vwap * 100
        if dist <= 1.8:
            score += 8
            reasons.append("near VWAP value area")
        elif dist > CHASE_VWAP_HARD_REJECT_PCT:
            return False, f"chase rejected: premium {round(dist,2)}% away from VWAP", score
    if levels:
        if side == "CE":
            ref_levels = [levels.get("prev_high"), levels.get("swing_low"), vwap]
            if any(near_level(last["low"], x, 0.75) for x in ref_levels if x):
                score += 10
                reasons.append("throwback/retest trade location")
        else:
            ref_levels = [levels.get("prev_low"), levels.get("swing_high"), vwap]
            if any(near_level(last["high"], x, 0.75) for x in ref_levels if x):
                score += 10
                reasons.append("pullback/retest trade location")
    if not reasons:
        reasons.append("acceptable but not ideal trade location")
    return True, ", ".join(reasons), score


def scalping_indicator_score(candles: List[Dict[str, Any]], side: str, vwap: Optional[float]) -> Dict[str, Any]:
    out = {"score": 0, "reasons": [], "rsi": None, "stochastic": None, "ema_fast": None, "ema_slow": None, "bollinger": None, "macd": None}
    if len(candles) < 8:
        return out
    closes = [float(c["close"]) for c in candles]
    fast = ema(closes, 5) or ema(closes, min(5, len(closes)))
    mid = ema(closes, 9) or sma(closes, min(9, len(closes)))
    slow = ema(closes, 21) if len(closes) >= 21 else sma(closes, min(12, len(closes)))
    stoch = calc_stochastic(candles, min(14, len(candles)))
    rsi = calc_rsi(candles, min(14, max(2, len(candles)-1)))
    bb = calc_bollinger(candles, min(20, len(candles)))
    macd = calc_macd(candles)
    last = candles[-1]
    out.update({"rsi": rsi, "stochastic": stoch, "ema_fast": round(fast, 2) if fast else None, "ema_slow": round(slow, 2) if slow else None, "bollinger": bb, "macd": macd})

    if side == "CE":
        if fast and slow and fast > slow and last["close"] >= fast:
            out["score"] += 10
            out["reasons"].append("EMA trend aligned bullish")
        if vwap and last["close"] > vwap:
            out["score"] += 8
            out["reasons"].append("above VWAP intraday")
        if stoch is not None and 20 <= stoch <= 85:
            out["score"] += 6
            out["reasons"].append("stochastic in bullish usable zone")
        if rsi is not None and 45 <= rsi <= 72:
            out["score"] += 6
            out["reasons"].append("RSI momentum healthy")
        if bb and last["close"] > bb["middle"] and last["close"] < bb["upper"] * 1.01:
            out["score"] += 5
            out["reasons"].append("Bollinger middle support/expansion")
    else:
        if fast and slow and fast < slow and last["close"] <= fast:
            out["score"] += 10
            out["reasons"].append("EMA trend aligned bearish")
        if vwap and last["close"] < vwap:
            out["score"] += 8
            out["reasons"].append("below VWAP intraday")
        if stoch is not None and 15 <= stoch <= 80:
            out["score"] += 6
            out["reasons"].append("stochastic in bearish usable zone")
        if rsi is not None and 28 <= rsi <= 55:
            out["score"] += 6
            out["reasons"].append("RSI bearish momentum healthy")
        if bb and last["close"] < bb["middle"] and last["close"] > bb["lower"] * 0.99:
            out["score"] += 5
            out["reasons"].append("Bollinger middle rejection/expansion")
    return out


def book_knowledge_score(candles: List[Dict[str, Any]], side: str, vwap: Optional[float], levels: Optional[Dict[str, Any]], entry: Optional[float] = None) -> Dict[str, Any]:
    """Combines PDF/book knowledge into a single score block for RIGA."""
    if not candles or side not in ["CE", "PE"]:
        return {"score": 0, "reasons": [], "candles": {}, "chart": {}, "scalping": {}, "filters": []}

    candle_info = detect_candlestick_patterns(candles, levels)
    chart_info = detect_chart_patterns(candles, levels)
    scalp_info = scalping_indicator_score(candles, side, vwap)
    failed, fail_reason = false_breakout_filter(candles, levels, side)
    loc_ok, loc_reason, loc_score = trade_location_filter(float(entry or candles[-1]["close"]), candles, vwap, levels, side)

    score = 0
    reasons = []
    if side == "CE":
        score += min(candle_info.get("bull_score", 0), 22)
        if candle_info.get("bullish"):
            reasons.append("candlestick: " + "; ".join(candle_info["bullish"][:3]))
        score += min(chart_info.get("bull_score", 0), 24)
    else:
        score += min(candle_info.get("bear_score", 0), 22)
        if candle_info.get("bearish"):
            reasons.append("candlestick: " + "; ".join(candle_info["bearish"][:3]))
        score += min(chart_info.get("bear_score", 0), 24)

    if chart_info.get("activation"):
        reasons.append("chart pattern activated: " + chart_info.get("activation"))
    elif any("No activated" not in p for p in chart_info.get("patterns", [])):
        reasons.append("chart context: " + "; ".join(chart_info.get("patterns", [])[:2]))

    score += min(scalp_info.get("score", 0), 24)
    if scalp_info.get("reasons"):
        reasons.append("scalping filters: " + "; ".join(scalp_info["reasons"][:3]))

    if loc_ok:
        score += min(loc_score, 12)
        reasons.append("trade location: " + loc_reason)
    else:
        score -= 20
        reasons.append(loc_reason)

    filters = []
    if failed:
        score -= 35
        filters.append(fail_reason)
        reasons.append("false breakout filter: " + fail_reason)

    # If both bullish and bearish pattern scores are strong, reduce confidence: conflicting context.
    if candle_info.get("bull_score", 0) >= 12 and candle_info.get("bear_score", 0) >= 12:
        score -= 10
        filters.append("conflicting bullish/bearish candlestick context")

    return {
        "score": max(0, min(int(score), 60)),
        "reasons": reasons,
        "candles": candle_info,
        "chart": chart_info,
        "scalping": scalp_info,
        "filters": filters,
    }


# -----------------------------
# Option Chain
# -----------------------------

def get_auto_option_chain(index_name, spot_price, strikes_around=3):
    index_name = index_name.upper()

    if index_name not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Use NIFTY, BANKNIFTY, FINNIFTY, SENSEX")

    cfg = INDEX_CONFIG[index_name]
    master = load_scrip_master()
    atm = round_to_step(spot_price, cfg["step"])
    allowed = {atm + i * cfg["step"] for i in range(-strikes_around, strikes_around + 1)}
    today = now_ist().date()
    found = []

    for s in master:
        try:
            if s.get("name") != cfg["option_name"]:
                continue
            if s.get("exch_seg") != cfg["option_exchange"]:
                continue
            if s.get("instrumenttype") != "OPTIDX":
                continue

            symbol = s.get("symbol", "")
            if not (symbol.endswith("CE") or symbol.endswith("PE")):
                continue

            strike = int(float(s.get("strike", 0)) / 100)
            if strike not in allowed:
                continue

            expiry_dt = parse_expiry(s.get("expiry"))
            if not expiry_dt or expiry_dt.date() < today:
                continue

            found.append({
                "exchange": cfg["option_exchange"],
                "tradingsymbol": symbol,
                "symboltoken": str(s.get("token")),
                "strike": strike,
                "type": "CE" if symbol.endswith("CE") else "PE",
                "expiry": s.get("expiry"),
                "expiry_dt": expiry_dt,
            })
        except Exception:
            continue

    if not found:
        return atm, None, []

    nearest = min(x["expiry_dt"] for x in found)
    options = [x for x in found if x["expiry_dt"] == nearest]
    for x in options:
        x.pop("expiry_dt", None)

    options.sort(key=lambda x: (abs(x["strike"] - atm), x["strike"], x["type"]))
    return atm, nearest.strftime("%d%b%Y").upper(), options


# -----------------------------
# RIGA Logic
# -----------------------------

def analyze_index_structure(index_name: str, spot_data: Dict[str, Any], candles: List[Dict[str, Any]]):
    if not candles or len(candles) < MIN_CANDLES:
        ltp = spot_data.get("ltp")
        close = spot_data.get("close")

        if safe_num(ltp) and safe_num(close):
            chg = pct_change(ltp, close)
            if chg >= 0.12:
                return {
                    "bias": "BULLISH",
                    "option_side": "CE",
                    "score": 55,
                    "candle_count": len(candles or []),
                    "reason": f"fallback bullish vs close {round(chg, 2)}%",
                    "bull_score": 55,
                    "bear_score": 0,
                    "transition_scan": False,
                    "transition_sides": ["CE"],
                }
            if chg <= -0.12:
                return {
                    "bias": "BEARISH",
                    "option_side": "PE",
                    "score": 55,
                    "candle_count": len(candles or []),
                    "reason": f"fallback bearish vs close {round(chg, 2)}%",
                    "bull_score": 0,
                    "bear_score": 55,
                    "transition_scan": False,
                    "transition_sides": ["PE"],
                }

        return {
            "bias": "NEUTRAL",
            "option_side": None,
            "score": 0,
            "candle_count": len(candles or []),
            "reason": "not enough index candle data",
            "bull_score": 0,
            "bear_score": 0,
            "transition_scan": False,
            "transition_sides": [],
        }

    last = candles[-1]
    levels = find_swing_levels(candles, 20)
    vwap = calc_vwap(candles)
    vol_ok, vol_ratio, vol_available = volume_spike(candles)

    last_close = last["close"]
    prev_close = candles[-2]["close"]
    first_open = candles[0]["open"]

    score_bull = 0
    score_bear = 0
    bull = []
    bear = []

    intraday_change = pct_change(last_close, first_open)
    last_momentum = pct_change(last_close, prev_close)

    if intraday_change > 0.12:
        score_bull += 20
        bull.append(f"index intraday bullish {round(intraday_change, 2)}%")
    if intraday_change < -0.12:
        score_bear += 20
        bear.append(f"index intraday bearish {round(intraday_change, 2)}%")

    if vwap:
        if last_close > vwap:
            score_bull += 15
            bull.append("index above VWAP")
        elif last_close < vwap:
            score_bear += 15
            bear.append("index below VWAP")

    if levels and last_close > levels["prev_high"]:
        score_bull += 25
        bull.append("index breakout above swing high")
    if levels and last_close < levels["prev_low"]:
        score_bear += 25
        bear.append("index breakdown below swing low")

    cq = classify_candle(last)
    if cq in ["A_PLUS_BULL", "A_BULL"]:
        score_bull += 15
        bull.append(f"index {cq}")
    if cq in ["A_PLUS_BEAR", "A_BEAR"]:
        score_bear += 15
        bear.append(f"index {cq}")

    if last_momentum > 0.03:
        score_bull += 10
        bull.append("last candle bullish momentum")
    if last_momentum < -0.03:
        score_bear += 10
        bear.append("last candle bearish momentum")

    if vol_ok:
        score_bull += 5
        score_bear += 5
        bull.append(f"volume spike {vol_ratio}x")
        bear.append(f"volume spike {vol_ratio}x")

    trap, trap_reason = trap_filter(last)
    if trap:
        score_bull -= 20
        score_bear -= 20

    base = {
        "vwap": round(vwap, 2) if vwap else None,
        "candle_count": len(candles),
        "last_candle": last,
        "trap": trap_reason if trap else "no trap",
        "volume_available": vol_available,
    }

    if score_bull >= 55 and score_bull > score_bear:
        return {
            **base,
            "bias": "BULLISH",
            "option_side": "CE",
            "score": min(score_bull, 95),
            "reason": ", ".join(bull),
            "bull_score": score_bull,
            "bear_score": score_bear,
            "transition_scan": False,
            "transition_sides": ["CE"],
        }

    if score_bear >= 55 and score_bear > score_bull:
        return {
            **base,
            "bias": "BEARISH",
            "option_side": "PE",
            "score": min(score_bear, 95),
            "reason": ", ".join(bear),
            "bull_score": score_bull,
            "bear_score": score_bear,
            "transition_scan": False,
            "transition_sides": ["PE"],
        }

    neutral_score = max(score_bull, score_bear)
    transition_sides = []

    if neutral_score >= TRANSITION_SCAN_MIN_SCORE:
        if abs(score_bull - score_bear) <= 15:
            transition_sides = ["CE", "PE"]
        elif score_bull > score_bear:
            transition_sides = ["CE"]
        else:
            transition_sides = ["PE"]
    elif PREMIUM_LED_SCAN_ALWAYS:
        transition_sides = ["CE", "PE"]

    return {
        **base,
        "bias": "NEUTRAL",
        "option_side": None,
        "score": neutral_score,
        "bull_score": score_bull,
        "bear_score": score_bear,
        "transition_scan": bool(transition_sides),
        "transition_sides": transition_sides,
        "reason": "premium-led transition scan enabled; option premium must confirm" if transition_sides else "index structure not clean enough",
    }


def analyze_option_buy_setup(index_bias, opt, ltp_data, candles, atm, index_name):
    side = index_bias.get("option_side") or index_bias.get("forced_option_side")

    if side not in ["CE", "PE"]:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "index side unavailable"}
    if opt.get("type") != side:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "option side not aligned"}
    if not ltp_data or not safe_num(ltp_data.get("ltp")):
        return {"bias": "NO TRADE", "confidence": 0, "reason": "no option LTP"}
    if not candles or len(candles) < MIN_CANDLES:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "not enough option candle data", "candle_count": len(candles or [])}

    opt_fresh, opt_freshness_reason = is_candle_data_fresh(candles, max_stale_minutes=18)
    if not opt_fresh:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": f"option candle data not fresh: {opt_freshness_reason}",
            "candle_count": len(candles or []),
        }

    last = candles[-1]
    prev = candles[-2]
    levels = find_swing_levels(candles, 20)
    vwap = calc_vwap(candles)
    vol_ok, vol_ratio, vol_available = volume_spike(candles)

    entry = float(ltp_data["ltp"])
    last_close = float(last["close"])

    # Live option premiums move fast. Keep a chase guard, but do not reject
    # valid setups for tiny LTP/candle-close mismatch.
    ltp_distance_pct = abs(entry - last_close) / last_close * 100 if last_close else 999
    if ltp_distance_pct > LTP_SIGNAL_DISTANCE_MAX_PCT:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "entry": round(entry, 2),
            "signal_close": round(last_close, 2),
            "ltp_distance_pct": round(ltp_distance_pct, 2),
            "reason": f"LTP too far from signal candle close >{LTP_SIGNAL_DISTANCE_MAX_PCT}%; stale/chase entry rejected",
            "candle_count": len(candles),
        }

    if not levels:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "no swing levels"}

    idx_score = int(index_bias.get("score", 0) or 0)
    score = 0
    reasons = [index_bias.get("reason", "")]

    if idx_score >= 85:
        score += 20
    elif idx_score >= 70:
        score += 16
    elif idx_score >= 55:
        score += 10
    elif index_bias.get("transition_scan") and idx_score >= TRANSITION_SCAN_MIN_SCORE:
        score += 5
    else:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "reason": "index score too weak for fresh option buying setup",
            "index_score": idx_score,
            "candle_count": len(candles),
        }

    # Book/PDF knowledge engine adds named candlestick, chart-pattern, scalping,
    # trade-location, and false-breakout intelligence without weakening RIGA rules.
    book_block = book_knowledge_score(candles, side, vwap, levels, entry=entry)
    book_score = int(book_block.get("score", 0) or 0)
    if book_score > 0:
        score += min(book_score, 35)
        reasons.extend(book_block.get("reasons", [])[:4])
    if book_block.get("filters"):
        return {
            "bias": "NO TRADE",
            "confidence": max(0, score - 20),
            "book_score": book_score,
            "book_filters": book_block.get("filters"),
            "book_knowledge": book_block,
            "reason": "; ".join(book_block.get("filters", [])),
            "candle_count": len(candles),
        }

    book_reason_text = " ".join(str(x) for x in book_block.get("reasons", []))
    if "chase rejected" in book_reason_text.lower():
        return {
            "bias": "NO TRADE",
            "confidence": max(0, score - 15),
            "book_score": book_score,
            "book_knowledge": book_block,
            "reason": "premium too extended from VWAP / chase rejected; wait for pullback/retest",
            "candle_count": len(candles),
        }

    cq = classify_candle(last)
    strength = candle_strength(last)
    mom = pct_change(last["close"], prev["close"])
    body = max(candle_body(last), 0.01)

    if mom >= 3.0 and strength >= 0.75:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "candle": cq,
            "candle_strength": round(strength, 2),
            "momentum_pct": round(mom, 2),
            "reason": "late momentum chase rejected; wait for retest",
        }

    if candle_strength(last) < 0.28:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "candle": cq,
            "trap_filter": "weak body / indecision",
            "reason": "trap rejected: weak body / indecision",
            "candle_count": len(candles),
        }

    if upper_wick(last) > body * 2.6 and candle_position(last) < 0.62:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "candle": cq,
            "trap_filter": "upper wick rejection / fake breakout risk",
            "reason": "trap rejected: upper wick rejection / fake breakout risk",
            "candle_count": len(candles),
        }

    # OPTION PREMIUM EXECUTION FILTER
    # For both BUY CE and BUY PE, option premium itself must show bullish strength.
    # This accepts only ATM/near-ATM option premium support/retest/throwback holds.
    retest_candidates = []
    if vwap:
        retest_candidates.append(("VWAP_RETEST_HOLD", float(vwap)))
    if levels.get("prev_high"):
        retest_candidates.append(("PREV_SWING_HIGH_THROWBACK", float(levels["prev_high"])))
    if levels.get("prev_low"):
        retest_candidates.append(("PREV_SWING_LOW_SUPPORT", float(levels["prev_low"])))
    if levels.get("swing_low"):
        retest_candidates.append(("RECENT_SWING_LOW_SUPPORT", float(levels["swing_low"])))

    retest_level_name = None
    retest_level = None
    for name, level in retest_candidates:
        if level <= 0:
            continue

        # Slightly wider tolerance because option premiums have spread/noise.
        tolerance = 0.015

        touched_or_swept = last["low"] <= level * (1 + tolerance)
        reclaimed = last["close"] >= level
        bullish_acceptance = (
            is_bull_candle(last)
            or last["close"] > prev["high"]
            or candle_position(last) >= 0.65
            or lower_wick(last) >= body * 0.25
        )

        # Avoid accepting a close far below the retest level.
        held_level = touched_or_swept and reclaimed and bullish_acceptance

        if held_level:
            retest_level_name = name
            retest_level = level
            break

    momentum_breakout = False
    if not retest_level:
        breakout_ref = float(levels.get("prev_high") or 0)
        vwap_ok = (not vwap) or last["close"] >= vwap
        breakout_ok = (
            MOMENTUM_BREAKOUT_ALLOW
            and breakout_ref > 0
            and last["close"] > breakout_ref
            and candle_position(last) >= 0.72
            and is_bull_candle(last)
            and (vol_ok or candle_position(last) >= 0.78)
            and 0.12 <= mom <= MOMENTUM_BREAKOUT_MAX_PREMIUM_PCT
            and vwap_ok
            and not index_bias.get("fallback_mode")
        )

        if breakout_ok:
            momentum_breakout = True
            retest_level_name = "MOMENTUM_BREAKOUT_ABOVE_PREV_HIGH"
            retest_level = breakout_ref
            reasons.append("momentum breakout mode: premium breakout above previous swing high with volume")
            score += 24
        else:
            return {
                "bias": "NO TRADE",
                "confidence": score,
                "reason": "no support/retest/throwback hold on ATM/near-ATM option premium",
                "premium_vwap": round(vwap, 2) if vwap else None,
                "prev_swing_high": round(levels.get("prev_high"), 2) if levels.get("prev_high") else None,
                "prev_swing_low": round(levels.get("prev_low"), 2) if levels.get("prev_low") else None,
                "recent_swing_low": round(levels.get("swing_low"), 2) if levels.get("swing_low") else None,
                "last_low": round(last["low"], 2),
                "last_close": round(last["close"], 2),
                "candle_count": len(candles),
            }

    pattern = "PREMIUM_BREAKOUT_MOMENTUM" if momentum_breakout else "PREMIUM_RETEST_HOLD"
    if not momentum_breakout:
        score += 30
        reasons.append(f"ATM/near-ATM option premium support/retest hold at {retest_level_name}")

    if vwap:
        distance_from_vwap = pct_change(entry, vwap)
        if distance_from_vwap > CHASE_VWAP_HARD_REJECT_PCT:
            return {
                "bias": "NO TRADE",
                "confidence": score,
                "pattern": pattern,
                "premium_vwap": round(vwap, 2),
                "distance_from_vwap_pct": round(distance_from_vwap, 2),
                "reason": "premium too extended from VWAP / chase rejected",
            }

    if cq == "A_PLUS_BULL":
        score += 18
        reasons.append("A+ bullish premium candle")
    elif cq == "A_BULL":
        score += 14
        reasons.append("A grade bullish premium candle")
    elif cq == "B_BULL":
        score += 8
        reasons.append("B bullish premium candle with retest confirmation")
    elif candle_position(last) >= 0.65 and last["close"] >= retest_level:
        score += 6
        reasons.append("premium acceptance candle after retest")
    else:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "pattern": pattern,
            "candle": cq,
            "reason": "option premium candle quality not strong enough after retest/support hold",
        }

    if 0.15 < mom < 3.0:
        score += 10
        reasons.append(f"controlled premium momentum {round(mom, 2)}%")
    elif 0.05 < mom <= 0.15:
        score += 5
        reasons.append(f"mild premium momentum {round(mom, 2)}%")

    if vwap and last["close"] > vwap:
        score += 8
        reasons.append("premium accepted above VWAP")

    if vol_ok:
        score += 8
        reasons.append(f"volume expansion {vol_ratio}x")
    else:
        reasons.append("volume not confirmed")

    step = INDEX_CONFIG[index_name]["step"]
    atm_distance = abs(opt["strike"] - atm)

    if atm_distance == 0:
        score += 10
        reasons.append("ATM strike")
    elif atm_distance <= step:
        score += 6
        reasons.append("near ATM strike")
    else:
        return {"bias": "NO TRADE", "confidence": score, "atm_distance": atm_distance, "reason": "far from ATM rejected for option buying"}

    buffer = max(entry * DEFAULT_BUFFER_PCT, 2.0)
    structure_sl = min(levels["swing_low"], levels["prev_low"]) - buffer
    risk = round(entry - structure_sl, 2)
    risk_pct = (risk / entry) * 100 if entry else 999

    if structure_sl <= 0 or structure_sl >= entry:
        return {"bias": "NO TRADE", "confidence": score, "reason": "invalid structure SL"}
    if risk_pct > MAX_RISK_PCT:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "entry": round(entry, 2),
            "sl": round(structure_sl, 2),
            "risk_pct": round(risk_pct, 2),
            "reason": f"risk too wide for option buying >{MAX_RISK_PCT}%",
        }
    if risk_pct < MIN_RISK_PCT:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "entry": round(entry, 2),
            "sl": round(structure_sl, 2),
            "risk_pct": round(risk_pct, 2),
            "reason": "risk too tight / noise SL",
        }

    t1 = round(entry + risk * 1.5, 2)
    t2 = round(entry + risk * 2.0, 2)
    t3 = round(entry + risk * 3.0, 2)

    if not (structure_sl < entry < t1 < t2 < t3):
        return {"bias": "NO TRADE", "confidence": score, "reason": "RR structure invalid"}

    day_high = max(c["high"] for c in candles)
    if t1 > day_high * 1.15:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "entry": round(entry, 2),
            "target_t1": t1,
            "day_high": round(day_high, 2),
            "reason": "target too far above current option day high / RR unrealistic",
        }

    raw_score = score
    if idx_score < 70:
        confidence = min(raw_score, 75)
    elif idx_score < 85:
        confidence = min(raw_score, 85)
    else:
        confidence = min(raw_score, 95)

    if confidence < CONFIDENCE_MIN:
        return {
            "bias": "NO TRADE",
            "confidence": confidence,
            "raw_score": raw_score,
            "index_score": idx_score,
            "pattern": pattern,
            "candle": cq,
            "reason": f"RIGA sniper score below {CONFIDENCE_MIN}",
        }

    if FINAL_REQUIRE_FRESH_INDEX and index_bias.get("fallback_mode"):
        return {
            "bias": "NO TRADE",
            "confidence": min(confidence, 69),
            "raw_score": raw_score,
            "index_score": idx_score,
            "pattern": pattern,
            "candle": cq,
            "entry": round(entry, 2),
            "sl": round(structure_sl, 2),
            "targets": {"t1": t1, "t2": t2, "t3": t3},
            "reason": "final trade rejected because index candles are stale/unavailable; keep as watchlist only",
        }

    return {
        "bias": "BUY_CE" if side == "CE" else "BUY_PE",
        "entry": round(entry, 2),
        "sl": round(structure_sl, 2),
        "target": t2,
        "targets": {"t1": t1, "t2": t2, "t3": t3},
        "risk": risk,
        "risk_pct": round(risk_pct, 2),
        "confidence": confidence,
        "raw_score": raw_score,
        "index_score": idx_score,
        "pattern": pattern,
        "retest_level": retest_level_name,
        "retest_price": round(retest_level, 2),
        "candle": cq,
        "candle_strength": round(strength, 2),
        "momentum_pct": round(mom, 2),
        "premium_vwap": round(vwap, 2) if vwap else None,
        "volume_spike": vol_ratio if vol_ok else None,
        "volume_available": vol_available,
        "atm_distance": atm_distance,
        "book_score": book_score,
        "book_knowledge": book_block,
        "book_patterns": {
            "candlestick": book_block.get("candles", {}),
            "chart": book_block.get("chart", {}),
            "scalping": book_block.get("scalping", {}),
        },
        "trap_filter": "no trap",
        "candle_count": len(candles),
        "day_high": round(day_high, 2),
        "reason": ", ".join([r for r in reasons if r]),
    }



def make_watchlist_candidate(index_name: str, opt: Dict[str, Any], ltp_data: Optional[Dict[str, Any]], candles: List[Dict[str, Any]], atm: int, index_bias: Dict[str, Any], signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Create a developing setup so fast markets do not look like plain NO TRADE.

    This is NOT a final trade. It gives trigger/retest/SL levels to watch.
    Final trade still requires /quickScan or /rigaScan confirmed BUY_CE/BUY_PE.
    """
    if index_name in BLACKLISTED_INDEXES:
        return None
    if not ltp_data or not safe_num(ltp_data.get("ltp")):
        return None
    if not candles or len(candles) < MIN_CANDLES:
        return None

    side = opt.get("type")
    if side not in ["CE", "PE"]:
        return None

    levels = find_swing_levels(candles, 20)
    if not levels:
        return None

    entry_ltp = float(ltp_data["ltp"])
    last = candles[-1]
    prev = candles[-2]
    vwap = calc_vwap(candles)
    vol_ok, vol_ratio, _ = volume_spike(candles)
    cq = classify_candle(last)
    mom = pct_change(last["close"], prev["close"])
    step = INDEX_CONFIG[index_name]["step"]
    atm_distance = abs(opt["strike"] - atm)

    # Only ATM/near-ATM developing ideas.
    if atm_distance > step:
        return None

    score = int(signal.get("confidence", 0) or 0)
    reasons = []
    if signal.get("reason"):
        reasons.append(str(signal.get("reason")))

    idx_score = int(index_bias.get("score", 0) or 0)
    score = max(score, min(idx_score, 60))

    if cq == "A_PLUS_BULL":
        score += 16
        reasons.append("developing A+ bullish premium candle")
    elif cq == "A_BULL":
        score += 12
        reasons.append("developing A bullish premium candle")
    elif cq == "B_BULL" or candle_position(last) >= 0.62:
        score += 8
        reasons.append("premium candle position improving")

    if vwap and last["close"] >= vwap:
        score += 8
        reasons.append("premium above VWAP")
    elif vwap and abs(entry_ltp - vwap) / vwap * 100 <= 2.0:
        score += 5
        reasons.append("premium near VWAP value area")

    if vol_ok:
        score += 8
        reasons.append(f"volume expansion {vol_ratio}x")

    if 0.05 <= mom <= 3.0:
        score += 6
        reasons.append(f"premium momentum {round(mom, 2)}%")

    if atm_distance == 0:
        score += 8
        reasons.append("ATM strike")
    else:
        score += 5
        reasons.append("near ATM strike")

    # Trigger logic: option premium itself must break and then hold.
    prev_high = float(levels.get("prev_high") or last["high"])
    trigger = max(prev_high, last["high"], entry_ltp)
    trigger = round(trigger * 1.005, 2)

    retest_hold = None
    if vwap and vwap > 0:
        retest_hold = max(float(vwap), float(levels.get("prev_low") or 0))
    else:
        retest_hold = float(levels.get("prev_low") or 0)
    retest_hold = round(retest_hold, 2) if retest_hold and retest_hold > 0 else None

    buffer = max(entry_ltp * DEFAULT_BUFFER_PCT, 2.0)
    structure_sl = min(float(levels.get("swing_low") or entry_ltp), float(levels.get("prev_low") or entry_ltp)) - buffer
    if structure_sl <= 0 or structure_sl >= entry_ltp:
        return None

    risk = round(entry_ltp - structure_sl, 2)
    risk_pct = (risk / entry_ltp) * 100 if entry_ltp else 999
    if risk_pct > MAX_RISK_PCT * 1.25:
        # Wide risk watchlist is not useful; level may be too late.
        return None

    t1 = round(entry_ltp + risk * 1.5, 2)
    t2 = round(entry_ltp + risk * 2.0, 2)

    # Developing confidence should not be presented as final confidence.
    confidence = max(DEVELOPING_MIN_CONFIDENCE, min(int(score), 69 if signal.get("bias") != "BUY_CE" and signal.get("bias") != "BUY_PE" else 74))
    if confidence < DEVELOPING_MIN_CONFIDENCE:
        return None

    return {
        "index": index_name,
        "symbol": opt.get("tradingsymbol"),
        "strike": opt.get("strike"),
        "option_type": side,
        "option_trade": "BUY CE" if side == "CE" else "BUY PE",
        "ltp": round(entry_ltp, 2),
        "watch_entry_above": trigger,
        "retest_hold_zone": retest_hold,
        "sl_below": round(structure_sl, 2),
        "targets_if_triggered": {"t1": t1, "t2": t2},
        "confidence": confidence,
        "atm_distance": atm_distance,
        "premium_vwap": round(vwap, 2) if vwap else None,
        "reason": "; ".join(reasons[:5]) if reasons else "developing premium setup; wait for breakout + retest hold",
        "rule": "WATCHLIST ONLY: enter only after trigger breakout + retest hold; no direct chase",
    }


def select_best_watchlist(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not candidates:
        return []

    def key(x: Dict[str, Any]):
        return (
            int(x.get("confidence", 0) or 0),
            -int(x.get("atm_distance", 9999) or 9999),
            float(x.get("ltp", 0) or 0),
        )

    unique = {}
    for c in sorted(candidates, key=key, reverse=True):
        sym = c.get("symbol")
        if sym and sym not in unique:
            unique[sym] = c
    return list(unique.values())[:WATCHLIST_MAX_ITEMS]

def select_best_trade(trades):
    if not trades:
        return None

    def score_key(t):
        sig = t["signal"]
        return (
            sig.get("confidence", 0),
            -sig.get("atm_distance", 9999),
            -sig.get("risk_pct", 99),
        )

    return sorted(trades, key=score_key, reverse=True)[0]


# -----------------------------
# Core scan functions
# -----------------------------

def build_index_candle_fallback_bias(index: str, spot_data: Dict[str, Any], candles: List[Dict[str, Any]], freshness_reason: str) -> Dict[str, Any]:
    ltp = spot_data.get("ltp")
    close = spot_data.get("close")
    transition_sides = ["CE", "PE"]
    score = 35
    direction_note = "spot direction unavailable"

    if safe_num(ltp) and safe_num(close) and close:
        chg = pct_change(float(ltp), float(close))
        direction_note = f"spot vs previous close {round(chg, 2)}%"

        if chg >= 0.12:
            transition_sides = ["CE"]
            score = 45 if chg < 0.40 else 50
        elif chg <= -0.12:
            transition_sides = ["PE"]
            score = 45 if chg > -0.40 else 50

    return {
        "bias": "NEUTRAL",
        "option_side": None,
        "score": score,
        "bull_score": score if transition_sides == ["CE"] else 0,
        "bear_score": score if transition_sides == ["PE"] else 0,
        "candle_count": len(candles or []),
        "last_candle": candles[-1] if candles else None,
        "trap": "index candle unavailable",
        "volume_available": False,
        "transition_scan": True,
        "transition_sides": transition_sides,
        "fallback_mode": True,
        "freshness_reason": freshness_reason,
        "reason": f"index candles unavailable/stale ({freshness_reason}); {direction_note}; premium-led ATM/near-ATM option scan enabled",
        "index": index,
    }


def scan_one_index(index: str, strikes_around: int = 3, interval: str = "FIVE_MINUTE", debug: bool = False):
    index = index.upper()

    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    if index in BLACKLISTED_INDEXES:
        return {
            "index": index,
            "spot_ltp": None,
            "spot_close": None,
            "index_bias": {
                "bias": "BLACKLISTED",
                "option_side": None,
                "score": 0,
                "reason": f"{index} manually blacklisted after repeated weak/loss signals",
                "index": index,
            },
            "atm": None,
            "nearest_expiry": None,
            "index_candle_fresh": False,
            "freshness_reason": "BLACKLISTED",
            "total_options_found": 0,
            "side_options_found": 0,
            "ltp_checked": 0,
            "candle_requests": 0,
            "options_with_candles": 0,
            "options_without_candles": 0,
            "options_with_insufficient_candles": 0,
            "total_options_scanned": 0,
            "trade_count": 0,
            "best_trade": "NO TRADE",
            "watchlist": [],
            "rejected": [{"reason": f"{index} blacklisted"}],
            "reason": f"{index} manually blacklisted",
        }

    client = get_client()
    cfg = INDEX_CONFIG[index]
    spot_item = cfg["spot"]

    spot_data = get_ltp(client, spot_item)
    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    hist_token = spot_item.get("hist_token", spot_item["symboltoken"])
    index_candles = get_candles(client, spot_item["exchange"], hist_token, interval=interval)
    fresh, freshness_reason = is_candle_data_fresh(index_candles)

    atm, expiry, options = get_auto_option_chain(index, float(spot_data["ltp"]), strikes_around)
    total_options_found = len(options)
    option_debug_summary = {
        "total_options_found": total_options_found,
        "side_options_found": 0,
        "ltp_checked": 0,
        "candle_requests": 0,
        "options_with_candles": 0,
        "options_without_candles": 0,
        "options_with_insufficient_candles": 0,
    }

    if not fresh:
        session = market_session_status()
        if not session["is_open"]:
            return {
                "index": index,
                "spot_ltp": spot_data["ltp"],
                "spot_close": spot_data.get("close"),
                "index_bias": {
                    "bias": "MARKET_CLOSED",
                    "option_side": None,
                    "score": 0,
                    "candle_count": len(index_candles or []),
                    "last_candle": index_candles[-1] if index_candles else None,
                    "reason": freshness_reason,
                    "index": index,
                },
                "atm": atm,
                "nearest_expiry": expiry,
                "index_candle_fresh": fresh,
                "freshness_reason": freshness_reason,
                **option_debug_summary,
                "total_options_scanned": 0,
                "trade_count": 0,
                "best_trade": "NO TRADE",
            }
        index_bias = build_index_candle_fallback_bias(index, spot_data, index_candles, freshness_reason)
    else:
        index_bias = analyze_index_structure(index, spot_data, index_candles)
        index_bias["fallback_mode"] = False
        index_bias["freshness_reason"] = freshness_reason

    index_bias["index"] = index
    side = index_bias.get("option_side")
    transition_sides = index_bias.get("transition_sides", []) if index_bias.get("transition_scan") else []

    if side in ["CE", "PE"]:
        sides_to_scan = [side]
    elif transition_sides:
        sides_to_scan = transition_sides
    else:
        return {
            "index": index,
            "spot_ltp": spot_data["ltp"],
            "spot_close": spot_data.get("close"),
            "index_bias": index_bias,
            "atm": atm,
            "nearest_expiry": expiry,
            **option_debug_summary,
            "total_options_scanned": 0,
            "trade_count": 0,
            "best_trade": "NO TRADE",
            "reason": "index neutral, option scan skipped",
        }

    max_distance = INDEX_CONFIG[index]["step"] * (TRANSITION_STRIKES_AROUND if side not in ["CE", "PE"] else MAX_SCAN_STRIKES_AROUND)
    side_options = [
        opt for opt in options
        if opt["type"] in sides_to_scan and abs(opt["strike"] - atm) <= max_distance
    ]
    option_debug_summary["side_options_found"] = len(side_options)

    trades = []
    watchlist_candidates = []
    rejected = []
    scanned = 0

    for opt in side_options:
        symbol = opt.get("tradingsymbol")
        symboltoken = opt.get("symboltoken")

        if not symboltoken:
            rejected.append({"symbol": symbol, "strike": opt.get("strike"), "type": opt.get("type"), "reason": "NO_SYMBOL_TOKEN", "confidence": 0, "candle_count": 0})
            continue

        option_debug_summary["ltp_checked"] += 1
        ltp_data = get_ltp(client, opt)

        option_debug_summary["candle_requests"] += 1
        opt_candles = get_candles(client, opt["exchange"], symboltoken, interval=interval)
        candle_count = len(opt_candles or [])

        if candle_count <= 0:
            option_debug_summary["options_without_candles"] += 1
        elif candle_count < MIN_CANDLES:
            option_debug_summary["options_with_insufficient_candles"] += 1
        else:
            option_debug_summary["options_with_candles"] += 1
            scanned += 1

        effective_index_bias = dict(index_bias)
        if effective_index_bias.get("option_side") not in ["CE", "PE"]:
            effective_index_bias["forced_option_side"] = opt.get("type")
            effective_index_bias["bias"] = "BULLISH" if opt.get("type") == "CE" else "BEARISH"
            effective_index_bias["transition_scan"] = True
            effective_index_bias["score"] = max(int(effective_index_bias.get("score", 0) or 0), TRANSITION_SCAN_MIN_SCORE)
            effective_index_bias["reason"] = f"transition scan; option premium confirmation required; {effective_index_bias.get('reason', '')}"

        signal = analyze_option_buy_setup(effective_index_bias, opt, ltp_data, opt_candles, atm, index)

        final_signal_ok = (
            signal.get("bias") in ["BUY_CE", "BUY_PE"]
            and signal.get("confidence", 0) >= CONFIDENCE_MIN
            and not effective_index_bias.get("fallback_mode")
            and index not in BLACKLISTED_INDEXES
            and "chase rejected" not in str(signal.get("reason", "")).lower()
        )

        if final_signal_ok:
            trades.append({
                "index": index,
                "option": opt,
                "data": ltp_data,
                "signal": signal,
                "atm_distance": signal.get("atm_distance", abs(opt["strike"] - atm)),
            })
        else:
            candidate = make_watchlist_candidate(index, opt, ltp_data, opt_candles, atm, effective_index_bias, signal)
            if candidate:
                watchlist_candidates.append(candidate)

            # v11.5: always keep a compact rejection summary so NO TRADE is explainable.
            rejected.append({
                "symbol": symbol,
                "strike": opt.get("strike"),
                "type": opt.get("type"),
                "ltp": ltp_data.get("ltp") if isinstance(ltp_data, dict) else None,
                "reason": signal.get("reason"),
                "confidence": signal.get("confidence", 0),
                "candle_count": signal.get("candle_count", candle_count),
            })

    best_trade = select_best_trade(trades)
    watchlist = select_best_watchlist(watchlist_candidates)
    out = {
        "index": index,
        "spot_ltp": spot_data["ltp"],
        "spot_close": spot_data.get("close"),
        "index_bias": index_bias,
        "atm": atm,
        "nearest_expiry": expiry,
        "index_candle_fresh": fresh,
        "freshness_reason": freshness_reason,
        **option_debug_summary,
        "total_options_scanned": scanned,
        "trade_count": len(trades),
        "watchlist_count": len(watchlist),
        "best_trade": best_trade if best_trade else "NO TRADE",
        "watchlist": watchlist,
        "rejected_summary": rejected[:REJECTED_SUMMARY_MAX_ITEMS],
    }

    if debug:
        out["rejected"] = rejected[:50]

    return out


# -----------------------------
# Request Models
# -----------------------------

class SpotPriceRequest(BaseModel):
    index: str = "NIFTY"
    token: Optional[str] = None


class OptionChainRequest(BaseModel):
    index: str = "NIFTY"
    strikes_around: int = 3
    include_premium: bool = False
    token: Optional[str] = None


class ScanOptionsRequest(BaseModel):
    index: str = "NIFTY"
    strikes_around: int = 3
    interval: str = "FIVE_MINUTE"
    debug: bool = False
    token: Optional[str] = None


class ScanAllMarketsRequest(BaseModel):
    strikes_around: int = 3
    interval: str = "FIVE_MINUTE"
    debug: bool = False
    token: Optional[str] = None


class MarketPremiumsRequest(BaseModel):
    index: Optional[str] = None
    strikes_around: int = 1
    token: Optional[str] = None


class CandleTestRequest(BaseModel):
    index: str = "NIFTY"
    interval: str = "FIVE_MINUTE"
    token: Optional[str] = None


class OptionCandleTestRequest(BaseModel):
    index: str = "NIFTY"
    strike: Optional[int] = None
    side: Side = "CE"
    interval: str = "FIVE_MINUTE"
    token: Optional[str] = None


# -----------------------------
# API Routes
# -----------------------------

@app.get("/")
def root():
    return {
        "name": "RIGA AI Option Buying Scanner",
        "version": APP_VERSION,
        "status": "ok",
        "rule": "Bullish -> BUY_CE, Bearish -> BUY_PE, otherwise NO TRADE",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "server_time": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "market_session": market_session_status(),
        "routes": [
            "/getSpotPrice",
            "/getOptionChain",
            "/marketPremiums",
            "/scanOptions",
            "/scanAllMarkets",
            "/quickScan",
            "/quickScanText",
            "/rigaScan",
            "/knowledge",
            "/candles-test",
            "/option-candles-test",
        ],
    }




@app.get("/knowledge")
def knowledge_engine(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)
    return {
        "status": "ok",
        "version": APP_VERSION,
        "book_engine": BOOK_KNOWLEDGE_VERSION,
        "knowledge_sources": BOOK_KNOWLEDGE_SOURCES,
        "active_rules": [
            "Pattern must activate by breakout/breakdown close",
            "Candlestick must match context: trend + support/resistance + volume/momentum",
            "Retest/throwback/VWAP trade location preferred",
            "False breakout and trap rejection active",
            "Scalping indicators are confirmation only, not standalone signals",
            "Final output remains BUY_CE, BUY_PE, or NO TRADE",
        ],
    }

@app.get("/candles-test")
def candles_test(
    index: str = Query("NIFTY"),
    interval: str = Query("FIVE_MINUTE"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)
    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")
    client = get_client()
    item = INDEX_CONFIG[index]["spot"]
    hist_token = item.get("hist_token", item["symboltoken"])
    return get_candles_debug(client, item["exchange"], hist_token, interval)


@app.get("/option-candles-test")
def option_candles_test(
    index: str = Query("NIFTY"),
    strike: Optional[int] = Query(None),
    side: Side = Query("CE"),
    interval: str = Query("FIVE_MINUTE"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)
    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])
    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    atm, expiry, options = get_auto_option_chain(index, float(spot_data["ltp"]), strikes_around=6)
    wanted_strike = strike or atm

    match = None
    for opt in options:
        if opt.get("strike") == wanted_strike and opt.get("type") == side:
            match = opt
            break

    if not match:
        return {
            "index": index,
            "atm": atm,
            "nearest_expiry": expiry,
            "requested": {"strike": wanted_strike, "side": side},
            "found": False,
            "available": [
                {
                    "tradingsymbol": o.get("tradingsymbol"),
                    "strike": o.get("strike"),
                    "type": o.get("type"),
                    "symboltoken": o.get("symboltoken"),
                    "expiry": o.get("expiry"),
                }
                for o in options
            ],
        }

    return fetch_option_candles_debug(client, match, interval=interval)


@app.get("/getSpotPrice")
def get_spot_price(
    index: str = Query("NIFTY"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)
    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")
    client = get_client()
    return get_ltp(client, INDEX_CONFIG[index]["spot"])


@app.get("/spot")
def spot_alias(
    index: str = Query("NIFTY"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    return get_spot_price(index=index, authorization=authorization, token=token)


@app.get("/getOptionChain")
def get_option_chain(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    include_premium: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)
    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])
    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    atm, expiry, options = get_auto_option_chain(index, float(spot_data["ltp"]), strikes_around)
    if include_premium:
        for opt in options:
            opt["premium"] = get_ltp(client, opt)

    return {
        "index": index,
        "spot": spot_data,
        "atm": atm,
        "nearest_expiry": expiry,
        "options_count": len(options),
        "options": options,
    }


@app.get("/option-chain")
def option_chain_alias(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    include_premium: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    return get_option_chain(index=index, strikes_around=strikes_around, include_premium=include_premium, authorization=authorization, token=token)


def _premium_payload(opt: Dict[str, Any], premium: Optional[Dict[str, Any]], atm: int) -> Dict[str, Any]:
    p = premium or {}
    ltp = p.get("ltp") if isinstance(p, dict) else None
    close = p.get("close") if isinstance(p, dict) else None

    change = None
    change_pct = None
    if safe_num(ltp) and safe_num(close) and close:
        change = round(float(ltp) - float(close), 2)
        change_pct = round(pct_change(float(ltp), float(close)), 2)

    return {
        "symbol": opt.get("tradingsymbol"),
        "exchange": opt.get("exchange"),
        "token": str(opt.get("symboltoken")),
        "strike": opt.get("strike"),
        "type": opt.get("type"),
        "expiry": opt.get("expiry"),
        "atm_distance": abs(int(opt.get("strike", 0)) - int(atm)),
        "ltp": ltp,
        "open": p.get("open") if isinstance(p, dict) else None,
        "high": p.get("high") if isinstance(p, dict) else None,
        "low": p.get("low") if isinstance(p, dict) else None,
        "close": close,
        "change": change,
        "change_pct": change_pct,
    }


def _market_premiums_for_index(client, index: str, strikes_around: int = 1) -> Dict[str, Any]:
    index = index.upper()
    if index not in INDEX_CONFIG:
        return {"status": "error", "index": index, "reason": "Invalid index"}

    spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])
    if not spot_data or not safe_num(spot_data.get("ltp")):
        return {"status": "error", "index": index, "reason": "Spot data failed"}

    atm, expiry, options = get_auto_option_chain(index, float(spot_data["ltp"]), strikes_around)
    rows = []
    for opt in options:
        premium = get_ltp(client, opt)
        rows.append(_premium_payload(opt, premium, atm))

    rows.sort(key=lambda x: (x["atm_distance"], x["strike"], 0 if x["type"] == "CE" else 1))
    atm_ce = next((x for x in rows if x["strike"] == atm and x["type"] == "CE"), None)
    atm_pe = next((x for x in rows if x["strike"] == atm and x["type"] == "PE"), None)

    return {
        "status": "ok",
        "index": index,
        "spot_ltp": spot_data.get("ltp"),
        "spot_close": spot_data.get("close"),
        "atm": atm,
        "nearest_expiry": expiry,
        "atm_ce": atm_ce,
        "atm_pe": atm_pe,
        "options_count": len(rows),
        "options": rows,
    }


@app.get("/marketPremiums")
def market_premiums(
    index: Optional[str] = Query(None),
    strikes_around: int = Query(1),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)
    client = get_client()
    strikes_around = max(0, min(int(strikes_around), 6))
    indexes = [index.upper()] if index else ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
    markets = {}

    for idx in indexes:
        markets[idx] = _market_premiums_for_index(client, idx, strikes_around)

    return {
        "status": "ok",
        "version": APP_VERSION,
        "server_time": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "market_session": market_session_status(),
        "strikes_around": strikes_around,
        "markets": markets,
    }


@app.get("/market-premiums")
def market_premiums_alias(
    index: Optional[str] = Query(None),
    strikes_around: int = Query(1),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    return market_premiums(index=index, strikes_around=strikes_around, authorization=authorization, token=token)


@app.get("/scanOptions")
def scan_options(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    debug: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)
    return scan_one_index(index=index, strikes_around=strikes_around, interval=interval, debug=debug)


@app.get("/scan-options")
def scan_options_alias(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    debug: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    return scan_options(index=index, strikes_around=strikes_around, interval=interval, debug=debug, authorization=authorization, token=token)


def empty_trade_payload(reason: str = "No valid setup") -> Dict[str, Any]:
    return {
        "trade_available": False,
        "result": "NO TRADE",
        "bias": "NEUTRAL",
        "option_trade": None,
        "index": None,
        "symbol": None,
        "strike": None,
        "entry": None,
        "sl": None,
        "targets": None,
        "confidence": 0,
        "reason": reason,
    }


def trade_to_payload(trade: Any) -> Dict[str, Any]:
    if not isinstance(trade, dict):
        return empty_trade_payload()

    sig = trade.get("signal", {}) or {}
    opt = trade.get("option", {}) or {}
    bias = sig.get("bias")

    if bias not in ["BUY_CE", "BUY_PE"]:
        return empty_trade_payload(sig.get("reason", "No valid setup"))

    return {
        "trade_available": True,
        "result": bias,
        "bias": "Bullish" if bias == "BUY_CE" else "Bearish",
        "option_trade": "BUY CE" if bias == "BUY_CE" else "BUY PE",
        "index": trade.get("index"),
        "symbol": opt.get("tradingsymbol"),
        "strike": opt.get("strike"),
        "option_type": opt.get("type"),
        "expiry": opt.get("expiry"),
        "entry": sig.get("entry"),
        "sl": sig.get("sl"),
        "targets": sig.get("targets"),
        "target": sig.get("target"),
        "risk": sig.get("risk"),
        "risk_pct": sig.get("risk_pct"),
        "confidence": sig.get("confidence"),
        "pattern": sig.get("pattern"),
        "candle": sig.get("candle"),
        "momentum_pct": sig.get("momentum_pct"),
        "premium_vwap": sig.get("premium_vwap"),
        "atm_distance": sig.get("atm_distance"),
        "book_score": sig.get("book_score"),
        "book_patterns": sig.get("book_patterns"),
        "reason": sig.get("reason"),
    }


@app.get("/scanAllMarkets")
def scan_all_markets(
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    debug: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)
    markets: Dict[str, Any] = {}
    valid_trades: List[Dict[str, Any]] = []
    all_watchlist: List[Dict[str, Any]] = []

    for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
        try:
            res = scan_one_index(index=idx, strikes_around=strikes_around, interval=interval, debug=debug)
            raw_best = res.get("best_trade")
            best_payload = trade_to_payload(raw_best)

            if best_payload.get("trade_available") and idx not in BLACKLISTED_INDEXES:
                valid_trades.append(raw_best)

            for w in res.get("watchlist", []) or []:
                if w.get("index") not in BLACKLISTED_INDEXES:
                    all_watchlist.append(w)

            index_bias = res.get("index_bias") or {}
            markets[idx] = {
                "status": "ok",
                "spot_ltp": res.get("spot_ltp"),
                "spot_close": res.get("spot_close"),
                "bias": index_bias.get("bias"),
                "index_score": index_bias.get("score"),
                "index_reason": index_bias.get("reason"),
                "atm": res.get("atm"),
                "nearest_expiry": res.get("nearest_expiry"),
                "index_candle_fresh": res.get("index_candle_fresh"),
                "freshness_reason": res.get("freshness_reason"),
                "total_options_found": res.get("total_options_found"),
                "side_options_found": res.get("side_options_found"),
                "candle_requests": res.get("candle_requests"),
                "options_with_candles": res.get("options_with_candles"),
                "options_without_candles": res.get("options_without_candles"),
                "options_with_insufficient_candles": res.get("options_with_insufficient_candles"),
                "total_options_scanned": res.get("total_options_scanned"),
                "trade_count": res.get("trade_count"),
                "watchlist_count": res.get("watchlist_count", 0),
                "best_trade": best_payload,
                "watchlist": res.get("watchlist", []),
                "rejected_summary": res.get("rejected_summary", []),
            }

            if debug and res.get("rejected"):
                markets[idx]["rejected"] = res.get("rejected")[:20]

        except Exception as exc:
            markets[idx] = {
                "status": "error",
                "spot_ltp": None,
                "spot_close": None,
                "bias": "ERROR",
                "index_score": 0,
                "index_reason": str(exc),
                "atm": None,
                "nearest_expiry": None,
                "index_candle_fresh": False,
                "freshness_reason": "ERROR",
                "total_options_found": 0,
                "side_options_found": 0,
                "candle_requests": 0,
                "options_with_candles": 0,
                "options_without_candles": 0,
                "options_with_insufficient_candles": 0,
                "total_options_scanned": 0,
                "trade_count": 0,
                "best_trade": empty_trade_payload(str(exc)),
            }

    raw_overall_best = select_best_trade(valid_trades)
    overall_payload = trade_to_payload(raw_overall_best)
    overall_watchlist = select_best_watchlist(all_watchlist)

    return {
        "status": "ok",
        "version": APP_VERSION,
        "trade_available": overall_payload.get("trade_available", False),
        "result": overall_payload.get("result", "NO TRADE"),
        "overall_best_trade": overall_payload,
        "watchlist": overall_watchlist,
        "watchlist_count": len(overall_watchlist),
        "blacklisted_indexes": sorted(list(BLACKLISTED_INDEXES)),
        "markets": markets,
    }


@app.get("/quickScan")
def quick_scan(
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    data = scan_all_markets(strikes_around=strikes_around, interval=interval, debug=True, authorization=authorization, token=token)
    best = data.get("overall_best_trade") or empty_trade_payload()
    return {
        "status": data.get("status", "ok"),
        "version": data.get("version", APP_VERSION),
        "trade_available": best.get("trade_available", False),
        "result": best.get("result", "NO TRADE"),
        "bias": best.get("bias"),
        "option_trade": best.get("option_trade"),
        "index": best.get("index"),
        "symbol": best.get("symbol"),
        "strike": best.get("strike"),
        "option_type": best.get("option_type"),
        "expiry": best.get("expiry"),
        "entry": best.get("entry"),
        "sl": best.get("sl"),
        "targets": best.get("targets"),
        "confidence": best.get("confidence"),
        "reason": best.get("reason"),
        "watchlist": data.get("watchlist", []),
        "watchlist_count": data.get("watchlist_count", 0),
        "blacklisted_indexes": data.get("blacklisted_indexes", []),
        "markets": data.get("markets", {}),
    }




def format_watchlist_text(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "Watchlist: —"
    lines = ["Watchlist:"]
    for i, w in enumerate(items[:5], 1):
        lines.append(
            f"{i}) {w.get('index')} {w.get('symbol')} {w.get('option_trade')} | "
            f"LTP {w.get('ltp')} | Trigger above {w.get('watch_entry_above')} | "
            f"Retest hold {w.get('retest_hold_zone')} | SL below {w.get('sl_below')} | "
            f"Conf {w.get('confidence')}%"
        )
    return "\n".join(lines)
@app.get("/quickScanText")
def quick_scan_text(
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    data = quick_scan(strikes_around=strikes_around, interval=interval, authorization=authorization, token=token)

    if not data.get("trade_available"):
        return {
            "text": (
                "NO TRADE\n"
                "Market:\n"
                "Bias: Bullish / Bearish not confirmed\n"
                "Option Trade: NO TRADE\n"
                "Strike: —\n"
                "Entry: —\n"
                "Stop Loss: —\n"
                "Target: —\n"
                f"Confidence: {data.get('confidence', 0)}%\n"
                f"Reason: {data.get('reason') or 'No valid setup'}\n\n"
                f"{format_watchlist_text(data.get('watchlist', []))}"
            )
        }

    return {
        "text": (
            "Market:\n"
            f"Bias: {data.get('bias')}\n"
            f"Option Trade: {data.get('option_trade')}\n"
            f"Strike: {data.get('symbol') or data.get('strike')}\n"
            f"Entry: {data.get('entry')}\n"
            f"Stop Loss: {data.get('sl')}\n"
            f"Target: {data.get('targets')}\n"
            f"Confidence: {data.get('confidence')}%\n"
            f"Reason: {data.get('reason')}"
        )
    }


@app.get("/rigaScan")
def riga_scan_simple(
    token: Optional[str] = Query(None),
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    authorization: Optional[str] = Header(None),
):
    check_token(authorization, token)

    data = quick_scan(
        strikes_around=strikes_around,
        interval=interval,
        authorization=authorization,
        token=token,
    )

    if not data.get("trade_available"):
        return {
            "text": (
                "NO TRADE\n\n"
                "Market:\n"
                "Bias: Bullish / Bearish not confirmed\n"
                "Option Trade: NO TRADE\n"
                "Strike: —\n"
                "Entry: —\n"
                "Stop Loss: —\n"
                "Target: —\n"
                f"Confidence: {data.get('confidence', 0)}%\n"
                f"Reason: {data.get('reason') or 'No valid setup'}\n\n"
                f"{format_watchlist_text(data.get('watchlist', []))}"
            )
        }

    return {
        "text": (
            "Market:\n"
            f"Bias: {data.get('bias')}\n"
            f"Option Trade: {data.get('option_trade')}\n"
            f"Strike: {data.get('symbol') or data.get('strike')}\n"
            f"Entry: {data.get('entry')}\n"
            f"Stop Loss: {data.get('sl')}\n"
            f"Target: {data.get('targets')}\n"
            f"Confidence: {data.get('confidence')}%\n"
            f"Reason: {data.get('reason')}"
        )
    }


@app.post("/rigaScan")
def riga_scan_simple_post(payload: ScanAllMarketsRequest, authorization: Optional[str] = Header(None)):
    return riga_scan_simple(
        token=payload.token,
        strikes_around=payload.strikes_around,
        interval=payload.interval,
        authorization=authorization,
    )


@app.get("/scan-all-options")
def scan_all_options_alias(
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    debug: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    return scan_all_markets(strikes_around=strikes_around, interval=interval, debug=debug, authorization=authorization, token=token)


@app.get("/scan_all_markets")
def scan_all_markets_snake_alias(
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    return scan_all_markets(strikes_around=strikes_around, interval=interval, authorization=authorization, token=token)


# -----------------------------
# POST Routes for ChatGPT Actions / plugin body calls
# -----------------------------

@app.post("/getSpotPrice")
def get_spot_price_post(payload: SpotPriceRequest, authorization: Optional[str] = Header(None)):
    return get_spot_price(index=payload.index, authorization=authorization, token=payload.token)


@app.post("/spot")
def spot_alias_post(payload: SpotPriceRequest, authorization: Optional[str] = Header(None)):
    return get_spot_price_post(payload=payload, authorization=authorization)


@app.post("/getOptionChain")
def get_option_chain_post(payload: OptionChainRequest, authorization: Optional[str] = Header(None)):
    return get_option_chain(index=payload.index, strikes_around=payload.strikes_around, include_premium=payload.include_premium, authorization=authorization, token=payload.token)


@app.post("/option-chain")
def option_chain_alias_post(payload: OptionChainRequest, authorization: Optional[str] = Header(None)):
    return get_option_chain_post(payload=payload, authorization=authorization)


@app.post("/marketPremiums")
def market_premiums_post(payload: MarketPremiumsRequest, authorization: Optional[str] = Header(None)):
    return market_premiums(index=payload.index, strikes_around=payload.strikes_around, authorization=authorization, token=payload.token)


@app.post("/market-premiums")
def market_premiums_alias_post(payload: MarketPremiumsRequest, authorization: Optional[str] = Header(None)):
    return market_premiums_post(payload=payload, authorization=authorization)


@app.post("/scanOptions")
def scan_options_post(payload: ScanOptionsRequest, authorization: Optional[str] = Header(None)):
    return scan_options(index=payload.index, strikes_around=payload.strikes_around, interval=payload.interval, debug=payload.debug, authorization=authorization, token=payload.token)


@app.post("/scan-options")
def scan_options_alias_post(payload: ScanOptionsRequest, authorization: Optional[str] = Header(None)):
    return scan_options_post(payload=payload, authorization=authorization)


@app.post("/scanAllMarkets")
def scan_all_markets_post(payload: ScanAllMarketsRequest, authorization: Optional[str] = Header(None)):
    return scan_all_markets(strikes_around=payload.strikes_around, interval=payload.interval, debug=payload.debug, authorization=authorization, token=payload.token)


@app.post("/scan-all-options")
def scan_all_options_alias_post(payload: ScanAllMarketsRequest, authorization: Optional[str] = Header(None)):
    return scan_all_markets_post(payload=payload, authorization=authorization)


@app.post("/scan_all_markets")
def scan_all_markets_snake_alias_post(payload: ScanAllMarketsRequest, authorization: Optional[str] = Header(None)):
    return scan_all_markets_post(payload=payload, authorization=authorization)


@app.post("/quickScan")
def quick_scan_post(payload: ScanAllMarketsRequest, authorization: Optional[str] = Header(None)):
    return quick_scan(strikes_around=payload.strikes_around, interval=payload.interval, authorization=authorization, token=payload.token)


@app.post("/quickScanText")
def quick_scan_text_post(payload: ScanAllMarketsRequest, authorization: Optional[str] = Header(None)):
    return quick_scan_text(strikes_around=payload.strikes_around, interval=payload.interval, authorization=authorization, token=payload.token)


@app.post("/candles-test")
def candles_test_post(payload: CandleTestRequest, authorization: Optional[str] = Header(None)):
    return candles_test(index=payload.index, interval=payload.interval, authorization=authorization, token=payload.token)


@app.post("/option-candles-test")
def option_candles_test_post(payload: OptionCandleTestRequest, authorization: Optional[str] = Header(None)):
    return option_candles_test(index=payload.index, strike=payload.strike, side=payload.side, interval=payload.interval, authorization=authorization, token=payload.token)


# -----------------------------
# Formatter
# -----------------------------

def format_riga_output(trade: Any) -> str:
    if trade == "NO TRADE" or not isinstance(trade, dict):
        return "NO TRADE"

    sig = trade.get("signal", {})
    bias = sig.get("bias")

    if bias not in ["BUY_CE", "BUY_PE"]:
        return "NO TRADE"

    market_bias = "Bullish" if bias == "BUY_CE" else "Bearish"
    option_trade = "BUY CE" if bias == "BUY_CE" else "BUY PE"

    return (
        f"Market:\n"
        f"Bias: {market_bias}\n"
        f"Option Trade: {option_trade}\n"
        f"Strike: {trade.get('option', {}).get('strike')}\n"
        f"Entry: {sig.get('entry')}\n"
        f"Stop Loss: {sig.get('sl')}\n"
        f"Target: {sig.get('targets')}\n"
        f"Confidence: {sig.get('confidence')}%\n"
        f"Reason: {sig.get('reason')}"
    )


if __name__ == "__main__":
    print("RIGA AI main.py v11.6 Balanced Opportunity loaded. Run with: uvicorn main:app --reload")
