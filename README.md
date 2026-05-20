# RIGA v9 PRO SNIPER 🚀

Advanced high probability option buying system.

-------------------------------------

## 🔥 FEATURES

- BUY CE / BUY PE only
- No SELL trades
- Multi-timeframe structure analysis
- Index structure engine
- Premium candle engine
- VWAP logic
- Swing high / swing low breakout logic
- Volume spike detection
- Liquidity trap rejection
- Structure-based SL
- Sniper trade scoring system
- ATM / Near ATM preference
- One best trade selection

-------------------------------------

## 📡 API ENDPOINTS

- `/health`
- `/spot`
- `/option-chain`
- `/scan-options`
- `/scan-all-options`

-------------------------------------

## ⚠️ RIGA RULES

- Only high probability trades
- Reject fake breakouts
- Reject weak candles
- Reject wide SL
- Reject bad RR
- Avoid far OTM trades
- Prefer ATM / near ATM
- Final output:
  - BUY_CE
  - BUY_PE
  - NO_TRADE

-------------------------------------

## ⚙️ DEPLOYMENT

Build:
pip install -r requirements.txt

Start:
uvicorn main:app --host 0.0.0.0 --port $PORT

-------------------------------------

## 🎯 GOAL

Sniper option buying system:
- Wait patiently
- Avoid noise
- Catch strong momentum
- High probability execution
- Best trade from full market

-------------------------------------

## ⚠️ NOTE

- Signal only
- No auto order placement
- Uses Angel One SmartAPI
- Requires valid API credentials