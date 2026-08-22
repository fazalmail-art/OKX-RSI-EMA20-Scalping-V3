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

last_candle = {}

state = {}

order_lock = threading.Lock()


# =========================================================
# LOG
# =========================================================

def log(message):

    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True
    )


# =========================================================
# DECIMAL
# =========================================================

def dec(value):

    return Decimal(
        str(value)
    )


def fmt(value):

    if value is None:

        return "-"

    return (
        f"{Decimal(str(value)):.8f}"
        .rstrip("0")
        .rstrip(".")
    )


# =========================================================
# UTC TIME
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

def create_signature(
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
        SECRET_KEY.encode(),
        message.encode(),
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
# PRIVATE REQUEST - FIXED
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


    # -----------------------------------------------------
    # GET PARAMETERS MUST BE PART OF SIGNED REQUEST PATH
    # -----------------------------------------------------

    query_string = ""

    if params:

        query_string = (
            "?"
            +
            urlencode(
                [
                    (
                        str(key),
                        str(value)
                    )
                    for key, value
                    in params.items()
                    if value is not None
                ]
            )
        )


    request_path = (
        path
        +
        query_string
    )


    # -----------------------------------------------------
    # BODY
    # -----------------------------------------------------

    body = ""

    if (
        method != "GET"
        and
        payload is not None
    ):

        body = json.dumps(
            payload,
            separators=(
                ",",
                ":"
            )
        )


    # -----------------------------------------------------
    # TIMESTAMP
    # -----------------------------------------------------

    timestamp = utc_timestamp()


    # -----------------------------------------------------
    # HEADERS
    # -----------------------------------------------------

    headers = {

        "Content-Type":
            "application/json",

        "OK-ACCESS-KEY":
            API_KEY,

        "OK-ACCESS-SIGN":
            create_signature(
                timestamp,
                method,
                request_path,
                body
            ),

        "OK-ACCESS-PASSPHRASE":
            PASSPHRASE,

        "OK-ACCESS-TIMESTAMP":
            timestamp
    }


    # -----------------------------------------------------
    # DEMO HEADER
    # -----------------------------------------------------

    if DEMO:

        headers[
            "x-simulated-trading"
        ] = "1"


    log(
        "OKX PRIVATE REQUEST | "
        f"{method} {request_path}"
    )


    # -----------------------------------------------------
    # REQUEST
    # -----------------------------------------------------

    response = session.request(

        method,

        BASE_URL
        +
        request_path,

        headers=headers,

        json=(
            payload
            if method != "GET"
            else None
        ),

        timeout=15
    )


    # -----------------------------------------------------
    # RESPONSE
    # -----------------------------------------------------

    try:

        data = response.json()

    except Exception:

        data = {
            "code":
                str(response.status_code),

            "msg":
                response.text
        }


    if response.status_code >= 400:

        raise RuntimeError(
            "OKX HTTP "
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
# CANDLES
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
        data.get("data", [])
    ):

        candles.append(
            {
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
                    row[8]
                    if len(row) > 8
                    else "1"
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
        Decimal(period)
    )

    result[
        period - 1
    ] = value

    multiplier = (
        Decimal("2")
        /
        Decimal(
            period + 1
        )
    )

    for i in range(
        period,
        len(values)
    ):

        value = (
            values[i]
            *
            multiplier
            +
            value
            *
            (
                Decimal("1")
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

        rs = (
            gain
            /
            loss
        )

        return (
            Decimal("100")
            -
            Decimal("100")
            /
            (
                Decimal("1")
                +
                rs
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
            (period - 1)
            +
            gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss
            *
            (period - 1)
            +
            losses[i]
        ) / Decimal(period)

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


    total = (
        plus_di
        +
        minus_di
    )


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
# 15 MIN TREND
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
        -
        1
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
# ANALYSIS
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
        -
        1
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
    # EMA POSITION
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
    # EMA SLOPE
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
        Decimal("100")
        <
        Decimal("0.15")
    )


    if near_ema:

        reasons.append(
            "Near EMA20"
        )


    # -----------------------------------------------------
    # SCORE
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
        >=
        MIN_SCORE
    ):

        signal = "BUY"


    elif (
        sell_score
        >
        buy_score
        and
        sell_score
        >=
        MIN_SCORE
    ):

        signal = "SELL"


    # -----------------------------------------------------
    # 15M TREND PROTECTION
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


    elif (
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
# INSTRUMENT
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
            +
            symbol
        )


    item = data[
        "data"
    ][0]


    return {

        "ctVal":
            dec(
                item["ctVal"]
            ),

        "lotSz":
            dec(
                item["lotSz"]
            ),

        "minSz":
            dec(
                item["minSz"]
            ),

        "tickSz":
            dec(
                item["tickSz"]
            ),

        "state":
            item["state"]
    }


# =========================================================
# ROUNDING
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


# =========================================================
# ORDER SIZE
# =========================================================

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
            f"size={size}, "
            f"minSz={info['minSz']}, "
            f"lotSz={info['lotSz']}, "
            f"ctVal={info['ctVal']}"
        )


    return (
        size,
        info
    )


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
    symbol=None
):

    params = None


    if symbol:

        params = {
            "instId":
                symbol
        }


    return private_request(
        "GET",
        "/api/v5/account/positions",
        params=params
    )


# =========================================================
# CHECK POSITION
# =========================================================

def has_open_position(
    symbol
):

    data = get_positions(
        symbol
    )


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


            if position_size != 0:

                return True


        except Exception:

            continue


    return False


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
            fmt(
                LEVERAGE
            ),

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
    signal
):

    if not DEMO:

        raise RuntimeError(
            "SAFETY STOP: "
            "DEMO must be true"
        )


    if not AUTO_TRADE:

        return {

            "status":
                "BLOCKED",

            "message":
                "AUTO_TRADE=false"
        }


    if signal[
        "signal"
    ] not in (
        "BUY",
        "SELL"
    ):

        return {

            "status":
                "BLOCKED",

            "message":
                "No executable signal"
        }


    with order_lock:

        if has_open_position(
            symbol
        ):

            return {

                "status":
                    "SKIPPED",

                "message":
                    "Existing position already open"
            }


        price = dec(
            signal["entry"]
        )


        size, info = (
            calculate_order_size(
                symbol,
                price
            )
        )


        # Set leverage first

        set_leverage(
            symbol
        )


        if signal[
            "signal"
        ] == "BUY":

            side = "buy"

            sl = (
                price
                *
                (
                    Decimal("1")
                    -
                    SL_PERCENT
                    /
                    Decimal("100")
                )
            )

            tp = (
                price
                *
                (
                    Decimal("1")
                    +
                    TP_PERCENT
                    /
                    Decimal("100")
                )
            )


        else:

            side = "sell"

            sl = (
                price
                *
                (
                    Decimal("1")
                    +
                    SL_PERCENT
                    /
                    Decimal("100")
                )
            )

            tp = (
                price
                *
                (
                    Decimal("1")
                    -
                    TP_PERCENT
                    /
                    Decimal("100")
                )
            )


        sl = floor_step(
            sl,
            info["tickSz"]
        )

        tp = floor_step(
            tp,
            info["tickSz"]
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
                "BOT"
                +
                uuid.uuid4()
                .hex[:16],

            "attachAlgoOrds":
                [
                    {

                        "slTriggerPx":
                            fmt(sl),

                        "slOrdPx":
                            "-1",

                        "tpTriggerPx":
                            fmt(tp),

                        "tpOrdPx":
                            "-1"
                    }
                ]
        }


        result = private_request(
            "POST",
            "/api/v5/trade/order",
            payload=payload
        )


        log(
            "ORDER SUCCESS | "
            f"{symbol} | "
            f"{side.upper()} | "
            f"SIZE={fmt(size)} | "
            f"SL={fmt(sl)} | "
            f"TP={fmt(tp)}"
        )


        return {

            "status":
                "ORDER_PLACED",

            "symbol":
                symbol,

            "side":
                side.upper(),

            "size":
                fmt(size),

            "sl":
                fmt(sl),

            "tp":
                fmt(tp),

            "okx":
                result
        }


# =========================================================
# PRIVATE API TEST
# =========================================================

def test_private():

    data = get_account_config()

    item = data.get(
        "data",
        [{}]
    )[0]


    return {

        "connected":
            True,

        "uid":
            item.get(
                "uid"
            ),

        "account_level":
            item.get(
                "acctLv"
            ),

        "position_mode":
            item.get(
                "posMode"
            )
    }


# =========================================================
# DASHBOARD DATA
# =========================================================

def dashboard_rows():

    rows = []


    for symbol in SYMBOLS:

        item = state.get(
            symbol,
            {}
        )


        rows.append(
            {

                "pair":
                    symbol,

                "signal":
                    item.get(
                        "signal",
                        "-"
                    ),

                "score":
                    item.get(
                        "score",
                        "-"
                    ),

                "buy":
                    item.get(
                        "buy",
                        "-"
                    ),

                "sell":
                    item.get(
                        "sell",
                        "-"
                    ),

                "entry":
                    fmt(
                        item.get(
                            "entry"
                        )
                    ),

                "rsi14":
                    fmt(
                        item.get(
                            "rsi14"
                        )
                    ),

                "rsi100":
                    fmt(
                        item.get(
                            "rsi100"
                        )
                    ),

                "ema20":
                    fmt(
                        item.get(
                            "ema20"
                        )
                    ),

                "adx":
                    fmt(
                        item.get(
                            "adx"
                        )
                    ),

                "atr_pct":
                    fmt(
                        item.get(
                            "atr_pct"
                        )
                    ),

                "volume":
                    fmt(
                        item.get(
                            "volume_ratio"
                        )
                    ),

                "trend15":
                    item.get(
                        "trend15",
                        "-"
                    ),

                "reason":
                    item.get(
                        "reason",
                        ""
                    ),

                "status":
                    item.get(
                        "status",
                        "-"
                    )
            }
        )


    return rows


# =========================================================
# WEB DASHBOARD
# =========================================================

@app.get("/")
def home():

    rows = dashboard_rows()


    html = """
<!DOCTYPE html>

<html>

<head>

<meta http-equiv="refresh"
      content="10">

<title>
OKX V4 Bot
</title>

<style>

body {
    font-family: Arial;
    background: #111;
    color: #eee;
    padding: 20px;
}

table {
    border-collapse: collapse;
    width: 100%;
}

th, td {
    border: 1px solid #444;
    padding: 7px;
    font-size: 12px;
}

th {
    background: #222;
}

</style>

</head>

<body>

<h2>
OKX RSI EMA20 ADX ATR Volume Scalping V4
</h2>

<p>
Mode: DEMO |
Margin: $%s |
Leverage: %sx |
Auto Trade: %s
</p>

<p>
Public API:
CONNECTED
|
Private API:
%s
</p>

<p>
Last activity:
%s
</p>

<table>

<tr>

<th>Pair</th>
<th>Signal</th>
<th>Score</th>
<th>BUY</th>
<th>SELL</th>
<th>Entry</th>
<th>RSI14</th>
<th>RSI100</th>
<th>EMA20</th>
<th>ADX</th>
<th>ATR%%</th>
<th>Volume</th>
<th>15m Trend</th>
<th>Reason</th>
<th>Status</th>

</tr>

%s

</table>

</body>

</html>
""" % (

        MARGIN_USDT,

        LEVERAGE,

        AUTO_TRADE,

        (
            "CONNECTED"
            if state.get(
                "_private"
            )
            else
            "ERROR"
        ),

        state.get(
            "_activity",
            "starting"
        ),

        "".join(

            f"""
<tr>

<td>{r['pair']}</td>

<td>{r['signal']}</td>

<td>{r['score']}/10</td>

<td>{r['buy']}</td>

<td>{r['sell']}</td>

<td>{r['entry']}</td>

<td>{r['rsi14']}</td>

<td>{r['rsi100']}</td>

<td>{r['ema20']}</td>

<td>{r['adx']}</td>

<td>{r['atr_pct']}</td>

<td>{r['volume']}</td>

<td>{r['trend15']}</td>

<td>{r['reason']}</td>

<td>{r['status']}</td>

</tr>
"""

            for r in rows
        )
    )


    return Response(
        html,
        mimetype="text/html"
    )


# =========================================================
# API STATUS
# =========================================================

@app.get(
    "/api/status"
)
def api_status():

    return jsonify(

        {

            "bot":
                "OKX RSI EMA20 ADX ATR Volume Scalping V4",

            "demo":
                DEMO,

            "auto_trade":
                AUTO_TRADE,

            "margin_usdt":
                str(
                    MARGIN_USDT
                ),

            "leverage":
                str(
                    LEVERAGE
                ),

            "status":
                "running",

            "public_api":
                "CONNECTED",

            "private_api":
                (
                    "CONNECTED"
                    if state.get(
                        "_private"
                    )
                    else
                    "ERROR"
                ),

            "last_activity":
                state.get(
                    "_activity",
                    "starting"
                ),

            "pairs":
                dashboard_rows()
        }
    )


# =========================================================
# BOT WORKER
# =========================================================

def worker():

    log(
        "=============================================="
    )

    log(
        "OKX RSI + EMA20 + ADX + ATR "
        "+ Volume V4 STARTED"
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
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "=============================================="
    )


    # -----------------------------------------------------
    # PRIVATE API TEST
    # -----------------------------------------------------

    try:

        test_private()

        state[
            "_private"
        ] = True

        log(
            "OKX PRIVATE API CONNECTED"
        )


    except Exception as error:

        state[
            "_private"
        ] = False

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

            state[
                "_activity"
            ] = (
                f"CHECKING {symbol}"
            )


            try:

                log(
                    f"CHECKING {symbol}"
                )


                analysis = (
                    analyze_symbol(
                        symbol
                    )
                )


                state[
                    symbol
                ] = analysis


                # -------------------------------------------------
                # VALID SIGNAL
                # -------------------------------------------------

                if (
                    analysis[
                        "signal"
                    ]
                    in (
                        "BUY",
                        "SELL"
                    )
                    and
                    analysis[
                        "score"
                    ]
                    >=
                    MIN_SCORE
                ):


                    log(
                        "================================"
                    )

                    log(
                        f"SIGNAL | "
                        f"{symbol} | "
                        f"{analysis['signal']} | "
                        f"{analysis['score']}/10"
                    )

                    log(
                        f"REASON | "
                        f"{analysis['reason']}"
                    )

                    log(
                        f"ENTRY | "
                        f"{analysis['entry']}"
                    )


                    # -------------------------------------------------
                    # EXECUTE
                    # -------------------------------------------------

                    try:

                        result = (
                            place_order(
                                symbol,
                                analysis
                            )
                        )


                        state[
                            symbol
                        ][
                            "status"
                        ] = result.get(
                            "status",
                            "UNKNOWN"
                        )


                        log(
                            json.dumps(
                                result,
                                default=str
                            )
                        )


                    except Exception as error:

                        state[
                            symbol
                        ][
                            "status"
                        ] = "ERROR"


                        log(
                            f"TRADE ERROR | "
                            f"{symbol} | "
                            f"{type(error).__name__}: "
                            f"{error}"
                        )


                # -------------------------------------------------
                # NO VALID SIGNAL
                # -------------------------------------------------

                else:

                    state[
                        symbol
                    ][
                        "status"
                    ] = "NO TRADE"


                    log(
                        f"{symbol}: "
                        f"NO TRADE | "
                        f"SCORE="
                        f"{analysis.get('score', 0)}/10 | "
                        f"{analysis.get('reason', '')}"
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
                        ),

                    "status":
                        "ERROR"
                }


                log(
                    f"{symbol} ANALYSIS ERROR | "
                    f"{type(error).__name__}: "
                    f"{error}"
                )


        time.sleep(
            POLL_SECONDS
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
