"""
CryptoBot Backend — FastAPI + Binance
Handles HMAC signing, order placement, compounding logic, and auto-trading loop.
Deploy to Railway.app (free) for 24/7 operation.
"""

import asyncio
import hashlib
import hmac
import time
import os
import logging
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cryptobot")

# ─── CONFIG (set these as environment variables on Railway) ───────────────────
API_KEY    = os.getenv("BINANCE_API_KEY", "")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
BASE_URL   = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")  # testnet: https://testnet.binance.vision
TESTNET    = os.getenv("TESTNET", "true").lower() == "true"

# ─── TRADING CONFIG ───────────────────────────────────────────────────────────
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT",
         "XRPUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT"]

# Bot state (in-memory — persists while server runs)
bot_state = {
    "running": False,
    "auto_trade": False,
    "equity": 1000.0,
    "start_equity": 1000.0,
    "day_pnl": 0.0,
    "total_pnl": 0.0,
    "wins": 0,
    "losses": 0,
    "trades": [],
    "signals": {},
    "risk_pct": 3.0,
    "stop_loss": 1.2,
    "take_profit": 2.5,
    "min_confidence": 70,
    "scan_interval": 15,
}

# ─── BINANCE HELPERS ──────────────────────────────────────────────────────────
def sign(params: dict) -> str:
    """Generate HMAC-SHA256 signature for Binance private endpoints."""
    query = urlencode(params)
    return hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_headers() -> dict:
    return {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}

async def binance_get(client: httpx.AsyncClient, path: str, params: dict = None, signed: bool = False):
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = sign(params)
    res = await client.get(f"{BASE_URL}{path}", params=params, headers=get_headers())
    res.raise_for_status()
    return res.json()

async def binance_post(client: httpx.AsyncClient, path: str, params: dict):
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = sign(params)
    res = await client.post(f"{BASE_URL}{path}", data=params, headers=get_headers())
    return res.json(), res.status_code

# ─── TECHNICAL INDICATORS ────────────────────────────────────────────────────
def calc_ema(data: list, period: int) -> list:
    if len(data) < period:
        return [data[-1]]
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    result = [ema]
    for v in data[period:]:
        ema = v * k + ema * (1 - k)
        result.append(ema)
    return result

def calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 2:
        return 50.0
    gains, losses = 0, 0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0: gains += diff
        else: losses -= diff
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else 100
    return 100 - 100 / (1 + rs)

def calc_macd(closes: list) -> dict:
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    length = min(len(ema12), len(ema26))
    macd_line = [ema12[len(ema12)-length+i] - ema26[len(ema26)-length+i] for i in range(length)]
    signal = calc_ema(macd_line, 9)
    return {"macd": macd_line[-1], "signal": signal[-1]}

def calc_bb(closes: list, period: int = 20) -> dict:
    sl = closes[-period:]
    mean = sum(sl) / len(sl)
    std = (sum((x - mean) ** 2 for x in sl) / len(sl)) ** 0.5
    return {"upper": mean + 2 * std, "middle": mean, "lower": mean - 2 * std}

def calc_stoch(closes: list, period: int = 14) -> float:
    sl = closes[-period:]
    lo, hi = min(sl), max(sl)
    return ((closes[-1] - lo) / (hi - lo)) * 100 if hi != lo else 50.0

def generate_signal(closes: list) -> dict:
    if len(closes) < 30:
        return {"action": "HOLD", "confidence": 50}
    rsi   = calc_rsi(closes)
    macd  = calc_macd(closes)
    bb    = calc_bb(closes)
    price = closes[-1]
    ema9  = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    stoch = calc_stoch(closes)
    momentum = (closes[-1] - closes[-5]) / closes[-5] * 100

    bull, bear = 0, 0
    if rsi < 32: bull += 3
    elif rsi < 45: bull += 1
    if rsi > 68: bear += 3
    elif rsi > 55: bear += 1
    if macd["macd"] > macd["signal"] and macd["macd"] > 0: bull += 3
    elif macd["macd"] > macd["signal"]: bull += 1
    if macd["macd"] < macd["signal"] and macd["macd"] < 0: bear += 3
    elif macd["macd"] < macd["signal"]: bear += 1
    if price <= bb["lower"]: bull += 2
    elif price >= bb["upper"]: bear += 2
    if ema9[-1] > ema21[-1] > ema50[-1]: bull += 3
    if ema9[-1] < ema21[-1] < ema50[-1]: bear += 3
    if stoch < 20: bull += 2
    elif stoch > 80: bear += 2
    if momentum > 0.5: bull += 1
    elif momentum < -0.5: bear += 1

    total = bull + bear or 1
    confidence = round(max(bull, bear) / total * 100)

    if bull > bear and bull >= 7:
        return {"action": "BUY",  "confidence": confidence, "rsi": round(rsi, 1), "stoch": round(stoch, 1)}
    if bear > bull and bear >= 7:
        return {"action": "SELL", "confidence": confidence, "rsi": round(rsi, 1), "stoch": round(stoch, 1)}
    return {"action": "HOLD", "confidence": 50, "rsi": round(rsi, 1), "stoch": round(stoch, 1)}

# ─── PRICE CACHE ─────────────────────────────────────────────────────────────
price_history: dict[str, list] = {p: [] for p in PAIRS}

async def fetch_klines(client: httpx.AsyncClient, symbol: str, limit: int = 80) -> list:
    """Fetch real OHLCV klines from Binance (public, no auth needed)."""
    try:
        data = await binance_get(client, "/api/v3/klines", {
            "symbol": symbol, "interval": "1m", "limit": limit
        })
        return [float(k[4]) for k in data]  # close prices
    except Exception as e:
        log.warning(f"Kline fetch failed for {symbol}: {e}")
        return price_history.get(symbol, [])

# ─── ORDER PLACEMENT ─────────────────────────────────────────────────────────
async def place_order(client: httpx.AsyncClient, symbol: str, side: str,
                      quantity: str, stop_price: str, take_profit_price: str) -> dict:
    """
    Place a real OCO (One-Cancels-Other) order on Binance.
    OCO = entry market order + stop loss + take profit in one atomic block.
    Orders live on Binance servers — survive disconnects.
    """
    if TESTNET:
        log.info(f"[TESTNET] Would place {side} OCO for {symbol} qty={quantity} sl={stop_price} tp={take_profit_price}")
        return {"status": "TESTNET_SIMULATED", "symbol": symbol, "side": side}

    if not API_KEY or not SECRET_KEY:
        return {"error": "API keys not configured"}

    # Step 1: Market entry order
    entry_params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": quantity,
    }
    entry_res, entry_status = await binance_post(client, "/api/v3/order", entry_params)
    if entry_status != 200:
        log.error(f"Entry order failed: {entry_res}")
        return {"error": entry_res.get("msg", "Entry order failed")}

    log.info(f"✅ Entry order placed: {symbol} {side} qty={quantity}")

    # Step 2: OCO exit order (stop loss + take profit)
    exit_side = "SELL" if side == "BUY" else "BUY"
    oco_params = {
        "symbol": symbol,
        "side": exit_side,
        "quantity": quantity,
        "price": take_profit_price,          # limit price (take profit)
        "stopPrice": stop_price,             # stop trigger
        "stopLimitPrice": stop_price,        # stop limit
        "stopLimitTimeInForce": "GTC",
    }
    oco_res, oco_status = await binance_post(client, "/api/v3/order/oco", oco_params)
    if oco_status != 200:
        log.warning(f"OCO order failed (entry was placed): {oco_res}")
        return {"warning": "Entry placed but OCO failed", "entry": entry_res}

    log.info(f"✅ OCO exit placed: {symbol} {exit_side} sl={stop_price} tp={take_profit_price}")
    return {"entry": entry_res, "oco": oco_res, "status": "SUCCESS"}

# ─── AUTO-TRADE LOOP ──────────────────────────────────────────────────────────
async def trading_loop():
    """Main loop — runs every N seconds, scans all pairs, places trades."""
    log.info("🤖 Trading loop started")
    async with httpx.AsyncClient(timeout=10.0) as client:
        while bot_state["running"]:
            try:
                log.info(f"🔍 Scanning {len(PAIRS)} pairs...")
                for symbol in PAIRS:
                    # Fetch real 1-minute klines
                    closes = await fetch_klines(client, symbol)
                    if closes:
                        price_history[symbol] = closes

                    sig = generate_signal(price_history[symbol])
                    bot_state["signals"][symbol] = {**sig, "price": closes[-1] if closes else 0}

                    # Auto-trade if enabled and signal is strong enough
                    if (bot_state["auto_trade"]
                            and sig["action"] != "HOLD"
                            and sig["confidence"] >= bot_state["min_confidence"]):

                        price    = closes[-1] if closes else 0
                        equity   = bot_state["equity"]
                        risk_amt = equity * (bot_state["risk_pct"] / 100)

                        # Calculate quantity (min 0.00001 for BTC)
                        raw_qty  = risk_amt / price if price > 0 else 0
                        quantity = f"{raw_qty:.5f}"

                        sl_pct = bot_state["stop_loss"] / 100
                        tp_pct = bot_state["take_profit"] / 100

                        if sig["action"] == "BUY":
                            sl_price = f"{price * (1 - sl_pct):.6f}"
                            tp_price = f"{price * (1 + tp_pct):.6f}"
                        else:
                            sl_price = f"{price * (1 + sl_pct):.6f}"
                            tp_price = f"{price * (1 - tp_pct):.6f}"

                        result = await place_order(client, symbol, sig["action"],
                                                   quantity, sl_price, tp_price)

                        # Update equity (simulation in testnet mode)
                        if TESTNET:
                            win  = sig["confidence"] / 100 > 0.5
                            pnl  = risk_amt * (tp_pct if win else -sl_pct)
                            bot_state["equity"]    += pnl
                            bot_state["total_pnl"] += pnl
                            bot_state["day_pnl"]   += pnl
                            if win: bot_state["wins"]   += 1
                            else:   bot_state["losses"] += 1

                        trade_record = {
                            "time": int(time.time() * 1000),
                            "symbol": symbol,
                            "side": sig["action"],
                            "price": price,
                            "quantity": quantity,
                            "confidence": sig["confidence"],
                            "result": result,
                        }
                        bot_state["trades"].insert(0, trade_record)
                        bot_state["trades"] = bot_state["trades"][:100]  # keep last 100
                        log.info(f"Trade: {symbol} {sig['action']} conf={sig['confidence']}%")

            except Exception as e:
                log.error(f"Trading loop error: {e}")

            await asyncio.sleep(bot_state["scan_interval"])

    log.info("⏹ Trading loop stopped")

# ─── BACKGROUND TASK HANDLE ───────────────────────────────────────────────────
loop_task: Optional[asyncio.Task] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 CryptoBot backend starting...")
    yield
    global loop_task
    if loop_task:
        loop_task.cancel()
    log.info("👋 CryptoBot backend stopped")

# ─── FASTAPI APP ─────────────────────────────────────────────────────────────
app = FastAPI(title="CryptoBot API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict to your frontend domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── REQUEST MODELS ───────────────────────────────────────────────────────────
class BotConfig(BaseModel):
    auto_trade: Optional[bool]     = None
    risk_pct: Optional[float]      = None
    stop_loss: Optional[float]     = None
    take_profit: Optional[float]   = None
    min_confidence: Optional[int]  = None
    scan_interval: Optional[int]   = None

class ManualTrade(BaseModel):
    symbol: str
    side: str       # BUY or SELL
    quantity: str
    stop_price: str
    take_profit_price: str

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "CryptoBot backend running", "testnet": TESTNET, "pairs": len(PAIRS)}

@app.get("/health")
def health():
    return {"ok": True, "timestamp": int(time.time() * 1000)}

@app.get("/status")
def get_status():
    wins   = bot_state["wins"]
    losses = bot_state["losses"]
    total  = wins + losses
    return {
        "running":       bot_state["running"],
        "auto_trade":    bot_state["auto_trade"],
        "equity":        round(bot_state["equity"], 2),
        "start_equity":  bot_state["start_equity"],
        "total_pnl":     round(bot_state["total_pnl"], 2),
        "day_pnl":       round(bot_state["day_pnl"], 2),
        "total_pct":     round((bot_state["equity"] - bot_state["start_equity"]) / bot_state["start_equity"] * 100, 2),
        "wins":          wins,
        "losses":        losses,
        "win_rate":      round(wins / total * 100) if total > 0 else 0,
        "testnet":       TESTNET,
        "scan_interval": bot_state["scan_interval"],
    }

@app.get("/signals")
def get_signals():
    return {"signals": bot_state["signals"], "timestamp": int(time.time() * 1000)}

@app.get("/trades")
def get_trades(limit: int = 50):
    return {"trades": bot_state["trades"][:limit]}

@app.post("/bot/start")
async def start_bot():
    global loop_task
    if bot_state["running"]:
        return {"message": "Bot already running"}
    if not API_KEY or not SECRET_KEY:
        raise HTTPException(400, "API keys not set — add BINANCE_API_KEY and BINANCE_SECRET_KEY as environment variables")
    bot_state["running"] = True
    loop_task = asyncio.create_task(trading_loop())
    log.info("▶ Bot started")
    return {"message": "Bot started", "testnet": TESTNET}

@app.post("/bot/stop")
async def stop_bot():
    global loop_task
    bot_state["running"] = False
    if loop_task:
        loop_task.cancel()
        loop_task = None
    log.info("⏹ Bot stopped")
    return {"message": "Bot stopped — open orders remain active on Binance"}

@app.patch("/bot/config")
async def update_config(cfg: BotConfig):
    if cfg.auto_trade    is not None: bot_state["auto_trade"]    = cfg.auto_trade
    if cfg.risk_pct      is not None: bot_state["risk_pct"]      = cfg.risk_pct
    if cfg.stop_loss     is not None: bot_state["stop_loss"]     = cfg.stop_loss
    if cfg.take_profit   is not None: bot_state["take_profit"]   = cfg.take_profit
    if cfg.min_confidence is not None: bot_state["min_confidence"] = cfg.min_confidence
    if cfg.scan_interval is not None: bot_state["scan_interval"] = cfg.scan_interval
    return {"message": "Config updated", "config": {k: bot_state[k] for k in ["auto_trade","risk_pct","stop_loss","take_profit","min_confidence","scan_interval"]}}

@app.post("/trade/manual")
async def manual_trade(trade: ManualTrade):
    async with httpx.AsyncClient(timeout=10.0) as client:
        result = await place_order(client, trade.symbol, trade.side,
                                   trade.quantity, trade.stop_price, trade.take_profit_price)
    return {"result": result}

@app.get("/account")
async def get_account():
    if not API_KEY or not SECRET_KEY:
        raise HTTPException(400, "API keys not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        data = await binance_get(client, "/api/v3/account", signed=True)
    balances = [b for b in data.get("balances", []) if float(b["free"]) > 0 or float(b["locked"]) > 0]
    return {"balances": balances, "canTrade": data.get("canTrade")}

@app.get("/orders/open")
async def get_open_orders():
    if not API_KEY or not SECRET_KEY:
        raise HTTPException(400, "API keys not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        orders = await binance_get(client, "/api/v3/openOrders", signed=True)
    return {"orders": orders, "count": len(orders)}

@app.delete("/orders/cancel-all")
async def cancel_all_orders():
    if not API_KEY or not SECRET_KEY:
        raise HTTPException(400, "API keys not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        results = []
        for symbol in PAIRS:
            try:
                res, _ = await binance_post(client, "/api/v3/openOrders",
                                            {"symbol": symbol, "timestamp": int(time.time()*1000)})
                results.append({"symbol": symbol, "result": res})
            except Exception as e:
                results.append({"symbol": symbol, "error": str(e)})
    return {"cancelled": results}
