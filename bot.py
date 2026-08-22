import os
import time
import json
import hmac
import base64
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# OKX DEMO SCALPING BOT V5
# RSI CROSS/RETEST + EMA20 RETEST
# ADX + ATR + VOLUME + 15M TREND
# ============================================================

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

POLL_SECONDS = int(
    os.getenv("POLL_SECONDS", "15")
)

MARGIN_USDT = Decimal(
    os.getenv("MARGIN_USDT", "20")
)

LEVERAGE = Decimal(
    os.getenv("LEVERAGE", "5")
)

SL_PERCENT = Decimal(
    os.getenv("SL_PERCENT", "0.4")
)

TP_PERCENT = Decimal(
    os.getenv("TP_PERCENT", "0.8")
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

RSI_PERIOD = 14
RSI_SLOW_PERIOD = 100
EMA_PERIOD = 20
ATR_PERIOD = 14
ADX_PERIOD = 14

RETEST_LOOKBACK = 3

TD_MODE = os.getenv(
    "TD_MODE",
    "isolated"
)

POS_SIDE = os.getenv(
    "POS_SIDE",
    "net"
)

SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,ETH-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP"
    ).split(",")
    if x.strip()
]

app = Flask(__name__)

session = requests.Session()

last_processed_candle = {}


# ============================================================
# LOGGING
# ============================================================

def log(message):

    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True
    )


def fmt(value, places=8):

    if value is None:
        return ""

    return (
        f"{Decimal(value):.{places}f}"
        .rstrip("0")
        .rstrip(".")
    )


# ============================================================
# OKX SIGNATURE
# ============================================================

def utc_timestamp():

    return (
        datetime.now(timezone.utc)
        .isoformat(
            timespec="milliseconds"
        )
        .replace(
            "+00:00",
            "Z"
        )
    )


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


# ============================================================
# PUBLIC REQUEST
# ============================================================

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
            f"OKX PUBLIC ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# ============================================================
# PRIVATE REQUEST
# ============================================================

def private_request(
    method,
    path,
    payload=None,
    params=None
):

    if not API_KEY:

        raise RuntimeError(
            "OKX_API_KEY is missing"
        )

    if not SECRET_KEY:

        raise RuntimeError(
            "OKX_SECRET_KEY is missing"
        )

    if not PASSPHRASE:

        raise RuntimeError(
            "OKX_PASSPHRASE is missing"
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
            f"OKX PRIVATE ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# ============================================================
# CANDLES
# ============================================================

def get_candles(
    symbol,
    bar,
    limit=180
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

        candles.append(
            {
                "ts":
                    int(row[0]),

                "open":
                    Decimal(row[1]),

                "high":
                    Decimal(row[2]),

                "low":
                    Decimal(row[3]),

                "close":
                    Decimal(row[4]),

                "volume":
                    Decimal(row[5]),

                "confirm":
                    row[8]
                    if len(row) > 8
                    else "1"
            }
        )

    return [
        c for c in candles
        if c["confirm"] == "1"
    ]


# ============================================================
# INSTRUMENT INFORMATION
# ============================================================

def get_instrument(symbol):

    data = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": symbol
        }
    )

    rows = data.get("data", [])

    if not rows:

        raise RuntimeError(
            f"Instrument not found: {symbol}"
        )

    row = rows[0]

    if row.get("state") != "live":

        raise RuntimeError(
            f"{symbol} is not live: "
            f"{row.get('state')}"
        )

    return {
        "ctVal":
            Decimal(row["ctVal"]),

        "lotSz":
            Decimal(row["lotSz"]),

        "minSz":
            Decimal(row["minSz"]),

        "tickSz":
            Decimal(row["tickSz"])
    }


def round_step(
    value,
    step
):

    if step <= 0:

        return value

    units = (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    )

    return units * step


# ============================================================
# EMA
# ============================================================

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


# ============================================================
# RSI
# ============================================================

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

    if avg_loss == 0:

        result[
            period
        ] = Decimal("100")

    else:

        rs = (
            avg_gain
            / avg_loss
        )

        result[
            period
        ] = (
            Decimal("100")
            -
            Decimal("100")
            / (
                Decimal("1")
                + rs
            )
        )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            avg_gain
            * Decimal(period - 1)
            +
            gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss
            * Decimal(period - 1)
            +
            losses[i]
        ) / Decimal(period)

        if avg_loss == 0:

            result[
                i + 1
            ] = Decimal("100")

        else:

            rs = (
                avg_gain
                / avg_loss
            )

            result[
                i + 1
            ] = (
                Decimal("100")
                -
                Decimal("100")
                / (
                    Decimal("1")
                    + rs
                )
            )

    return result


# ============================================================
# ATR
# ============================================================

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

        trs.append(
            max(
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
        )

    return (
        sum(
            trs[-period:],
            Decimal("0")
        )
        / Decimal(period)
    )


# ============================================================
# ADX
# ============================================================

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


# ============================================================
# 15M TREND
# ============================================================

def get_trend(
    symbol
):

    candles = get_candles(
        symbol,
        TREND_BAR,
        80
    )

    if len(candles) < 22:

        return "flat"

    closes = [
        c["close"]
        for c in candles
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
        ema20[i] >= ema20[i - 1]
    ):

        return "bull"

    if (
        closes[i] < ema20[i]
        and
        ema20[i] <= ema20[i - 1]
    ):

        return "bear"

    return "flat"


# ============================================================
# CANDLE REJECTION
# ============================================================

def bullish_rejection(
    candle,
    level
):

    return (
        candle["low"] <= level
        and
        candle["close"] > level
        and
        candle["close"] > candle["open"]
    )


def bearish_rejection(
    candle,
    level
):

    return (
        candle["high"] >= level
        and
        candle["close"] < level
        and
        candle["close"] < candle["open"]
    )


# ============================================================
# RSI CROSS + RETEST
# ============================================================

def find_rsi_retest_signal(
    candles,
    rsi14,
    rsi100
):

    i = len(candles) - 1

    for bars_ago in range(
        1,
        4
    ):

        cross_i = (
            i - bars_ago
        )

        if cross_i <= 0:

            continue

        if (
            rsi14[cross_i - 1]
            is None
            or
            rsi100[cross_i - 1]
            is None
            or
            rsi14[cross_i]
            is None
            or
            rsi100[cross_i]
            is None
        ):

            continue

        bullish_cross = (
            rsi14[cross_i - 1]
            <=
            rsi100[cross_i - 1]
            and
            rsi14[cross_i]
            >
            rsi100[cross_i]
        )

        bearish_cross = (
            rsi14[cross_i - 1]
            >=
            rsi100[cross_i - 1]
            and
            rsi14[cross_i]
            <
            rsi100[cross_i]
        )

        if bullish_cross:

            if (
                rsi14[i]
                >
                rsi100[i]
                and
                rsi14[i]
                >=
                Decimal("50")
            ):

                return (
                    "buy",
                    f"RSI_CROSS_RETEST_{bars_ago}BAR"
                )

        if bearish_cross:

            if (
                rsi14[i]
                <
                rsi100[i]
                and
                rsi14[i]
                <=
                Decimal("50")
            ):

                return (
                    "sell",
                    f"RSI_CROSS_RETEST_{bars_ago}BAR"
                )

    return None, None


# ============================================================
# EMA20 CROSS + RETEST
# ============================================================

def find_ema_retest_signal(
    candles,
    ema20
):

    i = len(candles) - 1

    for bars_ago in range(
        1,
        4
    ):

        cross_i = (
            i - bars_ago
        )

        if cross_i <= 0:

            continue

        if (
            ema20[cross_i] is None
            or
            ema20[cross_i - 1] is None
        ):

            continue

        bullish_cross = (
            candles[cross_i - 1]["close"]
            <=
            ema20[cross_i - 1]
            and
            candles[cross_i]["close"]
            >
            ema20[cross_i]
        )

        bearish_cross = (
            candles[cross_i - 1]["close"]
            >=
            ema20[cross_i - 1]
            and
            candles[cross_i]["close"]
            <
            ema20[cross_i]
        )

        if (
            bullish_cross
            and
            bullish_rejection(
                candles[i],
                ema20[i]
            )
        ):

            return (
                "buy",
                f"EMA20_RETEST_{bars_ago}BAR"
            )

        if (
            bearish_cross
            and
            bearish_rejection(
                candles[i],
                ema20[i]
            )
        ):

            return (
                "sell",
                f"EMA20_RETEST_{bars_ago}BAR"
            )

    return None, None


# ============================================================
# SIGNAL ENGINE
# ============================================================

def get_signal(
    symbol
):

    candles = get_candles(
        symbol,
        BAR,
        180
    )

    if len(candles) < 110:

        return None

    closes = [
        c["close"]
        for c in candles
    ]

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

    i = len(candles) - 1

    if (
        rsi14[i] is None
        or
        rsi100[i] is None
        or
        ema20[i] is None
        or
        atr is None
    ):

        return None

    # --------------------------------------------------------
    # PRIMARY SIGNAL:
    # RSI CROSS + RETEST
    # OR
    # EMA20 CROSS + RETEST
    # --------------------------------------------------------

    rsi_side, rsi_reason = (
        find_rsi_retest_signal(
            candles,
            rsi14,
            rsi100
        )
    )

    ema_side, ema_reason = (
        find_ema_retest_signal(
            candles,
            ema20
        )
    )

    # RSI setup gets priority.
    # If RSI setup does not exist, EMA20 retest is used.

    signal = (
        rsi_side
        if rsi_side is not None
        else ema_side
    )

    reason = (
        rsi_reason
        if rsi_reason is not None
        else ema_reason
    )

    if signal is None:

        return None

    # --------------------------------------------------------
    # VOLUME FILTER
    # --------------------------------------------------------

    average_volume = (
        sum(
            (
                c["volume"]
                for c in candles[-21:-1]
            ),
            Decimal("0")
        )
        /
        Decimal("20")
    )

    volume_ok = (
        candles[i]["volume"]
        >=
        average_volume
        *
        VOLUME_MULT
    )

    # --------------------------------------------------------
    # ATR FILTER
    # --------------------------------------------------------

    atr_percent = (
        atr
        /
        closes[i]
        *
        Decimal("100")
    )

    atr_ok = (
        atr_percent
        >=
        ATR_MIN_PCT
    )

    # --------------------------------------------------------
    # ADX FILTER
    # --------------------------------------------------------

    adx_ok = (
        adx
        >=
        ADX_MIN
    )

    # --------------------------------------------------------
    # 15M TREND FILTER
    # --------------------------------------------------------

    trend = get_trend(
        symbol
    )

    trend_ok = True

    if (
        trend == "bull"
        and
        signal != "buy"
    ):

        trend_ok = False

    if (
        trend == "bear"
        and
        signal != "sell"
    ):

        trend_ok = False

    # --------------------------------------------------------
    # ALL FILTERS MUST PASS
    # --------------------------------------------------------

    if not adx_ok:

        return None

    if not volume_ok:

        return None

    if not atr_ok:

        return None

    if not trend_ok:

        return None

    entry = closes[i]

    if signal == "buy":

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

    else:

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

    return {

        "signal":
            signal,

        "reason":
            reason,

        "entry":
            entry,

        "sl":
            sl,

        "tp":
            tp,

        "rsi14":
            rsi14[i],

        "rsi100":
            rsi100[i],

        "ema20":
            ema20[i],

        "adx":
            adx,

        "atr":
            atr,

        "atr_percent":
            atr_percent,

        "volume":
            candles[i]["volume"],

        "average_volume":
            average_volume,

        "trend15":
            trend
    }


# ============================================================
# LEVERAGE
# ============================================================

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

    if POS_SIDE != "net":

        payload[
            "posSide"
        ] = POS_SIDE

    try:

        return private_request(
            "POST",
            "/api/v5/account/set-leverage",
            payload
        )

    except Exception as error:

        log(
            f"{symbol}: "
            f"leverage warning: "
            f"{error}"
        )

        return None


# ============================================================
# OPEN POSITION CHECK
# ============================================================

def get_open_position(
    symbol
):

    data = private_request(
        "GET",
        "/api/v5/account/positions",
        params={
            "instType":
                "SWAP",

            "instId":
                symbol
        }
    )

    for row in data.get(
        "data",
        []
    ):

        pos = Decimal(
            row.get(
                "pos",
                "0"
            )
        )

        if pos != 0:

            return row

    return None


# ============================================================
# CONTRACT SIZE
# ============================================================

def calculate_contract_size(
    symbol,
    entry
):

    info = get_instrument(
        symbol
    )

    target_notional = (
        MARGIN_USDT
        *
        LEVERAGE
    )

    raw_contracts = (
        target_notional
        /
        (
            info["ctVal"]
            *
            entry
        )
    )

    size = round_step(
        raw_contracts,
        info["lotSz"]
    )

    if size < info["minSz"]:

        size = info["minSz"]

    return (
        size,
        info
    )


# ============================================================
# ORDER
# ============================================================

def place_order(
    symbol,
    signal
):

    if not DEMO:

        return {
            "status":
                "blocked",

            "message":
                "This version requires DEMO mode."
        }

    if not AUTO_TRADE:

        return {
            "status":
                "blocked",

            "message":
                "AUTO_TRADE=false"
        }

    existing = get_open_position(
        symbol
    )

    if existing:

        return {

            "status":
                "blocked",

            "message":
                "Existing position detected. "
                "Duplicate trade prevented.",

            "position":
                existing.get("pos")
        }

    entry = signal["entry"]

    sl = signal["sl"]

    tp = signal["tp"]

    size, info = (
        calculate_contract_size(
            symbol,
            entry
        )
    )

    set_leverage(
        symbol
    )

    side = (
        "buy"
        if signal["signal"] == "buy"
        else
        "sell"
    )

    client_id = (
        "rsiema"
        +
        str(
            int(
                time.time()
                * 1000
            )
        )[-20:]
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
            client_id,

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

    if POS_SIDE != "net":

        payload[
            "posSide"
        ] = (
            "long"
            if signal["signal"] == "buy"
            else
            "short"
        )

    log(
        f"ORDER SUBMIT | "
        f"{symbol} | "
        f"{side.upper()} | "
        f"contracts={size} | "
        f"entry≈{entry} | "
        f"SL={sl} | "
        f"TP={tp}"
    )

    result = private_request(
        "POST",
        "/api/v5/trade/order",
        payload
    )

    log(
        "ORDER RESPONSE | "
        +
        json.dumps(
            result,
            default=str
        )
    )

    return {

        "status":
            "submitted",

        "symbol":
            symbol,

        "side":
            side,

        "contracts":
            str(size),

        "entry":
            str(entry),

        "sl":
            str(sl),

        "tp":
            str(tp),

        "okx":
            result
    }


# ============================================================
# CONNECTION TEST
# ============================================================

def test_okx():

    public_get(
        "/api/v5/market/ticker",
        {
            "instId":
                "BTC-USDT-SWAP"
        }
    )

    log(
        "OKX MARKET CONNECTED"
    )

    if (
        API_KEY
        and
        SECRET_KEY
        and
        PASSPHRASE
    ):

        private_request(
            "GET",
            "/api/v5/account/balance"
        )

        log(
            "OKX PRIVATE API CONNECTED"
        )

    else:

        log(
            "WARNING: "
            "API credentials are missing"
        )


# ============================================================
# PROCESS SYMBOL
# ============================================================

def process_symbol(
    symbol
):

    candles = get_candles(
        symbol,
        BAR,
        180
    )

    if not candles:

        return

    candle_time = (
        candles[-1]["ts"]
    )

    if (
        last_processed_candle.get(
            symbol
        )
        ==
        candle_time
    ):

        return

    last_processed_candle[
        symbol
    ] = candle_time

    log(
        f"CHECKING SIGNAL | "
        f"{symbol}"
    )

    signal = get_signal(
        symbol
    )

    if signal is None:

        log(
            f"{symbol}: "
            f"NO VALID SIGNAL "
            f"(waiting for RSI/EMA "
            f"retest + filters)"
        )

        return

    log(
        f"*** VALID SIGNAL *** "
        f"{symbol} "
        f"{signal['signal'].upper()} | "
        f"reason={signal['reason']} | "
        f"entry={signal['entry']} | "
        f"SL={signal['sl']} | "
        f"TP={signal['tp']} | "
        f"ADX={signal['adx']} | "
        f"ATR%={signal['atr_percent']} | "
        f"Trend15={signal['trend15']}"
    )

    try:

        result = place_order(
            symbol,
            signal
        )

        log(
            f"TRADE RESULT | "
            f"{symbol} | "
            +
            json.dumps(
                result,
                default=str
            )
        )

    except Exception as error:

        log(
            f"ORDER ERROR | "
            f"{symbol} | "
            f"{type(error).__name__}: "
            f"{error}"
        )


# ============================================================
# WORKER
# ============================================================

def worker():

    log(
        "=============================================="
    )

    log(
        "OKX RSI + EMA20 RETEST "
        "SCALPING V5 STARTED"
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
        f"SL={SL_PERCENT}% "
        f"| TP={TP_PERCENT}%"
    )

    log(
        f"ADX_MIN={ADX_MIN}"
    )

    log(
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "=============================================="
    )

    try:

        test_okx()

    except Exception as error:

        log(
            "OKX CONNECTION ERROR: "
            f"{type(error).__name__}: "
            f"{error}"
        )

    while True:

        for symbol in SYMBOLS:

            try:

                process_symbol(
                    symbol
                )

            except Exception as error:

                log(
                    f"{symbol} LOOP ERROR: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

        time.sleep(
            POLL_SECONDS
        )


# ============================================================
# WEB
# ============================================================

@app.get("/")
def home():

    return jsonify(
        {
            "bot":
                "OKX RSI EMA20 RETEST Scalping V5",

            "status":
                "running",

            "demo":
                DEMO,

            "auto_trade":
                AUTO_TRADE,

            "margin_usdt":
                str(MARGIN_USDT),

            "leverage":
                str(LEVERAGE),

            "sl_percent":
                str(SL_PERCENT),

            "tp_percent":
                str(TP_PERCENT),

            "timeframe":
                BAR,

            "trend_timeframe":
                TREND_BAR,

            "symbols":
                SYMBOLS
        }
    )


@app.get("/health")
def health():

    return jsonify(
        {
            "status":
                "ok"
        }
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    import threading

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    threading.Thread(
        target=worker,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=port
    )
