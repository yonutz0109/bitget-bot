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
import csv
from datetime import datetime
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
TRADES_CSV = "trades.csv"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BGBUSDT", "UNIUSDT", "DOGEUSDT",
           "SOLUSDT", "ADAUSDT", "LINKUSDT"]

# --- Indicatori ---
RSI_PERIOD = 14
RSI_BUY_15M = 45
RSI_MIN_1H = 32
RSI_MAX_1H = 60
RSI_SELL = 65

EMA_TOLERANCE = 0.985
EMA_PERIOD_TREND = 50  # pe 1h
EMA_PERIOD_MACRO = 50  # pe 4h pentru filtru macro

# --- Risk Management (Dinamic cu ATR) ---
RISK_PER_TRADE = 0.02          # redus de la 5% la 2% - 5% e prea mult pt un bot
                                # automat fara supraveghere; cateva pierderi la
                                # rand (normale statistic) erodeaza rapid contul
MAX_ALLOCATION_PER_TRADE = 0.25
MIN_TRADE_USDT = 5

# NOU: limita globala de expunere - cu 3 pozitii simultane posibile la 25%
# fiecare, se putea ajunge teoretic la 75% din cont in 3 monede corelate.
# Acum limitam expunerea TOTALA (suma tuturor pozitiilor deschise), nu doar
# per-tranzactie.
MAX_TOTAL_EXPOSURE_PCT = 0.60   # maxim 60% din equity in pozitii deschise simultan

# NOU: limita de pierdere zilnica - daca pierderile realizate intr-o zi
# depasesc acest procent din equity, botul nu mai deschide pozitii noi pana
# a doua zi (pozitiile deschise existente continua sa fie gestionate normal
# - stop-loss/trailing tot functioneaza, doar nu se mai cumpara nimic nou).
MAX_DAILY_DRAWDOWN_PCT = 0.05   # 5% din equity

# NOU: comision estimat per parte (taker fee Bitget standard ~0.1%), folosit
# ca sa aratam PnL NET (dupa taxe) in mesaje, nu doar miscarea bruta de pret.
FEE_RATE_PER_SIDE = 0.001

# --- Stop Loss & Trailing (Optimizate) ---
TRAILING_TRIGGER = 0.025   # activează trailing după +2.5%
TRAILING_DISTANCE = 0.015  # la 1.5% sub vârf
BREAKEVEN_TRIGGER = 0.015  # activează stop protejat după +1.5%
BREAKEVEN_DISTANCE = 0.005 # stop protejat la -0.5% dupa activare
PARTIAL_PROFIT_TRIGGER = 0.025 # vinde 50% la +2.5%
RSI_SELL_MIN_DROP_FROM_PEAK = 0.003

# --- Time & Position ---
COOLDOWN_MINUTES = 45
MAX_CONCURRENT_POSITIONS = 3
MAX_HOLD_HOURS = 48

# --- Volatilitate ---
ATR_PERIOD = 14

# --- Volum & Spread ---
MIN_VOLUME_RATIO = 1.2
MAX_SPREAD_PCT = 0.003

# --- Correlation ---
CORRELATION_WINDOW = 50
MAX_CORRELATION = 0.85

# --- Market Regime ---
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25

# --- Filtru BTC (protectie pentru altcoin-uri) ---
# Nu cumparam niciun altcoin daca BTC e sub trendul lui pe 1h sau a scazut
# brusc in ultimele cateva ore - altcoin-urile urmeaza aproape mereu BTC
# in jos, indiferent ce arata indicatorii lor locali.
BTC_SYMBOL = "BTCUSDT"
BTC_DROP_THRESHOLD = 0.04       # BTC scazut >4% in ultimele BTC_DROP_LOOKBACK_H ore = stop
BTC_DROP_LOOKBACK_H = 4
BTC_EMA_TOLERANCE = 0.99        # BTC trebuie sa fie aproape de/peste EMA50 1h

# --- Fear & Greed Index (protectie suplimentara la frica extrema) ---
# Foloseste API-ul public gratuit alternative.me, fara autentificare.
# In frica extrema (index foarte mic), piata poate continua sa scada brusc
# indiferent de semnalele tehnice locale - devenim mai precauti la cumparari.
FEAR_GREED_ENABLED = True
FEAR_GREED_EXTREME_THRESHOLD = 15   # sub acest prag = "frica extrema", blocam buy-uri noi
FEAR_GREED_CACHE_MINUTES = 30       # indexul se schimba o data pe zi, nu are rost sa cerem des

REQUEST_TIMEOUT = 10
LOOP_INTERVAL = 120

if not DRY_RUN and not (API_KEY and SECRET_KEY and PASSPHRASE):
    print("EROARE: lipsesc credențialele Bitget și DRY_RUN=false. Opresc botul.")
    sys.exit(1)

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("bot")

def log_json(event_type, data):
    entry = {"timestamp": datetime.now().isoformat(), "event": event_type, "data": data}
    logger.info(json.dumps(entry))

# ---------------- HTTP SESSION ----------------
session = requests.Session()
retry_cfg = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry_cfg))

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # NOU: reincercam de 3 ori daca trimiterea esueaza (hiccup de retea, timeout,
    # rate-limit temporar de la Telegram) - inainte, un singur esec insemna ca
    # notificarea se pierdea definitiv, desi tranzactia pe Bitget reusea normal.
    for attempt in range(1, 4):
        try:
            r = session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return
            logger.error(f"Telegram HTTP {r.status_code} (incercarea {attempt}/3): {r.text[:200]}")
        except Exception as e:
            logger.error(f"Telegram error (incercarea {attempt}/3): {e}")
        if attempt < 3:
            time.sleep(2 * attempt)  # backoff: 2s, apoi 4s
    logger.error(f"❌ Mesaj Telegram PIERDUT definitiv dupa 3 incercari: {msg[:100]}")

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

def safe_request(method, url, headers=None, data=None, params=None):
    try:
        r = session.request(method, url, headers=headers, data=data, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            # NOU: incercam sa extragem mesajul REAL de eroare trimis de Bitget
            # in corpul raspunsului (ex: {"code":"40762","msg":"size too small"}),
            # nu doar codul HTTP - altfel place_order() arata generic "no response"
            # in loc de motivul concret pentru care ordinul a fost respins.
            try:
                body = r.json()
                bitget_msg = body.get("msg", "")
                bitget_code = body.get("code", "")
                logger.error(f"HTTP {r.status_code} la {url} — Bitget code={bitget_code}, msg={bitget_msg}")
                return body  # contine deja code/msg reale, place_order le foloseste direct
            except ValueError:
                logger.error(f"HTTP {r.status_code} la {url}: {r.text[:300]}")
                return None
        return r.json()
    except Exception as e:
        logger.error(f"Eroare rețea la {url}: {e}")
        return None

# ---------------- HEARTBEAT & STATE ----------------
positions = {}
last_sell_time = {}
price_history = {}
virtual_balance = None  # Pentru DRY_RUN
daily_realized_pnl = 0.0   # NOU: suma PnL realizat (net) azi
daily_pnl_date = None      # NOU: ziua curenta (string YYYY-MM-DD), pt reset

def update_heartbeat(status="ok", extra=None):
    try:
        hb = {"timestamp": datetime.now().isoformat(), "status": status, "positions_count": len(positions), "extra": extra or {}}
        with open(HEARTBEAT_FILE, "w") as f: json.dump(hb, f)
    except Exception as e: logger.error(f"Heartbeat error: {e}")

def load_state():
    global positions, last_sell_time, price_history, virtual_balance, daily_realized_pnl, daily_pnl_date
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: saved = json.load(f)
            positions = saved.get("positions", {})
            last_sell_time = saved.get("last_sell_time", {})
            virtual_balance = saved.get("virtual_balance", None)
            daily_realized_pnl = saved.get("daily_realized_pnl", 0.0)
            daily_pnl_date = saved.get("daily_pnl_date", None)
            ph_raw = saved.get("price_history", {})
            price_history = {k: deque(v, maxlen=CORRELATION_WINDOW*2) for k, v in ph_raw.items()}
            logger.info(f"Stare încărcată: {len(positions)} poziții")
        except Exception as e:
            logger.error(f"Nu am putut încărca starea: {e}")
            positions, last_sell_time, price_history = {}, {}, {}

def save_state():
    tmp = STATE_FILE + ".tmp"
    try:
        state = {
            "positions": positions,
            "last_sell_time": last_sell_time,
            "virtual_balance": virtual_balance,
            "daily_realized_pnl": daily_realized_pnl,
            "daily_pnl_date": daily_pnl_date,
            "price_history": {k: list(v) for k, v in price_history.items()}
        }
        with open(tmp, "w") as f: json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e: logger.error(f"Save state error: {e}")

def handle_shutdown(signum, frame):
    logger.info("Semnal de oprire primit, salvez starea...")
    save_state()
    sys.exit(0)

# ---------------- PRECIZIE & API ----------------
quantity_precision = {}

def load_symbol_precision():
    path = "/api/v2/spot/public/symbols"
    data = safe_request("GET", BASE_URL + path)
    if data and data.get("code") == "00000":
        for s in data.get("data", []):
            sym = s.get("symbol", "")
            if sym in SYMBOLS: quantity_precision[sym] = int(s.get("quantityPrecision", 4))
    else:
        for s in SYMBOLS: quantity_precision[s] = 4

def floor_qty(symbol, qty):
    decimals = quantity_precision.get(symbol, 4)
    factor = 10 ** decimals
    return math.floor(qty * factor) / factor

def get_spot_balance(coin="USDT"):
    if DRY_RUN and coin == "USDT":
        return virtual_balance if virtual_balance is not None else 0.0

    path = "/api/v2/spot/account/assets"
    full_path = path + f"?coin={coin}"
    headers = get_headers("GET", full_path)
    data = safe_request("GET", BASE_URL + full_path, headers=headers)
    if data and data.get("code") == "00000":
        for asset in data.get("data", []):
            if asset["coin"] == coin: return float(asset["available"])
    return 0.0

def get_candles(symbol, granularity="15min", limit=150):
    path = f"/api/v2/spot/market/candles?symbol={symbol}&granularity={granularity}&limit={limit}"
    data = safe_request("GET", BASE_URL + path)
    return data.get("data", []) if data and data.get("code") == "00000" else []

def get_orderbook(symbol, limit=5):
    path = f"/api/v2/spot/market/orderbook?symbol={symbol}&limit={limit}"
    data = safe_request("GET", BASE_URL + path)
    return data.get("data", {}) if data and data.get("code") == "00000" else None

def get_closes(candles): return [float(c[4]) for c in reversed(candles)]
def get_volumes(candles): return [float(c[5]) for c in reversed(candles)]
def get_highs(candles): return [float(c[2]) for c in reversed(candles)]
def get_lows(candles): return [float(c[3]) for c in reversed(candles)]

def get_current_price(symbol):
    path = f"/api/v2/spot/market/tickers?symbol={symbol}"
    data = safe_request("GET", BASE_URL + path)
    if data and data.get("code") == "00000":
        tickers = data.get("data", [])
        if tickers: return float(tickers[0]["lastPr"])
    return 0.0

def get_spread(symbol):
    ob = get_orderbook(symbol, limit=1)
    if not ob: return 1.0
    bids, asks = ob.get("bids", []), ob.get("asks", [])
    if not bids or not asks: return 1.0
    bid, ask = float(bids[0][0]), float(asks[0][0])
    mid = (bid + ask) / 2
    return (ask - bid) / mid if mid > 0 else 1.0

# ---------------- INDICATORI ----------------
def calculate_rsi_ema(closes, period=14):
    if len(closes) < period + 1: return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    k = 2 / (period + 1)
    for i in range(period, len(gains)):
        avg_gain = gains[i] * k + avg_gain * (1 - k)
        avg_loss = losses[i] * k + avg_loss * (1 - k)
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(closes, period=50):
    if len(closes) < period: return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for price in closes[period:]: ema = price * k + ema * (1 - k)
    return ema

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return None
    trs = []
    for i in range(1, len(closes)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i-1])
        tr3 = abs(lows[i] - closes[i-1])
        trs.append(max(tr1, tr2, tr3))
    atr = sum(trs[:period]) / period
    k = 2 / (period + 1)
    for tr in trs[period:]: atr = tr * k + atr * (1 - k)
    return atr

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2 + 1: return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        tr1, tr2, tr3 = highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])
        trs.append(max(tr1, tr2, tr3))
        up_move, down_move = highs[i] - highs[i-1], lows[i-1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)

    atr, plus_sm, minus_sm = sum(trs[:period]), sum(plus_dm[:period]), sum(minus_dm[:period])
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

    if len(dx_values) < period: return None
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]: adx = (adx * (period - 1) + dx) / period
    return round(adx, 2)

def calculate_vwap(candles):
    if not candles: return None
    total_pv, total_v = 0, 0
    for c in reversed(candles[-20:]):
        tp = (float(c[2]) + float(c[3]) + float(c[4])) / 3
        v = float(c[5])
        total_pv += tp * v
        total_v += v
    return total_pv / total_v if total_v > 0 else None

def calculate_correlation(prices_a, prices_b, window=50):
    if len(prices_a) < window + 1 or len(prices_b) < window + 1: return 0.0
    def returns(p):
        return [(p[i] - p[i-1])/p[i-1] for i in range(1, len(p)) if p[i-1] > 0]
    ra, rb = returns(prices_a[-window-1:]), returns(prices_b[-window-1:])
    n = min(len(ra), len(rb))
    if n < 20: return 0.0
    ra, rb = ra[-n:], rb[-n:]
    ma, mb = sum(ra)/n, sum(rb)/n
    num = sum((ra[i]-ma)*(rb[i]-mb) for i in range(n))
    da = math.sqrt(sum((x-ma)**2 for x in ra))
    db = math.sqrt(sum((x-mb)**2 for x in rb))
    if da == 0 or db == 0: return 0.0
    return num / (da * db)

# ---------------- TRADING & RISK ----------------
def place_order(symbol, side, amount_usdt=None, quantity=None):
    client_oid = str(uuid.uuid4())
    if DRY_RUN:
        logger.info(f"[DRY_RUN] Simulez {side} {symbol} ${amount_usdt or quantity}")
        return {"code": "00000", "dry_run": True, "clientOid": client_oid}

    path = "/api/v2/spot/trade/place-order"
    body = {"symbol": symbol, "force": "gtc", "clientOid": client_oid}

    if side == "buy":
        # NOU: BUG REPARAT — API-ul Bitget v2 NU are un parametru "funds".
        # Toate exemplele oficiale (place-order, batch-orders, websocket) folosesc
        # doar "size" — pentru orderType=market + side=buy, "size" reprezinta suma
        # in USDT de cheltuit (nu cantitatea de moneda). Cu "funds", Bitget nu
        # gasea niciun camp "size" in request si respingea ordinul cu eroarea
        # "Parameter size cannot be empty" (asta a cauzat esecurile repetate la UNI).
        body.update(side="buy", orderType="market", size=str(round(amount_usdt * 0.999, 2)))
    else:
        body.update(side="sell", orderType="market", size=str(floor_qty(symbol, quantity)))

    body_str = json.dumps(body)
    headers = get_headers("POST", path, body_str)
    result = safe_request("POST", BASE_URL + path, headers=headers, data=body_str)
    log_json("ORDER", {"symbol": symbol, "side": side, "client_oid": client_oid, "result": result})
    return result if result is not None else {"code": "error", "msg": "no response"}

def get_total_equity(usdt_bal):
    total = usdt_bal
    for sym, pos in positions.items():
        price = get_current_price(sym)
        if price > 0: total += pos["quantity"] * price
    return total

def compute_stop_pct(atr, price):
    if not atr or price <= 0: return 0.025
    return max(0.012, min(0.04, 1.5 * atr / price))

def compute_trade_size(usdt_balance, total_equity, stop_pct, price):
    risk_amount = total_equity * RISK_PER_TRADE
    size_from_risk = risk_amount / stop_pct
    max_allowed = usdt_balance * MAX_ALLOCATION_PER_TRADE * 0.999
    return min(size_from_risk, max_allowed, usdt_balance)

def check_correlation_filter(symbol):
    if not positions: return True
    for existing_sym in positions:
        if existing_sym == symbol or existing_sym not in price_history or symbol not in price_history: continue
        corr = calculate_correlation(list(price_history[symbol]), list(price_history[existing_sym]), CORRELATION_WINDOW)
        if abs(corr) > MAX_CORRELATION:
            logger.info(f"🚫 {symbol} corelat {corr:.2f} cu {existing_sym}, sar.")
            return False
    return True

def log_trade_closed(symbol, entry, exit_price, qty, hours, reason, regime):
    pnl_usd = (exit_price - entry) * qty
    pnl_pct = (exit_price - entry) / entry
    try:
        with open(TRADES_CSV, "a", newline="") as f:
            csv.writer(f).writerow([datetime.now().isoformat(), symbol, entry, exit_price, qty, round(pnl_usd,4), round(pnl_pct*100,2), round(hours,2), reason, regime])
    except Exception as e: logger.error(f"CSV log error: {e}")

# ---------------- DRAWDOWN ZILNIC & PnL NET ----------------
def check_and_reset_daily_pnl():
    """Reseteaza contorul de PnL zilnic daca a inceput o zi noua."""
    global daily_realized_pnl, daily_pnl_date
    today = datetime.now().strftime("%Y-%m-%d")
    if daily_pnl_date != today:
        if daily_pnl_date is not None:
            logger.info(f"📅 Zi noua - reset PnL zilnic (ieri: {daily_realized_pnl:+.2f} USDT)")
        daily_pnl_date = today
        daily_realized_pnl = 0.0

def daily_drawdown_exceeded(total_equity):
    """True daca pierderile realizate azi depasesc pragul - blocheaza cumparari noi."""
    if total_equity <= 0:
        return False
    limit = -total_equity * MAX_DAILY_DRAWDOWN_PCT
    return daily_realized_pnl <= limit

def net_pnl_pct(gross_pnl_pct):
    """PnL procentual NET, dupa scaderea comisioanelor estimate de dus-intors (buy+sell)."""
    return gross_pnl_pct - (2 * FEE_RATE_PER_SIDE)


# ---------------- FILTRU BTC ----------------
def get_btc_health():
    """
    Verifica o singura data pe ciclu daca BTC e intr-o stare sanatoasa.
    Returneaza (healthy: bool, motiv: str) - daca healthy=False, nu se
    cumpara niciun altcoin in ciclul asta, indiferent de semnalele locale.
    """
    candles_1h = get_candles(BTC_SYMBOL, "1h", 100)
    if not candles_1h:
        return True, "date BTC indisponibile, nu blochez"  # fail-open, nu vrem sa blocam tot din eroare de retea

    closes_1h = get_closes(candles_1h)
    ema50_1h = calculate_ema(closes_1h, EMA_PERIOD_TREND)
    price = get_current_price(BTC_SYMBOL)

    if price == 0 or ema50_1h is None:
        return True, "date BTC incomplete, nu blochez"

    ema_ok = price > ema50_1h * BTC_EMA_TOLERANCE

    # Scadere in ultimele BTC_DROP_LOOKBACK_H ore (candele de 1h)
    drop_ok = True
    if len(closes_1h) > BTC_DROP_LOOKBACK_H:
        price_then = closes_1h[-(BTC_DROP_LOOKBACK_H + 1)]
        if price_then > 0:
            change = (price - price_then) / price_then
            if change <= -BTC_DROP_THRESHOLD:
                drop_ok = False

    if not ema_ok and not drop_ok:
        return False, f"BTC sub EMA1h si scazut {change*100:.1f}% in {BTC_DROP_LOOKBACK_H}h"
    if not ema_ok:
        return False, "BTC sub EMA1h"
    if not drop_ok:
        return False, f"BTC scazut {change*100:.1f}% in {BTC_DROP_LOOKBACK_H}h"

    return True, "BTC sanatos"


# ---------------- FEAR & GREED INDEX ----------------
_fear_greed_cache = {"value": None, "label": "", "timestamp": 0}

def get_fear_greed():
    """
    Citeste Fear & Greed Index (0-100) de la alternative.me, API public gratuit.
    Cache de FEAR_GREED_CACHE_MINUTES ca sa nu cerem la fiecare ciclu (indexul
    se actualizeaza doar o data pe zi). Fail-open: daca cererea esueaza, nu
    blocam botul, doar ignoram acest filtru pentru ciclul curent.
    """
    if not FEAR_GREED_ENABLED:
        return None, "", True

    now = time.time()
    if _fear_greed_cache["value"] is not None and (now - _fear_greed_cache["timestamp"]) < FEAR_GREED_CACHE_MINUTES * 60:
        v = _fear_greed_cache["value"]
        return v, _fear_greed_cache["label"], v > FEAR_GREED_EXTREME_THRESHOLD

    try:
        r = session.get("https://api.alternative.me/fng/?limit=1", timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                value = int(data[0]["value"])
                label = data[0].get("value_classification", "")
                _fear_greed_cache.update(value=value, label=label, timestamp=now)
                return value, label, value > FEAR_GREED_EXTREME_THRESHOLD
    except Exception as e:
        logger.warning(f"Fear&Greed indisponibil, ignor filtrul: {e}")

    return None, "", True  # fail-open — nu blocam daca nu putem citi indexul


# ---------------- BOT LOOP ----------------
def run_bot():
    global virtual_balance, daily_realized_pnl, daily_pnl_date
    mode = "🧪 DRY RUN" if DRY_RUN else "💰 LIVE"
    load_symbol_precision()
    load_state()

    if DRY_RUN and virtual_balance is None:
        # NOU: get_spot_balance("USDT") in DRY_RUN citeste variabila virtual_balance
        # care e inca None aici — deci trebuie citita balanta REALA explicit,
        # ocolind functia (altfel primeai mereu fallback 1000$, niciodata soldul real).
        real_start = get_total_equity_bypass_dryrun()
        virtual_balance = real_start if real_start > 0 else 1000.0

    # NOU: reparat crash-ul — format string-ul vechi punea 'real' (text) intr-un
    # format numeric {:.2f}, ceea ce arunca ValueError la fiecare pornire LIVE.
    balance_display = f"${virtual_balance:.2f}" if DRY_RUN else "real (din cont)"

    start_msg = (f"🤖 Bot v16 (Fear&Greed + trailing prioritar) pornit! Mod: {mode}\n"
                 f"Balance start: {balance_display}\n"
                 f"Features: ATR-Stops, Partial Profit, Macro Trend, Corelații Returns, Filtru BTC,\n"
                 f"Fear&Greed filter, RSI nu mai taie trend-urile active,\n"
                 f"Risc 2%, Expunere max {MAX_TOTAL_EXPOSURE_PCT*100:.0f}%, Daily DD max {MAX_DAILY_DRAWDOWN_PCT*100:.0f}%\n"
                 f"Monitorizez: {', '.join(SYMBOLS)}")
    logger.info(start_msg)
    send_telegram(start_msg)

    while True:
        try:
            update_heartbeat("ok", {"loop": "start"})
            check_and_reset_daily_pnl()
            usdt_balance = get_spot_balance("USDT")
            total_equity = get_total_equity(usdt_balance)

            dd_blocked = daily_drawdown_exceeded(total_equity)
            logger.info(f"💰 USDT: ${usdt_balance:.2f} | Equity: ${total_equity:.2f} | Pozitii: {len(positions)}/{MAX_CONCURRENT_POSITIONS} | "
                       f"PnL azi: {daily_realized_pnl:+.2f} USDT{' | 🛑 DAILY DRAWDOWN ATINS' if dd_blocked else ''}")

            for sym in SYMBOLS:
                p = get_current_price(sym)
                if p > 0:
                    if sym not in price_history: price_history[sym] = deque(maxlen=CORRELATION_WINDOW*2)
                    price_history[sym].append(p)

            # NOU: verificam sanatatea BTC o singura data pe ciclu, nu per moneda.
            btc_healthy, btc_reason = get_btc_health()
            if not btc_healthy:
                logger.info(f"🚫 Filtru BTC activ: {btc_reason} — nu cumpar niciun altcoin in acest ciclu.")

            # NOU: verificam Fear & Greed Index o singura data pe ciclu.
            fg_value, fg_label, fg_ok = get_fear_greed()
            if fg_value is not None:
                logger.info(f"😨 Fear&Greed: {fg_value} ({fg_label}){' — FRICA EXTREMA, precautie la buy' if not fg_ok else ''}")

            for symbol in SYMBOLS:
                try:
                    coin = symbol.replace("USDT", "")
                    candles_15m = get_candles(symbol, "15min", 150)
                    candles_1h = get_candles(symbol, "1h", 100)
                    candles_4h = get_candles(symbol, "4h", 100)

                    if not candles_15m or not candles_1h or not candles_4h: continue

                    closes_15m, closes_1h, closes_4h = get_closes(candles_15m), get_closes(candles_1h), get_closes(candles_4h)
                    highs_1h, lows_1h, volumes_15m = get_highs(candles_1h), get_lows(candles_1h), get_volumes(candles_15m)

                    rsi_15m = calculate_rsi_ema(closes_15m, RSI_PERIOD)
                    rsi_1h = calculate_rsi_ema(closes_1h, RSI_PERIOD)
                    ema50_1h = calculate_ema(closes_1h, EMA_PERIOD_TREND)
                    ema50_4h = calculate_ema(closes_4h, EMA_PERIOD_MACRO)
                    vwap = calculate_vwap(candles_15m)
                    atr = calculate_atr(highs_1h, lows_1h, closes_1h, ATR_PERIOD)
                    adx = calculate_adx(highs_1h, lows_1h, closes_1h, ADX_PERIOD)
                    price = get_current_price(symbol)

                    if price == 0 or ema50_1h is None or ema50_4h is None: continue

                    macro_uptrend = price > ema50_4h * EMA_TOLERANCE
                    ema_ok = price > ema50_1h * EMA_TOLERANCE
                    regime = "TREND" if adx and adx > ADX_TREND_THRESHOLD else "RANGE"

                    avg_volume = sum(volumes_15m[-20:]) / 20 if len(volumes_15m) >= 20 else 0
                    current_volume = volumes_15m[-1] if volumes_15m else 0
                    volume_ok = current_volume > avg_volume * MIN_VOLUME_RATIO if avg_volume > 0 else False
                    spread = get_spread(symbol)
                    spread_ok = spread < MAX_SPREAD_PCT

                    logger.info(f"📊 {symbol} | ${price:.4f} | RSI:{rsi_15m}/{rsi_1h} | Macro:{'✅' if macro_uptrend else '❌'} | Regime:{regime}")

                    if symbol not in positions:
                        if len(positions) >= MAX_CONCURRENT_POSITIONS: continue

                        in_cooldown = symbol in last_sell_time and (time.time() - last_sell_time[symbol]) / 60 < COOLDOWN_MINUTES
                        rsi_ok = rsi_15m < RSI_BUY_15M and RSI_MIN_1H < rsi_1h < RSI_MAX_1H
                        if regime == "TREND" and adx and adx > 30:
                            rsi_ok = rsi_15m < RSI_BUY_15M + 5 and rsi_1h < RSI_MAX_1H + 5

                        vwap_ok = price < vwap * 1.01 if vwap else True
                        corr_ok = check_correlation_filter(symbol)

                        all_ok = (not dd_blocked and btc_healthy and fg_ok and macro_uptrend and ema_ok and not in_cooldown
                                  and rsi_ok and volume_ok and spread_ok and vwap_ok and corr_ok)

                        if not all_ok:
                            blocked = []
                            if dd_blocked: blocked.append("daily-drawdown")
                            if not btc_healthy: blocked.append(f"BTC({btc_reason})")
                            if not fg_ok: blocked.append(f"FearGreed({fg_value})")
                            if not macro_uptrend: blocked.append("macro4h")
                            if not ema_ok: blocked.append("EMA1h")
                            if in_cooldown: blocked.append("cooldown")
                            if not rsi_ok: blocked.append("RSI")
                            if not volume_ok: blocked.append("volum")
                            if not spread_ok: blocked.append("spread")
                            if not vwap_ok: blocked.append("VWAP")
                            if not corr_ok: blocked.append("corelatie")
                            if blocked:
                                logger.info(f"⏸️ {symbol}: nu cumpar — blocat de: {', '.join(blocked)}")

                        if all_ok:
                            stop_pct = compute_stop_pct(atr, price)
                            trade_amount = compute_trade_size(usdt_balance, total_equity, stop_pct, price)

                            # NOU: cap global de expunere - suma tuturor pozitiilor deschise
                            # + tranzactia noua nu poate depasi MAX_TOTAL_EXPOSURE_PCT din equity.
                            current_exposure = max(0.0, total_equity - usdt_balance)
                            max_exposure_allowed = total_equity * MAX_TOTAL_EXPOSURE_PCT
                            room_left = max(0.0, max_exposure_allowed - current_exposure)
                            if trade_amount > room_left:
                                trade_amount = room_left
                                logger.info(f"⚠️ {symbol}: sizing redus la ${trade_amount:.2f} din cauza limitei de expunere globala ({MAX_TOTAL_EXPOSURE_PCT*100:.0f}%)")

                            if trade_amount >= MIN_TRADE_USDT:
                                # NOU: masuram soldul monedei INAINTE de cumparare, ca sa
                                # calculam cantitatea reala prin diferenta, nu prin soldul
                                # total (care putea include praf ramas de la alte tranzactii).
                                balance_before = 0.0 if DRY_RUN else get_spot_balance(coin)

                                result = place_order(symbol, "buy", amount_usdt=trade_amount)
                                if result.get("code") == "00000":
                                    if DRY_RUN:
                                        real_qty = (trade_amount * 0.999) / price
                                        virtual_balance -= trade_amount
                                    else:
                                        time.sleep(1.5)  # timp scurt pt. ca ordinul sa se deconteze
                                        balance_after = get_spot_balance(coin)
                                        real_qty = balance_after - balance_before
                                        if real_qty <= 0:
                                            # fallback daca decontarea a intarziat
                                            real_qty = (trade_amount * 0.999) / price

                                    positions[symbol] = {
                                        "price": price, "quantity": real_qty, "peak": price,
                                        "opened_at": datetime.now().isoformat(),
                                        "breakeven_activated": False, "partial_sold": False,
                                        "stop_pct": stop_pct
                                    }
                                    save_state()
                                    msg = (f"🟢 BUY {symbol}\n💵 ${trade_amount:.2f} la ${price:.4f}\n"
                                           f"📊 RSI={rsi_15m}, ADX={adx}, Stop={stop_pct*100:.1f}%\n"
                                           f"{'🧪 SIM' if DRY_RUN else '💰 REAL'}")
                                    logger.info(msg); send_telegram(msg)
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
                        drop_from_peak = (pos["peak"] - price) / pos["peak"] if pos["peak"] > 0 else 0

                        if not pos.get("breakeven_activated") and pnl_pct >= BREAKEVEN_TRIGGER:
                            pos["breakeven_activated"] = True
                            logger.info(f"🔒 {symbol}: Stop protejat activat la +{pnl_pct*100:.1f}%")
                            send_telegram(f"🔒 {symbol}: Stop protejat la +{pnl_pct*100:.1f}%")

                        if not pos.get("partial_sold") and pnl_pct >= PARTIAL_PROFIT_TRIGGER:
                            half_qty = floor_qty(symbol, pos["quantity"] / 2)
                            if half_qty > 0:
                                r = place_order(symbol, "sell", quantity=half_qty)
                                if r.get("code") == "00000":
                                    pos["quantity"] -= half_qty
                                    pos["partial_sold"] = True
                                    pos["breakeven_activated"] = True
                                    if DRY_RUN: virtual_balance += half_qty * price * 0.999
                                    send_telegram(f"💸 {symbol}: Vândut 50% la +{pnl_pct*100:.1f}%")
                                    save_state()

                        should_sell, reason = False, ""
                        stop_pct = pos.get("stop_pct", 0.025)

                        if pnl_pct <= -stop_pct:
                            should_sell, reason = True, f"🛑 SL {pnl_pct*100:.1f}% (Stop: {stop_pct*100:.1f}%)"
                        elif pos.get("breakeven_activated") and pnl_pct <= -BREAKEVEN_DISTANCE:
                            should_sell, reason = True, f"🔒 Stop protejat (PnL: {pnl_pct*100:.1f}%)"
                        elif peak_pnl >= TRAILING_TRIGGER and drop_from_peak >= TRAILING_DISTANCE:
                            should_sell, reason = True, f"📉 Trailing (Vârf: +{peak_pnl*100:.1f}%, Acum: +{pnl_pct*100:.1f}%)"
                        # NOU: RSI poate forta vanzarea DOAR daca trailing stop-ul inca
                        # nu s-a "armat" (peak_pnl < TRAILING_TRIGGER) - adica pozitia
                        # inca nu a demonstrat un trend real de crestere. Odata ce
                        # trailing-ul e activ, lasam DOAR trailing/stop-loss/breakeven
                        # sa decida iesirea - inainte, RSI>65 vindea la doar 0.3%
                        # scadere de la varf chiar si intr-un trend bun, taind
                        # castiguri mari si generand tranzactii/comisioane inutile.
                        elif (peak_pnl < TRAILING_TRIGGER and rsi_15m > RSI_SELL
                              and drop_from_peak >= RSI_SELL_MIN_DROP_FROM_PEAK):
                            should_sell, reason = True, f"📊 RSI={rsi_15m} > {RSI_SELL}"
                        else:
                            opened_dt = datetime.fromisoformat(pos["opened_at"])
                            hours_held = (datetime.now() - opened_dt).total_seconds() / 3600
                            if hours_held >= MAX_HOLD_HOURS:
                                should_sell, reason = True, f"⏰ Time exit ({hours_held:.1f}h)"

                        if should_sell:
                            sell_qty = floor_qty(symbol, pos["quantity"])
                            if sell_qty > 0:
                                result = place_order(symbol, "sell", quantity=sell_qty)
                                if result.get("code") == "00000":
                                    if DRY_RUN: virtual_balance += sell_qty * price * 0.999
                                    emoji = "✅" if pnl_pct > 0 else "❌"
                                    opened_dt = datetime.fromisoformat(pos["opened_at"])
                                    hours_held = (datetime.now() - opened_dt).total_seconds() / 3600

                                    # NOU: PnL NET (dupa scaderea comisioanelor estimate de
                                    # dus-intors), ca sa nu arate un profit brut inselator.
                                    net_pct = net_pnl_pct(pnl_pct)
                                    net_usd = (price - entry) * sell_qty - (entry * sell_qty + price * sell_qty) * FEE_RATE_PER_SIDE

                                    # NOU: actualizam contorul de pierdere zilnica cu suma NETA
                                    daily_realized_pnl += net_usd

                                    msg = (f"🔴 SELL {symbol}\n{reason}\n{emoji} PnL brut: {pnl_pct*100:+.1f}% | "
                                           f"net (cu taxe): {net_pct*100:+.1f}% (${net_usd:+.2f})\n"
                                           f"📅 PnL azi: {daily_realized_pnl:+.2f} USDT\n"
                                           f"{'🧪 SIM' if DRY_RUN else '💰 REAL'}")
                                    logger.info(msg); send_telegram(msg)
                                    log_trade_closed(symbol, entry, price, sell_qty, hours_held, reason, regime)
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
                    logger.error(f"❌ Eroare {symbol}: {e}")

            save_state()
            update_heartbeat("ok", {"loop": "end"})
            logger.info(f"⏳ Aștept {LOOP_INTERVAL} secunde...\n")
            time.sleep(LOOP_INTERVAL)

        except Exception as e:
            logger.error(f"❌ Eroare bucla principală: {e}")
            update_heartbeat("error", {"error": str(e)})
            send_telegram(f"⚠️ Bot eroare: {e}")
            time.sleep(60)


def get_total_equity_bypass_dryrun():
    """Citeste soldul USDT REAL de pe Bitget, ocolind virtual_balance
    (folosit doar o data, la pornire, ca sa initializam simularea cu bani reali)."""
    path = "/api/v2/spot/account/assets"
    full_path = path + "?coin=USDT"
    headers = get_headers("GET", full_path)
    data = safe_request("GET", BASE_URL + full_path, headers=headers)
    if data and data.get("code") == "00000":
        for asset in data.get("data", []):
            if asset["coin"] == "USDT": return float(asset["available"])
    return 0.0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    run_bot()
