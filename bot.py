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

OKX_WS_PUBLIC = os.getenv(
    "OKX_WS_PUBLIC",
    "wss://wsuspap.okx.com:8443/ws/v5/public"
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

SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP"
    ).split(",")
    if x.strip()
]

MARGIN_USDT = float(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = int(os.getenv("LEVERAGE", "3"))

TD_MODE = os.getenv("TD_MODE", "isolated")

STRUCTURE_LOOKBACK = int(
    os.getenv("STRUCTURE_LOOKBACK", "80")
)

PIVOT_LEFT = int(
    os.getenv("PIVOT_LEFT", "2")
)

PIVOT_RIGHT = int(
    os.getenv("PIVOT_RIGHT", "2")
)

RSI_LENGTH = int(
    os.getenv("RSI_LENGTH", "14")
)

RSI_MA_LENGTH = int(
    os.getenv("RSI_MA_LENGTH", "7")
)

BREAK_BUFFER_PCT = float(
    os.getenv("BREAK_BUFFER_PCT", "0.015")
)

RETEST_TOLERANCE_PCT = float(
    os.getenv("RETEST_TOLERANCE_PCT", "0.10")
)

MIN_BODY_RATIO = float(
    os.getenv("MIN_BODY_RATIO", "0.45")
)

MIN_VOLUME_RATIO = float(
    os.getenv("MIN_VOLUME_RATIO", "1.05")
)

MAX_EXTENSION_ATR = float(
    os.getenv("MAX_EXTENSION_ATR", "1.20")
)

MIN_HOLD_SECONDS = int(
    os.getenv("MIN_HOLD_SECONDS", "3")
)

MAX_HOLD_SECONDS = int(
    os.getenv("MAX_HOLD_SECONDS", "30")
)

TRAIL_START_SECONDS = int(
    os.getenv("TRAIL_START_SECONDS", "12")
)

TRAIL_ATR_MULT = float(
    os.getenv("TRAIL_ATR_MULT", "0.75")
)

EMERGENCY_SL_ATR = float(
    os.getenv("EMERGENCY_SL_ATR", "1.60")
)

COOLDOWN_SECONDS = int(
    os.getenv("COOLDOWN_SECONDS", "45")
)

MAX_DAILY_LOSS_USDT = float(
    os.getenv("MAX_DAILY_LOSS_USDT", "30")
)

MAX_CONSECUTIVE_LOSSES = int(
    os.getenv("MAX_CONSECUTIVE_LOSSES", "4")
)

PORT = int(
    os.getenv("PORT", "8080")
)


# ============================================================
# WHATSAPP
# ============================================================

WHATSAPP_ACCESS_TOKEN = os.getenv(
    "WHATSAPP_ACCESS_TOKEN",
    ""
)

WHATSAPP_PHONE_NUMBER_ID = os.getenv(
    "WHATSAPP_PHONE_NUMBER_ID",
    ""
)

WHATSAPP_TO_NUMBER = os.getenv(
    "WHATSAPP_TO_NUMBER",
    ""
)

WHATSAPP_API_VERSION = os.getenv(
    "WHATSAPP_API_VERSION",
    ""
)


# ============================================================
# GLOBAL STATE
# ============================================================

app = Flask(__name__)

state = {
    "status": "starting",
    "ws_connected": False,
    "last_data": None,
    "last_signal": None,
    "last_error": None,
    "daily_pnl": 0.0,
    "consecutive_losses": 0,
    "trades": 0,
    "wins": 0,
    "losses": 0,
}

data_lock = threading.Lock()

one_second_data = {
    symbol: [] for symbol in SYMBOLS
}

candles_15s = {
    symbol: [] for symbol in SYMBOLS
}

positions = {}

last_trade_time = {
    symbol: 0 for symbol in SYMBOLS
}


# ============================================================
# UTILITY
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(
        f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{msg}",
        flush=True
    )


def okx_timestamp():
    return datetime.now(
        timezone.utc
    ).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


# ============================================================
# OKX SIGNATURE
# ============================================================

def okx_signature(timestamp, method, request_path, body=""):
    message = (
        timestamp +
        method.upper() +
        request_path +
        body
    )

    mac = hmac.new(
        OKX_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    )

    return base64.b64encode(
        mac.digest()
    ).decode()


def okx_headers(method, path, body=""):
    timestamp = okx_timestamp()

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": okx_signature(
            timestamp,
            method,
            path,
            body
        ),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
    }

    if OKX_DEMO:
        headers["x-simulated-trading"] = "1"

    return headers


# ============================================================
# OKX REST
# ============================================================

def private_request(
    method,
    path,
    params=None,
    payload=None
):

    body = ""

    if payload:
        body = json.dumps(
            payload,
            separators=(",", ":")
        )

    url = OKX_BASE_URL + path

    try:
        headers = okx_headers(
            method,
            path,
            body
        )

        response = requests.request(
            method=method,
            url=url,
            params=params,
            data=body,
            headers=headers,
            timeout=10
        )

        return response.json()

    except Exception as e:
        state["last_error"] = str(e)
        log(f"REST ERROR: {e}")
        return {}


def public_request(
    path,
    params=None
):

    try:
        response = requests.get(
            OKX_BASE_URL + path,
            params=params,
            timeout=10
        )

        return response.json()

    except Exception as e:
        state["last_error"] = str(e)
        log(f"PUBLIC REST ERROR: {e}")
        return {}


# ============================================================
# INSTRUMENT
# ============================================================

instrument_cache = {}


def get_instrument(symbol):

    if symbol in instrument_cache:
        return instrument_cache[symbol]

    result = public_request(
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
        return size

    lot_size = float(
        inst.get("lotSz", "1")
    )

    min_size = float(
        inst.get("minSz", lot_size)
    )

    rounded = (
        np.floor(size / lot_size)
        * lot_size
    )

    return max(
        rounded,
        min_size
    )


# ============================================================
# 5M CANDLES
# ============================================================

def get_5m_candles(symbol):

    result = public_request(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": "5m",
            "limit": str(
                STRUCTURE_LOOKBACK + 20
            )
        }
    )

    rows = result.get("data", [])

    if not rows:
        return pd.DataFrame()

    rows = list(reversed(rows))

    records = []

    for r in rows:

        if len(r) < 9:
            continue

        records.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
            "confirm": r[8]
            if len(r) > 8
            else "0"
        })

    df = pd.DataFrame(records)

    return add_indicators(df)


# ============================================================
# INDICATORS
# ============================================================

def ema(series, length):

    return series.ewm(
        span=length,
        adjust=False
    ).mean()


def rsi(series, length=14):

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

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )

    result = 100 - (
        100 / (1 + rs)
    )

    return result.fillna(50)


def atr(df, length=14):

    high_low = (
        df["high"] -
        df["low"]
    )

    high_close = (
        df["high"] -
        df["close"].shift()
    ).abs()

    low_close = (
        df["low"] -
        df["close"].shift()
    ).abs()

    tr = pd.concat(
        [
            high_low,
            high_close,
            low_close
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()


def add_indicators(df):

    if df.empty:
        return df

    df["ema20"] = ema(
        df["close"],
        20
    )

    df["ema50"] = ema(
        df["close"],
        50
    )

    df["rsi"] = rsi(
        df["close"],
        RSI_LENGTH
    )

    df["rsi_ma"] = (
        df["rsi"]
        .rolling(RSI_MA_LENGTH)
        .mean()
    )

    df["atr"] = atr(
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
# STRUCTURE
# ============================================================

def find_pivots(df):

    highs = []
    lows = []

    if len(df) < (
        PIVOT_LEFT +
        PIVOT_RIGHT +
        5
    ):
        return highs, lows

    for i in range(
        PIVOT_LEFT,
        len(df) - PIVOT_RIGHT
    ):

        high_value = df["high"].iloc[i]

        left_highs = df[
            "high"
        ].iloc[
            i - PIVOT_LEFT:i
        ]

        right_highs = df[
            "high"
        ].iloc[
            i + 1:
            i + 1 + PIVOT_RIGHT
        ]

        if (
            high_value > left_highs.max()
            and
            high_value > right_highs.max()
        ):
            highs.append(
                (i, high_value)
            )

        low_value = df["low"].iloc[i]

        left_lows = df[
            "low"
        ].iloc[
            i - PIVOT_LEFT:i
        ]

        right_lows = df[
            "low"
        ].iloc[
            i + 1:
            i + 1 + PIVOT_RIGHT
        ]

        if (
            low_value < left_lows.min()
            and
            low_value < right_lows.min()
        ):
            lows.append(
                (i, low_value)
            )

    return highs, lows


def analyze_structure(df):

    if len(df) < 20:
        return None

    highs, lows = find_pivots(df)

    if not highs and not lows:
        return None

    last_high = (
        highs[-1][1]
        if highs
        else None
    )

    last_low = (
        lows[-1][1]
        if lows
        else None
    )

    last_close = float(
        df["close"].iloc[-1]
    )

    direction = "NONE"

    if last_high is not None:

        if last_close > (
            last_high *
            (
                1 +
                BREAK_BUFFER_PCT / 100
            )
        ):
            direction = "BUY"

    if last_low is not None:

        if last_close < (
            last_low *
            (
                1 -
                BREAK_BUFFER_PCT / 100
            )
        ):
            direction = "SELL"

    return {
        "direction": direction,
        "resistance": last_high,
        "support": last_low,
        "close": last_close,
        "atr": float(
            df["atr"].iloc[-1]
        ),
    }


# ============================================================
# 1 SECOND -> 15 SECOND
# ============================================================

def add_1s_candle(
    symbol,
    candle
):

    with data_lock:

        one_second_data[symbol].append(
            candle
        )

        if len(
            one_second_data[symbol]
        ) > 300:

            one_second_data[symbol] = (
                one_second_data[symbol][-300:]
            )


def build_15s_candles(symbol):

    with data_lock:

        rows = list(
            one_second_data[symbol]
        )

    if not rows:
        return

    buckets = {}

    for r in rows:

        bucket = (
            r["ts"] // 15000
        ) * 15000

        buckets.setdefault(
            bucket,
            []
        ).append(r)

    finished = []

    for bucket, items in buckets.items():

        if len(items) < 12:
            continue

        items.sort(
            key=lambda x: x["ts"]
        )

        finished.append({
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

    if not finished:
        return

    df = pd.DataFrame(
        finished
    )

    df = df.drop_duplicates(
        "ts"
    ).sort_values("ts")

    df = add_indicators(df)

    with data_lock:

        candles_15s[symbol] = (
            df.tail(100)
            .to_dict("records")
        )


# ============================================================
# ENTRY SIGNAL
# ============================================================

def find_entry(
    symbol,
    structure
):

    with data_lock:

        rows = list(
            candles_15s[symbol]
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

    body = abs(
        current["close"] -
        current["open"]
    )

    candle_range = (
        current["high"] -
        current["low"]
    )

    if candle_range <= 0:
        return None

    body_ratio = (
        body / candle_range
    )

    if body_ratio < MIN_BODY_RATIO:
        return None

    volume_ratio = current[
        "volume_ratio"
    ]

    if (
        pd.notna(volume_ratio)
        and
        volume_ratio <
        MIN_VOLUME_RATIO
    ):
        return None

    atr_value = current["atr"]

    if (
        pd.isna(atr_value)
        or
        atr_value <= 0
    ):
        return None

    # BUY
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

            extension = (
                current["close"] -
                structure["resistance"]
            )

            if extension > (
                atr_value *
                MAX_EXTENSION_ATR
            ):
                return None

            return {
                "side": "buy",
                "price": float(
                    current["close"]
                ),
                "atr": float(
                    atr_value
                ),
                "reason": "5m BOS + 15s bullish confirmation"
            }

    # SELL
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

            extension = (
                structure["support"] -
                current["close"]
            )

            if extension > (
                atr_value *
                MAX_EXTENSION_ATR
            ):
                return None

            return {
                "side": "sell",
                "price": float(
                    current["close"]
                ),
                "atr": float(
                    atr_value
                ),
                "reason": "5m BOS + 15s bearish confirmation"
            }

    return None


# ============================================================
# ORDER SIZE
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
        ct_val = 1

    notional = (
        MARGIN_USDT *
        LEVERAGE
    )

    size = (
        notional /
        (
            price *
            ct_val
        )
    )

    return round_size(
        symbol,
        size
    )


# ============================================================
# MARKET ORDER
# ============================================================

def place_market_order(
    symbol,
    side,
    size
):

    if not API_READY():
        return None

    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": side,
        "ordType": "market",
        "sz": str(size)
    }

    result = private_request(
        "POST",
        "/api/v5/trade/order",
        payload=payload
    )

    log(
        f"ORDER {symbol} {side} "
        f"{size}: {result}"
    )

    data = result.get("data", [])

    if not data:
        return None

    if data[0].get("sCode") != "0":
        return None

    return data[0]


# ============================================================
# API READY
# ============================================================

def API_READY():

    if not OKX_API_KEY:
        log("OKX_API_KEY missing")
        return False

    if not OKX_SECRET_KEY:
        log("OKX_SECRET_KEY missing")
        return False

    if not OKX_PASSPHRASE:
        log("OKX_PASSPHRASE missing")
        return False

    if not OKX_DEMO and not ALLOW_LIVE:
        log(
            "LIVE trading blocked. "
            "Set ALLOW_LIVE=true only if you really want live."
        )
        return False

    return True


# ============================================================
# WHATSAPP ALERT
# ============================================================

def send_whatsapp(message):

    if not all([
        WHATSAPP_ACCESS_TOKEN,
        WHATSAPP_PHONE_NUMBER_ID,
        WHATSAPP_TO_NUMBER,
        WHATSAPP_API_VERSION
    ]):
        return False

    url = (
        f"https://graph.facebook.com/"
        f"{WHATSAPP_API_VERSION}/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization":
            f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type":
            "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": WHATSAPP_TO_NUMBER,
        "type": "text",
        "text": {
            "body": message
        }
    }

    try:

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10
        )

        log(
            f"WHATSAPP: "
            f"{response.status_code} "
            f"{response.text}"
        )

        return response.ok

    except Exception as e:

        log(
            f"WHATSAPP ERROR: {e}"
        )

        return False


# ============================================================
# OPEN POSITION
# ============================================================

def open_position(
    symbol,
    signal
):

    now = time.time()

    if (
        now -
        last_trade_time[symbol]
        <
        COOLDOWN_SECONDS
    ):
        return

    if symbol in positions:
        return

    if (
        state["daily_pnl"] <=
        -MAX_DAILY_LOSS_USDT
    ):
        return

    if (
        state["consecutive_losses"] >=
        MAX_CONSECUTIVE_LOSSES
    ):
        return

    price = signal["price"]

    size = calculate_size(
        symbol,
        price
    )

    if size <= 0:
        return

    if not AUTO_TRADE:

        log(
            f"SIGNAL ONLY: "
            f"{symbol} "
            f"{signal['side']} "
            f"@ {price}"
        )

        send_whatsapp(
            f"🤖 OKX DEMO SIGNAL\n"
            f"{symbol}\n"
            f"{signal['side'].upper()}\n"
            f"Price: {price}\n"
            f"Reason: {signal['reason']}"
        )

        state["last_signal"] = {
            "symbol": symbol,
            **signal
        }

        return

    order = place_market_order(
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

    message = (
        f"🟢 OKX DEMO ENTRY\n"
        f"{symbol}\n"
        f"Side: {signal['side'].upper()}\n"
        f"Entry: {price}\n"
        f"Size: {size}\n"
        f"Reason: {signal['reason']}"
    )

    log(message)

    send_whatsapp(message)


# ============================================================
# CLOSE POSITION
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

        order = place_market_order(
            symbol,
            close_side,
            position["size"]
        )

        if not order:
            return

    entry = position["entry"]

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

    message = (
        f"🔴 OKX EXIT\n"
        f"{symbol}\n"
        f"Side: {side.upper()}\n"
        f"Entry: {entry}\n"
        f"Exit: {price}\n"
        f"Estimated PnL: {pnl:.4f} USDT\n"
        f"Reason: {reason}"
    )

    log(message)

    send_whatsapp(message)

    del positions[symbol]


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def manage_position(
    symbol
):

    position = positions.get(
        symbol
    )

    if not position:
        return

    with data_lock:

        rows = list(
            candles_15s[symbol]
        )

    if not rows:
        return

    df = pd.DataFrame(rows)

    df = add_indicators(df)

    current = df.iloc[-1]

    price = float(
        current["close"]
    )

    atr_value = float(
        current["atr"]
    )

    side = position["side"]

    age = (
        time.time() -
        position["time"]
    )

    entry = position["entry"]

    if side == "buy":

        if price > position["best_price"]:
            position["best_price"] = price

        emergency_sl = (
            entry -
            (
                atr_value *
                EMERGENCY_SL_ATR
            )
        )

        trailing_sl = (
            position["best_price"] -
            (
                atr_value *
                TRAIL_ATR_MULT
            )
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
                "Trailing structure exit"
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
                "15s momentum failure"
            )

            return

    else:

        if price < position["best_price"]:
            position["best_price"] = price

        emergency_sl = (
            entry +
            (
                atr_value *
                EMERGENCY_SL_ATR
            )
        )

        trailing_sl = (
            position["best_price"] +
            (
                atr_value *
                TRAIL_ATR_MULT
            )
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
                "Trailing structure exit"
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
                "15s momentum failure"
            )

            return

    if age >= MAX_HOLD_SECONDS:

        close_position(
            symbol,
            price,
            "Maximum 30 second hold"
        )


# ============================================================
# WEBSOCKET
# ============================================================

def websocket_message(
    ws,
    message
):

    try:

        if message == "ping":

            ws.send("pong")
            return

        obj = json.loads(
            message
        )

        if obj.get("event") == "error":

            log(
                "WS ERROR: "
                + str(obj)
            )

            state["last_error"] = str(
                obj
            )

            return

        if obj.get("event") == "subscribe":

            log(
                "WS SUBSCRIBED: "
                + str(obj.get("arg"))
            )

            return

        arg = obj.get(
            "arg",
            {}
        )

        channel = arg.get(
            "channel"
        )

        if channel != "candle1s":
            return

        data = obj.get(
            "data",
            []
        )

        if not data:
            return

        symbol = arg.get(
            "instId"
        )

        if symbol not in SYMBOLS:
            return

        row = data[0]

        ts = int(row[0])

        candle = {
            "ts": ts,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        }

        add_1s_candle(
            symbol,
            candle
        )

        state["last_data"] = {
            "symbol": symbol,
            "price": candle["close"],
            "time": ts
        }

        build_15s_candles(
            symbol
        )

        # manage existing position
        manage_position(
            symbol
        )

    except Exception as e:

        state["last_error"] = str(e)

        log(
            f"WS MESSAGE ERROR: {e}"
        )


def websocket_open(ws):

    state["ws_connected"] = True
    state["status"] = "running"

    log(
        "OKX DEMO WEBSOCKET CONNECTED"
    )

    args = []

    for symbol in SYMBOLS:

        args.append({
            "channel": "candle1s",
            "instId": symbol
        })

    subscribe = {
        "op": "subscribe",
        "args": args
    }

    ws.send(
        json.dumps(subscribe)
    )

    log(
        "Subscribed to candle1s: "
        + ",".join(SYMBOLS)
    )


def websocket_error(
    ws,
    error
):

    state["ws_connected"] = False
    state["last_error"] = str(
        error
    )

    log(
        f"WEBSOCKET ERROR: {error}"
    )


def websocket_close(
    ws,
    code,
    msg
):

    state["ws_connected"] = False

    log(
        f"WEBSOCKET CLOSED: "
        f"{code} {msg}"
    )


def websocket_loop():

    while True:

        try:

            log(
                "Connecting OKX Demo WebSocket..."
            )

            ws = websocket.WebSocketApp(
                OKX_WS_BUSINESS,
                on_open=websocket_open,
                on_message=websocket_message,
                on_error=websocket_error,
                on_close=websocket_close
            )

            ws.run_forever(
                ping_interval=15,
                ping_timeout=10
            )

        except Exception as e:

            state["last_error"] = str(e)

            log(
                f"WS LOOP ERROR: {e}"
            )

        state["ws_connected"] = False

        log(
            "Reconnecting in 5 seconds..."
        )

        time.sleep(5)


# ============================================================
# STRUCTURE LOOP
# ============================================================

def structure_loop():

    while True:

        try:

            for symbol in SYMBOLS:

                df = get_5m_candles(
                    symbol
                )

                if df.empty:
                    continue

                # Ignore unfinished 5m candle
                if len(df) > 1:

                    df = df.iloc[:-1].copy()

                structure = analyze_structure(
                    df
                )

                if not structure:
                    continue

                signal = find_entry(
                    symbol,
                    structure
                )

                if signal:

                    log(
                        f"SIGNAL "
                        f"{symbol} "
                        f"{signal['side']} "
                        f"@ {signal['price']}"
                    )

                    state["last_signal"] = {
                        "symbol": symbol,
                        **signal
                    }

                    open_position(
                        symbol,
                        signal
                    )

        except Exception as e:

            state["last_error"] = str(e)

            log(
                f"STRUCTURE LOOP ERROR: {e}"
            )

        time.sleep(2)


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "bot": "ICT SwiftEdge OKX Scalper",
        "status": state["status"],
        "okx_demo": OKX_DEMO,
        "auto_trade": AUTO_TRADE,
        "symbols": SYMBOLS,
        "websocket": state["ws_connected"],
        "positions": positions,
        "daily_pnl": state["daily_pnl"],
        "trades": state["trades"],
        "wins": state["wins"],
        "losses": state["losses"],
        "last_data": state["last_data"],
        "last_signal": state["last_signal"],
        "last_error": state["last_error"]
    })


@app.route("/health")
def health():

    return "OK", 200


@app.route("/api/status")
def api_status():

    return jsonify({
        **state,
        "positions": positions
    })


# ============================================================
# START
# ============================================================

def start_bot():

    log("=" * 60)
    log("ICT SWIFTEDGE OKX SCALPER")
    log("=" * 60)

    log(
        f"OKX DEMO: {OKX_DEMO}"
    )

    log(
        f"AUTO TRADE: {AUTO_TRADE}"
    )

    log(
        f"WS: {OKX_WS_BUSINESS}"
    )

    log(
        f"SYMBOLS: {SYMBOLS}"
    )

    log("=" * 60)

    ws_thread = threading.Thread(
        target=websocket_loop,
        daemon=True
    )

    ws_thread.start()

    structure_thread = threading.Thread(
        target=structure_loop,
        daemon=True
    )

    structure_thread.start()


if __name__ == "__main__":

    start_bot()

    app.run(
        host="0.0.0.0",
        port=PORT
    )
