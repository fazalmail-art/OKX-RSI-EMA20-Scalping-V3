import os
import time
import json
import hmac
import base64
import hashlib
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template_string
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://www.okx.com"
)

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

# SAFETY: DEMO ONLY
DEMO = os.getenv(
    "OKX_DEMO",
    "true"
).lower() == "true"

LIVE_TRADING = False

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

MIN_SCORE = int(
    os.getenv("MIN_SCORE", "7")
)

ADX_MIN = Decimal(
    os.getenv("ADX_MIN", "18")
)

VOLUME_MULT = Decimal(
    os.getenv("VOLUME_MULT", "0.80")
)

ATR_MIN_PCT = Decimal(
    os.getenv("ATR_MIN_PCT", "0.05")
)

TD_MODE = os.getenv(
    "TD_MODE",
    "isolated"
)

# =========================================================
# PAIRS
# =========================================================

SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "SOL-USDT-SWAP",
    "XAU-USDT-SWAP"
]

# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)

session = requests.Session()

last_candle = {}
last_trade_candle = {}

dashboard = {}

bot_started = False
private_api_ok = False
public_api_ok = False

last_error = ""
last_activity = "Starting..."

lock = threading.Lock()


# =========================================================
# HELPERS
# =========================================================

def now_string():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def log(message):
    global last_activity

    last_activity = str(message)

    print(
        f"[{now_string()}] {message}",
        flush=True
    )


def D(value, default="0"):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def decimal_string(value):
    if value is None:
        return "0"

    value = D(value)

    return format(
        value,
        "f"
    )


def round_down(value, step):
    value = D(value)
    step = D(step)

    if step <= 0:
        return value

    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step


# =========================================================
# OKX SIGNATURE
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
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    if data.get("code") != "0":
        raise RuntimeError(
            "OKX PUBLIC ERROR: "
            + str(data.get("code"))
            + " "
            + str(data.get("msg"))
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
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    if data.get("code") != "0":
        raise RuntimeError(
            "OKX PRIVATE ERROR: "
            + str(data.get("code"))
            + " "
            + str(data.get("msg"))
        )

    return data


# =========================================================
# INSTRUMENT INFORMATION
# =========================================================

instrument_cache = {}


def get_instrument(symbol):

    if symbol in instrument_cache:
        return instrument_cache[symbol]

    data = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": symbol
        }
    )

    rows = data.get(
        "data",
        []
    )

    if not rows:
        raise RuntimeError(
            f"Instrument unavailable: {symbol}"
        )

    item = rows[0]

    if item.get("state") != "live":
        raise RuntimeError(
            f"{symbol} is not live"
        )

    instrument_cache[
        symbol
    ] = item

    return item


# =========================================================
# CANDLES
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

    for i in range(
        period - 1,
        len(gains)
    ):
        if i >= period:
            avg_gain = (
                (
                    avg_gain
                    * Decimal(period - 1)
                )
                + gains[i]
            ) / Decimal(period)

            avg_loss = (
                (
                    avg_loss
                    * Decimal(period - 1)
                )
                + losses[i]
            ) / Decimal(period)

        if avg_loss == 0:
            result[i + 1] = Decimal("100")
        else:
            rs = (
                avg_gain
                / avg_loss
            )

            result[i + 1] = (
                Decimal("100")
                -
                (
                    Decimal("100")
                    /
                    (
                        Decimal("1")
                        + rs
                    )
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
# ADX / DIRECTION
# =========================================================

def calculate_adx(
    candles,
    period=14
):

    if len(candles) < period + 2:
        return (
            Decimal("0"),
            Decimal("0"),
            Decimal("0")
        )

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

        total_range += max(
            candles[i]["high"]
            - candles[i]["low"],
            Decimal("0")
        )

    if total_range == 0:
        return (
            Decimal("0"),
            Decimal("0"),
            Decimal("0")
        )

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
        return (
            Decimal("0"),
            plus_di,
            minus_di
        )

    adx = (
        abs(
            plus_di
            - minus_di
        )
        / total
        * Decimal("100")
    )

    return (
        adx,
        plus_di,
        minus_di
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
        or ema20[i - 1] is None
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
# SIGNAL ENGINE
# =========================================================

def get_signal(
    symbol
):

    candles = get_candles(
        symbol,
        BAR,
        180
    )

    candles = [
        x for x in candles
        if x["confirm"] == "1"
    ]

    if len(candles) < 105:
        return {
            "signal": "none",
            "score": 0,
            "reason":
                "Not enough candles"
        }

    closes = [
        x["close"]
        for x in candles
    ]

    volumes = [
        x["volume"]
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

    adx, plus_di, minus_di = (
        calculate_adx(
            candles,
            14
        )
    )

    i = len(candles) - 1

    if (
        rsi14[i] is None
        or rsi100[i] is None
        or ema20[i] is None
        or atr is None
    ):
        return {
            "signal": "none",
            "score": 0,
            "reason":
                "Indicator data unavailable"
        }

    price = closes[i]

    previous_price = closes[i - 1]

    # -----------------------------------------------------
    # RSI CROSS
    # -----------------------------------------------------

    buy_rsi_cross = (
        rsi14[i - 1]
        <= rsi100[i - 1]
        and
        rsi14[i]
        > rsi100[i]
    )

    sell_rsi_cross = (
        rsi14[i - 1]
        >= rsi100[i - 1]
        and
        rsi14[i]
        < rsi100[i]
    )

    # -----------------------------------------------------
    # EMA RETEST
    # -----------------------------------------------------

    ema_distance = (
        abs(
            price - ema20[i]
        )
        / price
        * Decimal("100")
    )

    ema_retest_zone = (
        ema_distance
        <= Decimal("0.20")
    )

    buy_ema_retest = (
        previous_price
        <= ema20[i - 1]
        and
        price > ema20[i]
    )

    sell_ema_retest = (
        previous_price
        >= ema20[i - 1]
        and
        price < ema20[i]
    )

    buy_ema_bullish = (
        price > ema20[i]
        and
        rsi14[i] >= Decimal("50")
    )

    sell_ema_bearish = (
        price < ema20[i]
        and
        rsi14[i] <= Decimal("50")
    )

    # -----------------------------------------------------
    # RSI DIRECTION
    # -----------------------------------------------------

    rsi_bullish = (
        rsi14[i] > Decimal("50")
        and
        rsi14[i] > rsi14[i - 1]
    )

    rsi_bearish = (
        rsi14[i] < Decimal("50")
        and
        rsi14[i] < rsi14[i - 1]
    )

    # -----------------------------------------------------
    # EMA SLOPE
    # -----------------------------------------------------

    ema_bullish = (
        ema20[i]
        > ema20[i - 1]
    )

    ema_bearish = (
        ema20[i]
        < ema20[i - 1]
    )

    # -----------------------------------------------------
    # VOLUME
    # -----------------------------------------------------

    average_volume = (
        sum(
            volumes[-21:-1],
            Decimal("0")
        )
        / Decimal("20")
    )

    volume_ratio = (
        volumes[i]
        / average_volume
        if average_volume > 0
        else Decimal("0")
    )

    volume_ok = (
        volume_ratio
        >= VOLUME_MULT
    )

    # -----------------------------------------------------
    # ATR
    # -----------------------------------------------------

    atr_percent = (
        atr
        / price
        * Decimal("100")
    )

    atr_ok = (
        atr_percent
        >= ATR_MIN_PCT
    )

    # -----------------------------------------------------
    # 15M TREND
    # -----------------------------------------------------

    trend = get_trend(
        symbol
    )

    # -----------------------------------------------------
    # SCORE
    # -----------------------------------------------------

    buy_score = 0
    sell_score = 0

    buy_reasons = []
    sell_reasons = []

    # RSI cross
    if buy_rsi_cross:
        buy_score += 2
        buy_reasons.append(
            "RSI bullish crossover"
        )

    if sell_rsi_cross:
        sell_score += 2
        sell_reasons.append(
            "RSI bearish crossover"
        )

    # EMA
    if buy_ema_retest:
        buy_score += 2
        buy_reasons.append(
            "EMA20 bullish retest"
        )
    elif buy_ema_bullish:
        buy_score += 1
        buy_reasons.append(
            "Price above EMA20"
        )

    if sell_ema_retest:
        sell_score += 2
        sell_reasons.append(
            "EMA20 bearish retest"
        )
    elif sell_ema_bearish:
        sell_score += 1
        sell_reasons.append(
            "Price below EMA20"
        )

    # RSI direction
    if rsi_bullish:
        buy_score += 1
        buy_reasons.append(
            "RSI bullish"
        )

    if rsi_bearish:
        sell_score += 1
        sell_reasons.append(
            "RSI bearish"
        )

    # EMA slope
    if ema_bullish:
        buy_score += 1
        buy_reasons.append(
            "EMA slope bullish"
        )

    if ema_bearish:
        sell_score += 1
        sell_reasons.append(
            "EMA slope bearish"
        )

    # ADX
    adx_ok = (
        adx >= ADX_MIN
    )

    if adx_ok:
        if plus_di >= minus_di:
            buy_score += 1
            buy_reasons.append(
                "ADX bullish direction"
            )
        else:
            sell_score += 1
            sell_reasons.append(
                "ADX bearish direction"
            )

    # Volume
    if volume_ok:
        if buy_score >= sell_score:
            buy_score += 1
            buy_reasons.append(
                "Volume confirmed"
            )
        else:
            sell_score += 1
            sell_reasons.append(
                "Volume confirmed"
            )

    # ATR
    if atr_ok:
        if buy_score >= sell_score:
            buy_score += 1
            buy_reasons.append(
                "ATR volatility OK"
            )
        else:
            sell_score += 1
            sell_reasons.append(
                "ATR volatility OK"
            )

    # 15m trend
    if trend == "bull":
        buy_score += 1
        buy_reasons.append(
            "15m bullish trend"
        )

    elif trend == "bear":
        sell_score += 1
        sell_reasons.append(
            "15m bearish trend"
        )

    # -----------------------------------------------------
    # DETERMINE DIRECTION
    # -----------------------------------------------------

    signal = "none"
    score = max(
        buy_score,
        sell_score
    )

    reasons = []

    if (
        buy_score >= MIN_SCORE
        and
        buy_score > sell_score
    ):
        signal = "buy"
        score = buy_score
        reasons = buy_reasons

    elif (
        sell_score >= MIN_SCORE
        and
        sell_score > buy_score
    ):
        signal = "sell"
        score = sell_score
        reasons = sell_reasons

    else:

        if (
            buy_score
            >= sell_score
        ):
            reasons = buy_reasons
        else:
            reasons = sell_reasons

        if not reasons:
            reasons = [
                "Waiting for confirmation"
            ]

    # -----------------------------------------------------
    # TREND FILTER
    # -----------------------------------------------------

    if signal == "buy" and trend == "bear":
        signal = "none"
        reasons.append(
            "Blocked by 15m bearish trend"
        )

    if signal == "sell" and trend == "bull":
        signal = "none"
        reasons.append(
            "Blocked by 15m bullish trend"
        )

    # -----------------------------------------------------
    # FILTER INFORMATION
    # -----------------------------------------------------

    if not adx_ok:
        reasons.append(
            f"ADX below {ADX_MIN}"
        )

    if not volume_ok:
        reasons.append(
            "Volume filter not confirmed"
        )

    if not atr_ok:
        reasons.append(
            "ATR too low"
        )

    if ema_retest_zone:
        reasons.append(
            "Near EMA20"
        )

    return {
        "signal":
            signal,
        "score":
            score,
        "buy_score":
            buy_score,
        "sell_score":
            sell_score,
        "reason":
            " | ".join(reasons[-6:]),
        "entry":
            price,
        "rsi14":
            rsi14[i],
        "rsi100":
            rsi100[i],
        "ema20":
            ema20[i],
        "adx":
            adx,
        "plus_di":
            plus_di,
        "minus_di":
            minus_di,
        "atr":
            atr,
        "atr_percent":
            atr_percent,
        "volume_ratio":
            volume_ratio,
        "trend15":
            trend,
        "buy_rsi_cross":
            buy_rsi_cross,
        "sell_rsi_cross":
            sell_rsi_cross,
        "buy_ema_retest":
            buy_ema_retest,
        "sell_ema_retest":
            sell_ema_retest,
        "ema_retest_zone":
            ema_retest_zone,
        "volume_ok":
            volume_ok,
        "adx_ok":
            adx_ok,
        "atr_ok":
            atr_ok
    }


# =========================================================
# POSITION SIZE
# =========================================================

def calculate_contract_size(
    symbol,
    price
):

    info = get_instrument(
        symbol
    )

    ct_val = D(
        info.get("ctVal"),
        "0"
    )

    lot_sz = D(
        info.get("lotSz"),
        "1"
    )

    min_sz = D(
        info.get("minSz"),
        "1"
    )

    if ct_val <= 0:
        raise RuntimeError(
            f"{symbol}: invalid ctVal"
        )

    notional = (
        MARGIN_USDT
        * LEVERAGE
    )

    contracts = (
        notional
        /
        (
            price
            * ct_val
        )
    )

    contracts = round_down(
        contracts,
        lot_sz
    )

    if contracts < min_sz:
        contracts = min_sz

    return contracts, info


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
            str(LEVERAGE),
        "mgnMode":
            TD_MODE
    }

    return private_request(
        "POST",
        "/api/v5/account/set-leverage",
        payload
    )


# =========================================================
# PLACE DEMO ORDER
# =========================================================

def place_demo_order(
    symbol,
    signal
):

    global last_error

    if not DEMO:
        return {
            "status":
                "blocked",
            "message":
                "DEMO mode is required"
        }

    if LIVE_TRADING:
        return {
            "status":
                "blocked",
            "message":
                "LIVE_TRADING safety lock"
        }

    if not API_KEY:
        return {
            "status":
                "blocked",
            "message":
                "API key missing"
        }

    entry = D(
        signal["entry"]
    )

    side = signal["signal"]

    # -----------------------------------------------------
    # CONTRACT SIZE
    # -----------------------------------------------------

    try:

        contracts, info = (
            calculate_contract_size(
                symbol,
                entry
            )
        )

    except Exception as error:

        last_error = str(error)

        return {
            "status":
                "error",
            "message":
                str(error)
        }

    # -----------------------------------------------------
    # SL / TP
    # -----------------------------------------------------

    if side == "buy":

        sl = (
            entry
            * (
                Decimal("1")
                - SL_PERCENT
                / Decimal("100")
            )
        )

        tp = (
            entry
            * (
                Decimal("1")
                + TP_PERCENT
                / Decimal("100")
            )
        )

        order_side = "buy"

    else:

        sl = (
            entry
            * (
                Decimal("1")
                + SL_PERCENT
                / Decimal("100")
            )
        )

        tp = (
            entry
            * (
                Decimal("1")
                - TP_PERCENT
                / Decimal("100")
            )
        )

        order_side = "sell"

    tick_sz = D(
        info.get("tickSz"),
        "0.0001"
    )

    sl = round_down(
        sl,
        tick_sz
    )

    tp = round_down(
        tp,
        tick_sz
    )

    # -----------------------------------------------------
    # LEVERAGE
    # -----------------------------------------------------

    try:
        set_leverage(
            symbol
        )
    except Exception as error:

        log(
            f"{symbol}: "
            f"Leverage setup warning: "
            f"{error}"
        )

    # -----------------------------------------------------
    # ORDER
    # -----------------------------------------------------

    client_id = (
        "RSIV4"
        + str(
            int(
                time.time()
            )
        )
    )[:32]

    attach_algo = {
        "attachAlgoClOrdId":
            client_id + "A",
        "tpTriggerPx":
            decimal_string(tp),
        "tpOrdPx":
            "-1",
        "tpTriggerPxType":
            "last",
        "slTriggerPx":
            decimal_string(sl),
        "slOrdPx":
            "-1",
        "slTriggerPxType":
            "last"
    }

    payload = {
        "instId":
            symbol,
        "tdMode":
            TD_MODE,
        "side":
            order_side,
        "ordType":
            "market",
        "sz":
            decimal_string(contracts),
        "clOrdId":
            client_id,
        "attachAlgoOrds":
            [
                attach_algo
            ]
    }

    log(
        f"{symbol}: "
        f"PLACING DEMO {side.upper()} "
        f"contracts={contracts} "
        f"entry={entry} "
        f"SL={sl} "
        f"TP={tp}"
    )

    try:

        result = private_request(
            "POST",
            "/api/v5/trade/order",
            payload
        )

        return {
            "status":
                "submitted",
            "symbol":
                symbol,
            "side":
                side,
            "contracts":
                str(contracts),
            "entry":
                str(entry),
            "sl":
                str(sl),
            "tp":
                str(tp),
            "okx":
                result
        }

    except Exception as error:

        last_error = str(error)

        log(
            f"{symbol}: "
            f"ORDER ERROR: "
            f"{error}"
        )

        return {
            "status":
                "error",
            "symbol":
                symbol,
            "message":
                str(error)
        }


# =========================================================
# OKX CONNECTION
# =========================================================

def test_okx():

    global public_api_ok
    global private_api_ok
    global last_error

    public_api_ok = False
    private_api_ok = False

    log(
        "OKX public market connection..."
    )

    try:

        data = public_get(
            "/api/v5/market/ticker",
            {
                "instId":
                    "BTC-USDT-SWAP"
            }
        )

        if data.get("data"):

            price = (
                data["data"][0]
                .get("last")
            )

            public_api_ok = True

            log(
                "OKX MARKET CONNECTED | "
                f"BTC={price}"
            )

    except Exception as error:

        last_error = str(error)

        log(
            "OKX PUBLIC ERROR: "
            f"{error}"
        )

    # -----------------------------------------------------

    if (
        API_KEY
        and SECRET_KEY
        and PASSPHRASE
    ):

        try:

            private_request(
                "GET",
                "/api/v5/account/balance"
            )

            private_api_ok = True

            log(
                "OKX PRIVATE API CONNECTED"
            )

        except Exception as error:

            last_error = str(error)

            log(
                "OKX PRIVATE API ERROR: "
                f"{error}"
            )

    else:

        last_error = (
            "API credentials missing"
        )

        log(
            "OKX PRIVATE API NOT CONNECTED"
        )


# =========================================================
# DASHBOARD UPDATE
# =========================================================

def update_dashboard(
    symbol,
    data
):

    with lock:

        dashboard[
            symbol
        ] = {
            "symbol":
                symbol,
            **{
                k:
                    (
                        str(v)
                        if isinstance(
                            v,
                            Decimal
                        )
                        else v
                    )
                for k, v in data.items()
            },
            "updated":
                now_string()
        }


# =========================================================
# BOT WORKER
# =========================================================

def worker():

    global bot_started

    bot_started = True

    log(
        "================================"
    )

    log(
        "OKX RSI EMA20 ADX ATR "
        "VOLUME SCALPING V4"
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
        f"NOTIONAL=${MARGIN_USDT * LEVERAGE}"
    )

    log(
        f"TIMEFRAME={BAR}"
    )

    log(
        f"TREND={TREND_BAR}"
    )

    log(
        f"MIN SCORE={MIN_SCORE}/10"
    )

    log(
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "================================"
    )

    test_okx()

    while True:

        for symbol in SYMBOLS:

            try:

                log(
                    f"CHECKING {symbol}"
                )

                # -----------------------------------------
                # INSTRUMENT CHECK
                # -----------------------------------------

                try:

                    info = get_instrument(
                        symbol
                    )

                except Exception as error:

                    update_dashboard(
                        symbol,
                        {
                            "signal":
                                "unavailable",
                            "score":
                                0,
                            "reason":
                                str(error),
                            "status":
                                "ERROR"
                        }
                    )

                    log(
                        f"{symbol}: "
                        f"INSTRUMENT ERROR "
                        f"{error}"
                    )

                    continue

                # -----------------------------------------
                # CANDLE DATA
                # -----------------------------------------

                candles = get_candles(
                    symbol,
                    BAR,
                    180
                )

                confirmed = [
                    x for x in candles
                    if x["confirm"] == "1"
                ]

                if len(confirmed) < 105:

                    update_dashboard(
                        symbol,
                        {
                            "signal":
                                "none",
                            "score":
                                0,
                            "reason":
                                "Not enough candles",
                            "status":
                                "WAITING"
                        }
                    )

                    continue

                candle_time = (
                    confirmed[-1]["ts"]
                )

                # -----------------------------------------
                # ONLY PROCESS NEW CANDLE
                # -----------------------------------------

                if (
                    last_candle.get(symbol)
                    == candle_time
                ):
                    continue

                last_candle[
                    symbol
                ] = candle_time

                # -----------------------------------------
                # SIGNAL
                # -----------------------------------------

                signal = get_signal(
                    symbol
                )

                update_dashboard(
                    symbol,
                    {
                        **signal,
                        "status":
                            (
                                "SIGNAL"
                                if signal.get(
                                    "signal"
                                )
                                in (
                                    "buy",
                                    "sell"
                                )
                                else "NO TRADE"
                            ),
                        "instrument":
                            info.get(
                                "instId"
                            )
                    }
                )

                # -----------------------------------------
                # LOG ANALYSIS
                # -----------------------------------------

                log(
                    f"{symbol}: "
                    f"SCORE="
                    f"{signal.get('score', 0)}/10 "
                    f"BUY="
                    f"{signal.get('buy_score', 0)} "
                    f"SELL="
                    f"{signal.get('sell_score', 0)} "
                    f"RSI="
                    f"{signal.get('rsi14', 'N/A')} "
                    f"EMA="
                    f"{signal.get('ema20', 'N/A')} "
                    f"ADX="
                    f"{signal.get('adx', 'N/A')} "
                    f"TREND="
                    f"{signal.get('trend15', 'N/A')}"
                )

                # -----------------------------------------
                # VALID SIGNAL
                # -----------------------------------------

                if signal.get(
                    "signal"
                ) in (
                    "buy",
                    "sell"
                ):

                    # prevent same candle duplicate
                    if (
                        last_trade_candle.get(
                            symbol
                        )
                        != candle_time
                    ):

                        log(
                            f"*** VALID SIGNAL *** "
                            f"{symbol} "
                            f"{signal['signal'].upper()} "
                            f"SCORE="
                            f"{signal['score']}/10"
                        )

                        result = (
                            place_demo_order(
                                symbol,
                                signal
                            )
                        )

                        last_trade_candle[
                            symbol
                        ] = candle_time

                        update_dashboard(
                            symbol,
                            {
                                **signal,
                                "status":
                                    result.get(
                                        "status"
                                    ),
                                "order_result":
                                    result
                            }
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
                        f"NO VALID SIGNAL | "
                        f"SCORE="
                        f"{signal.get('score', 0)}/10 | "
                        f"{signal.get('reason', '')}"
                    )

            except Exception as error:

                last_error = str(error)

                log(
                    f"{symbol} ERROR: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                update_dashboard(
                    symbol,
                    {
                        "status":
                            "ERROR",
                        "signal":
                            "none",
                        "score":
                            0,
                        "reason":
                            str(error)
                    }
                )

        time.sleep(
            POLL_SECONDS
        )


# =========================================================
# DASHBOARD HTML
# =========================================================

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width,
      initial-scale=1.0">

<title>OKX Scalping Bot V4</title>

<style>

body {
    font-family: Arial, sans-serif;
    background: #111;
    color: #eee;
    margin: 0;
    padding: 15px;
}

h1 {
    font-size: 22px;
}

.top {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px;
    margin-bottom: 15px;
}

.card {
    background: #1c1c1c;
    border-radius: 10px;
    padding: 12px;
    border: 1px solid #333;
}

.good {
    color: #32e875;
}

.bad {
    color: #ff5555;
}

.warn {
    color: #ffd166;
}

table {
    width: 100%;
    border-collapse: collapse;
    background: #181818;
    font-size: 12px;
}

th, td {
    padding: 8px;
    border: 1px solid #333;
    text-align: center;
}

th {
    background: #252525;
}

.buy {
    color: #32e875;
    font-weight: bold;
}

.sell {
    color: #ff5555;
    font-weight: bold;
}

.none {
    color: #ffd166;
}

.reason {
    max-width: 300px;
    text-align: left;
}

.small {
    font-size: 11px;
    color: #aaa;
}

@media(max-width:700px) {

    table {
        font-size: 10px;
    }

    th, td {
        padding: 5px;
    }
}

</style>

<script>

async function refreshData() {

    try {

        const response =
            await fetch('/api/status');

        const data =
            await response.json();

        document.getElementById(
            'public'
        ).innerText =
            data.public_api
            ? 'CONNECTED'
            : 'NOT CONNECTED';

        document.getElementById(
            'private'
        ).innerText =
            data.private_api
            ? 'CONNECTED'
            : 'NOT CONNECTED';

        document.getElementById(
            'last'
        ).innerText =
            data.last_activity;

        let html = '';

        for (
            const item of data.symbols
        ) {

            let signal =
                item.signal || 'none';

            let cls =
                signal === 'buy'
                ? 'buy'
                : signal === 'sell'
                ? 'sell'
                : 'none';

            html += `
            <tr>

            <td>
                ${item.symbol}
            </td>

            <td class="${cls}">
                ${signal.toUpperCase()}
            </td>

            <td>
                ${item.score || 0}/10
            </td>

            <td>
                ${item.buy_score || 0}
            </td>

            <td>
                ${item.sell_score || 0}
            </td>

            <td>
                ${item.entry || '-'}
            </td>

            <td>
                ${item.rsi14 || '-'}
            </td>

            <td>
                ${item.rsi100 || '-'}
            </td>

            <td>
                ${item.ema20 || '-'}
            </td>

            <td>
                ${item.adx || '-'}
            </td>

            <td>
                ${item.atr_percent || '-'}
            </td>

            <td>
                ${item.volume_ratio || '-'}
            </td>

            <td>
                ${item.trend15 || '-'}
            </td>

            <td class="reason">
                ${item.reason || '-'}
            </td>

            <td>
                ${item.status || '-'}
            </td>

            </tr>
            `;
        }

        document.getElementById(
            'rows'
        ).innerHTML = html;

    } catch (error) {

        console.log(error);

    }
}

setInterval(
    refreshData,
    5000
);

window.onload =
    refreshData;

</script>

</head>

<body>

<h1>
OKX RSI + EMA20 + ADX + ATR +
Volume Scalping V4
</h1>

<div class="top">

<div class="card">
Mode<br>
<strong>
DEMO
</strong>
</div>

<div class="card">
Margin<br>
<strong>
$20
</strong>
</div>

<div class="card">
Leverage<br>
<strong>
5x
</strong>
</div>

<div class="card">
Notional<br>
<strong>
$100
</strong>
</div>

<div class="card">
Public API<br>
<strong id="public">
CHECKING
</strong>
</div>

<div class="card">
Private API<br>
<strong id="private">
CHECKING
</strong>
</div>

</div>

<div class="card">

Last activity:

<strong id="last">
Loading...
</strong>

</div>

<br>

<div style="overflow-x:auto;">

<table>

<thead>

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

</thead>

<tbody id="rows">

<tr>
<td colspan="15">
Loading...
</td>
</tr>

</tbody>

</table>

</div>

<p class="small">

Strategy:
RSI crossover + EMA20 retest +
RSI direction + EMA slope +
ADX + ATR + Volume + 15m trend.

Minimum signal score:
7/10

</p>

</body>
</html>
"""


# =========================================================
# WEB ROUTES
# =========================================================

@app.get("/")
def home():

    return render_template_string(
        HTML
    )


@app.get("/api/status")
def api_status():

    with lock:

        rows = []

        for symbol in SYMBOLS:

            item = dashboard.get(
                symbol,
                {
                    "symbol":
                        symbol,
                    "signal":
                        "waiting",
                    "score":
                        0,
                    "status":
                        "WAITING",
                    "reason":
                        "Waiting for analysis"
                }
            )

            rows.append(
                item
            )

        return jsonify(
            {
                "bot":
                    "OKX RSI EMA20 ADX ATR Volume Scalping V4",
                "demo":
                    DEMO,
                "live_trading":
                    LIVE_TRADING,
                "margin_usdt":
                    str(MARGIN_USDT),
                "leverage":
                    str(LEVERAGE),
                "notional_usdt":
                    str(
                        MARGIN_USDT
                        * LEVERAGE
                    ),
                "timeframe":
                    BAR,
                "trend_timeframe":
                    TREND_BAR,
                "min_score":
                    MIN_SCORE,
                "public_api":
                    public_api_ok,
                "private_api":
                    private_api_ok,
                "status":
                    (
                        "running"
                        if bot_started
                        else "starting"
                    ),
                "last_error":
                    last_error,
                "last_activity":
                    last_activity,
                "symbols":
                    rows
            }
        )


@app.get("/health")
def health():

    return jsonify(
        {
            "status":
                "ok",
            "bot":
                "running"
                if bot_started
                else "starting",
            "demo":
                DEMO,
            "public_api":
                public_api_ok,
            "private_api":
                private_api_ok
        }
    )


# =========================================================
# START
# =========================================================

def start_worker():

    thread = threading.Thread(
        target=worker,
        daemon=True
    )

    thread.start()


if __name__ == "__main__":

    start_worker()

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
