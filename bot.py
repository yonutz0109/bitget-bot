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

RSI_PERIOD = 14
RSI_BUY_15M = 45
RSI_MIN_1H = 32
RSI_SELL = 65
EMA_TOLERANCE = 0.985
STOP_LOSS = 0.02
TRAILING_TRIGGER = 0.01
TRAILING_DISTANCE = 0.007

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log(f"Telegram error: {e}")

def sign(message, secret):
    mac = hmac.new(bytes(secret, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
    d = mac.digest()
    return base64.b64encode(d).decode()

def get_timestamp():
    return str(int(time.time() * 1000))

def get_headers(method, path, body=""):
    ts = get_timestamp()
    msg = ts + method.upper() + path + (body if body else "")
    signature = sign(msg, SECRET_KEY)
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US"
    }

# Precizia (nr. zecimale) pentru cantitate, per simbol - citita de la Bitget
quantity_precision = {}

def load_symbol_precision():
    try:
        path = "/api/v2/spot/public/symbols"
        r = requests.get(BASE_URL + path)
        data = r.json()
        if data.get("code") == "00000":
            for s in data.get("data", []):
                sym = s.get("symbol", "")
                if sym in SYMBOLS:
                    quantity_precision[sym] = int(s.get("quantityPrecision", 4))
            log(f"Precizie cantitate: {quantity_precision}")
    except Exception as e:
        log(f"Eroare la citirea preciziei: {e}")

def floor_qty(symbol, qty):
    decimals = quantity_precision.get(symbol, 4)
    factor = 10 ** decimals
    return math.floor(qty * factor) / factor

def get_spot_balance(coin="USDT"):
    path = "/api/v2/spot/account/assets"
    full_path = path + f"?coin={coin}"
    headers = get_headers("GET", full_path)
    r = requests.get(BASE_URL + full_path, headers=headers)
    data = r.json()
    if data.get("code") == "00000":
        for asset in data.get("data", []):
            if asset["coin"] == coin:
                return float(asset["available"])
    return 0.0

def get_candles(symbol, granularity="15min", limit=150):
    path = f"/api/v2/spot/market/candles?symbol={symbol}&granularity={granularity}&limit={limit}"
    r = requests.get(BASE_URL + path)
    data = r.json()
    if data.get("code") == "00000":
        return data.get("data", [])
    return []

def get_closes(candles):
    return [float(c[4]) for c in reversed(candles)]

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i-1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)

def calculate_ema(closes, period=50):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def get_current_price(symbol):
    path = f"/api/v2/spot/market/tickers?symbol={symbol}"
    r = requests.get(BASE_URL + path)
    data = r.json()
    if data.get("code") == "00000":
        tickers = data.get("data", [])
        if tickers:
            return float(tickers[0]["lastPr"])
    return 0.0

def place_order(symbol, side, amount_usdt=None, quantity=None):
    if DRY_RUN:
        log(f"[DRY_RUN] Simulez {side} {symbol}")
        return {"code": "00000", "dry_run": True}
    path = "/api/v2/spot/trade/place-order"
    if side == "buy":
        body = {"symbol": symbol, "side": "buy", "orderType": "market", "force": "gtc", "size": str(round(amount_usdt, 2))}
    else:
        qty = floor_qty(symbol, quantity)
        body = {"symbol": symbol, "side": "sell", "orderType": "market", "force": "gtc", "size": str(qty)}
    body_str = json.dumps(body)
    headers = get_headers("POST", path, body_str)
    r = requests.post(BASE_URL + path, headers=headers, data=body_str)
    return r.json()

def get_coin_balance(coin):
    return get_spot_balance(coin)

positions = {}

def run_bot():
    mode = "🧪 DRY RUN (simulare)" if DRY_RUN else "💰 LIVE (bani reali)"
    load_symbol_precision()
    start_msg = f"🤖 Bot pornit (v5 - fix vanzare zecimale)! Mod: {mode}\nMonitorizez: {', '.join(SYMBOLS)}"
    log(start_msg)
    send_telegram(start_msg)
    
    while True:
        try:
            usdt_balance = get_spot_balance("USDT")
            log(f"💰 Balanță USDT: ${usdt_balance:.2f} | Poziții: {len(positions)}")
            
            for symbol in SYMBOLS:
                coin = symbol.replace("USDT", "")
                
                candles_15m = get_candles(symbol, "15min", 150)
                candles_1h = get_candles(symbol, "1h", 100)
                if not candles_15m or not candles_1h:
                    continue
                
                closes_15m = get_closes(candles_15m)
                closes_1h = get_closes(candles_1h)
                
                rsi_15m = calculate_rsi(closes_15m, RSI_PERIOD)
                rsi_1h = calculate_rsi(closes_1h, RSI_PERIOD)
                ema50 = calculate_ema(closes_15m, 50)
                price = get_current_price(symbol)
                
                if price == 0 or ema50 is None:
                    continue
                
                ema_ok = price > ema50 * EMA_TOLERANCE
                trend = "✅" if ema_ok else "❌"
                log(f"📊 {symbol} | ${price:.4f} | RSI15m: {rsi_15m} | RSI1H: {rsi_1h} | EMA: {trend}")
                
                if symbol not in positions:
                    if rsi_15m < RSI_BUY_15M and rsi_1h > RSI_MIN_1H and ema_ok:
                        if usdt_balance >= 5:
                            trade_amount = usdt_balance * 0.25
                            result = place_order(symbol, "buy", amount_usdt=trade_amount)
                            if result.get("code") == "00000":
                                quantity = trade_amount / price
                                positions[symbol] = {"price": price, "quantity": quantity, "peak": price}
                                msg = f"🟢 BUY {symbol}\n💵 ${trade_amount:.2f} la ${price:.4f}\n📊 RSI15m={rsi_15m}, RSI1H={rsi_1h}\n{'🧪 SIMULARE' if DRY_RUN else '💰 REAL'}"
                                log(msg)
                                send_telegram(msg)
                                usdt_balance -= trade_amount
                            else:
                                log(f"❌ Eroare BUY: {result}")
                                send_telegram(f"❌ Eroare BUY {symbol}: {result.get('msg', 'necunoscut')}")
                
                else:
                    pos = positions[symbol]
                    entry = pos["price"]
                    pos["peak"] = max(pos["peak"], price)
                    pnl_pct = (price - entry) / entry
                    peak_pnl = (pos["peak"] - entry) / entry
                    drop_from_peak = (pos["peak"] - price) / pos["peak"]
                    
                    should_sell = False
                    reason = ""
                    
                    if pnl_pct <= -STOP_LOSS:
                        should_sell = True
                        reason = f"🛑 Stop-loss {pnl_pct*100:.1f}%"
                    elif peak_pnl >= TRAILING_TRIGGER and drop_from_peak >= TRAILING_DISTANCE:
                        should_sell = True
                        reason = f"📉 Trailing stop (vârf +{peak_pnl*100:.1f}%, acum +{pnl_pct*100:.1f}%)"
                    elif rsi_15m > RSI_SELL:
                        should_sell = True
                        reason = f"📊 RSI={rsi_15m} > {RSI_SELL}"
                    
                    if should_sell:
                        if DRY_RUN:
                            sell_qty = pos["quantity"]
                        else:
                            sell_qty = get_coin_balance(coin)
                        sell_qty = floor_qty(symbol, sell_qty)
                        if sell_qty > 0:
                            result = place_order(symbol, "sell", quantity=sell_qty)
                            if result.get("code") == "00000":
                                emoji = "✅" if pnl_pct > 0 else "❌"
                                msg = f"🔴 SELL {symbol}\n{reason}\n{emoji} PnL: {pnl_pct*100:+.1f}%\nPreț: ${price:.4f}\n{'🧪 SIMULARE' if DRY_RUN else '💰 REAL'}"
                                log(msg)
                                send_telegram(msg)
                                del positions[symbol]
                            else:
                                log(f"❌ Eroare SELL: {result}")
                                send_telegram(f"❌ Eroare SELL {symbol}: {result.get('msg', 'necunoscut')}")
                        else:
                            del positions[symbol]
            
            log("⏳ Aștept 2 minute...\n")
            time.sleep(120)
            
        except Exception as e:
            log(f"❌ Eroare: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
