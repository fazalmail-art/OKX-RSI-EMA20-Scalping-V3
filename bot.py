import os
import time
import json
import hmac
import base64
import hashlib
import threading
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, time as dtime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, Response
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# OKX SCALPING BOT V11
# Conservative vote/filter/veto engine.
# DEMO ONLY by default. No profitability guarantee.
# =========================================================

VERSION = "V12.1-FIXED"
BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"
# Safety: must be explicitly enabled in Railway/.env.
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

BAR = os.getenv("BAR", "5m")
TREND_BAR = os.getenv("TREND_BAR", "15m")
MARGIN_USDT = Decimal(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = Decimal(os.getenv("LEVERAGE", "3"))
TD_MODE = os.getenv("TD_MODE", "isolated")
MAX_TOTAL_NOTIONAL_USDT = Decimal(os.getenv("MAX_TOTAL_NOTIONAL_USDT", "530"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "10"))

SL_PERCENT = Decimal(os.getenv("SL_PERCENT", "0.50"))
TP_PERCENT = Decimal(os.getenv("TP_PERCENT", "0.80"))
BREAK_EVEN_TRIGGER_PCT = Decimal(os.getenv("BREAK_EVEN_TRIGGER_PCT", "0.30"))
BREAK_EVEN_OFFSET_PCT = Decimal(os.getenv("BREAK_EVEN_OFFSET_PCT", "0.05"))
TRAIL_START_PCT = Decimal(os.getenv("TRAIL_START_PCT", "0.50"))
TRAIL_DISTANCE_PCT = Decimal(os.getenv("TRAIL_DISTANCE_PCT", "0.30"))
PROTECTION_RETRY_SECONDS = int(os.getenv("PROTECTION_RETRY_SECONDS", "5"))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))
MAJOR_MIN_SCORE = int(os.getenv("MAJOR_MIN_SCORE", "6"))
NON_PRIORITY_MIN_SCORE = int(os.getenv("NON_PRIORITY_MIN_SCORE", "7"))

ADX_MIN = Decimal(os.getenv("ADX_MIN", "18"))
VOLUME_MULT = Decimal(os.getenv("VOLUME_MULT", "1.00"))
ATR_MIN_PCT = Decimal(os.getenv("ATR_MIN_PCT", "0.05"))
ATR_SL_MIN_PCT = Decimal(os.getenv("ATR_SL_MIN_PCT", "0.40"))
ATR_SL_MAX_PCT = Decimal(os.getenv("ATR_SL_MAX_PCT", "0.70"))
STRUCTURE_LOOKBACK = int(os.getenv("STRUCTURE_LOOKBACK", "20"))

FUNDING_EXTREME_PCT = Decimal(os.getenv("FUNDING_EXTREME_PCT", "0.03"))
FUNDING_LOOKBACK = int(os.getenv("FUNDING_LOOKBACK", "30"))
OI_UNWIND_PCT = Decimal(os.getenv("OI_UNWIND_PCT", "0.30"))

# 0.13 USDT is a real buffer. It is checked against estimated round-trip fees,
# rather than being a meaningless static pass/fail number.
FEE_BUFFER_USDT = Decimal(os.getenv("FEE_BUFFER_USDT", "0.13"))
FEE_RATE_PER_SIDE = Decimal(os.getenv("FEE_RATE_PER_SIDE", "0.0005"))

# Execution / liquidity safety.
ENTRY_ORDER_TYPE = os.getenv("ENTRY_ORDER_TYPE", "ioc").lower()  # ioc only is recommended
MAX_ENTRY_SLIPPAGE_PCT = Decimal(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.20"))
MIN_SIDE_DEPTH_MULT = Decimal(os.getenv("MIN_SIDE_DEPTH_MULT", "3.0"))
ORDERBOOK_LEVELS = int(os.getenv("ORDERBOOK_LEVELS", "5"))
ALLOW_MARKET_ENTRY = os.getenv("ALLOW_MARKET_ENTRY", "false").lower() == "true"

# Public data cache TTLs.
INSTRUMENT_CACHE_SECONDS = int(os.getenv("INSTRUMENT_CACHE_SECONDS", "3600"))
FUNDING_CACHE_SECONDS = int(os.getenv("FUNDING_CACHE_SECONDS", "300"))

PKT_TZ = ZoneInfo("Asia/Karachi")

# Selected PKT entry windows. Existing positions are managed outside them.
# Priority sub-windows are the first hour of each major session.
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

# Meme coins are NOT given a lower threshold.
MEME_SYMBOLS = {"DOGE-USDT-SWAP", "SHIB-USDT-SWAP"}

app = Flask(__name__)
session = requests.Session()
state = {}
state_lock = threading.Lock()
order_lock = threading.Lock()
server_offset_ms = 0
worker_started = False
worker_error = ""
position_snapshot_ts = 0.0
position_snapshot = {}
instrument_cache = {}
funding_cache = {}

TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
NON_RETRY_CODES = {"50123", "50113", "50114", "50013", "50119"}


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
    global server_offset_ms
    before = int(time.time() * 1000)
    data = public_get("/api/v5/public/time")
    after = int(time.time() * 1000)
    server_ms = int(data["data"][0]["ts"])
    server_offset_ms = server_ms - ((before + after) // 2)
    log(f"OKX TIME SYNCED | offset_ms={server_offset_ms}")


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
            code = str(data.get("code", ""))
            if response.status_code >= 400:
                if response.status_code not in TRANSIENT_HTTP or attempt == retries:
                    raise RuntimeError(f"OKX PRIVATE {response.status_code}: {data}")
                last = RuntimeError(f"HTTP {response.status_code}: {data}")
            elif code != "0":
                # Permission/auth/config errors must NOT be retried.
                if code in NON_RETRY_CODES or attempt == retries:
                    raise RuntimeError(f"OKX PRIVATE ERROR {code}: {data.get('msg')}")
                # Most business errors are also not worth retrying.
                raise RuntimeError(f"OKX PRIVATE ERROR {code}: {data.get('msg')}")
            else:
                return data
        except RuntimeError as exc:
            last = exc
            text = str(exc)
            if "50123" in text or "50113" in text or "50114" in text or "permission" in text.lower():
                raise
            if attempt == retries:
                raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
            if attempt == retries:
                raise
        if attempt < retries:
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


def is_bullish_candle(candle):
    return candle["close"] > candle["open"]


def is_bearish_candle(candle):
    return candle["close"] < candle["open"]


def candle_body_ratio(candle):
    rng = candle["high"] - candle["low"]
    if rng <= 0:
        return Decimal("0")
    return abs(candle["close"] - candle["open"]) / rng


def rejection_candle(candle, side):
    rng = candle["high"] - candle["low"]
    if rng <= 0:
        return False
    body = abs(candle["close"] - candle["open"])
    upper = candle["high"] - max(candle["open"], candle["close"])
    lower = min(candle["open"], candle["close"]) - candle["low"]
    if side == "buy":
        return lower >= body * Decimal("1.2") and lower >= rng * Decimal("0.35")
    return upper >= body * Decimal("1.2") and upper >= rng * Decimal("0.35")


def structure_levels(candles, lookback=STRUCTURE_LOOKBACK):
    window = candles[-lookback-1:-1] if len(candles) > lookback + 1 else candles[:-1]
    if not window:
        return None, None
    return min(x["low"] for x in window), max(x["high"] for x in window)


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
    cached = instrument_cache.get(symbol)
    if cached and not force and now - cached["ts"] < INSTRUMENT_CACHE_SECONDS:
        return cached["data"]
    data = public_get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol})
    rows = data.get("data", [])
    if not rows:
        raise RuntimeError(f"Instrument not found: {symbol}")
    x = rows[0]
    result = {"ctVal": dec(x["ctVal"]), "lotSz": dec(x["lotSz"]), "minSz": dec(x["minSz"]), "tickSz": dec(x["tickSz"]), "state": x.get("state", "")}
    instrument_cache[symbol] = {"ts": now, "data": result}
    return result


def get_funding_snapshot(symbol):
    now = time.time()
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


def calculate_order_size(symbol, price):
    info = get_instrument(symbol)
    if info["state"] not in ("live", "preopen"):
        raise RuntimeError(f"Instrument not live: {info['state']}")
    target = MARGIN_USDT * LEVERAGE
    raw = target / (info["ctVal"] * price)
    size = floor_step(raw, info["lotSz"])
    if size < info["minSz"]:
        minimum_notional = info["minSz"] * info["ctVal"] * price
        raise RuntimeError(f"{symbol}: margin ${fmt(MARGIN_USDT,2)} cannot meet minimum contract size; minimum notional≈${fmt(minimum_notional,2)}")
    return size, size * info["ctVal"] * price


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


def calculate_initial_sl_tp(side, entry, tick, atr_pct=None, support=None, resistance=None):
    # Hard risk band: structure can tighten the stop, never widen it beyond the cap.
    risk_pct = SL_PERCENT
    if atr_pct is not None and dec(atr_pct) > 0:
        risk_pct = max(ATR_SL_MIN_PCT, min(ATR_SL_MAX_PCT, dec(atr_pct) * Decimal("1.20")))
    risk_pct = max(Decimal("0.05"), min(ATR_SL_MAX_PCT, risk_pct))
    if side == "buy":
        atr_sl = entry * (Decimal("1") - risk_pct / Decimal("100"))
        sl = atr_sl
        if support is not None and support < entry:
            sl = max(atr_sl, support)
        tp = entry * (Decimal("1") + TP_PERCENT / Decimal("100"))
        sl = floor_step(sl, tick); tp = floor_step(tp, tick)
    else:
        atr_sl = entry * (Decimal("1") + risk_pct / Decimal("100"))
        sl = atr_sl
        if resistance is not None and resistance > entry:
            sl = min(atr_sl, resistance)
        tp = entry * (Decimal("1") - TP_PERCENT / Decimal("100"))
        sl = ceil_step(sl, tick); tp = ceil_step(tp, tick)
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
    # OKX requires posSide=net for closeFraction=1 in net mode; long/short
    # mode uses the actual position side. No literal "net" is sent to normal orders.
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


def emergency_close(symbol):
    payload = {"instId": symbol, "mgnMode": TD_MODE, "autoCxl": True}
    if position_mode == "long_short_mode":
        pos = get_cached_position(symbol)
        if pos:
            payload["posSide"] = pos.get("posSide", "")
    return private_request("POST", "/api/v5/trade/close-position", payload=payload)


def liquidity_guard(symbol, side, target_notional):
    bids, asks = get_orderbook(symbol, ORDERBOOK_LEVELS)
    levels = asks if side == "buy" else bids
    side_depth = sum(px * sz * get_instrument(symbol)["ctVal"] for px, sz in levels)
    return side_depth >= target_notional * MIN_SIDE_DEPTH_MULT, side_depth, levels[0][0]


def fee_buffer_ok(target_notional, tp_percent=None):
    tp_pct = TP_PERCENT if tp_percent is None else dec(tp_percent)
    gross = target_notional * tp_pct / Decimal("100")
    estimated_round_trip = target_notional * FEE_RATE_PER_SIDE * Decimal("2")
    required = estimated_round_trip + max(FEE_BUFFER_USDT, Decimal("0"))
    return gross >= required, gross, estimated_round_trip, required


def analyze(symbol, oi_change=None):
    info = session_info()
    cs = [x for x in get_candles(symbol, BAR, 180) if x["confirm"] == "1"]
    trend_cs = [x for x in get_candles(symbol, TREND_BAR, 80) if x["confirm"] == "1"]
    if len(cs) < 40 or len(trend_cs) < 22:
        return {"signal": "NONE", "score": 0, "max_score": 9, "required_score": 99, "session": info["name"], "priority_session": info["priority"], "reason": "Not enough candles"}

    values = [x["close"] for x in cs]
    i = len(values) - 1
    e20 = ema(values, 20)
    r14 = rsi(values, 14)
    atr_v = atr(cs)
    adx_v = adx(cs)
    macd_line, macd_sig = macd(values)
    vw = session_vwap(cs)
    trend15 = get_trend_from_candles(trend_cs)
    funding = get_funding_snapshot(symbol)
    if any(x is None for x in (e20[i], r14[i], atr_v, macd_line[i], macd_sig[i], vw)):
        return {"signal": "NONE", "score": 0, "max_score": 9, "required_score": 99, "session": info["name"], "priority_session": info["priority"], "reason": "Indicator unavailable"}

    avg_vol = sum(x["volume"] for x in cs[-21:-1]) / Decimal("20")
    volume_ratio = cs[i]["volume"] / avg_vol if avg_vol else Decimal("0")
    atr_pct = atr_v / values[i] * Decimal("100")

    buy = sell = 0
    votes = {}
    reasons = []
    if r14[i] > 50:
        buy += 1; votes["rsi50"] = "buy"
    elif r14[i] < 50:
        sell += 1; votes["rsi50"] = "sell"
    else: votes["rsi50"] = "none"
    if values[i] > e20[i]:
        buy += 1; votes["ema20"] = "buy"
    elif values[i] < e20[i]:
        sell += 1; votes["ema20"] = "sell"
    else: votes["ema20"] = "none"
    if e20[i] > e20[i-1]:
        buy += 1; votes["slope"] = "buy"
    elif e20[i] < e20[i-1]:
        sell += 1; votes["slope"] = "sell"
    else: votes["slope"] = "none"
    macd_bull = macd_line[i] > macd_sig[i]
    macd_bear = macd_line[i] < macd_sig[i]
    if macd_bull:
        buy += 1; votes["macd"] = "buy"
    elif macd_bear:
        sell += 1; votes["macd"] = "sell"
    else: votes["macd"] = "none"
    if values[i] > vw:
        buy += 1; votes["vwap"] = "buy"
    elif values[i] < vw:
        sell += 1; votes["vwap"] = "sell"
    else: votes["vwap"] = "none"
    if trend15 == "bull":
        buy += 1; votes["trend15"] = "buy"
    elif trend15 == "bear":
        sell += 1; votes["trend15"] = "sell"
    else: votes["trend15"] = "none"

    support, resistance = structure_levels(cs)
    last_candle = cs[i]
    bullish_candle_signal = is_bullish_candle(last_candle)
    bearish_candle_signal = is_bearish_candle(last_candle)
    bullish_rejection = rejection_candle(last_candle, "buy")
    bearish_rejection = rejection_candle(last_candle, "sell")
    if bullish_rejection and support is not None and last_candle["low"] <= support * Decimal("1.002"):
        buy += 1; votes["candle_structure"] = "buy"
    elif bearish_rejection and resistance is not None and last_candle["high"] >= resistance * Decimal("0.998"):
        sell += 1; votes["candle_structure"] = "sell"
    elif bullish_candle_signal and support is not None and last_candle["close"] > support:
        buy += 1; votes["candle_structure"] = "buy"
    elif bearish_candle_signal and resistance is not None and last_candle["close"] < resistance:
        sell += 1; votes["candle_structure"] = "sell"
    else:
        votes["candle_structure"] = "none"

    recent_high = max(x["high"] for x in cs[-21:-1])
    recent_low = min(x["low"] for x in cs[-21:-1])
    prev = cs[i-1]
    if prev["low"] < recent_low and prev["close"] > recent_low:
        buy += 1; votes["structure"] = "buy"
    elif prev["high"] > recent_high and prev["close"] < recent_high:
        sell += 1; votes["structure"] = "sell"
    else: votes["structure"] = "none"

    fp = funding.get("current")
    fthr = funding.get("threshold", FUNDING_EXTREME_PCT)
    if fp is not None and fp >= fthr:
        sell += 1; votes["funding"] = "sell"
    elif fp is not None and fp <= -fthr:
        buy += 1; votes["funding"] = "buy"
    else: votes["funding"] = "none"

    filters = {
        "adx": adx_v >= ADX_MIN,
        "volume": volume_ratio >= VOLUME_MULT,
        "atr": atr_pct >= ATR_MIN_PCT,
        "session": info["active"],
        "oi": True if oi_change is None else oi_change >= -OI_UNWIND_PCT,
    }
    score = max(buy, sell)
    required = (max(MIN_SCORE, MAJOR_MIN_SCORE) if symbol in MEME_SYMBOLS else MAJOR_MIN_SCORE) if info["priority"] else NON_PRIORITY_MIN_SCORE
    signal = "NONE"
    if buy > sell and buy >= required:
        signal = "BUY"
    elif sell > buy and sell >= required:
        signal = "SELL"

    if trend15 == "bull" and signal == "SELL": signal = "NONE"; reasons.append("15m bullish veto")
    if trend15 == "bear" and signal == "BUY": signal = "NONE"; reasons.append("15m bearish veto")
    if signal == "BUY" and macd_bear: signal = "NONE"; reasons.append("MACD veto")
    if signal == "SELL" and macd_bull: signal = "NONE"; reasons.append("MACD veto")

    failed = [k for k,v in filters.items() if not v]
    if signal != "NONE" and failed:
        signal = "NONE"; reasons.append("Filters failed: " + ",".join(failed))

    target = MARGIN_USDT * LEVERAGE
    fee_ok, gross, fees, required_gross = fee_buffer_ok(target)
    if signal != "NONE" and not fee_ok:
        signal = "NONE"; reasons.append(f"Fee buffer failed gross={fmt(gross,4)} required={fmt(required_gross,4)}")

    reasons.extend([
        f"ADX={fmt(adx_v,2)}", f"VOL={fmt(volume_ratio,2)}x", f"ATR={fmt(atr_pct,3)}%",
        f"session={info['name']}", f"priority={info['priority']}", f"OI={fmt(oi_change,3)}%" if oi_change is not None else "OI=warming",
    ])
    return {
        "signal": signal, "score": score, "buy": buy, "sell": sell, "max_score": 9,
        "required_score": required, "votes": votes, "filters": filters,
        "macd_veto": (signal == "NONE" and ((buy > sell and macd_bear) or (sell > buy and macd_bull))),
        "entry": values[i], "rsi14": r14[i], "ema20": e20[i], "macd": macd_line[i], "macd_signal": macd_sig[i],
        "vwap": vw, "adx": adx_v, "atr_pct": atr_pct, "volume_ratio": volume_ratio,
        "trend15": trend15, "funding_pct": fp, "funding_threshold": fthr, "oi_change_pct": oi_change,
        "session": info["name"], "priority_session": info["priority"], "fee_buffer_ok": fee_ok,
        "fee_buffer_usdt": FEE_BUFFER_USDT, "support": support, "resistance": resistance,
        "bullish_candle": bullish_candle_signal, "bearish_candle": bearish_candle_signal,
        "bullish_rejection": bullish_rejection, "bearish_rejection": bearish_rejection,
        "reason": " | ".join(reasons),
    }


def place_order(symbol, analysis, snapshot):
    if not AUTO_TRADE:
        return {"status": "BLOCKED", "reason": "AUTO_TRADE=false"}
    if not DEMO and not ALLOW_LIVE:
        return {"status": "BLOCKED", "reason": "LIVE trading disabled; set ALLOW_LIVE=true explicitly"}
    if analysis.get("signal") not in ("BUY", "SELL"):
        return {"status": "NO_TRADE", "reason": "Signal not approved"}
    if snapshot.get(symbol):
        return {"status": "BLOCKED", "reason": "Existing position"}

    with order_lock:
        # Use the cycle snapshot; do not create a second full private scan for every symbol.
        current_exposure = total_open_notional(snapshot)
        open_count = len(snapshot)
        target_notional = MARGIN_USDT * LEVERAGE
        if snapshot.get(symbol):
            return {"status": "BLOCKED", "reason": "Existing position"}
        if open_count >= MAX_CONCURRENT_POSITIONS:
            return {"status": "BLOCKED", "reason": f"Concurrent-position cap {open_count}/{MAX_CONCURRENT_POSITIONS}"}
        if current_exposure + target_notional > MAX_TOTAL_NOTIONAL_USDT:
            return {"status": "BLOCKED", "reason": f"Exposure cap ${fmt(current_exposure,2)} + ${fmt(target_notional,2)} > ${fmt(MAX_TOTAL_NOTIONAL_USDT,2)}"}

        side = "buy" if analysis["signal"] == "BUY" else "sell"
        ticker_price = get_ticker(symbol)
        size, actual_notional = calculate_order_size(symbol, ticker_price)
        info = get_instrument(symbol)
        ok_depth, depth, best = liquidity_guard(symbol, side, target_notional)
        if not ok_depth:
            return {"status": "BLOCKED", "reason": f"Side liquidity too low: ${fmt(depth,2)} depth"}

        set_leverage(symbol)
        payload = {"instId": symbol, "tdMode": TD_MODE, "side": side, "sz": fmt(size), "clOrdId": "bot" + uuid.uuid4().hex[:24]}
        if position_mode == "long_short_mode":
            payload["posSide"] = "long" if side == "buy" else "short"

        if ENTRY_ORDER_TYPE == "ioc":
            slip = MAX_ENTRY_SLIPPAGE_PCT / Decimal("100")
            px = best * (Decimal("1") + slip) if side == "buy" else best * (Decimal("1") - slip)
            px = floor_step(px, info["tickSz"]) if side == "buy" else ceil_step(px, info["tickSz"])
            payload["ordType"] = "ioc"
            payload["px"] = fmt(px)
        else:
            payload["ordType"] = "market"

        log(f"ORDER SUBMIT | {symbol} | {side.upper()} | ${fmt(actual_notional,2)} | {ENTRY_ORDER_TYPE} | exposure=${fmt(current_exposure,2)}")
        result = private_request("POST", "/api/v5/trade/order", payload=payload)
        row = (result.get("data") or [{}])[0]
        if row.get("sCode") not in (None, "", "0"):
            raise RuntimeError(f"ORDER REJECTED | {row.get('sCode')} | {row.get('sMsg')}")

        filled = None
        for _ in range(12):
            time.sleep(1)
            snap2 = refresh_position_snapshot()
            filled = snap2.get(symbol)
            if filled:
                break
        if not filled:
            return {"status": "NOT_FILLED", "reason": "IOC/entry did not create a position", "ordId": row.get("ordId", "")}

        entry = dec(filled.get("avgPx") or ticker_price)
        sl, tp = calculate_initial_sl_tp(
            side, entry, info["tickSz"],
            atr_pct=analysis.get("atr_pct"),
            support=analysis.get("support"),
            resistance=analysis.get("resistance"),
        )
        cancel_existing_protection(symbol, filled)
        last_error = None
        for attempt in range(1, 4):
            try:
                protection = place_full_position_oco(symbol, side, sl, tp, info["tickSz"])
                time.sleep(1)
                if not protection_exists(symbol, filled):
                    raise RuntimeError("OCO submitted but protection not verified")
                with state_lock:
                    state.setdefault(symbol, {}).update({"entry_price": entry, "current_sl": sl, "current_tp": tp, "position_size": dec(filled.get("pos", "0")), "protection": "ACTIVE", "protection_algo": protection, "step_level": 0})
                return {"status": "ORDER_AND_PROTECTION_ACTIVE", "symbol": symbol, "side": side, "size": fmt(size), "entry": fmt(entry), "sl": fmt(sl), "tp": fmt(tp), "actual_notional": fmt(actual_notional,2), "ordId": row.get("ordId", "")}
            except Exception as exc:
                last_error = exc
                log(f"PROTECTION RETRY | {symbol} | {attempt}/3 | {exc}")
                if attempt < 3:
                    time.sleep(PROTECTION_RETRY_SECONDS)
        try:
            emergency_close(symbol)
            return {"status": "EMERGENCY_CLOSED", "reason": "Protection failed", "error": str(last_error)}
        except Exception as close_exc:
            raise RuntimeError(f"CRITICAL POSITION OPEN/PROTECTION FAILED/EMERGENCY CLOSE FAILED | protection={last_error} | close={close_exc}")


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

    # Restart recovery: inspect OKX protection first. If local state is gone, do NOT
    # overwrite an existing OCO with a fresh initial SL/TP.
    protection_active = protection_exists(symbol, position)
    if current_sl is None or current_tp is None:
        current_sl, current_tp = calculate_initial_sl_tp(side, avg, tick)
        if protection_active:
            for algo in position.get("closeOrderAlgo", []) or []:
                if str(algo.get("closeFraction", "")) == "1":
                    if algo.get("slTriggerPx"):
                        current_sl = dec(algo["slTriggerPx"])
                    if algo.get("tpTriggerPx"):
                        current_tp = dec(algo["tpTriggerPx"])
                    break

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

    # Step-based TP extension while never loosening SL.
    steps = int((profit / Decimal("0.50")).to_integral_value(rounding=ROUND_DOWN)) if profit > 0 else 0
    if steps > 0:
        step_trigger = Decimal("0.50")
        if side == "buy":
            candidate_tp = avg * (Decimal("1") + Decimal(steps + 1) * step_trigger / Decimal("100"))
            new_tp = max(new_tp, floor_step(candidate_tp, tick))
        else:
            candidate_tp = avg * (Decimal("1") - Decimal(steps + 1) * step_trigger / Decimal("100"))
            new_tp = min(new_tp, ceil_step(candidate_tp, tick))

    if not protection_active:
        log(f"PROTECTION MISSING | {symbol} | restoring")
        try:
            cancel_existing_protection(symbol, position)
            place_full_position_oco(symbol, side, new_sl, new_tp, tick)
            time.sleep(1)
            if not protection_exists(symbol, position):
                raise RuntimeError("Protection restoration not verified")
            protection_active = True
        except Exception as exc:
            log(f"PROTECTION RESTORE FAILED | {symbol} | {exc}")
            try:
                emergency_close(symbol)
            except Exception as close_exc:
                log(f"CRITICAL EMERGENCY CLOSE FAILED | {symbol} | {close_exc}")
            return

    changed = new_sl != current_sl or new_tp != current_tp
    if changed:
        try:
            cancel_existing_protection(symbol, position)
            place_full_position_oco(symbol, side, new_sl, new_tp, tick)
            time.sleep(1)
            if not protection_exists(symbol, position):
                raise RuntimeError("new OCO verification failed")
            current_sl, current_tp = new_sl, new_tp
        except Exception as exc:
            log(f"STEP OCO UPDATE FAILED | {symbol} | {exc}")
            try:
                place_full_position_oco(symbol, side, current_sl, current_tp, tick)
            except Exception as restore_exc:
                log(f"CRITICAL OCO RESTORE FAILED | {symbol} | {restore_exc}")
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
        })


def startup_checks():
    log("====================================================")
    log(f"OKX SCALPING BOT {VERSION} | VOTE + FILTER + VETO")
    log(f"DEMO={DEMO} | AUTO_TRADE={AUTO_TRADE} | ALLOW_LIVE={ALLOW_LIVE}")
    log(f"MARGIN=${MARGIN_USDT} | LEVERAGE={LEVERAGE}x | MAX_EXPOSURE=${MAX_TOTAL_NOTIONAL_USDT} | MAX_POSITIONS={MAX_CONCURRENT_POSITIONS}")
    log(f"SL={SL_PERCENT}% | TP={TP_PERCENT}% | FEE_BUFFER=${FEE_BUFFER_USDT}")
    log(f"SCORE priority={max(MIN_SCORE, MAJOR_MIN_SCORE)} | non-priority={NON_PRIORITY_MIN_SCORE} | max=9")
    log(f"ENTRY={ENTRY_ORDER_TYPE} | MAX_SLIPPAGE={MAX_ENTRY_SLIPPAGE_PCT}% | DEPTH_MULT={MIN_SIDE_DEPTH_MULT}x")
    log(f"SYMBOLS={SYMBOLS}")
    log("====================================================")
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
            time.sleep(max(10, POLL_SECONDS))

    while True:
        cycle_start = time.time()
        try:
            snapshot = refresh_position_snapshot()
        except Exception as exc:
            log(f"POSITION SNAPSHOT ERROR | {exc}")
            snapshot = dict(position_snapshot)

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

                # OI delta is stored locally per symbol, so no extra private call.
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

                log(f"{symbol}: {analysis['signal']} {analysis.get('score',0)}/{analysis.get('max_score',9)} need={analysis.get('required_score','-')} | {analysis.get('session')} | {analysis.get('reason','')}")

                if analysis.get("signal") in ("BUY", "SELL") and analysis.get("score", 0) >= analysis.get("required_score", 99):
                    result = place_order(symbol, analysis, snapshot)
                    log("TRADE RESULT | " + json.dumps(result, default=str))
                    with state_lock:
                        state[symbol]["trade_status"] = result.get("status", "UNKNOWN")
                        state[symbol]["trade_result"] = result
                    if result.get("status") == "ORDER_AND_PROTECTION_ACTIVE":
                        # Avoid a second private position scan in the same cycle.
                        # The next cycle will refresh the authoritative snapshot.
                        try:
                            snapshot = dict(snapshot)
                            snapshot[symbol] = {
                                "instId": symbol,
                                "pos": result.get("size", "0"),
                                "avgPx": result.get("entry", "0"),
                                "markPx": result.get("entry", "0"),
                            }
                        except Exception:
                            pass
                else:
                    with state_lock:
                        state[symbol]["trade_status"] = "NO TRADE"
            except Exception as exc:
                log(f"{symbol} ERROR | {type(exc).__name__}: {exc}")
                with state_lock:
                    state.setdefault(symbol, {})["trade_status"] = "ERROR"
                    state[symbol]["trade_error"] = str(exc)

        elapsed = time.time() - cycle_start
        time.sleep(max(1, POLL_SECONDS - int(elapsed)))


HTML = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>OKX Scalping Bot V12.1 FIXED</title><style>body{font-family:Arial,sans-serif;background:#0b0d10;color:#eee;margin:0;padding:14px}.card{background:#15191f;border:1px solid #2b313a;border-radius:10px;padding:10px;margin-bottom:8px}.wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px;min-width:1250px}th,td{padding:7px;border-bottom:1px solid #2b313a;text-align:left;white-space:nowrap}th{background:#171b21;position:sticky;top:0}.buy{color:#45d483;font-weight:bold}.sell{color:#ff6565;font-weight:bold}.active{color:#45d483;font-weight:bold}.danger{color:#ff6565;font-weight:bold}</style></head><body><h2>OKX SCALP V12.1 FIXED</h2><div id="top"></div><div id="activity">Loading...</div><div class="wrap"><table><thead><tr><th>Pair</th><th>Signal</th><th>Score</th><th>Priority</th><th>Session</th><th>15m</th><th>ADX</th><th>Vol</th><th>ATR%</th><th>Funding</th><th>OI Δ</th><th>Entry</th><th>Mark</th><th>P/L%</th><th>SL</th><th>TP</th><th>Protection</th><th>Status</th></tr></thead><tbody id="rows"></tbody></table></div><script>function e(x){return String(x??"-").replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}async function refresh(){try{const s=await fetch('/api/status').then(r=>r.json());document.getElementById('top').innerHTML='<div class="card">Mode: <b>'+e(s.mode)+'</b> | Auto: <b>'+e(s.auto_trade)+'</b> | Margin: <b>$'+e(s.margin)+'</b> | Lev: <b>'+e(s.leverage)+'x</b> | Exposure: <b>$'+e(s.exposure)+' / $'+e(s.max_exposure)+'</b> | Worker: <b>'+e(s.worker_error||'OK')+'</b></div><div class="card">Public: <b>'+e(s.public_api)+'</b> | Private: <b>'+e(s.private_api)+'</b> | Position snapshot: <b>'+e(s.position_snapshot_age)+'s</b></div>';document.getElementById('activity').textContent='Last activity: '+e(s.last_activity)+' | '+e(s.updated);let h='';for(const [sym,x] of Object.entries(s.symbols||{})){const sig=x.signal||'NONE';h+='<tr><td>'+e(sym)+'</td><td class="'+(sig==='BUY'?'buy':sig==='SELL'?'sell':'')+'">'+e(sig)+'</td><td>'+e(x.score)+'/'+e(x.max_score)+' need '+e(x.required_score)+'</td><td>'+e(x.priority_session)+'</td><td>'+e(x.session)+'</td><td>'+e(x.trend15)+'</td><td>'+e(x.adx)+'</td><td>'+e(x.volume_ratio)+'</td><td>'+e(x.atr_pct)+'</td><td>'+e(x.funding_pct)+'</td><td>'+e(x.oi_change_pct)+'</td><td>'+e(x.entry_price||x.entry)+'</td><td>'+e(x.mark_price)+'</td><td>'+e(x.profit_pct)+'</td><td>'+e(x.current_sl)+'</td><td>'+e(x.current_tp)+'</td><td class="'+(x.protection==='ACTIVE'?'active':'danger')+'">'+e(x.protection||'NONE')+'</td><td>'+e(x.trade_status||'WAITING')+'</td></tr>'}document.getElementById('rows').innerHTML=h}catch(err){document.getElementById('activity').textContent='Dashboard error: '+err}}refresh();setInterval(refresh,5000)</script></body></html>'''


@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


@app.get("/api/status")
def api_status():
    with state_lock:
        symbols = {k: v.copy() for k, v in state.items() if k in SYMBOLS}
        public_api = state.get("public_api", "STARTING")
        private_api = state.get("private_api", "STARTING")
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
        "position_snapshot_age": round(max(0, time.time() - position_snapshot_ts), 1) if position_snapshot_ts else None,
        "last_activity": activity, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "symbols": symbols
    })


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "bot": VERSION, "demo": DEMO, "auto_trade": AUTO_TRADE, "worker_started": worker_started})


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
