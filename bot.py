# ============================================================
# OKX 5M BOS + RETEST + 15S SCALPING BOT
# Railway Ready - Single bot.py
# ============================================================

import os
import time
import json
import hmac
import base64
import hashlib
import threading
from datetime import datetime, timezone

import requests
import pandas as pd
from flask import Flask, jsonify
import websocket


# ============================================================
# CONFIG
# ============================================================

OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://us.okx.com").rstrip("/")

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

# Demo by default
OKX_DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"

AUTO_TRADE = os.getenv("AUTO_TRADE", "true").lower() == "true"

# Live trading is deliberately blocked unless explicitly enabled
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

MARGIN_USDT = float(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "3"))

TD_MODE = os.getenv("TD_MODE", "isolated")

# Do not automatically oversize small accounts
ALLOW_MIN_SIZE_OVERSIZE = (
    os.getenv("ALLOW_MIN_SIZE_OVERSIZE", "false").lower() == "true"
)


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
# STRATEGY SETTINGS
# ============================================================

# 5-minute structure
STRUCTURE_LOOKBACK = int(os.getenv("STRUCTURE_LOOKBACK", "100"))

# 0.015% breakout buffer
BREAK_BUFFER_PCT = float(
    os.getenv("BREAK_BUFFER_PCT", "0.015")
) / 100.0

# Candle body quality
MIN_BODY_RATIO = float(os.getenv("MIN_BODY_RATIO", "0.35"))

# Volume is a soft filter, not an aggressive filter
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "0.80"))

# Retest tolerance around broken level
RETEST_TOLERANCE_PCT = float(
    os.getenv("RETEST_TOLERANCE_PCT", "0.08")
) / 100.0

# Number of completed 15-second candles allowed for retest
RETEST_MAX_15S = int(os.getenv("RETEST_MAX_15S", "8"))


# ============================================================
# POSITION MANAGEMENT
# ============================================================

# Maximum normal holding time
MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", "30"))

# Minimum time between new trades
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "45"))

# Emergency hard percentage stop
EMERGENCY_SL_PCT = float(
    os.getenv("EMERGENCY_SL_PCT", "0.35")
) / 100.0

# Small buffer beyond retest extreme
SL_BUFFER_PCT = float(
    os.getenv("SL_BUFFER_PCT", "0.02")
) / 100.0

# Break-even activation
BE_TRIGGER_PCT = float(
    os.getenv("BE_TRIGGER_PCT", "0.25")
) / 100.0

# Once BE activates, lock a tiny amount
BE_LOCK_PCT = float(
    os.getenv("BE_LOCK_PCT", "0.02")
) / 100.0

# Trailing starts here
TRAIL_TRIGGER_PCT = float(
    os.getenv("TRAIL_TRIGGER_PCT", "0.35")
) / 100.0

# Trailing distance
TRAIL_DISTANCE_PCT = float(
    os.getenv("TRAIL_DISTANCE_PCT", "0.15")
) / 100.0

# Opposite candle exit
OPPOSITE_EXIT_ENABLED = (
    os.getenv("OPPOSITE_EXIT_ENABLED", "true").lower() == "true"
)

OPPOSITE_BODY_RATIO = float(
    os.getenv("OPPOSITE_BODY_RATIO", "0.60")
)

# Round trip estimated cost
ROUND_TRIP_COST_USDT = float(
    os.getenv("ROUND_TRIP_COST_USDT", "0.05")
)


# ============================================================
# SERVER
# ============================================================

PORT = int(os.getenv("PORT", "8080"))


# ============================================================
# GLOBAL STATE
# ============================================================

app = Flask(__name__)

state_lock = threading.RLock()

ACTIVE_SYMBOLS = []

INSTRUMENTS = {}

ONE_SEC_DATA = {}
BARS_15S = {}

LAST_15S_BUCKET = {}

LAST_5M_ANALYSIS = {}

PENDING_BREAKOUT = {}

POSITIONS = {}

LAST_TRADE_TIME = {}

LAST_PRICE = {}
LAST_PRICE_TIME = {}

CLOSING = set()

BOT_START_TIME = time.time()

LAST_ERROR = ""

WS_STATUS = "starting"

POS_MODE = "net_mode"


# ============================================================
# LOGGING
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def set_error(message):
    global LAST_ERROR
    LAST_ERROR = str(message)
    log(f"[ERROR] {message}")


# ============================================================
# TIME
# ============================================================

def utc_ts():
    return time.time()


def iso_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


# ============================================================
# OKX AUTH
# ============================================================

def okx_signature(timestamp, method, request_path, body=""):
    message = timestamp + method.upper() + request_path + body

    digest = hmac.new(
        OKX_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(digest).decode()


def okx_headers(method, request_path, body=""):
    timestamp = iso_timestamp()

    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": okx_signature(
            timestamp,
            method,
            request_path,
            body
        ),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
    }

    if OKX_DEMO:
        headers["x-simulated-trading"] = "1"

    return headers


# ============================================================
# REST REQUEST
# ============================================================

def rest_request(
    method,
    path,
    params=None,
    body=None,
    private=False,
    timeout=10
):
    global LAST_ERROR

    method = method.upper()

    query = ""

    if params:
        parts = []

        for key, value in params.items():
            if value is not None:
                parts.append(
                    f"{key}={requests.utils.quote(str(value), safe='')}"
                )

        if parts:
            query = "?" + "&".join(parts)

    request_path = path + query

    body_text = ""

    if body is not None:
        body_text = json.dumps(
            body,
            separators=(",", ":")
        )

    url = OKX_BASE_URL + request_path

    try:

        if private:
            headers = okx_headers(
                method,
                request_path,
                body_text
            )
        else:
            headers = {
                "Content-Type": "application/json"
            }

            if OKX_DEMO:
                headers["x-simulated-trading"] = "1"

        response = requests.request(
            method,
            url,
            params=None,
            data=body_text if body is not None else None,
            headers=headers,
            timeout=timeout
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "code": str(response.status_code),
                "msg": response.text
            }

        if response.status_code >= 400:
            set_error(
                f"HTTP {response.status_code}: {data}"
            )
            return None

        if isinstance(data, dict):
            if str(data.get("code", "0")) != "0":
                set_error(
                    f"OKX API: {data.get('code')} {data.get('msg')}"
                )
                return data

        return data

    except Exception as e:
        set_error(
            f"REST {method} {path}: {e}"
        )
        return None


# ============================================================
# SYMBOL VALIDATION
# ============================================================

def validate_symbols():

    global ACTIVE_SYMBOLS
    global INSTRUMENTS

    log("[SYMBOL] Checking OKX instruments...")

    ACTIVE_SYMBOLS = []
    INSTRUMENTS = {}

    for symbol in REQUESTED_SYMBOLS:

        data = rest_request(
            "GET",
            "/api/v5/public/instruments",
            params={
                "instType": "SWAP",
                "instId": symbol
            },
            private=False
        )

        if not data:
            log(f"[SYMBOL SKIP] {symbol}")
            continue

        rows = data.get("data", [])

        if not rows:
            log(
                f"[SYMBOL SKIP] {symbol} does not exist on this endpoint"
            )
            continue

        info = rows[0]

        if info.get("state") not in (None, "", "live"):
            log(
                f"[SYMBOL SKIP] {symbol} state={info.get('state')}"
            )
            continue

        try:
            INSTRUMENTS[symbol] = {
                "ctVal": float(info.get("ctVal", "1")),
                "lotSz": float(info.get("lotSz", "1")),
                "minSz": float(info.get("minSz", "1")),
                "tickSz": float(info.get("tickSz", "0.0001")),
                "ctType": info.get("ctType", ""),
            }

            ACTIVE_SYMBOLS.append(symbol)

            log(
                f"[SYMBOL OK] {symbol} "
                f"ctVal={INSTRUMENTS[symbol]['ctVal']} "
                f"lotSz={INSTRUMENTS[symbol]['lotSz']} "
                f"minSz={INSTRUMENTS[symbol]['minSz']}"
            )

        except Exception as e:
            log(
                f"[SYMBOL SKIP] {symbol}: {e}"
            )

    log(
        f"[SYMBOL] Active symbols: {ACTIVE_SYMBOLS}"
    )

    return ACTIVE_SYMBOLS


# ============================================================
# ACCOUNT CONFIG
# ============================================================

def load_account_config():

    global POS_MODE

    if not OKX_API_KEY:
        log("[ACCOUNT] API key not configured")
        return

    data = rest_request(
        "GET",
        "/api/v5/account/config",
        private=True
    )

    if not data:
        return

    rows = data.get("data", [])

    if not rows:
        return

    POS_MODE = rows[0].get(
        "posMode",
        "net_mode"
    )

    log(
        f"[ACCOUNT] Position mode: {POS_MODE}"
    )


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_body_ratio(row):

    high = float(row["high"])
    low = float(row["low"])
    open_price = float(row["open"])
    close = float(row["close"])

    rng = high - low

    if rng <= 0:
        return 0.0

    return abs(close - open_price) / rng


def bullish(row):
    return float(row["close"]) > float(row["open"])


def bearish(row):
    return float(row["close"]) < float(row["open"])


# ============================================================
# GET 5M CANDLES
# ============================================================

def get_5m_candles(symbol, limit=120):

    data = rest_request(
        "GET",
        "/api/v5/market/candles",
        params={
            "instId": symbol,
            "bar": "5m",
            "limit": str(limit)
        },
        private=False
    )

    if not data:
        return None

    rows = data.get("data", [])

    if not rows:
        return None

    parsed = []

    for r in rows:

        try:
            parsed.append({
                "ts": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "confirm": int(r[8]) if len(r) > 8 else 1
            })
        except Exception:
            continue

    if not parsed:
        return None

    df = pd.DataFrame(parsed)

    df = df.sort_values("ts").reset_index(drop=True)

    # Only completed candles
    if len(df) > 1:
        if df.iloc[-1]["confirm"] == 0:
            df = df.iloc[:-1].copy()

    return df


# ============================================================
# 5M STRUCTURE / BOS
# ============================================================

def analyze_5m(symbol):

    df = get_5m_candles(
        symbol,
        STRUCTURE_LOOKBACK + 10
    )

    if df is None or len(df) < 20:
        return None

    current = df.iloc[-1]

    previous = df.iloc[
        :-1
    ]

    lookback_df = previous.tail(
        STRUCTURE_LOOKBACK
    )

    if len(lookback_df) < 10:
        return None

    resistance = float(
        lookback_df["high"].max()
    )

    support = float(
        lookback_df["low"].min()
    )

    current_close = float(
        current["close"]
    )

    body_ratio = candle_body_ratio(
        current
    )

    volume_avg = float(
        lookback_df["volume"].tail(20).mean()
    )

    current_volume = float(
        current["volume"]
    )

    if volume_avg > 0:
        volume_ratio = current_volume / volume_avg
    else:
        volume_ratio = 1.0

    buy_break = (
        current_close
        > resistance * (1.0 + BREAK_BUFFER_PCT)
        and body_ratio >= MIN_BODY_RATIO
        and volume_ratio >= MIN_VOLUME_RATIO
    )

    sell_break = (
        current_close
        < support * (1.0 - BREAK_BUFFER_PCT)
        and body_ratio >= MIN_BODY_RATIO
        and volume_ratio >= MIN_VOLUME_RATIO
    )

    result = {
        "timestamp": int(current["ts"]),
        "close": current_close,
        "resistance": resistance,
        "support": support,
        "body_ratio": body_ratio,
        "volume_ratio": volume_ratio,
        "signal": None
    }

    if buy_break:
        result["signal"] = "BUY"

    elif sell_break:
        result["signal"] = "SELL"

    return result


# ============================================================
# 5M ANALYSIS LOOP
# ============================================================

def structure_loop():

    while True:

        try:

            for symbol in list(ACTIVE_SYMBOLS):

                result = analyze_5m(
                    symbol
                )

                if not result:
                    continue

                candle_ts = result["timestamp"]

                if LAST_5M_ANALYSIS.get(symbol) == candle_ts:
                    continue

                LAST_5M_ANALYSIS[symbol] = candle_ts

                signal = result["signal"]

                log(
                    f"[5M] {symbol} "
                    f"CLOSE={result['close']:.8f} "
                    f"RES={result['resistance']:.8f} "
                    f"SUP={result['support']:.8f} "
                    f"BODY={result['body_ratio']:.2f} "
                    f"VOL={result['volume_ratio']:.2f} "
                    f"SIGNAL={signal}"
                )

                if signal:

                    PENDING_BREAKOUT[symbol] = {
                        "side": signal,
                        "level": (
                            result["resistance"]
                            if signal == "BUY"
                            else result["support"]
                        ),
                        "created_ts": candle_ts,
                        "expires_bars": RETEST_MAX_15S,
                        "bars_seen": 0,
                        "breakout_close": result["close"],
                    }

                    log(
                        f"[BOS] {symbol} "
                        f"{signal} "
                        f"level={PENDING_BREAKOUT[symbol]['level']}"
                    )

        except Exception as e:

            set_error(
                f"Structure loop: {e}"
            )

        time.sleep(5)


# ============================================================
# 1S -> 15S AGGREGATION
# ============================================================

def add_confirmed_1s(symbol, row):

    if symbol not in ONE_SEC_DATA:
        ONE_SEC_DATA[symbol] = {}

    ts = int(row["ts"])

    ONE_SEC_DATA[symbol][ts] = row

    # Keep only recent 2 minutes
    cutoff = ts - 120000

    old_keys = [
        x for x in ONE_SEC_DATA[symbol].keys()
        if x < cutoff
    ]

    for x in old_keys:
        del ONE_SEC_DATA[symbol][x]

    bucket = (
        ts // 15000
    ) * 15000

    if LAST_15S_BUCKET.get(symbol) == bucket:
        return

    start = bucket
    end = bucket + 14000

    needed = list(
        range(
            start,
            end + 1,
            1000
        )
    )

    rows = []

    for x in needed:

        if x in ONE_SEC_DATA[symbol]:
            rows.append(
                ONE_SEC_DATA[symbol][x]
            )

    # Need complete 15 seconds
    if len(rows) != 15:
        return

    rows.sort(
        key=lambda x: int(x["ts"])
    )

    bar = {
        "ts": bucket,
        "open": float(rows[0]["open"]),
        "high": max(
            float(x["high"]) for x in rows
        ),
        "low": min(
            float(x["low"]) for x in rows
        ),
        "close": float(rows[-1]["close"]),
        "volume": sum(
            float(x["volume"]) for x in rows
        )
    }

    LAST_15S_BUCKET[symbol] = bucket

    if symbol not in BARS_15S:
        BARS_15S[symbol] = []

    BARS_15S[symbol].append(
        bar
    )

    BARS_15S[symbol] = BARS_15S[symbol][-100:]

    process_15s_bar(
        symbol,
        bar
    )


# ============================================================
# 15S RETEST + ENTRY
# ============================================================

def process_15s_bar(symbol, bar):

    # First manage existing position
    if symbol in POSITIONS:

        manage_15s_exit(
            symbol,
            bar
        )

        return

    pending = PENDING_BREAKOUT.get(
        symbol
    )

    if not pending:
        return

    pending["bars_seen"] += 1

    if pending["bars_seen"] > pending["expires_bars"]:

        log(
            f"[RETEST EXPIRED] {symbol} "
            f"{pending['side']}"
        )

        PENDING_BREAKOUT.pop(
            symbol,
            None
        )

        return

    side = pending["side"]
    level = float(
        pending["level"]
    )

    low = float(bar["low"])
    high = float(bar["high"])
    close = float(bar["close"])

    body_ratio = candle_body_ratio(
        bar
    )

    tol = RETEST_TOLERANCE_PCT

    zone_low = level * (1.0 - tol)
    zone_high = level * (1.0 + tol)

    # ========================================================
    # BUY RETEST
    # ========================================================

    if side == "BUY":

        touched = (
            low <= zone_high
            and low >= zone_low
        )

        confirmed = (
            close > level
            and bullish(bar)
            and body_ratio >= MIN_BODY_RATIO
        )

        if touched and confirmed:

            log(
                f"[RETEST CONFIRMED] {symbol} BUY "
                f"level={level:.8f} "
                f"close={close:.8f}"
            )

            execute_entry(
                symbol,
                "BUY",
                level,
                bar
            )

            PENDING_BREAKOUT.pop(
                symbol,
                None
            )

    # ========================================================
    # SELL RETEST
    # ========================================================

    elif side == "SELL":

        touched = (
            high >= zone_low
            and high <= zone_high
        )

        confirmed = (
            close < level
            and bearish(bar)
            and body_ratio >= MIN_BODY_RATIO
        )

        if touched and confirmed:

            log(
                f"[RETEST CONFIRMED] {symbol} SELL "
                f"level={level:.8f} "
                f"close={close:.8f}"
            )

            execute_entry(
                symbol,
                "SELL",
                level,
                bar
            )

            PENDING_BREAKOUT.pop(
                symbol,
                None
            )


# ============================================================
# CONTRACT SIZE
# ============================================================

def calculate_order_size(symbol, price):

    info = INSTRUMENTS.get(
        symbol
    )

    if not info:
        return 0.0

    ct_val = info["ctVal"]
    lot_sz = info["lotSz"]
    min_sz = info["minSz"]

    if price <= 0:
        return 0.0

    target_notional = (
        MARGIN_USDT
        * LEVERAGE
    )

    raw_size = (
        target_notional
        / (price * ct_val)
    )

    if lot_sz > 0:
        size = (
            int(raw_size / lot_sz)
            * lot_sz
        )
    else:
        size = raw_size

    if size < min_sz:

        if not ALLOW_MIN_SIZE_OVERSIZE:

            log(
                f"[SIZE BLOCK] {symbol}: "
                f"calculated={size}, "
                f"minimum={min_sz}, "
                f"oversize disabled"
            )

            return 0.0

        size = min_sz

    return float(size)


# ============================================================
# PLACE MARKET ORDER
# ============================================================

def place_market_order(
    symbol,
    side,
    size,
    pos_side=None,
    reduce_only=False
):

    if not OKX_API_KEY:
        log(
            "[ORDER BLOCK] OKX API credentials missing"
        )
        return None

    if not ALLOW_LIVE and not OKX_DEMO:

        log(
            "[ORDER BLOCK] Live trading disabled. "
            "Set ALLOW_LIVE=true to enable."
        )

        return None

    order = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": side.lower(),
        "ordType": "market",
        "sz": str(size),
    }

    # Net mode
    if POS_MODE == "net_mode":

        if reduce_only:
            order["reduceOnly"] = "true"

    # Hedge / long-short mode
    else:

        if pos_side:
            order["posSide"] = pos_side

    log(
        f"[ORDER SEND] {symbol} "
        f"side={side} "
        f"size={size} "
        f"reduceOnly={reduce_only}"
    )

    data = rest_request(
        "POST",
        "/api/v5/trade/order",
        body=order,
        private=True
    )

    if not data:
        return None

    rows = data.get(
        "data",
        []
    )

    if not rows:
        log(
            f"[ORDER ERROR] {data}"
        )
        return None

    item = rows[0]

    if item.get("sCode") != "0":

        log(
            f"[ORDER REJECTED] "
            f"{item.get('sCode')} "
            f"{item.get('sMsg')}"
        )

        return None

    ord_id = item.get(
        "ordId"
    )

    if not ord_id:
        return None

    log(
        f"[ORDER ACCEPTED] "
        f"{symbol} ordId={ord_id}"
    )

    return ord_id


# ============================================================
# WAIT ORDER FILL
# ============================================================

def wait_order_filled(
    symbol,
    ord_id,
    timeout=8
):

    started = time.time()

    while time.time() - started < timeout:

        data = rest_request(
            "GET",
            "/api/v5/trade/order",
            params={
                "instId": symbol,
                "ordId": ord_id
            },
            private=True
        )

        if data:

            rows = data.get(
                "data",
                []
            )

            if rows:

                order = rows[0]

                state = order.get(
                    "state",
                    ""
                )

                if state == "filled":

                    avg_px = float(
                        order.get(
                            "avgPx"
                        ) or order.get(
                            "fillPx"
                        ) or 0
                    )

                    fill_sz = float(
                        order.get(
                            "accFillSz"
                        ) or order.get(
                            "sz"
                        ) or 0
                    )

                    log(
                        f"[ORDER FILLED] {symbol} "
                        f"ordId={ord_id} "
                        f"avgPx={avg_px} "
                        f"fillSz={fill_sz}"
                    )

                    return {
                        "state": "filled",
                        "avgPx": avg_px,
                        "fillSz": fill_sz
                    }

                if state in (
                    "canceled",
                    "mmp_canceled"
                ):

                    log(
                        f"[ORDER CANCELED] {symbol} "
                        f"ordId={ord_id}"
                    )

                    return None

        time.sleep(0.5)

    log(
        f"[ORDER FILL TIMEOUT] "
        f"{symbol} ordId={ord_id}"
    )

    return None


# ============================================================
# GET ACTUAL POSITION
# ============================================================

def get_position(symbol):

    data = rest_request(
        "GET",
        "/api/v5/account/positions",
        params={
            "instId": symbol
        },
        private=True
    )

    if not data:
        return None

    rows = data.get(
        "data",
        []
    )

    for p in rows:

        try:
            pos = float(
                p.get("pos", "0")
            )
        except Exception:
            continue

        if abs(pos) <= 0:
            continue

        pos_side = p.get(
            "posSide",
            "net"
        )

        if POS_MODE == "long_short_mode":

            if pos_side == "long":
                direction = "BUY"

            elif pos_side == "short":
                direction = "SELL"

            else:
                direction = (
                    "BUY"
                    if pos > 0
                    else "SELL"
                )

        else:

            direction = (
                "BUY"
                if pos > 0
                else "SELL"
            )

        entry = float(
            p.get("avgPx")
            or p.get("openAvgPx")
            or 0
        )

        return {
            "symbol": symbol,
            "direction": direction,
            "pos": abs(pos),
            "entry": entry,
            "posSide": pos_side,
            "raw": p
        }

    return None


# ============================================================
# EXECUTE ENTRY
# ============================================================

def execute_entry(
    symbol,
    direction,
    structure_level,
    retest_bar
):

    with state_lock:

        if symbol in POSITIONS:
            log(
                f"[ENTRY BLOCK] {symbol} "
                f"already has position"
            )
            return

        now = time.time()

        last = LAST_TRADE_TIME.get(
            symbol,
            0
        )

        if now - last < COOLDOWN_SECONDS:

            log(
                f"[COOLDOWN] {symbol} "
                f"{COOLDOWN_SECONDS - (now-last):.1f}s"
            )

            return

        price = LAST_PRICE.get(
            symbol,
            0
        )

        if price <= 0:

            log(
                f"[ENTRY BLOCK] {symbol} "
                f"no live price"
            )

            return

        size = calculate_order_size(
            symbol,
            price
        )

        if size <= 0:
            return

        log(
            f"[SIGNAL] {symbol} "
            f"{direction} "
            f"entry≈{price:.8f} "
            f"size={size}"
        )

        if not AUTO_TRADE:

            log(
                f"[PAPER ONLY] {symbol} "
                f"{direction}"
            )

            return

        if direction == "BUY":

            api_side = "buy"
            pos_side = (
                "long"
                if POS_MODE == "long_short_mode"
                else None
            )

        else:

            api_side = "sell"
            pos_side = (
                "short"
                if POS_MODE == "long_short_mode"
                else None
            )

        ord_id = place_market_order(
            symbol,
            api_side,
            size,
            pos_side=pos_side,
            reduce_only=False
        )

        if not ord_id:
            return

        filled = wait_order_filled(
            symbol,
            ord_id
        )

        if not filled:

            log(
                f"[ENTRY FAILED] "
                f"{symbol} order not filled"
            )

            return

        # Confirm actual exchange position
        actual_position = get_position(
            symbol
        )

        if not actual_position:

            log(
                f"[ENTRY WARNING] {symbol} "
                f"order filled but position "
                f"not immediately visible"
            )

            time.sleep(0.5)

            actual_position = get_position(
                symbol
            )

        if not actual_position:

            log(
                f"[ENTRY ERROR] {symbol} "
                f"could not confirm position"
            )

            return

        actual_entry = float(
            actual_position["entry"]
        )

        actual_size = float(
            actual_position["pos"]
        )

        # ====================================================
        # Initial stop from retest extreme
        # ====================================================

        if direction == "BUY":

            retest_stop = (
                float(retest_bar["low"])
                * (1.0 - SL_BUFFER_PCT)
            )

            emergency_stop = (
                actual_entry
                * (1.0 - EMERGENCY_SL_PCT)
            )

            initial_stop = max(
                retest_stop,
                emergency_stop
            )

        else:

            retest_stop = (
                float(retest_bar["high"])
                * (1.0 + SL_BUFFER_PCT)
            )

            emergency_stop = (
                actual_entry
                * (1.0 + EMERGENCY_SL_PCT)
            )

            initial_stop = min(
                retest_stop,
                emergency_stop
            )

        POSITIONS[symbol] = {
            "direction": direction,
            "entry": actual_entry,
            "size": actual_size,
            "opened_at": time.time(),
            "structure_level": float(
                structure_level
            ),
            "stop_price": float(
                initial_stop
            ),
            "initial_stop": float(
                initial_stop
            ),
            "best_price": actual_entry,
            "max_favorable_pct": 0.0,
            "be_active": False,
            "trailing_active": False,
            "last_15s_ts": None,
        }

        LAST_TRADE_TIME[symbol] = time.time()

        log(
            f"[TRADE EXECUTED] {symbol} "
            f"{direction} "
            f"ENTRY={actual_entry:.8f} "
            f"SIZE={actual_size} "
            f"INITIAL_SL={initial_stop:.8f} "
            f"HOLD_MAX={MAX_HOLD_SECONDS}s"
        )


# ============================================================
# COST-AWARE MANAGEMENT
# ============================================================

def get_cost_pct(position):

    entry = float(
        position["entry"]
    )

    size = float(
        position["size"]
    )

    symbol = None

    # Find instrument through position caller
    # Actual notional estimated from entry and size
    if entry <= 0 or size <= 0:
        return 0.0

    # Conservative fallback:
    # use current target notional
    notional = max(
        MARGIN_USDT * LEVERAGE,
        1.0
    )

    return (
        ROUND_TRIP_COST_USDT
        / notional
    )


# ============================================================
# MANAGE POSITION EACH SECOND
# ============================================================

def position_manager():

    while True:

        try:

            for symbol in list(POSITIONS.keys()):

                manage_position(
                    symbol
                )

        except Exception as e:

            set_error(
                f"Position manager: {e}"
            )

        time.sleep(1)


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def manage_position(symbol):

    with state_lock:

        if symbol not in POSITIONS:
            return

        if symbol in CLOSING:
            return

        position = POSITIONS.get(
            symbol
        )

        if not position:
            return

        price = LAST_PRICE.get(
            symbol,
            0
        )

        if price <= 0:
            return

        direction = position["direction"]
        entry = float(
            position["entry"]
        )

        opened_at = float(
            position["opened_at"]
        )

        elapsed = (
            time.time()
            - opened_at
        )

        # ================================================
        # PNL MOVE
        # ================================================

        if direction == "BUY":

            move_pct = (
                price - entry
            ) / entry

            if price > position["best_price"]:
                position["best_price"] = price

            stop_hit = (
                price <= position["stop_price"]
            )

        else:

            move_pct = (
                entry - price
            ) / entry

            if price < position["best_price"]:
                position["best_price"] = price

            stop_hit = (
                price >= position["stop_price"]
            )

        position["max_favorable_pct"] = max(
            position["max_favorable_pct"],
            move_pct
        )

        # ================================================
        # COST AWARE BREAK EVEN
        # ================================================

        cost_pct = get_cost_pct(
            position
        )

        be_trigger = max(
            BE_TRIGGER_PCT,
            cost_pct + 0.0005
        )

        trail_trigger = max(
            TRAIL_TRIGGER_PCT,
            cost_pct * 1.5
        )

        # ================================================
        # BREAK EVEN
        # ================================================

        if (
            not position["be_active"]
            and move_pct >= be_trigger
        ):

            if direction == "BUY":

                new_stop = (
                    entry
                    * (1.0 + BE_LOCK_PCT)
                )

                position["stop_price"] = max(
                    position["stop_price"],
                    new_stop
                )

            else:

                new_stop = (
                    entry
                    * (1.0 - BE_LOCK_PCT)
                )

                position["stop_price"] = min(
                    position["stop_price"],
                    new_stop
                )

            position["be_active"] = True

            log(
                f"[BREAK-EVEN] {symbol} "
                f"stop={position['stop_price']:.8f}"
            )

        # ================================================
        # TRAILING STOP
        # ================================================

        if move_pct >= trail_trigger:

            if direction == "BUY":

                trail_stop = (
                    position["best_price"]
                    * (1.0 - TRAIL_DISTANCE_PCT)
                )

                if trail_stop > position["stop_price"]:

                    position["stop_price"] = (
                        trail_stop
                    )

                    if not position["trailing_active"]:

                        position["trailing_active"] = True

                        log(
                            f"[TRAIL START] {symbol} "
                            f"stop={trail_stop:.8f}"
                        )

            else:

                trail_stop = (
                    position["best_price"]
                    * (1.0 + TRAIL_DISTANCE_PCT)
                )

                if trail_stop < position["stop_price"]:

                    position["stop_price"] = (
                        trail_stop
                    )

                    if not position["trailing_active"]:

                        position["trailing_active"] = True

                        log(
                            f"[TRAIL START] {symbol} "
                            f"stop={trail_stop:.8f}"
                        )

        # ================================================
        # STOP EXIT
        # ================================================

        if stop_hit:

            log(
                f"[STOP EXIT] {symbol} "
                f"price={price:.8f} "
                f"stop={position['stop_price']:.8f}"
            )

            close_position(
                symbol,
                "TRAIL/SL"
            )

            return

        # ================================================
        # MAX HOLD
        # ================================================

        if elapsed >= MAX_HOLD_SECONDS:

            log(
                f"[MAX HOLD EXIT] {symbol} "
                f"held={elapsed:.1f}s "
                f"move={move_pct*100:.3f}%"
            )

            close_position(
                symbol,
                "MAX_HOLD"
            )

            return


# ============================================================
# 15S EXIT LOGIC
# ============================================================

def manage_15s_exit(symbol, bar):

    position = POSITIONS.get(
        symbol
    )

    if not position:
        return

    direction = position["direction"]
    entry = float(
        position["entry"]
    )

    structure_level = float(
        position["structure_level"]
    )

    close = float(
        bar["close"]
    )

    body_ratio = candle_body_ratio(
        bar
    )

    # Prevent duplicate processing
    bar_ts = int(
        bar["ts"]
    )

    if position.get("last_15s_ts") == bar_ts:
        return

    position["last_15s_ts"] = bar_ts

    # ================================================
    # BUY
    # ================================================

    if direction == "BUY":

        # Strong bearish failure
        structure_failure = (
            close
            < structure_level
            * (1.0 - RETEST_TOLERANCE_PCT)
        )

        opposite_momentum = (
            OPPOSITE_EXIT_ENABLED
            and bearish(bar)
            and body_ratio >= OPPOSITE_BODY_RATIO
            and close < entry
        )

        if structure_failure:

            log(
                f"[STRUCTURE FAIL EXIT] {symbol} BUY"
            )

            close_position(
                symbol,
                "STRUCTURE_FAIL"
            )

            return

        if opposite_momentum:

            log(
                f"[OPPOSITE 15S EXIT] {symbol} BUY"
            )

            close_position(
                symbol,
                "OPPOSITE_15S"
            )

            return

    # ================================================
    # SELL
    # ================================================

    else:

        structure_failure = (
            close
            > structure_level
            * (1.0 + RETEST_TOLERANCE_PCT)
        )

        opposite_momentum = (
            OPPOSITE_EXIT_ENABLED
            and bullish(bar)
            and body_ratio >= OPPOSITE_BODY_RATIO
            and close > entry
        )

        if structure_failure:

            log(
                f"[STRUCTURE FAIL EXIT] {symbol} SELL"
            )

            close_position(
                symbol,
                "STRUCTURE_FAIL"
            )

            return

        if opposite_momentum:

            log(
                f"[OPPOSITE 15S EXIT] {symbol} SELL"
            )

            close_position(
                symbol,
                "OPPOSITE_15S"
            )

            return


# ============================================================
# CLOSE POSITION
# ============================================================

def close_position(symbol, reason):

    with state_lock:

        if symbol in CLOSING:
            return

        position = POSITIONS.get(
            symbol
        )

        if not position:
            return

        CLOSING.add(
            symbol
        )

    try:

        # ALWAYS get actual exchange position
        actual = get_position(
            symbol
        )

        if not actual:

            log(
                f"[CLOSE] {symbol}: "
                f"position already closed"
            )

            POSITIONS.pop(
                symbol,
                None
            )

            return

        actual_size = float(
            actual["pos"]
        )

        actual_direction = actual["direction"]

        if actual_direction == "BUY":

            api_side = "sell"

            pos_side = (
                "long"
                if POS_MODE == "long_short_mode"
                else None
            )

        else:

            api_side = "buy"

            pos_side = (
                "short"
                if POS_MODE == "long_short_mode"
                else None
            )

        log(
            f"[CLOSE SEND] {symbol} "
            f"reason={reason} "
            f"size={actual_size}"
        )

        ord_id = place_market_order(
            symbol,
            api_side,
            actual_size,
            pos_side=pos_side,
            reduce_only=True
        )

        if not ord_id:

            log(
                f"[CLOSE FAILED] {symbol}"
            )

            return

        filled = wait_order_filled(
            symbol,
            ord_id
        )

        if filled:

            time.sleep(0.3)

            remaining = get_position(
                symbol
            )

            if remaining:

                log(
                    f"[CLOSE WARNING] {symbol} "
                    f"remaining position="
                    f"{remaining['pos']}"
                )

            else:

                log(
                    f"[POSITION CLOSED] {symbol} "
                    f"reason={reason}"
                )

                POSITIONS.pop(
                    symbol,
                    None
                )

                LAST_TRADE_TIME[symbol] = (
                    time.time()
                )

        else:

            log(
                f"[CLOSE FILL UNKNOWN] "
                f"{symbol}"
            )

    except Exception as e:

        set_error(
            f"Close {symbol}: {e}"
        )

    finally:

        CLOSING.discard(
            symbol
        )


# ============================================================
# WEBSOCKET MESSAGE
# ============================================================

def handle_ws_message(message):

    try:

        data = json.loads(
            message
        )

    except Exception:
        return

    event = data.get(
        "event"
    )

    if event == "subscribe":

        arg = data.get(
            "arg",
            {}
        )

        log(
            f"[WS SUBSCRIBED] "
            f"{arg}"
        )

        return

    if event == "error":

        set_error(
            f"WS ERROR: {data}"
        )

        return

    if data.get("code") == "64008":

        log(
            "[WS NOTICE] OKX WebSocket "
            "service upgrade notice"
        )

        return

    rows = data.get(
        "data"
    )

    if not rows:
        return

    arg = data.get(
        "arg",
        {}
    )

    channel = arg.get(
        "channel"
    )

    symbol = arg.get(
        "instId"
    )

    if channel != "candle1s":
        return

    if symbol not in ACTIVE_SYMBOLS:
        return

    for r in rows:

        try:

            ts = int(r[0])

            open_price = float(r[1])
            high = float(r[2])
            low = float(r[3])
            close = float(r[4])
            volume = float(r[5])

            confirm = (
                int(r[8])
                if len(r) > 8
                else 0
            )

        except Exception:
            continue

        # Current price updates even before candle closes
        LAST_PRICE[symbol] = close
        LAST_PRICE_TIME[symbol] = time.time()

        # Only completed 1-second candles are used
        # to build the 15-second execution candles.
        if confirm != 1:
            continue

        row = {
            "ts": ts,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume
        }

        add_confirmed_1s(
            symbol,
            row
        )


# ============================================================
# WEBSOCKET LOOP
# ============================================================

def websocket_loop():

    global WS_STATUS

    # US OKX demo
    if OKX_DEMO:

        ws_url = (
            "wss://wsuspap.okx.com:8443/"
            "ws/v5/business"
        )

    else:

        ws_url = (
            "wss://wsus.okx.com:8443/"
            "ws/v5/business"
        )

    while True:

        try:

            if not ACTIVE_SYMBOLS:

                log(
                    "[WS] No active symbols"
                )

                time.sleep(10)

                validate_symbols()

                continue
