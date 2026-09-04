# ============================================================
# ICT SWIFTEDGE SCALPER V2
# 5 MINUTE STRUCTURE + 15 SECOND EXECUTION
# OKX SWAP / Railway Ready
# ============================================================

import os
import time
import json
import hmac
import base64
import hashlib
import threading
from decimal import Decimal, ROUND_DOWN
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
import pandas as pd
import numpy as np
from flask import Flask, jsonify
import websocket
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

OKX_BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://www.okx.com"
)

OKX_WS_PUBLIC = os.getenv(
    "OKX_WS_PUBLIC",
    "wss://ws.okx.com:8443/ws/v5/public"
)

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

# Demo should remain TRUE while testing
OKX_DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"

# Auto trade
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"

# Extra protection against accidental live trading
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

# Symbols
SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP"
    ).split(",")
    if x.strip()
]

# Money / leverage
MARGIN_USDT = Decimal(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = Decimal(os.getenv("LEVERAGE", "3"))

TD_MODE = os.getenv(
    "TD_MODE",
    "isolated"
)

# Server
PORT = int(os.getenv("PORT", "8080"))

# ============================================================
# 5M STRUCTURE SETTINGS
# ============================================================

STRUCTURE_LOOKBACK = int(
    os.getenv("STRUCTURE_LOOKBACK", "80")
)

PIVOT_LEFT = int(
    os.getenv("PIVOT_LEFT", "2")
)

PIVOT_RIGHT = int(
    os.getenv("PIVOT_RIGHT", "2")
)

# ============================================================
# 15 SECOND ENTRY SETTINGS
# ============================================================

RSI_LENGTH = int(
    os.getenv("RSI_LENGTH", "14")
)

RSI_MA_LENGTH = int(
    os.getenv("RSI_MA_LENGTH", "7")
)

BREAK_BUFFER_PCT = Decimal(
    os.getenv("BREAK_BUFFER_PCT", "0.015")
)

RETEST_TOLERANCE_PCT = Decimal(
    os.getenv("RETEST_TOLERANCE_PCT", "0.10")
)

MIN_BODY_RATIO = Decimal(
    os.getenv("MIN_BODY_RATIO", "0.45")
)

MIN_VOLUME_RATIO = Decimal(
    os.getenv("MIN_VOLUME_RATIO", "1.05")
)

# Do not chase a huge candle
MAX_EXTENSION_ATR = Decimal(
    os.getenv("MAX_EXTENSION_ATR", "1.20")
)

# ============================================================
# EXIT SETTINGS
# ============================================================

# User requested 15–30 second fast exit
MIN_HOLD_SECONDS = int(
    os.getenv("MIN_HOLD_SECONDS", "3")
)

MAX_HOLD_SECONDS = int(
    os.getenv("MAX_HOLD_SECONDS", "30")
)

# Start trailing protection after this time
TRAIL_START_SECONDS = int(
    os.getenv("TRAIL_START_SECONDS", "12")
)

TRAIL_ATR_MULT = Decimal(
    os.getenv("TRAIL_ATR_MULT", "0.75")
)

# Emergency protection only
EMERGENCY_SL_ATR = Decimal(
    os.getenv("EMERGENCY_SL_ATR", "1.60")
)

# ============================================================
# RISK CONTROL
# ============================================================

COOLDOWN_SECONDS = int(
    os.getenv("COOLDOWN_SECONDS", "45")
)

MAX_DAILY_LOSS = Decimal(
    os.getenv("MAX_DAILY_LOSS_USDT", "30")
)

MAX_CONSECUTIVE_LOSSES = int(
    os.getenv("MAX_CONSECUTIVE_LOSSES", "4")
)

# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
)

# ============================================================
# APP / STATE
# ============================================================

app = Flask(__name__)

state_lock = threading.Lock()

# 1 second candles received from websocket
one_second_data = defaultdict(lambda: deque(maxlen=120))

# Locally created 15 second candles
candles_15s = defaultdict(lambda: deque(maxlen=300))

# Latest 5m dataframe
structure_data = {}

# Active positions
positions = {}

# Cooldowns
cooldowns = {}

# Instrument cache
instrument_cache = {}

# Last status
last_status = {}

# Recent trades
trade_history = deque(maxlen=300)

# Websocket
ws_connected = False

# Statistics
stats = {
    "date": datetime.now(timezone.utc).date().isoformat(),
    "wins": 0,
    "losses": 0,
    "pnl": Decimal("0"),
    "consecutive_losses": 0,
}


# ============================================================
# BASIC HELPERS
# ============================================================

def D(value):
    return Decimal(str(value))


def current_time():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def timestamp_ms():
    return int(time.time() * 1000)


def reset_daily_stats():
    today = datetime.now(timezone.utc).date().isoformat()

    if stats["date"] != today:
        stats["date"] = today
        stats["wins"] = 0
        stats["losses"] = 0
        stats["pnl"] = Decimal("0")
        stats["consecutive_losses"] = 0


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:
        return

    if not TELEGRAM_CHAT_ID:
        return

    try:

        url = (
            "https://api.telegram.org/bot"
            + TELEGRAM_BOT_TOKEN
            + "/sendMessage"
        )

        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=8
        )

    except Exception as e:

        print(
            "Telegram error:",
            e
        )


# ============================================================
# OKX SIGNATURE
# ============================================================

def okx_signature(
    timestamp,
    method,
    request_path,
    body=""
):

    message = (
        timestamp
        + method.upper()
        + request_path
        + body
    )

    digest = hmac.new(
        OKX_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(
        digest
    ).decode()


# ============================================================
# OKX PRIVATE REQUEST
# ============================================================

def private_request(
    method,
    path,
    params=None,
    body=None
):

    params = params or {}
    body = body or {}

    method = method.upper()

    if method == "GET":

        query = urlencode(params)

        request_path = (
            path
            + ("?" + query if query else "")
        )

        body_text = ""

    else:

        request_path = path

        body_text = json.dumps(
            body,
            separators=(",", ":")
        )

    ts = (
        datetime.now(timezone.utc)
        .isoformat(
            timespec="milliseconds"
        )
        .replace("+00:00", "Z")
    )

    headers = {

        "OK-ACCESS-KEY":
            OKX_API_KEY,

        "OK-ACCESS-SIGN":
            okx_signature(
                ts,
                method,
                request_path,
                body_text
            ),

        "OK-ACCESS-TIMESTAMP":
            ts,

        "OK-ACCESS-PASSPHRASE":
            OKX_PASSPHRASE,

        "Content-Type":
            "application/json"
    }

    if OKX_DEMO:

        headers[
            "x-simulated-trading"
        ] = "1"

    response = requests.request(

        method,

        OKX_BASE_URL
        + request_path,

        headers=headers,

        params=(
            params
            if method == "GET"
            else None
        ),

        json=(
            body
            if method != "GET"
            else None
        ),

        timeout=10
    )

    response.raise_for_status()

    data = response.json()

    if str(data.get("code")) != "0":

        raise RuntimeError(
            f"OKX error: {data}"
        )

    return data


# ============================================================
# OKX PUBLIC REQUEST
# ============================================================

def public_request(
    path,
    params=None
):

    response = requests.get(

        OKX_BASE_URL + path,

        params=params or {},

        timeout=10
    )

    response.raise_for_status()

    data = response.json()

    if str(data.get("code")) != "0":

        raise RuntimeError(
            f"OKX public error: {data}"
        )

    return data


# ============================================================
# INSTRUMENT INFORMATION
# ============================================================

def get_instrument(inst_id):

    if inst_id in instrument_cache:

        return instrument_cache[
            inst_id
        ]

    data = public_request(

        "/api/v5/public/instruments",

        {
            "instType": "SWAP",
            "instId": inst_id
        }
    )

    if not data["data"]:

        raise RuntimeError(
            f"Instrument not found: {inst_id}"
        )

    x = data["data"][0]

    info = {

        "ctVal":
            D(x["ctVal"]),

        "lotSz":
            D(x["lotSz"]),

        "minSz":
            D(x["minSz"]),

        "tickSz":
            D(x["tickSz"])
    }

    instrument_cache[
        inst_id
    ] = info

    return info


def round_step(
    value,
    step
):

    if step <= 0:

        return value

    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step


# ============================================================
# MARKET PRICE
# ============================================================

def get_last_price(symbol):

    data = public_request(

        "/api/v5/market/ticker",

        {
            "instId": symbol
        }
    )

    return D(
        data["data"][0]["last"]
    )


# ============================================================
# INDICATORS
# ============================================================

def EMA(
    series,
    period
):

    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def RSI(
    series,
    period=14
):

    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    rs = (
        avg_gain
        /
        avg_loss.replace(
            0,
            np.nan
        )
    )

    return (
        100
        -
        100 / (1 + rs)
    )


def ATR(
    df,
    period=14
):

    previous_close = (
        df["close"].shift(1)
    )

    tr = pd.concat(

        [
            df["high"] - df["low"],

            (
                df["high"]
                - previous_close
            ).abs(),

            (
                df["low"]
                - previous_close
            ).abs()
        ],

        axis=1

    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


def add_indicators(df):

    df = df.copy()

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
        .rolling(
            RSI_MA_LENGTH
        )
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

    df["body"] = (
        df["close"]
        - df["open"]
    ).abs()

    df["range"] = (
        df["high"]
        - df["low"]
    ).replace(
        0,
        np.nan
    )

    df["body_ratio"] = (
        df["body"]
        /
        df["range"]
    )

    return df


# ============================================================
# GET CONFIRMED 5M CANDLES
# ============================================================

def get_5m_candles(symbol):

    data = public_request(

        "/api/v5/market/candles",

        {
            "instId": symbol,
            "bar": "5m",
            "limit": "100"
        }
    )

    rows = []

    for x in reversed(
        data["data"]
    ):

        # OKX candle:
        # ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm

        if len(x) >= 9:

            if x[8] != "1":
                continue

        rows.append({

            "ts":
                int(x[0]),

            "open":
                float(x[1]),

            "high":
                float(x[2]),

            "low":
                float(x[3]),

            "close":
                float(x[4]),

            "volume":
                float(x[5])
        })

    df = pd.DataFrame(
        rows
    )

    if len(df) < 40:

        return None

    return add_indicators(
        df
    )


# ============================================================
# CONFIRMED PIVOTS
# ============================================================

def find_pivots(df):

    highs = []
    lows = []

    left = PIVOT_LEFT
    right = PIVOT_RIGHT

    for i in range(
        left,
        len(df) - right
    ):

        high = df.iloc[i][
            "high"
        ]

        low = df.iloc[i][
            "low"
        ]

        left_high = df.iloc[
            i-left:i
        ]["high"].max()

        right_high = df.iloc[
            i+1:i+right+1
        ]["high"].max()

        left_low = df.iloc[
            i-left:i
        ]["low"].min()

        right_low = df.iloc[
            i+1:i+right+1
        ]["low"].min()

        if (
            high > left_high
            and high >= right_high
        ):

            highs.append(
                (i, high)
            )

        if (
            low < left_low
            and low <= right_low
        ):

            lows.append(
                (i, low)
            )

    return highs, lows


# ============================================================
# 5M STRUCTURE ENGINE
# ============================================================

def analyze_structure(df):

    highs, lows = find_pivots(
        df
    )

    if len(highs) < 2:
        return {
            "bias": "NONE",
            "level": None,
            "event": None,
            "atr": D("0")
        }

    if len(lows) < 2:
        return {
            "bias": "NONE",
            "level": None,
            "event": None,
            "atr": D("0")
        }

    current = df.iloc[-1]
    previous = df.iloc[-2]

    last_high = D(
        highs[-1][1]
    )

    previous_high = D(
        highs[-2][1]
    )

    last_low = D(
        lows[-1][1]
    )

    previous_low = D(
        lows[-2][1]
    )

    close_now = D(
        current["close"]
    )

    close_previous = D(
        previous["close"]
    )

    bullish_bos = (
        close_now > last_high
        and
        close_previous <= last_high
    )

    bearish_bos = (
        close_now < last_low
        and
        close_previous >= last_low
    )

    # Market structure context
    if (
        close_now > last_high
    ):

        return {

            "bias":
                "BUY",

            "level":
                last_high,

            "event":
                "BOS",

            "atr":
                D(current["atr"])
        }

    if (
        close_now < last_low
    ):

        return {

            "bias":
                "SELL",

            "level":
                last_low,

            "event":
                "BOS",

            "atr":
                D(current["atr"])
        }

    # EMA structure context
    if (
        D(current["ema20"])
        >
        D(current["ema50"])
    ):

        return {

            "bias":
                "BUY",

            "level":
                last_high,

            "event":
                None,

            "atr":
                D(current["atr"])
        }

    if (
        D(current["ema20"])
        <
        D(current["ema50"])
    ):

        return {

            "bias":
                "SELL",

            "level":
                last_low,

            "event":
                None,

            "atr":
                D(current["atr"])
        }

    return {

        "bias":
            "NONE",

        "level":
            None,

        "event":
            None,

        "atr":
            D(current["atr"])
    }


# ============================================================
# BUILD 15 SECOND CANDLES
# ============================================================

def add_1s_data(
    symbol,
    timestamp,
    open_price,
    high,
    low,
    close,
    volume
):

    bucket = (
        timestamp // 15000
    ) * 15000

    with state_lock:

        q = one_second_data[
            symbol
        ]

        q.append(

            (
                timestamp,
                open_price,
                high,
                low,
                close,
                volume
            )
        )

        # Keep only recent data
        while q and (
            q[0][0]
            <
            bucket - 60000
        ):

            q.popleft()

        candles = [

            x for x in q

            if (
                x[0] // 15000
            ) * 15000
            ==
            bucket
        ]

        # Need enough 1s samples
        if len(candles) < 10:

            return None

        # Do not finalize before 15 seconds
        if timestamp < (
            bucket + 14000
        ):

            return None

        if (
            candles_15s[symbol]
            and
            candles_15s[symbol][-1]["ts"]
            ==
            bucket
        ):

            return None

        candle = {

            "ts":
                bucket,

            "open":
                candles[0][1],

            "high":
                max(
                    x[2]
                    for x in candles
                ),

            "low":
                min(
                    x[3]
                    for x in candles
                ),

            "close":
                candles[-1][4],

            "volume":
                sum(
                    x[5]
                    for x in candles
                )
        }

        candles_15s[
            symbol
        ].append(
            candle
        )

        return candle


# ============================================================
# 15 SECOND DATAFRAME
# ============================================================

def get_15s_df(symbol):

    with state_lock:

        rows = list(
            candles_15s[symbol]
        )

    if len(rows) < 35:

        return None

    df = pd.DataFrame(
        rows
    )

    return add_indicators(
        df
    )


# ============================================================
# ENTRY LOGIC
# ============================================================

def find_entry(
    symbol
):

    df5 = structure_data.get(
        symbol
    )

    if df5 is None:

        return None

    df15 = get_15s_df(
        symbol
    )

    if df15 is None:

        return None

    structure = analyze_structure(
        df5
    )

    if structure["level"] is None:

        return None

    x = df15.iloc[-1]
    prev = df15.iloc[-2]

    level = structure[
        "level"
    ]

    price = D(
        x["close"]
    )

    atr15 = D(
        x["atr"]
    )

    if atr15 <= 0:

        return None

    # ------------------------------------
    # Candle quality
    # ------------------------------------

    body_ratio = D(
        x["body_ratio"]
    )

    if body_ratio < MIN_BODY_RATIO:

        return None

    # ------------------------------------
    # RSI-MA confirmation
    # ------------------------------------

    if (
        pd.isna(x["rsi"])
        or
        pd.isna(x["rsi_ma"])
    ):

        return None

    buy_rsi = (
        x["rsi"] > 50
        and
        x["rsi_ma"] > 50
    )

    sell_rsi = (
        x["rsi"] < 50
        and
        x["rsi_ma"] < 50
    )

    # ------------------------------------
    # EMA confirmation
    # ------------------------------------

    buy_ema = (
        x["ema20"]
        >
        x["ema50"]
    )

    sell_ema = (
        x["ema20"]
        <
        x["ema50"]
    )

    # ------------------------------------
    # Volume
    # ------------------------------------

    volume_ok = False

    if not pd.isna(
        x["volume_ma"]
    ):

        volume_ok = (
            D(x["volume"])
            >=
            D(x["volume_ma"])
            *
            MIN_VOLUME_RATIO
        )

    # ------------------------------------
    # Price break
    # ------------------------------------

    break_up = (
        price
        >
        level
        *
        (
            Decimal("1")
            +
            BREAK_BUFFER_PCT
            /
            Decimal("100")
        )
    )

    break_down = (
        price
        <
        level
        *
        (
            Decimal("1")
            -
            BREAK_BUFFER_PCT
            /
            Decimal("100")
        )
    )

    # ------------------------------------
    # Retest
    # ------------------------------------

    tolerance = (
        level
        *
        RETEST_TOLERANCE_PCT
        /
        Decimal("100")
    )

    previous_low = D(
        prev["low"]
    )

    previous_high = D(
        prev["high"]
    )

    retest_buy = (
        previous_low
        <=
        level + tolerance
    )

    retest_sell = (
        previous_high
        >=
        level - tolerance
    )

    # ------------------------------------
    # Candle direction
    # ------------------------------------

    green = (
        D(x["close"])
        >
        D(x["open"])
    )

    red = (
        D(x["close"])
        <
        D(x["open"])
    )

    # ------------------------------------
    # Do not chase
    # ------------------------------------

    extension = abs(
        price - level
    )

    not_chasing = (
        extension
        <=
        atr15
        *
        MAX_EXTENSION_ATR
    )

    if not not_chasing:

        return None

    # ========================================================
    # BUY
    # ========================================================

    if (
        structure["bias"]
        ==
        "BUY"

        and
        break_up

        and
        retest_buy

        and
        green

        and
        buy_rsi

        and
        buy_ema
    ):

        return {

            "side":
                "BUY",

            "entry":
                price,

            "level":
                level,

            "event":
                structure["event"]
                or
                "STRUCTURE_BREAK",

            "atr":
                atr15,

            "rsi":
                float(x["rsi"]),

            "volume_ok":
                volume_ok
        }

    # ========================================================
    # SELL
    # ========================================================

    if (
        structure["bias"]
        ==
        "SELL"

        and
        break_down

        and
        retest_sell

        and
        red

        and
        sell_rsi

        and
        sell_ema
    ):

        return {

            "side":
                "SELL",

            "entry":
                price,

            "level":
                level,

            "event":
                structure["event"]
                or
                "STRUCTURE_BREAK",

            "atr":
                atr15,

            "rsi":
                float(x["rsi"]),

            "volume_ok":
                volume_ok
        }

    return None


# ============================================================
# ORDER SIZE
# ============================================================

def calculate_size(
    symbol,
    price
):

    info = get_instrument(
        symbol
    )

    notional = (
        MARGIN_USDT
        *
        LEVERAGE
    )

    raw_size = (
        notional
        /
        (
            price
            *
            info["ctVal"]
        )
    )

    size = round_step(
        raw_size,
        info["lotSz"]
    )

    if size < info["minSz"]:

        size = info["minSz"]

    return size


# ============================================================
# MARKET ENTRY
# ============================================================

def market_entry(
    symbol,
    side,
    size
):

    if not AUTO_TRADE:

        return {

            "simulated":
                True,

            "ordId":
                "SIM-"
                + str(timestamp_ms())
        }

    if (
        not OKX_DEMO
        and
        not ALLOW_LIVE
    ):

        raise RuntimeError(
            "LIVE trading blocked. "
            "Set ALLOW_LIVE=true explicitly."
        )

    order_side = (
        "buy"
        if side == "BUY"
        else "sell"
    )

    body = {

        "instId":
            symbol,

        "tdMode":
            TD_MODE,

        "side":
            order_side,

        "ordType":
            "market",

        "sz":
            str(size)
    }

    result = private_request(

        "POST",

        "/api/v5/trade/order",

        body=body
    )

    return result[
        "data"
    ][0]


# ============================================================
# MARKET CLOSE
# ============================================================

def market_close(
    symbol,
    side,
    size
):

    if not AUTO_TRADE:

        return {

            "simulated":
                True,

            "ordId":
                "SIM-CLOSE-"
                + str(timestamp_ms())
        }

    close_side = (
        "sell"
        if side == "BUY"
        else "buy"
    )

    body = {

        "instId":
            symbol,

        "tdMode":
            TD_MODE,

        "side":
            close_side,

        "ordType":
            "market",

        "sz":
            str(size),

        "reduceOnly":
            "true"
    }

    result = private_request(

        "POST",

        "/api/v5/trade/order",

        body=body
    )

    return result[
        "data"
    ][0]


# ============================================================
# PNL ESTIMATE
# ============================================================

def estimate_pnl(
    position,
    exit_price
):

    if position["side"] == "BUY":

        movement = (
            exit_price
            -
            position["entry"]
        )

    else:

        movement = (
            position["entry"]
            -
            exit_price
        )

    return (
        movement
        *
        position["size"]
        *
        position["ctVal"]
    )


# ============================================================
# CAN WE TRADE?
# ============================================================

def trading_allowed():

    reset_daily_stats()

    if (
        stats["pnl"]
        <=
        -MAX_DAILY_LOSS
    ):

        return False

    if (
        stats["consecutive_losses"]
        >=
        MAX_CONSECUTIVE_LOSSES
    ):

        return False

    return True


# ============================================================
# TELEGRAM ENTRY
# ============================================================

def telegram_entry(
    symbol,
    signal,
    size
):

    message = (

        "🟢 <b>ICT SWIFTEDGE 15s SCALP</b>\n\n"

        f"<b>{symbol}</b>\n"

        f"Direction: <b>{signal['side']}</b>\n"

        f"Entry: <code>{signal['entry']}</code>\n"

        f"Structure: {signal['event']}\n"

        f"Break Level: <code>{signal['level']}</code>\n"

        f"RSI: {signal['rsi']:.2f}\n"

        f"Volume Confirm: "
        f"{'YES' if signal['volume_ok'] else 'NO'}\n"

        f"Size: <code>{size}</code>\n"

        "Monitoring: 15–30 seconds\n"

        f"Time: {current_time()}"
    )

    send_telegram(
        message
    )


# ============================================================
# TELEGRAM EXIT
# ============================================================

def telegram_exit(
    symbol,
    position,
    reason,
    exit_price,
    pnl
):

    icon = (
        "✅"
        if pnl >= 0
        else
        "❌"
    )

    hold_time = (
        time.time()
        -
        position["entry_time"]
    )

    message = (

        f"{icon} <b>SCALP EXIT</b>\n\n"

        f"<b>{symbol}</b>\n"

        f"Direction: {position['side']}\n"

        f"Entry: <code>{position['entry']}</code>\n"

        f"Exit: <code>{exit_price}</code>\n"

        f"Reason: {reason}\n"

        f"Estimated PnL: "
        f"<code>{pnl:.4f} USDT</code>\n"

        f"Hold: {hold_time:.1f}s\n"

        f"Time: {current_time()}"
    )

    send_telegram(
        message
    )


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

    try:

        price = get_last_price(
            symbol
        )

    except Exception as e:

        print(
            "Price error:",
            e
        )

        return

    elapsed = (
        time.time()
        -
        position["entry_time"]
    )

    df15 = get_15s_df(
        symbol
    )

    if df15 is None:

        return

    x = df15.iloc[-1]

    atr15 = D(
        x["atr"]
    )

    if atr15 <= 0:

        atr15 = position[
            "atr"
        ]

    reason = None

    side = position[
        "side"
    ]

    entry = position[
        "entry"
    ]

    # ========================================================
    # 1. EMERGENCY STOP
    # ========================================================

    if side == "BUY":

        emergency_stop = (
            entry
            -
            position["atr"]
            *
            EMERGENCY_SL_ATR
        )

        if price <= emergency_stop:

            reason = (
                "EMERGENCY_ATR_SL"
            )

    else:

        emergency_stop = (
            entry
            +
            position["atr"]
            *
            EMERGENCY_SL_ATR
        )

        if price >= emergency_stop:

            reason = (
                "EMERGENCY_ATR_SL"
            )

    # ========================================================
    # 2. STRUCTURE / MOMENTUM FAILURE
    # ========================================================

    if (
        reason is None
        and
        elapsed >= MIN_HOLD_SECONDS
    ):

        candle_open = D(
            x["open"]
        )

        candle_close = D(
            x["close"]
        )

        rsi_value = float(
            x["rsi"]
        )

        if side == "BUY":

            bearish_candle = (
                candle_close
                <
                candle_open
            )

            if (
                bearish_candle
                and
                rsi_value < 48
            ):

                reason = (
                    "OPPOSITE_15S_MOMENTUM"
                )

        else:

            bullish_candle = (
                candle_close
                >
                candle_open
            )

            if (
                bullish_candle
                and
                rsi_value > 52
            ):

                reason = (
                    "OPPOSITE_15S_MOMENTUM"
                )

    # ========================================================
    # 3. TRAILING PROTECTION
    # ========================================================

    if (
        reason is None
        and
        elapsed >= TRAIL_START_SECONDS
    ):

        if side == "BUY":

            new_trail = (
                price
                -
                atr15
                *
                TRAIL_ATR_MULT
            )

            if (
                price > entry
                and
                (
                    position["trail"]
                    is None
                    or
                    new_trail
                    >
                    position["trail"]
                )
            ):

                position[
                    "trail"
                ] = new_trail

            if (
                position["trail"]
                is not None
                and
                price
                <
                position["trail"]
            ):

                reason = (
                    "TRAILING_EXIT"
                )

        else:

            new_trail = (
                price
                +
                atr15
                *
                TRAIL_ATR_MULT
            )

            if (
                price < entry
                and
                (
                    position["trail"]
                    is None
                    or
                    new_trail
                    <
                    position["trail"]
                )
            ):

                position[
                    "trail"
                ] = new_trail

            if (
                position["trail"]
                is not None
                and
                price
                >
                position["trail"]
            ):

                reason = (
                    "TRAILING_EXIT"
                )

    # ========================================================
    # 4. MAX 30 SECOND EXIT
    # ========================================================

    if (
        reason is None
        and
        elapsed >= MAX_HOLD_SECONDS
    ):

        reason = (
            "TIME_EXIT_30S"
        )

    # ========================================================
    # EXECUTE EXIT
    # ========================================================

    if reason is None:

        return

    try:

        market_close(

            symbol,

            side,

            position["size"]
        )

    except Exception as e:

        print(
            "Close order error:",
            e
        )

        send_telegram(

            f"⚠️ <b>CLOSE ERROR</b>\n"
            f"{symbol}\n"
            f"{e}"
        )

        return

    pnl = estimate_pnl(
        position,
        price
    )

    reset_daily_stats()

    with state_lock:

        stats["pnl"] += pnl

        if pnl >= 0:

            stats["wins"] += 1

            stats[
                "consecutive_losses"
            ] = 0

        else:

            stats["losses"] += 1

            stats[
                "consecutive_losses"
            ] += 1

        trade_history.append({

            "symbol":
                symbol,

            "side":
                side,

            "entry":
                str(entry),

            "exit":
                str(price),

            "pnl":
                float(pnl),

            "reason":
                reason,

            "time":
                current_time()
        })

        positions.pop(
            symbol,
            None
        )

        cooldowns[
            symbol
        ] = time.time()

    telegram_exit(

        symbol,

        position,

        reason,

        price,

        pnl
    )


# ============================================================
# PROCESS NEW 15s CANDLE
# ============================================================

def process_15s_candle(
    symbol
):

    # First manage existing trade
    if symbol in positions:

        manage_position(
            symbol
        )

        return

    if not trading_allowed():

        return

    if (
        symbol in cooldowns
        and
        (
            time.time()
            -
            cooldowns[symbol]
        )
        <
        COOLDOWN_SECONDS
    ):

        return

    signal = find_entry(
        symbol
    )

    structure = None

    if symbol in structure_data:

        structure = analyze_structure(
            structure_data[symbol]
        )

    last_status[
        symbol
    ] = {

        "time":
            current_time(),

        "signal":
            (
                signal["side"]
                if signal
                else
                "NONE"
            ),

        "structure":
            (
                structure["bias"]
                if structure
                else
                "NONE"
            ),

        "level":
            (
                str(structure["level"])
                if structure
                and
                structure["level"]
                else
                None
            )
    }

    if not signal:

        return

    try:

        price = D(
            signal["entry"]
        )

        size = calculate_size(
            symbol,
            price
        )

        order = market_entry(

            symbol,

            signal["side"],

            size
        )

        instrument = get_instrument(
            symbol
        )

        positions[
            symbol
        ] = {

            "side":
                signal["side"],

            "entry":
                price,

            "size":
                size,

            "ctVal":
                instrument["ctVal"],

            "atr":
                signal["atr"],

            "entry_time":
                time.time(),

            "trail":
                None,

            "ordId":
                order.get(
                    "ordId"
                )
        }

        telegram_entry(

            symbol,

            signal,

            size
        )

        print(
            f"ENTRY {symbol} "
            f"{signal['side']} "
            f"@ {price}"
        )

    except Exception as e:

        print(
            "Entry error:",
            e
        )

        send_telegram(

            f"⚠️ <b>ENTRY ERROR</b>\n"
            f"{symbol}\n"
            f"{e}"
        )


# ============================================================
# WEBSOCKET
# ============================================================

def ws_on_message(
    ws,
    message
):

    try:

        data = json.loads(
            message
        )

        if data.get(
            "event"
        ) == "subscribe":

            return

        arg = data.get(
            "arg",
            {}
        )

        if (
            arg.get("channel")
            !=
            "candle1s"
        ):

            return

        symbol = arg.get(
            "instId"
        )

        if symbol not in SYMBOLS:

            return

        for x in data.get(
            "data",
            []
        ):

            if len(x) < 9:

                continue

            # Confirmed 1-second candle only
            if x[8] != "1":

                continue

            ts = int(
                x[0]
            )

            candle = add_1s_data(

                symbol,

                ts,

                float(x[1]),

                float(x[2]),

                float(x[3]),

                float(x[4]),

                float(x[5])
            )

            if candle:

                process_15s_candle(
                    symbol
                )

    except Exception as e:

        print(
            "WS message error:",
            e
        )


def ws_on_open(
    ws
):

    global ws_connected

    ws_connected = True

    subscriptions = [

        {
            "channel":
                "candle1s",

            "instId":
                symbol
        }

        for symbol in SYMBOLS
    ]

    ws.send(
        json.dumps({

            "op":
                "subscribe",

            "args":
                subscriptions
        })
    )

    print(
        "WebSocket connected"
    )

    print(
        "Subscribed:",
        SYMBOLS
    )


def ws_on_error(
    ws,
    error
):

    global ws_connected

    ws_connected = False

    print(
        "WebSocket error:",
        error
    )


def ws_on_close(
    ws,
    code,
    message
):

    global ws_connected

    ws_connected = False

    print(
        "WebSocket closed:",
        code,
        message
    )


def websocket_loop():

    global ws_connected

    while True:

        try:

            socket = websocket.WebSocketApp(

                OKX_WS_PUBLIC,

                on_open=
                    ws_on_open,

                on_message=
                    ws_on_message,

                on_error=
                    ws_on_error,

                on_close=
                    ws_on_close
            )

            socket.run_forever(

                ping_interval=20,

                ping_timeout=10
            )

        except Exception as e:

            print(
                "WebSocket loop error:",
                e
            )

        ws_connected = False

        time.sleep(3)


# ============================================================
# 5M STRUCTURE UPDATE LOOP
# ============================================================

def structure_loop():

    while True:

        for symbol in SYMBOLS:

            try:

                df = get_5m_candles(
                    symbol
                )

                if df is not None:

                    structure_data[
                        symbol
                    ] = df

            except Exception as e:

                print(
                    "5m update error",
                    symbol,
                    e
                )

        time.sleep(10)


# ============================================================
# FLASK DASHBOARD
# ============================================================

@app.route("/")
def home():

    return """

    <html>

    <head>

    <title>
    ICT SwiftEdge 15s Scalper V2
    </title>

    </head>

    <body
    style="
    font-family:Arial;
    max-width:900px;
    margin:30px auto;
    "
    >

    <h2>
    ICT SwiftEdge Scalper V2
    </h2>

    <h3>
    5m Structure + 15s Execution
    </h3>

    <p>
    <a href="/health">
    Health
    </a>
    </p>

    <p>
    <a href="/api/status">
    Bot Status
    </a>
    </p>

    </body>

    </html>

    """


@app.route("/health")
def health():

    return jsonify({

        "status":
            "running",

        "websocket":
            ws_connected,

        "demo":
            OKX_DEMO,

        "auto_trade":
            AUTO_TRADE,

        "live_allowed":
            ALLOW_LIVE,

        "strategy":
            "5m structure + 15s scalping"
    })


@app.route("/api/status")
def status():

    reset_daily_stats()

    with state_lock:

        total = (
            stats["wins"]
            +
            stats["losses"]
        )

        win_rate = (
            stats["wins"]
            /
            total
            *
            100
            if total
            else
            0
        )

        return jsonify({

            "strategy":
                "ICT SwiftEdge V2",

            "entry":
                "15 seconds",

            "structure":
                "5 minutes",

            "demo":
                OKX_DEMO,

            "auto_trade":
                AUTO_TRADE,

            "live_allowed":
                ALLOW_LIVE,

            "websocket":
                ws_connected,

            "symbols":
                SYMBOLS,

            "positions": {

                symbol: {

                    "side":
                        position["side"],

                    "entry":
                        str(position["entry"]),

                    "size":
                        str(position["size"]),

                    "age_seconds":
                        round(
                            time.time()
                            -
                            position["entry_time"],
                            2
                        )
                }

                for symbol, position
                in positions.items()
            },

            "statistics": {

                "wins":
                    stats["wins"],

                "losses":
                    stats["losses"],

                "win_rate":
                    round(
                        win_rate,
                        2
                    ),

                "pnl_estimate":
                    float(
                        stats["pnl"]
                    ),

                "consecutive_losses":
                    stats[
                        "consecutive_losses"
                    ]
            },

            "last_status":
                last_status,

            "recent_trades":
                list(
                    trade_history
                )[-20:]
        })


# ============================================================
# CONFIG VALIDATION
# ============================================================

def validate():

    if not OKX_DEMO:

        if not ALLOW_LIVE:

            raise RuntimeError(

                "Live mode blocked. "
                "Set ALLOW_LIVE=true "
                "if you really want live trading."
            )

    if AUTO_TRADE:

        if not OKX_API_KEY:

            raise RuntimeError(
                "OKX_API_KEY missing"
            )

        if not OKX_SECRET_KEY:

            raise RuntimeError(
                "OKX_SECRET_KEY missing"
            )

        if not OKX_PASSPHRASE:

            raise RuntimeError(
                "OKX_PASSPHRASE missing"
            )

    if MARGIN_USDT <= 0:

        raise RuntimeError(
            "MARGIN_USDT must be > 0"
        )

    if LEVERAGE <= 0:

        raise RuntimeError(
            "LEVERAGE must be > 0"
        )

    if (
        MAX_HOLD_SECONDS
        <
        MIN_HOLD_SECONDS
    ):

        raise RuntimeError(
            "MAX_HOLD_SECONDS "
            "must be >= "
            "MIN_HOLD_SECONDS"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    validate()

    print("=" * 60)

    print(
        "ICT SWIFTEDGE SCALPER V2"
    )

    print(
        "5m STRUCTURE + 15s EXECUTION"
    )

    print(
        "Demo:",
        OKX_DEMO
    )

    print(
        "Auto Trade:",
        AUTO_TRADE
    )

    print(
        "Symbols:",
        SYMBOLS
    )

    print(
        "Margin:",
        MARGIN_USDT
    )

    print(
        "Leverage:",
        LEVERAGE
    )

    print(
        "Max Hold:",
        MAX_HOLD_SECONDS,
        "seconds"
    )

    print("=" * 60)

    threading.Thread(
        target=structure_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=websocket_loop,
        daemon=True
    ).start()

    app.run(

        host="0.0.0.0",

        port=PORT,

        threaded=True
    )


if __name__ == "__main__":

    main()
