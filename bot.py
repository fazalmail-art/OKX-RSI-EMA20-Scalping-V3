import os
import json
import time
import hmac
import base64
import hashlib
import threading
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
import websocket
from flask import Flask, jsonify


# ============================================================
# CONFIG
# ============================================================

OKX_BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://us.okx.com"
)

OKX_WS_BUSINESS = os.getenv(
    "OKX_WS_BUSINESS",
    "wss://wsuspap.okx.com:8443/ws/v5/business"
)

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

OKX_DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"

AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

# HYPE deliberately included
REQUESTED_SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "HYPE-USDT-SWAP",
]

MARGIN_USDT = float(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
TD_MODE = os.getenv("TD_MODE", "isolated")

STRUCTURE_LOOKBACK = int(os.getenv("STRUCTURE_LOOKBACK", "80"))
PIVOT_LEFT = int(os.getenv("PIVOT_LEFT", "2"))
PIVOT_RIGHT = int(os.getenv("PIVOT_RIGHT", "2"))

RSI_LENGTH = 14
RSI_MA_LENGTH = 7

BREAK_BUFFER_PCT = 0.015
MIN_BODY_RATIO = 0.45
MIN_VOLUME_RATIO = 1.05
MAX_EXTENSION_ATR = 1.20

MIN_HOLD_SECONDS = 3
MAX_HOLD_SECONDS = 30

TRAIL_START_SECONDS = 12
TRAIL_ATR_MULT = 0.75
EMERGENCY_SL_ATR = 1.60

COOLDOWN_SECONDS = 45
MAX_DAILY_LOSS_USDT = 30
MAX_CONSECUTIVE_LOSSES = 4

PORT = int(os.getenv("PORT", "8080"))

# WhatsApp
WHATSAPP_ACCESS_TOKEN = os.getenv(
    "WHATSAPP_ACCESS_TOKEN", ""
)

WHATSAPP_PHONE_NUMBER_ID = os.getenv(
    "WHATSAPP_PHONE_NUMBER_ID", ""
)

WHATSAPP_TO_NUMBER = os.getenv(
    "WHATSAPP_TO_NUMBER", ""
)

WHATSAPP_API_VERSION = os.getenv(
    "WHATSAPP_API_VERSION", ""
)


# ============================================================
# APP / STATE
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
    "consecutive_losses": 0,
}

valid_symbols = []
positions = {}
last_trade_time = {}

one_second_data = {}
candles_15s = {}

data_lock = threading.Lock()


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True
    )


# ============================================================
# OKX AUTH
# ============================================================

def timestamp():
    return datetime.now(
        timezone.utc
    ).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def sign(ts, method, path, body=""):

    message = (
        ts +
        method.upper() +
        path +
        body
    )

    digest = hmac.new(
        OKX_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(
        digest
    ).decode()


def headers(method, path, body=""):

    ts = timestamp()

    h = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign(
            ts,
            method,
            path,
            body
        ),
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

        r = requests.get(
            OKX_BASE_URL + path,
            params=params,
            timeout=10
        )

        return r.json()

    except Exception as e:

        state["last_error"] = str(e)
        log(f"REST ERROR: {e}")
        return {}


def private_post(path, payload):

    body = json.dumps(
        payload,
        separators=(",", ":")
    )

    try:

        r = requests.post(
            OKX_BASE_URL + path,
            headers=headers(
                "POST",
                path,
                body
            ),
            data=body,
            timeout=10
        )

        result = r.json()

        state["last_order"] = result

        return result

    except Exception as e:

        state["last_error"] = str(e)
        log(f"PRIVATE REST ERROR: {e}")
        return {}


# ============================================================
# CHECK API
# ============================================================

def api_ready():

    if not OKX_API_KEY:
        log("ERROR: OKX_API_KEY missing")
        return False

    if not OKX_SECRET_KEY:
        log("ERROR: OKX_SECRET_KEY missing")
        return False

    if not OKX_PASSPHRASE:
        log("ERROR: OKX_PASSPHRASE missing")
        return False

    if not OKX_DEMO and not ALLOW_LIVE:
        log("LIVE trading blocked")
        return False

    return True


# ============================================================
# VALIDATE SYMBOLS
# ============================================================

def validate_symbols():

    global valid_symbols

    log("Checking OKX SWAP instruments...")

    valid_symbols = []

    for symbol in REQUESTED_SYMBOLS:

        result = public_get(
            "/api/v5/public/instruments",
            {
                "instType": "SWAP",
                "instId": symbol
            }
        )

        data = result.get("data", [])

        if data:

            valid_symbols.append(symbol)

            log(
                f"[SYMBOL OK] {symbol}"
            )

        else:

            log(
                f"[SYMBOL SKIP] {symbol} "
                f"does not exist on this OKX endpoint"
            )

    for symbol in valid_symbols:

        one_second_data.setdefault(
            symbol,
            []
        )

        candles_15s.setdefault(
            symbol,
            []
        )

        last_trade_time.setdefault(
            symbol,
            0
        )

    log(
        "ACTIVE SYMBOLS: "
        + ", ".join(valid_symbols)
    )

    return valid_symbols


# ============================================================
# INSTRUMENT
# ============================================================

instrument_cache = {}


def get_instrument(symbol):

    if symbol in instrument_cache:
        return instrument_cache[symbol]

    result = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": symbol
        }
    )

    data = result.get("data", [])

    if not data:
        return None

    instrument_cache[symbol] = data[0]

    return data[0]


def round_size(symbol, size):

    inst = get_instrument(symbol)

    if not inst:
        return 0

    lot = float(
        inst.get("lotSz", "1")
    )

    minimum = float(
        inst.get("minSz", lot)
    )

    if lot <= 0:
        return 0

    result = (
        np.floor(size / lot)
        * lot
    )

    if result < minimum:
        return 0

    return result


# ============================================================
# 5M CANDLES
# ============================================================

def get_5m_candles(symbol):

    result = public_get(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": "5m",
            "limit": str(
                STRUCTURE_LOOKBACK + 20
            )
        }
    )

    rows = result.get(
        "data",
        []
    )

    if not rows:
        return pd.DataFrame()

    rows = list(
        reversed(rows)
    )

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

    df = pd.DataFrame(records)

    return add_indicators(df)


# ============================================================
# INDICATORS
# ============================================================

def EMA(series, length):

    return series.ewm(
        span=length,
        adjust=False
    ).mean()


def RSI(series, length=14):

    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    rs = (
        avg_gain /
        avg_loss.replace(
            0,
            np.nan
        )
    )

    value = 100 - (
        100 / (1 + rs)
    )

    return value.fillna(50)


def ATR(df, length=14):

    hl = (
        df["high"] -
        df["low"]
    )

    hc = (
        df["high"] -
        df["close"].shift()
    ).abs()

    lc = (
        df["low"] -
        df["close"].shift()
    ).abs()

    tr = pd.concat(
        [hl, hc, lc],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()


def add_indicators(df):

    if df.empty:
        return df

    df["ema20"] = EMA(
        df["close"],
        20
    )

    df["ema50"] = EMA(
        df["close"],
        50
    )

    df["rsi"] = RSI(
        df["close"],
        RSI_LENGTH
    )

    df["rsi_ma"] = (
        df["rsi"]
        .rolling(RSI_MA_LENGTH)
        .mean()
    )

    df["atr"] = ATR(
        df,
        14
    )

    df["volume_ma"] = (
        df["volume"]
        .rolling(20)
        .mean()
    )

    df["volume_ratio"] = (
        df["volume"] /
        df["volume_ma"].replace(
            0,
            np.nan
        )
    )

    return df


# ============================================================
# PIVOTS / STRUCTURE
# ============================================================

def pivots(df):

    highs = []
    lows = []

    if len(df) < 15:
        return highs, lows

    for i in range(
        PIVOT_LEFT,
        len(df) - PIVOT_RIGHT
    ):

        h = df["high"].iloc[i]
        l = df["low"].iloc[i]

        left_h = df[
            "high"
        ].iloc[
            i-PIVOT_LEFT:i
        ]

        right_h = df[
            "high"
        ].iloc[
            i+1:i+1+PIVOT_RIGHT
        ]

        left_l = df[
            "low"
        ].iloc[
            i-PIVOT_LEFT:i
        ]

        right_l = df[
            "low"
        ].iloc[
            i+1:i+1+PIVOT_RIGHT
        ]

        if (
            h > left_h.max()
            and
            h > right_h.max()
        ):
            highs.append(
                (i, h)
            )

        if (
            l < left_l.min()
            and
            l < right_l.min()
        ):
            lows.append(
                (i, l)
            )

    return highs, lows


def analyze_structure(df):

    if len(df) < 25:
        return None

    highs, lows = pivots(df)

    resistance = (
        highs[-1][1]
        if highs
        else None
    )

    support = (
        lows[-1][1]
        if lows
        else None
    )

    close = float(
        df["close"].iloc[-1]
    )

    direction = "NONE"

    if resistance:

        if close > resistance * (
            1 + BREAK_BUFFER_PCT / 100
        ):
            direction = "BUY"

    if support:

        if close < support * (
            1 - BREAK_BUFFER_PCT / 100
        ):
            direction = "SELL"

    return {
        "direction": direction,
        "resistance": resistance,
        "support": support,
        "close": close,
        "atr": float(
            df["atr"].iloc[-1]
        )
    }


# ============================================================
# 1 SECOND DATA -> 15 SECOND CANDLES
# ============================================================

def add_1s(symbol, candle):

    with data_lock:

        one_second_data[
            symbol
        ].append(candle)

        one_second_data[
            symbol
        ] = one_second_data[
            symbol
        ][-300:]


def build_15s(symbol):

    with data_lock:

        rows = list(
            one_second_data[
                symbol
            ]
        )

    if not rows:
        return False

    buckets = {}

    for r in rows:

        bucket = (
            r["ts"] // 15000
        ) * 15000

        buckets.setdefault(
            bucket,
            []
        ).append(r)

    completed = []

    current_bucket = (
        int(time.time() * 1000)
        // 15000
    ) * 15000

    for bucket, items in buckets.items():

        # Do not use current unfinished bucket
        if bucket >= current_bucket:
            continue

        # Require most seconds to be present
        if len(items) < 12:
            continue

        items.sort(
            key=lambda x: x["ts"]
        )

        completed.append({
            "ts": bucket,
            "open": items[0]["open"],
            "high": max(
                x["high"]
                for x in items
            ),
            "low": min(
                x["low"]
                for x in items
            ),
            "close": items[-1]["close"],
            "volume": sum(
                x["volume"]
                for x in items
            )
        })

    if not completed:
        return False

    df = pd.DataFrame(
        completed
    )

    df = df.drop_duplicates(
        "ts"
    ).sort_values("ts")

    df = add_indicators(df)

    with data_lock:

        candles_15s[
            symbol
        ] = df.tail(
            100
        ).to_dict(
            "records"
        )

    return True


# ============================================================
# ENTRY
# ============================================================

def find_entry(
    symbol,
    structure
):

    with data_lock:

        rows = list(
            candles_15s[
                symbol
            ]
        )

    if len(rows) < 20:
        return None

    df = pd.DataFrame(rows)

    df = add_indicators(df)

    current = df.iloc[-1]
    previous = df.iloc[-2]

    direction = structure[
        "direction"
    ]

    if direction not in (
        "BUY",
        "SELL"
    ):
        return None

    candle_range = (
        current["high"] -
        current["low"]
    )

    if candle_range <= 0:
        return None

    body = abs(
        current["close"] -
        current["open"]
    )

    body_ratio = (
        body /
        candle_range
    )

    if body_ratio < MIN_BODY_RATIO:
        return None

    if (
        pd.notna(
            current["volume_ratio"]
        )
        and
        current["volume_ratio"]
        < MIN_VOLUME_RATIO
    ):
        return None

    atr = current["atr"]

    if pd.isna(atr) or atr <= 0:
        return None

    # ---------------- BUY ----------------

    if direction == "BUY":

        bullish = (
            current["close"] >
            current["open"]
        )

        momentum = (
            current["close"] >
            previous["close"]
        )

        rsi_ok = (
            current["rsi"] > 50
            and
            current["rsi"] >
            current["rsi_ma"]
        )

        ema_ok = (
            current["close"] >
            current["ema20"]
        )

        if (
            bullish
            and momentum
            and rsi_ok
            and ema_ok
        ):

            return {
                "side": "buy",
                "price": float(
                    current["close"]
                ),
                "atr": float(atr),
                "reason":
                    "5M BOS + 15S confirmation"
            }

    # ---------------- SELL ----------------

    if direction == "SELL":

        bearish = (
            current["close"] <
            current["open"]
        )

        momentum = (
            current["close"] <
            previous["close"]
        )

        rsi_ok = (
            current["rsi"] < 50
            and
            current["rsi"] <
            current["rsi_ma"]
        )

        ema_ok = (
            current["close"] <
            current["ema20"]
        )

        if (
            bearish
            and momentum
            and rsi_ok
            and ema_ok
        ):

            return {
                "side": "sell",
                "price": float(
                    current["close"]
                ),
                "atr": float(atr),
                "reason":
                    "5M BOS + 15S confirmation"
            }

    return None


# ============================================================
# SIZE
# ============================================================

def calculate_size(
    symbol,
    price
):

    inst = get_instrument(symbol)

    if not inst:
        return 0

    ct_val = float(
        inst.get("ctVal", "1")
    )

    if ct_val <= 0:
        return 0

    notional = (
        MARGIN_USDT *
        LEVERAGE
    )

    raw_size = (
        notional /
        (
            price *
            ct_val
        )
    )

    return round_size(
        symbol,
        raw_size
    )


# ============================================================
# ORDER
# ============================================================

def market_order(
    symbol,
    side,
    size
):

    if not api_ready():
        return None

    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": side,
        "ordType": "market",
        "sz": str(size)
    }

    log(
        f"[ORDER REQUEST] "
        f"{symbol} {side} size={size}"
    )

    result = private_post(
        "/api/v5/trade/order",
        payload
    )

    log(
        f"[ORDER RESPONSE] {result}"
    )

    data = result.get(
        "data",
        []
    )

    if not data:
        log(
            "[ORDER FAILED] No data returned"
        )
        return None

    if data[0].get(
        "sCode"
    ) != "0":

        log(
            "[ORDER FAILED] "
            + str(data[0])
        )

        return None

    log(
        f"[ORDER ACCEPTED] "
        f"ordId={data[0].get('ordId')}"
    )

    return data[0]


# ============================================================
# OPEN
# ============================================================

def open_position(
    symbol,
    signal
):

    if symbol in positions:
        log(
            f"[WAIT] {symbol} already has position"
        )
        return

    now = time.time()

    if (
        now -
        last_trade_time.get(
            symbol,
            0
        )
        <
        COOLDOWN_SECONDS
    ):
        log(
            f"[WAIT] {symbol} cooldown"
        )
        return

    if (
        state["daily_pnl"]
        <=
        -MAX_DAILY_LOSS_USDT
    ):
        log(
            "[RISK STOP] Daily loss limit"
        )
        return

    if (
        state["consecutive_losses"]
        >=
        MAX_CONSECUTIVE_LOSSES
    ):
        log(
            "[RISK STOP] Consecutive losses"
        )
        return

    price = signal["price"]

    size = calculate_size(
        symbol,
        price
    )

    if size <= 0:

        log(
            f"[SIZE ERROR] {symbol}"
        )

        return

    # Signal-only mode
    if not AUTO_TRADE:

        log(
            f"🟡 SIGNAL ONLY | "
            f"{symbol} | "
            f"{signal['side'].upper()} | "
            f"price={price}"
        )

        send_whatsapp(
            "🟡 OKX SIGNAL\n"
            f"{symbol}\n"
            f"Side: {signal['side'].upper()}\n"
            f"Price: {price}\n"
            f"Size: {size}\n"
            f"{signal['reason']}"
        )

        return

    order = market_order(
        symbol,
        signal["side"],
        size
    )

    if not order:
        return

    positions[symbol] = {
        "side": signal["side"],
        "entry": price,
        "size": size,
        "atr": signal["atr"],
        "time": time.time(),
        "best_price": price,
        "ord_id": order.get(
            "ordId"
        )
    }

    last_trade_time[symbol] = (
        time.time()
    )

    state["trades"] += 1

    log(
        f"🟢 POSITION OPENED | "
        f"{symbol} | "
        f"{signal['side'].upper()} | "
        f"entry={price} | "
        f"size={size}"
    )

    send_whatsapp(
        "🟢 OKX DEMO TRADE OPENED\n"
        f"{symbol}\n"
        f"Side: {signal['side'].upper()}\n"
        f"Entry: {price}\n"
        f"Size: {size}"
    )


# ============================================================
# CLOSE
# ============================================================

def close_position(
    symbol,
    price,
    reason
):

    position = positions.get(
        symbol
    )

    if not position:
        return

    side = position["side"]

    close_side = (
        "sell"
        if side == "buy"
        else "buy"
    )

    if AUTO_TRADE:

        order = market_order(
            symbol,
            close_side,
            position["size"]
        )

        if not order:
            log(
                f"[CLOSE FAILED] {symbol}"
            )
            return

    entry = position["entry"]

    # Approximate PnL
    if side == "buy":

        pnl = (
            price - entry
        ) * position["size"]

    else:

        pnl = (
            entry - price
        ) * position["size"]

    state["daily_pnl"] += pnl

    if pnl >= 0:

        state["wins"] += 1
        state["consecutive_losses"] = 0

    else:

        state["losses"] += 1
        state["consecutive_losses"] += 1

    log(
        f"🔴 POSITION CLOSED | "
        f"{symbol} | "
        f"entry={entry} | "
        f"exit={price} | "
        f"PnL≈{pnl:.4f} | "
        f"reason={reason}"
    )

    send_whatsapp(
        "🔴 OKX DEMO TRADE CLOSED\n"
        f"{symbol}\n"
        f"Side: {side.upper()}\n"
        f"Entry: {entry}\n"
        f"Exit: {price}\n"
        f"Estimated PnL: {pnl:.4f} USDT\n"
        f"Reason: {reason}"
    )

    del positions[symbol]


# ============================================================
# POSITION MANAGER
# ============================================================

def manage_position(symbol):

    position = positions.get(
        symbol
    )

    if not position:
        return

    with data_lock:

        rows = list(
            candles_15s[
                symbol
            ]
        )

    if not rows:
        return

    df = pd.DataFrame(rows)

    df = add_indicators(df)

    current = df.iloc[-1]

    price = float(
        current["close"]
    )

    atr = float(
        current["atr"]
    )

    age = (
        time.time() -
        position["time"]
    )

    side = position["side"]
    entry = position["entry"]

    # ========================================================
    # BUY
    # ========================================================

    if side == "buy":

        if price > position["best_price"]:
            position["best_price"] = price

        emergency_sl = (
            entry -
            atr * EMERGENCY_SL_ATR
        )

        trailing_sl = (
            position["best_price"] -
            atr * TRAIL_ATR_MULT
        )

        if price <= emergency_sl:

            close_position(
                symbol,
                price,
                "Emergency ATR SL"
            )

            return

        if (
            age >= TRAIL_START_SECONDS
            and
            price <= trailing_sl
        ):

            close_position(
                symbol,
                price,
                "Trailing exit"
            )

            return

        if (
            age >= MIN_HOLD_SECONDS
            and
            price < current["ema20"]
        ):

            close_position(
                symbol,
                price,
                "15S momentum failure"
            )

            return

    # ========================================================
    # SELL
    # ========================================================

    else:

        if price < position["best_price"]:
            position["best_price"] = price

        emergency_sl = (
            entry +
            atr * EMERGENCY_SL_ATR
        )

        trailing_sl = (
            position["best_price"] +
            atr * TRAIL_ATR_MULT
        )

        if price >= emergency_sl:

            close_position(
                symbol,
                price,
                "Emergency ATR SL"
            )

            return

        if (
            age >= TRAIL_START_SECONDS
            and
            price >= trailing_sl
        ):

            close_position(
                symbol,
                price,
                "Trailing exit"
            )

            return

        if (
            age >= MIN_HOLD_SECONDS
            and
            price > current["ema20"]
        ):

            close_position(
                symbol,
                price,
                "15S momentum failure"
            )

            return

    # Maximum holding time
    if age >= MAX_HOLD_SECONDS:

        close_position(
            symbol,
            price,
            "Maximum 30 second hold"
        )


# ============================================================
# WHATSAPP
# ============================================================

def send_whatsapp(message):

    if not all([
        WHATSAPP_ACCESS_TOKEN,
        WHATSAPP_PHONE_NUMBER_ID,
        WHATSAPP_TO_NUMBER,
        WHATSAPP_API_VERSION
    ]):

        log(
            "[WHATSAPP] Not configured"
        )

        return False

    url = (
        "https://graph.facebook.com/"
        f"{WHATSAPP_API_VERSION}/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )

    payload = {
        "messaging_product": "whatsapp",
        "to": WHATSAPP_TO_NUMBER,
        "type": "text",
        "text": {
            "body": message
        }
    }

    try:

        r = requests.post(
            url,
            headers={
                "Authorization":
                    "Bearer "
                    + WHATSAPP_ACCESS_TOKEN,
                "Content-Type":
                    "application/json"
            },
            json=payload,
            timeout=10
        )

        log(
            f"[WHATSAPP] "
            f"{r.status_code} "
            f"{r.text}"
        )

        return r.ok

    except Exception as e:

        log(
            f"[WHATSAPP ERROR] {e}"
        )

        return False


# ============================================================
# WEBSOCKET MESSAGE
# ============================================================

def ws_message(ws, message):

    try:

        obj = json.loads(
            message
        )

        if obj.get("event") == "error":

            log(
                f"❌ WS ERROR: {obj}"
            )

            state["last_error"] = str(
                obj
            )

            return

        if obj.get("event") == "subscribe":

            log(
                f"✅ WS SUBSCRIBED: "
                f"{obj.get('arg')}"
            )

            return

        arg = obj.get(
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

        if symbol not in valid_symbols:
            return

        data = obj.get(
            "data",
            []
        )

        if not data:
            return

        r = data[0]

        candle = {
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5])
        }

        add_1s(
            symbol,
            candle
        )

        state["last_data"] = {
            "symbol": symbol,
            "price": candle["close"],
            "time": candle["ts"]
        }

        # Log every 1s data only periodically
        if candle["ts"] // 5000 != (
            candle["ts"] - 1000
        ) // 5000:

            log(
                f"[DATA] "
                f"{symbol} "
                f"price={candle['close']}"
            )

        built = build_15s(
            symbol
        )

        if built:

            log(
                f"[15S] "
                f"{symbol} candle formed"
            )

        manage_position(
            symbol
        )

    except Exception as e:

        state["last_error"] = str(e)

        log(
            f"❌ WS MESSAGE ERROR: {e}"
        )


# ============================================================
# WEBSOCKET OPEN
# ============================================================

def ws_open(ws):

    state["ws_connected"] = True
    state["status"] = "RUNNING"

    log(
        "=========================================="
    )

    log(
        "✅ OKX WEBSOCKET CONNECTED"
    )

    log(
        f"Demo={OKX_DEMO}"
    )

    log(
        f"Symbols={valid_symbols}"
    )

    log(
        "=========================================="
    )

    args = []

    for symbol in valid_symbols:

        args.append({
            "channel": "candle1s",
            "instId": symbol
        })

    if not args:

        log(
            "❌ No valid symbols to subscribe"
        )

        return

    request = {
        "op": "subscribe",
        "args": args
    }

    ws.send(
        json.dumps(request)
    )

    log(
        "📡 Subscription request sent"
    )


# ============================================================
# WEBSOCKET ERROR / CLOSE
# ============================================================

def ws_error(ws, error):

    state["ws_connected"] = False
    state["last_error"] = str(error)

    log(
        f"❌ WS ERROR: {error}"
    )


def ws_close(ws, code, message):

    state["ws_connected"] = False

    log(
        f"⚠️ WS CLOSED: "
        f"{code} {message}"
    )

    log(
        "Reconnecting automatically..."
    )


# ============================================================
# WEBSOCKET LOOP
# ============================================================

def websocket_loop():

    while True:

        try:

            if not valid_symbols:

                validate_symbols()

            log(
                "Connecting to OKX WebSocket..."
            )

            ws = websocket.WebSocketApp(
                OKX_WS_BUSINESS,
                on_open=ws_open,
                on_message=ws_message,
                on_error=ws_error,
                on_close=ws_close
            )

            ws.run_forever(
                ping_interval=15,
                ping_timeout=10
            )

        except Exception as e:

            state["last_error"] = str(e)

            log(
                f"❌ WS LOOP ERROR: {e}"
            )

        state["ws_connected"] = False

        log(
            "⏳ WebSocket reconnect in 5 seconds..."
        )

        time.sleep(5)


# ============================================================
# STRUCTURE LOOP
# ============================================================

def structure_loop():

    last_status = {}

    while True:

        try:

            for symbol in valid_symbols:

                df = get_5m_candles(
                    symbol
                )

                if df.empty:

                    log(
                        f"[5M] {symbol}: "
                        f"no candle data"
                    )

                    continue

                # Remove unfinished 5m candle
                if len(df) > 1:
                    df = df.iloc[:-1].copy()

                structure = analyze_structure(
                    df
                )

                if not structure:
                    continue

                direction = structure[
                    "direction"
                ]

                price = structure[
                    "close"
                ]

                # Don't spam identical messages
                if last_status.get(
                    symbol
                ) != direction:

                    log(
                        f"[5M STRUCTURE] "
                        f"{symbol} | "
                        f"{direction} | "
                        f"price={price}"
                    )

                    last_status[
                        symbol
                    ] = direction

                signal = find_entry(
                    symbol,
                    structure
                )

                if signal:

                    log(
                        f"🚨 SIGNAL FOUND | "
                        f"{symbol} | "
                        f"{signal['side'].upper()} | "
                        f"price={signal['price']} | "
                        f"{signal['reason']}"
                    )

                    state[
                        "last_signal"
                    ] = {
                        "symbol": symbol,
                        **signal
                    }

                    open_position(
                        symbol,
                        signal
                    )

                else:

                    log(
                        f"[WAIT] {symbol} | "
                        f"5M={direction} | "
                        f"No valid 15S entry"
                    )

        except Exception as e:

            state["last_error"] = str(e)

            log(
                f"❌ STRUCTURE ERROR: {e}"
            )

        time.sleep(5)


# ============================================================
# FLASK DASHBOARD
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "bot":
            "ICT SwiftEdge OKX Scalper",
        "status":
            state["status"],
        "demo":
            OKX_DEMO,
        "auto_trade":
            AUTO_TRADE,
        "websocket":
            state["ws_connected"],
        "symbols":
            valid_symbols,
        "positions":
            positions,
        "daily_pnl":
            state["daily_pnl"],
        "trades":
            state["trades"],
        "wins":
            state["wins"],
        "losses":
            state["losses"],
        "last_data":
            state["last_data"],
        "last_signal":
            state["last_signal"],
        "last_error":
            state["last_error"]
    })


@app.route("/health")
def health():

    return "OK", 200


@app.route("/api/status")
def status():

    return jsonify({
        **state,
        "symbols": valid_symbols,
        "positions": positions
    })


# ============================================================
# START
# ============================================================

def start():

    log("")
    log("==========================================")
    log("🚀 ICT SWIFTEDGE OKX SCALPER")
    log("==========================================")

    log(
        f"OKX DEMO: {OKX_DEMO}"
    )

    log(
        f"AUTO TRADE: {AUTO_TRADE}"
    )

    log(
        f"WS URL: {OKX_WS_BUSINESS}"
    )

    log(
        "Requested: "
        + ", ".join(REQUESTED_SYMBOLS)
    )

    if not api_ready():

        log(
            "⚠️ API credentials not complete"
        )

    validate_symbols()

    threading.Thread(
        target=websocket_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=structure_loop,
        daemon=True
    ).start()

    log(
        "✅ Bot threads started"
    )


if __name__ == "__main__":

    start()

    app.run(
        host="0.0.0.0",
        port=PORT
    )
