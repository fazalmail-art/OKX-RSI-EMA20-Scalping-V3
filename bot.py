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
from datetime import datetime, timezone, time as dtime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, Response, request as flask_request
from dotenv import load_dotenv

load_dotenv()

# =============================================================
# OKX SCALPING BOT V15.0 — COST-AWARE / FAIL-CLOSED / AUDITABLE
#
# IMPORTANT:
#   DEMO=true and AUTO_TRADE=false are intentionally safe defaults.
#   Set AUTO_TRADE=true only after demo validation.
#   Set DEMO=false and ALLOW_LIVE=true only after independent review.
# =============================================================

VERSION = "V15.0-COST-AWARE-AUDITABLE"
BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

BAR = os.getenv("BAR", "15m")
TREND_BAR = os.getenv("TREND_BAR", "1H")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
SYMBOLS = [x.strip() for x in os.getenv(
    "SYMBOLS",
    "ETH-USDT-SWAP,SOL-USDT-SWAP,DOGE-USDT-SWAP,NEAR-USDT-SWAP",
).split(",") if x.strip()]

MARGIN_USDT = Decimal(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = Decimal(os.getenv("LEVERAGE", "3"))
TD_MODE = os.getenv("TD_MODE", "isolated")
MAX_TOTAL_NOTIONAL_USDT = Decimal(os.getenv("MAX_TOTAL_NOTIONAL_USDT", "530"))

SL_PERCENT = Decimal(os.getenv("SL_PERCENT", "0.38"))
TP_PERCENT = Decimal(os.getenv("TP_PERCENT", "1.00"))
MAX_SL_DISTANCE_PCT = Decimal(os.getenv("MAX_SL_DISTANCE_PCT", "0.80"))
BREAK_EVEN_TRIGGER_PCT = Decimal(os.getenv("BREAK_EVEN_TRIGGER_PCT", "0.35"))
BREAK_EVEN_OFFSET_PCT = Decimal(os.getenv("BREAK_EVEN_OFFSET_PCT", "0.10"))
TRAIL_START_PCT = Decimal(os.getenv("TRAIL_START_PCT", "0.50"))
TRAIL_DISTANCE_PCT = Decimal(os.getenv("TRAIL_DISTANCE_PCT", "0.20"))
STEP_TRIGGER_PCT = Decimal(os.getenv("STEP_TRIGGER_PCT", "0.50"))

# Cost model. FEE_RATE_PER_SIDE must match the actual account fee tier.
FEE_RATE_PER_SIDE = Decimal(os.getenv("FEE_RATE_PER_SIDE", "0.0006"))
ROUND_TRIP_SLIPPAGE_PCT = Decimal(os.getenv("ROUND_TRIP_SLIPPAGE_PCT", "0.10"))
MIN_NET_TP_USDT = Decimal(os.getenv("MIN_NET_TP_USDT", "0.02"))
ENTRY_ORDER_TYPE = os.getenv("ENTRY_ORDER_TYPE", "ioc").lower()
MAX_ENTRY_SLIPPAGE_PCT = Decimal(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.10"))
ORDERBOOK_LEVELS = int(os.getenv("ORDERBOOK_LEVELS", "5"))
MIN_SIDE_DEPTH_MULT = Decimal(os.getenv("MIN_SIDE_DEPTH_MULT", "3.0"))

# Keep deployment alive when an old environment contains MIN_SCORE=6 or another invalid value.
# The effective maximum score requirement is 5/6; invalid values fall back safely to 5.
MIN_SCORE_RAW = os.getenv("MIN_SCORE", "5")
try:
    MIN_SCORE = int(MIN_SCORE_RAW)
except (TypeError, ValueError):
    MIN_SCORE = 5
if not 1 <= MIN_SCORE <= 5:
    MIN_SCORE = 5
PA_LOOKBACK = int(os.getenv("PA_LOOKBACK", "24"))
PA_MAX_AGE_BARS = int(os.getenv("PA_MAX_AGE_BARS", "6"))
PA_RETEST_MAX_BARS = int(os.getenv("PA_RETEST_MAX_BARS", "5"))
PA_BOS_MAX_AGE_BARS = int(os.getenv("PA_BOS_MAX_AGE_BARS", "5"))
SR_ATR_MULT = Decimal(os.getenv("SR_ATR_MULT", "1.30"))
SR_PCT = Decimal(os.getenv("SR_PCT", "0.10"))
PA_SWEEP_ATR_BUFFER = Decimal(os.getenv("PA_SWEEP_ATR_BUFFER", "0.20"))
ADX_MIN = Decimal(os.getenv("ADX_MIN", "18"))
VOLUME_MULT = Decimal(os.getenv("VOLUME_MULT", "1.00"))
ATR_MIN_PCT = Decimal(os.getenv("ATR_MIN_PCT", "0.05"))

OI_UNWIND_PCT = Decimal(os.getenv("OI_UNWIND_PCT", "0.30"))
OI_SAMPLE_SECONDS = int(os.getenv("OI_SAMPLE_SECONDS", "300"))
FUNDING_LOOKBACK = int(os.getenv("FUNDING_LOOKBACK", "30"))

DAILY_MAX_LOSS_PCT = Decimal(os.getenv("DAILY_MAX_LOSS_PCT", "3"))
MAX_DRAWDOWN_PCT = Decimal(os.getenv("MAX_DRAWDOWN_PCT", "8"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))
TIME_RESYNC_SECONDS = int(os.getenv("TIME_RESYNC_SECONDS", "1800"))
VOL_SIZE_ADJUST = os.getenv("VOL_SIZE_ADJUST", "true").lower() == "true"
VOL_SIZE_BASELINE_ATR_PCT = Decimal(os.getenv("VOL_SIZE_BASELINE_ATR_PCT", "0.15"))
VOL_SIZE_MIN_MULT = Decimal(os.getenv("VOL_SIZE_MIN_MULT", "0.50"))
VOL_SIZE_MAX_MULT = Decimal(os.getenv("VOL_SIZE_MAX_MULT", "1.25"))
MAX_CORRELATED_POSITIONS = int(os.getenv("MAX_CORRELATED_POSITIONS", "2"))
DB_PATH = os.getenv("DB_PATH", "bot_state_v15.db")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
KILL_SWITCH_TOKEN = os.getenv("KILL_SWITCH_TOKEN", "")

try:
    # Default: ETH/SOL/DOGE/NEAR are all high-beta to broad crypto-market moves,
    # so treat them as one correlated group. MAX_CORRELATED_POSITIONS caps how
    # many of this group can be open at once (prevents 1 market move from
    # appearing as several "independent" same-direction trades).
    CORRELATION_GROUPS = json.loads(os.getenv(
        "CORRELATION_GROUPS",
        '{"CRYPTO_BETA": ["ETH-USDT-SWAP", "SOL-USDT-SWAP", "DOGE-USDT-SWAP", "NEAR-USDT-SWAP"]}',
    ))
except Exception:
    CORRELATION_GROUPS = {}

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

app = Flask(__name__)
session = requests.Session()
state = {}
state_lock = threading.Lock()
order_lock = threading.Lock()
db_lock = threading.Lock()
risk_lock = threading.Lock()
instrument_cache = {}
instrument_cache_lock = threading.Lock()
funding_cache = {}
funding_cache_lock = threading.Lock()
position_snapshot = {}
position_snapshot_ts = 0.0
server_offset_ms = 0
last_time_sync = 0.0
position_mode = "net"
worker_started = False
worker_error = ""

risk_state = {
    "trading_halted": False,
    "halt_reason": "",
    "day": None,
    "day_start_equity": None,
    "peak_equity": None,
    "consecutive_losses": 0,
    "last_trade_result": None,
}

TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def dec(value):
    return Decimal(str(value))


def fmt(value, places=12):
    if value is None:
        return "-"
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def floor_step(value, step):
    return value if step <= 0 else (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value, step):
    return value if step <= 0 else (value / step).to_integral_value(rounding=ROUND_UP) * step


# --------------------------- Database ---------------------------

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
                symbol TEXT, side TEXT, pos_side TEXT,
                entry REAL, exit REAL, sl REAL, tp REAL,
                requested_size TEXT, filled_size TEXT,
                requested_notional REAL, filled_notional REAL,
                fee_usdt REAL DEFAULT 0, funding_usdt REAL DEFAULT 0,
                realized_pnl REAL, opened_at TEXT, closed_at TEXT,
                status TEXT, reason TEXT, entry_order_id TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, level TEXT, symbol TEXT, message TEXT
            );
            CREATE TABLE IF NOT EXISTS kv_state (
                key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
            );
            """)
            # Backward-compatible migration if the old database is reused.
            existing = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
            for col, typ in {
                "pos_side": "TEXT", "requested_size": "TEXT", "filled_size": "TEXT",
                "requested_notional": "REAL", "filled_notional": "REAL",
                "fee_usdt": "REAL DEFAULT 0", "funding_usdt": "REAL DEFAULT 0",
                "realized_pnl": "REAL", "entry_order_id": "TEXT",
            }.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
            conn.commit()
        finally:
            conn.close()


def db_event(level, symbol, message):
    try:
        with db_lock:
            conn = db_connect()
            conn.execute("INSERT INTO events(ts,level,symbol,message) VALUES(?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), level, symbol or "", str(message)[:4000]))
            conn.commit()
            conn.close()
    except Exception as exc:
        log(f"DB EVENT FAILED | {exc}")


def db_trade_open(symbol, side, pos_side, entry, sl, tp, requested_size, filled_size,
                  requested_notional, filled_notional, order_id):
    with db_lock:
        conn = db_connect()
        cur = conn.execute(
            """INSERT INTO trades(symbol,side,pos_side,entry,sl,tp,requested_size,filled_size,
               requested_notional,filled_notional,opened_at,status,entry_order_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, side, pos_side, float(entry), float(sl), float(tp), fmt(requested_size),
             fmt(filled_size), float(requested_notional), float(filled_notional),
             datetime.now(timezone.utc).isoformat(), "OPEN", order_id),
        )
        conn.commit()
        conn.close()
        return cur.lastrowid


def db_trade_close(symbol, pos_side, exit_price, realized_pnl, fee, funding, reason):
    with db_lock:
        conn = db_connect()
        row = conn.execute(
            "SELECT id FROM trades WHERE symbol=? AND (pos_side=? OR pos_side IS NULL) AND status='OPEN' ORDER BY id DESC LIMIT 1",
            (symbol, pos_side),
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE trades SET exit=?, realized_pnl=?, fee_usdt=?, funding_usdt=?, closed_at=?,
                   status='CLOSED', reason=? WHERE id=?""",
                (float(exit_price), float(realized_pnl) if realized_pnl is not None else None,
                 float(fee or 0), float(funding or 0), datetime.now(timezone.utc).isoformat(), reason, row[0]),
            )
            conn.commit()
        conn.close()


def db_kv_set(key, value):
    with db_lock:
        conn = db_connect()
        conn.execute("""INSERT INTO kv_state(key,value,updated_at) VALUES(?,?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                     (key, json.dumps(value, default=str), datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()


def db_kv_get(key, default=None):
    try:
        with db_lock:
            conn = db_connect()
            row = conn.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
            conn.close()
        return json.loads(row[0]) if row else default
    except Exception:
        return default


def alert(message, level="INFO", symbol=None):
    log(f"ALERT[{level}] {('(' + symbol + ') ') if symbol else ''}{message}")
    db_event(level, symbol, message)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": f"[{level}] {VERSION}\n{message}"}, timeout=8)
        except Exception as exc:
            log(f"TELEGRAM FAILED | {exc}")


# --------------------------- OKX API ---------------------------

def public_get(path, params=None, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(BASE_URL + path, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            if str(data.get("code", "0")) != "0":
                raise RuntimeError(f"OKX PUBLIC {data.get('code')}: {data.get('msg')}")
            return data
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.5 * attempt)
    raise RuntimeError(f"OKX PUBLIC failed {path}: {last}")


def sync_okx_time():
    global server_offset_ms, last_time_sync
    before = int(time.time() * 1000)
    data = public_get("/api/v5/public/time")
    after = int(time.time() * 1000)
    server_ms = int(data["data"][0]["ts"])
    server_offset_ms = server_ms - ((before + after) // 2)
    last_time_sync = time.time()


def maybe_resync_time():
    if time.time() - last_time_sync >= TIME_RESYNC_SECONDS:
        try:
            sync_okx_time()
        except Exception as exc:
            log(f"TIME RESYNC WARNING | {exc}")


def utc_timestamp():
    ms = int(time.time() * 1000) + server_offset_ms
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sign(timestamp, method, request_path, body=""):
    prehash = timestamp + method.upper() + request_path + body
    digest = hmac.new(SECRET_KEY.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def private_request(method, path, payload=None, params=None, retries=3):
    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        raise RuntimeError("OKX credentials are missing")
    method = method.upper()
    query = ""
    if params:
        query = "?" + urlencode([(str(k), str(v)) for k, v in params.items()])
    request_path = path + query
    body = "" if payload is None else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    last = None
    for attempt in range(1, retries + 1):
        ts = utc_timestamp()
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": API_KEY,
            "OK-ACCESS-SIGN": sign(ts, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        }
        if DEMO:
            headers["x-simulated-trading"] = "1"
        try:
            r = session.request(method, BASE_URL + path, headers=headers, params=params,
                                data=body or None, timeout=15)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}
            if r.status_code >= 400:
                if r.status_code in TRANSIENT_HTTP and attempt < retries:
                    last = RuntimeError(f"HTTP {r.status_code}: {data}")
                    time.sleep(0.6 * attempt)
                    continue
                raise RuntimeError(f"OKX PRIVATE HTTP {r.status_code}: {data}")
            if str(data.get("code", "")) != "0":
                raise RuntimeError(f"OKX PRIVATE {data.get('code')}: {data.get('msg')}")
            return data
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
            if attempt == retries:
                raise RuntimeError(f"OKX PRIVATE network failure: {last}")
            time.sleep(0.6 * attempt)
    raise RuntimeError(f"OKX PRIVATE failed {method} {path}: {last}")


def ticker(symbol):
    rows = public_get("/api/v5/market/ticker", {"instId": symbol}).get("data", [])
    if not rows:
        raise RuntimeError(f"Ticker unavailable: {symbol}")
    return dec(rows[0]["last"])


def mark_price(symbol):
    rows = public_get("/api/v5/public/mark-price", {"instType": "SWAP", "instId": symbol}).get("data", [])
    return dec(rows[0]["markPx"]) if rows else ticker(symbol)


def orderbook(symbol, levels=None):
    rows = public_get("/api/v5/market/books", {"instId": symbol, "sz": str(levels or ORDERBOOK_LEVELS)}).get("data", [])
    if not rows:
        raise RuntimeError(f"Orderbook unavailable: {symbol}")
    bids = [(dec(x[0]), dec(x[1])) for x in rows[0].get("bids", [])]
    asks = [(dec(x[0]), dec(x[1])) for x in rows[0].get("asks", [])]
    if not bids or not asks:
        raise RuntimeError(f"Empty orderbook: {symbol}")
    return bids, asks


def candles(symbol, bar, limit=180):
    rows = public_get("/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": str(limit)}).get("data", [])
    result = []
    for row in reversed(rows):
        result.append({"ts": int(row[0]), "open": dec(row[1]), "high": dec(row[2]), "low": dec(row[3]),
                       "close": dec(row[4]), "volume": dec(row[5]), "confirm": row[8] if len(row) > 8 else "1"})
    return result


def instrument(symbol, force=False):
    now = time.time()
    with instrument_cache_lock:
        cached = instrument_cache.get(symbol)
        if cached and not force and now - cached["ts"] < 3600:
            return cached["data"]
    rows = public_get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol}).get("data", [])
    if not rows:
        raise RuntimeError(f"Instrument not found: {symbol}")
    x = rows[0]
    data = {"ctVal": dec(x["ctVal"]), "lotSz": dec(x["lotSz"]), "minSz": dec(x["minSz"]),
            "tickSz": dec(x["tickSz"]), "state": x.get("state", "")}
    with instrument_cache_lock:
        instrument_cache[symbol] = {"ts": now, "data": data}
    return data


def funding_snapshot(symbol):
    now = time.time()
    with funding_cache_lock:
        if symbol in funding_cache and now - funding_cache[symbol]["ts"] < 300:
            return funding_cache[symbol]
    try:
        current = public_get("/api/v5/public/funding-rate", {"instId": symbol}).get("data", [])
        hist = public_get("/api/v5/public/funding-rate-history", {"instId": symbol, "limit": str(FUNDING_LOOKBACK)}).get("data", [])
        current_rate = dec(current[0]["fundingRate"]) * 100 if current else None
        history = [abs(dec(x["fundingRate"]) * 100) for x in hist]
        threshold = max(sorted(history)[min(int(len(history) * 0.85), len(history) - 1)], Decimal("0.005")) if len(history) >= 10 else Decimal("0.03")
        snap = {"ts": now, "current": current_rate, "threshold": threshold}
    except Exception as exc:
        log(f"FUNDING WARNING | {symbol} | {exc}")
        snap = {"ts": now, "current": None, "threshold": Decimal("0.03")}
    with funding_cache_lock:
        funding_cache[symbol] = snap
    return snap


def account_equity():
    data = private_request("GET", "/api/v5/account/balance")
    rows = data.get("data", [])
    if not rows:
        return None
    usdt = next((x for x in rows[0].get("details", []) if x.get("ccy") == "USDT"), None)
    return dec((usdt or {}).get("eq") or (usdt or {}).get("cashBal") or rows[0].get("totalEq") or "0")


def refresh_position_mode():
    global position_mode
    rows = private_request("GET", "/api/v5/account/config").get("data", [])
    raw = str(rows[0].get("posMode", "net")).lower() if rows else "net"
    position_mode = "long_short_mode" if raw in ("long_short", "long_short_mode") else "net"
    log(f"POSITION MODE | {position_mode}")


def refresh_positions():
    global position_snapshot, position_snapshot_ts
    rows = private_request("GET", "/api/v5/account/positions", params={"instType": "SWAP"}).get("data", [])
    snap = {}
    for row in rows:
        try:
            if dec(row.get("pos", "0")) != 0:
                snap.setdefault(row.get("instId"), []).append(row)
        except Exception:
            continue
    with state_lock:
        position_snapshot = snap
        position_snapshot_ts = time.time()
    return snap


def positions_for(symbol, snapshot=None):
    return list((snapshot if snapshot is not None else position_snapshot).get(symbol, []))


def position_key(symbol, pos):
    return f"{symbol}:{pos.get('posSide', 'net')}"


def position_side(pos):
    if position_mode == "long_short_mode":
        return "buy" if pos.get("posSide") == "long" else "sell"
    return "buy" if dec(pos.get("pos", "0")) > 0 else "sell"


def position_notional(symbol, pos):
    info = instrument(symbol)
    px = dec(pos.get("markPx") or pos.get("avgPx") or "0")
    return abs(dec(pos.get("pos", "0"))) * info["ctVal"] * px


def total_open_notional(snapshot=None):
    snap = snapshot if snapshot is not None else position_snapshot
    return sum((position_notional(s, p) for s, rows in snap.items() for p in rows), Decimal("0"))


def correlated_group_members(symbol):
    """All correlation-group symbol lists that `symbol` belongs to."""
    return [members for members in CORRELATION_GROUPS.values() if symbol in members]


def correlated_open_count(symbol, snapshot=None):
    """Max number of *other open* positions that share a correlation group with `symbol`.

    Used as a pre-trade veto so the bot cannot open more than
    MAX_CORRELATED_POSITIONS same-beta positions at once (e.g. ETH+SOL+DOGE+NEAR
    all long at the same time on one market-wide move).
    """
    groups = correlated_group_members(symbol)
    if not groups:
        return 0
    snap = snapshot if snapshot is not None else position_snapshot
    open_symbols = {s for s, rows in snap.items() if rows and s != symbol}
    return max((len(open_symbols.intersection(members)) for members in groups), default=0)


# --------------------------- Risk ---------------------------

def risk_init():
    saved = db_kv_get("risk_state")
    with risk_lock:
        if saved:
            risk_state.update(saved)
        today = datetime.now(PKT_TZ).date().isoformat()
        if risk_state.get("day") != today:
            eq = account_equity()
            risk_state.update({"day": today, "day_start_equity": float(eq) if eq is not None else None,
                               "consecutive_losses": 0, "trading_halted": False, "halt_reason": ""})
        if risk_state.get("peak_equity") is None:
            eq = account_equity()
            risk_state["peak_equity"] = float(eq) if eq is not None else None
        db_kv_set("risk_state", risk_state)


def risk_check():
    today = datetime.now(PKT_TZ).date().isoformat()
    with risk_lock:
        if risk_state.get("day") != today:
            eq = account_equity()
            risk_state.update({"day": today, "day_start_equity": float(eq) if eq is not None else None,
                               "consecutive_losses": 0})
        if risk_state.get("trading_halted"):
            return False, risk_state.get("halt_reason", "HALTED")
    try:
        eq = account_equity()
    except Exception as exc:
        return False, f"EQUITY_UNAVAILABLE: {exc}"
    if eq is None or eq <= 0:
        return False, "EQUITY_UNAVAILABLE"
    with risk_lock:
        peak = dec(str(risk_state.get("peak_equity") or eq))
        day_start = dec(str(risk_state.get("day_start_equity") or eq))
        if eq > peak:
            peak = eq
            risk_state["peak_equity"] = float(eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else Decimal("0")
        daily = (day_start - eq) / day_start * 100 if day_start > 0 else Decimal("0")
        if dd >= MAX_DRAWDOWN_PCT:
            risk_state.update({"trading_halted": True, "halt_reason": f"MAX_DRAWDOWN {fmt(dd, 2)}%"})
            db_kv_set("risk_state", risk_state)
            alert(risk_state["halt_reason"], "CRITICAL")
            return False, risk_state["halt_reason"]
        if daily >= DAILY_MAX_LOSS_PCT:
            risk_state.update({"trading_halted": True, "halt_reason": f"DAILY_LOSS {fmt(daily, 2)}%"})
            db_kv_set("risk_state", risk_state)
            alert(risk_state["halt_reason"], "CRITICAL")
            return False, risk_state["halt_reason"]
        if risk_state.get("consecutive_losses", 0) >= MAX_CONSECUTIVE_LOSSES:
            risk_state.update({"trading_halted": True, "halt_reason": "MAX_CONSECUTIVE_LOSSES"})
            db_kv_set("risk_state", risk_state)
            alert(risk_state["halt_reason"], "CRITICAL")
            return False, risk_state["halt_reason"]
        db_kv_set("risk_state", risk_state)
    return True, ""


def record_result(pnl):
    if pnl is None:
        return
    with risk_lock:
        risk_state["consecutive_losses"] = risk_state.get("consecutive_losses", 0) + 1 if pnl < 0 else 0
        risk_state["last_trade_result"] = float(pnl)
        db_kv_set("risk_state", risk_state)


def kill(reason):
    with risk_lock:
        risk_state.update({"trading_halted": True, "halt_reason": reason})
        db_kv_set("risk_state", risk_state)
    alert(reason, "CRITICAL")


def resume():
    with risk_lock:
        risk_state.update({"trading_halted": False, "halt_reason": ""})
        db_kv_set("risk_state", risk_state)
    alert("Trading resumed manually", "WARNING")


# --------------------------- Indicators ---------------------------

def confirmed(cs):
    return [x for x in cs if x.get("confirm") == "1"]


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    v = sum(values[:period], Decimal("0")) / period
    out[period - 1] = v
    k = Decimal("2") / (period + 1)
    for i in range(period, len(values)):
        v = values[i] * k + v * (1 - k)
        out[i] = v
    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains = [max(values[i] - values[i - 1], Decimal("0")) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], Decimal("0")) for i in range(1, len(values))]
    ag = sum(gains[:period], Decimal("0")) / period
    al = sum(losses[:period], Decimal("0")) / period
    def val(g, l):
        return Decimal("100") if l == 0 else Decimal("100") - Decimal("100") / (1 + g / l)
    out[period] = val(ag, al)
    for j in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[j]) / period
        al = (al * (period - 1) + losses[j]) / period
        out[j + 1] = val(ag, al)
    return out


def rma(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    v = sum(values[:period], Decimal("0")) / period
    out[period - 1] = v
    for i in range(period, len(values)):
        v = (v * (period - 1) + values[i]) / period
        out[i] = v
    return out


def atr(cs, period=14):
    if len(cs) <= period:
        return None
    tr = [max(cs[i]["high"] - cs[i]["low"], abs(cs[i]["high"] - cs[i - 1]["close"]),
              abs(cs[i]["low"] - cs[i - 1]["close"])) for i in range(1, len(cs))]
    return next((x for x in reversed(rma(tr, period)) if x is not None), None)


def adx(cs, period=14):
    if len(cs) < period * 2 + 2:
        return Decimal("0")
    plus, minus, tr = [], [], []
    for i in range(1, len(cs)):
        up = cs[i]["high"] - cs[i - 1]["high"]
        down = cs[i - 1]["low"] - cs[i]["low"]
        plus.append(up if up > down and up > 0 else Decimal("0"))
        minus.append(down if down > up and down > 0 else Decimal("0"))
        tr.append(max(cs[i]["high"] - cs[i]["low"], abs(cs[i]["high"] - cs[i - 1]["close"]),
                      abs(cs[i]["low"] - cs[i - 1]["close"])))
    p, m, t = rma(plus, period), rma(minus, period), rma(tr, period)
    dx = []
    for i in range(len(tr)):
        if p[i] is None or m[i] is None or not t[i]:
            continue
        pdi, mdi = p[i] / t[i] * 100, m[i] / t[i] * 100
        dx.append(abs(pdi - mdi) / (pdi + mdi) * 100 if pdi + mdi else Decimal("0"))
    return next((x for x in reversed(rma(dx, period)) if x is not None), Decimal("0"))


def macd(values):
    e12, e26 = ema(values, 12), ema(values, 26)
    line = [e12[i] - e26[i] if e12[i] is not None and e26[i] is not None else None for i in range(len(values))]
    valid = [x for x in line if x is not None]
    sig = ema(valid, 9)
    return line, [None] * (len(values) - len(sig)) + sig


def session_vwap(cs):
    if not cs:
        return None
    # Use PKT calendar day because the configured sessions are PKT-based.
    last_dt = datetime.fromtimestamp(cs[-1]["ts"] / 1000, timezone.utc).astimezone(PKT_TZ)
    day = last_dt.date()
    q = [x for x in cs if datetime.fromtimestamp(x["ts"] / 1000, timezone.utc).astimezone(PKT_TZ).date() == day]
    q = q or cs[-30:]
    vol = sum((x["volume"] for x in q), Decimal("0"))
    return q[-1]["close"] if vol == 0 else sum(((x["high"] + x["low"] + x["close"]) / 3) * x["volume"] for x in q) / vol


def session_info():
    now = datetime.now(PKT_TZ).time()
    for start, end, label in PRIORITY_WINDOWS:
        if start <= now < end:
            return {"name": label, "active": True, "priority": True}
    for start, end, label in SESSION_WINDOWS:
        if start <= now < end:
            return {"name": label, "active": True, "priority": False}
    return {"name": "OFF_SESSION", "active": False, "priority": False}


def trend(cs):
    cs = confirmed(cs)
    if len(cs) < 22:
        return "flat"
    v = [x["close"] for x in cs]
    e = ema(v, 20)
    i = len(v) - 1
    if e[i] is None or e[i - 1] is None:
        return "flat"
    return "bull" if v[i] > e[i] and e[i] > e[i - 1] else "bear" if v[i] < e[i] and e[i] < e[i - 1] else "flat"


def rejection(c):
    rng = max(c["high"] - c["low"], Decimal("0.00000001"))
    body = abs(c["close"] - c["open"])
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    cp = (c["close"] - c["low"]) / rng
    br = body / rng
    if lower >= body * Decimal("1.5") and cp >= Decimal("0.65") and br <= Decimal("0.65"):
        return "BULLISH"
    if upper >= body * Decimal("1.5") and cp <= Decimal("0.35") and br <= Decimal("0.65"):
        return "BEARISH"
    return "NONE"


def price_zone(levels, price, tol):
    if not levels:
        return None
    best = min(levels, key=lambda x: abs(price - x))
    return best if abs(price - best) <= tol else None


def price_action(cs):
    c = confirmed(cs)
    empty = {"sr": "NONE", "sweep": "NONE", "reclaim": "NONE", "bos": "NONE", "retest": "NONE",
             "pa_buy": 0, "pa_sell": 0, "buy_complete": False, "sell_complete": False,
             "stages_buy": [], "stages_sell": [], "candle": "NONE", "order_block": "NONE"}
    if len(c) < 50:
        return empty
    price = c[-1]["close"]
    av = atr(c) or price * Decimal("0.002")
    tol = max(av * SR_ATR_MULT, price * SR_PCT / 100)
    buffer = max(av * PA_SWEEP_ATR_BUFFER, price * Decimal("0.0005"))
    end = len(c) - 2
    start = max(2, end - PA_LOOKBACK)
    highs, lows = [], []
    for j in range(start, max(start, end - 2)):
        if c[j]["high"] >= max(c[j-2]["high"], c[j-1]["high"], c[j+1]["high"], c[j+2]["high"]):
            highs.append(c[j]["high"])
        if c[j]["low"] <= min(c[j-2]["low"], c[j-1]["low"], c[j+1]["low"], c[j+2]["low"]):
            lows.append(c[j]["low"])
    support = price_zone([x for x in lows if x <= price + tol], price, tol)
    resistance = price_zone([x for x in highs if x >= price - tol], price, tol)
    support = support if support is not None else min(x["low"] for x in c[max(0, end-12):end])
    resistance = resistance if resistance is not None else max(x["high"] for x in c[max(0, end-12):end])
    bull_sr = abs(price - support) <= tol
    bear_sr = abs(resistance - price) <= tol
    sr = "SUPPORT" if bull_sr and not bear_sr else "RESISTANCE" if bear_sr and not bull_sr else "BOTH" if bull_sr and bear_sr else "NONE"
    seq = max(start, len(c) - PA_LOOKBACK)
    bs = bb = br = None
    ss = sb = sr_idx = None
    for j in range(seq, len(c)):
        x = c[j]
        if x["low"] <= support + buffer and x["low"] < support and x["close"] > support:
            bs = j
        if x["high"] >= resistance - buffer and x["high"] > resistance and x["close"] < resistance:
            ss = j
    if bs is not None:
        ref = c[max(seq, bs - 5):bs]
        if len(ref) >= 3:
            broken = max(x["high"] for x in ref)
            for j in range(bs + 1, len(c)):
                if c[j]["close"] > broken:
                    bb = j
                    break
            if bb is not None:
                for j in range(bb + 1, min(len(c), bb + PA_RETEST_MAX_BARS + 1)):
                    if c[j]["low"] <= broken + tol and c[j]["close"] > broken:
                        br = j
                        break
    if ss is not None:
        ref = c[max(seq, ss - 5):ss]
        if len(ref) >= 3:
            broken = min(x["low"] for x in ref)
            for j in range(ss + 1, len(c)):
                if c[j]["close"] < broken:
                    sb = j
                    break
            if sb is not None:
                for j in range(sb + 1, min(len(c), sb + PA_RETEST_MAX_BARS + 1)):
                    if c[j]["high"] >= broken - tol and c[j]["close"] < broken:
                        sr_idx = j
                        break
    bull_sweep, bear_sweep = bs is not None, ss is not None
    # Reclaim is a separate validation of the sweep candle; do not count SWEEP twice.
    bull_reclaim = bull_sweep and c[bs]["close"] > support
    bear_reclaim = bear_sweep and c[ss]["close"] < resistance
    bull_bos, bear_bos = bb is not None and bb > bs, sb is not None and sb > ss
    bull_retest = br is not None and len(c) - 1 - br <= PA_MAX_AGE_BARS and br > (bb or -1)
    bear_retest = sr_idx is not None and len(c) - 1 - sr_idx <= PA_MAX_AGE_BARS and sr_idx > (sb or -1)
    bull_bos_recent = bb is not None and len(c) - 1 - bb <= PA_BOS_MAX_AGE_BARS
    bear_bos_recent = sb is not None and len(c) - 1 - sb <= PA_BOS_MAX_AGE_BARS
    buy = [bull_sr, bull_sweep, bull_reclaim, bull_bos, bull_retest]
    sell = [bear_sr, bear_sweep, bear_reclaim, bear_bos, bear_retest]
    buy_complete = all(buy[:4]) and (bull_bos_recent or bull_retest)
    sell_complete = all(sell[:4]) and (bear_bos_recent or bear_retest)
    candle = rejection(c[-1])
    return {"sr": sr, "sweep": "BULLISH" if bull_sweep else "BEARISH" if bear_sweep else "NONE",
            "reclaim": "BULLISH" if bull_reclaim else "BEARISH" if bear_reclaim else "NONE",
            "bos": "BULLISH" if bull_bos else "BEARISH" if bear_bos else "NONE",
            "retest": "BULLISH" if bull_retest else "BEARISH" if bear_retest else "NONE",
            "pa_buy": sum(buy), "pa_sell": sum(sell), "buy_complete": buy_complete, "sell_complete": sell_complete,
            "candle": candle, "order_block": "BULLISH" if bull_bos else "BEARISH" if bear_bos else "NONE",
            "bull_bos_recent": bull_bos_recent, "bear_bos_recent": bear_bos_recent,
            "stages_buy": [{"name": n, "ok": bool(v)} for n, v in zip(("SR", "SWEEP", "RECLAIM", "BOS", "RETEST"), buy)],
            "stages_sell": [{"name": n, "ok": bool(v)} for n, v in zip(("SR", "SWEEP", "RECLAIM", "BOS", "RETEST"), sell)]}


# --------------------------- Signal and cost model ---------------------------

def volatility_multiplier(atr_pct):
    if not VOL_SIZE_ADJUST or not atr_pct or atr_pct <= 0:
        return Decimal("1")
    return max(VOL_SIZE_MIN_MULT, min(VOL_SIZE_MAX_MULT, VOL_SIZE_BASELINE_ATR_PCT / atr_pct))


def cost_check(notional):
    fees = notional * FEE_RATE_PER_SIDE * 2
    slippage = notional * ROUND_TRIP_SLIPPAGE_PCT / 100
    gross_tp = notional * TP_PERCENT / 100
    net_tp = gross_tp - fees - slippage
    return net_tp >= MIN_NET_TP_USDT, {"gross_tp": gross_tp, "fees": fees, "slippage": slippage, "net_tp": net_tp}


def analyze(symbol, oi_change=None):
    info = session_info()
    cs = confirmed(candles(symbol, BAR, 180))
    ts = confirmed(candles(symbol, TREND_BAR, 80))
    empty = {"signal": "NONE", "score": 0, "required_score": min(MIN_SCORE, 5), "session": info["name"],
             "priority_session": info["priority"], "trend15": "flat", "blockers": ["DATA"]}
    if len(cs) < 50 or len(ts) < 22:
        return {**empty, "block_reason": "Not enough confirmed candles"}
    values = [x["close"] for x in cs]
    i = len(values) - 1
    e20, r14, av = ema(values, 20), rsi(values, 14), atr(cs)
    ml, ms = macd(values)
    vw, adx_v = session_vwap(cs), adx(cs)
    if any(x is None for x in (e20[i], r14[i], av, ml[i], ms[i], vw)):
        return {**empty, "block_reason": "Indicator unavailable"}
    atr_pct = av / values[i] * 100
    avg_vol = sum(x["volume"] for x in cs[-21:-1]) / 20
    vol_ratio = cs[i]["volume"] / avg_vol if avg_vol else Decimal("0")
    tr = trend(ts)
    pa = price_action(cs)
    flow_ok = sum([adx_v >= ADX_MIN, vol_ratio >= VOLUME_MULT, atr_pct >= ATR_MIN_PCT]) >= 2
    buy_structure = pa["sr"] in ("SUPPORT", "BOTH") and pa["sweep"] == "BULLISH" and pa["reclaim"] == "BULLISH"
    sell_structure = pa["sr"] in ("RESISTANCE", "BOTH") and pa["sweep"] == "BEARISH" and pa["reclaim"] == "BEARISH"
    # These six points are intentionally distinct. PA completeness is the hard gate,
    # not an extra duplicated score point on top of BOS and RETEST.
    buy_points = [tr == "bull", buy_structure, pa["bos"] == "BULLISH", pa["retest"] == "BULLISH", flow_ok,
                  values[i] > e20[i] and values[i] > vw and ml[i] > ms[i]]
    sell_points = [tr == "bear", sell_structure, pa["bos"] == "BEARISH", pa["retest"] == "BEARISH", flow_ok,
                   values[i] < e20[i] and values[i] < vw and ml[i] < ms[i]]
    buy_score, sell_score = sum(buy_points), sum(sell_points)
    direction = "buy" if pa["buy_complete"] and not pa["sell_complete"] else "sell" if pa["sell_complete"] and not pa["buy_complete"] else "buy" if pa["buy_complete"] and buy_score >= sell_score else "sell" if pa["sell_complete"] else "none"
    funding = funding_snapshot(symbol)
    funding_rate, funding_threshold = funding.get("current"), funding.get("threshold")
    blockers = []
    if not info["active"]: blockers.append("SESSION")
    if oi_change is not None and oi_change < -OI_UNWIND_PCT: blockers.append("OI")
    if funding_rate is not None and abs(funding_rate) > funding_threshold: blockers.append("FUNDING")
    if direction == "none": blockers.append("PA")
    score = max(buy_score, sell_score)
    required = min(max(MIN_SCORE, 5), 5)
    if direction != "none" and score < required: blockers.append("SCORE")
    signal = "BUY" if direction == "buy" and buy_score >= required else "SELL" if direction == "sell" and sell_score >= required else "NONE"
    if signal == "BUY" and (tr == "bear" or ml[i] < ms[i]): blockers.append("VETO"); signal = "NONE"
    if signal == "SELL" and (tr == "bull" or ml[i] > ms[i]): blockers.append("VETO"); signal = "NONE"
    mult = volatility_multiplier(atr_pct)
    preview = MARGIN_USDT * mult * LEVERAGE
    fee_ok, costs = cost_check(preview)
    if signal != "NONE" and not fee_ok: blockers.append("COST"); signal = "NONE"
    if not blockers and signal == "NONE": blockers.append("NO_SIGNAL")
    missing_buy = [x["name"] for x in pa["stages_buy"] if not x["ok"]]
    missing_sell = [x["name"] for x in pa["stages_sell"] if not x["ok"]]
    reason = (f"PA_BUY={pa['pa_buy']}/5 missing_buy={','.join(missing_buy) or '-'} | "
              f"PA_SELL={pa['pa_sell']}/5 missing_sell={','.join(missing_sell) or '-'} | "
              f"trend={tr} | ADX={fmt(adx_v,2)} | VOL={fmt(vol_ratio,2)}x | ATR={fmt(atr_pct,3)}% | "
              f"funding={fmt(funding_rate,4) if funding_rate is not None else 'NA'}%/"
              f"thr={fmt(funding_threshold,4)}% | "
              f"score={score}/6 | blockers={','.join(blockers)}")
    return {"signal": signal, "score": score, "buy": buy_score, "sell": sell_score, "max_score": 6,
            "required_score": required, "session": info["name"], "priority_session": info["priority"],
            "trend15": tr, "entry": values[i], "rsi14": r14[i], "ema20": e20[i], "macd": ml[i],
            "macd_signal": ms[i], "vwap": vw, "adx": adx_v, "atr_pct": atr_pct, "volume_ratio": vol_ratio,
            "pa_score": max(pa["pa_buy"], pa["pa_sell"]), "pa_buy": pa["pa_buy"], "pa_sell": pa["pa_sell"],
            "pa_complete_buy": pa["buy_complete"], "pa_complete_sell": pa["sell_complete"],
            "gate_stages_buy": pa["stages_buy"], "gate_stages_sell": pa["stages_sell"],
            "funding_rate": fmt(funding_rate, 6) if funding_rate is not None else None,
            "funding_threshold": fmt(funding_threshold, 6),
            "blockers": blockers, "costs": {k: fmt(v, 6) for k, v in costs.items()},
            "fee_buffer_ok": fee_ok, "reason": reason,
            "block_reason": "; ".join(blockers) if blockers else None,
            "oi_change_pct": oi_change}


# --------------------------- Execution ---------------------------

def set_leverage(symbol):
    payload = {"instId": symbol, "lever": fmt(LEVERAGE), "mgnMode": TD_MODE}
    if position_mode == "long_short_mode":
        for side in ("long", "short"):
            p = dict(payload); p["posSide"] = side
            private_request("POST", "/api/v5/account/set-leverage", p)
    else:
        private_request("POST", "/api/v5/account/set-leverage", payload)


def calculate_size(symbol, price, atr_pct):
    info = instrument(symbol)
    if info["state"] not in ("live", "preopen"):
        raise RuntimeError(f"Instrument state is {info['state']}")
    mult = volatility_multiplier(atr_pct)
    raw = MARGIN_USDT * mult * LEVERAGE / (info["ctVal"] * price)
    size = floor_step(raw, info["lotSz"])
    if size < info["minSz"]:
        raise RuntimeError(f"Minimum contract size not met: {fmt(size)} < {fmt(info['minSz'])}")
    return size, size * info["ctVal"] * price, mult


def initial_sl_tp(side, entry, tick):
    if side == "buy":
        sl = floor_step(entry * (1 - SL_PERCENT / 100), tick)
        tp = floor_step(entry * (1 + TP_PERCENT / 100), tick)
        sl = max(sl, ceil_step(entry * (1 - MAX_SL_DISTANCE_PCT / 100), tick))
    else:
        sl = ceil_step(entry * (1 + SL_PERCENT / 100), tick)
        tp = ceil_step(entry * (1 - TP_PERCENT / 100), tick)
        sl = min(sl, floor_step(entry * (1 + MAX_SL_DISTANCE_PCT / 100), tick))
    return sl, tp


def liquidity_ok(symbol, side, notional):
    bids, asks = orderbook(symbol, ORDERBOOK_LEVELS)
    levels = asks if side == "buy" else bids
    info = instrument(symbol)
    depth = sum(px * sz * info["ctVal"] for px, sz in levels)
    return depth >= notional * MIN_SIDE_DEPTH_MULT, depth


def place_oco(symbol, side, sl, tp, tick):
    close_side = "sell" if side == "buy" else "buy"
    payload = {"instId": symbol, "tdMode": TD_MODE, "side": close_side, "ordType": "oco",
               "reduceOnly": True, "closeFraction": "1", "tpTriggerPx": fmt(tp), "tpOrdPx": "-1",
               "tpTriggerPxType": "mark", "slTriggerPx": fmt(sl), "slOrdPx": "-1",
               "slTriggerPxType": "mark", "algoClOrdId": "p" + uuid.uuid4().hex[:30]}
    payload["posSide"] = "net" if position_mode == "net" else ("long" if side == "buy" else "short")
    if side == "buy":
        payload["slTriggerPx"] = fmt(floor_step(sl, tick)); payload["tpTriggerPx"] = fmt(floor_step(tp, tick))
    else:
        payload["slTriggerPx"] = fmt(ceil_step(sl, tick)); payload["tpTriggerPx"] = fmt(ceil_step(tp, tick))
    result = private_request("POST", "/api/v5/trade/order-algo", payload)
    row = (result.get("data") or [{}])[0]
    if row.get("sCode") not in (None, "", "0"):
        raise RuntimeError(f"OCO rejected {row.get('sCode')}: {row.get('sMsg')}")
    if not row.get("algoId"):
        raise RuntimeError("OCO response has no algoId")
    return row["algoId"]


def pending_algo(symbol):
    return private_request("GET", "/api/v5/trade/orders-algo-pending", params={"instType": "SWAP", "instId": symbol, "ordType": "oco"}).get("data", [])


def cancel_algos(symbol):
    rows = pending_algo(symbol)
    ids = [str(x["algoId"]) for x in rows if x.get("algoId")]
    if ids:
        private_request("POST", "/api/v5/trade/cancel-algos", [{"instId": symbol, "algoId": x} for x in ids])


def emergency_close(symbol, pos):
    payload = {"instId": symbol, "mgnMode": TD_MODE, "autoCxl": True}
    if position_mode == "long_short_mode": payload["posSide"] = pos.get("posSide", "")
    return private_request("POST", "/api/v5/trade/close-position", payload)


def place_order(symbol, analysis, snapshot):
    if not AUTO_TRADE:
        return {"status": "BLOCKED", "reason": "AUTO_TRADE=false"}
    if not DEMO and not ALLOW_LIVE:
        return {"status": "BLOCKED", "reason": "ALLOW_LIVE=false"}
    if analysis.get("signal") not in ("BUY", "SELL"):
        return {"status": "NO_TRADE", "reason": "Signal not approved"}
    # Revalidate on fresh candles so a stale signal cannot become an entry.
    try:
        fresh = analyze(symbol, analysis.get("oi_change_pct"))
        if fresh.get("signal") != analysis.get("signal"):
            return {"status": "BLOCKED", "reason": f"STALE_SIGNAL fresh={fresh.get('signal')} old={analysis.get('signal')}"}
        analysis = fresh
    except Exception as exc:
        return {"status": "BLOCKED", "reason": f"REVALIDATION_ERROR: {exc}"}
    ok, why = risk_check()
    if not ok:
        return {"status": "BLOCKED", "reason": "RISK:" + why}
    if positions_for(symbol, snapshot):
        return {"status": "BLOCKED", "reason": "Existing position"}
    with order_lock:
        fresh = refresh_positions()
        if positions_for(symbol, fresh):
            return {"status": "BLOCKED", "reason": "Position appeared before order"}
        corr_count = correlated_open_count(symbol, fresh)
        if corr_count >= MAX_CORRELATED_POSITIONS:
            return {"status": "BLOCKED",
                    "reason": f"CORRELATION_LIMIT open={corr_count} max={MAX_CORRELATED_POSITIONS}"}
        current = total_open_notional(fresh)
        price = ticker(symbol)
        size, requested_notional, mult = calculate_size(symbol, price, analysis.get("atr_pct"))
        cost_ok, costs = cost_check(requested_notional)
        if not cost_ok:
            return {"status": "BLOCKED", "reason": "COST:" + json.dumps({k: fmt(v, 6) for k, v in costs.items()})}
        if current + requested_notional > MAX_TOTAL_NOTIONAL_USDT:
            return {"status": "BLOCKED", "reason": f"EXPOSURE {fmt(current)}+{fmt(requested_notional)}>{fmt(MAX_TOTAL_NOTIONAL_USDT)}"}
        liquid, depth = liquidity_ok(symbol, "buy" if analysis["signal"] == "BUY" else "sell", requested_notional)
        if not liquid:
            return {"status": "BLOCKED", "reason": f"LIQUIDITY depth={fmt(depth, 4)}"}
        side = "buy" if analysis["signal"] == "BUY" else "sell"
        info = instrument(symbol)
        set_leverage(symbol)
        payload = {"instId": symbol, "tdMode": TD_MODE, "side": side, "sz": fmt(size),
                   "clOrdId": "bot" + uuid.uuid4().hex[:24], "ordType": ENTRY_ORDER_TYPE}
        if position_mode == "long_short_mode": payload["posSide"] = "long" if side == "buy" else "short"
        if ENTRY_ORDER_TYPE == "ioc":
            bids, asks = orderbook(symbol, 1)
            best = asks[0][0] if side == "buy" else bids[0][0]
            slip = MAX_ENTRY_SLIPPAGE_PCT / 100
            px = best * (1 + slip) if side == "buy" else best * (1 - slip)
            payload["px"] = fmt(floor_step(px, info["tickSz"]) if side == "buy" else ceil_step(px, info["tickSz"]))
        result = private_request("POST", "/api/v5/trade/order", payload)
        row = (result.get("data") or [{}])[0]
        if row.get("sCode") not in (None, "", "0"):
            raise RuntimeError(f"ORDER REJECTED {row.get('sCode')}: {row.get('sMsg')}")
        order_id = row.get("ordId")
        if not order_id:
            raise RuntimeError("Order response has no ordId")
        order_state = None
        for _ in range(10):
            time.sleep(1)
            rows = private_request("GET", "/api/v5/trade/order", params={"instId": symbol, "ordId": order_id}).get("data", [])
            if rows:
                order_state = rows[0].get("state")
                if order_state in ("filled", "partially_filled", "canceled"):
                    break
        fresh = refresh_positions()
        pos_rows = positions_for(symbol, fresh)
        if not pos_rows:
            return {"status": "NOT_FILLED", "order_state": order_state, "ordId": order_id}
        pos = pos_rows[0]
        entry = dec(pos.get("avgPx") or price)
        filled_size = abs(dec(pos.get("pos", "0")))
        filled_notional = position_notional(symbol, pos)
        sl, tp = initial_sl_tp(side, entry, info["tickSz"])
        try:
            cancel_algos(symbol)
            algo_id = place_oco(symbol, side, sl, tp, info["tickSz"])
        except Exception as exc:
            alert(f"Protection failed after entry: {exc}. Emergency close required.", "CRITICAL", symbol)
            try:
                emergency_close(symbol, pos)
                return {"status": "EMERGENCY_CLOSED", "reason": str(exc), "ordId": order_id}
            except Exception as close_exc:
                kill(f"UNPROTECTED_POSITION {symbol}: {close_exc}")
                return {"status": "CRITICAL_UNPROTECTED", "reason": str(close_exc), "ordId": order_id}
        db_trade_open(symbol, side, pos.get("posSide", "net"), entry, sl, tp, size, filled_size,
                      requested_notional, filled_notional, order_id)
        with state_lock:
            state.setdefault(position_key(symbol, pos), {}).update({"entry_price": entry, "current_sl": sl,
                "current_tp": tp, "position_size": filled_size, "protection": "ACTIVE", "algo_id": algo_id,
                "step_level": 0, "trade_status": "OPEN"})
        alert(f"OPEN {side.upper()} {symbol} entry={fmt(entry)} size={fmt(filled_size)} SL={fmt(sl)} TP={fmt(tp)} netTP≈{fmt(costs['net_tp'], 4)}", "INFO", symbol)
        return {"status": "ORDER_AND_PROTECTION_ACTIVE", "ordId": order_id, "algoId": algo_id,
                "filled_size": fmt(filled_size), "entry": fmt(entry), "sl": fmt(sl), "tp": fmt(tp)}


def manage_position(symbol, pos):
    avg = dec(pos.get("avgPx", "0"))
    if avg <= 0:
        return
    key = position_key(symbol, pos)
    side = position_side(pos)
    price = mark_price(symbol)
    info = instrument(symbol)
    with state_lock:
        saved = state.get(key, {}).copy()
    default_sl, default_tp = initial_sl_tp(side, avg, info["tickSz"])
    sl = dec(str(saved.get("current_sl") or default_sl))
    tp = dec(str(saved.get("current_tp") or default_tp))
    # On restart, recover the exchange's current OCO triggers before applying new trailing logic.
    for algo in (pos.get("closeOrderAlgo") or []):
        if str(algo.get("closeFraction", "")) == "1":
            if algo.get("slTriggerPx"): sl = dec(algo["slTriggerPx"])
            if algo.get("tpTriggerPx"): tp = dec(algo["tpTriggerPx"])
            break
    step = int(saved.get("step_level", 0))
    profit = (price - avg) / avg * 100 if side == "buy" else (avg - price) / avg * 100
    if profit >= BREAK_EVEN_TRIGGER_PCT:
        be = avg * (1 + BREAK_EVEN_OFFSET_PCT / 100) if side == "buy" else avg * (1 - BREAK_EVEN_OFFSET_PCT / 100)
        be = floor_step(be, info["tickSz"]) if side == "buy" else ceil_step(be, info["tickSz"])
        sl = max(sl, be) if side == "buy" else min(sl, be)
    if profit >= TRAIL_START_PCT:
        tr = price * (1 - TRAIL_DISTANCE_PCT / 100) if side == "buy" else price * (1 + TRAIL_DISTANCE_PCT / 100)
        tr = floor_step(tr, info["tickSz"]) if side == "buy" else ceil_step(tr, info["tickSz"])
        sl = max(sl, tr) if side == "buy" else min(sl, tr)
    achieved = int((profit / STEP_TRIGGER_PCT).to_integral_value(rounding=ROUND_DOWN)) if profit > 0 else 0
    if achieved > step:
        step = achieved
        tp = max(tp, floor_step(avg * (1 + (step + 1) * STEP_TRIGGER_PCT / 100), info["tickSz"])) if side == "buy" else min(tp, ceil_step(avg * (1 - (step + 1) * STEP_TRIGGER_PCT / 100), info["tickSz"]))
    try:
        current_algos = pending_algo(symbol)
        old_sl = dec(str(saved.get("current_sl") or sl))
        old_tp = dec(str(saved.get("current_tp") or tp))
        changed = abs(sl - old_sl) >= info["tickSz"] or abs(tp - old_tp) >= info["tickSz"]
        if not current_algos or changed:
            cancel_algos(symbol)
            place_oco(symbol, side, sl, tp, info["tickSz"])
        with state_lock:
            state.setdefault(key, {}).update({"entry_price": avg, "mark_price": price, "profit_pct": profit,
                "current_sl": sl, "current_tp": tp, "position_size": abs(dec(pos.get("pos", "0"))),
                "protection": "ACTIVE", "step_level": step, "trade_status": "MANAGED"})
    except Exception as exc:
        alert(f"Protection management failed: {exc}. Emergency close required.", "CRITICAL", symbol)
        try:
            emergency_close(symbol, pos)
        except Exception as close_exc:
            kill(f"EMERGENCY_CLOSE_FAILED {symbol}: {close_exc}")


def positions_history(symbol):
    try:
        return private_request("GET", "/api/v5/account/positions-history", params={"instType": "SWAP", "instId": symbol, "limit": "20"}).get("data", [])
    except Exception as exc:
        log(f"POSITION HISTORY WARNING | {symbol} | {exc}")
        return []


def reconcile_closed(prev, new):
    for symbol, old_rows in prev.items():
        new_keys = {position_key(symbol, x) for x in new.get(symbol, [])}
        for old in old_rows:
            if position_key(symbol, old) in new_keys:
                continue
            pos_side = old.get("posSide", "net")
            hist = [x for x in positions_history(symbol) if x.get("posSide", "net") == pos_side]
            h = hist[0] if hist else {}
            exit_px = dec(h.get("closeAvgPx") or mark_price(symbol))
            pnl = dec(h["realizedPnl"]) if h.get("realizedPnl") not in (None, "") else None
            fee = dec(h.get("fee", "0")) if h.get("fee") not in (None, "") else Decimal("0")
            funding = dec(h.get("fundingFee", "0")) if h.get("fundingFee") not in (None, "") else Decimal("0")
            if pnl is None:
                side = position_side(old); avg = dec(old.get("avgPx", "0")); sz = abs(dec(old.get("pos", "0"))); cv = instrument(symbol)["ctVal"]
                pnl = (exit_px - avg) * sz * cv if side == "buy" else (avg - exit_px) * sz * cv
                reason = "estimated_mark_pnl"
            else:
                reason = "exchange_realized_pnl"
            db_trade_close(symbol, pos_side, exit_px, pnl, fee, funding, reason)
            # Only exchange-reported realized PnL may update circuit-breaker loss counts.
            if reason == "exchange_realized_pnl":
                record_result(pnl)
            alert(f"CLOSED {symbol} exit={fmt(exit_px)} realizedPnL={fmt(pnl, 6)} fee={fmt(fee, 6)} funding={fmt(funding, 6)} source={reason}", "INFO", symbol)


# --------------------------- Worker / web ---------------------------

def validate_config():
    problems = []
    if str(MIN_SCORE_RAW).strip() != str(MIN_SCORE):
        log(f"CONFIG WARNING | MIN_SCORE={MIN_SCORE_RAW!r} is invalid; using safe value MIN_SCORE=5")
    if not SYMBOLS: problems.append("SYMBOLS is empty")
    if MARGIN_USDT <= 0 or LEVERAGE <= 0: problems.append("MARGIN_USDT and LEVERAGE must be positive")
    if SL_PERCENT <= 0 or TP_PERCENT <= 0 or SL_PERCENT > MAX_SL_DISTANCE_PCT: problems.append("Invalid SL/TP configuration")
    if FEE_RATE_PER_SIDE < 0 or ROUND_TRIP_SLIPPAGE_PCT < 0 or MIN_NET_TP_USDT < 0: problems.append("Invalid cost configuration")
    if ENTRY_ORDER_TYPE not in ("ioc", "market"): problems.append("ENTRY_ORDER_TYPE must be ioc or market")
    if not (1 <= MIN_SCORE <= 5): problems.append("MIN_SCORE must be between 1 and 5")
    if not (0 < DAILY_MAX_LOSS_PCT <= 50 and 0 < MAX_DRAWDOWN_PCT <= 80): problems.append("Invalid risk limits")
    if AUTO_TRADE and (not API_KEY or not SECRET_KEY or not PASSPHRASE): problems.append("AUTO_TRADE requires all OKX credentials")
    if not DEMO and not ALLOW_LIVE and AUTO_TRADE: problems.append("Live auto trading requires ALLOW_LIVE=true")
    if problems:
        raise RuntimeError("CONFIG INVALID: " + " | ".join(problems))


def startup():
    # Initialize persistence first so configuration/startup failures are recorded cleanly.
    db_init()
    validate_config()
    risk_init()
    sync_okx_time()
    refresh_position_mode()
    private_request("GET", "/api/v5/account/balance")
    refresh_positions()
    for symbol in SYMBOLS:
        instrument(symbol)
    log(f"{VERSION} started | DEMO={DEMO} AUTO_TRADE={AUTO_TRADE} BASE_URL={BASE_URL} SYMBOLS={SYMBOLS}")
    alert(f"Started {VERSION}; DEMO={DEMO}; AUTO_TRADE={AUTO_TRADE}", "INFO")


def worker():
    global worker_started, worker_error
    while True:
        try:
            startup(); worker_started = True; worker_error = ""; break
        except Exception as exc:
            worker_started = False; worker_error = str(exc)
            log(f"STARTUP RETRY | {type(exc).__name__}: {exc}")
            db_event("ERROR", "", f"STARTUP RETRY {exc}")
            time.sleep(max(10, POLL_SECONDS))
    previous = {}
    oi_cache = {}
    oi_sample_ts = {}
    while True:
        started = time.time(); maybe_resync_time()
        try:
            snapshot = refresh_positions()
        except Exception as exc:
            log(f"POSITION SNAPSHOT ERROR | {exc}"); snapshot = dict(position_snapshot)
        try:
            reconcile_closed(previous, snapshot)
        except Exception as exc:
            log(f"RECONCILIATION ERROR | {exc}")
        previous = {s: list(rows) for s, rows in snapshot.items()}
        for symbol in SYMBOLS:
            try:
                rows = positions_for(symbol, snapshot)
                if rows:
                    for pos in rows: manage_position(symbol, pos)
                    continue
                now = time.time()
                oi_change = oi_cache.get(symbol)
                if now - oi_sample_ts.get(symbol, 0) >= OI_SAMPLE_SECONDS:
                    oi_rows = public_get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": symbol}).get("data", [])
                    if oi_rows:
                        current = dec(oi_rows[0].get("oiCcy") or oi_rows[0].get("oi") or "0")
                        old = state.get(symbol, {}).get("oi_prev")
                        if old not in (None, Decimal("0")):
                            oi_change = (current - old) / old * 100
                            oi_cache[symbol] = oi_change
                        with state_lock: state.setdefault(symbol, {})["oi_prev"] = current
                    oi_sample_ts[symbol] = now
                analysis = analyze(symbol, oi_change)
                with state_lock:
                    state.setdefault(symbol, {}).update(analysis)
                    state[symbol]["last_checked"] = datetime.now(PKT_TZ).strftime("%Y-%m-%d %H:%M:%S")
                    state[symbol]["trade_status"] = "SIGNAL" if analysis["signal"] in ("BUY", "SELL") else "NO TRADE"
                log(f"{symbol}: {analysis['signal']} {analysis['score']}/{analysis['max_score']} | {analysis['reason']}")
                if analysis["signal"] in ("BUY", "SELL") and analysis["score"] >= analysis["required_score"]:
                    result = place_order(symbol, analysis, snapshot)
                    log("TRADE RESULT | " + json.dumps(result, default=str))
                    with state_lock:
                        state.setdefault(symbol, {})["trade_result"] = result
                        state[symbol]["trade_status"] = result.get("status", "UNKNOWN")
                        if result.get("status") not in ("ORDER_AND_PROTECTION_ACTIVE",):
                            state[symbol]["block_reason"] = result.get("reason", "EXECUTION_BLOCK")
            except Exception as exc:
                log(f"{symbol} ERROR | {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                db_event("ERROR", symbol, str(exc))
                with state_lock:
                    state.setdefault(symbol, {}).update({"trade_status": "ERROR", "trade_error": str(exc), "block_reason": "ERROR"})
        time.sleep(max(1, POLL_SECONDS - int(time.time() - started)))


HTML = """<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>OKX Bot V15</title><style>body{font-family:Arial;background:#0b0d10;color:#eee;margin:14px}.card{background:#15191f;border:1px solid #2b313a;border-radius:8px;padding:10px;margin-bottom:10px}table{width:100%;border-collapse:collapse;font-size:12px;min-width:1300px}th,td{padding:7px;border-bottom:1px solid #2b313a;text-align:left;white-space:nowrap}th{background:#171b21;position:sticky;top:0}.bad{color:#ff6565}.good{color:#45d483}.warn{color:#f2b84b}.wrap{overflow:auto}</style></head><body><h2>OKX Bot V15 — Cost-Aware Audit View</h2><div id='top'></div><div class='wrap'><table><thead><tr><th>Symbol</th><th>Signal</th><th>Score</th><th>Trend</th><th>Session</th><th>Blockers</th><th>Reason</th><th>Entry</th><th>Mark</th><th>P/L%</th><th>SL</th><th>TP</th><th>Status</th></tr></thead><tbody id='rows'></tbody></table></div><script>const e=x=>String(x??'-').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));async function refresh(){try{const s=await fetch('/api/status').then(r=>r.json());document.getElementById('top').innerHTML='<div class="card">Mode: <b>'+e(s.mode)+'</b> | Auto: <b>'+e(s.auto_trade)+'</b> | Risk Halted: <b class="'+(s.trading_halted?'bad':'good')+'">'+e(s.trading_halted)+'</b> '+e(s.halt_reason||'')+' | Exposure: '+e(s.exposure)+' / '+e(s.max_exposure)+' | Worker: '+e(s.status)+' | Error: '+e(s.worker_error||'-')+'</div>';let h='';for(const [sym,x] of Object.entries(s.symbols||{})){h+='<tr><td>'+e(sym)+'</td><td>'+e(x.signal)+'</td><td>'+e(x.score)+'/'+e(x.max_score)+' need '+e(x.required_score)+'</td><td>'+e(x.trend15)+'</td><td>'+e(x.session)+'</td><td class="warn">'+e((x.blockers||[]).join(','))+'</td><td>'+e(x.reason||x.block_reason)+'</td><td>'+e(x.entry_price||x.entry)+'</td><td>'+e(x.mark_price)+'</td><td>'+e(x.profit_pct)+'</td><td>'+e(x.current_sl)+'</td><td>'+e(x.current_tp)+'</td><td>'+e(x.trade_status)+'</td></tr>'}document.getElementById('rows').innerHTML=h}catch(err){document.getElementById('top').textContent='Dashboard error: '+err}}refresh();setInterval(refresh,5000)</script></body></html>"""


@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


@app.get("/api/status")
def api_status():
    with state_lock:
        symbols = {k: v.copy() for k, v in state.items() if k in SYMBOLS}
    with risk_lock:
        halted, reason = risk_state.get("trading_halted", False), risk_state.get("halt_reason", "")
    return jsonify({"bot": VERSION, "status": "running" if worker_started else "starting", "mode": "DEMO" if DEMO else "LIVE",
                    "auto_trade": AUTO_TRADE, "margin": str(MARGIN_USDT), "leverage": str(LEVERAGE),
                    "exposure": str(total_open_notional()), "max_exposure": str(MAX_TOTAL_NOTIONAL_USDT),
                    "trading_halted": halted, "halt_reason": reason, "worker_error": worker_error,
                    "position_snapshot_age": round(time.time() - position_snapshot_ts, 1) if position_snapshot_ts else None,
                    "symbols": symbols})


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "bot": VERSION, "worker_started": worker_started, "auto_trade": AUTO_TRADE, "demo": DEMO})


@app.post("/api/kill")
def api_kill():
    body = flask_request.get_json(silent=True) or {}
    if not KILL_SWITCH_TOKEN or body.get("token") != KILL_SWITCH_TOKEN:
        return jsonify({"status": "DENIED"}), 403
    kill("Manual kill switch")
    return jsonify({"status": "HALTED"})


@app.post("/api/resume")
def api_resume():
    body = flask_request.get_json(silent=True) or {}
    if not KILL_SWITCH_TOKEN or body.get("token") != KILL_SWITCH_TOKEN:
        return jsonify({"status": "DENIED"}), 403
    resume()
    return jsonify({"status": "RESUMED"})


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)
