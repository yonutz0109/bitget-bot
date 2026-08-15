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

# v22: fisierele de stare se scriu intr-un director configurabil prin variabila
# de mediu DATA_DIR. Pe Northflank atasam un volum persistent montat la /data
# si setam DATA_DIR=/data - altfel, la fiecare redeploy filesystem-ul se
# reseteaza, bot_state.json dispare, iar pozitiile deschise raman "orfane":
# botul uita de ele (fara stop-loss, fara trailing) si poate chiar recumpara
# aceeasi moneda crezand ca nu are pozitie. Exact asta s-a intamplat cu UNI.
# Daca DATA_DIR nu e setat, se foloseste directorul curent (comportament vechi).
DATA_DIR = os.environ.get("DATA_DIR", ".")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception as _e:
    print(f"ATENTIE: nu pot crea {DATA_DIR} ({_e}), folosesc directorul curent.")
    DATA_DIR = "."

STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.json")
TRADES_CSV = os.path.join(DATA_DIR, "trades.csv")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BGBUSDT", "UNIUSDT", "DOGEUSDT",
           "SOLUSDT", "ADAUSDT", "LINKUSDT"]

# --- Indicatori ---
RSI_PERIOD = 14
RSI_BUY_15M = 45
RSI_MIN_1H = 32
RSI_MAX_1H = 60
RSI_SELL = 70

# --- Strategie Momentum ---
# v21: fereastra RSI largita. Inainte ceream rsi_15m intre 52 si 62 - la un
# breakout REAL, RSI-ul pe 15m sare frecvent peste 62 in cateva minute, deci
# prindeam doar breakout-uri firave, exact opusul a ce vrem de la momentum.
# Acum: 50-72 pe 15m (sub RSI_SELL=70 nu mai e o cerinta - un breakout puternic
# POATE fi supracumparat pe termen scurt), iar pe 1h largim la 35-68.
MOMENTUM_ENABLED = True
RSI_MOMENTUM_MIN = 50
RSI_MOMENTUM_MAX = 72
RSI_MOMENTUM_1H_MIN = 35
RSI_MOMENTUM_1H_MAX = 68
MOMENTUM_BREAKOUT_LOOKBACK = 10
MOMENTUM_BREAKOUT_MARGIN = 0.001

EMA_TOLERANCE = 0.985
EMA_PERIOD_TREND = 50  # pe 1h
EMA_PERIOD_MACRO = 30  # pe 4h

# --- Risk Management ---
RISK_PER_TRADE = 0.02
MAX_ALLOCATION_PER_TRADE = 0.35
MIN_TRADE_USDT = 5

# v21: in regim RANGE nu mai blocam complet cumpararea (vezi REQUIRE_TREND_REGIME),
# dar reducem dimensiunea pozitiei - datele din trades.csv au aratat rata de
# pierdere mai mare in RANGE, deci intram, dar cu risc injumatatit.
RANGE_SIZE_MULTIPLIER = 0.5

MAX_TOTAL_EXPOSURE_PCT = 0.75
MAX_DAILY_DRAWDOWN_PCT = 0.05
FEE_RATE_PER_SIDE = 0.001

# --- Stop Loss & Trailing ---
TRAILING_TRIGGER = 0.025
TRAILING_DISTANCE = 0.015
BREAKEVEN_TRIGGER = 0.015   # activeaza stopul protejat dupa +1.5%

# v25: "stop protejat" chiar protejeaza acum.
# Inainte: BREAKEVEN_DISTANCE = 0.005, iar conditia era `pnl_pct <= -BREAKEVEN_DISTANCE`,
# adica stopul se aseza la -0.5% SUB pretul de intrare. Rezultatul: o pozitie care
# ti-a aratat deja +1.5% profit putea fi inchisa in PIERDERE. Exact asta s-a
# intamplat cu ADA pe 07.08: varf +1.5% la 09:16, iesire -0.6% brut / -0.8% net.
# Un stop numit "breakeven" care iese pe minus e o contradictie in termeni.
# Acum: nivel POZITIV, exprimat direct ca pragul de PnL brut la care iesim.
# 0.2% brut acopera exact comisioanele dus-intors (2 x 0.1%), deci iesirea e
# aproximativ pe ZERO net, in loc de -0.8%.
# ATENTIE: semnul e invers fata de vechea constanta - valoare POZITIVA inseamna
# DEASUPRA pretului de intrare. De-asta am si redenumit-o, ca sa nu se confunde.
BREAKEVEN_STOP_LEVEL = 0.002
PARTIAL_PROFIT_TRIGGER = 0.025
RSI_SELL_MIN_DROP_FROM_PEAK = 0.003

# v23: BUG DE LOGICA REPARAT. Regula "RSI > 70 -> vinde" nu verifica deloc daca
# pozitia e pe profit. Ce se intampla in practica (confirmat de trades.csv):
# botul cumpara cu RSI jos (36-44), pretul urca putin - destul cat RSI sa sara
# peste 70, dar NU destul cat sa acopere pretul de intrare + comisioane. Apoi la
# o scadere de doar 0.3% de la varf, regula RSI inchide pozitia... in pierdere.
#   UNI:  intrare 4.059  -> iesire 4.031  = -0.69%  (motiv: RSI 72.41 > 70)
#   ADA:  intrare 0.189  -> iesire 0.1886 = -0.21%  (motiv: RSI 70.64 > 70)
# Ambele inchise de RSI, ambele pe minus. Nu e ghinion, e o regula care vinde
# "pentru ca e supracumparat" chiar cand TU esti sub pretul de intrare - moment
# in care nu exista niciun profit de protejat.
# Fix: RSI poate inchide pozitia DOAR daca PnL-ul NET (dupa comisioane dus-intors)
# e pozitiv. Sub acel prag, lasam stop-loss-ul sa decida - el exista exact pentru
# cazul in care lucrurile merg prost.
RSI_SELL_REQUIRES_PROFIT = True
RSI_SELL_MIN_NET_PROFIT = 0.002   # PnL net minim ca RSI sa aiba voie sa vanda (0.2%)

# --- Time & Position ---
COOLDOWN_MINUTES = 45
MAX_CONCURRENT_POSITIONS = 3

# v26: MAX_HOLD_HOURS urcat de la 48h la 168h (7 zile).
# Motiv: cu 48h, pozitiile care stagneaza (fara sa atinga stop-loss, breakeven
# sau trailing) erau inchise fortat - inclusiv in weekend, cand piata e oricum
# mai linistita si mai putin probabil sa se miste. Regula taia des exact in
# pierdere mica, fara sa fi avut nicio sansa reala sa revina. Nu deranjeaza sa
# stea capitalul blocat intr-o pozitie stagnanta - stop-loss/trailing/breakeven
# raman active tot timpul si protejeaza oricum impotriva unei miscari mari.
# 168h ramane totusi o plasa de siguranta pentru o moneda cu adevarat "moarta",
# nu eliminata complet.
MAX_HOLD_HOURS = 168

# --- Volatilitate ---
ATR_PERIOD = 14

# --- Volum & Spread ---
# v21: BUG REPARAT — inainte comparam volumes_15m[-1], adica candela CURENTA,
# INCOMPLETA. In primele minute ale unei candele de 15m volumul e natural mic,
# deci filtrul pica aproape mereu, indiferent de piata. Acum folosim ultima
# candela INCHISA ([-2]) si comparam cu media candelelor inchise anterioare.
MIN_VOLUME_RATIO = 1.1   # coborat de la 1.2 - cu masuratoarea corecta, 1.2 e prea strict
MAX_SPREAD_PCT = 0.003

# v24: volum DIRECTIONAL, aplicat DOAR strategiei de momentum.
# Problema pe care o rezolva: "volum mare" e ambiguu. O candela ROSIE cu volum
# urias (vanzare agresiva / dump) trece testul volumului la fel de bine ca una
# verde, desi semnaleaza exact opusul unei oportunitati de breakout.
# De ce DOAR pe momentum: mean-reversion cumpara intentionat pe slabiciune -
# scopul ei e sa prinda un dip. Sa-i ceri lumanare verde ar contrazice premisa
# strategiei si ar bloca exact tranzactiile pe care e construita.
MOMENTUM_REQUIRES_GREEN_CANDLE = True

# --- Correlation ---
# v21: relaxat de la 0.85 la 0.92 - altcoin-urile au corelatie de retururi
# aproape mereu >0.85 intre ele, deci dupa prima pozitie deschisa toate
# celelalte erau blocate automat. MAX_CONCURRENT_POSITIONS=3 era teoretic:
# practic aveam mereu 1 singura pozitie. 0.92 blocheaza doar perechile
# aproape identice (ex. BTC/ETH in zile foarte corelate).
CORRELATION_WINDOW = 50
MAX_CORRELATION = 0.92

# --- Market Regime ---
# v21: ADX prag coborat de la 25 la 20, SI regimul TREND nu mai e obligatoriu.
# Inainte "regime == TREND" era o conditie AND absoluta, rar indeplinita.
# Acum: in RANGE cumparam doar pe mean-reversion (nu momentum - un breakout
# fara trend e de obicei fals) si cu pozitie redusa la RANGE_SIZE_MULTIPLIER.
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 20
REQUIRE_TREND_REGIME = False
ALLOW_MOMENTUM_IN_RANGE = False

# --- Filtru BTC ---
BTC_SYMBOL = "BTCUSDT"
BTC_DROP_THRESHOLD = 0.04
BTC_DROP_LOOKBACK_H = 4
BTC_EMA_TOLERANCE = 0.99

# --- Fear & Greed Index ---
# v21: DEZACTIVAT. Pragul de 15 ("frica extrema") se atinge extrem de rar -
# practic era cod mort care adauga un apel API pe ciclu fara sa blocheze
# vreodata ceva. Filtrul BTC acopera deja aceeasi nevoie, mai reactiv.
# Se poate reactiva oricand punand True.
FEAR_GREED_ENABLED = False
FEAR_GREED_EXTREME_THRESHOLD = 15
FEAR_GREED_CACHE_MINUTES = 30

# --- Filtru piata generala crypto ---
# v21: DEZACTIVAT, acelasi motiv - o scadere de peste 3% a capitalizarii
# totale in 24h e rara, iar cand se intampla filtrul BTC deja a blocat.
MARKET_TREND_ENABLED = False
MARKET_DROP_THRESHOLD = 0.03
MARKET_TREND_CACHE_MINUTES = 15

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
    for attempt in range(1, 4):
        try:
            r = session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return
            logger.error(f"Telegram HTTP {r.status_code} (incercarea {attempt}/3): {r.text[:200]}")
        except Exception as e:
            logger.error(f"Telegram error (incercarea {attempt}/3): {e}")
        if attempt < 3:
            time.sleep(2 * attempt)
    logger.error(f"❌ Mesaj Telegram PIERDUT definitiv dupa 3 incercari: {msg[:100]}")

def format_block_stats():
    """
    v21 NOU: raport agregat cu motivele pentru care botul NU a cumparat.
    Asta e informatia cea mai valoroasa pentru reglaje viitoare: in loc sa
    ghicim ce filtru e prea strict, vedem numeric care blocheaza cel mai des.
    """
    if not block_stats:
        return "📊 Încă niciun blocaj înregistrat (sau botul tocmai a pornit)."

    total = sum(block_stats.values())
    lines = [f"📊 Motive pentru care NU am cumpărat"]
    if block_stats_since:
        lines.append(f"(de la {block_stats_since})")
    lines.append(f"Total verificări blocate: {total}\n")

    for reason, count in sorted(block_stats.items(), key=lambda x: -x[1]):
        pct = 100 * count / total if total else 0
        lines.append(f"{reason}: {count} ({pct:.0f}%)")

    lines.append("\n💡 Filtrul din capul listei e cel mai bun candidat de relaxat.")
    return "\n".join(lines)

def check_telegram_commands():
    """
    Verifica o data pe ciclu daca a venit o comanda noua pe Telegram.
    Comenzi: /pause /resume /status /stats /resetstats
    """
    global bot_paused, telegram_last_update_id, block_stats, block_stats_since
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": telegram_last_update_id + 1, "timeout": 0, "limit": 20}
        r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return

        results = r.json().get("result", [])
        for update in results:
            telegram_last_update_id = max(telegram_last_update_id, update.get("update_id", 0))
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip().lower()
            msg_date = msg.get("date", 0)

            if chat_id != str(TELEGRAM_CHAT_ID):
                continue

            if msg_date and msg_date < bot_start_time - 5:
                continue

            if text in ("/pause", "pauza", "/pauza"):
                if not bot_paused:
                    bot_paused = True
                    logger.info("⏸️ Bot PAUZAT manual prin Telegram.")
                    send_telegram("⏸️ Bot pauzat. Nu mai deschid poziții noi.\nPozițiile existente rămân gestionate normal (stop-loss/trailing active).\nScrie /resume ca să reiau.")
                else:
                    send_telegram("⏸️ Botul e deja pauzat.")

            elif text in ("/resume", "reia", "/reia", "/start_bot"):
                if bot_paused:
                    bot_paused = False
                    logger.info("▶️ Bot REPORNIT manual prin Telegram.")
                    send_telegram("▶️ Bot repornit. Reiau căutarea de oportunități noi.")
                else:
                    send_telegram("▶️ Botul rulează deja normal.")

            elif text in ("/status",):
                mode_txt = "⏸️ PAUZAT" if bot_paused else "▶️ ACTIV"
                pos_txt = ", ".join(positions.keys()) if positions else "niciuna"
                send_telegram(f"{mode_txt}\nPoziții deschise: {len(positions)}/{MAX_CONCURRENT_POSITIONS} ({pos_txt})\nPnL azi: {daily_realized_pnl:+.2f} USDT")

            # v21 NOU: raportul de blocaje, ca sa stim ce filtru sa relaxam
            elif text in ("/stats", "/statistici"):
                send_telegram(format_block_stats())

            elif text in ("/resetstats",):
                block_stats = {}
                block_stats_since = datetime.now().strftime("%d.%m %H:%M")
                save_state()
                send_telegram("🔄 Statisticile de blocaje au fost resetate.")

        if results:
            save_state()

    except Exception as e:
        logger.warning(f"Telegram getUpdates eroare (ignor, reincerc urmatorul ciclu): {e}")

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
            try:
                body = r.json()
                bitget_msg = body.get("msg", "")
                bitget_code = body.get("code", "")
                logger.error(f"HTTP {r.status_code} la {url} — Bitget code={bitget_code}, msg={bitget_msg}")
                return body
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
virtual_balance = None
daily_realized_pnl = 0.0
daily_pnl_date = None
bot_paused = False
telegram_last_update_id = 0
bot_start_time = 0.0
block_stats = {}          # v21 NOU: contor {motiv: de_cate_ori_a_blocat}
block_stats_since = None  # v21 NOU: de cand se aduna statisticile

def update_heartbeat(status="ok", extra=None):
    try:
        hb = {"timestamp": datetime.now().isoformat(), "status": status, "positions_count": len(positions), "extra": extra or {}}
        with open(HEARTBEAT_FILE, "w") as f: json.dump(hb, f)
    except Exception as e: logger.error(f"Heartbeat error: {e}")

def load_state():
    global positions, last_sell_time, price_history, virtual_balance, daily_realized_pnl, daily_pnl_date, bot_paused, telegram_last_update_id, block_stats, block_stats_since
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: saved = json.load(f)
            positions = saved.get("positions", {})
            last_sell_time = saved.get("last_sell_time", {})
            virtual_balance = saved.get("virtual_balance", None)
            daily_realized_pnl = saved.get("daily_realized_pnl", 0.0)
            daily_pnl_date = saved.get("daily_pnl_date", None)
            bot_paused = saved.get("bot_paused", False)
            telegram_last_update_id = saved.get("telegram_last_update_id", 0)
            block_stats = saved.get("block_stats", {})
            block_stats_since = saved.get("block_stats_since", None)
            ph_raw = saved.get("price_history", {})
            price_history = {k: deque(v, maxlen=CORRELATION_WINDOW*2) for k, v in ph_raw.items()}
            logger.info(f"Stare încărcată: {len(positions)} poziții" + (" | ⏸️ PAUZAT" if bot_paused else ""))
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
            "bot_paused": bot_paused,
            "telegram_last_update_id": telegram_last_update_id,
            "block_stats": block_stats,
            "block_stats_since": block_stats_since,
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

def reconcile_position_balance(symbol, coin, pos):
    """
    v20: cand un SELL esueaza cu balanta insuficienta, verificam balanta REALA
    de pe Bitget si corectam sau stergem pozitia - altfel botul reincerca
    orbeste aceeasi cantitate gresita la fiecare ciclu, la infinit.
    """
    real_balance = get_spot_balance(coin)
    tracked_qty = pos["quantity"]

    if real_balance <= 0:
        logger.warning(f"⚠️ {symbol}: balanta reala {coin}=0, dar pozitia interna avea qty={tracked_qty}. Sterg pozitia (desincronizata).")
        send_telegram(
            f"⚠️ {symbol}: Poziție desincronizată detectată — balanța reală de {coin} e 0, "
            f"dar botul credea că are {tracked_qty}. Am șters poziția din evidența internă "
            f"(fără să înregistrez PnL - verifică manual istoricul de tranzacții pe Bitget)."
        )
        return None

    diff_ratio = abs(real_balance - tracked_qty) / max(tracked_qty, 1e-9)
    if diff_ratio > 0.01:
        logger.warning(f"⚠️ {symbol}: qty interna {tracked_qty} != balanta reala {real_balance}. Corectez.")
        send_telegram(
            f"⚠️ {symbol}: Am corectat cantitatea internă de la {tracked_qty} "
            f"la balanța reală {real_balance:.6f} {coin} (SELL eșuase din cauza dezacordului)."
        )
        pos["quantity"] = real_balance
        save_state()

    return pos["quantity"]

def normalize_candles(candles):
    """
    v24 NOU: sorteaza candelele CRONOLOGIC CRESCATOR dupa timestamp (campul [0]).

    De ce: codul vechi presupunea implicit o anumita ordine si o "repara" cu
    reversed() in fiecare functie separat - iar functiile NU erau de acord intre
    ele. get_closes() facea reversed(candles), dar calculate_vwap() facea
    candles[-20:] SI APOI reversed. Cu aceeasi lista de intrare, una lua cele
    mai NOI 20 de candele, cealalta pe cele mai VECHI 20 (adica date de acum
    ~37 de ore la interval de 15m). Cel putin una din ele era sigur gresita.
    Solutia: normalizam o singura data, la sursa, dupa timestamp real. Asa
    codul devine imun la ordinea in care API-ul returneaza datele, indiferent
    daca Bitget schimba comportamentul in viitor. Dupa normalizare,
    candles[-1] = cea mai RECENTA candela, peste tot, mereu.
    """
    if not candles:
        return []
    try:
        return sorted(candles, key=lambda c: int(c[0]))
    except (ValueError, TypeError, IndexError) as e:
        logger.warning(f"Nu pot sorta candelele dupa timestamp ({e}), le las asa cum au venit.")
        return candles

def get_candles(symbol, granularity="15min", limit=150):
    path = f"/api/v2/spot/market/candles?symbol={symbol}&granularity={granularity}&limit={limit}"
    data = safe_request("GET", BASE_URL + path)
    raw = data.get("data", []) if data and data.get("code") == "00000" else []
    return normalize_candles(raw)

def get_orderbook(symbol, limit=5):
    path = f"/api/v2/spot/market/orderbook?symbol={symbol}&limit={limit}"
    data = safe_request("GET", BASE_URL + path)
    return data.get("data", {}) if data and data.get("code") == "00000" else None

# v24: candelele vin deja sortate cronologic crescator din get_candles(),
# deci NU mai facem reversed() aici. [-1] = cea mai recenta, [-2] = ultima inchisa.
def get_closes(candles): return [float(c[4]) for c in candles]
def get_volumes(candles): return [float(c[5]) for c in candles]
def get_highs(candles): return [float(c[2]) for c in candles]
def get_lows(candles): return [float(c[3]) for c in candles]
def get_opens(candles): return [float(c[1]) for c in candles]

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
    """
    v24 CORECTAT: RSI dupa formula originala a lui J. Welles Wilder.
    Inainte foloseam netezire EMA clasica, k = 2/(period+1) = 0.133 pentru
    period=14. Wilder foloseste k = 1/period = 0.071 - adica JUMATATE. Efectul:
    RSI-ul nostru era de ~2x mai reactiv decat cel afisat de TradingView sau
    Bitget, deci atingea 70 (si 30) mult mai usor si mai des decat "ar fi
    trebuit". Asta explica de ce regula de vanzare pe RSI>70 se declansa atat
    de devreme - indicatorul sarea peste prag la miscari mici de pret.
    ATENTIE la interpretare: cu formula corecta, valorile RSI stau mai aproape
    de 50, deci pragurile de cumparare (<45) si vanzare (>70) se ating MAI RAR.
    E normal - inseamna semnale mai selective, nu ca botul s-a stricat.
    """
    if len(closes) < period + 1: return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    k = 1 / period  # Wilder's smoothing (RMA), NU 2/(period+1)
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

def calculate_vwap(candles, lookback=20):
    """
    v24 REPARAT: acum ia efectiv ultimele `lookback` candele (cele mai recente),
    nu cele mai vechi - vezi explicatia din normalize_candles().

    Nota de precizie: asta NU e VWAP-ul institutional (care se reseteaza zilnic
    la 00:00 UTC), ci un VWMA - medie ponderata cu volumul pe 20 de perioade.
    Pastram VWMA intentionat: e un reper mobil, util ca filtru de mean-reversion
    la orice ora, pe cand VWAP-ul de sesiune e practic inutil in primele ore
    dupa miezul noptii, cand are prea putine date in spate.
    """
    if not candles: return None
    total_pv, total_v = 0, 0
    for c in candles[-lookback:]:
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

def compute_trade_size(usdt_balance, total_equity, stop_pct, price, size_multiplier=1.0):
    """v21: size_multiplier permite pozitii mai mici in regim RANGE."""
    risk_amount = total_equity * RISK_PER_TRADE * size_multiplier
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

def log_trade_closed(symbol, entry, exit_price, qty, hours, reason, regime,
                      entry_strategy="?", rsi_at_entry=None, adx_at_entry=None, volume_ratio_at_entry=None):
    """
    v27.1: extins cu campurile de la INTRARE (entry_strategy, RSI, ADX, volum),
    nu doar cele de la iesire. Fara astea, trades.csv nu poate raspunde la
    intrebari de genul "tranzactiile momentum cu ADX>30 merg mai bine decat
    cele mean-reversion cu ADX<25?" - exact analiza pe care backtesting-ul
    trebuie sa o faca. Campurile vechi raman neschimbate, doar adaugam la coada.
    """
    pnl_usd = (exit_price - entry) * qty
    pnl_pct = (exit_price - entry) / entry
    try:
        with open(TRADES_CSV, "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now().isoformat(), symbol, entry, exit_price, qty,
                round(pnl_usd,4), round(pnl_pct*100,2), round(hours,2), reason, regime,
                entry_strategy,
                rsi_at_entry if rsi_at_entry is not None else "",
                adx_at_entry if adx_at_entry is not None else "",
                round(volume_ratio_at_entry,2) if volume_ratio_at_entry is not None else ""
            ])
    except Exception as e: logger.error(f"CSV log error: {e}")

# ---------------- DRAWDOWN ZILNIC & PnL NET ----------------
def check_and_reset_daily_pnl():
    global daily_realized_pnl, daily_pnl_date
    today = datetime.now().strftime("%Y-%m-%d")
    if daily_pnl_date != today:
        if daily_pnl_date is not None:
            logger.info(f"📅 Zi noua - reset PnL zilnic (ieri: {daily_realized_pnl:+.2f} USDT)")
        daily_pnl_date = today
        daily_realized_pnl = 0.0

def daily_drawdown_exceeded(total_equity):
    if total_equity <= 0:
        return False
    limit = -total_equity * MAX_DAILY_DRAWDOWN_PCT
    return daily_realized_pnl <= limit

def net_pnl_pct(gross_pnl_pct):
    return gross_pnl_pct - (2 * FEE_RATE_PER_SIDE)


# ---------------- FILTRU BTC ----------------
def get_btc_health():
    candles_1h = get_candles(BTC_SYMBOL, "1h", 100)
    if not candles_1h:
        return True, "date BTC indisponibile, nu blochez"

    closes_1h = get_closes(candles_1h)
    ema50_1h = calculate_ema(closes_1h, EMA_PERIOD_TREND)
    price = get_current_price(BTC_SYMBOL)

    if price == 0 or ema50_1h is None:
        return True, "date BTC incomplete, nu blochez"

    ema_ok = price > ema50_1h * BTC_EMA_TOLERANCE

    drop_ok = True
    change = 0.0
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

    return None, "", True


# ---------------- FILTRU PIATA GENERALA ----------------
_market_cache = {"change_24h": None, "timestamp": 0}

def get_market_health():
    if not MARKET_TREND_ENABLED:
        return True, ""

    now = time.time()
    if _market_cache["change_24h"] is not None and (now - _market_cache["timestamp"]) < MARKET_TREND_CACHE_MINUTES * 60:
        change = _market_cache["change_24h"]
        ok = change > -MARKET_DROP_THRESHOLD * 100
        return ok, f"piata totala {change:+.1f}% in 24h"

    try:
        r = session.get("https://api.coingecko.com/api/v3/global", timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json().get("data", {})
            change = data.get("market_cap_change_percentage_24h_usd")
            if change is not None:
                _market_cache.update(change_24h=change, timestamp=now)
                ok = change > -MARKET_DROP_THRESHOLD * 100
                return ok, f"piata totala {change:+.1f}% in 24h"
    except Exception as e:
        logger.warning(f"Piata generala indisponibila, ignor filtrul: {e}")

    return True, ""


# ---------------- BOT LOOP ----------------
def run_bot():
    global virtual_balance, daily_realized_pnl, daily_pnl_date, bot_start_time, block_stats_since
    bot_start_time = time.time()
    mode = "🧪 DRY RUN" if DRY_RUN else "💰 LIVE"
    load_symbol_precision()
    load_state()

    if block_stats_since is None:
        block_stats_since = datetime.now().strftime("%d.%m %H:%M")

    if DRY_RUN and virtual_balance is None:
        real_start = get_total_equity_bypass_dryrun()
        virtual_balance = real_start if real_start > 0 else 1000.0

    balance_display = f"${virtual_balance:.2f}" if DRY_RUN else "real (din cont)"

    start_msg = (f"🤖 Bot v27.1 (logging extins) pornit! Mod: {mode}\n"
                 f"Balance start: {balance_display}\n"
                 f"💾 Date salvate în: {DATA_DIR}{' ✅ persistent' if DATA_DIR != '.' else ' ⚠️ EFEMER - se pierde la redeploy!'}\n"
                 f"🔧 v27.1: trades.csv acum salvează și RSI/ADX/volum de la INTRARE, nu doar\n"
                 f"   la ieșire — pregătire pentru backtesting (ca să vedem ce combinații chiar merg).\n"
                 f"🔧 v27: Alocare max per tranzactie 25%→{MAX_ALLOCATION_PER_TRADE*100:.0f}%, expunere totala max 60%→{MAX_TOTAL_EXPOSURE_PCT*100:.0f}%.\n"
                 f"   Pozitii mai mari per moneda (ex. ~$6-9 → ~$10-15), risc procentual per tranzactie neschimbat.\n"
                 f"🔧 v26: Time exit la {MAX_HOLD_HOURS}h (7 zile), nu 48h — pozițiile stagnante nu mai sunt\n"
                 f"   tăiate forțat în weekend, când piața oricum se mișcă mai puțin.\n"
                 f"🔧 v25: stop protejat iese la +{BREAKEVEN_STOP_LEVEL*100:.1f}% brut (≈ zero net), nu la -0.5%.\n"
                 f"   Înainte: o poziție care atinsese +1.5% putea ieși în PIERDERE (ADA: -0.8% net).\n"
                 f"🔧 v24 — corecții de calcul:\n"
                 f"• RSI: formula Wilder (k=1/14), ca pe TradingView. Înainte era 2x prea reactiv\n"
                 f"   → valorile stau mai aproape de 50, deci semnale MAI RARE dar mai reale\n"
                 f"• Candele sortate după timestamp — VWAP folosea date vechi de ~37h\n"
                 f"• Momentum cere candelă verde (volum mare pe roșu = dump, nu breakout)\n"
                 f"🔧 v23: RSI>70 închide poziția DOAR dacă e pe profit net (min +{RSI_SELL_MIN_NET_PROFIT*100:.1f}%).\n"
                 f"   Înainte vindea și în pierdere — a cauzat 2 ieșiri pe minus (UNI -0.7%, ADA -0.2%).\n"
                 f"Restul (v21):\n"
                 f"• Volum: măsurat pe candela ÎNCHISĂ (bug reparat), prag {MIN_VOLUME_RATIO}\n"
                 f"• Regim: TREND nu mai e obligatoriu (ADX>{ADX_TREND_THRESHOLD}); în RANGE intru cu poziție {RANGE_SIZE_MULTIPLIER*100:.0f}%\n"
                 f"• Momentum: RSI 15m {RSI_MOMENTUM_MIN}-{RSI_MOMENTUM_MAX}, breakout pe MAXIME reale\n"
                 f"• Corelație: prag urcat la {MAX_CORRELATION} (permite mai multe poziții)\n"
                 f"• Fear&Greed + piață generală: dezactivate (blocau rar, cod mort)\n"
                 f"Comenzi: /pause /resume /status /stats /resetstats\n"
                 f"Risc 2%, Alocare max/tranzactie {MAX_ALLOCATION_PER_TRADE*100:.0f}%, Expunere max {MAX_TOTAL_EXPOSURE_PCT*100:.0f}%, Daily DD max {MAX_DAILY_DRAWDOWN_PCT*100:.0f}%\n"
                 f"Monitorizez: {', '.join(SYMBOLS)}")
    logger.info(start_msg)
    send_telegram(start_msg)

    while True:
        try:
            update_heartbeat("ok", {"loop": "start"})
            check_telegram_commands()
            check_and_reset_daily_pnl()
            usdt_balance = get_spot_balance("USDT")
            total_equity = get_total_equity(usdt_balance)

            dd_blocked = daily_drawdown_exceeded(total_equity)
            logger.info(f"💰 USDT: ${usdt_balance:.2f} | Equity: ${total_equity:.2f} | Pozitii: {len(positions)}/{MAX_CONCURRENT_POSITIONS} | "
                       f"PnL azi: {daily_realized_pnl:+.2f} USDT{' | 🛑 DAILY DRAWDOWN ATINS' if dd_blocked else ''}"
                       f"{' | ⏸️ PAUZAT' if bot_paused else ''}")

            for sym in SYMBOLS:
                p = get_current_price(sym)
                if p > 0:
                    if sym not in price_history: price_history[sym] = deque(maxlen=CORRELATION_WINDOW*2)
                    price_history[sym].append(p)

            btc_healthy, btc_reason = get_btc_health()
            if not btc_healthy:
                logger.info(f"🚫 Filtru BTC activ: {btc_reason} — nu cumpar niciun altcoin in acest ciclu.")

            fg_value, fg_label, fg_ok = get_fear_greed()
            if fg_value is not None:
                logger.info(f"😨 Fear&Greed: {fg_value} ({fg_label}){' — FRICA EXTREMA, precautie la buy' if not fg_ok else ''}")

            mkt_ok, mkt_reason = get_market_health()
            if not mkt_ok:
                logger.info(f"🚫 Piata generala slaba: {mkt_reason} — precautie la buy-uri noi.")

            for symbol in SYMBOLS:
                try:
                    coin = symbol.replace("USDT", "")
                    candles_15m = get_candles(symbol, "15min", 150)
                    candles_1h = get_candles(symbol, "1h", 100)
                    candles_4h = get_candles(symbol, "4h", 100)

                    if not candles_15m or not candles_1h or not candles_4h: continue

                    closes_15m, closes_1h, closes_4h = get_closes(candles_15m), get_closes(candles_1h), get_closes(candles_4h)
                    highs_1h, lows_1h, volumes_15m = get_highs(candles_1h), get_lows(candles_1h), get_volumes(candles_15m)
                    highs_15m = get_highs(candles_15m)  # v21: pentru breakout pe maxime reale

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

                    # v21 BUG REPARAT: foloseam volumes_15m[-1] = candela CURENTA,
                    # INCOMPLETA. In primele minute ale unei candele de 15m volumul
                    # e natural mic, deci filtrul pica aproape mereu indiferent de
                    # piata - probabil cel mai mare blocaj tacut din bot. Acum
                    # comparam ultima candela INCHISA cu media candelelor inchise.
                    if len(volumes_15m) >= 22:
                        last_closed_volume = volumes_15m[-2]
                        avg_volume = sum(volumes_15m[-21:-1]) / 20
                    else:
                        last_closed_volume, avg_volume = 0, 0
                    volume_ok = last_closed_volume > avg_volume * MIN_VOLUME_RATIO if avg_volume > 0 else False

                    # v24: a fost ultima candela INCHISA verde (close > open)?
                    # Folosit doar de momentum - vezi MOMENTUM_REQUIRES_GREEN_CANDLE.
                    opens_15m = get_opens(candles_15m)
                    last_candle_green = (len(opens_15m) >= 2 and len(closes_15m) >= 2
                                         and closes_15m[-2] > opens_15m[-2])

                    spread = get_spread(symbol)
                    spread_ok = spread < MAX_SPREAD_PCT

                    logger.info(f"📊 {symbol} | ${price:.4f} | RSI:{rsi_15m}/{rsi_1h} | Macro:{'✅' if macro_uptrend else '❌'} | "
                                f"Regime:{regime}(ADX {adx}) | Vol:{last_closed_volume/avg_volume if avg_volume else 0:.2f}x")

                    if symbol not in positions:
                        if len(positions) >= MAX_CONCURRENT_POSITIONS: continue

                        in_cooldown = symbol in last_sell_time and (time.time() - last_sell_time[symbol]) / 60 < COOLDOWN_MINUTES
                        corr_ok = check_correlation_filter(symbol)

                        # v21: regimul TREND nu mai e o conditie AND absoluta.
                        # In RANGE intram doar pe mean-reversion (un breakout fara
                        # trend confirmat e de obicei fals) si cu pozitie redusa.
                        regime_ok = (regime == "TREND") if REQUIRE_TREND_REGIME else True
                        size_multiplier = 1.0 if regime == "TREND" else RANGE_SIZE_MULTIPLIER

                        # ---- Calea 1: MEAN-REVERSION - cumpara "ieftin" ----
                        mean_rev_rsi_ok = rsi_15m < RSI_BUY_15M and RSI_MIN_1H < rsi_1h < RSI_MAX_1H
                        if regime == "TREND" and adx and adx > 30:
                            mean_rev_rsi_ok = rsi_15m < RSI_BUY_15M + 5 and rsi_1h < RSI_MAX_1H + 5
                        mean_rev_vwap_ok = price < vwap * 1.01 if vwap else True
                        mean_rev_ok = mean_rev_rsi_ok and mean_rev_vwap_ok

                        # ---- Calea 2: MOMENTUM - cumpara "in forta" ----
                        # v21 doua reparatii:
                        # 1. recent_high se calcula din CLOSES, nu din HIGHS - deci
                        #    nu era un breakout adevarat, ci doar "inchidere peste
                        #    inchiderile recente". Acum folosim maximele reale.
                        # 2. fereastra RSI largita (vezi comentariile de la config).
                        momentum_ok = False
                        if MOMENTUM_ENABLED and len(highs_15m) > MOMENTUM_BREAKOUT_LOOKBACK:
                            if regime == "RANGE" and not ALLOW_MOMENTUM_IN_RANGE:
                                momentum_ok = False
                            else:
                                recent_high = max(highs_15m[-(MOMENTUM_BREAKOUT_LOOKBACK + 1):-1])
                                breakout_ok = price > recent_high * (1 + MOMENTUM_BREAKOUT_MARGIN)
                                momentum_rsi_ok = (RSI_MOMENTUM_MIN < rsi_15m < RSI_MOMENTUM_MAX
                                                    and RSI_MOMENTUM_1H_MIN < rsi_1h < RSI_MOMENTUM_1H_MAX)
                                # v24: breakout-ul trebuie confirmat de o candela verde -
                                # altfel "volum mare" poate insemna dump, nu cerere.
                                green_ok = last_candle_green or not MOMENTUM_REQUIRES_GREEN_CANDLE
                                momentum_ok = breakout_ok and momentum_rsi_ok and green_ok

                        entry_ok = mean_rev_ok or momentum_ok
                        entry_strategy = "momentum" if (momentum_ok and not mean_rev_ok) else "mean-reversion"

                        all_ok = (not bot_paused and not dd_blocked and btc_healthy and fg_ok and mkt_ok
                                  and macro_uptrend and ema_ok and not in_cooldown and volume_ok
                                  and spread_ok and corr_ok and regime_ok and entry_ok)

                        if not all_ok:
                            blocked = []
                            if bot_paused: blocked.append("PAUZAT-manual")
                            if dd_blocked: blocked.append("daily-drawdown")
                            if not btc_healthy: blocked.append("BTC")
                            if not fg_ok: blocked.append("FearGreed")
                            if not mkt_ok: blocked.append("piata-generala")
                            if not macro_uptrend: blocked.append("macro4h")
                            if not ema_ok: blocked.append("EMA1h")
                            if in_cooldown: blocked.append("cooldown")
                            if not entry_ok: blocked.append("semnal-intrare")
                            if not volume_ok: blocked.append("volum")
                            if not spread_ok: blocked.append("spread")
                            if not corr_ok: blocked.append("corelatie")
                            if not regime_ok: blocked.append("regim-RANGE")

                            # v21 NOU: agregam motivele intr-un contor persistent.
                            # Asta e informatia care iti spune, dupa o saptamana de
                            # rulare, exact ce filtru merita relaxat urmatorul -
                            # in loc sa ghicim. Vezi comanda /stats.
                            for b in blocked:
                                block_stats[b] = block_stats.get(b, 0) + 1

                            if blocked:
                                logger.info(f"⏸️ {symbol}: nu cumpar — blocat de: {', '.join(blocked)}")

                        if all_ok:
                            stop_pct = compute_stop_pct(atr, price)
                            trade_amount = compute_trade_size(usdt_balance, total_equity, stop_pct, price, size_multiplier)

                            current_exposure = max(0.0, total_equity - usdt_balance)
                            max_exposure_allowed = total_equity * MAX_TOTAL_EXPOSURE_PCT
                            room_left = max(0.0, max_exposure_allowed - current_exposure)
                            if trade_amount > room_left:
                                trade_amount = room_left
                                logger.info(f"⚠️ {symbol}: sizing redus la ${trade_amount:.2f} din cauza limitei de expunere globala ({MAX_TOTAL_EXPOSURE_PCT*100:.0f}%)")

                            if trade_amount >= MIN_TRADE_USDT:
                                balance_before = 0.0 if DRY_RUN else get_spot_balance(coin)

                                result = place_order(symbol, "buy", amount_usdt=trade_amount)
                                if result.get("code") == "00000":
                                    if DRY_RUN:
                                        real_qty = (trade_amount * 0.999) / price
                                        virtual_balance -= trade_amount
                                    else:
                                        time.sleep(1.5)
                                        balance_after = get_spot_balance(coin)
                                        real_qty = balance_after - balance_before
                                        if real_qty <= 0:
                                            real_qty = (trade_amount * 0.999) / price

                                    volume_ratio_entry = (last_closed_volume / avg_volume) if avg_volume else None
                                    positions[symbol] = {
                                        "price": price, "quantity": real_qty, "peak": price,
                                        "opened_at": datetime.now().isoformat(),
                                        "breakeven_activated": False, "partial_sold": False,
                                        "stop_pct": stop_pct, "entry_strategy": entry_strategy,
                                        "regime": regime,
                                        # v27.1: pastram contextul de la intrare pentru backtesting/analiza
                                        "rsi_at_entry": rsi_15m, "adx_at_entry": adx,
                                        "volume_ratio_at_entry": volume_ratio_entry
                                    }
                                    save_state()
                                    strat_emoji = "🚀" if entry_strategy == "momentum" else "🔻"
                                    size_note = f" (poziție {size_multiplier*100:.0f}% - regim {regime})" if size_multiplier < 1.0 else ""
                                    msg = (f"🟢 BUY {symbol} {strat_emoji} {entry_strategy}\n💵 ${trade_amount:.2f} la ${price:.4f}{size_note}\n"
                                           f"📊 RSI={rsi_15m}, ADX={adx}, Regim={regime}, Stop={stop_pct*100:.1f}%\n"
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
                                elif not DRY_RUN:
                                    logger.error(f"❌ Eroare partial SELL {symbol}: {r}")
                                    reconcile_position_balance(symbol, coin, pos)

                        should_sell, reason = False, ""
                        stop_pct = pos.get("stop_pct", 0.025)

                        if pnl_pct <= -stop_pct:
                            should_sell, reason = True, f"🛑 SL {pnl_pct*100:.1f}% (Stop: {stop_pct*100:.1f}%)"
                        # v25: iesim la +0.2% brut (≈ zero net), nu la -0.5%.
                        elif pos.get("breakeven_activated") and pnl_pct <= BREAKEVEN_STOP_LEVEL:
                            should_sell, reason = True, f"🔒 Stop protejat la +{BREAKEVEN_STOP_LEVEL*100:.1f}% (PnL: {pnl_pct*100:+.1f}%)"
                        elif peak_pnl >= TRAILING_TRIGGER and drop_from_peak >= TRAILING_DISTANCE:
                            should_sell, reason = True, f"📉 Trailing (Vârf: +{peak_pnl*100:.1f}%, Acum: +{pnl_pct*100:.1f}%)"
                        # v23: conditia noua - RSI vinde doar daca suntem efectiv pe
                        # profit net. Vezi comentariul de la RSI_SELL_REQUIRES_PROFIT.
                        elif (peak_pnl < TRAILING_TRIGGER and rsi_15m > RSI_SELL
                              and drop_from_peak >= RSI_SELL_MIN_DROP_FROM_PEAK
                              and (not RSI_SELL_REQUIRES_PROFIT
                                   or net_pnl_pct(pnl_pct) >= RSI_SELL_MIN_NET_PROFIT)):
                            should_sell, reason = True, f"📊 RSI={rsi_15m} > {RSI_SELL} (net +{net_pnl_pct(pnl_pct)*100:.1f}%)"
                        else:
                            # v23: daca RSI ar fi vandut dar pozitia e pe minus, logam -
                            # asa vezi in loguri de cate ori te-a salvat noua regula.
                            if (peak_pnl < TRAILING_TRIGGER and rsi_15m > RSI_SELL
                                    and drop_from_peak >= RSI_SELL_MIN_DROP_FROM_PEAK):
                                logger.info(f"🛡️ {symbol}: RSI={rsi_15m} ar fi vandut, dar PnL net "
                                            f"{net_pnl_pct(pnl_pct)*100:+.2f}% < {RSI_SELL_MIN_NET_PROFIT*100:.1f}% "
                                            f"— tin pozitia, las stop-loss-ul sa decida.")
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

                                    net_pct = net_pnl_pct(pnl_pct)
                                    net_usd = (price - entry) * sell_qty - (entry * sell_qty + price * sell_qty) * FEE_RATE_PER_SIDE

                                    daily_realized_pnl += net_usd

                                    msg = (f"🔴 SELL {symbol}\n{reason}\n{emoji} PnL brut: {pnl_pct*100:+.1f}% | "
                                           f"net (cu taxe): {net_pct*100:+.1f}% (${net_usd:+.2f})\n"
                                           f"📅 PnL azi: {daily_realized_pnl:+.2f} USDT\n"
                                           f"{'🧪 SIM' if DRY_RUN else '💰 REAL'}")
                                    logger.info(msg); send_telegram(msg)
                                    log_trade_closed(symbol, entry, price, sell_qty, hours_held, reason, pos.get("regime", "?"),
                                                      entry_strategy=pos.get("entry_strategy", "?"),
                                                      rsi_at_entry=pos.get("rsi_at_entry"),
                                                      adx_at_entry=pos.get("adx_at_entry"),
                                                      volume_ratio_at_entry=pos.get("volume_ratio_at_entry"))
                                    last_sell_time[symbol] = time.time()
                                    del positions[symbol]
                                    save_state()
                                else:
                                    error_msg = result.get('msg', 'necunoscut')
                                    logger.error(f"❌ Eroare SELL: {result}")

                                    if not DRY_RUN and ("insufficient" in error_msg.lower() or "balance" in error_msg.lower()):
                                        corrected = reconcile_position_balance(symbol, coin, pos)
                                        if corrected is None:
                                            last_sell_time[symbol] = time.time()
                                            del positions[symbol]
                                            save_state()
                                    else:
                                        send_telegram(f"❌ Eroare SELL {symbol}: {error_msg}")
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
