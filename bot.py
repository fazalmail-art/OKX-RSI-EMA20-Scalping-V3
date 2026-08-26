import os
import time
import json
import hmac
import base64
import hashlib
import threading
import uuid
import sqlite3
import traceback
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, time as dtime, date
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, Response, request as flask_request
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# OKX SCALPING BOT V14.2 — SIX-POINT CONFLUENCE + INSTITUTIONAL RISK LAYER
#                          + PA-GATE/SCORE ALIGNMENT + SIMPLIFIED DASHBOARD
# DEMO ONLY by default. No profitability guarantee. Trading involves risk
# of total capital loss. This is not financial advice.
#
# V14.2: dashboard simplified -- raw indicator columns (ADX, volume ratio,
# ATR%, funding, OI) removed from the UI (still computed internally and
# available via /api/status JSON for anyone who wants them). Trend and
# Signal are now large colored badges so the current directional bias is
# readable at a glance for manual/discretionary entries.
#
# V14.1: fixed a mismatch where pa_score (shown as X/5) counted RETEST as
# its 5th stage while the actual trade-enabling gate required BOS_RECENT
# instead -- a fully "5/5" setup could still have its gate closed because
# BOS aged past PA_BOS_MAX_AGE_BARS by the time retest printed (retest is
# downstream of BOS by construction). Gate now accepts BOS_RECENT OR
# RETEST_ok. PA_BOS_MAX_AGE_BARS default 3 -> 5 (small extra breathing
# room). SR + SWEEP + RECLAIM + BOS remain unconditionally mandatory.
#
# V14.0: SQLite persistence (trades/events/kv_state), Telegram alerting,
# circuit breakers (daily loss / max drawdown / consecutive losses),
# manual kill switch (/api/kill, /api/resume), layered emergency close
# (close-position -> manual reduce-only order -> critical alert + halt),
# volatility-adjusted position sizing, correlation-aware exposure limits,
# periodic OKX time resync, startup config validation.
#
# V13.1 core (unchanged): lock separation for instrument/funding caches,
# PA-first price action detection (SR/SWEEP/RECLAIM/BOS/RETEST), step-based
# trailing take-profit, IOC fresh-price slippage guard, business-vs-
# transient error handling in private_request().
# =========================================================

VERSION = "V14.2-SIX-POINT-INSTITUTIONAL-SIMPLIFIED"
BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

BAR = os.getenv("BAR", "5m")
TREND_BAR = os.getenv("TREND_BAR", "15m")
MARGIN_USDT = Decimal(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = Decimal(os.getenv("LEVERAGE", "3"))
TD_MODE = os.getenv("TD_MODE", "isolated")
MAX_TOTAL_NOTIONAL_USDT = Decimal(os.getenv("MAX_TOTAL_NOTIONAL_USDT", "530"))

SL_PERCENT = Decimal(os.getenv("SL_PERCENT", "0.46"))
TP_PERCENT = Decimal(os.getenv("TP_PERCENT", "0.80"))
MAX_SL_DISTANCE_PCT = Decimal(os.getenv("MAX_SL_DISTANCE_PCT", "0.80"))
BREAK_EVEN_TRIGGER_PCT = Decimal(os.getenv("BREAK_EVEN_TRIGGER_PCT", "0.35"))
BREAK_EVEN_OFFSET_PCT = Decimal(os.getenv("BREAK_EVEN_OFFSET_PCT", "0.25"))
TRAIL_START_PCT = Decimal(os.getenv("TRAIL_START_PCT", "0.50"))
TRAIL_DISTANCE_PCT = Decimal(os.getenv("TRAIL_DISTANCE_PCT", "0.30"))
PROTECTION_RETRY_SECONDS = int(os.getenv("PROTECTION_RETRY_SECONDS", "5"))
STEP_TRIGGER_PCT = Decimal(os.getenv("STEP_TRIGGER_PCT", "0.50"))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "5"))
MAJOR_MIN_SCORE = int(os.getenv("MAJOR_MIN_SCORE", "5"))
NON_PRIORITY_MIN_SCORE = int(os.getenv("NON_PRIORITY_MIN_SCORE", "5"))

PA_LOOKBACK = int(os.getenv("PA_LOOKBACK", "24"))
PA_MAX_AGE_BARS = int(os.getenv("PA_MAX_AGE_BARS", "6"))
PA_RETEST_MAX_BARS = int(os.getenv("PA_RETEST_MAX_BARS", "5"))
SR_ATR_MULT = Decimal(os.getenv("SR_ATR_MULT", os.getenv("PA_SR_ATR_DISTANCE", "1.30")))
SR_PCT = Decimal(os.getenv("SR_PCT", "0.10"))
PA_SWEEP_ATR_BUFFER = Decimal(os.getenv("PA_SWEEP_ATR_BUFFER", "0.20"))
PA_BOS_MAX_AGE_BARS = int(os.getenv("PA_BOS_MAX_AGE_BARS", "5"))

ADX_MIN = Decimal(os.getenv("ADX_MIN", "18"))
VOLUME_MULT = Decimal(os.getenv("VOLUME_MULT", "1.00"))
ATR_MIN_PCT = Decimal(os.getenv("ATR_MIN_PCT", "0.05"))

FUNDING_EXTREME_PCT = Decimal(os.getenv("FUNDING_EXTREME_PCT", "0.03"))
FUNDING_LOOKBACK = int(os.getenv("FUNDING_LOOKBACK", "30"))
OI_UNWIND_PCT = Decimal(os.getenv("OI_UNWIND_PCT", "0.30"))

FEE_BUFFER_USDT = Decimal(os.getenv("FEE_BUFFER_USDT", "0.13"))
FEE_RATE_PER_SIDE = Decimal(os.getenv("FEE_RATE_PER_SIDE", "0.0006"))

ENTRY_ORDER_TYPE = os.getenv("ENTRY_ORDER_TYPE", "ioc")
MAX_ENTRY_SLIPPAGE_PCT = Decimal(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.20"))
MIN_SIDE_DEPTH_MULT = Decimal(os.getenv("MIN_SIDE_DEPTH_MULT", "3.0"))
ORDERBOOK_LEVELS = int(os.getenv("ORDERBOOK_LEVELS", "5"))

INSTRUMENT_CACHE_SECONDS = int(os.getenv("INSTRUMENT_CACHE_SECONDS", "3600"))
FUNDING_CACHE_SECONDS = int(os.getenv("FUNDING_CACHE_SECONDS", "300"))

DB_PATH = os.getenv("DB_PATH", "bot_state.db")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DAILY_MAX_LOSS_PCT = Decimal(os.getenv("DAILY_MAX_LOSS_PCT", "3"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))
MAX_DRAWDOWN_PCT = Decimal(os.getenv("MAX_DRAWDOWN_PCT", "8"))
TIME_RESYNC_SECONDS = int(os.getenv("TIME_RESYNC_SECONDS", "1800"))
VOL_SIZE_ADJUST = os.getenv("VOL_SIZE_ADJUST", "true").lower() == "true"
VOL_SIZE_BASELINE_ATR_PCT = Decimal(os.getenv("VOL_SIZE_BASELINE_ATR_PCT", "0.15"))
VOL_SIZE_MIN_MULT = Decimal(os.getenv("VOL_SIZE_MIN_MULT", "0.5"))
VOL_SIZE_MAX_MULT = Decimal(os.getenv("VOL_SIZE_MAX_MULT", "1.25"))
MAX_CORRELATED_POSITIONS = int(os.getenv("MAX_CORRELATED_POSITIONS", "2"))
try:
    CORRELATION_GROUPS = json.loads(os.getenv("CORRELATION_GROUPS", "{}"))
except Exception:
    CORRELATION_GROUPS = {}
KILL_SWITCH_TOKEN = os.getenv("KILL_SWITCH_TOKEN", "")

PKT_TZ = ZoneInfo("Asia/Karachi")

SESSION_WINDOWS = (
    (dtime(1, 0), dtime(2, 30), "OBSERVED_01"),
    (dtime(6, 0), dtime(7, 0), "OBSERVED_06"),
    (dtime(10, 0), dtime(11, 0), "OBSERVED_10"),
    (dtime(13, 0), dtime(17, 0), "LONDON"),
    (dtime(17, 0), dtime(21, 0), "LONDON_NY_OVERLAP"),
    (dtime(19, 0), dtime(23, 0), "NY"),
)
PRIORITY_WINDOWS = (
    (dtime(1, 0), dtime(2, 0), "OBSERVED_01_PRIORITY"),
    (dtime(6, 0), dtime(7, 0), "OBSERVED_06_PRIORITY"),
    (dtime(10, 0), dtime(11, 0), "OBSERVED_10_PRIORITY"),
    (dtime(13, 0), dtime(14, 0), "LONDON_PRIORITY"),
    (dtime(17, 0), dtime(18, 0), "OVERLAP_PRIORITY"),
    (dtime(19, 0), dtime(20, 0), "NY_PRIORITY"),
)

SYMBOLS = [x.strip() for x in os.getenv(
    "SYMBOLS",
    "BTC-USDT-SWAP,ETH-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP,SOL-USDT-SWAP,SHIB-USDT-SWAP,FIL-USDT-SWAP,NEAR-USDT-SWAP,ICP-USDT-SWAP,XAU-USDT-SWAP"
).split(",") if x.strip()]

MEME_SYMBOLS = {"DOGE-USDT-SWAP", "SHIB-USDT-SWAP"}

app = Flask(__name__)
session = requests.Session()

state = {}
state_lock = threading.Lock()
order_lock = threading.Lock()
db_lock = threading.Lock()

server_offset_ms = 0
last_time_sync = 0.0
worker_started = False
worker_error = ""

position_snapshot_ts = 0.0
position_snapshot = {}

instrument_cache = {}
instrument_cache_lock = threading.Lock()
funding_cache = {}
funding_cache_lock = threading.Lock()

TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
NON_RETRY_CODES = {"50123", "50113", "50114", "50013", "50119"}

risk_state = {
    "trading_halted": False,
    "halt_reason": "",
    "day": None,
    "day_start_equity": None,
    "peak_equity": None,
    "consecutive_losses": 0,
    "last_trade_result": None,
}
risk_lock = threading.Lock()


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def dec(value):
    return Decimal(str(value))


def fmt(value, places=12):
    if value is None:
        return "-"
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def floor_step(value, step):
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value, step):
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


# =========================================================
# PERSISTENCE LAYER (SQLite)
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init():
    with db_lock:
        conn = db_connect()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, entry REAL, exit REAL,
                sl REAL, tp REAL, size TEXT, notional REAL,
                pnl_usdt REAL, opened_at TEXT, closed_at TEXT,
                status TEXT, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, level TEXT, symbol TEXT, message TEXT
            );
            CREATE TABLE IF NOT EXISTS kv_state (
                key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
            );
            """)
            conn.commit()
        finally:
            conn.close()


def db_log_event(level, symbol, message):
    try:
        with db_lock:
            conn = db_connect()
            try:
                conn.execute(
                    "INSERT INTO events (ts, level, symbol, message) VALUES (?,?,?,?)",
                    (datetime.now().isoformat(), level, symbol or "", message[:2000]),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        log(f"DB EVENT LOG FAILED | {exc}")


def db_record_trade_open(symbol, side, entry, sl, tp, size, notional):
    try:
        with db_lock:
            conn = db_connect()
            try:
                cur = conn.execute(
                    "INSERT INTO trades (symbol, side, entry, sl, tp, size, notional, opened_at, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (symbol, side, float(entry), float(sl), float(tp), fmt(size), float(notional),
                     datetime.now().isoformat(), "OPEN"),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
    except Exception as exc:
        log(f"DB TRADE OPEN LOG FAILED | {exc}")
        return None


def db_record_trade_close(symbol, exit_price, pnl_usdt, reason):
    try:
        with db_lock:
            conn = db_connect()
            try:
                conn.execute(
                    "UPDATE trades SET exit=?, pnl_usdt=?, closed_at=?, status=?, reason=? "
                    "WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
                    (float(exit_price), float(pnl_usdt), datetime.now().isoformat(), "CLOSED", reason, symbol),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        log(f"DB TRADE CLOSE LOG FAILED | {exc}")


def db_kv_set(key, value):
    try:
        with db_lock:
            conn = db_connect()
            try:
                conn.execute(
                    "INSERT INTO kv_state (key, value, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, json.dumps(value, default=str), datetime.now().isoformat()),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        log(f"DB KV SET FAILED | {key} | {exc}")


def db_kv_get(key, default=None):
    try:
        with db_lock:
            conn = db_connect()
            try:
                row = conn.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
                return json.loads(row[0]) if row else default
            finally:
                conn.close()
    except Exception as exc:
        log(f"DB KV GET FAILED | {key} | {exc}")
        return default


# =========================================================
# ALERTING LAYER (Telegram, optional)
# =========================================================

def send_alert(message, level="INFO", symbol=None):
    log(f"ALERT[{level}] {('(' + symbol + ') ') if symbol else ''}{message}")
    db_log_event(level, symbol, message)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        text = f"[{level}] {VERSION}\n{('Symbol: ' + symbol) if symbol else ''}\n{message}"
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=8,
        )
    except Exception as exc:
        log(f"TELEGRAM ALERT FAILED | {exc}")


# =========================================================
# OKX API LAYER
# =========================================================

def public_get(path, params=None, retries=3, raw=False):
    last = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(BASE_URL + path, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            if raw:
                return data
            if data.get("code") != "0":
                raise RuntimeError(f"OKX PUBLIC {data.get('code')}: {data.get('msg')}")
            return data
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.4 * attempt)
    raise RuntimeError(f"OKX PUBLIC failed {path}: {last}")


def sync_okx_time():
    global server_offset_ms, last_time_sync
    before = int(time.time() * 1000)
    data = public_get("/api/v5/public/time")
    after = int(time.time() * 1000)
    server_ms = int(data["data"][0]["ts"])
    server_offset_ms = server_ms - ((before + after) // 2)
    last_time_sync = time.time()
    log(f"OKX TIME SYNCED | offset_ms={server_offset_ms}")


def maybe_resync_time():
    if time.time() - last_time_sync >= TIME_RESYNC_SECONDS:
        try:
            sync_okx_time()
        except Exception as exc:
            log(f"TIME RESYNC WARNING | {exc}")


def utc_timestamp():
    ms = int(time.time() * 1000) + server_offset_ms
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_signature(timestamp, method, request_path, body=""):
    prehash = timestamp + method.upper() + request_path + body
    digest = hmac.new(SECRET_KEY.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def private_request(method, path, payload=None, params=None, retries=3):
    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        raise RuntimeError("OKX API credentials missing")
    method = method.upper()
    request_path = path
    if params:
        request_path += "?" + urlencode([(str(k), str(v)) for k, v in params.items()])
    body = "" if payload is None else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    last = None
    for attempt in range(1, retries + 1):
        timestamp = utc_timestamp()
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": API_KEY,
            "OK-ACCESS-SIGN": create_signature(timestamp, method, request_path, body),
            "OK-ACCESS-PASSPHRASE": PASSPHRASE,
            "OK-ACCESS-TIMESTAMP": timestamp,
        }
        if DEMO:
            headers["x-simulated-trading"] = "1"
        try:
            response = session.request(method, BASE_URL + path, headers=headers, data=body or None, params=params, timeout=15)
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}

            if response.status_code >= 400:
                if response.status_code in TRANSIENT_HTTP and attempt < retries:
                    last = RuntimeError(f"HTTP {response.status_code}: {data}")
                    time.sleep(0.6 * attempt)
                    continue
                raise RuntimeError(f"OKX PRIVATE {response.status_code}: {data}")

            code = str(data.get("code", ""))
            if code != "0":
                raise RuntimeError(f"OKX PRIVATE ERROR {code}: {data.get('msg')}")

            return data

        except RuntimeError:
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
            if attempt == retries:
                raise RuntimeError(f"OKX PRIVATE failed {method} {path}: {last}")
            time.sleep(0.6 * attempt)

    raise RuntimeError(f"OKX PRIVATE failed {method} {path}: {last}")


def get_ticker(symbol):
    rows = public_get("/api/v5/market/ticker", {"instId": symbol}).get("data", [])
    if not rows:
        raise RuntimeError(f"Ticker unavailable: {symbol}")
    return dec(rows[0]["last"])


def get_mark_price(symbol):
    rows = public_get("/api/v5/public/mark-price", {"instType": "SWAP", "instId": symbol}).get("data", [])
    return dec(rows[0]["markPx"]) if rows else get_ticker(symbol)


def get_orderbook(symbol, sz=None):
    params = {"instId": symbol, "sz": str(sz or ORDERBOOK_LEVELS)}
    rows = public_get("/api/v5/market/books", params).get("data", [])
    if not rows:
        raise RuntimeError(f"Orderbook unavailable: {symbol}")
    row = rows[0]
    bids = [(dec(x[0]), dec(x[1])) for x in row.get("bids", [])]
    asks = [(dec(x[0]), dec(x[1])) for x in row.get("asks", [])]
    if not bids or not asks:
        raise RuntimeError(f"Empty orderbook: {symbol}")
    return bids, asks


def get_candles(symbol, bar, limit=180):
    data = public_get("/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": str(limit)})
    out = []
    for row in reversed(data.get("data", [])):
        out.append({
            "ts": int(row[0]), "open": dec(row[1]), "high": dec(row[2]), "low": dec(row[3]),
            "close": dec(row[4]), "volume": dec(row[5]), "confirm": row[8] if len(row) > 8 else "1"
        })
    return out


def ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    value = sum(values[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = value
    k = Decimal("2") / Decimal(period + 1)
    for i in range(period, len(values)):
        value = values[i] * k + value * (Decimal("1") - k)
        result[i] = value
    return result


def rsi(values, period):
    result = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(values[i] - values[i - 1], Decimal("0")) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], Decimal("0")) for i in range(1, len(values))]
    ag = sum(gains[:period], Decimal("0")) / Decimal(period)
    al = sum(losses[:period], Decimal("0")) / Decimal(period)

    def value(g, l):
        if l == 0:
            return Decimal("100")
        rs = g / l
        return Decimal("100") - Decimal("100") / (Decimal("1") + rs)

    result[period] = value(ag, al)
    for j in range(period, len(gains)):
        ag = (ag * Decimal(period - 1) + gains[j]) / Decimal(period)
        al = (al * Decimal(period - 1) + losses[j]) / Decimal(period)
        result[j + 1] = value(ag, al)
    return result


def rma(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    x = sum(values[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = x
    for i in range(period, len(values)):
        x = (x * Decimal(period - 1) + values[i]) / Decimal(period)
        result[i] = x
    return result


def atr(candles, period=14):
    if len(candles) <= period:
        return None
    trs = []
    for i in range(1, len(candles)):
        trs.append(max(candles[i]["high"] - candles[i]["low"], abs(candles[i]["high"] - candles[i - 1]["close"]), abs(candles[i]["low"] - candles[i - 1]["close"])))
    vals = rma(trs, period)
    return next((v for v in reversed(vals) if v is not None), None)


def adx(candles, period=14):
    if len(candles) < period * 2 + 2:
        return Decimal("0")
    plus, minus, trs = [], [], []
    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus.append(up if up > down and up > 0 else Decimal("0"))
        minus.append(down if down > up and down > 0 else Decimal("0"))
        trs.append(max(candles[i]["high"] - candles[i]["low"], abs(candles[i]["high"] - candles[i - 1]["close"]), abs(candles[i]["low"] - candles[i - 1]["close"])))
    sp, sm, st = rma(plus, period), rma(minus, period), rma(trs, period)
    dx = []
    for i in range(len(trs)):
        if sp[i] is None or sm[i] is None or not st[i]:
            continue
        pdi = sp[i] / st[i] * Decimal("100")
        mdi = sm[i] / st[i] * Decimal("100")
        total = pdi + mdi
        dx.append(abs(pdi - mdi) / total * Decimal("100") if total else Decimal("0"))
    series = rma(dx, period)
    return next((v for v in reversed(series) if v is not None), Decimal("0"))


def macd(values):
    e12, e26 = ema(values, 12), ema(values, 26)
    line = [e12[i] - e26[i] if e12[i] is not None and e26[i] is not None else None for i in range(len(values))]
    valid = [x for x in line if x is not None]
    sig_valid = ema(valid, 9)
    signal = [None] * (len(values) - len(sig_valid)) + sig_valid
    return line, signal


def session_vwap(candles):
    if not candles:
        return None
    last = candles[-1]["ts"]
    day_start = last - (last % (24 * 60 * 60 * 1000))
    q = [x for x in candles if x["ts"] >= day_start] or candles[-30:]
    vol = sum((x["volume"] for x in q), Decimal("0"))
    return q[-1]["close"] if vol == 0 else sum(((x["high"] + x["low"] + x["close"]) / Decimal("3")) * x["volume"] for x in q) / vol


def session_info():
    now = datetime.now(PKT_TZ).time()
    for start, end, label in PRIORITY_WINDOWS:
        if start <= now < end:
            return {"name": label, "priority": True, "active": True}
    for start, end, label in SESSION_WINDOWS:
        if start <= now < end:
            return {"name": label, "priority": False, "active": True}
    return {"name": "OFF_SESSION", "priority": False, "active": False}


def get_trend_from_candles(candles):
    cs = [x for x in candles if x["confirm"] == "1"]
    if len(cs) < 22:
        return "flat"
    values = [x["close"] for x in cs]
    e = ema(values, 20)
    i = len(values) - 1
    if e[i] is None or e[i - 1] is None:
        return "flat"
    if values[i] > e[i] and e[i] > e[i - 1]:
        return "bull"
    if values[i] < e[i] and e[i] < e[i - 1]:
        return "bear"
    return "flat"


def get_instrument(symbol, force=False):
    now = time.time()
    with instrument_cache_lock:
        cached = instrument_cache.get(symbol)
        if cached and not force and now - cached["ts"] < INSTRUMENT_CACHE_SECONDS:
            return cached["data"]
    data = public_get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol})
    rows = data.get("data", [])
    if not rows:
        raise RuntimeError(f"Instrument not found: {symbol}")
    x = rows[0]
    result = {"ctVal": dec(x["ctVal"]), "lotSz": dec(x["lotSz"]), "minSz": dec(x["minSz"]), "tickSz": dec(x["tickSz"]), "state": x.get("state", "")}
    with instrument_cache_lock:
        instrument_cache[symbol] = {"ts": now, "data": result}
    return result


def get_funding_snapshot(symbol):
    now = time.time()
    with funding_cache_lock:
        cached = funding_cache.get(symbol)
        if cached and now - cached["ts"] < FUNDING_CACHE_SECONDS:
            return cached
    try:
        current_rows = public_get("/api/v5/public/funding-rate", {"instId": symbol}).get("data", [])
        current = dec(current_rows[0]["fundingRate"]) * Decimal("100") if current_rows else None
        hist = public_get("/api/v5/public/funding-rate-history", {"instId": symbol, "limit": str(FUNDING_LOOKBACK)}).get("data", [])
        history = [dec(x["fundingRate"]) * Decimal("100") for x in hist]
        if len(history) >= 10:
            vals = sorted(abs(x) for x in history)
            threshold = max(vals[min(int(len(vals) * 0.85), len(vals) - 1)], Decimal("0.005"))
        else:
            threshold = FUNDING_EXTREME_PCT
        snap = {"ts": now, "current": current, "threshold": threshold}
    except Exception as exc:
        log(f"FUNDING WARNING | {symbol} | {exc}")
        snap = {"ts": now, "current": None, "threshold": FUNDING_EXTREME_PCT}
    with funding_cache_lock:
        funding_cache[symbol] = snap
    return snap


def refresh_position_snapshot():
    global position_snapshot, position_snapshot_ts
    data = private_request("GET", "/api/v5/account/positions", params={"instType": "SWAP"})
    snap = {}
    for row in data.get("data", []):
        try:
            if dec(row.get("pos", "0")) != 0:
                snap[row.get("instId")] = row
        except Exception:
            continue
    with state_lock:
        position_snapshot = snap
        position_snapshot_ts = time.time()
        for symbol in SYMBOLS:
            state.setdefault(symbol, {})["position_present"] = symbol in snap
    return snap


def get_cached_position(symbol):
    return position_snapshot.get(symbol)


def position_notional(position, symbol):
    if not position:
        return Decimal("0")
    try:
        info = get_instrument(symbol)
        return abs(dec(position.get("pos", "0"))) * info["ctVal"] * dec(position.get("markPx") or position.get("avgPx") or "0")
    except Exception:
        return Decimal("0")


def total_open_notional(snapshot=None):
    snap = snapshot if snapshot is not None else position_snapshot
    total = Decimal("0")
    for symbol, pos in snap.items():
        total += position_notional(pos, symbol)
    return total


def get_account_equity():
    try:
        data = private_request("GET", "/api/v5/account/balance")
        rows = data.get("data", [])
        if not rows:
            return None
        detail = rows[0].get("details", [])
        usdt = next((d for d in detail if d.get("ccy") == "USDT"), None)
        if usdt:
            return dec(usdt.get("eq") or usdt.get("cashBal") or "0")
        return dec(rows[0].get("totalEq", "0"))
    except Exception as exc:
        log(f"EQUITY FETCH WARNING | {exc}")
        return None


# =========================================================
# RISK / CIRCUIT BREAKER LAYER
# =========================================================

def risk_init():
    saved = db_kv_get("risk_state")
    with risk_lock:
        if saved:
            risk_state.update(saved)
        today = date.today().isoformat()
        if risk_state.get("day") != today:
            equity = get_account_equity()
            risk_state["day"] = today
            risk_state["day_start_equity"] = float(equity) if equity is not None else None
            risk_state["consecutive_losses"] = 0
            risk_state["trading_halted"] = False
            risk_state["halt_reason"] = ""
        if risk_state.get("peak_equity") is None:
            equity = get_account_equity()
            risk_state["peak_equity"] = float(equity) if equity is not None else None
        db_kv_set("risk_state", risk_state)


def risk_roll_day_if_needed():
    today = date.today().isoformat()
    with risk_lock:
        if risk_state.get("day") != today:
            equity = get_account_equity()
            risk_state["day"] = today
            risk_state["day_start_equity"] = float(equity) if equity is not None else risk_state.get("day_start_equity")
            risk_state["consecutive_losses"] = 0
            if risk_state.get("halt_reason", "").startswith("DAILY_LOSS"):
                risk_state["trading_halted"] = False
                risk_state["halt_reason"] = ""
            db_kv_set("risk_state", risk_state)


def risk_check_circuit_breakers():
    risk_roll_day_if_needed()
    with risk_lock:
        if risk_state.get("trading_halted"):
            return False, risk_state.get("halt_reason", "HALTED")

    equity = get_account_equity()
    if equity is None:
        return True, ""

    with risk_lock:
        if risk_state.get("peak_equity") is None or equity > Decimal(str(risk_state["peak_equity"])):
            risk_state["peak_equity"] = float(equity)

        peak = Decimal(str(risk_state["peak_equity"])) if risk_state.get("peak_equity") else equity
        day_start = Decimal(str(risk_state["day_start_equity"])) if risk_state.get("day_start_equity") else equity

        drawdown_pct = (peak - equity) / peak * Decimal("100") if peak > 0 else Decimal("0")
        daily_loss_pct = (day_start - equity) / day_start * Decimal("100") if day_start > 0 else Decimal("0")

        if drawdown_pct >= MAX_DRAWDOWN_PCT:
            risk_state["trading_halted"] = True
            risk_state["halt_reason"] = f"MAX_DRAWDOWN {fmt(drawdown_pct,2)}% >= {MAX_DRAWDOWN_PCT}%"
            db_kv_set("risk_state", risk_state)
            send_alert(f"Trading halted: max drawdown hit ({fmt(drawdown_pct,2)}%).", "CRITICAL")
            return False, risk_state["halt_reason"]

        if daily_loss_pct >= DAILY_MAX_LOSS_PCT:
            risk_state["trading_halted"] = True
            risk_state["halt_reason"] = f"DAILY_LOSS {fmt(daily_loss_pct,2)}% >= {DAILY_MAX_LOSS_PCT}%"
            db_kv_set("risk_state", risk_state)
            send_alert(f"Trading halted: daily loss limit hit ({fmt(daily_loss_pct,2)}%).", "CRITICAL")
            return False, risk_state["halt_reason"]

        if risk_state.get("consecutive_losses", 0) >= MAX_CONSECUTIVE_LOSSES:
            risk_state["trading_halted"] = True
            risk_state["halt_reason"] = f"CONSECUTIVE_LOSSES {risk_state['consecutive_losses']} >= {MAX_CONSECUTIVE_LOSSES}"
            db_kv_set("risk_state", risk_state)
            send_alert(f"Trading halted: {risk_state['consecutive_losses']} consecutive losing trades.", "CRITICAL")
            return False, risk_state["halt_reason"]

        db_kv_set("risk_state", risk_state)
    return True, ""


def risk_record_trade_result(pnl_usdt):
    with risk_lock:
        if pnl_usdt is not None and pnl_usdt < 0:
            risk_state["consecutive_losses"] = risk_state.get("consecutive_losses", 0) + 1
        elif pnl_usdt is not None and pnl_usdt >= 0:
            risk_state["consecutive_losses"] = 0
        risk_state["last_trade_result"] = float(pnl_usdt) if pnl_usdt is not None else None
        db_kv_set("risk_state", risk_state)


def manual_kill_switch(reason="Manual kill switch"):
    with risk_lock:
        risk_state["trading_halted"] = True
        risk_state["halt_reason"] = reason
        db_kv_set("risk_state", risk_state)
    send_alert(f"KILL SWITCH ACTIVATED: {reason}", "CRITICAL")


def manual_resume_trading():
    with risk_lock:
        risk_state["trading_halted"] = False
        risk_state["halt_reason"] = ""
        db_kv_set("risk_state", risk_state)
    send_alert("Trading resumed manually.", "INFO")


# =========================================================
# CORRELATION-AWARE EXPOSURE LIMIT
# =========================================================

def symbol_correlation_group(symbol):
    for group_name, members in CORRELATION_GROUPS.items():
        if symbol in members:
            return group_name
    return None


def correlated_exposure_ok(symbol, snapshot):
    group = symbol_correlation_group(symbol)
    if not group:
        return True, ""
    members = CORRELATION_GROUPS.get(group, [])
    open_in_group = sum(1 for m in members if snapshot.get(m))
    if open_in_group >= MAX_CORRELATED_POSITIONS:
        return False, f"Correlation group '{group}' already has {open_in_group}/{MAX_CORRELATED_POSITIONS} positions open"
    return True, ""


# =========================================================
# VOLATILITY-ADJUSTED POSITION SIZING
# =========================================================

def volatility_size_multiplier(atr_pct):
    if not VOL_SIZE_ADJUST or atr_pct is None or atr_pct <= 0:
        return Decimal("1")
    ratio = VOL_SIZE_BASELINE_ATR_PCT / atr_pct
    mult = max(VOL_SIZE_MIN_MULT, min(VOL_SIZE_MAX_MULT, ratio))
    return mult


def calculate_order_size(symbol, price, atr_pct=None):
    info = get_instrument(symbol)
    if info["state"] not in ("live", "preopen"):
        raise RuntimeError(f"Instrument not live: {info['state']}")
    vol_mult = volatility_size_multiplier(atr_pct)
    effective_margin = MARGIN_USDT * vol_mult
    target = effective_margin * LEVERAGE
    raw = target / (info["ctVal"] * price)
    size = floor_step(raw, info["lotSz"])
    if size < info["minSz"]:
        minimum_notional = info["minSz"] * info["ctVal"] * price
        raise RuntimeError(f"{symbol}: margin ${fmt(effective_margin,2)} cannot meet minimum contract size; minimum notional≈${fmt(minimum_notional,2)}")
    return size, size * info["ctVal"] * price, vol_mult


def get_account_config():
    return private_request("GET", "/api/v5/account/config")


def refresh_position_mode():
    global position_mode
    data = get_account_config()
    rows = data.get("data", [])
    raw = str(rows[0].get("posMode", "net")).lower() if rows else "net"
    position_mode = "long_short_mode" if raw in ("long_short_mode", "long_short") else "net"
    log(f"POSITION MODE | {raw} -> {position_mode}")


position_mode = "net"


def set_leverage(symbol):
    payload = {"instId": symbol, "lever": fmt(LEVERAGE), "mgnMode": TD_MODE}
    if position_mode == "long_short_mode":
        for side in ("long", "short"):
            p = dict(payload)
            p["posSide"] = side
            private_request("POST", "/api/v5/account/set-leverage", payload=p)
        return
    private_request("POST", "/api/v5/account/set-leverage", payload=payload)


def position_side(position):
    if position_mode == "long_short_mode":
        return "buy" if position.get("posSide") == "long" else "sell"
    return "buy" if dec(position.get("pos", "0")) > 0 else "sell"


def cap_sl_distance(side, entry, sl, tick):
    max_dist = entry * MAX_SL_DISTANCE_PCT / Decimal("100")
    if side == "buy":
        floor_price = entry - max_dist
        if sl < floor_price:
            sl = ceil_step(floor_price, tick)
    else:
        ceil_price = entry + max_dist
        if sl > ceil_price:
            sl = floor_step(ceil_price, tick)
    return sl


def calculate_initial_sl_tp(side, entry, tick):
    if side == "buy":
        sl = floor_step(entry * (Decimal("1") - SL_PERCENT / Decimal("100")), tick)
        tp = floor_step(entry * (Decimal("1") + TP_PERCENT / Decimal("100")), tick)
    else:
        sl = ceil_step(entry * (Decimal("1") + SL_PERCENT / Decimal("100")), tick)
        tp = ceil_step(entry * (Decimal("1") - TP_PERCENT / Decimal("100")), tick)
    sl = cap_sl_distance(side, entry, sl, tick)
    return sl, tp


def get_pending_algo_orders(symbol):
    return private_request("GET", "/api/v5/trade/orders-algo-pending", params={"instType": "SWAP", "instId": symbol, "ordType": "oco"})


def cancel_algo(symbol, algo_id):
    return private_request("POST", "/api/v5/trade/cancel-algos", payload=[{"instId": symbol, "algoId": str(algo_id)}])


def protection_exists(symbol, position=None):
    if position:
        for row in position.get("closeOrderAlgo", []) or []:
            if row.get("algoId") and str(row.get("closeFraction", "")) == "1":
                return True
    try:
        rows = get_pending_algo_orders(symbol).get("data", [])
        return any(r.get("algoId") for r in rows)
    except Exception:
        return False


def cancel_existing_protection(symbol, position=None):
    ids = set()
    if position:
        for row in position.get("closeOrderAlgo", []) or []:
            if row.get("algoId"):
                ids.add(str(row["algoId"]))
    try:
        for row in get_pending_algo_orders(symbol).get("data", []):
            if row.get("algoId"):
                ids.add(str(row["algoId"]))
    except Exception as exc:
        log(f"OCO LIST WARNING | {symbol} | {exc}")
    for aid in ids:
        try:
            cancel_algo(symbol, aid)
        except Exception as exc:
            log(f"OCO CANCEL WARNING | {symbol} | {aid} | {exc}")


def place_full_position_oco(symbol, side, sl_price, tp_price, tick):
    if side == "buy":
        close_side = "sell"
        sl_price = floor_step(sl_price, tick)
        tp_price = floor_step(tp_price, tick)
    else:
        close_side = "buy"
        sl_price = ceil_step(sl_price, tick)
        tp_price = ceil_step(tp_price, tick)
    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": close_side,
        "ordType": "oco",
        "reduceOnly": True,
        "closeFraction": "1",
        "tpTriggerPx": fmt(tp_price), "tpOrdPx": "-1", "tpTriggerPxType": "mark",
        "slTriggerPx": fmt(sl_price), "slOrdPx": "-1", "slTriggerPxType": "mark",
        "algoClOrdId": "p" + uuid.uuid4().hex[:30],
    }
    if position_mode == "net":
        payload["posSide"] = "net"
    else:
        payload["posSide"] = "long" if side == "buy" else "short"
    result = private_request("POST", "/api/v5/trade/order-algo", payload=payload)
    row = (result.get("data") or [{}])[0]
    if row.get("sCode") not in (None, "", "0"):
        raise RuntimeError(f"OCO rejected | {row.get('sCode')} | {row.get('sMsg')}")
    if not row.get("algoId"):
        raise RuntimeError("OCO response has no algoId")
    return result


# =========================================================
# LAYERED EMERGENCY CLOSE
# =========================================================

def _manual_reduce_only_close(symbol, position):
    side = position_side(position)
    close_side = "sell" if side == "buy" else "buy"
    size = abs(dec(position.get("pos", "0")))
    payload = {
        "instId": symbol, "tdMode": TD_MODE, "side": close_side,
        "ordType": "market", "sz": fmt(size), "reduceOnly": True,
        "clOrdId": "kill" + uuid.uuid4().hex[:22],
    }
    if position_mode == "long_short_mode":
        payload["posSide"] = position.get("posSide", "")
    return private_request("POST", "/api/v5/trade/order", payload=payload)


def emergency_close(symbol, max_attempts=3):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            payload = {"instId": symbol, "mgnMode": TD_MODE, "autoCxl": True}
            if position_mode == "long_short_mode":
                pos = get_cached_position(symbol)
                if pos:
                    payload["posSide"] = pos.get("posSide", "")
            result = private_request("POST", "/api/v5/trade/close-position", payload=payload)
            send_alert(f"Emergency close succeeded via close-position (attempt {attempt}).", "WARNING", symbol)
            return result
        except Exception as exc:
            last_exc = exc
            log(f"EMERGENCY CLOSE (close-position) FAILED | {symbol} | attempt {attempt}/{max_attempts} | {exc}")
            time.sleep(min(2 * attempt, 6))

    for attempt in range(1, max_attempts + 1):
        try:
            pos = get_cached_position(symbol) or refresh_position_snapshot().get(symbol)
            if not pos:
                send_alert(f"Emergency close: no position found on re-check, assuming already closed.", "WARNING", symbol)
                return {"status": "ALREADY_CLOSED"}
            result = _manual_reduce_only_close(symbol, pos)
            send_alert(f"Emergency close succeeded via manual reduce-only order (attempt {attempt}).", "WARNING", symbol)
            return result
        except Exception as exc:
            last_exc = exc
            log(f"EMERGENCY CLOSE (manual order) FAILED | {symbol} | attempt {attempt}/{max_attempts} | {exc}")
            time.sleep(min(2 * attempt, 6))

    with state_lock:
        state.setdefault(symbol, {})["halted"] = True
        state[symbol]["halt_reason"] = f"Emergency close failed: {last_exc}"
    send_alert(
        f"CRITICAL: emergency close FAILED on both endpoints for {symbol}. "
        f"A position may be open and UNPROTECTED. Manual intervention required NOW. Last error: {last_exc}",
        "CRITICAL", symbol,
    )
    raise RuntimeError(f"EMERGENCY CLOSE TOTAL FAILURE | {symbol} | {last_exc}")


def liquidity_guard(symbol, side, target_notional):
    bids, asks = get_orderbook(symbol, ORDERBOOK_LEVELS)
    levels = asks if side == "buy" else bids
    side_depth = sum(px * sz * get_instrument(symbol)["ctVal"] for px, sz in levels)
    return side_depth >= target_notional * MIN_SIDE_DEPTH_MULT, side_depth, levels[0][0]


def fee_buffer_ok(target_notional):
    gross = target_notional * TP_PERCENT / Decimal("100")
    estimated_round_trip = target_notional * FEE_RATE_PER_SIDE * Decimal("2")
    required = estimated_round_trip + FEE_BUFFER_USDT
    return gross >= required, gross, estimated_round_trip, required


def _safe_div(a, b):
    return a / b if b else Decimal("0")


def _confirmed(candles):
    return [x for x in candles if x.get("confirm") == "1"]


def _candle_rejection_direction(c):
    rng = max(c["high"] - c["low"], Decimal("0.00000001"))
    body = abs(c["close"] - c["open"])
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    close_pos = (c["close"] - c["low"]) / rng
    body_ratio = body / rng
    if lower >= body * Decimal("1.5") and close_pos >= Decimal("0.65") and body_ratio <= Decimal("0.65"):
        return "BULLISH"
    if upper >= body * Decimal("1.5") and close_pos <= Decimal("0.35") and body_ratio <= Decimal("0.65"):
        return "BEARISH"
    return "NONE"


def _price_zone(levels, price, tol):
    if not levels:
        return None
    pts = sorted(levels)
    clusters = []
    current = [pts[0]]
    for lv in pts[1:]:
        if lv - current[-1] <= tol:
            current.append(lv)
        else:
            clusters.append(current)
            current = [lv]
    clusters.append(current)

    best_rep = None
    best_dist = None
    for cl in clusters:
        zone_low, zone_high = min(cl), max(cl)
        if price < zone_low:
            rep = zone_low
        elif price > zone_high:
            rep = zone_high
        else:
            rep = price
        d = abs(price - rep)
        if best_dist is None or d < best_dist:
            best_dist = d
            best_rep = rep
    return best_rep


def price_action_structure(candles):
    """Price-action-first sequence using closed candles only.

    Gate: pa_complete_buy/sell = SR and SWEEP and RECLAIM and BOS and
    (BOS_RECENT or RETEST_ok). Retest is downstream of BOS by construction,
    so accepting a valid retest as proof of freshness (instead of requiring
    BOS itself to still be within PA_BOS_MAX_AGE_BARS) removes the mismatch
    between the displayed pa_score (which counts retest as its 5th stage)
    and the actual trade-enabling gate, without weakening the 4 mandatory
    core stages.
    """
    c = _confirmed(candles)
    empty = {
        "sr": "NONE", "candle_rejection": "NONE", "bos": "NONE",
        "sweep": "NONE", "order_block": "NONE", "retest": "NONE",
        "sweep_reclaim": "NONE", "pa_score": 0, "pa_buy": 0, "pa_sell": 0,
        "bullish_setup": False, "bearish_setup": False,
        "pa_complete_buy": False, "pa_complete_sell": False,
        "sr_level": None, "broken_level": None, "ob_low": None, "ob_high": None,
        "gate_stages_buy": [], "gate_stages_sell": [],
    }
    if len(c) < 50:
        return empty

    last = c[-1]
    price = last["close"]
    av = atr(c) or price * Decimal("0.002")
    sr_tol = max(av * SR_ATR_MULT, price * (SR_PCT / Decimal("100")))
    sweep_buffer = max(av * PA_SWEEP_ATR_BUFFER, price * Decimal("0.0005"))

    level_end = len(c) - 2
    level_start = max(2, level_end - PA_LOOKBACK)
    swing_highs, swing_lows = [], []
    for j in range(level_start, level_end - 2):
        if c[j]["high"] >= max(c[j-2]["high"], c[j-1]["high"], c[j+1]["high"], c[j+2]["high"]):
            swing_highs.append(c[j]["high"])
        if c[j]["low"] <= min(c[j-2]["low"], c[j-1]["low"], c[j+1]["low"], c[j+2]["low"]):
            swing_lows.append(c[j]["low"])

    support_candidates = [x for x in swing_lows if x <= price + sr_tol]
    resistance_candidates = [x for x in swing_highs if x >= price - sr_tol]
    zone_support = _price_zone(support_candidates, price, sr_tol)
    zone_resistance = _price_zone(resistance_candidates, price, sr_tol)
    support = zone_support if zone_support is not None else min(x["low"] for x in c[max(0, level_end-12):level_end])
    resistance = zone_resistance if zone_resistance is not None else max(x["high"] for x in c[max(0, level_end-12):level_end])

    dist_support = abs(price - support)
    dist_resistance = abs(resistance - price)
    bull_sr_ok = dist_support <= sr_tol
    bear_sr_ok = dist_resistance <= sr_tol
    if bull_sr_ok and not bear_sr_ok:
        sr = "SUPPORT"
    elif bear_sr_ok and not bull_sr_ok:
        sr = "RESISTANCE"
    elif bull_sr_ok and bear_sr_ok:
        sr = "BOTH"
    else:
        sr = "NONE"

    seq_start = max(level_start + 2, len(c) - PA_LOOKBACK)
    bull_sweep_idx = bull_bos_idx = bull_retest_idx = None
    bear_sweep_idx = bear_bos_idx = bear_retest_idx = None
    bull_reclaim_idx = bear_reclaim_idx = None
    bull_broken = bear_broken = None

    if support is not None:
        for j in range(seq_start, len(c)):
            cj = c[j]
            if cj["low"] <= support + sweep_buffer and cj["low"] < support and cj["close"] > support:
                bull_sweep_idx = j
        if bull_sweep_idx is not None:
            bull_reclaim_idx = bull_sweep_idx
            ref = c[max(seq_start, bull_sweep_idx - 5):bull_sweep_idx]
            if len(ref) >= 3:
                bull_broken = max(x["high"] for x in ref)
                for j in range(bull_sweep_idx + 1, len(c)):
                    if c[j]["close"] > bull_broken:
                        bull_bos_idx = j
                        break

    if resistance is not None:
        for j in range(seq_start, len(c)):
            cj = c[j]
            if cj["high"] >= resistance - sweep_buffer and cj["high"] > resistance and cj["close"] < resistance:
                bear_sweep_idx = j
        if bear_sweep_idx is not None:
            bear_reclaim_idx = bear_sweep_idx
            ref = c[max(seq_start, bear_sweep_idx - 5):bear_sweep_idx]
            if len(ref) >= 3:
                bear_broken = min(x["low"] for x in ref)
                for j in range(bear_sweep_idx + 1, len(c)):
                    if c[j]["close"] < bear_broken:
                        bear_bos_idx = j
                        break

    bull_candle_idx = bear_candle_idx = None
    if bull_reclaim_idx is not None:
        end_excl = (bull_bos_idx + 1) if bull_bos_idx is not None else len(c)
        for j in range(bull_reclaim_idx, end_excl):
            if _candle_rejection_direction(c[j]) == "BULLISH":
                bull_candle_idx = j
    if bear_reclaim_idx is not None:
        end_excl = (bear_bos_idx + 1) if bear_bos_idx is not None else len(c)
        for j in range(bear_reclaim_idx, end_excl):
            if _candle_rejection_direction(c[j]) == "BEARISH":
                bear_candle_idx = j

    ob = "NONE"
    ob_low = ob_high = None
    if bull_bos_idx is not None:
        for j in range(bull_bos_idx - 1, max(bull_reclaim_idx or 0, seq_start), -1):
            if c[j]["close"] < c[j]["open"]:
                ob = "BULLISH"
                ob_low, ob_high = c[j]["low"], c[j]["high"]
                break
    elif bear_bos_idx is not None:
        for j in range(bear_bos_idx - 1, max(bear_reclaim_idx or 0, seq_start), -1):
            if c[j]["close"] > c[j]["open"]:
                ob = "BEARISH"
                ob_low, ob_high = c[j]["low"], c[j]["high"]
                break

    if bull_bos_idx is not None and bull_broken is not None:
        end = min(len(c), bull_bos_idx + PA_RETEST_MAX_BARS + 1)
        for j in range(bull_bos_idx + 1, end):
            touches_structure = c[j]["low"] <= bull_broken + sr_tol and c[j]["close"] > bull_broken
            touches_ob = ob == "BULLISH" and c[j]["low"] <= ob_high and c[j]["high"] >= ob_low and c[j]["close"] > c[j]["open"]
            if touches_structure or touches_ob:
                bull_retest_idx = j
                break
    if bear_bos_idx is not None and bear_broken is not None:
        end = min(len(c), bear_bos_idx + PA_RETEST_MAX_BARS + 1)
        for j in range(bear_bos_idx + 1, end):
            touches_structure = c[j]["high"] >= bear_broken - sr_tol and c[j]["close"] < bear_broken
            touches_ob = ob == "BEARISH" and c[j]["high"] >= ob_low and c[j]["low"] <= ob_high and c[j]["close"] < c[j]["open"]
            if touches_structure or touches_ob:
                bear_retest_idx = j
                break

    sweep = "BULLISH" if bull_sweep_idx is not None else "BEARISH" if bear_sweep_idx is not None else "NONE"
    sweep_reclaim = "BULLISH" if bull_reclaim_idx is not None else "BEARISH" if bear_reclaim_idx is not None else "NONE"
    bos = "BULLISH" if bull_bos_idx is not None else "BEARISH" if bear_bos_idx is not None else "NONE"
    candle_rejection = "BULLISH" if bull_candle_idx is not None and (bear_candle_idx is None or bull_candle_idx >= bear_candle_idx) else "BEARISH" if bear_candle_idx is not None else "NONE"
    retest = "BULLISH" if bull_retest_idx is not None and (bear_retest_idx is None or bull_retest_idx >= bear_retest_idx) else "BEARISH" if bear_retest_idx is not None else "NONE"

    bull_retest_recent = bull_retest_idx is not None and len(c) - 1 - bull_retest_idx <= PA_MAX_AGE_BARS
    bear_retest_recent = bear_retest_idx is not None and len(c) - 1 - bear_retest_idx <= PA_MAX_AGE_BARS

    bull_sweep_ok = bull_sweep_idx is not None
    bull_reclaim_ok = bull_reclaim_idx is not None and c[bull_reclaim_idx]["close"] > support
    bull_bos_ok = bull_bos_idx is not None and bull_reclaim_idx is not None and bull_bos_idx > bull_reclaim_idx
    bull_retest_ok = bull_retest_recent and bull_retest_idx is not None and bull_bos_idx is not None and bull_retest_idx > bull_bos_idx

    bear_sweep_ok = bear_sweep_idx is not None
    bear_reclaim_ok = bear_reclaim_idx is not None and c[bear_reclaim_idx]["close"] < resistance
    bear_bos_ok = bear_bos_idx is not None and bear_reclaim_idx is not None and bear_bos_idx > bear_reclaim_idx
    bear_retest_ok = bear_retest_recent and bear_retest_idx is not None and bear_bos_idx is not None and bear_retest_idx > bear_bos_idx

    bull_stage = [bull_sr_ok, bull_sweep_ok, bull_reclaim_ok, bull_bos_ok, bull_retest_ok]
    bear_stage = [bear_sr_ok, bear_sweep_ok, bear_reclaim_ok, bear_bos_ok, bear_retest_ok]
    pa_buy = sum(bull_stage)
    pa_sell = sum(bear_stage)
    PA_STAGE_COUNT = 5

    bull_bos_recent = bull_bos_idx is not None and len(c) - 1 - bull_bos_idx <= PA_BOS_MAX_AGE_BARS
    bear_bos_recent = bear_bos_idx is not None and len(c) - 1 - bear_bos_idx <= PA_BOS_MAX_AGE_BARS

    pa_complete_buy = bull_sr_ok and bull_sweep_ok and bull_reclaim_ok and bull_bos_ok and (bull_bos_recent or bull_retest_ok)
    pa_complete_sell = bear_sr_ok and bear_sweep_ok and bear_reclaim_ok and bear_bos_ok and (bear_bos_recent or bear_retest_ok)

    bull_candle_confirm = bull_candle_idx is not None
    bear_candle_confirm = bear_candle_idx is not None
    bull_ob_confirm = ob == "BULLISH"
    bear_ob_confirm = ob == "BEARISH"

    bullish_setup = pa_complete_buy
    bearish_setup = pa_complete_sell

    gate_stages_buy = [
        {"name": "SR", "ok": bool(bull_sr_ok)},
        {"name": "SWEEP", "ok": bool(bull_sweep_ok)},
        {"name": "RECLAIM", "ok": bool(bull_reclaim_ok)},
        {"name": "BOS", "ok": bool(bull_bos_ok), "recent": bool(bull_bos_recent)},
        {"name": "RETEST", "ok": bool(bull_retest_ok)},
    ]
    gate_stages_sell = [
        {"name": "SR", "ok": bool(bear_sr_ok)},
        {"name": "SWEEP", "ok": bool(bear_sweep_ok)},
        {"name": "RECLAIM", "ok": bool(bear_reclaim_ok)},
        {"name": "BOS", "ok": bool(bear_bos_ok), "recent": bool(bear_bos_recent)},
        {"name": "RETEST", "ok": bool(bear_retest_ok)},
    ]

    return {
        "sr": sr, "candle_rejection": candle_rejection, "bos": bos, "sweep": sweep,
        "sweep_reclaim": sweep_reclaim, "order_block": ob, "retest": retest,
        "pa_score": max(pa_buy, pa_sell), "pa_stage_count": PA_STAGE_COUNT,
        "pa_buy": pa_buy, "pa_sell": pa_sell,
        "pa_sequence_buy": "COMPLETE" if pa_complete_buy else "INCOMPLETE",
        "pa_sequence_sell": "COMPLETE" if pa_complete_sell else "INCOMPLETE",
        "pa_complete_buy": pa_complete_buy, "pa_complete_sell": pa_complete_sell,
        "bull_sr_ok": bull_sr_ok, "bear_sr_ok": bear_sr_ok,
        "bull_bos_recent": bull_bos_recent, "bear_bos_recent": bear_bos_recent,
        "bull_retest_recent": bull_retest_recent, "bear_retest_recent": bear_retest_recent,
        "candle_confirm_buy": bull_candle_confirm, "candle_confirm_sell": bear_candle_confirm,
        "ob_confirm_buy": bull_ob_confirm, "ob_confirm_sell": bear_ob_confirm,
        "bullish_setup": bullish_setup, "bearish_setup": bearish_setup,
        "sr_level": support if bullish_setup else resistance if bearish_setup else None,
        "broken_level": bull_broken if bullish_setup else bear_broken if bearish_setup else None,
        "ob_low": ob_low, "ob_high": ob_high,
        "gate_stages_buy": gate_stages_buy, "gate_stages_sell": gate_stages_sell,
        "bull_sweep_ok": bull_sweep_ok, "bull_reclaim_ok": bull_reclaim_ok, "bull_bos_ok": bull_bos_ok, "bull_retest_ok": bull_retest_ok,
        "bear_sweep_ok": bear_sweep_ok, "bear_reclaim_ok": bear_reclaim_ok, "bear_bos_ok": bear_bos_ok, "bear_retest_ok": bear_retest_ok,
    }


def analyze(symbol, oi_change=None):
    info = session_info()
    cs = _confirmed(get_candles(symbol, BAR, 180))
    trend_cs = _confirmed(get_candles(symbol, TREND_BAR, 80))
    empty = {
        "signal": "NONE", "score": 0, "max_score": 6, "required_score": 99,
        "session": info["name"], "priority_session": info["priority"],
        "block_reason": "Not enough confirmed candles yet", "gate_stages_buy": [], "gate_stages_sell": [],
        "trend15": "flat",
    }
    if len(cs) < 50 or len(trend_cs) < 22:
        return {**empty, "reason": "Not enough confirmed candles"}

    values = [x["close"] for x in cs]
    i = len(values) - 1
    e20 = ema(values, 20)
    r14 = rsi(values, 14)
    atr_v = atr(cs)
    adx_v = adx(cs)
    ml, ms = macd(values)
    vw = session_vwap(cs)
    trend15 = get_trend_from_candles(trend_cs)
    funding = get_funding_snapshot(symbol)
    if any(x is None for x in (e20[i], r14[i], atr_v, ml[i], ms[i], vw)):
        return {**empty, "reason": "Indicator unavailable", "block_reason": "Indicator data unavailable this cycle", "trend15": trend15}

    avg_vol = sum(x["volume"] for x in cs[-21:-1]) / Decimal("20")
    vol_ratio = cs[i]["volume"] / avg_vol if avg_vol else Decimal("0")
    atr_pct = atr_v / values[i] * Decimal("100")
    pa = price_action_structure(cs)

    trend_vote = "buy" if trend15 == "bull" else "sell" if trend15 == "bear" else "none"
    ema_vwap_vote = "buy" if values[i] > e20[i] and values[i] > vw else "sell" if values[i] < e20[i] and values[i] < vw else "none"
    macd_vote = "buy" if ml[i] > ms[i] else "sell" if ml[i] < ms[i] else "none"
    strength_vote = "buy" if adx_v >= ADX_MIN and vol_ratio >= VOLUME_MULT and values[i] > e20[i] else \
                    "sell" if adx_v >= ADX_MIN and vol_ratio >= VOLUME_MULT and values[i] < e20[i] else "none"
    base_buy = sum(x == "buy" for x in (trend_vote, ema_vwap_vote, macd_vote, strength_vote))
    base_sell = sum(x == "sell" for x in (trend_vote, ema_vwap_vote, macd_vote, strength_vote))

    def momentum_alignment(direction):
        if direction == "buy":
            votes = [values[i] > e20[i], values[i] > vw, ml[i] > ms[i]]
        else:
            votes = [values[i] < e20[i], values[i] < vw, ml[i] < ms[i]]
        return sum(votes) >= 2

    flow_ok = sum([
        adx_v >= ADX_MIN,
        vol_ratio >= VOLUME_MULT,
        atr_pct >= ATR_MIN_PCT,
    ]) >= 2
    buy_points = [
        trend15 == "bull",
        pa.get("bull_sr_ok", False) and pa.get("bull_sweep_ok", False) and pa.get("bull_reclaim_ok", False),
        pa.get("bull_bos_ok", False),
        pa.get("bull_retest_ok", False),
        flow_ok,
        momentum_alignment("buy"),
    ]
    sell_points = [
        trend15 == "bear",
        pa.get("bear_sr_ok", False) and pa.get("bear_sweep_ok", False) and pa.get("bear_reclaim_ok", False),
        pa.get("bear_bos_ok", False),
        pa.get("bear_retest_ok", False),
        flow_ok,
        momentum_alignment("sell"),
    ]
    buy = sum(buy_points)
    sell = sum(sell_points)
    score = max(buy, sell)
    MAX_SCORE = 6

    filters = {
        "SESSION": info["active"],
        "OI": True if oi_change is None else oi_change >= -OI_UNWIND_PCT,
    }
    configured_required = max(MIN_SCORE, MAJOR_MIN_SCORE, NON_PRIORITY_MIN_SCORE)
    required = min(configured_required, MAX_SCORE - 1)
    if configured_required != required:
        log(f"SCORE_CONFIG_CLAMP configured={configured_required} -> required={required} (hard cap 5/6)")

    if pa["pa_complete_buy"] and not pa["pa_complete_sell"]:
        direction = "buy"
    elif pa["pa_complete_sell"] and not pa["pa_complete_buy"]:
        direction = "sell"
    elif pa["pa_complete_buy"] and pa["pa_complete_sell"]:
        direction = "buy" if buy >= sell else "sell"
    else:
        direction = "none"

    signal = "NONE"
    if direction == "buy" and buy >= required:
        signal = "BUY"
    elif direction == "sell" and sell >= required:
        signal = "SELL"

    veto = None
    if trend15 == "bull" and signal == "SELL":
        signal = "NONE"; veto = "15m trend is bullish, vetoed SELL signal"
    if trend15 == "bear" and signal == "BUY":
        signal = "NONE"; veto = "15m trend is bearish, vetoed BUY signal"
    if signal == "BUY" and ml[i] < ms[i]:
        signal = "NONE"; veto = "MACD below signal line, vetoed BUY signal"
    if signal == "SELL" and ml[i] > ms[i]:
        signal = "NONE"; veto = "MACD above signal line, vetoed SELL signal"

    failed = [k for k, v in filters.items() if not v]
    if signal != "NONE" and failed:
        signal = "NONE"

    vol_mult_preview = volatility_size_multiplier(atr_pct)
    target = MARGIN_USDT * vol_mult_preview * LEVERAGE
    fee_ok, gross, fees, required_gross = fee_buffer_ok(target)
    if signal != "NONE" and not fee_ok:
        signal = "NONE"

    block_reason = None
    if signal in ("BUY", "SELL"):
        block_reason = None
    elif not info["active"]:
        block_reason = f"Session closed ({info['name']}) — outside configured trading windows"
    elif "OI" in failed:
        block_reason = "Open interest unwind filter blocked entry (possible large deleveraging)"
    elif direction == "none":
        buy_missing = [s["name"] for s in pa.get("gate_stages_buy", []) if not s["ok"]]
        sell_missing = [s["name"] for s in pa.get("gate_stages_sell", []) if not s["ok"]]
        if pa["pa_buy"] >= pa["pa_sell"]:
            block_reason = f"PA gate incomplete for BUY — missing: {', '.join(buy_missing) if buy_missing else 'timing/freshness'}"
        else:
            block_reason = f"PA gate incomplete for SELL — missing: {', '.join(sell_missing) if sell_missing else 'timing/freshness'}"
    elif veto:
        block_reason = veto
    elif not fee_ok:
        block_reason = f"Fee buffer not covered (gross {fmt(gross,4)} < required {fmt(required_gross,4)})"
    elif score < required:
        block_reason = f"Confluence score too low ({score}/{MAX_SCORE}, need {required})"
    else:
        block_reason = "Blocked (see reason string for details)"

    oi_part = f"OI={fmt(oi_change,3)}%" if oi_change is not None else "OI=warming"
    reason = (
        f"SR={pa['sr']} | CANDLE={pa['candle_rejection']} | BOS={pa['bos']} | "
        f"SWEEP={pa['sweep']} | RECLAIM={pa['sweep_reclaim']} | OB={pa['order_block']} | "
        f"RETEST={pa['retest']} | PA={pa['pa_score']}/{pa['pa_stage_count']} | PA_COMPLETE_BUY={pa['pa_complete_buy']} | "
        f"PA_COMPLETE_SELL={pa['pa_complete_sell']} | PTS_BUY={buy}/6 PTS_SELL={sell}/6 | "
        f"ADX={fmt(adx_v,2)} | VOL={fmt(vol_ratio,2)}x | ATR={fmt(atr_pct,3)}% | VOL_SIZE_MULT={fmt(vol_mult_preview,2)} | "
        f"session={info['name']} | priority={info['priority']} | {oi_part}"
    )
    if failed:
        reason += " | FILTER_FAIL=" + ",".join(failed)
    if not fee_ok:
        reason += f" | FEE_FAIL gross={fmt(gross,4)} required={fmt(required_gross,4)}"
    if direction == "none":
        reason += " | PA_VETO=CORE_PA_NEEDS_SR_SWEEP_RECLAIM_AND_(FRESH_BOS_OR_RETEST)"

    return {
        "signal": signal, "score": score, "buy": buy, "sell": sell, "max_score": MAX_SCORE,
        "required_score": required,
        "votes": {"TREND15": trend_vote, "EMA_VWAP": ema_vwap_vote, "MACD": macd_vote, "STRENGTH": strength_vote, "POINTS_BUY": buy_points, "POINTS_SELL": sell_points},
        "filters": filters, "entry": values[i], "rsi14": r14[i], "ema20": e20[i],
        "macd": ml[i], "macd_signal": ms[i], "vwap": vw, "adx": adx_v,
        "atr_pct": atr_pct, "volume_ratio": vol_ratio, "trend15": trend15,
        "funding_pct": funding.get("current"), "funding_threshold": funding.get("threshold"),
        "oi_change_pct": oi_change, "session": info["name"], "priority_session": info["priority"],
        "sr": pa["sr"], "candle_rejection": pa["candle_rejection"], "bos": pa["bos"],
        "sweep": pa["sweep"], "sweep_reclaim": pa["sweep_reclaim"], "order_block": pa["order_block"],
        "retest": pa["retest"], "pa_score": pa["pa_score"], "pa_buy": pa["pa_buy"],
        "pa_sell": pa["pa_sell"], "pa_sequence_buy": pa.get("pa_sequence_buy"),
        "pa_sequence_sell": pa.get("pa_sequence_sell"),
        "pa_complete_buy": pa.get("pa_complete_buy"), "pa_complete_sell": pa.get("pa_complete_sell"),
        "bull_sr_ok": pa.get("bull_sr_ok"), "bear_sr_ok": pa.get("bear_sr_ok"),
        "bull_bos_recent": pa.get("bull_bos_recent"), "bear_bos_recent": pa.get("bear_bos_recent"),
        "bull_retest_recent": pa.get("bull_retest_recent"), "bear_retest_recent": pa.get("bear_retest_recent"),
        "candle_confirm_buy": pa.get("candle_confirm_buy"),
        "candle_confirm_sell": pa.get("candle_confirm_sell"), "ob_confirm_buy": pa.get("ob_confirm_buy"),
        "ob_confirm_sell": pa.get("ob_confirm_sell"), "base_score": max(base_buy, base_sell),
        "fee_buffer_ok": fee_ok, "fee_buffer_usdt": FEE_BUFFER_USDT, "vol_size_mult": vol_mult_preview,
        "gate_stages_buy": pa.get("gate_stages_buy", []), "gate_stages_sell": pa.get("gate_stages_sell", []),
        "block_reason": block_reason,
        "reason": reason,
    }


def place_order(symbol, analysis, snapshot):
    if not AUTO_TRADE:
        return {"status": "BLOCKED", "reason": "AUTO_TRADE=false"}
    if not DEMO and not ALLOW_LIVE:
        return {"status": "BLOCKED", "reason": "LIVE trading disabled; set ALLOW_LIVE=true explicitly"}
    if analysis.get("signal") not in ("BUY", "SELL"):
        return {"status": "NO_TRADE", "reason": "Signal not approved"}

    ok, reason = risk_check_circuit_breakers()
    if not ok:
        return {"status": "BLOCKED", "reason": f"Circuit breaker: {reason}"}

    corr_ok, corr_reason = correlated_exposure_ok(symbol, snapshot)
    if not corr_ok:
        return {"status": "BLOCKED", "reason": corr_reason}

    required_pa = "pa_complete_buy" if analysis["signal"] == "BUY" else "pa_complete_sell"
    if not analysis.get(required_pa, False):
        return {"status": "BLOCKED", "reason": f"PA gate failed at execution boundary: {required_pa}=False"}

    try:
        fresh_analysis = analyze(symbol, analysis.get("oi_change_pct"))
        fresh_pa = "pa_complete_buy" if analysis["signal"] == "BUY" else "pa_complete_sell"
        if fresh_analysis.get("signal") != analysis["signal"] or not fresh_analysis.get(fresh_pa, False):
            return {"status": "BLOCKED", "reason": f"Fresh PA revalidation failed: signal={fresh_analysis.get('signal')} {fresh_pa}={fresh_analysis.get(fresh_pa)}"}
    except Exception as exc:
        return {"status": "BLOCKED", "reason": f"Fresh PA revalidation error: {exc}"}

    if snapshot.get(symbol):
        return {"status": "BLOCKED", "reason": "Existing position"}

    with state_lock:
        if state.get(symbol, {}).get("halted"):
            return {"status": "BLOCKED", "reason": f"Symbol halted: {state[symbol].get('halt_reason')}"}

    with order_lock:
        fresh = refresh_position_snapshot()
        if fresh.get(symbol):
            return {"status": "BLOCKED", "reason": "Position appeared before order"}
        current_exposure = total_open_notional(fresh)
        atr_pct_for_sizing = analysis.get("atr_pct")
        target_notional_preview = MARGIN_USDT * volatility_size_multiplier(atr_pct_for_sizing) * LEVERAGE
        if current_exposure + target_notional_preview > MAX_TOTAL_NOTIONAL_USDT:
            return {"status": "BLOCKED", "reason": f"Exposure cap ${fmt(current_exposure,2)} + ${fmt(target_notional_preview,2)} > ${fmt(MAX_TOTAL_NOTIONAL_USDT,2)}"}

        side = "buy" if analysis["signal"] == "BUY" else "sell"
        ticker_price = get_ticker(symbol)
        info = get_instrument(symbol)
        size, actual_notional, vol_mult = calculate_order_size(symbol, ticker_price, atr_pct_for_sizing)
        ok_depth, depth, best = liquidity_guard(symbol, side, actual_notional)
        if not ok_depth:
            return {"status": "BLOCKED", "reason": f"Side liquidity too low: ${fmt(depth,2)} depth"}

        set_leverage(symbol)
        payload = {"instId": symbol, "tdMode": TD_MODE, "side": side, "sz": fmt(size), "clOrdId": "bot" + uuid.uuid4().hex[:24]}
        if position_mode == "long_short_mode":
            payload["posSide"] = "long" if side == "buy" else "short"

        if ENTRY_ORDER_TYPE == "ioc":
            fresh_bids, fresh_asks = get_orderbook(symbol, 1)
            fresh_best = fresh_asks[0][0] if side == "buy" else fresh_bids[0][0]
            slip = MAX_ENTRY_SLIPPAGE_PCT / Decimal("100")
            px = fresh_best * (Decimal("1") + slip) if side == "buy" else fresh_best * (Decimal("1") - slip)
            px = floor_step(px, info["tickSz"]) if side == "buy" else ceil_step(px, info["tickSz"])
            payload["ordType"] = "ioc"
            payload["px"] = fmt(px)
            log(f"IOC PRICE | {symbol} | {side} | ref_best={fmt(fresh_best)} | slip={MAX_ENTRY_SLIPPAGE_PCT}% | limit_px={fmt(px)} | vol_mult={fmt(vol_mult,2)}")
        else:
            payload["ordType"] = "market"

        log(f"ORDER SUBMIT | {symbol} | {side.upper()} | ${fmt(actual_notional,2)} | {ENTRY_ORDER_TYPE} | exposure=${fmt(current_exposure,2)} | vol_mult={fmt(vol_mult,2)}")
        result = private_request("POST", "/api/v5/trade/order", payload=payload)
        row = (result.get("data") or [{}])[0]
        if row.get("sCode") not in (None, "", "0"):
            raise RuntimeError(f"ORDER REJECTED | {row.get('sCode')} | {row.get('sMsg')}")
        ord_id = row.get("ordId", "")

        order_state = None
        for _ in range(8):
            time.sleep(1)
            try:
                od = private_request("GET", "/api/v5/trade/order", params={"instId": symbol, "ordId": ord_id})
                rows = od.get("data", [])
                if rows:
                    order_state = rows[0].get("state")
                    if order_state in ("filled", "partially_filled", "canceled"):
                        break
            except Exception as exc:
                log(f"ORDER STATUS CHECK WARNING | {symbol} | {exc}")

        filled = None
        if order_state in ("filled", "partially_filled"):
            snap2 = refresh_position_snapshot()
            filled = snap2.get(symbol)
        if not filled:
            snap2 = refresh_position_snapshot()
            filled = snap2.get(symbol)
        if not filled:
            return {"status": "NOT_FILLED", "reason": f"IOC/entry did not create a position (order_state={order_state})", "ordId": ord_id}

        entry = dec(filled.get("avgPx") or ticker_price)
        sl, tp = calculate_initial_sl_tp(side, entry, info["tickSz"])
        cancel_existing_protection(symbol, filled)
        last_error = None
        for attempt in range(1, 4):
            try:
                protection = place_full_position_oco(symbol, side, sl, tp, info["tickSz"])
                time.sleep(1)
                if not protection_exists(symbol, filled):
                    raise RuntimeError("OCO submitted but protection not verified")
                with state_lock:
                    state.setdefault(symbol, {}).update({
                        "entry_price": entry, "current_sl": sl, "current_tp": tp,
                        "position_size": dec(filled.get("pos", "0")), "protection": "ACTIVE",
                        "protection_algo": protection, "step_level": 0,
                    })
                db_record_trade_open(symbol, side, entry, sl, tp, size, actual_notional)
                send_alert(f"Opened {side.upper()} {symbol} @ {fmt(entry)} | SL {fmt(sl)} TP {fmt(tp)} | notional ${fmt(actual_notional,2)}", "INFO", symbol)
                return {"status": "ORDER_AND_PROTECTION_ACTIVE", "symbol": symbol, "side": side, "size": fmt(size), "entry": fmt(entry), "sl": fmt(sl), "tp": fmt(tp), "actual_notional": fmt(actual_notional,2), "ordId": row.get("ordId", ""), "vol_mult": fmt(vol_mult,2)}
            except Exception as exc:
                last_error = exc
                log(f"PROTECTION RETRY | {symbol} | {attempt}/3 | {exc}")
                if attempt < 3:
                    time.sleep(PROTECTION_RETRY_SECONDS)

        send_alert(f"Protection failed 3x for {symbol} after entry. Triggering layered emergency close.", "CRITICAL", symbol)
        try:
            emergency_close(symbol)
            return {"status": "EMERGENCY_CLOSED", "reason": "Protection failed", "error": str(last_error)}
        except Exception as close_exc:
            with state_lock:
                state.setdefault(symbol, {})["halted"] = True
                state[symbol]["halt_reason"] = f"protection+emergency_close failed: {close_exc}"
            return {"status": "CRITICAL_UNPROTECTED", "protection_error": str(last_error), "close_error": str(close_exc)}


def manage_position(symbol, position):
    if not position:
        return
    avg = dec(position.get("avgPx", "0"))
    if avg <= 0:
        return
    side = position_side(position)
    price = get_mark_price(symbol)
    info = get_instrument(symbol)
    tick = info["tickSz"]
    profit = (price - avg) / avg * Decimal("100") if side == "buy" else (avg - price) / avg * Decimal("100")

    with state_lock:
        saved = state.get(symbol, {}).copy()
    current_sl = saved.get("current_sl")
    current_tp = saved.get("current_tp")
    step_level = saved.get("step_level", 0)

    protection_active = protection_exists(symbol, position)
    if current_sl is None or current_tp is None:
        current_sl, current_tp = calculate_initial_sl_tp(side, avg, tick)
        step_level = 0
        if protection_active:
            for algo in position.get("closeOrderAlgo", []) or []:
                if str(algo.get("closeFraction", "")) == "1":
                    if algo.get("slTriggerPx"):
                        current_sl = dec(algo["slTriggerPx"])
                    if algo.get("tpTriggerPx"):
                        current_tp = dec(algo["tpTriggerPx"])
                    break
            current_sl = cap_sl_distance(side, avg, current_sl, tick)
            try:
                if side == "buy" and current_tp > avg:
                    ratio = (current_tp / avg - Decimal("1")) * Decimal("100") / STEP_TRIGGER_PCT
                elif side == "sell" and current_tp < avg:
                    ratio = (Decimal("1") - current_tp / avg) * Decimal("100") / STEP_TRIGGER_PCT
                else:
                    ratio = Decimal("0")
                inferred_step = max(0, int(ratio.to_integral_value(rounding=ROUND_DOWN)) - 1)
                step_level = inferred_step
                log(f"RESTART RECOVERY | {symbol} | reconstructed step_level={step_level} from exchange TP={fmt(current_tp)}")
            except Exception as exc:
                log(f"RESTART RECOVERY WARNING | {symbol} | could not infer step_level | {exc}")

    new_sl = current_sl
    new_tp = current_tp

    if profit >= BREAK_EVEN_TRIGGER_PCT:
        be = avg * (Decimal("1") + BREAK_EVEN_OFFSET_PCT / Decimal("100")) if side == "buy" else avg * (Decimal("1") - BREAK_EVEN_OFFSET_PCT / Decimal("100"))
        be = floor_step(be, tick) if side == "buy" else ceil_step(be, tick)
        new_sl = max(new_sl, be) if side == "buy" else min(new_sl, be)

    if profit >= TRAIL_START_PCT:
        tr = price * (Decimal("1") - TRAIL_DISTANCE_PCT / Decimal("100")) if side == "buy" else price * (Decimal("1") + TRAIL_DISTANCE_PCT / Decimal("100"))
        tr = floor_step(tr, tick) if side == "buy" else ceil_step(tr, tick)
        new_sl = max(new_sl, tr) if side == "buy" else min(new_sl, tr)

    new_sl = cap_sl_distance(side, avg, new_sl, tick)

    achieved_step = int((profit / STEP_TRIGGER_PCT).to_integral_value(rounding=ROUND_DOWN)) if profit > 0 else 0
    if achieved_step > step_level:
        step_level = achieved_step
        if side == "buy":
            candidate_tp = avg * (Decimal("1") + Decimal(step_level + 1) * STEP_TRIGGER_PCT / Decimal("100"))
            new_tp = max(new_tp, floor_step(candidate_tp, tick))
        else:
            candidate_tp = avg * (Decimal("1") - Decimal(step_level + 1) * STEP_TRIGGER_PCT / Decimal("100"))
            new_tp = min(new_tp, ceil_step(candidate_tp, tick))

    if not protection_active:
        log(f"PROTECTION MISSING | {symbol} | restoring")
        send_alert(f"Protection missing on live position, restoring OCO.", "WARNING", symbol)
        try:
            cancel_existing_protection(symbol, position)
            place_full_position_oco(symbol, side, new_sl, new_tp, tick)
            time.sleep(1)
            if not protection_exists(symbol, position):
                raise RuntimeError("Protection restoration not verified")
            protection_active = True
        except Exception as exc:
            log(f"PROTECTION RESTORE FAILED | {symbol} | {exc}")
            send_alert(f"Protection restore FAILED. Triggering layered emergency close. Error: {exc}", "CRITICAL", symbol)
            try:
                emergency_close(symbol)
            except Exception as close_exc:
                log(f"CRITICAL EMERGENCY CLOSE FAILED | {symbol} | {close_exc}")
            return

    sl_changed = abs(new_sl - current_sl) >= tick
    tp_changed = abs(new_tp - current_tp) >= tick
    changed = sl_changed or tp_changed
    if changed:
        try:
            cancel_existing_protection(symbol, position)
            place_full_position_oco(symbol, side, new_sl, new_tp, tick)
            time.sleep(1)
            if not protection_exists(symbol, position):
                raise RuntimeError("new OCO verification failed")
            current_sl, current_tp = new_sl, new_tp
            log(f"OCO UPDATED | {symbol} | SL {fmt(current_sl)} -> {fmt(new_sl)} | TP {fmt(current_tp)} -> {fmt(new_tp)} | step={step_level}")
        except Exception as exc:
            log(f"STEP OCO UPDATE FAILED | {symbol} | {exc}")
            try:
                place_full_position_oco(symbol, side, current_sl, current_tp, tick)
            except Exception as restore_exc:
                log(f"CRITICAL OCO RESTORE FAILED | {symbol} | {restore_exc}")
                send_alert(f"OCO restore FAILED after failed update. Triggering layered emergency close. Error: {restore_exc}", "CRITICAL", symbol)
                try:
                    emergency_close(symbol)
                except Exception as close_exc:
                    log(f"CRITICAL EMERGENCY CLOSE FAILED | {symbol} | {close_exc}")
                return

    with state_lock:
        state.setdefault(symbol, {}).update({
            "entry_price": avg, "mark_price": price, "profit_pct": profit,
            "current_sl": current_sl, "current_tp": current_tp,
            "protection": "ACTIVE", "position_size": dec(position.get("pos", "0")),
            "step_level": step_level,
        })


def detect_and_record_closed_positions(prev_snapshot, new_snapshot):
    for symbol, pos in prev_snapshot.items():
        if symbol in new_snapshot:
            continue
        try:
            avg = dec(pos.get("avgPx", "0"))
            side = position_side(pos)
            exit_price = get_mark_price(symbol)
            sz = abs(dec(pos.get("pos", "0")))
            ct_val = get_instrument(symbol)["ctVal"]
            pnl = (exit_price - avg) * sz * ct_val if side == "buy" else (avg - exit_price) * sz * ct_val
            db_record_trade_close(symbol, exit_price, pnl, "position_closed")
            risk_record_trade_result(pnl)
            send_alert(f"Position closed. Estimated PnL: {fmt(pnl,4)} USDT", "INFO", symbol)
            with state_lock:
                state.setdefault(symbol, {}).update({
                    "current_sl": None, "current_tp": None, "step_level": 0,
                    "protection": "NONE", "trade_status": "CLOSED",
                })
        except Exception as exc:
            log(f"CLOSE DETECTION WARNING | {symbol} | {exc}")


def validate_config():
    problems = []
    if MARGIN_USDT <= 0:
        problems.append("MARGIN_USDT must be > 0")
    if LEVERAGE <= 0 or LEVERAGE > 50:
        problems.append("LEVERAGE looks out of a sane range (0, 50]")
    if SL_PERCENT <= 0 or SL_PERCENT > MAX_SL_DISTANCE_PCT:
        problems.append("SL_PERCENT must be > 0 and <= MAX_SL_DISTANCE_PCT")
    if TP_PERCENT <= 0:
        problems.append("TP_PERCENT must be > 0")
    if MAX_TOTAL_NOTIONAL_USDT < MARGIN_USDT * LEVERAGE:
        problems.append("MAX_TOTAL_NOTIONAL_USDT is smaller than a single position's notional")
    if DAILY_MAX_LOSS_PCT <= 0 or DAILY_MAX_LOSS_PCT > 50:
        problems.append("DAILY_MAX_LOSS_PCT looks out of a sane range")
    if MAX_DRAWDOWN_PCT <= 0 or MAX_DRAWDOWN_PCT > 80:
        problems.append("MAX_DRAWDOWN_PCT looks out of a sane range")
    if not SYMBOLS:
        problems.append("SYMBOLS list is empty")
    if problems:
        msg = "CONFIG VALIDATION FAILED:\n" + "\n".join(f" - {p}" for p in problems)
        log(msg)
        send_alert(msg, "CRITICAL")
        if AUTO_TRADE:
            raise RuntimeError("Refusing to start with AUTO_TRADE=true and invalid configuration. Fix env vars.")


def startup_checks():
    log("====================================================")
    log(f"OKX SCALPING BOT {VERSION} | PA-FIRST + INDICATOR FILTERS + RISK LAYER + SIMPLIFIED DASHBOARD")
    log(f"DEMO={DEMO} | AUTO_TRADE={AUTO_TRADE} | ALLOW_LIVE={ALLOW_LIVE}")
    log(f"MARGIN=${MARGIN_USDT} | LEVERAGE={LEVERAGE}x | MAX_EXPOSURE=${MAX_TOTAL_NOTIONAL_USDT}")
    log(f"SL={SL_PERCENT}% | TP={TP_PERCENT}% | FEE_BUFFER=${FEE_BUFFER_USDT}")
    log(f"PA_BOS_MAX_AGE_BARS={PA_BOS_MAX_AGE_BARS} | GATE=BOS_RECENT_OR_RETEST_OK")
    log(f"DAILY_MAX_LOSS={DAILY_MAX_LOSS_PCT}% | MAX_DRAWDOWN={MAX_DRAWDOWN_PCT}% | MAX_CONSEC_LOSSES={MAX_CONSECUTIVE_LOSSES}")
    log(f"VOL_SIZE_ADJUST={VOL_SIZE_ADJUST} | CORRELATION_GROUPS={list(CORRELATION_GROUPS.keys())}")
    log(f"TELEGRAM_ALERTS={'ON' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else 'OFF'}")
    log(f"SYMBOLS={SYMBOLS}")
    log("====================================================")
    validate_config()
    db_init()
    risk_init()
    sync_okx_time()
    refresh_position_mode()
    public_get("/api/v5/market/ticker", {"instId": "BTC-USDT-SWAP"})
    private_request("GET", "/api/v5/account/balance")
    refresh_position_snapshot()
    for symbol in SYMBOLS:
        try:
            get_instrument(symbol)
        except Exception as exc:
            log(f"INSTRUMENT WARNING | {symbol} | {exc}")
    with state_lock:
        state["public_api"] = "CONNECTED"
        state["private_api"] = "CONNECTED"
    send_alert(f"{VERSION} started. Mode={'DEMO' if DEMO else 'LIVE'} AUTO_TRADE={AUTO_TRADE}", "INFO")


def worker():
    global worker_started, worker_error
    while True:
        try:
            startup_checks()
            worker_started = True
            worker_error = ""
            break
        except Exception as exc:
            worker_started = False
            worker_error = str(exc)
            log(f"STARTUP RETRY | {type(exc).__name__}: {exc}")
            send_alert(f"Startup failed, retrying: {exc}", "CRITICAL")
            time.sleep(max(10, POLL_SECONDS))

    prev_snapshot = {}
    while True:
        cycle_start = time.time()
        maybe_resync_time()

        try:
            snapshot = refresh_position_snapshot()
        except Exception as exc:
            log(f"POSITION SNAPSHOT ERROR | {exc}")
            snapshot = dict(position_snapshot)

        try:
            detect_and_record_closed_positions(prev_snapshot, snapshot)
        except Exception as exc:
            log(f"CLOSE DETECTION ERROR | {exc}")
        prev_snapshot = dict(snapshot)

        for symbol in SYMBOLS:
            try:
                with state_lock:
                    state.setdefault(symbol, {})["last_activity"] = "CHECKING " + symbol
                position = snapshot.get(symbol)
                if position:
                    manage_position(symbol, position)
                    with state_lock:
                        state[symbol]["trade_status"] = "POSITION MANAGED"
                    continue

                with state_lock:
                    if state.get(symbol, {}).get("halted"):
                        state[symbol]["trade_status"] = f"HALTED: {state[symbol].get('halt_reason')}"
                        continue

                oi_now = None
                try:
                    oi_rows = public_get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": symbol}).get("data", [])
                    if oi_rows:
                        current = dec(oi_rows[0].get("oiCcy") or oi_rows[0].get("oi") or "0")
                        with state_lock:
                            prev = state.setdefault(symbol, {}).get("oi_prev")
                            state[symbol]["oi_prev"] = current
                        if prev not in (None, Decimal("0")):
                            oi_now = (current - prev) / prev * Decimal("100")
                except Exception as exc:
                    log(f"OI WARNING | {symbol} | {exc}")

                analysis = analyze(symbol, oi_now)
                with state_lock:
                    state.setdefault(symbol, {}).update(analysis)
                    state[symbol]["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                log(f"{symbol}: {analysis['signal']} {analysis.get('score',0)}/{analysis.get('max_score',9)} need={analysis.get('required_score','-')} | trend={analysis.get('trend15')} | {analysis.get('session')} | {analysis.get('block_reason','')} | {analysis.get('reason','')}")

                if analysis.get("signal") in ("BUY", "SELL") and analysis.get("score", 0) >= analysis.get("required_score", 99):
                    result = place_order(symbol, analysis, snapshot)
                    log("TRADE RESULT | " + json.dumps(result, default=str))
                    with state_lock:
                        state[symbol]["trade_status"] = result.get("status", "UNKNOWN")
                        state[symbol]["trade_result"] = result
                        if result.get("status") != "ORDER_AND_PROTECTION_ACTIVE":
                            state[symbol]["block_reason"] = result.get("reason", state[symbol].get("block_reason"))
                    if result.get("status") == "ORDER_AND_PROTECTION_ACTIVE":
                        try:
                            snapshot = refresh_position_snapshot()
                        except Exception:
                            pass
                else:
                    with state_lock:
                        state[symbol]["trade_status"] = "NO TRADE"
            except Exception as exc:
                log(f"{symbol} ERROR | {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                with state_lock:
                    state.setdefault(symbol, {})["trade_status"] = "ERROR"
                    state[symbol]["trade_error"] = str(exc)
                db_log_event("ERROR", symbol, str(exc))

        elapsed = time.time() - cycle_start
        time.sleep(max(1, POLL_SECONDS - int(elapsed)))


HTML = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>OKX Scalping Bot V14.2</title><style>
body{font-family:Arial,sans-serif;background:#0b0d10;color:#eee;margin:0;padding:14px}
.card{background:#15191f;border:1px solid #2b313a;border-radius:10px;padding:10px;margin-bottom:8px}
.wrap{overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:1100px}
th,td{padding:8px;border-bottom:1px solid #2b313a;text-align:left;white-space:nowrap;vertical-align:middle}
th{background:#171b21;position:sticky;top:0}
.buy{color:#45d483;font-weight:bold}
.sell{color:#ff6565;font-weight:bold}
.active{color:#45d483;font-weight:bold}
.danger{color:#ff6565;font-weight:bold}
button{background:#ff6565;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer}
.pipe{display:flex;gap:3px}
.dot{width:24px;height:20px;border-radius:4px;font-size:9px;display:flex;align-items:center;justify-content:center;font-weight:bold;color:#0b0d10}
.dot.ok{background:#45d483}
.dot.fail{background:#3a3f47;color:#8a919c}
.why{color:#f2b84b;white-space:normal;max-width:280px}
.trend-bull{background:#123d2a;color:#45d483;font-weight:bold;padding:5px 12px;border-radius:6px;display:inline-block}
.trend-bear{background:#3d1212;color:#ff6565;font-weight:bold;padding:5px 12px;border-radius:6px;display:inline-block}
.trend-flat{background:#2b2f36;color:#9aa0a8;font-weight:bold;padding:5px 12px;border-radius:6px;display:inline-block}
.sigbuy{background:#123d2a;color:#45d483;font-weight:bold;padding:5px 12px;border-radius:6px;display:inline-block}
.sigsell{background:#3d1212;color:#ff6565;font-weight:bold;padding:5px 12px;border-radius:6px;display:inline-block}
.signone{background:#2b2f36;color:#9aa0a8;font-weight:bold;padding:5px 12px;border-radius:6px;display:inline-block}
</style></head><body>
<h2>OKX SCALP V14.2 — SIMPLIFIED VIEW</h2>
<div id="top"></div><div id="activity">Loading...</div>
<div class="wrap"><table><thead><tr>
<th>Pair</th><th>Trend</th><th>Signal</th><th>Score</th><th>Session</th>
<th>BUY Gate (SR→SWP→RCL→BOS→RTS)</th><th>SELL Gate (SR→SWP→RCL→BOS→RTS)</th>
<th>Why no trade</th>
<th>Entry</th><th>Mark</th><th>P/L%</th><th>SL</th><th>TP</th><th>Step</th><th>Protection</th><th>Status</th>
</tr></thead><tbody id="rows"></tbody></table></div>
<script>
function e(x){return String(x??"-").replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function trendBadge(t){
  if(t==='bull') return '<span class="trend-bull">▲ UPTREND</span>';
  if(t==='bear') return '<span class="trend-bear">▼ DOWNTREND</span>';
  return '<span class="trend-flat">— FLAT</span>';
}
function signalBadge(sig){
  if(sig==='BUY') return '<span class="sigbuy">▲ BUY</span>';
  if(sig==='SELL') return '<span class="sigsell">▼ SELL</span>';
  return '<span class="signone">NONE</span>';
}
function pipe(stages){
  if(!stages || !stages.length) return '<span style="color:#666">n/a</span>';
  return '<div class="pipe">' + stages.map(s=>'<div class="dot '+(s.ok?'ok':'fail')+'" title="'+e(s.name)+(s.recent===false?' (stale)':'')+'">'+e(s.name.slice(0,3))+'</div>').join('') + '</div>';
}
async function refresh(){
  try{
    const s=await fetch('/api/status').then(r=>r.json());
    document.getElementById('top').innerHTML='<div class="card">Mode: <b>'+e(s.mode)+'</b> | Auto: <b>'+e(s.auto_trade)+'</b> | Margin: <b>$'+e(s.margin)+'</b> | Lev: <b>'+e(s.leverage)+'x</b> | Exposure: <b>$'+e(s.exposure)+' / $'+e(s.max_exposure)+'</b> | Risk Halted: <b>'+e(s.trading_halted)+'</b> '+e(s.halt_reason||'')+'</div><div class="card">Public: <b>'+e(s.public_api)+'</b> | Private: <b>'+e(s.private_api)+'</b> | Snapshot age: <b>'+e(s.position_snapshot_age)+'s</b> | <button onclick="killSwitch()">KILL SWITCH</button></div>';
    document.getElementById('activity').textContent='Last activity: '+e(s.last_activity)+' | '+e(s.updated);
    let h='';
    for(const [sym,x] of Object.entries(s.symbols||{})){
      const sig=x.signal||'NONE';
      h+='<tr><td>'+e(sym)+'</td>'
        +'<td>'+trendBadge(x.trend15)+'</td>'
        +'<td>'+signalBadge(sig)+'</td>'
        +'<td>'+e(x.score)+'/'+e(x.max_score)+' need '+e(x.required_score)+'</td>'
        +'<td>'+e(x.session)+'</td>'
        +'<td>'+pipe(x.gate_stages_buy)+'</td>'
        +'<td>'+pipe(x.gate_stages_sell)+'</td>'
        +'<td class="why">'+e(x.block_reason||(sig!=='NONE'?'—':''))+'</td>'
        +'<td>'+e(x.entry_price||x.entry)+'</td><td>'+e(x.mark_price)+'</td><td>'+e(x.profit_pct)+'</td>'
        +'<td>'+e(x.current_sl)+'</td><td>'+e(x.current_tp)+'</td><td>'+e(x.step_level)+'</td>'
        +'<td class="'+(x.protection==='ACTIVE'?'active':'danger')+'">'+e(x.protection||'NONE')+'</td>'
        +'<td>'+e(x.trade_status||'WAITING')+'</td></tr>';
    }
    document.getElementById('rows').innerHTML=h;
  }catch(err){document.getElementById('activity').textContent='Dashboard error: '+err}
}
function killSwitch(){const token=prompt('Enter kill-switch token:');if(token===null)return;fetch('/api/kill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})}).then(r=>r.json()).then(d=>alert(JSON.stringify(d)))}
refresh();setInterval(refresh,5000)
</script></body></html>'''


@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


@app.get("/api/status")
def api_status():
    with state_lock:
        symbols = {k: v.copy() for k, v in state.items() if k in SYMBOLS}
        public_api = state.get("public_api", "STARTING")
        private_api = state.get("private_api", "STARTING")
    with risk_lock:
        trading_halted = risk_state.get("trading_halted", False)
        halt_reason = risk_state.get("halt_reason", "")
    activity = "STARTING"
    for symbol in SYMBOLS:
        if symbols.get(symbol, {}).get("last_activity"):
            activity = symbols[symbol]["last_activity"]
    return jsonify({
        "bot": f"OKX Scalping Bot {VERSION}", "status": "running" if worker_started else "starting",
        "mode": "DEMO" if DEMO else "LIVE", "demo": DEMO, "auto_trade": AUTO_TRADE,
        "margin": str(MARGIN_USDT), "leverage": str(LEVERAGE), "notional": str(MARGIN_USDT * LEVERAGE),
        "max_exposure": str(MAX_TOTAL_NOTIONAL_USDT), "exposure": str(total_open_notional()),
        "public_api": public_api, "private_api": private_api, "worker_error": worker_error,
        "trading_halted": trading_halted, "halt_reason": halt_reason,
        "position_snapshot_age": round(max(0, time.time() - position_snapshot_ts), 1) if position_snapshot_ts else None,
        "last_activity": activity, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "symbols": symbols
    })


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "bot": VERSION, "demo": DEMO, "auto_trade": AUTO_TRADE, "worker_started": worker_started})


@app.post("/api/kill")
def api_kill():
    body = flask_request.get_json(silent=True) or {}
    token = body.get("token", "")
    if not KILL_SWITCH_TOKEN:
        return jsonify({"status": "DISABLED", "reason": "KILL_SWITCH_TOKEN not configured"}), 400
    if token != KILL_SWITCH_TOKEN:
        return jsonify({"status": "DENIED", "reason": "Invalid token"}), 403
    manual_kill_switch("Manual kill switch via /api/kill")
    return jsonify({"status": "HALTED"})


@app.post("/api/resume")
def api_resume():
    body = flask_request.get_json(silent=True) or {}
    token = body.get("token", "")
    if not KILL_SWITCH_TOKEN or token != KILL_SWITCH_TOKEN:
        return jsonify({"status": "DENIED"}), 403
    manual_resume_trading()
    return jsonify({"status": "RESUMED"})


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
