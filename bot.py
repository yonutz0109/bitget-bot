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
import logging
from datetime import datetime, timedelta
from collections import deque
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
LOG_FILE = "bot.log"
HEARTBEAT_FILE = "heartbeat.json"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BGBUSDT", "UNIUSDT", "DOGEUSDT"]

# --- Indicatori ---
RSI_PERIOD = 14
RSI_BUY_15M = 45
RSI_MIN_1H = 32
RSI_MAX_1H = 60
RSI_SELL = 65

EMA_TOLERANCE = 0.985
EMA_PERIOD_TREND = 50  # pe 1h

# --- Risk Management ---
STOP_LOSS = 0.025
TRAILING_TRIGGER = 0.015
TRAILING_DISTANCE = 0.012
BREAKEVEN_TRIGGER = 0.010  # activeaza stop protejat dupa +1%
BREAKEVEN_DISTANCE = 0.005  # stop protejat la -0.5% dupa activare

RSI_SELL_MIN_DROP_FROM_PEAK = 0.003

# --- Time & Position ---
COOLDOWN_MINUTES = 45
MAX_CONCURRENT_POSITIONS = 3
MAX_HOLD_HOURS = 48
RISK_PER_TRADE = 0.05
MAX_ALLOCATION_PER_TRADE = 0.25
MIN_TRADE_USDT = 5

# --- Volatilitate ---
ATR_PERIOD = 14
ATR_MULTIPLIER_MIN = 0.5
ATR_MULTIPLIER_MAX = 2.0

# --- Volum & Spread ---
MIN_VOLUME_RATIO = 1.2
MAX_SPREAD_PCT = 0.003

# --- Correlation ---
CORRELATION_WINDOW = 50
MAX_CORRELATION = 0.85

# --- Market Regime ---
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25

REQUEST_TIMEOUT = 10
LOOP_INTERVAL = 120

if not DRY_RUN and not (API_KEY and SECRET_KEY and PASSPHRASE):
    print("EROARE: lipsesc credențialele Bitget și DRY_RUN=false. Opresc botul.")
    sys.exit(1)

# ---------------- LOGGING STRUCTURAT ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("bot")

def log_json(event_type, data):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": event_type,
        "data": data
    }
    logger.info(json.dumps(entry))

# ---------------- HTTP SESSION CU RETRY ----------------
session = requests.Session()
retry_cfg = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry_cfg))


def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        logger.error(f"Telegram error: {e}")


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
    try:
        r = session.request(method, url, headers=headers, data=data, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.error(f"HTTP {r.status_code} la {url}: {r.text[:200]}")
            return None
        return r.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Eroare rețea la {url}: {e}")
        return None
    except ValueError as e:
        logger.error(f"Răspuns invalid (non-JSON) de la {url}: {e}")
        return None


# ---------------- HEARTBEAT ----------------
def update_heartbeat(status="ok", extra=None):
    try:
        hb = {
            "timestamp": datetime.now().isoformat(),
            "status": status,
            "positions_count": len(positions),
            "extra": extra or {}
        }
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump(hb, f)
    except Exception as e:
        logger.error(f"Heartbeat error: {e}")


# ---------------- PERSISTENȚĂ STARE ----------------
positions = {}
last_sell_time = {}
price_history = {}


def load_state():
    global positions, last_sell_time, price_history
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
            positions = saved.get("positions", {})
            last_sell_time = saved.get("last_sell_time", {})
            price_history = saved.get("price_history", {})
            logger.info(f"Stare încărcată: {len(positions)} poziții")
        except Exception as e:
            logger.error(f"Nu am putut încărca starea: {e}")
            positions, last_sell_time, price_history = {}, {}, {}
    else:
        positions, last_sell_time, price_history = {}, {}, {}


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "positions": positions,
                "last_sell_time": last_sell_time,
                "price_history": price_history
            }, f, indent=2)
    except Exception as e:
        logger.error(f"Nu am putut salva starea: {e}")


def handle_shutdown(signum, frame):
    logger.info("Semnal de oprire primit, salvez starea...")
    save_state()
    sys.exit(0)


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# ---------------- PRECIZIE ----------------
quantity_precision = {}


def load_symbol_precision():
    path = "/api/v2/spot/public/symbols"
    data = safe_request("GET", BASE_URL + path)
    if data and data.get("code") == "00000":
        for s in data.get("data", []):
            sym = s.get("symbol", "")
            if sym in SYMBOLS:
                quantity_precision[sym] = int(s.get("quantityPrecision", 4))
        logger.info(f"Precizie cantitate: {quantity_precision}")
    else:
        logger.warning("Nu am putut încărca precizia, folosesc default 4 zecimale.")


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


def get_orderbook(symbol, limit=5):
    path = f"/api/v2/spot/market/orderbook?symbol={symbol}&limit={limit}"
    data = safe_request("GET", BASE_URL + path)
    if data and data.get("code") == "00000":
        return data.get("data", {})
    return None


def get_closes(candles):
    return [float(c[4]) for c in reversed(candles)]


def get_volumes(candles):
    return [float(c[5]) for c in reversed(candles)]


def get_highs(candles):
    return [float(c[2]) for c in reversed(candles)]


def get_lows(candles):
    return [float(c[3]) for c in reversed(candles)]


# ---------------- INDICATORI AVANSAȚI ----------------
def calculate_rsi_ema(closes, period=14):
    if len(closes) < period + 1:
        return 50.0

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    k = 2 / (period + 1)

    for i in range(period, len(gains)):
        avg_gain = gains[i] * k + avg_gain * (1 - k)
        avg_loss = losses[i] * k + avg_loss * (1 - k)

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calculate_ema(closes, period=50):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None

    trs = []
    for i in range(1, len(closes)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i-1])
        tr3 = abs(lows[i] - closes[i-1])
        trs.append(max(tr1, tr2, tr3))

    atr = sum(trs[:period]) / period
    k = 2 / (period + 1)
    for tr in trs[period:]:
        atr = tr * k + atr * (1 - k)

    return atr


def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2 + 1:
        return None

    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i-1])
        tr3 = abs(lows[i] - closes[i-1])
        trs.append(max(tr1, tr2, tr3))

        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)

    atr = sum(trs[:period])
    plus_sm = sum(plus_dm[:period])
    minus_sm = sum(minus_dm[:period])

    dx_values = []
    for i in range(period, len(trs)):
        atr = atr - (atr / period) + trs[i]
        plus_sm = plus_sm - (plus_sm / period) + plus_dm[i]
        minus_sm = minus_sm - (minus_sm / period) + minus_dm[i]

        plus_di = 100 * (plus_sm / atr) if atr > 0 else 0
        minus_di = 100 * (minus_sm / atr) if atr > 0 else 0
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period

    return round(adx, 2)


def calculate_vwap(candles):
    if not candles:
        return None

    total_pv = 0
    total_v = 0
    for c in reversed(candles[-20:]):
        typical_price = (float(c[2]) + float(c[3]) + float(c[4])) / 3
        volume = float(c[5])
        total_pv += typical_price * volume
        total_v += volume

    return total_pv / total_v if total_v > 0 else None


def calculate_correlation(prices_a, prices_b, window=50):
    if len(prices_a) < window or len(prices_b) < window:
        return 0.0

    a = prices_a[-window:]
    b = prices_b[-window:]

    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)

    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(len(a)))
    den_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    den_b = math.sqrt(sum((x - mean_b) ** 2 for x in b))

    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


def get_current_price(symbol):
    path = f"/api/v2/spot/market/tickers?symbol={symbol}"
    data = safe_request("GET", BASE_URL + path)
    if data and data.get("code") == "00000":
        tickers = data.get("data", [])
        if tickers:
            return float(tickers[0]["lastPr"])
    return 0.0


def get_spread(symbol):
    ob = get_orderbook(symbol, limit=1)
    if not ob:
        return 1.0

    bids = ob.get("bids", [])
    asks = ob.get("asks", [])
    if not bids or not asks:
        return 1.0

    bid = float(bids[0][0])
    ask = float(asks[0][0])
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid
    return spread


def place_order(symbol, side, amount_usdt=None, quantity=None):
    client_oid = str(uuid.uuid4())
    if DRY_RUN:
        logger.info(f"[DRY_RUN] Simulez {side} {symbol} (clientOid={client_oid})")
        return {"code": "00000", "dry_run": True, "clientOid": client_oid}

    path = "/api/v2/spot/trade/place-order"
    if side == "buy":
        body = {
            "symbol": symbol, "side": "buy", "orderType": "market", "force": "gtc",
            "size": str(round(amount_usdt, 2)), "clientOid": client_oid
        }
    else:
        qty = floor_qty(symbol, quantity)
        body = {
            "symbol": symbol, "side": "sell", "orderType": "market", "force": "gtc",
            "size": str(qty), "clientOid": client_oid
        }

    body_str = json.dumps(body)
    headers = get_headers("POST", path, body_str)
    result = safe_request("POST", BASE_URL + path, headers=headers, data=body_str)

    log_json("ORDER", {
        "symbol": symbol,
        "side": side,
        "client_oid": client_oid,
        "result": result
    })

    return result if result is not None else {"code": "error", "msg": "no response"}


def get_coin_balance(coin):
    return get_spot_balance(coin)


# ---------------- RISC / SIZING AVANSAT ----------------
def get_total_equity():
    usdt = get_spot_balance("USDT")
    total = usdt

    for sym, pos in positions.items():
        price = get_current_price(sym)
        if price > 0:
            total += pos["quantity"] * price

    return total


def compute_trade_size(usdt_balance, total_equity, atr, current_price):
    risk_amount = total_equity * RISK_PER_TRADE

    atr_pct = atr / current_price if current_price > 0 and atr else STOP_LOSS

    volatility_factor = STOP_LOSS / max(atr_pct, 0.005)
    volatility_factor = max(ATR_MULTIPLIER_MIN, min(ATR_MULTIPLIER_MAX, volatility_factor))

    size_from_risk = (risk_amount / STOP_LOSS) * volatility_factor
    max_allowed = usdt_balance * MAX_ALLOCATION_PER_TRADE

    return min(size_from_risk, max_allowed, usdt_balance)


def check_correlation_filter(symbol):
    if not positions:
        return True

    if symbol not in price_history:
        price_history[symbol] = []

    current_price = get_current_price(symbol)
    if current_price > 0:
        price_history[symbol].append(current_price)
        if len(price_history[symbol]) > CORRELATION_WINDOW * 2:
            price_history[symbol] = price_history[symbol][-CORRELATION_WINDOW * 2:]

    for existing_sym in positions:
        if existing_sym not in price_history:
            continue

        corr = calculate_correlation(price_history[symbol], price_history[existing_sym], CORRELATION_WINDOW)
        if abs(corr) > MAX_CORRELATION:
            logger.info(f"🚫 {symbol} corelat {corr:.2f} cu {existing_sym}, sar.")
            return False

    return True


# ---------------- BOT LOOP ----------------
def run_bot():
    mode = "🧪 DRY RUN" if DRY_RUN else "💰 LIVE"
    load_symbol_precision()
    load_state()

    start_msg = (f"🤖 Bot v9 (Pro) pornit! Mod: {mode}\n"
                 f"Features: RSI-EMA, ADX (fix), VWAP, ATR-sizing, Correl-filter, Breakeven, Time-exit\n"
                 f"Monitorizez: {', '.join(SYMBOLS)}")
    logger.info(start_msg)
    send_telegram(start_msg)

    while True:
        try:
            update_heartbeat("ok", {"loop": "start"})

            usdt_balance = get_spot_balance("USDT")
            total_equity = get_total_equity()

            logger.info(f"💰 USDT: ${usdt_balance:.2f} | Equity: ${total_equity:.2f} | "
                       f"Poziții: {len(positions)}/{MAX_CONCURRENT_POSITIONS}")

            for symbol in SYMBOLS:
                try:
                    coin = symbol.replace("USDT", "")

                    candles_15m = get_candles(symbol, "15min", 150)
                    candles_1h = get_candles(symbol, "1h", 100)

                    if not candles_15m or not candles_1h:
                        logger.warning(f"⚠️ {symbol}: date lipsă, sar.")
                        continue

                    closes_15m = get_closes(candles_15m)
                    closes_1h = get_closes(candles_1h)
                    highs_1h = get_highs(candles_1h)
                    lows_1h = get_lows(candles_1h)
                    volumes_15m = get_volumes(candles_15m)

                    rsi_15m = calculate_rsi_ema(closes_15m, RSI_PERIOD)
                    rsi_1h = calculate_rsi_ema(closes_1h, RSI_PERIOD)
                    ema50_1h = calculate_ema(closes_1h, EMA_PERIOD_TREND)
                    vwap = calculate_vwap(candles_15m)
                    atr = calculate_atr(highs_1h, lows_1h, closes_1h, ATR_PERIOD)
                    adx = calculate_adx(highs_1h, lows_1h, closes_1h, ADX_PERIOD)

                    price = get_current_price(symbol)

                    if price == 0 or ema50_1h is None:
                        logger.warning(f"⚠️ {symbol}: preț sau EMA invalide, sar.")
                        continue

                    ema_ok = price > ema50_1h * EMA_TOLERANCE
                    trend_str = "✅" if ema_ok else "❌"

                    regime = "TREND" if adx and adx > ADX_TREND_THRESHOLD else "RANGE"

                    avg_volume = sum(volumes_15m[-20:]) / 20 if len(volumes_15m) >= 20 else 0
                    current_volume = volumes_15m[-1] if volumes_15m else 0
                    volume_ok = current_volume > avg_volume * MIN_VOLUME_RATIO if avg_volume > 0 else False

                    spread = get_spread(symbol)
                    spread_ok = spread < MAX_SPREAD_PCT

                    logger.info(f"📊 {symbol} | ${price:.4f} | RSI15m:{rsi_15m} RSI1H:{rsi_1h} | "
                               f"EMA1H:{trend_str} | ADX:{adx} | Regime:{regime} | "
                               f"Vol:{volume_ok} | Spread:{spread*100:.2f}%")

                    if symbol not in positions:
                        if len(positions) >= MAX_CONCURRENT_POSITIONS:
                            continue

                        in_cooldown = False
                        if symbol in last_sell_time:
                            minutes_since = (time.time() - last_sell_time[symbol]) / 60
                            if minutes_since < COOLDOWN_MINUTES:
                                in_cooldown = True

                        rsi_ok = rsi_15m < RSI_BUY_15M and RSI_MIN_1H < rsi_1h < RSI_MAX_1H
                        vwap_ok = price < vwap * 1.01 if vwap else True

                        if regime == "TREND" and adx and adx > 30:
                            rsi_ok = rsi_15m < RSI_BUY_15M + 5 and rsi_1h < RSI_MAX_1H + 5

                        corr_ok = check_correlation_filter(symbol)

                        all_ok = (not in_cooldown and rsi_ok and ema_ok and volume_ok
                                  and spread_ok and vwap_ok and corr_ok)

                        if not all_ok:
                            blocked_by = []
                            if in_cooldown: blocked_by.append("cooldown")
                            if not rsi_ok: blocked_by.append("RSI")
                            if not ema_ok: blocked_by.append("EMA")
                            if not volume_ok: blocked_by.append("volum")
                            if not spread_ok: blocked_by.append("spread")
                            if not vwap_ok: blocked_by.append("VWAP")
                            if not corr_ok: blocked_by.append("corelatie")
                            if blocked_by:
                                logger.info(f"⏸️ {symbol}: nu cumpar — blocat de: {', '.join(blocked_by)}")

                        if all_ok:
                            trade_amount = compute_trade_size(usdt_balance, total_equity, atr, price)

                            if trade_amount >= MIN_TRADE_USDT:
                                result = place_order(symbol, "buy", amount_usdt=trade_amount)

                                if result.get("code") == "00000":
                                    quantity = trade_amount / price
                                    positions[symbol] = {
                                        "price": price,
                                        "quantity": quantity,
                                        "peak": price,
                                        "opened_at": datetime.now().isoformat(),
                                        "clientOid": result.get("clientOid", ""),
                                        "breakeven_activated": False,
                                        "partial_sold": False
                                    }
                                    save_state()

                                    msg = (f"🟢 BUY {symbol}\n"
                                          f"💵 ${trade_amount:.2f} la ${price:.4f}\n"
                                          f"📊 RSI15m={rsi_15m}, RSI1H={rsi_1h}, ADX={adx}\n"
                                          f"📈 Regime: {regime}, Vol: {'✅' if volume_ok else '❌'}\n"
                                          f"{'🧪 SIMULARE' if DRY_RUN else '💰 REAL'}")
                                    logger.info(msg)
                                    send_telegram(msg)
                                    usdt_balance -= trade_amount
                                else:
                                    logger.error(f"❌ Eroare BUY: {result}")
                                    send_telegram(f"❌ Eroare BUY {symbol}: {result.get('msg', 'necunoscut')}")

                    else:
                        pos = positions[symbol]
                        entry = pos["price"]
                        pos["peak"] = max(pos["peak"], price)

                        pnl_pct = (price - entry) / entry
                        peak_pnl = (pos["peak"] - entry) / entry
                        drop_from_peak = (pos["peak"] - price) / pos["peak"]

                        if not pos.get("breakeven_activated") and pnl_pct >= BREAKEVEN_TRIGGER:
                            pos["breakeven_activated"] = True
                            logger.info(f"🔒 {symbol}: Stop protejat activat la +{pnl_pct*100:.1f}%")
                            send_telegram(f"🔒 {symbol}: Stop protejat la +{pnl_pct*100:.1f}% (permite -{BREAKEVEN_DISTANCE*100:.1f}% de acum)")

                        opened_dt = datetime.fromisoformat(pos["opened_at"])
                        hours_held = (datetime.now() - opened_dt).total_seconds() / 3600
                        time_exit = hours_held >= MAX_HOLD_HOURS

                        should_sell, reason = False, ""

                        if pnl_pct <= -STOP_LOSS:
                            should_sell, reason = True, f"🛑 Stop-loss {pnl_pct*100:.1f}%"

                        elif pos.get("breakeven_activated") and pnl_pct <= -BREAKEVEN_DISTANCE:
                            should_sell, reason = True, f"🔒 Stop protejat (-{BREAKEVEN_DISTANCE*100:.1f}%)"

                        elif peak_pnl >= TRAILING_TRIGGER and drop_from_peak >= TRAILING_DISTANCE:
                            should_sell, reason = True, (
                                f"📉 Trailing stop (vârf +{peak_pnl*100:.1f}%, acum +{pnl_pct*100:.1f}%)"
                            )

                        elif rsi_15m > RSI_SELL and drop_from_peak >= RSI_SELL_MIN_DROP_FROM_PEAK:
                            should_sell, reason = True, (
                                f"📊 RSI={rsi_15m} > {RSI_SELL} + scădere {drop_from_peak*100:.1f}%"
                            )

                        elif time_exit:
                            should_sell, reason = True, f"⏰ Time exit ({hours_held:.1f}h held, PnL {pnl_pct*100:+.1f}%)"

                        save_state()

                        if should_sell:
                            sell_qty = pos["quantity"] if DRY_RUN else get_coin_balance(coin)
                            sell_qty = floor_qty(symbol, sell_qty)

                            if sell_qty > 0:
                                result = place_order(symbol, "sell", quantity=sell_qty)

                                if result.get("code") == "00000":
                                    emoji = "✅" if pnl_pct > 0 else "❌"
                                    msg = (f"🔴 SELL {symbol}\n"
                                          f"{reason}\n"
                                          f"{emoji} PnL: {pnl_pct*100:+.1f}%\n"
                                          f"⏱️ Ținut: {hours_held:.1f}h\n"
                                          f"Preț: ${price:.4f}\n"
                                          f"{'🧪 SIMULARE' if DRY_RUN else '💰 REAL'}")
                                    logger.info(msg)
                                    send_telegram(msg)

                                    log_json("SELL", {
                                        "symbol": symbol,
                                        "pnl_pct": pnl_pct,
                                        "reason": reason,
                                        "hours_held": hours_held,
                                        "regime": regime
                                    })

                                    last_sell_time[symbol] = time.time()
                                    del positions[symbol]
                                    save_state()
                                else:
                                    logger.error(f"❌ Eroare SELL: {result}")
                                    send_telegram(f"❌ Eroare SELL {symbol}: {result.get('msg', 'necunoscut')}")
                            else:
                                last_sell_time[symbol] = time.time()
                                del positions[symbol]
                                save_state()

                except Exception as e:
                    logger.error(f"❌ Eroare la procesarea {symbol}: {e}")
                    continue

            update_heartbeat("ok", {"loop": "end"})
            logger.info(f"⏳ Aștept {LOOP_INTERVAL} secunde...\n")
            time.sleep(LOOP_INTERVAL)

        except Exception as e:
            logger.error(f"❌ Eroare în bucla principală: {e}")
            update_heartbeat("error", {"error": str(e)})
            send_telegram(f"⚠️ Bot eroare: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run_bot()
