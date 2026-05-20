import osimport requestsfrom datetime import datetimefrom typing import Optional

import pyotpfrom dotenv import load_dotenvfrom fastapi import FastAPI, Header, HTTPException, Queryfrom SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA v7 FINAL SNIPER", version="7.0")

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN", "")

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

client_obj = Nonescrip_master_cache = None

INDEX_CONFIG = {"NIFTY": {"spot": {"exchange": "NSE", "tradingsymbol": "NIFTY 50", "symboltoken": "26000"},"option_exchange": "NFO","option_name": "NIFTY","step": 50},"BANKNIFTY": {"spot": {"exchange": "NSE", "tradingsymbol": "NIFTY BANK", "symboltoken": "26009"},"option_exchange": "NFO","option_name": "BANKNIFTY","step": 100},"FINNIFTY": {"spot": {"exchange": "NSE", "tradingsymbol": "NIFTY FIN SERVICE", "symboltoken": "26037"},"option_exchange": "NFO","option_name": "FINNIFTY","step": 50},"SENSEX": {"spot": {"exchange": "BSE", "tradingsymbol": "SENSEX", "symboltoken": "1"},"option_exchange": "BFO","option_name": "SENSEX","step": 100}}

def check_token(authorization: Optional[str], token: Optional[str]):if not RIGA_ACTION_TOKEN:return

if authorization == f"Bearer {RIGA_ACTION_TOKEN}":
    return

if token == RIGA_ACTION_TOKEN:
    return

raise HTTPException(status_code=401, detail="Unauthorized")

def get_client():global client_obj

if client_obj:
    return client_obj

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
    raise HTTPException(status_code=500, detail="Angel login failed")

client_obj = client
return client

def load_scrip_master():global scrip_master_cache

if scrip_master_cache:
    return scrip_master_cache

res = requests.get(SCRIP_MASTER_URL, timeout=25)
res.raise_for_status()

scrip_master_cache = res.json()
return scrip_master_cache

def get_ltp(client, item):try:res = client.ltpData(item["exchange"],item["tradingsymbol"],str(item["symboltoken"]))except Exception:return None

if not res or not res.get("status"):
    return None

d = res.get("data", {})

return {
    "symbol": item["tradingsymbol"],
    "exchange": item["exchange"],
    "token": item["symboltoken"],
    "ltp": d.get("ltp"),
    "open": d.get("open"),
    "high": d.get("high"),
    "low": d.get("low"),
    "close": d.get("close")
}

def safe_num(value):return isinstance(value, (int, float))

def candle_stats(data):high = data.get("high")low = data.get("low")ltp = data.get("ltp")open_p = data.get("open")

if not all(safe_num(x) for x in [high, low, ltp, open_p]):
    return None

rng = high - low
if rng <= 0:
    return None

body = abs(ltp - open_p)
upper_wick = high - max(open_p, ltp)
lower_wick = min(open_p, ltp) - low
position = (ltp - low) / rng
momentum = ((ltp - open_p) / open_p) * 100 if open_p else 0
strength = body / rng

return {
    "range": rng,
    "body": body,
    "upper_wick": upper_wick,
    "lower_wick": lower_wick,
    "position": position,
    "momentum": momentum,
    "strength": strength
}

def candle_quality(data):st = candle_stats(data)if not st:return "BAD", 0

strength = st["strength"]

if strength >= 0.70:
    return "A_PLUS", strength
if strength >= 0.60:
    return "A_GRADE", strength
if strength >= 0.45:
    return "B_GRADE", strength

return "LOW_QUALITY", strength

def detect_pattern(data):st = candle_stats(data)if not st:return "NO_PATTERN"

pos = st["position"]
mom = st["momentum"]
strength = st["strength"]

if pos > 0.88 and mom > 0.75 and strength >= 0.60:
    return "BULLISH_BREAKOUT"

if pos < 0.12 and mom < -0.75 and strength >= 0.60:
    return "BEARISH_BREAKDOWN"

if pos > 0.72 and mom > 0.40 and strength >= 0.45:
    return "BULLISH_CONTINUATION"

if pos < 0.28 and mom < -0.40 and strength >= 0.45:
    return "BEARISH_CONTINUATION"

return "NO_PATTERN"

def liquidity_trap_filter(data):st = candle_stats(data)if not st:return True, "bad candle data"

body = st["body"]
upper_wick = st["upper_wick"]
lower_wick = st["lower_wick"]
pos = st["position"]

if body <= 0:
    return True, "doji/no body trap"

if upper_wick > body * 1.7 and pos < 0.75:
    return True, "upper wick rejection trap"

if lower_wick > body * 1.7 and pos > 0.25:
    return True, "lower wick rejection trap"

return False, "no liquidity trap"

def retest_filter(data, pattern):st = candle_stats(data)if not st:return False, "no retest data"

pos = st["position"]

# With only OHLC/LTP, this is an approximation:
# strong close after moving away from low/high means accepted retest.
if pattern in ["BULLISH_BREAKOUT", "BULLISH_CONTINUATION"]:
    if pos >= 0.78:
        return True, "bullish acceptance/retest approximation"

if pattern in ["BEARISH_BREAKDOWN", "BEARISH_CONTINUATION"]:
    if pos <= 0.22:
        return True, "bearish acceptance/retest approximation"

return False, "retest not confirmed"

def option_alignment(option_type, side):if option_type == "CE" and side == "BUY":return Trueif option_type == "PE" and side == "SELL":return Truereturn False

def riga_v7_logic(data, option_type=None):if not data:return {"bias": "NO TRADE","confidence": 0,"reason": "No data"}

st = candle_stats(data)
if not st:
    return {
        "bias": "NO TRADE",
        "confidence": 0,
        "reason": "Invalid OHLC data"
    }

pattern = detect_pattern(data)
candle, strength = candle_quality(data)
trap, trap_reason = liquidity_trap_filter(data)
retest, retest_reason = retest_filter(data, pattern)

momentum = st["momentum"]
position = st["position"]
rng = st["range"]
ltp = data["ltp"]

buy_score = 0
sell_score = 0
buy_reasons = []
sell_reasons = []

if pattern == "BULLISH_BREAKOUT":
    buy_score += 30
    buy_reasons.append("bullish breakout")
elif pattern == "BULLISH_CONTINUATION":
    buy_score += 20
    buy_reasons.append("bullish continuation")

if pattern == "BEARISH_BREAKDOWN":
    sell_score += 30
    sell_reasons.append("bearish breakdown")
elif pattern == "BEARISH_CONTINUATION":
    sell_score += 20
    sell_reasons.append("bearish continuation")

if candle == "A_PLUS":
    buy_score += 20
    sell_score += 20
    buy_reasons.append("A+ candle")
    sell_reasons.append("A+ candle")
elif candle == "A_GRADE":
    buy_score += 15
    sell_score += 15
    buy_reasons.append("A grade candle")
    sell_reasons.append("A grade candle")
elif candle == "B_GRADE":
    buy_score += 8
    sell_score += 8
    buy_reasons.append("B grade candle")
    sell_reasons.append("B grade candle")

if momentum > 0.75:
    buy_score += 20
    buy_reasons.append("strong bullish momentum")

if momentum < -0.75:
    sell_score += 20
    sell_reasons.append("strong bearish momentum")

if position > 0.85:
    buy_score += 15
    buy_reasons.append("price near day high")

if position < 0.15:
    sell_score += 15
    sell_reasons.append("price near day low")

if retest:
    if pattern in ["BULLISH_BREAKOUT", "BULLISH_CONTINUATION"]:
        buy_score += 10
        buy_reasons.append(retest_reason)
    if pattern in ["BEARISH_BREAKDOWN", "BEARISH_CONTINUATION"]:
        sell_score += 10
        sell_reasons.append(retest_reason)

if trap:
    buy_score -= 25
    sell_score -= 25
    buy_reasons.append(trap_reason)
    sell_reasons.append(trap_reason)

# CE/PE alignment
if option_type == "CE":
    sell_score -= 25
if option_type == "PE":
    buy_score -= 25

# BUY
if buy_score >= 70 and buy_score >= sell_score:
    if option_type and not option_alignment(option_type, "BUY"):
        return {
            "bias": "NO TRADE",
            "confidence": buy_score,
            "reason": "Option type not aligned with BUY"
        }

    sl = round(ltp - rng * 0.20, 2)
    target = round(ltp + (ltp - sl) * 2, 2)

    return {
        "bias": "BUY",
        "entry": round(ltp, 2),
        "sl": sl,
        "target": target,
        "confidence": min(buy_score, 95),
        "pattern": pattern,
        "candle": candle,
        "candle_strength": round(strength, 2),
        "retest": retest,
        "trap_filter": trap_reason,
        "reason": ", ".join(buy_reasons)
    }

# SELL
if sell_score >= 70 and sell_score > buy_score:
    if option_type and not option_alignment(option_type, "SELL"):
        return {
            "bias": "NO TRADE",
            "confidence": sell_score,
            "reason": "Option type not aligned with SELL"
        }

    sl = round(ltp + rng * 0.20, 2)
    target = round(ltp - (sl - ltp) * 2, 2)

    return {
        "bias": "SELL",
        "entry": round(ltp, 2),
        "sl": sl,
        "target": target,
        "confidence": min(sell_score, 95),
        "pattern": pattern,
        "candle": candle,
        "candle_strength": round(strength, 2),
        "retest": retest,
        "trap_filter": trap_reason,
        "reason": ", ".join(sell_reasons)
    }

return {
    "bias": "NO TRADE",
    "confidence": max(buy_score, sell_score),
    "pattern": pattern,
    "candle": candle,
    "candle_strength": round(strength, 2),
    "retest": retest,
    "trap_filter": trap_reason,
    "reason": "RIGA v7 confirmations below 70"
}

def round_to_step(price, step):return int(round(price / step) * step)

def parse_expiry(expiry):for fmt in ("%d%b%Y", "%d%b%y"):try:return datetime.strptime(str(expiry).upper(), fmt)except Exception:passreturn None

def get_auto_option_chain(index_name, spot_price, strikes_around=3):index_name = index_name.upper()

if index_name not in INDEX_CONFIG:
    raise HTTPException(
        status_code=400,
        detail="Use NIFTY, BANKNIFTY, FINNIFTY, SENSEX"
    )

cfg = INDEX_CONFIG[index_name]
master = load_scrip_master()

atm = round_to_step(spot_price, cfg["step"])
allowed = {
    atm + i * cfg["step"]
    for i in range(-strikes_around, strikes_around + 1)
}

today = datetime.now()
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
        if not expiry_dt or expiry_dt.date() < today.date():
            continue

        found.append({
            "exchange": cfg["option_exchange"],
            "tradingsymbol": symbol,
            "symboltoken": str(s.get("token")),
            "strike": strike,
            "type": "CE" if symbol.endswith("CE") else "PE",
            "expiry": s.get("expiry"),
            "expiry_dt": expiry_dt
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

def select_best_trade(trades):if not trades:return None

return sorted(
    trades,
    key=lambda x: x["signal"].get("confidence", 0),
    reverse=True
)[0]

@app.get("/")def root():return {"status": "RIGA v7 FINAL LIVE","features": ["clean best trade output","auto ATM option chain","retest filter","liquidity trap filter","candlestick grading","pattern confirmation","CE/PE alignment","70 confidence rule"]}

@app.get("/health")def health():return {"status": "ok"}

@app.get("/spot")def spot(index: str = Query("NIFTY"),authorization: Optional[str] = Header(None),token: Optional[str] = Query(None)):check_token(authorization, token)

index = index.upper()
if index not in INDEX_CONFIG:
    raise HTTPException(status_code=400, detail="Invalid index")

client = get_client()
return get_ltp(client, INDEX_CONFIG[index]["spot"])

@app.get("/option-chain")def option_chain(index: str = Query("NIFTY"),strikes_around: int = Query(3),authorization: Optional[str] = Header(None),token: Optional[str] = Query(None)):check_token(authorization, token)

index = index.upper()
if index not in INDEX_CONFIG:
    raise HTTPException(status_code=400, detail="Invalid index")

client = get_client()
spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])

if not spot_data:
    raise HTTPException(status_code=500, detail="Spot data failed")

atm, expiry, options = get_auto_option_chain(
    index,
    float(spot_data["ltp"]),
    strikes_around
)

return {
    "index": index,
    "spot": spot_data,
    "atm": atm,
    "nearest_expiry": expiry,
    "options_count": len(options),
    "options": options
}

@app.get("/scan-options")def scan_options(index: str = Query("NIFTY"),strikes_around: int = Query(3),authorization: Optional[str] = Header(None),token: Optional[str] = Query(None)):check_token(authorization, token)

index = index.upper()
if index not in INDEX_CONFIG:
    raise HTTPException(status_code=400, detail="Invalid index")

client = get_client()
spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])

if not spot_data:
    raise HTTPException(status_code=500, detail="Spot data failed")

atm, expiry, options = get_auto_option_chain(
    index,
    float(spot_data["ltp"]),
    strikes_around
)

trades = []
scanned = 0

for opt in options:
    data = get_ltp(client, opt)
    signal = riga_v7_logic(data, opt.get("type"))
    scanned += 1

    if signal.get("bias") in ["BUY", "SELL"] and signal.get("confidence", 0) >= 70:
        trades.append({
            "option": opt,
            "data": data,
            "signal": signal
        })

best_trade = select_best_trade(trades)

return {
    "index": index,
    "spot_ltp": spot_data["ltp"],
    "atm": atm,
    "nearest_expiry": expiry,
    "total_options_scanned": scanned,
    "trade_count": len(trades),
    "best_trade": best_trade if best_trade else "NO TRADE",
    "trades": trades[:5]
}

@app.get("/scan-all-options")def scan_all_options(strikes_around: int = Query(2),authorization: Optional[str] = Header(None),token: Optional[str] = Query(None)):check_token(authorization, token)

output = {}
overall_trades = []

for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
    try:
        client = get_client()
        spot_data = get_ltp(client, INDEX_CONFIG[idx]["spot"])

        if not spot_data:
            output[idx] = {"error": "spot failed"}
            continue

        atm, expiry, options = get_auto_option_chain(
            idx,
            float(spot_data["ltp"]),
            strikes_around
        )

        trades = []
        scanned = 0

        for opt in options:
            data = get_ltp(client, opt)
            signal = riga_v7_logic(data, opt.get("type"))
            scanned += 1

            if signal.get("bias") in ["BUY", "SELL"] and signal.get("confidence", 0) >= 70:
                trade_obj = {
                    "index": idx,
                    "option": opt,
                    "data": data,
                    "signal": signal
                }
                trades.append(trade_obj)
                overall_trades.append(trade_obj)

        best_trade = select_best_trade(trades)

        output[idx] = {
            "spot_ltp": spot_data["ltp"],
            "atm": atm,
            "expiry": expiry,
            "total_options_scanned": scanned,
            "trade_count": len(trades),
            "best_trade": best_trade if best_trade else "NO TRADE",
            "trades": trades[:3]
        }

    except Exception as e:
        output[idx] = {"error": str(e)}

overall_best = select_best_trade(overall_trades)

return {
    "overall_best_trade": overall_best if overall_best else "NO TRADE",
    "markets": output
}
