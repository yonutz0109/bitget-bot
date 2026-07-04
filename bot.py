import requests
import hmac
import hashlib
import base64
import time
import json
import os
import math
from datetime import datetime

API_KEY = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

BASE_URL = "https://api.bitget.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BGBUSDT", "UNIUSDT", "DOGEUSDT"]
POSITIONS_FILE = "positions.json"

# Parametri strategie
RSI_PERIOD = 14
RSI_BUY_1H = 40
RSI_MIN_4H = 40
RSI_SELL = 65
STOP_LOSS = 0.03
TRAILING_TRIGGER = 0.02
TRAILING_DISTANCE = 0.015

# Stocăm preciziile simbolurilor aici
SYMBOL_PRECISION = {}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log(f"Telegram error: {e}")

# --- PERSISTENȚĂ POZIȚII ---
def load_positions():
    global positions
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    else:
        positions = {}

def save_positions():
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=4)

# --- API HELPERS ---
def sign(message, secret):
    mac = hmac.new(bytes(secret, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
    return base64.b64encode(mac.digest()).decode()

def get_timestamp():
    return str(int(time.time() * 1000))

def get_headers(method, path, body=""):
    ts = get_timestamp()
    msg = ts + method.upper() + path + (body if body else "")
    return {
        "ACCESS-KEY": API_KEY, "ACCESS-SIGN": sign(msg, SECRET_KEY),
        "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json", "locale": "en-US"
    }

def fetch_symbol_precision():
    """Obține regulile de lot size de la Bitget la pornire"""
    global SYMBOL_PRECISION
    path = "/api/v2/spot/instruments?productType=SPOT"
    r = requests.get(BASE_URL + path)
    data = r.json()
    if data.get("code") == "00000":
        for inst in data.get("data", []):
            if inst["symbol"] in SYMBOLS:
                # basePrecision ne spune la câte zecimale putem vinde
                SYMBOL_PRECISION[inst["symbol"]] = int(inst.get("basePrecision", "8"))

def get_spot_balance(coin="USDT"):
    path = f"/api/v2/spot/account/assets?coin={coin}"
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path))
    data = r.json()
    if data.get("code") == "00000" and data.get("data"):
        return float(data["data"][0].get("available", 0))
    return 0.0

def get_candles(symbol, granularity="1h", limit=250):
    path = f"/api/v2/spot/market/candles?symbol={symbol}&granularity={granularity}&limit={limit}"
    r = requests.get(BASE_URL + path)
    if r.json().get("code") == "00000":
        return r.json().get("data", [])
    return []

def get_closes(candles):
    return [float(c[4]) for c in reversed(candles)]

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0)); losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / period; avg_loss = sum(losses) / period
    if avg_loss == 0: return 100
    rsi = 100 - (100 / (1 + (avg_gain / avg_loss)))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i-1]
        gain = max(diff, 0); loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0: rsi = 100
        else: rsi = 100 - (100 / (1 + (avg_gain / avg_loss)))
    return round(rsi, 2)

def calculate_ema(closes, period=200):
    if len(closes) < period: return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def get_current_price(symbol):
    path = f"/api/v2/spot/market/tickers?symbol={symbol}"
    r = requests.get(BASE_URL + path)
    data = r.json()
    if data.get("code") == "00000" and data.get("data"):
        return float(data["data"][0]["lastPr"])
    return 0.0

def place_order(symbol, side, amount_usdt=None, quantity=None):
    if DRY_RUN:
        log(f"[DRY_RUN] Simulez {side} {symbol}")
        return {"code": "00000", "dry_run": True}
    
    path = "/api/v2/spot/trade/place-order"
    if side == "buy":
        body = {"symbol": symbol, "side": "buy", "orderType": "market", "force": "gtc", "quoteSize": str(round(amount_usdt, 2))}
    else:
        # Aplicăm precizia corectă aici ca să evităm erorile API
        precision = SYMBOL_PRECISION.get(symbol, 6)
        rounded_qty = math.floor(quantity * (10 ** precision)) / (10 ** precision)
        if rounded_qty <= 0:
            log(f"Eroare: Cantitatea calculată e prea mică pentru a vinde ({quantity})")
            return {"code": "error"}
        body = {"symbol": symbol, "side": "sell", "orderType": "market", "force": "gtc", "size": str(rounded_qty)}
    
    body_str = json.dumps(body)
    r = requests.post(BASE_URL + path, headers=get_headers("POST", path, body_str), data=body_str)
    return r.json()

def run_bot():
    load_positions()
    fetch_symbol_precision()
    
    mode = "🧪 DRY RUN" if DRY_RUN else "💰 LIVE"
    start_msg = f"🤖 Bot pornit! Mod: {mode}\nMonitorizez: {', '.join(SYMBOLS)}\nStrategie: RSI 1H<{RSI_BUY_1H} + RSI 4H>{RSI_MIN_4H} + preț>EMA200 + trailing stop"
    log(start_msg)
    send_telegram(start_msg)
    
    while True:
        try:
            usdt_balance = get_spot_balance("USDT")
            log(f"💰 Balanță: ${usdt_balance:.2f} | Poziții deschise: {list(positions.keys())}")
            
            for symbol in SYMBOLS:
                coin = symbol.replace("USDT", "")
                candles_1h = get_candles(symbol, "1h", 250)
                candles_4h = get_candles(symbol, "4h", 100)
                if not candles_1h or not candles_4h: continue
                
                closes_1h = get_closes(candles_1h)
                closes_4h = get_closes(candles_4h)
                
                rsi_1h = calculate_rsi(closes_1h, RSI_PERIOD)
                rsi_4h = calculate_rsi(closes_4h, RSI_PERIOD)
                ema200 = calculate_ema(closes_1h, 200)
                price = get_current_price(symbol)
                if price == 0 or ema200 is None: continue
                
                above_ema = price > ema200
                log(f"📊 {symbol} | ${price:.4f} | RSI1H: {rsi_1h} | RSI4H: {rsi_4h} | EMA200: {'📈' if above_ema else '📉'}")
                
                # === CUMPARARE ===
                if symbol not in positions:
                    if rsi_1h < RSI_BUY_1H and rsi_4h > RSI_MIN_4H and above_ema:
                        if usdt_balance >= 5:
                            trade_amount = usdt_balance * 0.2
                            result = place_order(symbol, "buy", amount_usdt=trade_amount)
                            if result.get("code") == "00000":
                                quantity = trade_amount / price
                                positions[symbol] = {"price": price, "quantity": quantity, "peak": price}
                                save_positions() # SALVĂM POZIȚIA
                                msg = f"🟢 BUY {symbol}\n💵 ${trade_amount:.2f} la ${price:.4f}\n📊 RSI1H={rsi_1h}, RSI4H={rsi_4h}\n{'🧪 SIMULARE' if DRY_RUN else '💰 REAL'}"
                                log(msg); send_telegram(msg)
                                usdt_balance -= trade_amount
                
                # === VANZARE ===
                else:
                    pos = positions[symbol]
                    entry = pos["price"]
                    pos["peak"] = max(pos["peak"], price)
                    pnl_pct = (price - entry) / entry
                    peak_pnl = (pos["peak"] - entry) / entry
                    drop_from_peak = (pos["peak"] - price) / pos["peak"]
                    
                    should_sell, reason = False, ""
                    if pnl_pct <= -STOP_LOSS:
                        should_sell, reason = True, f"🛑 Stop-loss {pnl_pct*100:.1f}%"
                    elif peak_pnl >= TRAILING_TRIGGER and drop_from_peak >= TRAILING_DISTANCE:
                        should_sell, reason = True, f"📉 Trailing stop (vârf +{peak_pnl*100:.1f}%, acum +{pnl_pct*100:.1f}%)"
                    elif rsi_1h > RSI_SELL:
                        should_sell, reason = True, f"📊 RSI={rsi_1h} > {RSI_SELL}"
                    
                    if should_sell:
                        sell_qty = pos["quantity"] if DRY_RUN else get_spot_balance(coin)
                        if sell_qty > 0:
                            result = place_order(symbol, "sell", quantity=sell_qty)
                            if result.get("code") == "00000":
                                emoji = "✅" if pnl_pct > 0 else "❌"
                                msg = f"🔴 SELL {symbol}\n{reason}\n{emoji} PnL: {pnl_pct*100:+.1f}%\nPreț: ${price:.4f}\n{'🧪 SIMULARE' if DRY_RUN else '💰 REAL'}"
                                log(msg); send_telegram(msg)
                                del positions[symbol]
                                save_positions() # SALVĂM ȘTERGEREA POZIȚIEI
            
            log("⏳ Aștept 5 minute...\n")
            time.sleep(300)
            
        except Exception as e:
            log(f"❌ Eroare: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
