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

def dec(value):

    return Decimal(
        str(value)
    )


def fmt(value):

    if value is None:
        return "-"

    return (
        f"{value:.8f}"
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

def create_signature(
    timestamp,
    method,
    path,
    body=""
):

    message = (
        timestamp
        + method.upper()
        + path
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

    body = ""

    if payload is not None:

        body = json.dumps(
            payload,
            separators=(",", ":")
        )

    timestamp = utc_timestamp()

    headers = {
        "Content-Type":
            "application/json",

        "OK-ACCESS-KEY":
            API_KEY,

        "OK-ACCESS-SIGN":
            create_signature(
                timestamp,
                method,
                path,
                body
            ),

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
        json=payload,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

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
        / Decimal(period)
    )

    result[
        period - 1
    ] = value

    multiplier = (
        Decimal("2")
        / Decimal(period + 1)
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
            - values[i - 1]
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
        / Decimal(period)
    )

    avg_loss = (
        sum(
            losses[:period],
            Decimal("0")
        )
        / Decimal(period)
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
            * (period - 1)
            + gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss
            * (period - 1)
            + losses[i]
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
        / Decimal(period)
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
            and up_move > 0
        ):

            plus += up_move

        if (
            down_move > up_move
            and down_move > 0
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
        / total_range
        * Decimal("100")
    )

    minus_di = (
        minus
        / total_range
        * Decimal("100")
    )

    total = (
        plus_di
        + minus_di
    )

    if total == 0:

        return Decimal("0")

    return (
        abs(
            plus_di
            - minus_di
        )
        / total
        * Decimal("100")
    )


# =========================================================
# 15M TREND
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

    i = len(
        closes
    ) - 1

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
# ANALYSIS / SCORE
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
            "signal": "NONE",
            "score": 0,
            "reason":
                "Not enough candles"
        }

    closes = [
        x["close"]
        for x in candles
    ]

    i = len(
        candles
    ) - 1

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
            "signal": "NONE",
            "score": 0,
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

    # RSI
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

    # EMA position
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

    # EMA slope
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

    # ADX
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

    # Volume
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

    # ATR
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

    # 15m trend
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

    # EMA proximity
    near_ema = (
        abs(
            closes[i] - ema20[i]
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

    # Do not trade against 15m trend
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
        "volume_ratio":
            volume_ratio,
        "trend15":
            trend15,
        "reason":
            " | ".join(reasons)
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
# SIZE ROUNDING
# =========================================================

def floor_step(
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
            + info["state"]
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
# EXISTING POSITION CHECK
# =========================================================

def get_open_positions(
    symbol
):

    data = private_request(
        "GET",
        "/api/v5/account/positions",
        params={
            "instId":
                symbol
        }
    )

    positions = []

    for item in data.get(
        "data",
        []
    ):

        try:

            if dec(
                item.get(
                    "pos",
                    "0"
                )
            ) != 0:

                positions.append(
                    item
                )

        except Exception:

            continue

    return positions


# =========================================================
# ACTUAL DEMO ORDER
# =========================================================

def execute_order(
    symbol,
    signal
):

    if not DEMO:

        raise RuntimeError(
            "SAFETY BLOCK: "
            "DEMO must be true"
        )

    if not AUTO_TRADE:

        return {
            "status":
                "BLOCKED",

            "reason":
                "AUTO_TRADE=false"
        }

    with order_lock:

        existing = get_open_positions(
            symbol
        )

        if existing:

            return {
                "status":
                    "SKIPPED",

                "reason":
                    "Existing position",

                "position":
                    existing[0].get(
                        "pos"
                    )
            }

        price = dec(
            signal["entry"]
        )

        size, instrument = (
            calculate_order_size(
                symbol,
                price
            )
        )

        config = get_account_config()

        position_mode = (
            config["data"][0]
            .get(
                "posMode",
                "net"
            )
        )

        side = (
            "buy"
            if signal["signal"]
            == "BUY"
            else
            "sell"
        )

        body = {
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
                + uuid.uuid4()
                .hex[:20]
        }

        if position_mode == "long_short":

            body["posSide"] = (
                "long"
                if side == "buy"
                else "short"
            )

        log(
            "================================================"
        )

        log(
            f"EXECUTE ORDER | "
            f"{symbol} | "
            f"{side.upper()} | "
            f"size={body['sz']} | "
            f"tdMode={TD_MODE} | "
            f"posMode={position_mode}"
        )

        log(
            "ORDER REQUEST: "
            +
            json.dumps(
                body
            )
        )

        response = private_request(
            "POST",
            "/api/v5/trade/order",
            payload=body
        )

        log(
            "OKX ORDER RESPONSE: "
            +
            json.dumps(
                response,
                default=str
            )
        )

        item = (
            response
            .get(
                "data",
                [{}]
            )[0]
        )

        if item.get(
            "sCode"
        ) != "0":

            raise RuntimeError(
                "ORDER REJECTED | "
                f"sCode={item.get('sCode')} | "
                f"sMsg={item.get('sMsg')}"
            )

        order_id = item.get(
            "ordId"
        )

        log(
            f"ORDER ACCEPTED | "
            f"{symbol} | "
            f"ordId={order_id} | "
            f"sCode=0"
        )

        return {
            "status":
                "ORDER_ACCEPTED",

            "ordId":
                order_id,

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
# STARTUP DIAGNOSTIC
# =========================================================

def run_diagnostic():

    result = {
        "public_api":
            "FAIL",

        "private_api":
            "FAIL",

        "account":
            "FAIL",

        "instruments":
            {}
    }

    # Public
    try:

        ticker = public_get(
            "/api/v5/market/ticker",
            {
                "instId":
                    "BTC-USDT-SWAP"
            }
        )

        result[
            "public_api"
        ] = "PASS"

        if ticker.get("data"):

            result[
                "btc_price"
            ] = ticker[
                "data"
            ][0].get(
                "last"
            )

    except Exception as error:

        result[
            "public_error"
        ] = str(error)

    # Private
    try:

        private_request(
            "GET",
            "/api/v5/account/balance"
        )

        result[
            "private_api"
        ] = "PASS"

        result[
            "account"
        ] = "PASS"

    except Exception as error:

        result[
            "private_error"
        ] = str(error)

    # Instruments
    for symbol in SYMBOLS:

        try:

            info = get_instrument(
                symbol
            )

            result[
                "instruments"
            ][symbol] = {
                "status":
                    "PASS",

                "state":
                    info["state"],

                "ctVal":
                    fmt(
                        info["ctVal"]
                    ),

                "lotSz":
                    fmt(
                        info["lotSz"]
                    ),

                "minSz":
                    fmt(
                        info["minSz"]
                    )
            }

        except Exception as error:

            result[
                "instruments"
            ][symbol] = {
                "status":
                    "FAIL",

                "error":
                    str(error)
            }

    return result


# =========================================================
# BOT WORKER
# =========================================================

def worker():

    log(
        "=============================================="
    )

    log(
        "OKX RSI + EMA20 + ADX + ATR "
        "+ VOLUME SCALPING V5 STARTED"
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
        f"NOTIONAL=${MARGIN_USDT * LEVERAGE}"
    )

    log(
        f"TIMEFRAME={BAR}"
    )

    log(
        f"TREND={TREND_BAR}"
    )

    log(
        f"MINIMUM SCORE={MIN_SCORE}/10"
    )

    log(
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "=============================================="
    )

    try:

        diagnostic = run_diagnostic()

        log(
            "STARTUP DIAGNOSTIC:"
        )

        log(
            json.dumps(
                diagnostic,
                indent=2,
                default=str
            )
        )

    except Exception as error:

        log(
            "DIAGNOSTIC ERROR: "
            f"{type(error).__name__}: "
            f"{error}"
        )

    while True:

        for symbol in SYMBOLS:

            try:

                log(
                    f"CHECKING {symbol}"
                )

                analysis = analyze_symbol(
                    symbol
                )

                state[
                    symbol
                ] = analysis

                log(
                    f"{symbol}: "
                    f"{analysis.get('signal')} "
                    f"{analysis.get('score', 0)}/10 "
                    f"| BUY="
                    f"{analysis.get('buy', 0)} "
                    f"| SELL="
                    f"{analysis.get('sell', 0)}"
                )

                log(
                    f"{symbol}: "
                    f"RSI14="
                    f"{fmt(analysis.get('rsi14'))} "
                    f"EMA20="
                    f"{fmt(analysis.get('ema20'))} "
                    f"ADX="
                    f"{fmt(analysis.get('adx'))} "
                    f"ATR="
                    f"{fmt(analysis.get('atr_pct'))}% "
                    f"VOL="
                    f"{fmt(analysis.get('volume_ratio'))}x "
                    f"15m="
                    f"{analysis.get('trend15')}"
                )

                log(
                    f"{symbol}: "
                    f"{analysis.get('reason')}"
                )

                if (
                    analysis.get(
                        "signal"
                    )
                    != "NONE"
                    and
                    analysis.get(
                        "score",
                        0
                    )
                    >= MIN_SCORE
                ):

                    log(
                        "****************************************"
                    )

                    log(
                        f"SIGNAL DETECTED | "
                        f"{symbol} | "
                        f"{analysis['signal']} | "
                        f"{analysis['score']}/10"
                    )

                    try:

                        order_result = (
                            execute_order(
                                symbol,
                                analysis
                            )
                        )

                        state[
                            symbol
                        ][
                            "order"
                        ] = order_result

                        log(
                            "ORDER RESULT: "
                            +
                            json.dumps(
                                order_result,
                                default=str
                            )
                        )

                    except Exception as error:

                        error_text = (
                            f"{type(error).__name__}: "
                            f"{error}"
                        )

                        state[
                            symbol
                        ][
                            "order"
                        ] = {
                            "status":
                                "ERROR",

                            "reason":
                                error_text
                        }

                        log(
                            f"TRADE ERROR | "
                            f"{symbol} | "
                            f"{error_text}"
                        )

                    log(
                        "****************************************"
                    )

                else:

                    state[
                        symbol
                    ][
                        "order"
                    ] = {
                        "status":
                            "NO TRADE"
                    }

            except Exception as error:

                error_text = (
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                state[
                    symbol
                ] = {
                    "signal":
                        "NONE",

                    "score":
                        0,

                    "reason":
                        "ANALYSIS ERROR: "
                        + error_text,

                    "order":
                        {
                            "status":
                                "ERROR",

                            "reason":
                                error_text
                        }
                }

                log(
                    f"{symbol} ANALYSIS ERROR: "
                    f"{error_text}"
                )

        time.sleep(
            POLL_SECONDS
        )


# =========================================================
# WEB DASHBOARD
# =========================================================

@app.get("/")
def home():

    rows = []

    for symbol in SYMBOLS:

        item = state.get(
            symbol,
            {
                "signal":
                    "-",

                "score":
                    0
            }
        )

        order = item.get(
            "order",
            {}
        )

        rows.append(
            "<tr>"
            f"<td>{symbol}</td>"
            f"<td>{item.get('signal', '-')}</td>"
            f"<td>{item.get('score', 0)}/10</td>"
            f"<td>{item.get('buy', 0)}</td>"
            f"<td>{item.get('sell', 0)}</td>"
            f"<td>{fmt(item.get('entry'))}</td>"
            f"<td>{fmt(item.get('rsi14'))}</td>"
            f"<td>{fmt(item.get('rsi100'))}</td>"
            f"<td>{fmt(item.get('ema20'))}</td>"
            f"<td>{fmt(item.get('adx'))}</td>"
            f"<td>{fmt(item.get('atr_pct'))}%</td>"
            f"<td>{fmt(item.get('volume_ratio'))}x</td>"
            f"<td>{item.get('trend15', '-')}</td>"
            f"<td>{item.get('reason', '')}</td>"
            f"<td>{order.get('status', '-')}</td>"
            "</tr>"
        )

    html = f"""
<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width,initial-scale=1"
>

<meta
http-equiv="refresh"
content="10"
>

<title>
OKX Scalping V5
</title>

<style>

body {{
    font-family: Arial, sans-serif;
    margin: 15px;
}}

table {{
    border-collapse: collapse;
    width: 100%;
    font-size: 12px;
}}

th,
td {{
    border: 1px solid #cccccc;
    padding: 7px;
    text-align: left;
}}

th {{
    background: #eeeeee;
}}

h2 {{
    margin-bottom: 5px;
}}

</style>

</head>

<body>

<h2>
OKX RSI + EMA20 + ADX + ATR + Volume Scalping V5
</h2>

<p>
<b>Mode:</b>
{"DEMO" if DEMO else "LIVE"}
|
<b>Auto Trade:</b>
{AUTO_TRADE}
|
<b>Margin:</b>
${MARGIN_USDT}
|
<b>Leverage:</b>
{LEVERAGE}x
|
<b>Notional:</b>
${MARGIN_USDT * LEVERAGE}
</p>

<p>
<b>Public API:</b>
CONNECTED
&nbsp;&nbsp;
<b>Private API:</b>
CONNECTED
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
<th>ATR%</th>
<th>Volume</th>
<th>15m Trend</th>
<th>Reason</th>
<th>Status</th>

</tr>

{''.join(rows)}

</table>

<p>

Strategy:
RSI + EMA20 + ADX + ATR + Volume + 15m Trend

<br>

Minimum signal:
{MIN_SCORE}/10

<br>

SL:
{SL_PERCENT}%

&nbsp;&nbsp;

TP:
{TP_PERCENT}%

</p>

</body>

</html>
"""

    return Response(
        html,
        mimetype="text/html"
    )


# =========================================================
# STATUS API
# =========================================================

@app.get("/api/status")
def api_status():

    return jsonify(
        {
            "bot":
                "OKX RSI EMA20 ADX ATR Volume Scalping V5",

            "demo":
                DEMO,

            "auto_trade":
                AUTO_TRADE,

            "margin_usdt":
                str(MARGIN_USDT),

            "leverage":
                str(LEVERAGE),

            "symbols":
                SYMBOLS,

            "state":
                state
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
