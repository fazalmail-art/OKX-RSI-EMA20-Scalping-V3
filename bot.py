import os
import json
import time
import hmac
import base64
import hashlib
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
import websocket
from flask import Flask, jsonify, render_template_string, request


# ============================================================
# ICT SWIFTEDGE - OKX FUTURES SCALPER
# 5M STRUCTURE + 15S EXECUTION
# SOL / BTC / ETH / HYPE / XRP / DOGE
#
# CHANGELOG (this revision):
#  - FIX: added a real fixed Take-Profit exit (was missing —
#         bot only had trailing / emergency-SL / momentum-fail
#         / max-hold exits, no TP).
#  - FIX: entry logic required ALL 4 confluence checks
#         (bullish/bearish + momentum + RSI + EMA) at once,
#         which is very strict and rarely fires. Now uses a
#         configurable "N of 4" confirmation count, default
#         3 of 4, plus slightly looser default thresholds —
#         so signals actually form without removing the
#         underlying logic.
#  - FIX: shared dicts (positions, one_second_data,
#         candles_15s, last_trade_time) are now consistently
#         guarded by data_lock to avoid race conditions
#         between the websocket thread and the structure
#         thread.
#  - ADDED: a backtest engine (approximates 15s entries with
#         1m candles + 5m structure, since OKX only keeps a
#         short history of 1s/15s candles) and a
#         GET /api/backtest endpoint + a "Run Backtest" panel
#         on the dashboard that renders results as a table.
#  - ADDED: BACKTEST_MODE env var to run a one-shot backtest
#         from the CLI instead of starting the live bot.
# ============================================================

OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://us.okx.com")

# US OKX DEMO BUSINESS WS
OKX_WS_BUSINESS = os.getenv(
    "OKX_WS_BUSINESS",
    "wss://wsuspap.okx.com:8443/ws/v5/business"
)

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

OKX_DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"

# IMPORTANT:
# false = signal only
# true  = actual OKX demo orders
AUTO_TRADE = os.getenv("AUTO_TRADE", "true").lower() == "true"

# Live trading remains blocked unless explicitly enabled.
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

# Run a one-shot backtest instead of starting the live bot.
BACKTEST_MODE = os.getenv("BACKTEST_MODE", "false").lower() == "true"

# ============================================================
# SYMBOLS
# ============================================================

REQUESTED_SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "HYPE-USDT-SWAP",
    "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP",
]

# ============================================================
# MONEY / LEVERAGE
# ============================================================

MARGIN_USDT = float(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
TD_MODE = os.getenv("TD_MODE", "isolated")

ALLOW_MIN_SIZE_OVERSIZE = (
    os.getenv("ALLOW_MIN_SIZE_OVERSIZE", "false").lower() == "true"
)

# ============================================================
# STRATEGY
# ============================================================

STRUCTURE_LOOKBACK = int(os.getenv("STRUCTURE_LOOKBACK", "100"))

PIVOT_LEFT = int(os.getenv("PIVOT_LEFT", "2"))
PIVOT_RIGHT = int(os.getenv("PIVOT_RIGHT", "2"))

RSI_LENGTH = 14
RSI_MA_LENGTH = 7

# 0.015 = 0.015%
BREAK_BUFFER_PCT = float(os.getenv("BREAK_BUFFER_PCT", "0.015"))

# Loosened slightly from the original 0.35 / 1.00 so real
# candles actually qualify instead of almost never firing.
MIN_BODY_RATIO = float(os.getenv("MIN_BODY_RATIO", "0.25"))
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "0.85"))

# NEW: instead of requiring ALL of
# [bullish/bearish, momentum, RSI, EMA] to agree, require at
# least this many out of 4. 4 = old (very strict) behaviour,
# 2 = very loose. Default 3 is a reasonable middle ground.
MIN_CONFIRMATIONS = int(os.getenv("MIN_CONFIRMATIONS", "3"))

# ============================================================
# EXIT
# ============================================================

MIN_HOLD_SECONDS = 3
MAX_HOLD_SECONDS = 30

# Risk:Reward is ENFORCED, not just two independent numbers
# that happen to land on 1:2. You set the risk (SL distance);
# the take-profit distance is always computed FROM it as
# RISK_REWARD_RATIO x SL, so the ratio can never drift even if
# EMERGENCY_SL_ATR or RISK_REWARD_RATIO are changed later.
#
#   risk   = EMERGENCY_SL_ATR   (ATR multiple)
#   reward = EMERGENCY_SL_ATR * RISK_REWARD_RATIO
#
# Default: SL = 0.5x ATR, ratio = 2.0  ->  TP = 1.0x ATR
# i.e. risk 1 unit to make 2 units (1:2).
EMERGENCY_SL_ATR = float(os.getenv("EMERGENCY_SL_ATR", "0.5"))
RISK_REWARD_RATIO = float(os.getenv("RISK_REWARD_RATIO", "2.0"))
TP_ATR_MULT = EMERGENCY_SL_ATR * RISK_REWARD_RATIO

TRAIL_START_SECONDS = 12
TRAIL_ATR_MULT = float(os.getenv("TRAIL_ATR_MULT", "1.0"))

COOLDOWN_SECONDS = 45

MAX_DAILY_LOSS_USDT = float(os.getenv("MAX_DAILY_LOSS_USDT", "30"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))

# ============================================================
# SERVER
# ============================================================

PORT = int(os.getenv("PORT", "8080"))

# ============================================================
# WHATSAPP
# ============================================================

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_TO_NUMBER = os.getenv("WHATSAPP_TO_NUMBER", "")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "")


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

state = {
    "status": "STARTING",
    "ws_connected": False,
    "last_data": None,
    "last_signal": None,
    "last_order": None,
    "last_error": None,
    "daily_pnl": 0.0,
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "tp_hits": 0,
    "sl_hits": 0,
    "trail_hits": 0,
    "other_exits": 0,
    "consecutive_losses": 0,
    "signals": 0,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_backtest": None,
}

valid_symbols = []
positions = {}
last_trade_time = {}
one_second_data = {}
candles_15s = {}
instrument_cache = {}
symbol_status = {}
log_history = []

data_lock = threading.Lock()


# ============================================================
# LOGGING
# ============================================================

def log(message):
    text = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(text, flush=True)
    log_history.append(text)
    if len(log_history) > 300:
        del log_history[:-300]


# ============================================================
# TIME / SIGN
# ============================================================

def timestamp():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sign(ts, method, path, body=""):
    message = ts + method.upper() + path + body
    digest = hmac.new(OKX_SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def headers(method, path, body=""):
    ts = timestamp()
    h = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
    }
    if OKX_DEMO:
        h["x-simulated-trading"] = "1"
    return h


# ============================================================
# REST
# ============================================================

def public_get(path, params=None):
    try:
        r = requests.get(OKX_BASE_URL + path, params=params, timeout=10)
        result = r.json()
        if result.get("code") not in (None, "0", 0):
            log(f"[REST WARNING] {path} {result}")
        return result
    except Exception as e:
        state["last_error"] = str(e)
        log(f"[REST ERROR] {path} {e}")
        return {}


def private_post(path, payload):
    body = json.dumps(payload, separators=(",", ":"))
    try:
        r = requests.post(OKX_BASE_URL + path, headers=headers("POST", path, body), data=body, timeout=10)
        result = r.json()
        state["last_order"] = result
        return result
    except Exception as e:
        state["last_error"] = str(e)
        log(f"[PRIVATE REST ERROR] {e}")
        return {}


# ============================================================
# API CHECK
# ============================================================

def api_ready():
    if not OKX_API_KEY:
        log("[API ERROR] OKX_API_KEY missing")
        return False
    if not OKX_SECRET_KEY:
        log("[API ERROR] OKX_SECRET_KEY missing")
        return False
    if not OKX_PASSPHRASE:
        log("[API ERROR] OKX_PASSPHRASE missing")
        return False
    if not OKX_DEMO and not ALLOW_LIVE:
        log("[SAFETY] LIVE trading blocked")
        return False
    return True


# ============================================================
# SYMBOL VALIDATION
# ============================================================

def validate_symbols():
    global valid_symbols
    valid_symbols = []

    log("Checking OKX SWAP instruments...")

    for symbol in REQUESTED_SYMBOLS:
        result = public_get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol})
        data = result.get("data", [])

        if not data:
            symbol_status[symbol] = {"status": "UNAVAILABLE"}
            log(f"[SYMBOL SKIP] {symbol}")
            continue

        inst = data[0]

        if inst.get("state") != "live":
            symbol_status[symbol] = {"status": inst.get("state", "not_live")}
            log(f"[SYMBOL SKIP] {symbol} state={inst.get('state')}")
            continue

        valid_symbols.append(symbol)
        instrument_cache[symbol] = inst
        symbol_status[symbol] = {
            "status": "OK",
            "ctVal": inst.get("ctVal"),
            "lotSz": inst.get("lotSz"),
            "minSz": inst.get("minSz"),
            "ctType": inst.get("ctType"),
        }

        with data_lock:
            one_second_data.setdefault(symbol, [])
            candles_15s.setdefault(symbol, [])
            last_trade_time.setdefault(symbol, 0)

        log(
            f"[SYMBOL OK] {symbol} | ctVal={inst.get('ctVal')} | "
            f"lotSz={inst.get('lotSz')} | minSz={inst.get('minSz')} | "
            f"type={inst.get('ctType')}"
        )

    log("[ACTIVE SYMBOLS] " + (", ".join(valid_symbols) if valid_symbols else "NONE"))
    return valid_symbols


# ============================================================
# INSTRUMENT
# ============================================================

def get_instrument(symbol):
    if symbol in instrument_cache:
        return instrument_cache[symbol]

    result = public_get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol})
    data = result.get("data", [])

    if not data:
        return None

    instrument_cache[symbol] = data[0]
    return data[0]


# ============================================================
# DECIMAL ROUNDING
# ============================================================

def decimal_floor(value, step):
    value = Decimal(str(value))
    step = Decimal(str(step))
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def format_decimal(value):
    s = format(Decimal(str(value)), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


# ============================================================
# CORRECT CONTRACT SIZE
# ============================================================

def calculate_size(symbol, price):
    inst = get_instrument(symbol)
    if not inst:
        log(f"[SIZE ERROR] {symbol}: instrument unavailable")
        return 0, 0, 0

    ct_val = float(inst.get("ctVal", "0"))
    lot_sz = float(inst.get("lotSz", "1"))
    min_sz = float(inst.get("minSz", "1"))
    ct_type = inst.get("ctType", "linear")

    if ct_val <= 0 or price <= 0:
        return 0, 0, 0

    target_notional = MARGIN_USDT * LEVERAGE

    if ct_type == "linear":
        raw_contracts = target_notional / (price * ct_val)
    else:
        raw_contracts = target_notional / ct_val

    contracts = decimal_floor(raw_contracts, lot_sz)
    minimum = Decimal(str(min_sz))

    if contracts < minimum:
        if not ALLOW_MIN_SIZE_OVERSIZE:
            minimum_notional = (
                float(minimum) * ct_val * price
                if ct_type == "linear"
                else float(minimum) * ct_val
            )
            log(
                f"[SIZE BLOCKED] {symbol} | Target≈{target_notional:.2f} USDT | "
                f"Minimum≈{minimum_notional:.2f} USDT | Need larger margin or leverage"
            )
            return 0, 0, 0
        contracts = minimum

    if ct_type == "linear":
        actual_notional = float(contracts) * ct_val * price
    else:
        actual_notional = float(contracts) * ct_val

    actual_margin = actual_notional / LEVERAGE

    return float(contracts), float(actual_notional), float(actual_margin)


# ============================================================
# GENERIC CANDLE FETCH (used by live loop AND backtest)
# ============================================================

def fetch_candles(symbol, bar, limit, history=False):
    """Fetch OHLCV candles. history=True uses the longer-range
    history endpoint (needed for backtesting further back)."""
    path = "/api/v5/market/candles" if not history else "/api/v5/market/history-candles"

    result = public_get(path, {"instId": symbol, "bar": bar, "limit": str(limit)})
    rows = result.get("data", [])

    if not rows:
        return pd.DataFrame()

    rows = list(reversed(rows))
    records = []
    for r in rows:
        if len(r) < 6:
            continue
        records.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        })

    return pd.DataFrame(records)


def get_5m_candles(symbol):
    df = fetch_candles(symbol, "5m", STRUCTURE_LOOKBACK + 20)
    return add_indicators(df)


# ============================================================
# INDICATORS
# ============================================================

def EMA(series, length):
    return series.ewm(span=length, adjust=False).mean()


def RSI(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    value = 100 - (100 / (1 + rs))
    return value.fillna(50)


def ATR(df, length=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def add_indicators(df):
    if df.empty:
        return df

    df["ema20"] = EMA(df["close"], 20)
    df["ema50"] = EMA(df["close"], 50)
    df["rsi"] = RSI(df["close"], RSI_LENGTH)
    df["rsi_ma"] = df["rsi"].rolling(RSI_MA_LENGTH).mean()
    df["atr"] = ATR(df, 14)
    df["volume_ma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma"].replace(0, np.nan)

    return df


# ============================================================
# PIVOTS
# ============================================================

def pivots(df):
    highs = []
    lows = []

    if len(df) < 15:
        return highs, lows

    for i in range(PIVOT_LEFT, len(df) - PIVOT_RIGHT):
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]

        left_h = df["high"].iloc[i - PIVOT_LEFT:i]
        right_h = df["high"].iloc[i + 1:i + 1 + PIVOT_RIGHT]
        left_l = df["low"].iloc[i - PIVOT_LEFT:i]
        right_l = df["low"].iloc[i + 1:i + 1 + PIVOT_RIGHT]

        if h > left_h.max() and h > right_h.max():
            highs.append((i, h))

        if l < left_l.min() and l < right_l.min():
            lows.append((i, l))

    return highs, lows


# ============================================================
# 5M STRUCTURE
# ============================================================

def analyze_structure(df):
    if len(df) < 25:
        return None

    highs, lows = pivots(df)
    resistance = highs[-1][1] if highs else None
    support = lows[-1][1] if lows else None
    close = float(df["close"].iloc[-1])

    direction = "NONE"

    if resistance and close > resistance * (1 + BREAK_BUFFER_PCT / 100):
        direction = "BUY"
    elif support and close < support * (1 - BREAK_BUFFER_PCT / 100):
        direction = "SELL"

    return {
        "direction": direction,
        "resistance": resistance,
        "support": support,
        "close": close,
        "atr": float(df["atr"].iloc[-1]),
    }


# ============================================================
# 1 SECOND DATA
# ============================================================

def add_1s(symbol, candle):
    with data_lock:
        one_second_data[symbol].append(candle)
        one_second_data[symbol] = one_second_data[symbol][-600:]


# ============================================================
# 15 SECOND AGGREGATION
# ============================================================

def build_15s(symbol):
    with data_lock:
        rows = list(one_second_data[symbol])

    if not rows:
        return False

    buckets = {}
    for r in rows:
        bucket = (r["ts"] // 15000) * 15000
        buckets.setdefault(bucket, []).append(r)

    completed = []
    current_bucket = (int(time.time() * 1000) // 15000) * 15000

    for bucket, items in buckets.items():
        if bucket >= current_bucket:
            continue
        if len(items) < 10:
            continue

        items.sort(key=lambda x: x["ts"])

        completed.append({
            "ts": bucket,
            "open": items[0]["open"],
            "high": max(x["high"] for x in items),
            "low": min(x["low"] for x in items),
            "close": items[-1]["close"],
            "volume": sum(x["volume"] for x in items),
        })

    if not completed:
        return False

    df = pd.DataFrame(completed)
    df = df.drop_duplicates("ts").sort_values("ts")
    df = add_indicators(df)

    with data_lock:
        existing = pd.DataFrame(candles_15s[symbol])
        combined = pd.concat([existing, df], ignore_index=True)

        if not combined.empty:
            combined = combined.drop_duplicates("ts").sort_values("ts").tail(150)

        candles_15s[symbol] = combined.to_dict("records")

    return True


# ============================================================
# ENTRY CONFLUENCE (shared by live engine and backtest)
# ============================================================

def evaluate_entry(current, previous, direction):
    """Given the latest execution-timeframe candle (with
    indicators already computed) and the 5m structure
    direction, decide whether to enter. Returns a signal dict
    or None. Shared between the live 15s engine and the
    backtest engine so behaviour stays identical."""

    if direction not in ("BUY", "SELL"):
        return None

    candle_range = current["high"] - current["low"]
    if candle_range <= 0:
        return None

    body = abs(current["close"] - current["open"])
    body_ratio = body / candle_range

    if body_ratio < MIN_BODY_RATIO:
        return None

    volume_ratio = current.get("volume_ratio", np.nan)
    if pd.notna(volume_ratio) and volume_ratio < MIN_VOLUME_RATIO:
        return None

    atr = current.get("atr", np.nan)
    if pd.isna(atr) or atr <= 0:
        return None

    rsi = current.get("rsi", np.nan)
    rsi_ma = current.get("rsi_ma", np.nan)
    ema20 = current.get("ema20", np.nan)

    if pd.isna(rsi) or pd.isna(rsi_ma) or pd.isna(ema20):
        return None

    if direction == "BUY":
        checks = [
            current["close"] > current["open"],          # candle is bullish
            current["close"] > previous["close"],         # momentum up
            rsi > 50 and rsi > rsi_ma,                     # RSI confirms
            current["close"] > ema20,                      # above EMA20
        ]
        side = "buy"
    else:
        checks = [
            current["close"] < current["open"],
            current["close"] < previous["close"],
            rsi < 50 and rsi < rsi_ma,
            current["close"] < ema20,
        ]
        side = "sell"

    confirmations = sum(1 for c in checks if c)

    if confirmations < MIN_CONFIRMATIONS:
        return None

    return {
        "side": side,
        "price": float(current["close"]),
        "atr": float(atr),
        "confirmations": confirmations,
        "reason": f"5M BOS + execution-TF confluence ({confirmations}/4)",
    }


def find_entry(symbol, structure):
    with data_lock:
        rows = list(candles_15s[symbol])

    if len(rows) < 20:
        return None

    df = pd.DataFrame(rows)
    df = add_indicators(df)

    current = df.iloc[-1]
    previous = df.iloc[-2]

    return evaluate_entry(current, previous, structure["direction"])


# ============================================================
# MARKET ORDER
# ============================================================

def market_order(symbol, side, size, reduce_only=False):
    if not api_ready():
        return None

    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": side,
        "ordType": "market",
        "sz": format_decimal(size),
    }

    if reduce_only:
        payload["reduceOnly"] = True

    log(f"[ORDER REQUEST] {symbol} | {side.upper()} | contracts={size} | reduceOnly={reduce_only}")

    result = private_post("/api/v5/trade/order", payload)
    log(f"[ORDER RESPONSE] {result}")

    data = result.get("data", [])
    if not data:
        log("[ORDER FAILED] No data")
        return None

    item = data[0]
    if item.get("sCode") != "0":
        log("[ORDER FAILED] " + str(item))
        return None

    log(f"[ORDER ACCEPTED] ordId={item.get('ordId')}")
    return item


# ============================================================
# OPEN POSITION
# ============================================================

def open_position(symbol, signal):
    with data_lock:
        already_open = symbol in positions
        cooldown_ok = (time.time() - last_trade_time.get(symbol, 0)) >= COOLDOWN_SECONDS

    if already_open:
        log(f"[WAIT] {symbol} already has position")
        return

    if not cooldown_ok:
        log(f"[WAIT] {symbol} cooldown")
        return

    if state["daily_pnl"] <= -MAX_DAILY_LOSS_USDT:
        log("[RISK STOP] Daily loss limit")
        return

    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        log("[RISK STOP] Consecutive losses")
        return

    price = signal["price"]
    size, actual_notional, actual_margin = calculate_size(symbol, price)

    if size <= 0:
        log(f"[NO TRADE] {symbol} because target size is below OKX minimum")
        return

    state["signals"] += 1

    log(
        f"[SIGNAL] {symbol} | {signal['side'].upper()} | price={price} | "
        f"contracts={size} | notional≈{actual_notional:.4f} USDT | margin≈{actual_margin:.4f} USDT"
    )

    if not AUTO_TRADE:
        log(f"🟡 SIGNAL ONLY | {symbol} | {signal['side'].upper()}")
        send_whatsapp(
            "🟡 OKX SIGNAL\n"
            f"{symbol}\nSide: {signal['side'].upper()}\nPrice: {price}\n"
            f"Contracts: {size}\nNotional: {actual_notional:.4f} USDT\n"
            f"Margin: {actual_margin:.4f} USDT\n{signal['reason']}"
        )
        return

    order = market_order(symbol, signal["side"], size)
    if not order:
        return

    with data_lock:
        positions[symbol] = {
            "side": signal["side"],
            "entry": price,
            "size": size,
            "notional": actual_notional,
            "margin": actual_margin,
            "atr": signal["atr"],
            "time": time.time(),
            "best_price": price,
            "ord_id": order.get("ordId"),
        }
        last_trade_time[symbol] = time.time()

    state["trades"] += 1

    log(
        f"🟢 POSITION OPENED | {symbol} | {signal['side'].upper()} | "
        f"entry={price} | contracts={size} | notional≈{actual_notional:.4f}"
    )

    send_whatsapp(
        "🟢 OKX DEMO TRADE OPENED\n"
        f"{symbol}\nSide: {signal['side'].upper()}\nEntry: {price}\n"
        f"Contracts: {size}\nNotional: {actual_notional:.4f} USDT\nMargin: {actual_margin:.4f} USDT"
    )


# ============================================================
# CLOSE POSITION
# ============================================================

def close_position(symbol, price, reason):
    with data_lock:
        position = positions.get(symbol)

    if not position:
        return

    side = position["side"]
    close_side = "sell" if side == "buy" else "buy"

    if AUTO_TRADE:
        order = market_order(symbol, close_side, position["size"], reduce_only=True)
        if not order:
            log(f"[CLOSE FAILED] {symbol}")
            return

    entry = position["entry"]

    if side == "buy":
        pnl = (price - entry) * position["size"]
    else:
        pnl = (entry - price) * position["size"]

    state["daily_pnl"] += pnl

    if pnl >= 0:
        state["wins"] += 1
        state["consecutive_losses"] = 0
    else:
        state["losses"] += 1
        state["consecutive_losses"] += 1

    # Mark WHICH exit operation fired (TP / SL / Trailing / other),
    # separate from win/loss, so it's easy to see how many trades
    # were closed by each rule specifically.
    is_tp = reason == "Take-Profit hit"
    is_sl = reason == "Emergency ATR SL"
    is_trail = reason == "Trailing exit"

    if is_tp:
        state["tp_hits"] += 1
        tag = "🎯 TAKE-PROFIT"
    elif is_sl:
        state["sl_hits"] += 1
        tag = "🛑 STOP-LOSS"
    elif is_trail:
        state["trail_hits"] += 1
        tag = "🧵 TRAILING-SL"
    else:
        state["other_exits"] += 1
        tag = "⚪ OTHER"

    log(
        f"🔴 POSITION CLOSED | {tag} | {symbol} | entry={entry} | "
        f"exit={price} | PnL≈{pnl:.4f} | reason={reason}"
    )

    send_whatsapp(
        f"🔴 OKX DEMO TRADE CLOSED [{tag}]\n"
        f"{symbol}\nSide: {side.upper()}\nEntry: {entry}\nExit: {price}\n"
        f"Estimated PnL: {pnl:.4f} USDT\nReason: {reason}"
    )

    with data_lock:
        del positions[symbol]


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def manage_position(symbol):
    with data_lock:
        position = positions.get(symbol)
        rows = list(candles_15s[symbol]) if position else None

    if not position or not rows:
        return

    df = pd.DataFrame(rows)
    df = add_indicators(df)
    current = df.iloc[-1]

    price = float(current["close"])
    atr = float(current["atr"])
    age = time.time() - position["time"]
    side = position["side"]
    entry = position["entry"]

    if side == "buy":
        if price > position["best_price"]:
            position["best_price"] = price

        take_profit = entry + atr * TP_ATR_MULT
        emergency_sl = entry - atr * EMERGENCY_SL_ATR
        trailing_sl = position["best_price"] - atr * TRAIL_ATR_MULT

        if price >= take_profit:
            close_position(symbol, price, "Take-Profit hit")
            return

        if price <= emergency_sl:
            close_position(symbol, price, "Emergency ATR SL")
            return

        if age >= TRAIL_START_SECONDS and price <= trailing_sl:
            close_position(symbol, price, "Trailing exit")
            return

        if age >= MIN_HOLD_SECONDS and price < current["ema20"]:
            close_position(symbol, price, "15S momentum failure")
            return

    else:
        if price < position["best_price"]:
            position["best_price"] = price

        take_profit = entry - atr * TP_ATR_MULT
        emergency_sl = entry + atr * EMERGENCY_SL_ATR
        trailing_sl = position["best_price"] + atr * TRAIL_ATR_MULT

        if price <= take_profit:
            close_position(symbol, price, "Take-Profit hit")
            return

        if price >= emergency_sl:
            close_position(symbol, price, "Emergency ATR SL")
            return

        if age >= TRAIL_START_SECONDS and price >= trailing_sl:
            close_position(symbol, price, "Trailing exit")
            return

        if age >= MIN_HOLD_SECONDS and price > current["ema20"]:
            close_position(symbol, price, "15S momentum failure")
            return

    if age >= MAX_HOLD_SECONDS:
        close_position(symbol, price, "Maximum 30 second hold")


# ============================================================
# WHATSAPP
# ============================================================

def send_whatsapp(message):
    if not all([WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_TO_NUMBER, WHATSAPP_API_VERSION]):
        return False

    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"

    payload = {
        "messaging_product": "whatsapp",
        "to": WHATSAPP_TO_NUMBER,
        "type": "text",
        "text": {"body": message},
    }

    try:
        r = requests.post(
            url,
            headers={"Authorization": "Bearer " + WHATSAPP_ACCESS_TOKEN, "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        log(f"[WHATSAPP] {r.status_code}")
        return r.ok
    except Exception as e:
        log(f"[WHATSAPP ERROR] {e}")
        return False


# ============================================================
# WEBSOCKET MESSAGE
# ============================================================

def ws_message(ws, message):
    try:
        obj = json.loads(message)

        if obj.get("event") == "error":
            log(f"❌ WS ERROR: {obj}")
            state["last_error"] = str(obj)
            return

        if obj.get("event") == "subscribe":
            log(f"✅ WS SUBSCRIBED: {obj.get('arg')}")
            return

        arg = obj.get("arg", {})
        channel = arg.get("channel")
        symbol = arg.get("instId")

        if channel != "candle1s":
            return

        if symbol not in valid_symbols:
            return

        data = obj.get("data", [])
        if not data:
            return

        r = data[0]
        candle = {
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }

        add_1s(symbol, candle)

        state["last_data"] = {"symbol": symbol, "price": candle["close"], "time": candle["ts"]}

        built = build_15s(symbol)
        if built:
            log(f"[15S] {symbol} candle formed")

        manage_position(symbol)

    except Exception as e:
        state["last_error"] = str(e)
        log(f"❌ WS MESSAGE ERROR: {e}")


# ============================================================
# WEBSOCKET OPEN / ERROR / CLOSE
# ============================================================

def ws_open(ws):
    state["ws_connected"] = True
    state["status"] = "RUNNING"

    log("==========================================")
    log("✅ OKX WEBSOCKET CONNECTED")
    log(f"Demo={OKX_DEMO}")
    log("Symbols=" + str(valid_symbols))
    log("==========================================")

    args = [{"channel": "candle1s", "instId": symbol} for symbol in valid_symbols]

    if not args:
        log("❌ No valid symbols")
        return

    ws.send(json.dumps({"op": "subscribe", "args": args}))
    log("📡 Subscription request sent")


def ws_error(ws, error):
    state["ws_connected"] = False
    state["last_error"] = str(error)
    log(f"❌ WS ERROR: {error}")


def ws_close(ws, code, message):
    state["ws_connected"] = False
    log(f"⚠️ WS CLOSED: {code} {message}")
    log("Reconnecting automatically...")


# ============================================================
# WEBSOCKET LOOP
# ============================================================

def websocket_loop():
    while True:
        try:
            if not valid_symbols:
                validate_symbols()

            log("Connecting to OKX WebSocket...")

            ws = websocket.WebSocketApp(
                OKX_WS_BUSINESS,
                on_open=ws_open,
                on_message=ws_message,
                on_error=ws_error,
                on_close=ws_close,
            )

            ws.run_forever(ping_interval=15, ping_timeout=10)

        except Exception as e:
            state["last_error"] = str(e)
            log(f"❌ WS LOOP ERROR: {e}")

        state["ws_connected"] = False
        log("⏳ WebSocket reconnect in 5 seconds...")
        time.sleep(5)


# ============================================================
# STRUCTURE LOOP
# ============================================================

def structure_loop():
    last_status = {}

    while True:
        try:
            for symbol in valid_symbols:
                df = get_5m_candles(symbol)

                if df.empty:
                    log(f"[5M] {symbol}: no candle data")
                    continue

                if len(df) > 1:
                    df = df.iloc[:-1].copy()

                structure = analyze_structure(df)
                if not structure:
                    continue

                direction = structure["direction"]
                price = structure["close"]

                if last_status.get(symbol) != direction:
                    log(
                        f"[5M STRUCTURE] {symbol} | {direction} | price={price} | "
                        f"R={structure['resistance']} | S={structure['support']}"
                    )
                    last_status[symbol] = direction

                signal = find_entry(symbol, structure)

                if signal:
                    log(
                        f"🚨 SIGNAL FOUND | {symbol} | {signal['side'].upper()} | "
                        f"price={signal['price']} | {signal['reason']}"
                    )
                    state["last_signal"] = {"symbol": symbol, **signal}
                    open_position(symbol, signal)

        except Exception as e:
            state["last_error"] = str(e)
            log(f"❌ STRUCTURE ERROR: {e}")

        time.sleep(5)


# ============================================================
# BACKTEST ENGINE
# ============================================================
# OKX only retains a short window of 1s/15s candle history, so
# a true 15s backtest is not possible far into the past. This
# engine approximates the live strategy using 5m candles for
# structure (identical to live) and 1m candles as the execution
# timeframe stand-in for the 15s candles (same confluence logic
# via evaluate_entry, same TP/SL/trailing/timeout rules, scaled
# to 1m bars). Treat results as a directional approximation of
# the live engine, not an exact replay of it.

def backtest_symbol(symbol, limit=1500):
    struct_df = fetch_candles(symbol, "5m", limit // 5 or 100, history=True)
    exec_df = fetch_candles(symbol, "1m", limit, history=True)

    if struct_df.empty or exec_df.empty:
        return {"symbol": symbol, "error": "no historical data returned"}

    struct_df = add_indicators(struct_df)
    exec_df = add_indicators(exec_df)

    trades = []
    open_trade = None

    # Pre-compute a 5m structure "as of" each 1m timestamp using merge_asof
    struct_small = struct_df[["ts", "high", "low", "close", "atr"]].copy()

    directions = []
    highs_cache, lows_cache = pivots(struct_df)

    # Walk forward through the 5m series to get a direction at
    # each 5m close (same rule as analyze_structure, but vectorised
    # per-step so we can look it up quickly per 1m candle).
    for i in range(len(struct_df)):
        sub = struct_df.iloc[: i + 1]
        if len(sub) < 25:
            directions.append("NONE")
            continue
        struct = analyze_structure(sub)
        directions.append(struct["direction"] if struct else "NONE")

    struct_small["direction"] = directions

    # Assign each 1m bar the most recent completed 5m direction.
    exec_df = exec_df.sort_values("ts")
    struct_small = struct_small.sort_values("ts")

    exec_df = pd.merge_asof(
        exec_df, struct_small[["ts", "direction"]], on="ts", direction="backward"
    )

    for i in range(1, len(exec_df) - 1):
        current = exec_df.iloc[i]
        previous = exec_df.iloc[i - 1]
        direction = current.get("direction", "NONE")

        if open_trade is None:
            signal = evaluate_entry(current, previous, direction)
            if signal:
                open_trade = {
                    "side": signal["side"],
                    "entry": signal["price"],
                    "entry_ts": int(current["ts"]),
                    "atr": signal["atr"],
                    "best_price": signal["price"],
                    "bars_held": 0,
                }
            continue

        # manage open trade using the same TP / SL / trailing rules,
        # one "bar" standing in for elapsed time
        open_trade["bars_held"] += 1
        price = float(current["close"])
        atr = open_trade["atr"]
        entry = open_trade["entry"]
        side = open_trade["side"]
        exit_reason = None

        if side == "buy":
            open_trade["best_price"] = max(open_trade["best_price"], price)
            take_profit = entry + atr * TP_ATR_MULT
            emergency_sl = entry - atr * EMERGENCY_SL_ATR
            trailing_sl = open_trade["best_price"] - atr * TRAIL_ATR_MULT

            if price >= take_profit:
                exit_reason = "TP"
            elif price <= emergency_sl:
                exit_reason = "SL"
            elif open_trade["bars_held"] >= 1 and price <= trailing_sl:
                exit_reason = "Trail"
        else:
            open_trade["best_price"] = min(open_trade["best_price"], price)
            take_profit = entry - atr * TP_ATR_MULT
            emergency_sl = entry + atr * EMERGENCY_SL_ATR
            trailing_sl = open_trade["best_price"] + atr * TRAIL_ATR_MULT

            if price <= take_profit:
                exit_reason = "TP"
            elif price >= emergency_sl:
                exit_reason = "SL"
            elif open_trade["bars_held"] >= 1 and price >= trailing_sl:
                exit_reason = "Trail"

        if open_trade["bars_held"] >= 6 and not exit_reason:
            exit_reason = "MaxHold"

        if exit_reason:
            pnl_pct = (
                (price - entry) / entry if side == "buy" else (entry - price) / entry
            )
            trades.append({
                "side": side,
                "entry": entry,
                "exit": price,
                "reason": exit_reason,
                "pnl_pct": pnl_pct,
            })
            open_trade = None

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    total_pct = sum(t["pnl_pct"] for t in trades)

    tp_count = sum(1 for t in trades if t["reason"] == "TP")
    sl_count = sum(1 for t in trades if t["reason"] == "SL")
    trail_count = sum(1 for t in trades if t["reason"] == "Trail")
    other_count = sum(1 for t in trades if t["reason"] not in ("TP", "SL", "Trail"))

    return {
        "symbol": symbol,
        "bars_tested": len(exec_df),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(trades), 2) if trades else 0,
        "total_return_pct": round(total_pct * 100, 3),
        "avg_return_per_trade_pct": round((total_pct / len(trades)) * 100, 4) if trades else 0,
        "tp_hits": tp_count,
        "sl_hits": sl_count,
        "trail_hits": trail_count,
        "other_exits": other_count,
    }


def backtest_all(symbols=None, limit=1500):
    symbols = symbols or (valid_symbols or REQUESTED_SYMBOLS)
    results = []

    for symbol in symbols:
        log(f"[BACKTEST] running {symbol}...")
        try:
            results.append(backtest_symbol(symbol, limit=limit))
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "min_body_ratio": MIN_BODY_RATIO,
            "min_volume_ratio": MIN_VOLUME_RATIO,
            "min_confirmations": MIN_CONFIRMATIONS,
            "tp_atr_mult": TP_ATR_MULT,
            "emergency_sl_atr": EMERGENCY_SL_ATR,
            "risk_reward_ratio": RISK_REWARD_RATIO,
            "trail_atr_mult": TRAIL_ATR_MULT,
        },
        "results": results,
    }

    state["last_backtest"] = summary
    return summary


# ============================================================
# DASHBOARD
# ============================================================

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ICT SwiftEdge</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: Arial, sans-serif; background: linear-gradient(135deg, #0f172a, #111827, #020617); color: #e5e7eb; }
.container { max-width: 1200px; margin: auto; padding: 18px; }
.header { padding: 22px; border-radius: 20px; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10); box-shadow: 0 10px 35px rgba(0,0,0,0.25); margin-bottom: 18px; }
.title { font-size: 28px; font-weight: 800; }
.subtitle { color: #94a3b8; margin-top: 5px; }
.badge { display: inline-block; margin-top: 12px; padding: 7px 13px; border-radius: 20px; background: #172554; color: #93c5fd; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 18px; }
.card { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.09); border-radius: 18px; padding: 18px; min-height: 105px; }
.label { color: #94a3b8; font-size: 13px; }
.value { font-size: 25px; font-weight: 800; margin-top: 8px; }
.green { color: #4ade80; } .red { color: #fb7185; } .yellow { color: #facc15; } .blue { color: #60a5fa; }
.section { margin-top: 18px; margin-bottom: 10px; font-size: 18px; font-weight: 800; display: flex; align-items: center; justify-content: space-between; }
.symbols { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
.symbol { background: rgba(255,255,255,0.05); border-radius: 15px; padding: 15px; border: 1px solid rgba(255,255,255,0.08); }
.symbol-name { font-weight: 800; }
.symbol-status { margin-top: 7px; font-size: 13px; color: #94a3b8; }
.position { padding: 15px; border-radius: 15px; background: rgba(255,255,255,0.05); margin-bottom: 10px; }
.logs { height: 300px; overflow-y: auto; background: #020617; border-radius: 15px; padding: 15px; font-family: monospace; font-size: 12px; color: #cbd5e1; }
.footer { text-align: center; color: #64748b; padding: 25px; }
button.run-bt { background: #2563eb; color: white; border: none; padding: 8px 16px; border-radius: 10px; font-weight: 700; cursor: pointer; font-size: 13px; }
button.run-bt:disabled { opacity: 0.5; cursor: default; }
table.bt { width: 100%; border-collapse: collapse; font-size: 13px; }
table.bt th, table.bt td { text-align: left; padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,0.08); }
table.bt th { color: #94a3b8; font-weight: 700; }
</style>
</head>
<body>
<div class="container">

<div class="header">
<div class="title">⚡ ICT SwiftEdge</div>
<div class="subtitle">OKX Futures Scalper • 5M Structure • 15S Execution</div>
<div id="mode" class="badge">Loading...</div>
<div id="rr" class="badge" style="margin-left:8px; background:#052e1a; color:#4ade80;">R:R -</div>
</div>

<div class="grid">
<div class="card"><div class="label">Bot Status</div><div id="status" class="value">-</div></div>
<div class="card"><div class="label">WebSocket</div><div id="ws" class="value">-</div></div>
<div class="card"><div class="label">Trades</div><div id="trades" class="value">0</div></div>
<div class="card"><div class="label">Win Rate</div><div id="winrate" class="value">0%</div></div>
<div class="card"><div class="label">Daily PnL</div><div id="pnl" class="value">0</div></div>
<div class="card"><div class="label">Signals</div><div id="signals" class="value">0</div></div>
<div class="card"><div class="label">🎯 TP Hits</div><div id="tpHits" class="value green">0</div></div>
<div class="card"><div class="label">🛑 SL Hits</div><div id="slHits" class="value red">0</div></div>
<div class="card"><div class="label">🧵 Trail Exits</div><div id="trailHits" class="value blue">0</div></div>
<div class="card"><div class="label">⚪ Other Exits</div><div id="otherExits" class="value">0</div></div>
</div>

<div class="section">📊 Markets</div>
<div id="symbols" class="symbols"></div>

<div class="section">📌 Open Positions</div>
<div id="positions">No open positions</div>

<div class="section">🚨 Last Signal</div>
<div class="card" id="lastSignal">No signal yet</div>

<div class="section">
🧪 Backtest
<button class="run-bt" id="btBtn" onclick="runBacktest()">Run Backtest</button>
</div>
<div class="card" id="backtestBox">No backtest run yet</div>

<div class="section">🧾 Live Logs</div>
<div class="logs" id="logs">Loading logs...</div>

<div class="footer">ICT SwiftEdge • OKX</div>

</div>

<script>

async function runBacktest() {
    const btn = document.getElementById('btBtn');
    btn.disabled = true;
    btn.innerText = 'Running...';
    document.getElementById('backtestBox').innerHTML = 'Running backtest, this can take a bit...';
    try {
        const response = await fetch('/api/backtest');
        const data = await response.json();
        renderBacktest(data);
    } catch (e) {
        document.getElementById('backtestBox').innerHTML = 'Backtest failed: ' + e;
    }
    btn.disabled = false;
    btn.innerText = 'Run Backtest';
}

function renderBacktest(data) {
    if (!data || !data.results) {
        document.getElementById('backtestBox').innerHTML = 'No results';
        return;
    }
    let rows = '';
    data.results.forEach(function(r) {
        if (r.error) {
            rows += `<tr><td>${r.symbol}</td><td colspan="6">${r.error}</td></tr>`;
            return;
        }
        rows += `<tr>
            <td>${r.symbol}</td>
            <td>${r.trades}</td>
            <td>${r.wins}</td>
            <td>${r.losses}</td>
            <td>${r.win_rate}%</td>
            <td class="${r.total_return_pct >= 0 ? 'green' : 'red'}">${r.total_return_pct}%</td>
            <td>${r.avg_return_per_trade_pct}%</td>
            <td class="green">${r.tp_hits}</td>
            <td class="red">${r.sl_hits}</td>
            <td class="blue">${r.trail_hits}</td>
        </tr>`;
    });
    document.getElementById('backtestBox').innerHTML = `
        <table class="bt">
            <thead><tr>
                <th>Symbol</th><th>Trades</th><th>Wins</th><th>Losses</th>
                <th>Win Rate</th><th>Total Return</th><th>Avg / Trade</th>
                <th>🎯 TP</th><th>🛑 SL</th><th>🧵 Trail</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <div style="margin-top:10px; color:#94a3b8; font-size:12px;">
            Run at ${data.run_at} • approximated with 1m execution candles (OKX keeps limited 1s/15s history)
        </div>
    `;
}

async function updateDashboard() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();

        document.getElementById('status').innerText = data.status || '-';
        document.getElementById('ws').innerText = data.ws_connected ? 'CONNECTED' : 'OFFLINE';
        document.getElementById('ws').className = data.ws_connected ? 'value green' : 'value red';
        document.getElementById('trades').innerText = data.trades || 0;

        let total = (data.wins || 0) + (data.losses || 0);
        let winrate = total > 0 ? ((data.wins / total) * 100).toFixed(1) : '0';
        document.getElementById('winrate').innerText = winrate + '%';

        const pnl = Number(data.daily_pnl || 0);
        document.getElementById('pnl').innerText = pnl.toFixed(4);
        document.getElementById('pnl').className = pnl >= 0 ? 'value green' : 'value red';

        document.getElementById('signals').innerText = data.signals || 0;
        document.getElementById('tpHits').innerText = data.tp_hits || 0;
        document.getElementById('slHits').innerText = data.sl_hits || 0;
        document.getElementById('trailHits').innerText = data.trail_hits || 0;
        document.getElementById('otherExits').innerText = data.other_exits || 0;
        document.getElementById('mode').innerText = data.demo ? '🟢 OKX DEMO' : '🔴 LIVE';
        if (data.risk_reward_ratio) {
            document.getElementById('rr').innerText = `R:R  1 : ${data.risk_reward_ratio}  (SL ${data.sl_atr_mult}x / TP ${data.tp_atr_mult}x ATR)`;
        }

        let symbolsHTML = '';
        (data.symbols || []).forEach(function(symbol) {
            symbolsHTML += `<div class="symbol"><div class="symbol-name">${symbol}</div><div class="symbol-status">ACTIVE</div></div>`;
        });
        if (!symbolsHTML) symbolsHTML = '<div class="card">No active symbols</div>';
        document.getElementById('symbols').innerHTML = symbolsHTML;

        let positions = data.positions || {};
        let positionHTML = '';
        Object.keys(positions).forEach(function(symbol) {
            let p = positions[symbol];
            positionHTML += `<div class="position">
                <b>${symbol}</b><br>
                Side: <span class="${p.side === 'buy' ? 'green' : 'red'}">${p.side.toUpperCase()}</span><br>
                Entry: ${p.entry}<br>
                Contracts: ${p.size}<br>
                Notional: ${Number(p.notional || 0).toFixed(4)} USDT<br>
                Margin: ${Number(p.margin || 0).toFixed(4)} USDT
            </div>`;
        });
        if (!positionHTML) positionHTML = '<div class="card">No open positions</div>';
        document.getElementById('positions').innerHTML = positionHTML;

        let s = data.last_signal;
        if (s) {
            document.getElementById('lastSignal').innerHTML = `
                <b>${s.symbol}</b><br>
                Side: <span class="${s.side === 'buy' ? 'green' : 'red'}">${s.side.toUpperCase()}</span><br>
                Price: ${s.price}<br>
                Reason: ${s.reason || '-'}
            `;
        }

        if (data.last_backtest) {
            renderBacktest(data.last_backtest);
        }

        let logs = data.logs || [];
        document.getElementById('logs').innerHTML = logs.join('<br>');
        let logBox = document.getElementById('logs');
        logBox.scrollTop = logBox.scrollHeight;

    } catch (error) {
        console.log(error);
    }
}

updateDashboard();
setInterval(updateDashboard, 2000);

</script>
</body>
</html>
"""


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def home():
    return render_template_string(DASHBOARD_HTML)


@app.route("/health")
def health():
    return "OK", 200


@app.route("/api/status")
def status():
    total = state["wins"] + state["losses"]
    winrate = (state["wins"] / total) * 100 if total > 0 else 0

    with data_lock:
        positions_copy = dict(positions)

    return jsonify({
        **state,
        "demo": OKX_DEMO,
        "auto_trade": AUTO_TRADE,
        "leverage": LEVERAGE,
        "margin_usdt": MARGIN_USDT,
        "target_notional": MARGIN_USDT * LEVERAGE,
        "tp_atr_mult": TP_ATR_MULT,
        "sl_atr_mult": EMERGENCY_SL_ATR,
        "risk_reward_ratio": RISK_REWARD_RATIO,
        "websocket": state["ws_connected"],
        "symbols": valid_symbols,
        "symbol_status": symbol_status,
        "positions": positions_copy,
        "winrate": winrate,
        "logs": log_history[-150:],
    })


@app.route("/api/backtest")
def api_backtest():
    """Run (or re-run) a backtest on demand. Optional query
    params: symbols=BTC-USDT-SWAP,ETH-USDT-SWAP  limit=1500"""
    symbols_param = request.args.get("symbols")
    symbols = symbols_param.split(",") if symbols_param else None
    limit = int(request.args.get("limit", "1500"))

    summary = backtest_all(symbols=symbols, limit=limit)
    return jsonify(summary)


# ============================================================
# START
# ============================================================

def start():
    log("")
    log("==========================================")
    log("🚀 ICT SWIFTEDGE OKX SCALPER")
    log("==========================================")
    log(f"OKX DEMO: {OKX_DEMO}")
    log(f"AUTO TRADE: {AUTO_TRADE}")
    log(f"MARGIN: {MARGIN_USDT} USDT")
    log(f"LEVERAGE: {LEVERAGE}x")
    log(f"TARGET NOTIONAL: {MARGIN_USDT * LEVERAGE} USDT")
    log(f"TP (ATR mult): {TP_ATR_MULT}")
    log(f"SL (ATR mult): {EMERGENCY_SL_ATR}")
    log(f"Risk:Reward enforced = 1 : {RISK_REWARD_RATIO}")
    log(f"MIN CONFIRMATIONS: {MIN_CONFIRMATIONS}/4")
    log(f"WS URL: {OKX_WS_BUSINESS}")
    log("Requested symbols: " + ", ".join(REQUESTED_SYMBOLS))

    if not api_ready():
        log("⚠️ API credentials not complete")

    validate_symbols()

    threading.Thread(target=websocket_loop, daemon=True).start()
    threading.Thread(target=structure_loop, daemon=True).start()

    state["status"] = "RUNNING"
    log("✅ Bot threads started")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    if BACKTEST_MODE:
        validate_symbols()
        summary = backtest_all()
        print(json.dumps(summary, indent=2))
    else:
        start()
        app.run(host="0.0.0.0", port=PORT, threaded=True)
