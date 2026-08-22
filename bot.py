import os
import time
import json
import hmac
import base64
import hashlib
from decimal import Decimal
from datetime import datetime, timezone

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
)

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

DEMO = os.getenv(
    "OKX_DEMO",
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

SL_PERCENT = Decimal(
    os.getenv("SL_PERCENT", "0.4")
)

TP_PERCENT = Decimal(
    os.getenv("TP_PERCENT", "0.8")
)

POLL_SECONDS = int(
    os.getenv("POLL_SECONDS", "15")
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
    "isolated"
)

SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,ETH-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP"
    ).split(",")
    if x.strip()
]

# =========================================================
# APP
# =========================================================

app = Flask(__name__)

session = requests.Session()

last_candle = {}


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
# OKX SIGNATURE
# =========================================================

def utc_timestamp():

    return datetime.now(
        timezone.utc
    ).isoformat(
        timespec="milliseconds"
    ).replace(
        "+00:00",
        "Z"
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


# =========================================================
# PUBLIC OKX REQUEST
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
            f"OKX PUBLIC ERROR: "
            f"{data.get('code')} "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# PRIVATE OKX REQUEST
# =========================================================

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
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": create_signature(
            timestamp,
            method,
            path,
            body
        ),
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "OK-ACCESS-TIMESTAMP": timestamp
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
            f"OKX PRIVATE ERROR: "
            f"{data.get('code')} "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# MARKET DATA
# =========================================================

def get_candles(
    symbol,
    bar="5m",
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

        candles.append(
            {
                "ts": int(row[0]),
                "open": Decimal(row[1]),
                "high": Decimal(row[2]),
                "low": Decimal(row[3]),
                "close": Decimal(row[4]),
                "volume": Decimal(row[5]),
                "confirm": (
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
            + value
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
            - Decimal("100")
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
            * (period - 1)
            + gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss
            * (period - 1)
            + losses[i]
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
                - Decimal("100")
                / (
                    Decimal("1")
                    + rs
                )
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
# SIMPLE ADX
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
            - candles[i - 1]["high"]
        )

        down_move = (
            candles[i - 1]["low"]
            - candles[i]["low"]
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
            - candles[i]["low"]
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

    i = len(
        closes
    ) - 1

    if (
        ema20[i] is None
        or ema20[i - 1] is None
    ):

        return "flat"

    if (
        closes[i] > ema20[i]
        and ema20[i]
        > ema20[i - 1]
    ):

        return "bull"

    if (
        closes[i] < ema20[i]
        and ema20[i]
        < ema20[i - 1]
    ):

        return "bear"

    return "flat"


# =========================================================
# SIGNAL ENGINE
# =========================================================

def get_signal(
    symbol
):

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

        log(
            f"{symbol}: "
            f"Not enough candles"
        )

        return None

    closes = [
        x["close"]
        for x in candles
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

    i = len(
        candles
    ) - 1

    if (
        rsi14[i] is None
        or rsi100[i] is None
        or ema20[i] is None
        or atr is None
    ):

        return None

    buy_rsi = (
        rsi14[i - 1]
        <= rsi100[i - 1]
        and
        rsi14[i]
        > rsi100[i]
    )

    sell_rsi = (
        rsi14[i - 1]
        >= rsi100[i - 1]
        and
        rsi14[i]
        < rsi100[i]
    )

    buy_ema = (
        closes[i - 1]
        <= ema20[i - 1]
        and
        closes[i]
        > ema20[i]
        and
        rsi14[i] <= 55
    )

    sell_ema = (
        closes[i - 1]
        >= ema20[i - 1]
        and
        closes[i]
        < ema20[i]
        and
        rsi14[i] >= 45
    )

    signal = None
    reason = None

    if buy_rsi:

        signal = "buy"
        reason = "RSI_CROSS"

    elif sell_rsi:

        signal = "sell"
        reason = "RSI_CROSS"

    elif buy_ema:

        signal = "buy"
        reason = "EMA20"

    elif sell_ema:

        signal = "sell"
        reason = "EMA20"

    if signal is None:

        return None

    average_volume = (
        sum(
            x["volume"]
            for x in candles[-21:-1]
        )
        / Decimal("20")
    )

    volume_ok = (
        candles[i]["volume"]
        >=
        average_volume
        * VOLUME_MULT
    )

    atr_percent = (
        atr
        / closes[i]
        * Decimal("100")
    )

    if adx < ADX_MIN:

        return None

    if not volume_ok:

        return None

    if atr_percent < ATR_MIN_PCT:

        return None

    trend = get_trend(
        symbol
    )

    if (
        trend == "bull"
        and signal != "buy"
    ):

        return None

    if (
        trend == "bear"
        and signal != "sell"
    ):

        return None

    return {
        "signal": signal,
        "reason": reason,
        "entry": closes[i],
        "rsi14": rsi14[i],
        "rsi100": rsi100[i],
        "ema20": ema20[i],
        "adx": adx,
        "atr": atr,
        "trend15": trend
    }


# =========================================================
# OKX CONNECTION TEST
# =========================================================

def test_okx():

    log("Testing OKX market connection...")

    data = public_get(
        "/api/v5/market/ticker",
        {
            "instId":
            "BTC-USDT-SWAP"
        }
    )

    if data.get("data"):

        price = data[
            "data"
        ][0].get(
            "last"
        )

        log(
            "OKX MARKET CONNECTED | "
            f"BTC price={price}"
        )

    if API_KEY and SECRET_KEY and PASSPHRASE:

        account = private_request(
            "GET",
            "/api/v5/account/balance"
        )

        log(
            "OKX PRIVATE API CONNECTED"
        )

        return account

    log(
        "WARNING: OKX API credentials "
        "are missing"
    )

    return None


# =========================================================
# ORDER
# =========================================================

def place_demo_order(
    symbol,
    signal
):

    log(
        f"ORDER SIGNAL: "
        f"{symbol} "
        f"{signal['signal'].upper()} "
        f"Entry={signal['entry']} "
        f"SL={SL_PERCENT}% "
        f"TP={TP_PERCENT}%"
    )

    if not DEMO:

        log(
            "LIVE TRADING IS DISABLED "
            "IN THIS VERSION"
        )

        return {
            "status": "blocked",
            "message": (
                "DEMO mode required"
            )
        }

    if (
        not API_KEY
        or not SECRET_KEY
        or not PASSPHRASE
    ):

        return {
            "status": "blocked",
            "message": (
                "OKX API credentials missing"
            )
        }

    log(
        "Demo signal detected. "
        "Order execution requires "
        "contract-size verification "
        "before live API order placement."
    )

    return {
        "status": "signal",
        "symbol": symbol,
        "side": signal["signal"],
        "entry": str(
            signal["entry"]
        ),
        "sl_percent": str(
            SL_PERCENT
        ),
        "tp_percent": str(
            TP_PERCENT
        )
    }


# =========================================================
# BOT WORKER
# =========================================================

def worker():

    log(
        "===================================="
    )

    log(
        "OKX RSI + EMA20 + ADX + ATR "
        "+ Volume V3 STARTED"
    )

    log(
        f"DEMO={DEMO}"
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
        "===================================="
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

        log(
            "BOT LOOP: checking market..."
        )

        for symbol in SYMBOLS:

            try:

                log(
                    f"CHECKING {symbol}"
                )

                candles = get_candles(
                    symbol,
                    BAR,
                    160
                )

                confirmed = [
                    x for x in candles
                    if x["confirm"] == "1"
                ]

                log(
                    f"{symbol}: "
                    f"{len(confirmed)} "
                    f"confirmed candles"
                )

                if not confirmed:

                    continue

                candle_time = (
                    confirmed[-1]["ts"]
                )

                if (
                    last_candle.get(symbol)
                    == candle_time
                ):

                    continue

                last_candle[
                    symbol
                ] = candle_time

                signal = get_signal(
                    symbol
                )

                if signal:

                    log(
                        f"*** SIGNAL *** "
                        f"{symbol} "
                        f"{signal['signal'].upper()} "
                        f"Reason={signal['reason']} "
                        f"ADX={signal['adx']} "
                        f"Trend15={signal['trend15']} "
                        f"Entry={signal['entry']}"
                    )

                    result = place_demo_order(
                        symbol,
                        signal
                    )

                    log(
                        json.dumps(
                            result,
                            default=str
                        )
                    )

                else:

                    log(
                        f"{symbol}: "
                        f"No valid signal"
                    )

            except Exception as error:

                log(
                    f"{symbol} ERROR: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

        time.sleep(
            POLL_SECONDS
        )


# =========================================================
# WEB ENDPOINTS
# =========================================================

@app.get("/")
def home():

    return jsonify(
        {
            "bot":
            "OKX RSI EMA20 ADX ATR Volume Scalping V3",
            "status":
            "running",
            "demo":
            DEMO,
            "margin_usdt":
            str(MARGIN_USDT),
            "leverage":
            str(LEVERAGE),
            "timeframe":
            BAR,
            "trend_timeframe":
            TREND_BAR
        }
    )


@app.get("/health")
def health():

    return jsonify(
        {
            "status":
            "healthy",
            "demo":
            DEMO,
            "api_key_present":
            bool(API_KEY),
            "secret_present":
            bool(SECRET_KEY),
            "passphrase_present":
            bool(PASSPHRASE),
            "margin_usdt":
            str(MARGIN_USDT),
            "leverage":
            str(LEVERAGE),
            "symbols":
            SYMBOLS
        }
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    import threading

    thread = threading.Thread(
        target=worker,
        daemon=True
    )

    thread.start()

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    log(
        f"WEB SERVER STARTING "
        f"ON PORT {port}"
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
