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
from flask import Flask, jsonify
from dotenv import load_dotenv


load_dotenv()


# =========================================================
# SETTINGS
# =========================================================

BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://www.okx.com"
).rstrip("/")

API_KEY = os.getenv(
    "OKX_API_KEY",
    ""
)

SECRET_KEY = os.getenv(
    "OKX_SECRET_KEY",
    ""
)

PASSPHRASE = os.getenv(
    "OKX_PASSPHRASE",
    ""
)

DEMO = (
    os.getenv(
        "OKX_DEMO",
        "true"
    ).lower()
    == "true"
)

AUTO_TRADE = (
    os.getenv(
        "AUTO_TRADE",
        "true"
    ).lower()
    == "true"
)

BAR = os.getenv(
    "BAR",
    "5m"
)

TREND_BAR = os.getenv(
    "TREND_BAR",
    "15m"
)

MARGIN_USDT = Decimal(
    os.getenv(
        "MARGIN_USDT",
        "20"
    )
)

LEVERAGE = Decimal(
    os.getenv(
        "LEVERAGE",
        "5"
    )
)

SL_PERCENT = Decimal(
    os.getenv(
        "SL_PERCENT",
        "0.4"
    )
)

TP_PERCENT = Decimal(
    os.getenv(
        "TP_PERCENT",
        "0.8"
    )
)

POLL_SECONDS = int(
    os.getenv(
        "POLL_SECONDS",
        "20"
    )
)

MIN_SCORE = int(
    os.getenv(
        "MIN_SCORE",
        "7"
    )
)

ADX_MIN = Decimal(
    os.getenv(
        "ADX_MIN",
        "18"
    )
)

VOLUME_MULT = Decimal(
    os.getenv(
        "VOLUME_MULT",
        "0.8"
    )
)

ATR_MIN_PCT = Decimal(
    os.getenv(
        "ATR_MIN_PCT",
        "0.05"
    )
)

TD_MODE = os.getenv(
    "TD_MODE",
    "cross"
)


SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,"
        "ETH-USDT-SWAP,"
        "XRP-USDT-SWAP,"
        "DOGE-USDT-SWAP,"
        "SOL-USDT-SWAP,"
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

order_lock = threading.Lock()


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

def D(value):

    return Decimal(
        str(value)
    )


def fmt(value):

    if value is None:

        return "-"

    return (
        f"{D(value):.8f}"
        .rstrip("0")
        .rstrip(".")
    )


# =========================================================
# OKX TIME
# =========================================================

def utc_timestamp():

    return (
        datetime.now(
            timezone.utc
        )
        .isoformat(
            timespec="milliseconds"
        )
        .replace(
            "+00:00",
            "Z"
        )
    )


# =========================================================
# OKX SIGNATURE
# =========================================================

def sign_request(
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
# PUBLIC REQUEST
# =========================================================

def public_get(
    path,
    params=None
):

    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if data.get("code") != "0":

        raise RuntimeError(
            "OKX PUBLIC ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


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

    body = ""

    if (
        method not in (
            "GET",
            "DELETE"
        )
        and payload is not None
    ):

        body = json.dumps(
            payload,
            separators=(
                ",",
                ":"
            ),
            ensure_ascii=False
        )

    # -----------------------------------------------------
    # IMPORTANT:
    # GET parameters are part of OKX requestPath.
    # -----------------------------------------------------

    query = urlencode(
        [
            (
                key,
                str(value)
            )
            for key, value
            in (params or {}).items()
            if value is not None
        ]
    )

    request_path = path

    if query:

        request_path += (
            "?"
            + query
        )

    timestamp = utc_timestamp()

    signature = sign_request(
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

    # -----------------------------------------------------
    # OKX DEMO TRADING
    # -----------------------------------------------------

    if DEMO:

        headers[
            "x-simulated-trading"
        ] = "1"

    log(
        "OKX AUTH REQUEST | "
        f"{method} "
        f"{request_path}"
    )

    response = session.request(

        method,

        BASE_URL + path,

        headers=headers,

        params=params,

        data=(
            body
            if method not in (
                "GET",
                "DELETE"
            )
            else None
        ),

        timeout=15
    )

    try:

        data = response.json()

    except Exception:

        response.raise_for_status()

        raise RuntimeError(
            "OKX returned non-JSON response"
        )

    # -----------------------------------------------------
    # HTTP ERROR
    # -----------------------------------------------------

    if response.status_code >= 400:

        raise RuntimeError(
            f"OKX HTTP "
            f"{response.status_code}: "
            f"{data}"
        )

    # -----------------------------------------------------
    # OKX API ERROR
    # -----------------------------------------------------

    if data.get("code") != "0":

        raise RuntimeError(
            "OKX PRIVATE ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# MARKET CANDLES
# =========================================================

def get_candles(
    symbol,
    bar,
    limit=160
):

    data = public_get(

        "/api/v5/market/candles",

        {
            "instId":
                symbol,

            "bar":
                bar,

            "limit":
                str(limit)
        }
    )

    candles = []

    for row in reversed(
        data.get(
            "data",
            []
        )
    ):

        candles.append(

            {
                "ts":
                    int(row[0]),

                "open":
                    D(row[1]),

                "high":
                    D(row[2]),

                "low":
                    D(row[3]),

                "close":
                    D(row[4]),

                "volume":
                    D(row[5]),

                "confirm":
                    (
                        row[8]
                        if len(row) > 8
                        else "1"
                    )
            }
        )

    return candles


# =========================================================
# EMA
# =========================================================

def calculate_ema(
    values,
    period
):

    if len(values) < period:

        return [
            None
        ] * len(values)

    result = [
        None
    ] * len(values)

    value = (
        sum(
            values[:period],
            Decimal("0")
        )
        /
        D(period)
    )

    result[
        period - 1
    ] = value

    multiplier = (
        D(2)
        /
        D(period + 1)
    )

    for i in range(
        period,
        len(values)
    ):

        value = (
            values[i]
            * multiplier
            +
            value
            *
            (
                D(1)
                -
                multiplier
            )
        )

        result[i] = value

    return result


# =========================================================
# RSI
# =========================================================

def calculate_rsi(
    values,
    period
):

    result = [
        None
    ] * len(values)

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
                D(0)
            )
        )

        losses.append(
            max(
                -change,
                D(0)
            )
        )

    avg_gain = (
        sum(
            gains[:period],
            D(0)
        )
        /
        D(period)
    )

    avg_loss = (
        sum(
            losses[:period],
            D(0)
        )
        /
        D(period)
    )

    def rsi_value(
        gain,
        loss
    ):

        if loss == 0:

            return D(100)

        rs = (
            gain
            /
            loss
        )

        return (
            D(100)
            -
            D(100)
            /
            (
                D(1)
                + rs
            )
        )

    result[
        period
    ] = rsi_value(
        avg_gain,
        avg_loss
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            avg_gain
            *
            D(period - 1)
            +
            gains[i]
        ) / D(period)

        avg_loss = (
            avg_loss
            *
            D(period - 1)
            +
            losses[i]
        ) / D(period)

        result[
            i + 1
        ] = rsi_value(
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
                -
                previous_close
            ),

            abs(
                low
                -
                previous_close
            )
        )

        trs.append(tr)

    return (
        sum(
            trs[-period:],
            D(0)
        )
        /
        D(period)
    )


# =========================================================
# ADX
# =========================================================

def calculate_adx(
    candles,
    period=14
):

    if len(candles) < period + 2:

        return D(0)

    plus = D(0)
    minus = D(0)
    total_range = D(0)

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

        return D(0)

    plus_di = (
        plus
        /
        total_range
        *
        D(100)
    )

    minus_di = (
        minus
        /
        total_range
        *
        D(100)
    )

    total = (
        plus_di
        +
        minus_di
    )

    if total == 0:

        return D(0)

    return (
        abs(
            plus_di
            -
            minus_di
        )
        /
        total
        *
        D(100)
    )


# =========================================================
# 15 MINUTE TREND
# =========================================================

def get_trend(
    symbol
):

    candles = get_candles(
        symbol,
        TREND_BAR,
        80
    )

    candles = [
        x
        for x in candles
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

    i = (
        len(closes)
        - 1
    )

    if (
        ema20[i] is None
        or
        ema20[i - 1] is None
    ):

        return "flat"

    if (
        closes[i]
        >
        ema20[i]
        and
        ema20[i]
        >
        ema20[i - 1]
    ):

        return "bull"

    if (
        closes[i]
        <
        ema20[i]
        and
        ema20[i]
        <
        ema20[i - 1]
    ):

        return "bear"

    return "flat"


# =========================================================
# ANALYSIS + SCORE
# =========================================================

def analyze_symbol(
    symbol
):

    candles = get_candles(
        symbol,
        BAR,
        160
    )

    candles = [
        x
        for x in candles
        if x["confirm"] == "1"
    ]

    if len(candles) < 105:

        return {
            "signal":
                "NONE",

            "score":
                0,

            "buy":
                0,

            "sell":
                0,

            "reason":
                "Not enough candles"
        }

    closes = [
        x["close"]
        for x in candles
    ]

    i = (
        len(candles)
        - 1
    )

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

    trend15 = get_trend(
        symbol
    )

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
            "signal":
                "NONE",

            "score":
                0,

            "buy":
                0,

            "sell":
                0,

            "reason":
                "Indicator unavailable"
        }

    average_volume = (
        sum(
            x["volume"]
            for x in candles[-21:-1]
        )
        /
        D(20)
    )

    volume_ratio = (

        candles[i]["volume"]
        /
        average_volume

        if average_volume

        else D(0)
    )

    atr_percent = (
        atr
        /
        closes[i]
        *
        D(100)
    )

    buy_score = 0
    sell_score = 0

    reasons = []

    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # PRICE VS EMA20
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # EMA20 SLOPE
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # ADX
    # -----------------------------------------------------

    if adx >= ADX_MIN:

        if buy_score > sell_score:

            buy_score += 1

        elif sell_score > buy_score:

            sell_score += 1

        reasons.append(
            "ADX confirms strength"
        )

    else:

        reasons.append(
            "ADX below minimum"
        )

    # -----------------------------------------------------
    # VOLUME
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # ATR
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # 15M TREND
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # EMA PROXIMITY
    # -----------------------------------------------------

    near_ema = (

        abs(
            closes[i]
            -
            ema20[i]
        )
        /
        closes[i]
        *
        D(100)
        <
        D("0.15")
    )

    if near_ema:

        reasons.append(
            "Near EMA20"
        )

    # -----------------------------------------------------
    # FINAL SCORE
    # -----------------------------------------------------

    score = max(
        buy_score,
        sell_score
    )

    signal = "NONE"

    if (
        buy_score
        >
        sell_score
        and
        buy_score
        >= MIN_SCORE
    ):

        signal = "BUY"

    elif (
        sell_score
        >
        buy_score
        and
        sell_score
        >= MIN_SCORE
    ):

        signal = "SELL"

    # -----------------------------------------------------
    # DO NOT TRADE AGAINST 15M TREND
    # -----------------------------------------------------

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

        "signal":
            signal,

        "score":
            score,

        "buy":
            buy_score,

        "sell":
            sell_score,

        "entry":
            closes[i],

        "rsi14":
            rsi14[i],

        "rsi100":
            rsi100[i],

        "ema20":
            ema20[i],

        "adx":
            adx,

        "atr_pct":
            atr_percent,

        "volume_ratio":
            volume_ratio,

        "trend15":
            trend15,

        "reason":
            " | ".join(
                reasons
            )
    }


# =========================================================
# INSTRUMENT INFORMATION
# =========================================================

def get_instrument(
    symbol
):

    data = public_get(

        "/api/v5/public/instruments",

        {
            "instType":
                "SWAP",

            "instId":
                symbol
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
            D(item["ctVal"]),

        "lotSz":
            D(item["lotSz"]),

        "minSz":
            D(item["minSz"]),

        "tickSz":
            D(item["tickSz"]),

        "state":
            item["state"]
    }


# =========================================================
# SIZE ROUNDING
# =========================================================

def floor_step(
    value,
    step
):

    if step <= 0:

        return value

    return (

        value
        /
        step

    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step


def calculate_order_size(
    symbol,
    price
):

    info = get_instrument(
        symbol
    )

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

            "Order size below minimum. "
            f"Calculated={size}, "
            f"minSz={info['minSz']}, "
            f"lotSz={info['lotSz']}, "
            f"ctVal={info['ctVal']}"
        )

    return size, info


# =========================================================
# ACCOUNT CONFIG
# =========================================================

def get_account_config():

    return private_request(
        "GET",
        "/api/v5/account/config"
    )


# =========================================================
# POSITIONS
# =========================================================

def get_positions(
    symbol
):

    return private_request(

        "GET",

        "/api/v5/account/positions",

        params={
            "instId":
                symbol
        }
    )


# =========================================================
# SET LEVERAGE
# =========================================================

def set_leverage(
    symbol
):

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
# PLACE DEMO ORDER
# =========================================================

def place_order(
    symbol,
    analysis
):

    if not AUTO_TRADE:

        return {

            "status":
                "BLOCKED",

            "reason":
                "AUTO_TRADE=false"
        }

    if not DEMO:

        return {

            "status":
                "BLOCKED",

            "reason":
                "DEMO mode required"
        }

    with order_lock:

        # -------------------------------------------------
        # CHECK EXISTING POSITION
        # -------------------------------------------------

        positions = get_positions(
            symbol
        )

        active_positions = [

            p

            for p
            in positions.get(
                "data",
                []
            )

            if D(
                p.get(
                    "pos",
                    "0"
                )
            ) != 0
        ]

        if active_positions:

            return {

                "status":
                    "BLOCKED",

                "reason":
                    "Existing position"
            }

        # -------------------------------------------------
        # SET LEVERAGE
        # -------------------------------------------------

        try:

            set_leverage(
                symbol
            )

            log(
                f"{symbol}: "
                "LEVERAGE SET "
                f"{fmt(LEVERAGE)}x"
            )

        except Exception as error:

            log(
                f"{symbol}: "
                "LEVERAGE WARNING | "
                f"{type(error).__name__}: "
                f"{error}"
            )

        # -------------------------------------------------
        # CALCULATE CONTRACT SIZE
        # -------------------------------------------------

        price = analysis[
            "entry"
        ]

        size, info = (
            calculate_order_size(
                symbol,
                price
            )
        )

        # -------------------------------------------------
        # SIDE
        # -------------------------------------------------

        if analysis[
            "signal"
        ] == "BUY":

            side = "buy"

        else:

            side = "sell"

        # -------------------------------------------------
        # UNIQUE CLIENT ORDER ID
        # -------------------------------------------------

        client_id = (

            "bot"
            +
            uuid.uuid4().hex[:16]
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
                client_id
        }

        log(
            f"{symbol}: "
            f"PLACING DEMO ORDER | "
            f"side={side} "
            f"size={fmt(size)} "
            f"entry={fmt(price)}"
        )

        # -------------------------------------------------
        # SEND ORDER
        # -------------------------------------------------

        result = private_request(

            "POST",

            "/api/v5/trade/order",

            payload=payload
        )

        order_data = result.get(
            "data",
            []
        )

        if not order_data:

            raise RuntimeError(
                "OKX returned empty "
                "order data"
            )

        item = order_data[0]

        s_code = item.get(
            "sCode",
            ""
        )

        s_msg = item.get(
            "sMsg",
            ""
        )

        if s_code != "0":

            raise RuntimeError(

                "OKX ORDER REJECTED | "
                f"sCode={s_code} | "
                f"sMsg={s_msg}"
            )

        return {

            "status":
                "ORDER_ACCEPTED",

            "ordId":
                item.get(
                    "ordId"
                ),

            "clOrdId":
                client_id,

            "symbol":
                symbol,

            "side":
                side,

            "size":
                fmt(size),

            "entry":
                fmt(price),

            "sl_percent":
                fmt(SL_PERCENT),

            "tp_percent":
                fmt(TP_PERCENT)
        }


# =========================================================
# PRIVATE API TEST
# =========================================================

def test_private_api():

    return private_request(

        "GET",

        "/api/v5/account/balance"
    )


# =========================================================
# BOT WORKER
# =========================================================

def worker():

    log(
        "======================================"
    )

    log(
        "OKX RSI + EMA20 + ADX + ATR "
        "+ Volume Scalping V5 STARTED"
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
        f"TIMEFRAME={BAR}"
    )

    log(
        f"TREND={TREND_BAR}"
    )

    log(
        f"MIN_SCORE={MIN_SCORE}/10"
    )

    log(
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "======================================"
    )

    # -----------------------------------------------------
    # CONNECTION TEST
    # -----------------------------------------------------

    try:

        public_get(

            "/api/v5/market/ticker",

            {
                "instId":
                    "BTC-USDT-SWAP"
            }
        )

        log(
            "OKX PUBLIC MARKET CONNECTED"
        )

    except Exception as error:

        log(
            "OKX PUBLIC CONNECTION ERROR | "
            f"{type(error).__name__}: "
            f"{error}"
        )

    try:

        test_private_api()

        log(
            "OKX PRIVATE API CONNECTED"
        )

    except Exception as error:

        log(
            "OKX PRIVATE API ERROR | "
            f"{type(error).__name__}: "
            f"{error}"
        )

    # -----------------------------------------------------
    # MAIN LOOP
    # -----------------------------------------------------

    while True:

        for symbol in SYMBOLS:

            try:

                analysis = (
                    analyze_symbol(
                        symbol
                    )
                )

                state[
                    symbol
                ] = analysis

                log(

                    f"{symbol}: "
                    f"{analysis['signal']} "
                    f"{analysis['score']}/10 | "
                    f"BUY={analysis['buy']} "
                    f"SELL={analysis['sell']} | "
                    f"{analysis['reason']}"
                )

                # -------------------------------------------------
                # TRADE CONDITION
                # -------------------------------------------------

                if (

                    analysis["signal"]
                    in (
                        "BUY",
                        "SELL"
                    )

                    and

                    analysis["score"]
                    >=
                    MIN_SCORE
                ):

                    result = (
                        place_order(
                            symbol,
                            analysis
                        )
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

            except Exception as error:

                state[
                    symbol
                ] = {

                    "signal":
                        "ERROR",

                    "score":
                        0,

                    "buy":
                        0,

                    "sell":
                        0,

                    "reason":
                        (
                            f"{type(error).__name__}: "
                            f"{error}"
                        )
                }

                log(

                    f"TRADE ERROR | "
                    f"{symbol} | "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

        time.sleep(
            POLL_SECONDS
        )


# =========================================================
# WEB DASHBOARD
# =========================================================

@app.get("/")
def home():

    return jsonify(

        {

            "bot":
                "OKX RSI EMA20 ADX ATR Volume Scalping V5",

            "demo":
                DEMO,

            "auto_trade":
                AUTO_TRADE,

            "status":
                "running",

            "symbols":
                SYMBOLS,

            "timeframe":
                BAR,

            "trend_timeframe":
                TREND_BAR,

            "minimum_score":
                f"{MIN_SCORE}/10",

            "margin_usdt":
                str(
                    MARGIN_USDT
                ),

            "leverage":
                str(
                    LEVERAGE
                ),

            "public_api":
                "CONNECTED",

            "private_api":
                (
                    "CONFIGURED"
                    if API_KEY
                    else
                    "MISSING"
                ),

            "pairs":
                state
        }
    )


# =========================================================
# API STATUS
# =========================================================

@app.get("/api/status")
def api_status():

    return jsonify(

        {

            "status":
                "running",

            "demo":
                DEMO,

            "auto_trade":
                AUTO_TRADE,

            "pairs":
                state,

            "timestamp":
                utc_timestamp()
        }
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    threading.Thread(
        target=worker,
        daemon=True
    ).start()

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    app.run(

        host="0.0.0.0",

        port=port
    )
