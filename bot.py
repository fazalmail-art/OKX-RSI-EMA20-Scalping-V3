import os
import time
import json
import hmac
import base64
import hashlib
import logging
import threading
from datetime import datetime, timezone

import requests
import pandas as pd
from flask import Flask, jsonify

# ============================================================
# ICT SwiftEdge SMC Bot
# Pine-script-inspired BOS/MSS + RSI-MA + HTF trend + volume
# + ATR + fake-breakout/retest + trailing stop.
#
# SAFE DEFAULT:
#   OKX_DEMO=true
#   AUTO_TRADE=false
#
# Set AUTO_TRADE=true only after testing the signals in demo.
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("swiftedge")

app = Flask(__name__)

# -------------------------
# Configuration
# -------------------------
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com")
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

# OKX demo trading is selected with x-simulated-trading: 1.
OKX_DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"

# Futures / perpetual swap symbols.
SYMBOLS = [x.strip().upper() for x in os.getenv(
    "SYMBOLS",
    "BTC-USDT-SWAP,ETH-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP"
).split(",") if x.strip()]

SIGNAL_BAR = os.getenv("SIGNAL_BAR", "5m")
TREND_BAR = os.getenv("TREND_BAR", "15m")
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "220"))

# Pine-style dynamic settings, with 5m/15m emphasis.
CHART_TIMEFRAME = os.getenv("CHART_TIMEFRAME", "5M").upper()

# Risk / trade settings
MARGIN_USDT = float(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
TP_PERCENT = float(os.getenv("TP_PERCENT", "0.60"))
SL_PERCENT = float(os.getenv("SL_PERCENT", "0.35"))
TRAILING_ATR_MULT = float(os.getenv("TRAILING_ATR_MULT", "1.5"))

# Signal quality filters
MIN_SCORE = float(os.getenv("MIN_SCORE", "7.0"))
RSI_LENGTH = int(os.getenv("RSI_LENGTH", "14"))
RSI_MA_LENGTH = int(os.getenv("RSI_MA_LENGTH", "9"))
ATR_LENGTH = int(os.getenv("ATR_LENGTH", "14"))
ADX_LENGTH = int(os.getenv("ADX_LENGTH", "14"))
ADX_MIN = float(os.getenv("ADX_MIN", "18"))
VOLUME_MULT = float(os.getenv("VOLUME_MULT", "1.10"))
EMA_FAST = int(os.getenv("EMA_FAST", "20"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "50"))
EMA_TREND = int(os.getenv("EMA_TREND", "200"))

# Structure
PIVOT_LEFT = int(os.getenv("PIVOT_LEFT", "2"))
PIVOT_RIGHT = int(os.getenv("PIVOT_RIGHT", "2"))
RETEST_BARS = int(os.getenv("RETEST_BARS", "4"))

# Bot loop
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))
PORT = int(os.getenv("PORT", "8080"))

# Prevent duplicate entries per symbol.
last_trade_time = {}
last_signal_key = {}
positions = {}
instrument_cache = {}
state_lock = threading.Lock()


# -------------------------
# Utility / OKX REST
# -------------------------
def iso_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def okx_headers(method, request_path, body=""):
    ts = iso_timestamp()
    prehash = ts + method.upper() + request_path + body
    signature = base64.b64encode(
        hmac.new(
            OKX_SECRET_KEY.encode(),
            prehash.encode(),
            hashlib.sha256
        ).digest()
    ).decode()

    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
    }
    if OKX_DEMO:
        headers["x-simulated-trading"] = "1"
    return headers


def public_get(path, params=None):
    r = requests.get(OKX_BASE_URL + path, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX public error: {data}")
    return data["data"]


def private_request(method, path, params=None, payload=None):
    method = method.upper()
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""

    if method == "GET" and params:
        # Build query string exactly as signed.
        from urllib.parse import urlencode
        query = urlencode(params)
        request_path = path + "?" + query
    else:
        request_path = path

    headers = okx_headers(method, request_path, body)

    if method == "GET":
        r = requests.get(
            OKX_BASE_URL + path, params=params, headers=headers, timeout=15
        )
    else:
        r = requests.request(
            method, OKX_BASE_URL + path, headers=headers, data=body, timeout=15
        )

    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX private error: {data}")
    return data["data"]


def get_candles(inst_id, bar, limit=CANDLE_LIMIT):
    rows = public_get(
        "/api/v5/market/candles",
        {"instId": inst_id, "bar": bar, "limit": str(limit)}
    )
    # OKX: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
    cols = ["ts", "open", "high", "low", "close", "volume",
            "volCcy", "volCcyQuote", "confirm"]
    df = pd.DataFrame(rows, columns=cols)

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df = df.sort_values("ts").reset_index(drop=True)

    # Only confirmed candles. This intentionally avoids the Pine code's
    # lookahead_on repaint behavior.
    if "confirm" in df.columns:
        df = df[df["confirm"].astype(str) == "1"].copy()

    return df.reset_index(drop=True)


def get_instrument(inst_id):
    if inst_id in instrument_cache:
        return instrument_cache[inst_id]

    data = public_get(
        "/api/v5/public/instruments",
        {"instType": "SWAP", "instId": inst_id}
    )
    if not data:
        raise RuntimeError(f"Instrument not found: {inst_id}")

    x = data[0]
    info = {
        "ctVal": float(x["ctVal"]),
        "lotSz": float(x["lotSz"]),
        "minSz": float(x["minSz"]),
        "tickSz": float(x["tickSz"]),
    }
    instrument_cache[inst_id] = info
    return info


# -------------------------
# Indicators
# -------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    delta = s.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    avg_up = up.ewm(alpha=1/n, adjust=False).mean()
    avg_down = down.ewm(alpha=1/n, adjust=False).mean()
    rs = avg_up / avg_down.replace(0, 1e-12)
    return 100 - (100 / (1 + rs))


def atr(df, n=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def adx(df, n=14):
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()

    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr_n = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/n, adjust=False).mean() / atr_n.replace(0, 1e-12)
    minus_di = 100 * minus_dm.ewm(alpha=1/n, adjust=False).mean() / atr_n.replace(0, 1e-12)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-12)
    return dx.ewm(alpha=1/n, adjust=False).mean()


def add_indicators(df):
    df = df.copy()
    df["ema20"] = ema(df["close"], EMA_FAST)
    df["ema50"] = ema(df["close"], EMA_SLOW)
    df["ema200"] = ema(df["close"], EMA_TREND)

    df["rsi"] = rsi(df["close"], RSI_LENGTH)
    df["rsi_ma"] = df["rsi"].rolling(RSI_MA_LENGTH).mean()
    df["atr"] = atr(df, ATR_LENGTH)
    df["adx"] = adx(df, ADX_LENGTH)

    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, 1e-12)

    # VWAP using session-independent cumulative volume.
    # For crypto this is a stable rolling approximation rather than exchange
    # session VWAP.
    pv = df["close"] * df["volume"]
    df["vwap"] = pv.cumsum() / df["volume"].cumsum().replace(0, 1e-12)

    return df


# -------------------------
# SMC / BOS / MSS
# -------------------------
def confirmed_pivots(df, left=2, right=2):
    highs = []
    lows = []

    h = df["high"].values
    l = df["low"].values

    for i in range(left, len(df) - right):
        if h[i] == max(h[i-left:i+right+1]):
            highs.append((i, h[i]))
        if l[i] == min(l[i-left:i+right+1]):
            lows.append((i, l[i]))

    return highs, lows


def structure_signal(df):
    """
    Returns BOS/MSS state based on confirmed pivots.
    No lookahead: pivots are only usable after right confirmation bars.
    """
    highs, lows = confirmed_pivots(df, PIVOT_LEFT, PIVOT_RIGHT)
    if len(highs) < 2 or len(lows) < 2:
        return {"bull": False, "bear": False, "mss_bull": False, "mss_bear": False}

    last_high_idx, last_high = highs[-1]
    prev_high_idx, prev_high = highs[-2]
    last_low_idx, last_low = lows[-1]
    prev_low_idx, prev_low = lows[-2]

    close = df["close"].iloc[-1]
    prior_close = df["close"].iloc[-2]

    # Market bias from HH/HL vs LH/LL.
    bullish_structure = last_high >= prev_high and last_low >= prev_low
    bearish_structure = last_high <= prev_high and last_low <= prev_low

    bull_break = close > last_high and prior_close <= last_high
    bear_break = close < last_low and prior_close >= last_low

    # MSS = break against the current structural bias.
    mss_bull = bear_break is False and close > last_high and bearish_structure
    mss_bear = bear_break and bullish_structure

    return {
        "bull": bool(bull_break),
        "bear": bool(bear_break),
        "mss_bull": bool(mss_bull),
        "mss_bear": bool(mss_bear),
        "last_high": float(last_high),
        "last_low": float(last_low),
        "bullish_structure": bool(bullish_structure),
        "bearish_structure": bool(bearish_structure),
    }


# -------------------------
# Fake breakout / retest
# -------------------------
def fake_breakout_rejection(df):
    """
    Detects rejection of a recent swing level:
    bullish = price swept below a recent low but closed back above it.
    bearish = price swept above a recent high but closed back below it.
    """
    if len(df) < 10:
        return False, False

    recent = df.iloc[-RETEST_BARS-2:-1]
    current = df.iloc[-1]

    recent_low = recent["low"].min()
    recent_high = recent["high"].max()

    bull_rejection = (
        current["low"] < recent_low and
        current["close"] > recent_low and
        current["close"] > current["open"]
    )

    bear_rejection = (
        current["high"] > recent_high and
        current["close"] < recent_high and
        current["close"] < current["open"]
    )

    return bool(bull_rejection), bool(bear_rejection)


def retest_confirmation(df, direction):
    """
    Simple close + retest confirmation around EMA20 / VWAP.
    """
    if len(df) < 5:
        return False

    a = df.iloc[-1]
    b = df.iloc[-2]

    if direction == "LONG":
        return (
            a["close"] > a["ema20"] and
            b["low"] <= b["ema20"] * 1.0015 and
            a["close"] > b["high"]
        )

    return (
        a["close"] < a["ema20"] and
        b["high"] >= b["ema20"] * 0.9985 and
        a["close"] < b["low"]
    )


# -------------------------
# Scoring
# -------------------------
def analyze_symbol(inst_id):
    fast = add_indicators(get_candles(inst_id, SIGNAL_BAR))
    trend = add_indicators(get_candles(inst_id, TREND_BAR))

    if len(fast) < 80 or len(trend) < 80:
        return {"signal": "NONE", "score": 0, "reason": "not_enough_data"}

    f = fast.iloc[-1]
    t = trend.iloc[-1]
    structure = structure_signal(fast)
    fake_bull, fake_bear = fake_breakout_rejection(fast)

    long_points = 0
    short_points = 0
    reasons_long = []
    reasons_short = []

    # 1) BOS / MSS: strongest component
    if structure.get("bull") or structure.get("mss_bull"):
        long_points += 2
        reasons_long.append("BOS/MSS")
    if structure.get("bear") or structure.get("mss_bear"):
        short_points += 2
        reasons_short.append("BOS/MSS")

    # 2) RSI-MA, matching Pine concept
    if f["rsi_ma"] > 50 and f["rsi"] > f["rsi_ma"]:
        long_points += 1
        reasons_long.append("RSI-MA>50")
    if f["rsi_ma"] < 50 and f["rsi"] < f["rsi_ma"]:
        short_points += 1
        reasons_short.append("RSI-MA<50")

    # 3) 5m EMA trend
    if f["ema20"] > f["ema50"] > f["ema200"]:
        long_points += 1
        reasons_long.append("EMA trend")
    if f["ema20"] < f["ema50"] < f["ema200"]:
        short_points += 1
        reasons_short.append("EMA trend")

    # 4) HTF 15m confirmation
    if t["close"] > t["ema20"] > t["ema50"]:
        long_points += 2
        reasons_long.append("15m trend")
    if t["close"] < t["ema20"] < t["ema50"]:
        short_points += 2
        reasons_short.append("15m trend")

    # 5) VWAP
    if f["close"] > f["vwap"]:
        long_points += 1
        reasons_long.append("VWAP")
    if f["close"] < f["vwap"]:
        short_points += 1
        reasons_short.append("VWAP")

    # 6) ADX
    if f["adx"] >= ADX_MIN:
        if long_points > short_points:
            long_points += 1
            reasons_long.append(f"ADX {f['adx']:.1f}")
        elif short_points > long_points:
            short_points += 1
            reasons_short.append(f"ADX {f['adx']:.1f}")

    # 7) Volume
    if f["vol_ratio"] >= VOLUME_MULT:
        if long_points > short_points:
            long_points += 1
            reasons_long.append(f"Volume {f['vol_ratio']:.1f}x")
        elif short_points > long_points:
            short_points += 1
            reasons_short.append(f"Volume {f['vol_ratio']:.1f}x")

    # 8) Fake-breakout rejection
    if fake_bull:
        long_points += 1
        reasons_long.append("bull rejection")
    if fake_bear:
        short_points += 1
        reasons_short.append("bear rejection")

    # 9) Retest
    if retest_confirmation(fast, "LONG"):
        long_points += 1
        reasons_long.append("retest")
    if retest_confirmation(fast, "SHORT"):
        short_points += 1
        reasons_short.append("retest")

    signal = "NONE"
    score = max(long_points, short_points)
    reasons = []

    if long_points >= MIN_SCORE and long_points > short_points:
        signal = "LONG"
        reasons = reasons_long
    elif short_points >= MIN_SCORE and short_points > long_points:
        signal = "SHORT"
        reasons = reasons_short

    return {
        "symbol": inst_id,
        "signal": signal,
        "long_score": long_points,
        "short_score": short_points,
        "score": score,
        "price": float(f["close"]),
        "rsi": float(f["rsi"]),
        "rsi_ma": float(f["rsi_ma"]),
        "ema20": float(f["ema20"]),
        "ema50": float(f["ema50"]),
        "ema200": float(f["ema200"]),
        "vwap": float(f["vwap"]),
        "atr": float(f["atr"]),
        "adx": float(f["adx"]),
        "volume_ratio": float(f["vol_ratio"]),
        "reasons": reasons,
        "structure": structure,
        "timestamp": int(f["ts"]),
    }


# -------------------------
# Position sizing / orders
# -------------------------
def floor_to_step(value, step):
    if step <= 0:
        return value
    return (value // step) * step


def calc_contract_size(inst_id, price):
    info = get_instrument(inst_id)
    # Notional ~= contracts * ctVal * price for linear USDT swaps.
    target_notional = MARGIN_USDT * LEVERAGE
    raw_sz = target_notional / (price * info["ctVal"])
    sz = floor_to_step(raw_sz, info["lotSz"])

    if sz < info["minSz"]:
        sz = info["minSz"]

    return sz, info


def set_leverage(inst_id):
    payload = {
        "instId": inst_id,
        "lever": str(LEVERAGE),
        "mgnMode": "isolated",
    }
    return private_request("POST", "/api/v5/account/set-leverage", payload=payload)


def place_market_order(inst_id, side, sz, reduce_only=False):
    payload = {
        "instId": inst_id,
        "tdMode": "isolated",
        "side": side,
        "ordType": "market",
        "sz": str(sz),
        "reduceOnly": "true" if reduce_only else "false",
    }
    return private_request("POST", "/api/v5/trade/order", payload=payload)


def place_bracket_algos(inst_id, direction, sz, entry, tp, sl):
    """
    Attach TP/SL using OKX conditional algo order.
    We keep this separate from entry so entry failures do not leave a
    misleading local position.
    """
    side = "sell" if direction == "LONG" else "buy"

    payload = {
        "instId": inst_id,
        "tdMode": "isolated",
        "side": side,
        "ordType": "conditional",
        "sz": str(sz),
        "tpTriggerPx": str(tp),
        "tpOrdPx": "-1",
        "slTriggerPx": str(sl),
        "slOrdPx": "-1",
        "reduceOnly": "true",
    }
    return private_request("POST", "/api/v5/trade/order-algo", payload=payload)


def execute_signal(result):
    symbol = result["symbol"]
    signal = result["signal"]

    if signal == "NONE":
        return {"action": "none"}

    now = time.time()
    with state_lock:
        if now - last_trade_time.get(symbol, 0) < COOLDOWN_SECONDS:
            return {"action": "cooldown"}

        # Never stack a second local position for the same symbol.
        if symbol in positions:
            return {"action": "already_in_position"}

        signal_key = f"{result['timestamp']}:{signal}"
        if last_signal_key.get(symbol) == signal_key:
            return {"action": "duplicate"}

        last_signal_key[symbol] = signal_key

    price = result["price"]
    atr_value = result["atr"]

    if signal == "LONG":
        tp = price * (1 + TP_PERCENT / 100)
        sl = price * (1 - SL_PERCENT / 100)
        # ATR stop can be wider when volatility requires it.
        sl = min(sl, price - atr_value * TRAILING_ATR_MULT)
        side = "buy"
    else:
        tp = price * (1 - TP_PERCENT / 100)
        sl = price * (1 + SL_PERCENT / 100)
        sl = max(sl, price + atr_value * TRAILING_ATR_MULT)
        side = "sell"

    sz, info = calc_contract_size(symbol, price)

    record = {
        "symbol": symbol,
        "direction": signal,
        "entry": price,
        "tp": tp,
        "sl": sl,
        "size": sz,
        "time": now,
        "score": result["score"],
        "reasons": result["reasons"],
    }

    log.warning(
        "SIGNAL | %s | %s | score %.1f | price %.8f | TP %.8f | SL %.8f | reasons=%s",
        symbol, signal, result["score"], price, tp, sl, result["reasons"]
    )

    if not AUTO_TRADE:
        return {"action": "signal_only", **record}

    if not OKX_API_KEY or not OKX_SECRET_KEY or not OKX_PASSPHRASE:
        raise RuntimeError("AUTO_TRADE=true but OKX API credentials are missing.")

    set_leverage(symbol)
    order_side = side
    order_result = place_market_order(symbol, order_side, sz, False)

    try:
        place_bracket_algos(symbol, signal, sz, price, tp, sl)
    except Exception as e:
        # Entry succeeded but bracket failed: log loudly so the position can
        # be handled manually rather than pretending protection exists.
        log.exception("ENTRY OK BUT TP/SL ALGO FAILED for %s: %s", symbol, e)
        record["bracket_error"] = str(e)

    with state_lock:
        positions[symbol] = record
        last_trade_time[symbol] = now

    return {"action": "traded", "order": order_result, **record}


# -------------------------
# Monitoring
# -------------------------
latest_results = {}
last_error = None


def scanner_loop():
    global last_error

    log.info(
        "SwiftEdge SMC bot started | demo=%s | auto_trade=%s | symbols=%s | "
        "signal=%s | trend=%s | min_score=%s",
        OKX_DEMO, AUTO_TRADE, SYMBOLS, SIGNAL_BAR, TREND_BAR, MIN_SCORE
    )

    while True:
        for symbol in SYMBOLS:
            try:
                result = analyze_symbol(symbol)
                latest_results[symbol] = result

                log.info(
                    "%s | %s | score=%s L=%s S=%s | price=%s | RSI=%.1f | ADX=%.1f",
                    symbol,
                    result.get("signal"),
                    result.get("score"),
                    result.get("long_score"),
                    result.get("short_score"),
                    result.get("price"),
                    result.get("rsi"),
                    result.get("adx"),
                )

                if result.get("signal") in ("LONG", "SHORT"):
                    execute_signal(result)

                last_error = None

            except Exception as e:
                last_error = str(e)
                log.exception("Scanner error for %s: %s", symbol, e)

        time.sleep(POLL_SECONDS)


# -------------------------
# Railway health endpoints
# -------------------------
@app.get("/")
def home():
    return jsonify({
        "bot": "ICT SwiftEdge SMC",
        "status": "running",
        "demo": OKX_DEMO,
        "auto_trade": AUTO_TRADE,
        "symbols": SYMBOLS,
        "signal_bar": SIGNAL_BAR,
        "trend_bar": TREND_BAR,
        "min_score": MIN_SCORE,
        "latest": latest_results,
        "positions": positions,
        "last_error": last_error,
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": iso_timestamp()})


@app.get("/signals")
def signals():
    return jsonify(latest_results)


def start_scanner():
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    start_scanner()
    app.run(host="0.0.0.0", port=PORT)
