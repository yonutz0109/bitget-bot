import requests
import hmac
import hashlib
import base64
import time
import json
import os
import math
import uuid
import signal
import sys
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- CONFIG ----------------
API_KEY = os.environ.get("BITGET_API_KEY", "")
SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

BASE_URL = "https://api.bitget.com"
STATE_FILE = "bot_state.json"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BGBUSDT", "UNIUSDT", "DOGEUSDT"]

RSI_PERIOD = 14
RSI_BUY_15M = 45
RSI_MIN_1H = 32

# Limita superioara RSI1H la cumparare - datele reale au aratat cumparari facute
# cu RSI1H deja la 66-67 (deja supracumparat pe termen mai lung), ceea ce a dus
# rapid la iesiri pe RSI_SELL sau stop-loss. Nu mai cumparam peste acest prag.
RSI_MAX_1H = 60

RSI_SELL = 65
EMA_TOLERANCE = 0.985

# Marit de la 2% la 2.5% - 6 din 15 vanzari reale au fost stop-loss exact la
# -2.0/-2.1/-2.4%, semn ca 2% era prea strans pentru volatilitatea normala.
STOP_LOSS = 0.025

# Trigger si distanta marite - inainte iesea la doar +0.5/+0.8% profit
# (varf +1.2-1.5%, distanta 0.7%). Acum lasam mai mult loc de crestere.
TRAILING_TRIGGER = 0.015
TRAILING_DISTANCE = 0.012

# RSI>65 vinde doar daca pretul a inceput deja sa scada de la varf (nu in timp
# ce inca urca), ca sa nu taie trenduri bune doar pentru ca RSI e mare.
RSI_SELL_MIN_DROP_FROM_PEAK = 0.003  # 0.3%

# Cooldown dupa o vanzare pe acelasi simbol, ca sa evite reintrari rapide in
# acelasi pattern (UNI a fost vandut si recumparat de mai multe ori in aceeasi zi).
COOLDOWN_MINUTES = 45

MAX_CONCURRENT_POSITIONS = 3          # nu mai mult de N pozitii deschise simultan
RISK_PER_TRADE = 0.05                 # % din capitalul TOTAL riscat per tranzactie (nu alocat!)
MAX_ALLOCATION_PER_TRADE = 0.25       # cap superior, indiferent de calculul de risc
MIN_TRADE_USDT = 5
REQUEST_TIMEOUT = 10
LOOP_INTERVAL = 120

if not DRY_RUN and not (API_KEY and SECRET_KEY and PASSPHRASE):
    print("EROARE: lipsesc credențialele Bitget și DRY_RUN=false. Opresc botul.")
    sys.exit(1)

# ---------------- HTTP SESSION CU RETRY ----------------
session = requests.Session()
retry_cfg = Retry(total=3, backoff_factor=0.5,
                   status_forcelist=[429, 500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry_cfg))


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        log(f"Telegram error: {e}")


def sign(message, secret):
    mac = hmac.new(bytes(secret, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
    return base64.b64encode(mac.digest()).decode()


def get_timestamp():
    return str(int(time.time() * 1000))


def get_headers(method, path, body=""):
    ts = get_timestamp()
    msg = ts + method.upper() + path + (body if body else "")
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign(msg, SECRET_KEY),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US"
    }


def safe_request(method, url, headers=None, data=None, params=None):
    """Wrapper cu timeout + verificare status + parsare JSON sigura."""
    try:
        r = session.request(method, url, headers=headers, data=data, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log(f"HTTP {r.status_code} la {url}: {r.text[:200]}")
            return None
        return r.json()
    except requests.exceptions.RequestException as e:
        log(f"Eroare rețea la {url}: {e}")
        return None
    except ValueError as e:
        log(f"Răspuns invalid (non-JSON) de la {url}: {e}")
        return None


# ---------------- PERSISTENȚĂ STARE ----------------
def load_state():
    global positions, last_sell_time
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
            positions = saved.get("positions", {})
            last_sell_time = saved.get("last_sell_time", {})
            log(f"Stare încărcată din {STATE_FILE}: {len(positions)} poziții, {len(last_sell_time)} cooldown-uri")
        except Exception as e:
            log(f"Nu am putut încărca starea: {e}")
            positions = {}
            last_sell_time = {}
    else:
        positions = {}
        last_sell_time = {}


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"positions": positions, "last_sell_time": last_sell_time}, f, indent=2)
    except Exception as e:
        log(f"Nu am putut salva starea: {e}")


def handle_shutdown(signum, frame):
    log("Semnal de oprire primit, salvez starea...")
    save_state()
    sys.exit(0)


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# ---------------- PRECIZIE CANTITATE ----------------
quantity_precision = {}


def load_symbol_precision():
    path = "/api/v2/spot/public/symbols"
    data = safe_request("GET", BASE_URL + path)
    if data and data.get("code") == "00000":
        for s in data.get("data", []):
            sym = s.get("symbol", "")
            if sym in SYMBOLS:
                quantity_precision[sym] = int(s.get("quantityPrecision", 4))
        log(f"Precizie cantitate: {quantity_precision}")
    else:
        log("Nu am putut încărca precizia simbolurilor, folosesc default 4 zecimale.")


def floor_qty(symbol, qty):
    decimals = quantity_precision.get(symbol, 4)
    factor = 10 ** decimals
    return math.floor(qty * factor) / factor


# ---------------- API BITGET ----------------
def get_spot_balance(coin="USDT"):
    path = "/api/v2/spot/account/assets"
    full_path = path + f"?coin={coin}"
    headers = get_headers("GET", full_path)
    data = safe_request("GET", BASE_URL + full_path, headers=headers)
    if data and data.get("code") == "00000":
        for asset in data.get("data", []):
            if asset["coin"] == coin:
                return float(asset["available"])
    return 0.0


def get_candles(symbol, granularity="15min", limit=150):
    path = f"/api/v2/spot/market/candles?symbol={symbol}&granularity={granularity}&limit={limit}"
    data = safe_request("GET", BASE_URL + path)
    if data and data.get("code") == "00000":
        return data.get("data", [])
    return []


def get_closes(candles):
    return [float(c[4]) for c in reversed(candles)]


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        rsi = 100
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
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
    data = safe_request("GET", BASE_URL + path)
    if data and data.get("code") == "00000":
        tickers = data.get("data", [])
        if tickers:
            return float(tickers[0]["lastPr"])
    return 0.0


def place_order(symbol, side, amount_usdt=None, quantity=None):
    client_oid = str(uuid.uuid4())  # idempotență: fiecare ordin are un id unic
    if DRY_RUN:
        log(f"[DRY_RUN] Simulez {side} {symbol} (clientOid={client_oid})")
        return {"code": "00000", "dry_run": True, "clientOid": client_oid}

    path = "/api/v2/spot/trade/place-order"
    if side == "buy":
        body = {"symbol": symbol, "side": "buy", "orderType": "market", "force": "gtc",
                "size": str(round(amount_usdt, 2)), "clientOid": client_oid}
    else:
        qty = floor_qty(symbol, quantity)
        body = {"symbol": symbol, "side": "sell", "orderType": "market", "force": "gtc",
                "size": str(qty), "clientOid": client_oid}

    body_str = json.dumps(body)
    headers = get_headers("POST", path, body_str)
    result = safe_request("POST", BASE_URL + path, headers=headers, data=body_str)
    return result if result is not None else {"code": "error", "msg": "no response"}


def get_coin_balance(coin):
    return get_spot_balance(coin)


# ---------------- RISC / SIZING ----------------
def compute_trade_size(usdt_balance, total_equity):
    """
    Risc = % din capitalul TOTAL, calculat pe baza distanței până la stop-loss,
    nu procent fix din balanța disponibilă (evită supra-alocarea).
    """
    risk_amount = total_equity * RISK_PER_TRADE
    size_from_risk = risk_amount / STOP_LOSS
    max_allowed = usdt_balance * MAX_ALLOCATION_PER_TRADE
    return min(size_from_risk, max_allowed, usdt_balance)


# ---------------- BOT LOOP ----------------
positions = {}
last_sell_time = {}


def run_bot():
    mode = "🧪 DRY RUN (simulare)" if DRY_RUN else "💰 LIVE (bani reali)"
    load_symbol_precision()
    load_state()

    start_msg = (f"🤖 Bot pornit (v8 - infra robustă + risc ajustat)! Mod: {mode}\n"
                 f"Monitorizez: {', '.join(SYMBOLS)}")
    log(start_msg)
    send_telegram(start_msg)

    while True:
        try:
            usdt_balance = get_spot_balance("USDT")
            total_equity = usdt_balance  # simplificat; poți extinde cu valoarea pozițiilor deschise
            log(f"💰 Balanță USDT: ${usdt_balance:.2f} | Poziții: {len(positions)}/{MAX_CONCURRENT_POSITIONS}")

            for symbol in SYMBOLS:
                try:
                    coin = symbol.replace("USDT", "")

                    candles_15m = get_candles(symbol, "15min", 150)
                    candles_1h = get_candles(symbol, "1h", 100)
                    if not candles_15m or not candles_1h:
                        log(f"⚠️ {symbol}: date lipsă, sar peste.")
                        continue

                    closes_15m = get_closes(candles_15m)
                    closes_1h = get_closes(candles_1h)

                    rsi_15m = calculate_rsi(closes_15m, RSI_PERIOD)
                    rsi_1h = calculate_rsi(closes_1h, RSI_PERIOD)
                    ema50 = calculate_ema(closes_15m, 50)
                    price = get_current_price(symbol)

                    if price == 0 or ema50 is None:
                        log(f"⚠️ {symbol}: preț sau EMA invalide, sar peste.")
                        continue

                    ema_ok = price > ema50 * EMA_TOLERANCE
                    trend = "✅" if ema_ok else "❌"
                    log(f"📊 {symbol} | ${price:.4f} | RSI15m: {rsi_15m} | RSI1H: {rsi_1h} | EMA: {trend}")

                    if symbol not in positions:
                        if len(positions) >= MAX_CONCURRENT_POSITIONS:
                            continue

                        in_cooldown = False
                        if symbol in last_sell_time:
                            minutes_since_sell = (time.time() - last_sell_time[symbol]) / 60
                            if minutes_since_sell < COOLDOWN_MINUTES:
                                in_cooldown = True

                        if (not in_cooldown and rsi_15m < RSI_BUY_15M
                                and RSI_MIN_1H < rsi_1h < RSI_MAX_1H and ema_ok):
                            trade_amount = compute_trade_size(usdt_balance, total_equity)
                            if trade_amount >= MIN_TRADE_USDT:
                                result = place_order(symbol, "buy", amount_usdt=trade_amount)
                                if result.get("code") == "00000":
                                    quantity = trade_amount / price
                                    positions[symbol] = {
                                        "price": price, "quantity": quantity, "peak": price,
                                        "opened_at": datetime.now().isoformat(),
                                        "clientOid": result.get("clientOid", "")
                                    }
                                    save_state()
                                    msg = (f"🟢 BUY {symbol}\n💵 ${trade_amount:.2f} la ${price:.4f}\n"
                                           f"📊 RSI15m={rsi_15m}, RSI1H={rsi_1h}\n"
                                           f"{'🧪 SIMULARE' if DRY_RUN else '💰 REAL'}")
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

                        should_sell, reason = False, ""
                        if pnl_pct <= -STOP_LOSS:
                            should_sell, reason = True, f"🛑 Stop-loss {pnl_pct*100:.1f}%"
                        elif peak_pnl >= TRAILING_TRIGGER and drop_from_peak >= TRAILING_DISTANCE:
                            should_sell, reason = True, f"📉 Trailing stop (vârf +{peak_pnl*100:.1f}%, acum +{pnl_pct*100:.1f}%)"
                        elif rsi_15m > RSI_SELL and drop_from_peak >= RSI_SELL_MIN_DROP_FROM_PEAK:
                            should_sell, reason = True, f"📊 RSI={rsi_15m} > {RSI_SELL} + scădere {drop_from_peak*100:.1f}% de la vârf"

                        save_state()  # salvăm peak-ul actualizat chiar dacă nu vindem

                        if should_sell:
                            sell_qty = pos["quantity"] if DRY_RUN else get_coin_balance(coin)
                            sell_qty = floor_qty(symbol, sell_qty)
                            if sell_qty > 0:
                                result = place_order(symbol, "sell", quantity=sell_qty)
                                if result.get("code") == "00000":
                                    emoji = "✅" if pnl_pct > 0 else "❌"
                                    msg = (f"🔴 SELL {symbol}\n{reason}\n{emoji} PnL: {pnl_pct*100:+.1f}%\n"
                                           f"Preț: ${price:.4f}\n{'🧪 SIMULARE' if DRY_RUN else '💰 REAL'}")
                                    log(msg)
                                    send_telegram(msg)
                                    last_sell_time[symbol] = time.time()
                                    del positions[symbol]
                                    save_state()
                                else:
                                    log(f"❌ Eroare SELL: {result}")
                                    send_telegram(f"❌ Eroare SELL {symbol}: {result.get('msg', 'necunoscut')}")
                            else:
                                last_sell_time[symbol] = time.time()
                                del positions[symbol]
                                save_state()

                except Exception as e:
                    log(f"❌ Eroare la procesarea {symbol}: {e}")
                    continue

            log(f"⏳ Aștept {LOOP_INTERVAL} secunde...\n")
            time.sleep(LOOP_INTERVAL)

        except Exception as e:
            log(f"❌ Eroare în bucla principală: {e}")
            send_telegram(f"⚠️ Bot eroare neașteptată: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run_bot()
