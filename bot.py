import os
import time
import json
import hmac
import base64
import hashlib
import threading
import uuid

from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, Response
from dotenv import load_dotenv


load_dotenv()


# =========================================================
# SETTINGS
# =========================================================

BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://www.okx.com"
).rstrip("/")

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

DEMO = os.getenv(
    "OKX_DEMO",
    "true"
).lower() == "true"

AUTO_TRADE = os.getenv(
    "AUTO_TRADE",
    "true"
).lower() == "true"

BAR = os.getenv("BAR", "5m")
TREND_BAR = os.getenv("TREND_BAR", "15m")

MARGIN_USDT = Decimal(
    os.getenv("MARGIN_USDT", "20")
)

LEVERAGE = Decimal(
    os.getenv("LEVERAGE", "5")
)

# Initial protection
SL_PERCENT = Decimal(
    os.getenv("SL_PERCENT", "0.4")
)

TP_PERCENT = Decimal(
    os.getenv("TP_PERCENT", "0.8")
)

# =========================================================
# DYNAMIC POSITION MANAGEMENT
# =========================================================

DYNAMIC_SLTP = os.getenv(
    "DYNAMIC_SLTP",
    "true"
).lower() == "true"

# When profit reaches this percentage,
# SL moves to break-even.
BE_TRIGGER_PERCENT = Decimal(
    os.getenv("BE_TRIGGER_PERCENT", "0.40")
)

# Small amount above/below entry after BE.
# Example 0.03 = +0.03% for long.
BE_OFFSET_PERCENT = Decimal(
    os.getenv("BE_OFFSET_PERCENT", "0.03")
)

# Trailing starts after this profit.
TRAIL_TRIGGER_PERCENT = Decimal(
    os.getenv("TRAIL_TRIGGER_PERCENT", "0.60")
)

# Distance between current/peak price and SL.
TRAIL_DISTANCE_PERCENT = Decimal(
    os.getenv("TRAIL_DISTANCE_PERCENT", "0.35")
)

# TP will be extended after trailing starts.
TP_EXTENSION_PERCENT = Decimal(
    os.getenv("TP_EXTENSION_PERCENT", "0.60")
)

# Do not amend the same SL/TP too frequently.
SLTP_UPDATE_MIN_SECONDS = int(
    os.getenv(
        "SLTP_UPDATE_MIN_SECONDS",
        "20"
    )
)

# Emergency protection if position has no TP/SL.
EMERGENCY_PROTECTION = os.getenv(
    "EMERGENCY_PROTECTION",
    "true"
).lower() == "true"


POLL_SECONDS = int(
    os.getenv("POLL_SECONDS", "20")
)

MIN_SCORE = int(
    os.getenv("MIN_SCORE", "7")
)

ADX_MIN = Decimal(
    os.getenv("ADX_MIN", "18")
)

VOLUME_MULT = Decimal(
    os.getenv("VOLUME_MULT", "0.8")
)

ATR_MIN_PCT = Decimal(
    os.getenv("ATR_MIN_PCT", "0.05")
)

TD_MODE = os.getenv(
    "TD_MODE",
    "cross"
)


# =========================================================
# SYMBOLS
# =========================================================

SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,"
        "ETH-USDT-SWAP,"
        "XRP-USDT-SWAP,"
        "DOGE-USDT-SWAP,"
        "SOL-USDT-SWAP,"
        "SHIB-USDT-SWAP,"
        "XAU-USDT-SWAP"
    ).split(",")
    if x.strip()
]


# =========================================================
# APP
# =========================================================

app = Flask(__name__)

session = requests.Session()

state = {}
state_lock = threading.Lock()
order_lock = threading.Lock()

server_offset_ms = 0
worker_started = False


# =========================================================
# DYNAMIC MEMORY
# =========================================================

position_memory = {}

position_memory_lock = threading.Lock()


# =========================================================
# LOGGING
# =========================================================

def log(message):

    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True
    )


# =========================================================
# DECIMAL HELPERS
# =========================================================

def dec(value):

    return Decimal(str(value))


def fmt(value, places=8):

    if value is None:
        return "-"

    return (
        f"{value:.{places}f}"
        .rstrip("0")
        .rstrip(".")
    )


def floor_step(value, step):

    if step <= 0:
        return value

    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step


# =========================================================
# PUBLIC REQUEST
# =========================================================

def public_get(path, params=None, raw=False):

    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if raw:
        return data

    if data.get("code") != "0":

        raise RuntimeError(
            "OKX PUBLIC ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# SERVER TIME
# =========================================================

def sync_okx_time():

    global server_offset_ms

    local_before = int(
        time.time() * 1000
    )

    data = public_get(
        "/api/v5/public/time",
        raw=True
    )

    local_after = int(
        time.time() * 1000
    )

    server_ms = int(
        data["data"][0]["ts"]
    )

    local_mid = (
        local_before
        + local_after
    ) // 2

    server_offset_ms = (
        server_ms
        - local_mid
    )

    log(
        "OKX TIME SYNCED | "
        f"offset_ms={server_offset_ms}"
    )


def utc_timestamp():

    current_ms = (
        int(time.time() * 1000)
        + server_offset_ms
    )

    dt = datetime.fromtimestamp(
        current_ms / 1000,
        tz=timezone.utc
    )

    return (
        dt.isoformat(
            timespec="milliseconds"
        )
        .replace(
            "+00:00",
            "Z"
        )
    )


# =========================================================
# SIGNATURE
# =========================================================

def create_signature(
    timestamp,
    method,
    request_path,
    body=""
):

    prehash = (
        timestamp
        + method.upper()
        + request_path
        + body
    )

    digest = hmac.new(
        SECRET_KEY.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256
    ).digest()

    return base64.b64encode(
        digest
    ).decode()


# =========================================================
# PRIVATE REQUEST
# =========================================================

def private_request(
    method,
    path,
    payload=None,
    params=None
):

    if not API_KEY:
        raise RuntimeError(
            "OKX_API_KEY missing"
        )

    if not SECRET_KEY:
        raise RuntimeError(
            "OKX_SECRET_KEY missing"
        )

    if not PASSPHRASE:
        raise RuntimeError(
            "OKX_PASSPHRASE missing"
        )

    method = method.upper()

    request_path = path

    if params:

        query = urlencode(
            [
                (
                    str(key),
                    str(value)
                )
                for key, value
                in params.items()
            ]
        )

        request_path += "?" + query

    body = ""

    if payload is not None:

        body = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False
        )

    timestamp = utc_timestamp()

    signature = create_signature(
        timestamp,
        method,
        request_path,
        body
    )

    headers = {

        "Content-Type":
            "application/json",

        "OK-ACCESS-KEY":
            API_KEY,

        "OK-ACCESS-SIGN":
            signature,

        "OK-ACCESS-PASSPHRASE":
            PASSPHRASE,

        "OK-ACCESS-TIMESTAMP":
            timestamp
    }

    if DEMO:

        headers[
            "x-simulated-trading"
        ] = "1"

    response = session.request(
        method,
        BASE_URL + path,
        headers=headers,
        data=(
            body
            if body
            else None
        ),
        params=params,
        timeout=15
    )

    try:

        data = response.json()

    except Exception:

        data = {
            "raw": response.text
        }

    if response.status_code >= 400:

        raise RuntimeError(
            f"OKX HTTP "
            f"{response.status_code}: "
            f"{data}"
        )

    if data.get("code") != "0":

        raise RuntimeError(
            "OKX PRIVATE ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# MARKET DATA
# =========================================================

def get_candles(
    symbol,
    bar,
    limit=160
):

    data = public_get(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": bar,
            "limit": str(limit)
        }
    )

    candles = []

    for row in reversed(
        data.get("data", [])
    ):

        candles.append({

            "ts":
                int(row[0]),

            "open":
                dec(row[1]),

            "high":
                dec(row[2]),

            "low":
                dec(row[3]),

            "close":
                dec(row[4]),

            "volume":
                dec(row[5]),

            "confirm":
                (
                    row[8]
                    if len(row) > 8
                    else "1"
                )
        })

    return candles


def get_last_price(symbol):

    data = public_get(
        "/api/v5/market/ticker",
        {
            "instId": symbol
        }
    )

    rows = data.get("data", [])

    if not rows:

        raise RuntimeError(
            "Ticker unavailable: "
            + symbol
        )

    return dec(
        rows[0]["last"]
    )


# =========================================================
# EMA
# =========================================================

def calculate_ema(values, period):

    if len(values) < period:

        return [None] * len(values)

    result = [None] * len(values)

    value = (
        sum(
            values[:period],
            Decimal("0")
        )
        /
        Decimal(period)
    )

    result[period - 1] = value

    multiplier = (
        Decimal("2")
        /
        Decimal(period + 1)
    )

    for i in range(
        period,
        len(values)
    ):

        value = (
            values[i] * multiplier
            +
            value
            * (
                Decimal("1")
                - multiplier
            )
        )

        result[i] = value

    return result


# =========================================================
# RSI
# =========================================================

def calculate_rsi(values, period):

    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(
        1,
        len(values)
    ):

        change = (
            values[i]
            -
            values[i - 1]
        )

        gains.append(
            max(
                change,
                Decimal("0")
            )
        )

        losses.append(
            max(
                -change,
                Decimal("0")
            )
        )

    avg_gain = (
        sum(
            gains[:period],
            Decimal("0")
        )
        /
        Decimal(period)
    )

    avg_loss = (
        sum(
            losses[:period],
            Decimal("0")
        )
        /
        Decimal(period)
    )

    def rsi_value(
        gain,
        loss
    ):

        if loss == 0:
            return Decimal("100")

        rs = gain / loss

        return (
            Decimal("100")
            -
            Decimal("100")
            /
            (
                Decimal("1")
                + rs
            )
        )

    result[period] = rsi_value(
        avg_gain,
        avg_loss
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            avg_gain * (period - 1)
            +
            gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss * (period - 1)
            +
            losses[i]
        ) / Decimal(period)

        result[i + 1] = rsi_value(
            avg_gain,
            avg_loss
        )

    return result


# =========================================================
# ATR
# =========================================================

def calculate_atr(
    candles,
    period=14
):

    if len(candles) <= period:
        return None

    trs = []

    for i in range(
        1,
        len(candles)
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

        previous_close = (
            candles[i - 1]["close"]
        )

        tr = max(

            high - low,

            abs(
                high
                - previous_close
            ),

            abs(
                low
                - previous_close
            )
        )

        trs.append(tr)

    return (
        sum(
            trs[-period:],
            Decimal("0")
        )
        /
        Decimal(period)
    )


# =========================================================
# ADX
# =========================================================

def calculate_adx(
    candles,
    period=14
):

    if len(candles) < period + 2:

        return Decimal("0")

    plus = Decimal("0")
    minus = Decimal("0")
    total_range = Decimal("0")

    start = max(
        1,
        len(candles) - period
    )

    for i in range(
        start,
        len(candles)
    ):

        up_move = (
            candles[i]["high"]
            -
            candles[i - 1]["high"]
        )

        down_move = (
            candles[i - 1]["low"]
            -
            candles[i]["low"]
        )

        if (
            up_move > down_move
            and
            up_move > 0
        ):

            plus += up_move

        if (
            down_move > up_move
            and
            down_move > 0
        ):

            minus += down_move

        total_range += (
            candles[i]["high"]
            -
            candles[i]["low"]
        )

    if total_range == 0:

        return Decimal("0")

    plus_di = (
        plus
        /
        total_range
        *
        Decimal("100")
    )

    minus_di = (
        minus
        /
        total_range
        *
        Decimal("100")
    )

    total = plus_di + minus_di

    if total == 0:

        return Decimal("0")

    return (
        abs(
            plus_di
            -
            minus_di
        )
        /
        total
        *
        Decimal("100")
    )


# =========================================================
# 15M TREND
# =========================================================

def get_trend(symbol):

    candles = get_candles(
        symbol,
        TREND_BAR,
        80
    )

    candles = [
        x for x in candles
        if x["confirm"] == "1"
    ]

    if len(candles) < 22:

        return "flat"

    closes = [
        x["close"]
        for x in candles
    ]

    ema20 = calculate_ema(
        closes,
        20
    )

    i = len(closes) - 1

    if (
        ema20[i] is None
        or
        ema20[i - 1] is None
    ):

        return "flat"

    if (
        closes[i] > ema20[i]
        and
        ema20[i] > ema20[i - 1]
    ):

        return "bull"

    if (
        closes[i] < ema20[i]
        and
        ema20[i] < ema20[i - 1]
    ):

        return "bear"

    return "flat"


# =========================================================
# ANALYSIS
# =========================================================

def analyze_symbol(symbol):

    candles = get_candles(
        symbol,
        BAR,
        160
    )

    candles = [
        x for x in candles
        if x["confirm"] == "1"
    ]

    if len(candles) < 105:

        return {
            "signal": "NONE",
            "score": 0,
            "reason": "Not enough candles"
        }

    closes = [
        x["close"]
        for x in candles
    ]

    i = len(candles) - 1

    rsi14 = calculate_rsi(
        closes,
        14
    )

    rsi100 = calculate_rsi(
        closes,
        100
    )

    ema20 = calculate_ema(
        closes,
        20
    )

    atr = calculate_atr(
        candles,
        14
    )

    adx = calculate_adx(
        candles,
        14
    )

    trend15 = get_trend(symbol)

    if (
        rsi14[i] is None
        or
        rsi100[i] is None
        or
        ema20[i] is None
        or
        atr is None
    ):

        return {
            "signal": "NONE",
            "score": 0,
            "reason": "Indicator unavailable"
        }

    average_volume = (
        sum(
            x["volume"]
            for x in candles[-21:-1]
        )
        /
        Decimal("20")
    )

    volume_ratio = (

        candles[i]["volume"]
        /
        average_volume

        if average_volume
        else Decimal("0")
    )

    atr_percent = (
        atr
        /
        closes[i]
        *
        Decimal("100")
    )

    buy_score = 0
    sell_score = 0

    reasons = []

    if rsi14[i] > rsi100[i]:

        buy_score += 2

        reasons.append(
            "RSI bullish"
        )

    elif rsi14[i] < rsi100[i]:

        sell_score += 2

        reasons.append(
            "RSI bearish"
        )

    if (
        rsi14[i - 1]
        <= rsi100[i - 1]
        and
        rsi14[i]
        > rsi100[i]
    ):

        buy_score += 1

        reasons.append(
            "RSI bullish crossover"
        )

    elif (
        rsi14[i - 1]
        >= rsi100[i - 1]
        and
        rsi14[i]
        < rsi100[i]
    ):

        sell_score += 1

        reasons.append(
            "RSI bearish crossover"
        )

    if closes[i] > ema20[i]:

        buy_score += 1

        reasons.append(
            "Price above EMA20"
        )

    elif closes[i] < ema20[i]:

        sell_score += 1

        reasons.append(
            "Price below EMA20"
        )

    if ema20[i] > ema20[i - 1]:

        buy_score += 1

        reasons.append(
            "EMA slope bullish"
        )

    elif ema20[i] < ema20[i - 1]:

        sell_score += 1

        reasons.append(
            "EMA slope bearish"
        )

    near_ema = (

        abs(
            closes[i]
            -
            ema20[i]
        )
        /
        closes[i]
        *
        Decimal("100")
        <=
        Decimal("0.20")
    )

    bullish_retest = (
        near_ema
        and
        closes[i] > ema20[i]
        and
        candles[i]["low"] <= ema20[i]
    )

    bearish_retest = (
        near_ema
        and
        closes[i] < ema20[i]
        and
        candles[i]["high"] >= ema20[i]
    )

    if bullish_retest:

        buy_score += 1

        reasons.append(
            "EMA20 bullish retest"
        )

    elif bearish_retest:

        sell_score += 1

        reasons.append(
            "EMA20 bearish retest"
        )

    if adx >= ADX_MIN:

        if buy_score > sell_score:

            buy_score += 1

        elif sell_score > buy_score:

            sell_score += 1

        reasons.append(
            "ADX strength OK"
        )

    else:

        reasons.append(
            "ADX below minimum"
        )

    if volume_ratio >= VOLUME_MULT:

        if buy_score > sell_score:

            buy_score += 1

        elif sell_score > buy_score:

            sell_score += 1

        reasons.append(
            "Volume confirmed"
        )

    else:

        reasons.append(
            "Volume filter not confirmed"
        )

    if atr_percent >= ATR_MIN_PCT:

        if buy_score > sell_score:

            buy_score += 1

        elif sell_score > buy_score:

            sell_score += 1

        reasons.append(
            "ATR volatility OK"
        )

    else:

        reasons.append(
            "ATR too low"
        )

    if trend15 == "bull":

        buy_score += 1

        reasons.append(
            "15m bullish trend"
        )

    elif trend15 == "bear":

        sell_score += 1

        reasons.append(
            "15m bearish trend"
        )

    else:

        reasons.append(
            "15m trend flat"
        )

    score = max(
        buy_score,
        sell_score
    )

    signal = "NONE"

    if (
        buy_score > sell_score
        and
        buy_score >= MIN_SCORE
    ):

        signal = "BUY"

    elif (
        sell_score > buy_score
        and
        sell_score >= MIN_SCORE
    ):

        signal = "SELL"

    if (
        trend15 == "bull"
        and
        signal == "SELL"
    ):

        signal = "NONE"

        reasons.append(
            "Blocked by 15m bullish trend"
        )

    if (
        trend15 == "bear"
        and
        signal == "BUY"
    ):

        signal = "NONE"

        reasons.append(
            "Blocked by 15m bearish trend"
        )

    return {

        "signal": signal,

        "score": score,

        "buy": buy_score,

        "sell": sell_score,

        "entry": closes[i],

        "rsi14": rsi14[i],

        "rsi100": rsi100[i],

        "ema20": ema20[i],

        "adx": adx,

        "atr_pct": atr_percent,

        "volume_ratio": volume_ratio,

        "trend15": trend15,

        "reason": " | ".join(reasons)
    }


# =========================================================
# INSTRUMENT
# =========================================================

def get_instrument(symbol):

    data = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": symbol
        }
    )

    if not data.get("data"):

        raise RuntimeError(
            "Instrument not found: "
            + symbol
        )

    item = data["data"][0]

    return {

        "ctVal":
            dec(item["ctVal"]),

        "lotSz":
            dec(item["lotSz"]),

        "minSz":
            dec(item["minSz"]),

        "tickSz":
            dec(item["tickSz"]),

        "state":
            item["state"]
    }


# =========================================================
# ORDER SIZE
# =========================================================

def calculate_order_size(
    symbol,
    price
):

    info = get_instrument(symbol)

    if info["state"] != "live":

        raise RuntimeError(
            "Instrument not live: "
            +
            info["state"]
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
            info["ctVal"]
            *
            price
        )
    )

    size = floor_step(
        raw_size,
        info["lotSz"]
    )

    if size < info["minSz"]:

        raise RuntimeError(
            "Order size below minimum | "
            f"calculated={size} "
            f"minSz={info['minSz']} "
            f"lotSz={info['lotSz']} "
            f"ctVal={info['ctVal']}"
        )

    return size, info


# =========================================================
# ACCOUNT
# =========================================================

def get_positions(symbol=None):

    params = None

    if symbol:

        params = {
            "instId": symbol
        }

    return private_request(
        "GET",
        "/api/v5/account/positions",
        params=params
    )


def has_position(symbol):

    data = get_positions(symbol)

    for position in data.get(
        "data",
        []
    ):

        try:

            position_size = dec(
                position.get(
                    "pos",
                    "0"
                )
            )

        except Exception:

            position_size = Decimal("0")

        if position_size != 0:

            return True, position

    return False, None


# =========================================================
# LEVERAGE
# =========================================================

def set_leverage(symbol):

    payload = {

        "instId":
            symbol,

        "lever":
            fmt(LEVERAGE),

        "mgnMode":
            TD_MODE
    }

    return private_request(
        "POST",
        "/api/v5/account/set-leverage",
        payload=payload
    )


# =========================================================
# PENDING ALGO ORDERS
# =========================================================

def get_pending_algo_orders(symbol):

    return private_request(
        "GET",
        "/api/v5/trade/orders-algo-pending",
        params={
            "ordType": "conditional",
            "instId": symbol
        }
    )


# =========================================================
# AMEND ALGO
# =========================================================

def amend_algo(
    symbol,
    algo_id,
    sl_price=None,
    tp_price=None
):

    payload = {

        "instId":
            symbol,

        "algoId":
            str(algo_id),

        "cxlOnFail":
            False
    }

    if sl_price is not None:

        payload[
            "slTriggerPx"
        ] = fmt(sl_price)

        payload[
            "slOrdPx"
        ] = "-1"

        payload[
            "slTriggerPxType"
        ] = "mark"

    if tp_price is not None:

        payload[
            "tpTriggerPx"
        ] = fmt(tp_price)

        payload[
            "tpOrdPx"
        ] = "-1"

        payload[
            "tpTriggerPxType"
        ] = "mark"

    result = private_request(
        "POST",
        "/api/v5/trade/amend-algos",
        payload=payload
    )

    rows = result.get(
        "data",
        []
    )

    if rows:

        row = rows[0]

        if row.get("sCode") not in (
            None,
            "",
            "0"
        ):

            raise RuntimeError(
                "ALGO AMEND REJECTED | "
                f"{row.get('sCode')} | "
                f"{row.get('sMsg')}"
            )

    return result


# =========================================================
# EMERGENCY PROTECTION
# =========================================================

def place_emergency_protection(
    symbol,
    position,
    sl,
    tp
):

    pos_size = abs(
        dec(
            position.get(
                "pos",
                "0"
            )
        )
    )

    if pos_size <= 0:

        return None

    pos_side = position.get(
        "posSide",
        "net"
    )

    if pos_side == "short":

        close_side = "buy"

    else:

        close_side = "sell"

    payload = {

        "instId":
            symbol,

        "tdMode":
            TD_MODE,

        "side":
            close_side,

        "posSide":
            "net",

        "ordType":
            "conditional",

        "sz":
            fmt(pos_size),

        "tpTriggerPx":
            fmt(tp),

        "tpOrdPx":
            "-1",

        "tpTriggerPxType":
            "mark",

        "slTriggerPx":
            fmt(sl),

        "slOrdPx":
            "-1",

        "slTriggerPxType":
            "mark",

        "reduceOnly":
            True,

        "closeFraction":
            "1"
    }

    log(
        "EMERGENCY PROTECTION | "
        f"{symbol} | "
        f"SL={fmt(sl)} | "
        f"TP={fmt(tp)}"
    )

    return private_request(
        "POST",
        "/api/v5/trade/order-algo",
        payload=payload
    )


# =========================================================
# CALCULATE INITIAL SL/TP
# =========================================================

def calculate_initial_sltp(
    entry,
    side
):

    if side == "long":

        tp = (
            entry
            *
            (
                Decimal("1")
                +
                TP_PERCENT
                /
                Decimal("100")
            )
        )

        sl = (
            entry
            *
            (
                Decimal("1")
                -
                SL_PERCENT
                /
                Decimal("100")
            )
        )

    else:

        tp = (
            entry
            *
            (
                Decimal("1")
                -
                TP_PERCENT
                /
                Decimal("100")
            )
        )

        sl = (
            entry
            *
            (
                Decimal("1")
                +
                SL_PERCENT
                /
                Decimal("100")
            )
        )

    return sl, tp


# =========================================================
# DYNAMIC SL/TP CALCULATION
# =========================================================

def calculate_dynamic_targets(
    symbol,
    position,
    current_price,
    info
):

    avg_px_raw = position.get(
        "avgPx"
    )

    if not avg_px_raw:

        avg_px_raw = position.get(
            "nonSettleAvgPx"
        )

    if not avg_px_raw:

        return None

    entry = dec(avg_px_raw)

    pos_size = abs(
        dec(
            position.get(
                "pos",
                "0"
            )
        )
    )

    if pos_size <= 0:

        return None

    pos_side = position.get(
        "posSide",
        "net"
    )

    # Net mode: positive = long, negative = short.
    raw_pos = dec(
        position.get(
            "pos",
            "0"
        )
    )

    if pos_side == "short":

        side = "short"

    elif pos_side == "long":

        side = "long"

    elif raw_pos < 0:

        side = "short"

    else:

        side = "long"

    tick = info["tickSz"]

    with position_memory_lock:

        mem = position_memory.setdefault(
            symbol,
            {}
        )

        if (
            mem.get("entry")
            != entry
            or
            mem.get("side")
            != side
        ):

            mem.clear()

            mem["entry"] = entry
            mem["side"] = side
            mem["peak"] = current_price
            mem["trough"] = current_price
            mem["be_done"] = False
            mem["trail_active"] = False
            mem["last_update"] = 0
            mem["last_sl"] = None
            mem["last_tp"] = None

        if side == "long":

            if current_price > mem["peak"]:

                mem["peak"] = current_price

        else:

            if current_price < mem["trough"]:

                mem["trough"] = current_price

        if side == "long":

            profit_pct = (
                (
                    current_price
                    - entry
                )
                /
                entry
                *
                Decimal("100")
            )

        else:

            profit_pct = (
                (
                    entry
                    - current_price
                )
                /
                entry
                *
                Decimal("100")
            )

        initial_sl, initial_tp = (
            calculate_initial_sltp(
                entry,
                side
            )
        )

        new_sl = initial_sl
        new_tp = initial_tp

        # -------------------------------------------------
        # BREAK EVEN
        # -------------------------------------------------

        if profit_pct >= BE_TRIGGER_PERCENT:

            if side == "long":

                be_sl = (
                    entry
                    *
                    (
                        Decimal("1")
                        +
                        BE_OFFSET_PERCENT
                        /
                        Decimal("100")
                    )
                )

                if be_sl > new_sl:

                    new_sl = be_sl

            else:

                be_sl = (
                    entry
                    *
                    (
                        Decimal("1")
                        -
                        BE_OFFSET_PERCENT
                        /
                        Decimal("100")
                    )
                )

                if be_sl < new_sl:

                    new_sl = be_sl

            mem["be_done"] = True

        # -------------------------------------------------
        # TRAILING
        # -------------------------------------------------

        if (
            DYNAMIC_SLTP
            and
            profit_pct >= TRAIL_TRIGGER_PERCENT
        ):

            mem["trail_active"] = True

            if side == "long":

                trail_sl = (
                    mem["peak"]
                    *
                    (
                        Decimal("1")
                        -
                        TRAIL_DISTANCE_PERCENT
                        /
                        Decimal("100")
                    )
                )

                if trail_sl > new_sl:

                    new_sl = trail_sl

                dynamic_tp = (
                    current_price
                    *
                    (
                        Decimal("1")
                        +
                        TP_EXTENSION_PERCENT
                        /
                        Decimal("100")
                    )
                )

                if dynamic_tp > new_tp:

                    new_tp = dynamic_tp

            else:

                trail_sl = (
                    mem["trough"]
                    *
                    (
                        Decimal("1")
                        +
                        TRAIL_DISTANCE_PERCENT
                        /
                        Decimal("100")
                    )
                )

                if trail_sl < new_sl:

                    new_sl = trail_sl

                dynamic_tp = (
                    current_price
                    *
                    (
                        Decimal("1")
                        -
                        TP_EXTENSION_PERCENT
                        /
                        Decimal("100")
                    )
                )

                if dynamic_tp < new_tp:

                    new_tp = dynamic_tp

        # -------------------------------------------------
        # NEVER MOVE SL BACKWARDS
        # -------------------------------------------------

        old_sl = mem.get("last_sl")

        old_tp = mem.get("last_tp")

        if old_sl is not None:

            if side == "long":

                if new_sl < old_sl:

                    new_sl = old_sl

            else:

                if new_sl > old_sl:

                    new_sl = old_sl

        if old_tp is not None:

            if side == "long":

                if new_tp < old_tp:

                    new_tp = old_tp

            else:

                if new_tp > old_tp:

                    new_tp = old_tp

        new_sl = floor_step(
            new_sl,
            tick
        )

        new_tp = floor_step(
            new_tp,
            tick
        )

        # Safety geometry check.
        if side == "long":

            if new_sl >= current_price:

                new_sl = floor_step(
                    current_price
                    *
                    Decimal("0.999"),
                    tick
                )

            if new_tp <= current_price:

                new_tp = floor_step(
                    current_price
                    *
                    Decimal("1.001"),
                    tick
                )

        else:

            if new_sl <= current_price:

                new_sl = floor_step(
                    current_price
                    *
                    Decimal("1.001"),
                    tick
                )

            if new_tp >= current_price:

                new_tp = floor_step(
                    current_price
                    *
                    Decimal("0.999"),
                    tick
                )

        return {
            "entry": entry,
            "side": side,
            "profit_pct": profit_pct,
            "sl": new_sl,
            "tp": new_tp,
            "peak": mem["peak"],
            "trough": mem["trough"],
            "be_done": mem["be_done"],
            "trail_active": mem["trail_active"]
        }


# =========================================================
# MANAGE ONE OPEN POSITION
# =========================================================

def manage_position(symbol):

    exists, position = has_position(
        symbol
    )

    if not exists:

        with position_memory_lock:

            position_memory.pop(
                symbol,
                None
            )

        return

    current_price = get_last_price(
        symbol
    )

    info = get_instrument(
        symbol
    )

    targets = calculate_dynamic_targets(
        symbol,
        position,
        current_price,
        info
    )

    if not targets:

        return

    sl = targets["sl"]
    tp = targets["tp"]

    with state_lock:

        state.setdefault(
            symbol,
            {}
        )

        state[symbol][
            "position_side"
        ] = targets["side"]

        state[symbol][
            "position_entry"
        ] = fmt(
            targets["entry"]
        )

        state[symbol][
            "current_price"
        ] = fmt(
            current_price
        )

        state[symbol][
            "position_profit_pct"
        ] = fmt(
            targets["profit_pct"],
            3
        )

        state[symbol][
            "dynamic_sl"
        ] = fmt(sl)

        state[symbol][
            "dynamic_tp"
        ] = fmt(tp)

        state[symbol][
            "break_even"
        ] = targets["be_done"]

        state[symbol][
            "trailing"
        ] = targets["trail_active"]

    # -----------------------------------------------------
    # FIND EXISTING PROTECTIVE ALGO
    # -----------------------------------------------------

    data = get_pending_algo_orders(
        symbol
    )

    algos = data.get(
        "data",
        []
    )

    protective = None

    for algo in algos:

        if algo.get("state") != "live":

            continue

        if algo.get("ordType") != "conditional":

            continue

        algo_id = algo.get("algoId")

        if algo_id:

            protective = algo

            break

    # -----------------------------------------------------
    # NO PROTECTION = EMERGENCY PROTECTION
    # -----------------------------------------------------

    if protective is None:

        if EMERGENCY_PROTECTION:

            try:

                result = (
                    place_emergency_protection(
                        symbol,
                        position,
                        sl,
                        tp
                    )
                )

                log(
                    "PROTECTION RESTORED | "
                    f"{symbol} | "
                    f"SL={fmt(sl)} | "
                    f"TP={fmt(tp)}"
                )

                with state_lock:

                    state[symbol][
                        "trade_status"
                    ] = "PROTECTION RESTORED"

            except Exception as error:

                log(
                    "CRITICAL PROTECTION ERROR | "
                    f"{symbol} | "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                with state_lock:

                    state[symbol][
                        "trade_status"
                    ] = (
                        "CRITICAL: NO SL/TP"
                    )

        return

    # -----------------------------------------------------
    # EXISTING PROTECTION
    # -----------------------------------------------------

    algo_id = protective.get(
        "algoId"
    )

    old_sl_raw = protective.get(
        "slTriggerPx"
    )

    old_tp_raw = protective.get(
        "tpTriggerPx"
    )

    old_sl = (
        dec(old_sl_raw)
        if old_sl_raw
        else None
    )

    old_tp = (
        dec(old_tp_raw)
        if old_tp_raw
        else None
    )

    # -----------------------------------------------------
    # ONLY IMPROVE PROTECTION
    # -----------------------------------------------------

    amend_sl = False
    amend_tp = False

    if old_sl is None:

        amend_sl = True

    elif targets["side"] == "long":

        if sl > old_sl:

            amend_sl = True

    else:

        if sl < old_sl:

            amend_sl = True

    if old_tp is None:

        amend_tp = True

    elif targets["side"] == "long":

        if tp > old_tp:

            amend_tp = True

    else:

        if tp < old_tp:

            amend_tp = True

    if not amend_sl and not amend_tp:

        return

    # -----------------------------------------------------
    # RATE LIMIT / UPDATE THROTTLE
    # -----------------------------------------------------

    now = time.time()

    with position_memory_lock:

        mem = position_memory.get(
            symbol,
            {}
        )

        last_update = mem.get(
            "last_update",
            0
        )

    if (
        now - last_update
        <
        SLTP_UPDATE_MIN_SECONDS
    ):

        return

    # -----------------------------------------------------
    # AMEND
    # -----------------------------------------------------

    try:

        result = amend_algo(
            symbol,
            algo_id,
            sl if amend_sl else None,
            tp if amend_tp else None
        )

        with position_memory_lock:

            mem = position_memory.setdefault(
                symbol,
                {}
            )

            mem["last_update"] = now

            if amend_sl:

                mem["last_sl"] = sl

            if amend_tp:

                mem["last_tp"] = tp

        log(
            "DYNAMIC SL/TP UPDATED | "
            f"{symbol} | "
            f"side={targets['side']} | "
            f"profit={fmt(targets['profit_pct'], 3)}% | "
            f"SL={fmt(sl)} | "
            f"TP={fmt(tp)} | "
            f"BE={targets['be_done']} | "
            f"TRAIL={targets['trail_active']}"
        )

        with state_lock:

            state[symbol][
                "trade_status"
            ] = "DYNAMIC SL/TP ACTIVE"

            state[symbol][
                "trade_result"
            ] = result

    except Exception as error:

        log(
            "DYNAMIC SL/TP ERROR | "
            f"{symbol} | "
            f"{type(error).__name__}: "
            f"{error}"
        )

        with state_lock:

            state[symbol][
                "trade_status"
            ] = "SL/TP UPDATE ERROR"

            state[symbol][
                "trade_error"
            ] = str(error)


# =========================================================
# PLACE NEW ORDER
# =========================================================

def place_order(
    symbol,
    analysis
):

    if not AUTO_TRADE:

        return {
            "status": "BLOCKED",
            "reason": "AUTO_TRADE=false"
        }

    if not DEMO:

        return {
            "status": "BLOCKED",
            "reason": "Live trading disabled"
        }

    if analysis["signal"] not in (
        "BUY",
        "SELL"
    ):

        return {
            "status": "NO_TRADE",
            "reason": "Signal below threshold"
        }

    with order_lock:

        exists, position = has_position(
            symbol
        )

        if exists:

            return {
                "status": "BLOCKED",
                "reason": "Existing position"
            }

        price = analysis["entry"]

        size, info = (
            calculate_order_size(
                symbol,
                price
            )
        )

        set_leverage(symbol)

        side = (
            "buy"
            if analysis["signal"] == "BUY"
            else "sell"
        )

        position_side = (
            "long"
            if side == "buy"
            else "short"
        )

        sl, tp = (
            calculate_initial_sltp(
                price,
                position_side
            )
        )

        tick = info["tickSz"]

        sl = floor_step(
            sl,
            tick
        )

        tp = floor_step(
            tp,
            tick
        )

        payload = {

            "instId":
                symbol,

            "tdMode":
                TD_MODE,

            "side":
                side,

            "ordType":
                "market",

            "sz":
                fmt(size),

            "clOrdId":
                "bot"
                +
                uuid.uuid4()
                .hex[:24],

            "attachAlgoOrds":
                [
                    {

                        "tpTriggerPx":
                            fmt(tp),

                        "tpOrdPx":
                            "-1",

                        "tpTriggerPxType":
                            "mark",

                        "slTriggerPx":
                            fmt(sl),

                        "slOrdPx":
                            "-1",

                        "slTriggerPxType":
                            "mark"
                    }
                ]
        }

        log(
            "ORDER SUBMIT | "
            f"{symbol} | "
            f"{side.upper()} | "
            f"size={fmt(size)} | "
            f"SL={fmt(sl)} | "
            f"TP={fmt(tp)}"
        )

        result = private_request(
            "POST",
            "/api/v5/trade/order",
            payload=payload
        )

        rows = result.get(
            "data",
            []
        )

        row = (
            rows[0]
            if rows
            else {}
        )

        if row.get("sCode") not in (
            None,
            "0"
        ):

            raise RuntimeError(
                "ORDER REJECTED | "
                f"sCode={row.get('sCode')} | "
                f"sMsg={row.get('sMsg')}"
            )

        return {

            "status":
                "ORDER_SENT",

            "symbol":
                symbol,

            "side":
                side,

            "size":
                fmt(size),

            "sl":
                fmt(sl),

            "tp":
                fmt(tp),

            "ordId":
                row.get(
                    "ordId",
                    ""
                ),

            "result":
                result
        }


# =========================================================
# STARTUP CHECKS
# =========================================================

def startup_checks():

    log(
        "===================================================="
    )

    log(
        "OKX SCALPING BOT V6"
    )

    log(
        "DEMO + AUTO TRADE + DYNAMIC SL/TP + BREAK-EVEN"
    )

    log(
        "TRAILING SL + DYNAMIC TP + DASHBOARD"
    )

    log(
        f"DEMO={DEMO}"
    )

    log(
        f"AUTO_TRADE={AUTO_TRADE}"
    )

    log(
        f"MARGIN=${MARGIN_USDT}"
    )

    log(
        f"LEVERAGE={LEVERAGE}x"
    )

    log(
        f"INITIAL SL={SL_PERCENT}%"
    )

    log(
        f"INITIAL TP={TP_PERCENT}%"
    )

    log(
        f"BREAK-EVEN TRIGGER={BE_TRIGGER_PERCENT}%"
    )

    log(
        f"TRAIL TRIGGER={TRAIL_TRIGGER_PERCENT}%"
    )

    log(
        f"TRAIL DISTANCE={TRAIL_DISTANCE_PERCENT}%"
    )

    log(
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "===================================================="
    )

    try:

        sync_okx_time()

    except Exception as error:

        log(
            "TIME SYNC WARNING | "
            f"{type(error).__name__}: "
            f"{error}"
        )

    try:

        ticker = public_get(
            "/api/v5/market/ticker",
            {
                "instId":
                    "BTC-USDT-SWAP"
            }
        )

        price = (
            ticker["data"][0].get(
                "last",
                "-"
            )
        )

        log(
            "OKX MARKET CONNECTED | "
            f"BTC={price}"
        )

        with state_lock:

            state["public_api"] = (
                "CONNECTED"
            )

    except Exception as error:

        log(
            "PUBLIC API ERROR | "
            f"{type(error).__name__}: "
            f"{error}"
        )

        with state_lock:

            state["public_api"] = "ERROR"

    try:

        private_request(
            "GET",
            "/api/v5/account/balance"
        )

        log(
            "OKX PRIVATE API CONNECTED"
        )

        with state_lock:

            state["private_api"] = (
                "CONNECTED"
            )

    except Exception as error:

        log(
            "PRIVATE API ERROR | "
            f"{type(error).__name__}: "
            f"{error}"
        )

        with state_lock:

            state["private_api"] = (
                "ERROR: "
                + str(error)
            )

    # -----------------------------------------------------
    # VERIFY ALL SYMBOLS BEFORE TRADING
    # -----------------------------------------------------

    for symbol in SYMBOLS:

        try:

            info = get_instrument(symbol)

            log(
                "INSTRUMENT OK | "
                f"{symbol} | "
                f"state={info['state']} | "
                f"ctVal={info['ctVal']} | "
                f"lotSz={info['lotSz']} | "
                f"minSz={info['minSz']} | "
                f"tickSz={info['tickSz']}"
            )

        except Exception as error:

            log(
                "INSTRUMENT ERROR | "
                f"{symbol} | "
                f"{type(error).__name__}: "
                f"{error}"
            )


# =========================================================
# WORKER
# =========================================================

def worker():

    global worker_started

    worker_started = True

    startup_checks()

    while True:

        # -------------------------------------------------
        # FIRST: MANAGE EXISTING POSITIONS
        # -------------------------------------------------

        for symbol in SYMBOLS:

            try:

                manage_position(
                    symbol
                )

            except Exception as error:

                log(
                    "POSITION MANAGER ERROR | "
                    f"{symbol} | "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

        # -------------------------------------------------
        # SECOND: SEARCH FOR NEW TRADES
        # -------------------------------------------------

        for symbol in SYMBOLS:

            try:

                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

                    state[symbol][
                        "last_activity"
                    ] = (
                        "CHECKING "
                        + symbol
                    )

                analysis = analyze_symbol(
                    symbol
                )

                with state_lock:

                    state[symbol].update(
                        analysis
                    )

                    state[symbol][
                        "last_checked"
                    ] = (
                        datetime.now()
                        .strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    )

                log(
                    f"{symbol}: "
                    f"{analysis['signal']} "
                    f"{analysis.get('score', 0)}/10 | "
                    f"BUY={analysis.get('buy', 0)} "
                    f"SELL={analysis.get('sell', 0)} | "
                    f"{analysis.get('reason', '')}"
                )

                if (
                    analysis["signal"]
                    in (
                        "BUY",
                        "SELL"
                    )
                    and
                    analysis["score"]
                    >= MIN_SCORE
                ):

                    try:

                        result = place_order(
                            symbol,
                            analysis
                        )

                        log(
                            "TRADE RESULT | "
                            f"{symbol} | "
                            +
                            json.dumps(
                                result,
                                default=str
                            )
                        )

                        with state_lock:

                            state[symbol][
                                "trade_status"
                            ] = result.get(
                                "status",
                                "UNKNOWN"
                            )

                            state[symbol][
                                "trade_result"
                            ] = result

                    except Exception as error:

                        log(
                            "TRADE ERROR | "
                            f"{symbol} | "
                            f"{type(error).__name__}: "
                            f"{error}"
                        )

                        with state_lock:

                            state[symbol][
                                "trade_status"
                            ] = "ERROR"

                            state[symbol][
                                "trade_error"
                            ] = str(error)

                else:

                    with state_lock:

                        state[symbol][
                            "trade_status"
                        ] = "NO TRADE"

            except Exception as error:

                log(
                    f"{symbol} ANALYSIS ERROR | "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

                    state[symbol][
                        "trade_status"
                    ] = "ANALYSIS ERROR"

                    state[symbol][
                        "trade_error"
                    ] = str(error)

        time.sleep(
            POLL_SECONDS
        )


# =========================================================
# DASHBOARD HTML
# =========================================================

HTML = r"""
<!doctype html>

<html>

<head>

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
OKX Scalping Bot V6
</title>

<style>

body {
    font-family: Arial, sans-serif;
    background: #101216;
    color: #eeeeee;
    margin: 0;
    padding: 14px;
}

h2 {
    margin: 0 0 12px;
}

.top {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

.card {
    background: #1a1e24;
    border: 1px solid #303640;
    border-radius: 10px;
    padding: 10px;
}

.ok {
    color: #43d17a;
}

.bad {
    color: #ff6262;
}

.warn {
    color: #ffc857;
}

table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 12px;
    font-size: 12px;
}

th,
td {
    padding: 7px;
    border-bottom: 1px solid #303640;
    text-align: left;
    white-space: nowrap;
}

th {
    background: #191d23;
    position: sticky;
    top: 0;
}

.buy {
    color: #43d17a;
    font-weight: bold;
}

.sell {
    color: #ff6262;
    font-weight: bold;
}

.none {
    color: #aaaaaa;
}

.reason {
    white-space: normal;
    min-width: 300px;
}

.wrap {
    overflow: auto;
}

.small {
    font-size: 12px;
    color: #aaaaaa;
    margin-top: 10px;
}

</style>

</head>

<body>

<h2>
OKX Scalping Bot V6
</h2>

<div id="top" class="top">
</div>

<div id="activity" class="small">
Loading...
</div>

<div class="wrap">

<table>

<thead>

<tr>

<th>Pair</th>
<th>Signal</th>
<th>Score</th>
<th>Entry</th>
<th>Price</th>
<th>Profit %</th>
<th>SL</th>
<th>TP</th>
<th>BE</th>
<th>Trail</th>
<th>RSI14</th>
<th>RSI100</th>
<th>EMA20</th>
<th>ADX</th>
<th>ATR%</th>
<th>Volume</th>
<th>15m</th>
<th>Status</th>

</tr>

</thead>

<tbody id="rows">
</tbody>

</table>

</div>


<script>

function esc(x) {

    return String(
        x ?? "-"
    ).replace(
        /[&<>"']/g,
        function(m) {

            return {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;"
            }[m];

        }
    );

}


async function refresh() {

    try {

        const s =
            await fetch(
                "/api/status"
            ).then(
                r => r.json()
            );


        document.getElementById(
            "top"
        ).innerHTML =

            `<div class="card">
                Mode:
                <b>
                    ${esc(
                        s.demo
                        ? "DEMO"
                        : "LIVE"
                    )}
                </b>
            </div>`

            +

            `<div class="card">
                Auto Trade:
                <b>
                    ${esc(
                        s.auto_trade
                    )}
                </b>
            </div>`

            +

            `<div class="card">
                Margin:
                <b>
                    $${esc(
                        s.margin
                    )}
                </b>
            </div>`

            +

            `<div class="card">
                Leverage:
                <b>
                    ${esc(
                        s.leverage
                    )}x
                </b>
            </div>`

            +

            `<div class="card">
                Initial SL:
                <b>
                    ${esc(
                        s.initial_sl
                    )}%
                </b>
            </div>`

            +

            `<div class="card">
                Initial TP:
                <b>
                    ${esc(
                        s.initial_tp
                    )}%
                </b>
            </div>`

            +

            `<div class="card">
                Dynamic:
                <b class="ok">
                    ${esc(
                        s.dynamic_sltp
                    )}
                </b>
            </div>`

            +

            `<div class="card">
                Private API:
                <b class="${
                    s.private_api ===
                    "CONNECTED"
                    ? "ok"
                    : "bad"
                }">
                    ${esc(
                        s.private_api
                    )}
                </b>
            </div>`;


        document.getElementById(
            "activity"
        ).textContent =

            "Last activity: "
            +
            s.last_activity
            +
            " | Updated: "
            +
            s.updated;


        let html = "";


        for (
            const [sym, x]
            of Object.entries(
                s.symbols
            )
        ) {

            const sig =
                x.signal ||
                "NONE";


            const cls =
                sig === "BUY"
                ? "buy"
                :
                sig === "SELL"
                ? "sell"
                :
                "none";


            html +=

                `<tr>

                    <td>
                        ${esc(sym)}
                    </td>

                    <td
                        class="${cls}"
                    >
                        ${esc(sig)}
                    </td>

                    <td>
                        ${esc(x.score)}/10
                    </td>

                    <td>
                        ${esc(
                            x.position_entry ||
                            x.entry
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.current_price
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.position_profit_pct
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.dynamic_sl
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.dynamic_tp
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.break_even
                            ? "YES"
                            : "-"
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.trailing
                            ? "ACTIVE"
                            : "-"
                        )}
                    </td>

                    <td>
                        ${esc(x.rsi14)}
                    </td>

                    <td>
                        ${esc(x.rsi100)}
                    </td>

                    <td>
                        ${esc(x.ema20)}
                    </td>

                    <td>
                        ${esc(x.adx)}
                    </td>

                    <td>
                        ${esc(x.atr_pct)}
                    </td>

                    <td>
                        ${esc(x.volume_ratio)}
                    </td>

                    <td>
                        ${esc(x.trend15)}
                    </td>

                    <td>
                        ${esc(
                            x.trade_status ||
                            "WAITING"
                        )}
                    </td>

                </tr>`;

        }


        document.getElementById(
            "rows"
        ).innerHTML = html;


    } catch (e) {

        document.getElementById(
            "activity"
        ).textContent =
            "Dashboard error: "
            + e;

    }

}


refresh();

setInterval(
    refresh,
    5000
);

</script>

</body>

</html>
"""


# =========================================================
# WEB ROUTES
# =========================================================

@app.get("/")
def home():

    return Response(
        HTML,
        mimetype="text/html"
    )


@app.get("/api/status")
def api_status():

    with state_lock:

        symbols = {

            key:
                value.copy()

            for key, value
            in state.items()

            if key in SYMBOLS
        }

        public_api = state.get(
            "public_api",
            "STARTING"
        )

        private_api = state.get(
            "private_api",
            "STARTING"
        )

    activity = "STARTING"

    for symbol in SYMBOLS:

        if symbol in symbols:

            if symbols[symbol].get(
                "last_activity"
            ):

                activity = symbols[
                    symbol
                ][
                    "last_activity"
                ]

    return jsonify({

        "bot":
            "OKX Scalping Bot V6",

        "status":
            (
                "running"
                if worker_started
                else "starting"
            ),

        "demo":
            DEMO,

        "auto_trade":
            AUTO_TRADE,

        "margin":
            str(MARGIN_USDT),

        "leverage":
            str(LEVERAGE),

        "notional":
            str(
                MARGIN_USDT
                *
                LEVERAGE
            ),

        "initial_sl":
            str(SL_PERCENT),

        "initial_tp":
            str(TP_PERCENT),

        "dynamic_sltp":
            DYNAMIC_SLTP,

        "break_even_trigger":
            str(BE_TRIGGER_PERCENT),

        "trail_trigger":
            str(TRAIL_TRIGGER_PERCENT),

        "trail_distance":
            str(TRAIL_DISTANCE_PERCENT),

        "public_api":
            public_api,

        "private_api":
            private_api,

        "last_activity":
            activity,

        "updated":
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "symbols":
            symbols
    })


@app.get("/api/health")
def health():

    return jsonify({

        "status":
            "ok",

        "bot":
            "running"
            if worker_started
            else "starting",

        "demo":
            DEMO,

        "auto_trade":
            AUTO_TRADE,

        "dynamic_sltp":
            DYNAMIC_SLTP

    })


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    worker_thread = threading.Thread(
        target=worker,
        daemon=True
    )

    worker_thread.start()

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
