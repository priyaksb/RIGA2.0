"""
RIGA AI - Final main.py v10
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
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA AI Option Buying Scanner v10", version="10.4")

Side = Literal["CE", "PE"]
Bias = Literal["BULLISH", "BEARISH", "NEUTRAL"]

IST = timezone(timedelta(hours=5, minutes=30))

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN", "")

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

CONFIDENCE_MIN = 70
MAX_RISK_PCT = 22.0
MIN_RISK_PCT = 2.0
DEFAULT_BUFFER_PCT = 0.015
MIN_CANDLES = 8

# Transition scan:
# Do not skip option-premium scan only because the last index candle is weak/indecisive.
# If the broader index score is near directional, scan ATM/near-ATM CE and PE and let
# option premium structure decide. This improves opportunity capture without forcing trades.
TRANSITION_SCAN_MIN_SCORE = 25
TRANSITION_STRIKES_AROUND = 1
MAX_SCAN_STRIKES_AROUND = 6
PREMIUM_LED_SCAN_ALWAYS = True

client_obj = None
scrip_master_cache = None


# One clean config only.
# spot_token = LTP/quote token
# hist_token = Angel historical candle token for index
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

    totp = pyotp.TOTP(
        ANGEL_TOTP_SECRET.strip().replace(" ", "").upper()
    ).now()

    session = client.generateSession(
        ANGEL_CLIENT_CODE,
        ANGEL_PASSWORD,
        totp
    )

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
    """
    Render may run UTC. Angel historical API needs IST.
    Uses 09:15 to now during market, or previous trading session after close/before open.
    """
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
    """
    Prevent stale Friday/previous-day candles being treated as live scans.
    Does not handle NSE/BSE holidays, but catches weekends and pre/post-market.
    """
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
    # Angel often returns ISO like 2026-05-08T15:25:00+05:30
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
    """
    During market hours, last candle must be close to current IST time.
    Off-market, scans should not generate trades.
    """
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
    except Exception:
        return None

    if not res or not res.get("status"):
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
    except Exception:
        return []

    if not res or not res.get("status"):
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
    """
    Debug helper for option premium candles.
    Use this to verify whether Angel is returning candles for the exact option token.
    """
    dbg = get_candles_debug(
        client,
        opt.get("exchange", "NFO"),
        str(opt.get("symboltoken")),
        interval=interval,
    )
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
    """
    VWAP with fallback.

    Angel index candles often return volume = 0.
    In that case real VWAP cannot be calculated, so we use
    typical-price average as a practical structure reference.
    """
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

    # Fallback when all volumes are zero.
    # Not true VWAP, but better than null for trend/acceptance logic.
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
    """
    Returns:
    - spike_bool
    - ratio
    - available_bool

    Angel index candles may have volume = 0.
    If volume is unavailable, do not fail the setup; just mark unavailable.
    """
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
                    "transition_sides": ["CE"]
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
                    "transition_sides": ["PE"]
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
            "transition_sides": []
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
            "transition_sides": ["CE"]
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
            "transition_sides": ["PE"]
        }

    neutral_score = max(score_bull, score_bear)
    transition_sides = []

    # Productive scanner fix:
    # Earlier 35-40 score zones skipped option scanning completely.
    # In intraday options, this misses many valid trades where index is consolidating
    # but ATM option premium is already breaking out / holding VWAP.
    if neutral_score >= TRANSITION_SCAN_MIN_SCORE:
        if abs(score_bull - score_bear) <= 15:
            transition_sides = ["CE", "PE"]
        elif score_bull > score_bear:
            transition_sides = ["CE"]
        else:
            transition_sides = ["PE"]
    elif PREMIUM_LED_SCAN_ALWAYS:
        # Very weak index: do only premium-led ATM scan on both sides.
        # Final signal still needs option premium score >= CONFIDENCE_MIN.
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
        "reason": "premium-led transition scan enabled; option premium must confirm" if transition_sides else "index structure not clean enough"
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
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": "not enough option candle data",
            "candle_count": len(candles or [])
        }

    last = candles[-1]
    levels = find_swing_levels(candles, 20)
    vwap = calc_vwap(candles)
    vol_ok, vol_ratio, vol_available = volume_spike(candles)

    entry = float(ltp_data["ltp"])
    score = 0
    reasons = []

    idx_score = index_bias.get("score", 0)

    if idx_score >= 70:
        score += 20
    elif idx_score >= 55:
        score += 14
    elif index_bias.get("transition_scan") and idx_score >= TRANSITION_SCAN_MIN_SCORE:
        score += 8
    elif index_bias.get("transition_scan"):
        score += 3

    reasons.append(index_bias.get("reason", ""))

    if not levels:
        return {"bias": "NO TRADE", "confidence": score, "reason": "no swing levels"}

    recent3 = candles[-4:-1] if len(candles) >= 4 else candles[:-1]
    recent3_lows = [c["low"] for c in recent3] or [candles[-2]["low"]]
    prev = candles[-2]

    if last["close"] > levels["prev_high"]:
        score += 30
        reasons.append("premium breakout above swing high")
        pattern = "PREMIUM_BREAKOUT"
    elif vwap and last["low"] <= vwap * 1.004 and last["close"] > vwap and is_bull_candle(last):
        score += 24
        reasons.append("premium VWAP retest hold")
        pattern = "PREMIUM_RETEST_HOLD"
    elif vwap and last["low"] > min(recent3_lows) and last["close"] > prev["close"] and last["close"] > vwap:
        score += 20
        reasons.append("premium higher-low continuation above VWAP")
        pattern = "PREMIUM_HIGHER_LOW"
    elif vwap and last["close"] > vwap and last["close"] > candles[-2]["close"]:
        score += 16
        reasons.append("premium continuation above VWAP")
        pattern = "PREMIUM_CONTINUATION"
    else:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "reason": "premium has no breakout/retest/higher-low confirmation",
            "candle_count": len(candles)
        }

    cq = classify_candle(last)
    strength = candle_strength(last)
    mom = pct_change(last["close"], candles[-2]["close"])

    # Late breakout / exhaustion filter from book logic
    if strength >= 0.92 and mom >= 3.0:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "pattern": pattern,
            "candle": cq,
            "candle_strength": round(strength, 2),
            "momentum_pct": round(mom, 2),
            "reason": "exhaustion breakout / late entry rejected"
        }

    if vwap:
        distance_from_vwap = pct_change(entry, vwap)
        if distance_from_vwap > 8.0:
            return {
                "bias": "NO TRADE",
                "confidence": score,
                "pattern": pattern,
                "premium_vwap": round(vwap, 2),
                "distance_from_vwap_pct": round(distance_from_vwap, 2),
                "reason": "premium too extended from VWAP / chase rejected"
            }

    if cq == "A_PLUS_BULL":
        score += 22
        reasons.append("A+ bullish premium candle")
    elif cq == "A_BULL":
        score += 18
        reasons.append("A grade bullish premium candle")
    elif cq == "B_BULL" and pattern in ["PREMIUM_BREAKOUT", "PREMIUM_RETEST_HOLD", "PREMIUM_HIGHER_LOW"]:
        score += 12
        reasons.append("B bullish premium candle with structure confirmation")
    else:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "pattern": pattern,
            "candle": cq,
            "reason": "premium candle quality not strong enough"
        }

    if mom > 0.25:
        score += 16
        reasons.append(f"strong premium momentum {round(mom, 2)}%")
    elif mom > 0.08:
        score += 10
        reasons.append(f"premium momentum {round(mom, 2)}%")

    if vwap and last["close"] > vwap:
        score += 10
        reasons.append("premium above VWAP")

    if vol_ok:
        score += 10
        reasons.append(f"volume expansion {vol_ratio}x")

    # For option buying, lower-wick rejection after retest is often support absorption,
    # so reject only weak body or upper-wick supply rejection.
    body = max(candle_body(last), 0.01)
    if candle_strength(last) < 0.30:
        return {
            "bias": "NO TRADE",
            "confidence": max(score - 20, 0),
            "pattern": pattern,
            "candle": cq,
            "trap_filter": "weak body / indecision",
            "reason": "trap rejected: weak body / indecision"
        }
    if upper_wick(last) > body * 2.0 and candle_position(last) < 0.70:
        return {
            "bias": "NO TRADE",
            "confidence": max(score - 20, 0),
            "pattern": pattern,
            "candle": cq,
            "trap_filter": "upper wick rejection / fake breakout risk",
            "reason": "trap rejected: upper wick rejection / fake breakout risk"
        }
    trap_reason = "no trap"

    step = INDEX_CONFIG[index_name]["step"]
    atm_distance = abs(opt["strike"] - atm)

    if atm_distance == 0:
        score += 10
        reasons.append("ATM strike")
    elif atm_distance <= step:
        score += 7
        reasons.append("near ATM strike")
    elif atm_distance <= step * 2:
        score += 2
        reasons.append("acceptable strike distance")
    else:
        score -= 15
        reasons.append("far from ATM penalty")

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
            "reason": f"risk too wide >{MAX_RISK_PCT}%"
        }

    if risk_pct < MIN_RISK_PCT:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "entry": round(entry, 2),
            "sl": round(structure_sl, 2),
            "risk_pct": round(risk_pct, 2),
            "reason": "risk too tight / noise SL"
        }

    t1 = round(entry + risk * 1.5, 2)
    t2 = round(entry + risk * 2.0, 2)
    t3 = round(entry + risk * 3.0, 2)

    if not (structure_sl < entry < t1 < t2 < t3):
        return {"bias": "NO TRADE", "confidence": score, "reason": "RR structure invalid"}

    confidence = min(score, 95)

    if confidence < CONFIDENCE_MIN:
        return {
            "bias": "NO TRADE",
            "confidence": confidence,
            "pattern": pattern,
            "candle": cq,
            "reason": f"RIGA sniper score below {CONFIDENCE_MIN}"
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
        "pattern": pattern,
        "candle": cq,
        "candle_strength": round(strength, 2),
        "momentum_pct": round(mom, 2),
        "premium_vwap": round(vwap, 2) if vwap else None,
        "volume_spike": vol_ratio if vol_ok else None,
        "volume_available": vol_available,
        "atm_distance": atm_distance,
        "trap_filter": trap_reason,
        "candle_count": len(candles),
        "reason": ", ".join([r for r in reasons if r]),
    }


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

def scan_one_index(index: str, strikes_around: int = 3, interval: str = "FIVE_MINUTE", debug: bool = False):
    index = index.upper()

    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

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
        return {
            "index": index,
            "spot_ltp": spot_data["ltp"],
            "spot_close": spot_data.get("close"),
            "index_bias": {
                "bias": "MARKET_CLOSED" if "MARKET_" in freshness_reason else "STALE_DATA",
                "option_side": None,
                "score": 0,
                "candle_count": len(index_candles or []),
                "last_candle": index_candles[-1] if index_candles else None,
                "reason": freshness_reason,
                "index": index,
            },
            "atm": atm,
            "nearest_expiry": expiry,
            **option_debug_summary,
            "total_options_scanned": 0,
            "trade_count": 0,
            "best_trade": "NO TRADE",
        }

    index_bias = analyze_index_structure(index, spot_data, index_candles)
    index_bias["index"] = index

    side = index_bias.get("option_side")
    transition_sides = index_bias.get("transition_sides", []) if index_bias.get("transition_scan") else []

    if side in ["CE", "PE"]:
        sides_to_scan = [side]
    elif transition_sides:
        # During transition, scan only ATM/near-ATM strikes for the stronger side(s).
        # Final trade still requires option premium breakout/hold and confidence >= CONFIDENCE_MIN.
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

    # Keep RIGA option buying ATM-first. In transition mode restrict to ATM/near-ATM only.
    max_distance = INDEX_CONFIG[index]["step"] * (TRANSITION_STRIKES_AROUND if side not in ["CE", "PE"] else MAX_SCAN_STRIKES_AROUND)
    side_options = [
        opt for opt in options
        if opt["type"] in sides_to_scan and abs(opt["strike"] - atm) <= max_distance
    ]
    option_debug_summary["side_options_found"] = len(side_options)

    trades = []
    rejected = []
    scanned = 0

    for opt in side_options:
        symbol = opt.get("tradingsymbol")
        symboltoken = opt.get("symboltoken")

        if not symboltoken:
            rejected.append({
                "symbol": symbol,
                "strike": opt.get("strike"),
                "type": opt.get("type"),
                "reason": "NO_SYMBOL_TOKEN",
                "confidence": 0,
                "candle_count": 0,
            })
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
            effective_index_bias["reason"] = (
                f"transition scan; option premium confirmation required; "
                f"{effective_index_bias.get('reason', '')}"
            )

        signal = analyze_option_buy_setup(effective_index_bias, opt, ltp_data, opt_candles, atm, index)

        if signal.get("bias") in ["BUY_CE", "BUY_PE"] and signal.get("confidence", 0) >= CONFIDENCE_MIN:
            trades.append({
                "index": index,
                "option": opt,
                "data": ltp_data,
                "signal": signal,
                "atm_distance": signal.get("atm_distance", abs(opt["strike"] - atm)),
            })
        elif debug:
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

    out = {
        "index": index,
        "spot_ltp": spot_data["ltp"],
        "spot_close": spot_data.get("close"),
        "index_bias": index_bias,
        "atm": atm,
        "nearest_expiry": expiry,
        **option_debug_summary,
        "total_options_scanned": scanned,
        "trade_count": len(trades),
        "best_trade": best_trade if best_trade else "NO TRADE",
    }

    if debug:
        out["rejected"] = rejected[:20]

    return out


# -----------------------------
# API Routes
# -----------------------------

@app.get("/")
def root():
    return {
        "name": "RIGA AI Option Buying Scanner",
        "version": "10.4",
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
            "/scanOptions",
            "/scanAllMarkets",
            "/candles-test",
            "/option-candles-test",
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
    """
    Example:
    /option-candles-test?index=BANKNIFTY&strike=54400&side=PE&token=YOUR_TOKEN

    This verifies if Angel is returning candles for that exact option token.
    """
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
    return get_option_chain(
        index=index,
        strikes_around=strikes_around,
        include_premium=include_premium,
        authorization=authorization,
        token=token,
    )


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
    return scan_options(
        index=index,
        strikes_around=strikes_around,
        interval=interval,
        debug=debug,
        authorization=authorization,
        token=token,
    )


@app.get("/scanAllMarkets")
def scan_all_markets(
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    debug: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)

    markets = {}
    valid_trades = []

    for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
        try:
            res = scan_one_index(
                index=idx,
                strikes_around=strikes_around,
                interval=interval,
                debug=debug,
            )

            best = res.get("best_trade")
            if isinstance(best, dict) and best.get("signal", {}).get("bias") in ["BUY_CE", "BUY_PE"]:
                valid_trades.append(best)

            # Compact output only to avoid ResponseTooLargeError
            markets[idx] = {
                "spot_ltp": res.get("spot_ltp"),
                "index_bias": res.get("index_bias"),
                "atm": res.get("atm"),
                "nearest_expiry": res.get("nearest_expiry"),
                "total_options_found": res.get("total_options_found"),
                "side_options_found": res.get("side_options_found"),
                "candle_requests": res.get("candle_requests"),
                "options_with_candles": res.get("options_with_candles"),
                "options_without_candles": res.get("options_without_candles"),
                "options_with_insufficient_candles": res.get("options_with_insufficient_candles"),
                "total_options_scanned": res.get("total_options_scanned"),
                "trade_count": res.get("trade_count"),
                "best_trade": best,
            }

            if debug and res.get("rejected"):
                markets[idx]["rejected"] = res.get("rejected")

        except Exception as exc:
            markets[idx] = {
                "spot_ltp": None,
                "index_bias": {"bias": "ERROR", "reason": str(exc)},
                "atm": None,
                "trade_count": 0,
                "best_trade": "NO TRADE",
            }

    overall_best = select_best_trade(valid_trades)

    return {
        "overall_best_trade": overall_best if overall_best else "NO TRADE",
        "markets": markets,
    }


@app.get("/scan-all-options")
def scan_all_options_alias(
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    debug: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    return scan_all_markets(
        strikes_around=strikes_around,
        interval=interval,
        debug=debug,
        authorization=authorization,
        token=token,
    )


@app.get("/scan_all_markets")
def scan_all_markets_snake_alias(
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    return scan_all_markets(
        strikes_around=strikes_around,
        interval=interval,
        authorization=authorization,
        token=token,
    )


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
    print("RIGA AI main.py loaded. Run with: uvicorn main:app --reload")
