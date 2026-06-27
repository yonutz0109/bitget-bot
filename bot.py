import requests
import hmac
import hashlib
import base64
import time
import json
import os
from datetime import datetime

API_KEY = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")

BASE_URL = "https://api.bitget.com"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]

RSI_PERIOD = 14
RSI_BUY = 30
RSI_SELL = 70
PROFIT_TARGET = 0.05
STOP_LOSS = 0.03

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

def get_spot_balance(coin="USDT"):
    path = "/api/v2/spot/account/assets"
    params = f"?coin={coin}"
    full_path = path + params
    headers = get_headers("GET", full_path)
    r = requests.get(BASE_URL + full_path, headers=headers)
    data = r.json()
    log(f"DEBUG balance: {data}")
    if data.get("code") == "00000":
        for asset in data.get("data", []):
            if asset["coin"] == coin:
                return float(asset["available"])
    return 0.0

def get_coin_balance(coin):
    path = "/api/v2/spot/account/assets"
    params = f"?coin={coin}"
    full_path = path + params
    headers = get_headers("GET", full_path)
    r = requests.get(BASE_URL + full_path, headers=headers)
    data = r.json()
    if data.get("code") == "00000":
        for asset in data.get("data", []):
            if asset["coin"] == coin:
                return float(asset["available"])
    return 0.0

def get_candles(symbol, granularity="1H", limit=100):
    path = f"/api/v2/spot/market/candles?symbol={symbol}&granularity={granularity}&limit={limit}"
    r = requests.get(BASE_URL + path)
    data = r.json()
    if data.get("code") == "00000":
        return data.get("data", [])
    return []

def calculate_rsi(candles, period=14):
    closes = [float(c[4]) for c in reversed(candles)]
    if len(closes) < period + 1:
        return 50
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i-1]
        gain = diff if diff > 0 else 0
        loss = abs(diff) if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)

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
    path = "/api/v2/spot/trade/place-order"
    if side == "buy":
        body = {
            "symbol": symbol,
            "side": "buy",
            "orderType": "market",
            "force": "gtc",
            "quoteSize": str(round(amount_usdt, 2))
        }
    else:
        body = {
            "symbol": symbol,
            "side": "sell",
            "orderType": "market",
            "force": "gtc",
            "size": str(quantity)
        }
    body_str = json.dumps(body)
    headers = get_headers("POST", path, body_str)
    r = requests.post(BASE_URL + path, headers=headers, data=body_str)
    return r.json()

positions = {}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def run_bot():
    log("🤖 Bot pornit! Monitorizez: " + ", ".join(SYMBOLS))
    while True:
        try:
            usdt_balance = get_spot_balance("USDT")
            log(f"💰 Balanță USDT: ${usdt_balance:.2f}")
            for symbol in SYMBOLS:
                coin = symbol.replace("USDT", "")
                candles = get_candles(symbol, "1H", 50)
                if not candles:
                    continue
                rsi = calculate_rsi(candles, RSI_PERIOD)
                price = get_current_price(symbol)
                log(f"📊 {symbol} | Preț: ${price:.4f} | RSI: {rsi}")
                if rsi < RSI_BUY and symbol not in positions:
                    if usdt_balance >= 5:
                        trade_amount = usdt_balance * 0.3
                        log(f"🟢 BUY {symbol} | RSI={rsi} | ${trade_amount:.2f}")
                        result = place_order(symbol, "buy", amount_usdt=trade_amount)
                        if result.get("code") == "00000":
                            quantity = trade_amount / price
                            positions[symbol] = {"price": price, "quantity": quantity}
                            log(f"✅ Cumpărat {quantity:.6f} {coin} la ${price:.4f}")
                        else:
                            log(f"❌ Eroare BUY: {result}")
                elif symbol in positions:
                    entry_price = positions[symbol]["price"]
                    quantity = positions[symbol]["quantity"]
                    pnl_pct = (price - entry_price) / entry_price
                    should_sell = False
                    reason = ""
                    if rsi > RSI_SELL:
                        should_sell = True
                        reason = f"RSI={rsi} > {RSI_SELL}"
                    elif pnl_pct >= PROFIT_TARGET:
                        should_sell = True
                        reason = f"Profit +{pnl_pct*100:.1f}%"
                    elif pnl_pct <= -STOP_LOSS:
                        should_sell = True
                        reason = f"Stop-loss {pnl_pct*100:.1f}%"
                    if should_sell:
                        coin_balance = get_coin_balance(coin)
                        if coin_balance > 0:
                            log(f"🔴 SELL {symbol} | {reason} | {coin_balance:.6f} {coin}")
                            result = place_order(symbol, "sell", quantity=coin_balance)
                            if result.get("code") == "00000":
                                log(f"✅ Vândut la ${price:.4f} | PnL: {pnl_pct*100:.1f}%")
                                del positions[symbol]
                            else:
                                log(f"❌ Eroare SELL: {result}")
            log("⏳ Aștept 5 minute...\n")
            time.sleep(300)
        except Exception as e:
            log(f"❌ Eroare: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
